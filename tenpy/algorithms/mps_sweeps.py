"""'Sweep' algorithm and effective Hamiltonians.

Many MPS-based algorithms use a 'sweep' structure, wherein local updates are
performed on the MPS tensors sequentially, first from left to right, then from
right to left. This procedure is common to DMRG, TDVP, sequential time evolution,
etc.

Another common feature of these algorithms is the use of an effective local
Hamiltonian to perform the local updates. The most prominent example of this is
probably DMRG, where the local MPS object is optimized with respect to the rest
of the MPS-MPO-MPS network, the latter forming the effective Hamiltonian.

the :class:`Sweep` class attempts to generalize as many aspects of 'sweeping'
algorithms as possible. :class:`EffectiveH` and its subclasses implement the
effective Hamiltonians mentioned above. Currently, effective Hamiltonians for
1-site and 2-site optimization are implemented.

.. todo ::
    Rebuild TDVP engine as subclasses of sweep
    Do testing
"""
# Copyright 2018 TeNPy Developers, GNU GPLv3

import numpy as np
import time
import warnings

from ..linalg import np_conserved as npc
from ..networks.mps import MPSEnvironment
from ..networks.mpo import MPOEnvironment
from ..linalg.sparse import NpcLinearOperator
from ..tools.params import get_parameter, unused_parameters

__all__ = ['Sweep', 'EffectiveH', 'OneSiteH', 'TwoSiteH']


class Sweep:
    """Prototype class for a 'sweeping' algorithm.

    This is a superclass, intended to cover common procedures in all algorithms that 'sweep'. This
    includes DMRG, TDVP, TEBD, etc. Only DMRG is currently implemented in this way.

    Parameters
    ----------
    psi : :class:`~tenpy.networks.mps.MPS`
        Initial guess for the ground state, which is to be optimized in-place.
    model : :class:`~tenpy.models.MPOModel`
        The model representing the Hamiltonian for which we want to find the ground state.
    engine_params : dict
        Further optional parameters. These are usually algorithm-specific, and thus should be
        described in subclasses.

    Attributes
    ----------
    chi_list : dict | ``None``
        A dictionary to gradually increase the `chi_max` parameter of `trunc_params`. The key
        defines starting from which sweep `chi_max` is set to the value, e.g. ``{0: 50, 20: 100}``
        uses ``chi_max=50`` for the first 20 sweeps and ``chi_max=100`` afterwards. Overwrites
        `trunc_params['chi_list']``. By default (``None``) this feature is disabled.
    combine : bool
        Whether to combine legs into pipes as far as possible. This reduces the overhead of
        calculating charge combinations in the contractions. Makes the two-site DMRG engine
        equivalent to the old `EngineCombine`.
    E_trunc_list : list
        List of truncation energies throughout a sweep.
    env : :class:`~tenpy.networks.mpo.MPOEnvironment`
        Environment for contraction ``<psi|H|psi>``.
    finite : bool
        Whether the MPS boundary conditions are finite (True) or infinite (False)
    i0 : int
        Only set during sweep.
        Left-most of the `EffectiveH.length` sites to be updated in :meth:`update_local`.
    lanczos_params : dict
        Parameters for the Lanczos algorithm.
    mixer : :class:`Mixer` | ``None``
        If ``None``, no mixer is used (anymore), otherwise the mixer instance.
    move_right : bool
        Only set during sweep.
        Whether the next `i0` of the sweep will be right or left of the current one.
    ortho_to_envs : list
        List of environments. Any newly found states will be orthogonalized against these.
    shelve : bool
        If a simulation runs out of time (`time.time() - start_time > max_seconds`), the run will
        terminate with `shelve = True`.
    sweeps : int
        The number of sweeps already performed. (Useful for re-start).
    time0 : float
        Time marker for the start of the run.
    trunc_err_list : list
        List of truncation errors.
    trunc_params : dict
        Parameters for truncations.
    update_LP_RP : (bool, bool)
        Only set during a sweep.
        Whether it is necessary to update the `LP` and `RP`.
        The latter are chosen such that the environment is growing for infinite systems, but
        we only keep the minimal number of environment tensors in memory (inside :attr:`env`).
    verbose : bool | int
        Level of verbosity (i.e. how much status information to print); higher=more output.
    """

    def __init__(self, psi, model, engine_params):
        if not hasattr(self, "EffectiveH"):
            raise NotImplementedError("Subclass needs to set EffectiveH")
        self.psi = psi
        self.engine_params = engine_params
        self.verbose = get_parameter(engine_params, 'verbose', 1, 'Sweep')

        self.combine = get_parameter(engine_params, 'combine', False, 'Sweep')
        self.finite = self.psi.finite
        self.mixer = None  # means 'ignore mixer'; the mixer is activated in in :meth:`run`.

        self.lanczos_params = get_parameter(engine_params, 'lanczos_params', {}, 'Sweep')
        self.lanczos_params.setdefault('verbose', self.verbose / 10)  # reduced verbosity
        self.trunc_params = get_parameter(engine_params, 'trunc_params', {}, 'Sweep')
        self.trunc_params.setdefault('verbose', self.verbose / 10)  # reduced verbosity

        self.env = None
        self.ortho_to_envs = []
        self.init_env(model)
        self.i0 = 0
        self.move_right = True
        self.update_LP_RP = (True, False)

    def __del__(self):
        engine_params = self.engine_params
        unused_parameters(engine_params['lanczos_params'], "Sweep lanczos_params")
        unused_parameters(engine_params['trunc_params'], "Sweep trunc_params")
        if 'mixer_params' in engine_params and engine_params.get('mixer', True):
            unused_parameters(engine_params['mixer_params'], "Sweep mixer_params")
        unused_parameters(engine_params, "Sweep")

    def init_env(self, model=None):
        """(Re-)initialize the environment.

        This function is useful to (re-)start a Sweep with a slightly different
        model or different (engine) parameters. Note that we assume that we
        still have the same `psi`.
        Calls :meth:`reset_stats`.


        Parameters
        ----------
        model : :class:`~tenpy.models.MPOModel`
            The model representing the Hamiltonian for which we want to find the ground state.
            If ``None``, keep the model used before.

        Raises
        ------
        ValueError
            If the engine is re-initialized with a new model, which legs are incompatible with
            those of hte old model.
        """
        H = model.H_MPO if model is not None else self.env.H
        if self.env is None or self.finite:
            LP = get_parameter(self.engine_params, 'LP', None, 'Sweep')
            RP = get_parameter(self.engine_params, 'RP', None, 'Sweep')
            LP_age = get_parameter(self.engine_params, 'LP_age', 0, 'Sweep')
            RP_age = get_parameter(self.engine_params, 'RP_age', 0, 'Sweep')
        else:  # re-initialize
            compatible = True
            if model is not None:
                try:
                    H.get_W(0).get_leg('wL').test_equal(self.env.H.get_W(0).get_leg('wL'))
                except ValueError:
                    compatible = False
                    warnings.warn("The leg of the new model is incompatible with the previous one."
                                  "Rebuild environment from scratch.")
            if compatible:
                LP = self.env.get_LP(0, False)
                LP_age = self.env.get_LP_age(0)
                RP = self.env.get_RP(self.psi.L - 1, False)
                RP_age = self.env.get_RP_age(self.psi.L - 1)
            else:
                LP = get_parameter(self.engine_params, 'LP', None, 'Sweep')
                RP = get_parameter(self.engine_params, 'RP', None, 'Sweep')
                LP_age = get_parameter(self.engine_params, 'LP_age', 0, 'Sweep')
                RP_age = get_parameter(self.engine_params, 'RP_age', 0, 'Sweep')
            if self.engine_params.get('chi_list', None) is not None:
                warnings.warn("Re-using environment with `chi_list` set! Do you want this?")
        self.env = MPOEnvironment(self.psi, H, self.psi, LP, RP, LP_age, RP_age)

        # (re)initialize ortho_to_envs
        orthogonal_to = get_parameter(self.engine_params, 'orthogonal_to', [], 'Sweep')
        if len(orthogonal_to) > 0:
            if not self.finite:
                raise ValueError("Can't orthogonalize for infinite MPS: overlap not well defined.")
            self.ortho_to_envs = [MPSEnvironment(self.psi, ortho) for ortho in orthogonal_to]

        self.reset_stats()

        # initial sweeps of the environment (without mixer)
        if not self.finite:
            print("Initial sweeps...")
            # print(self.engine_params['start_env'])
            start_env = get_parameter(self.engine_params, 'start_env', 1, 'Sweep')
            self.environment_sweeps(start_env)

    def reset_stats(self):
        """Reset the statistics. Useful if you want to start a new Sweep run.

        This method is expected to be overwritten by subclass, and should then
        define self.update_stats and self.sweep_stats dicts consistent with the
        statistics generated by the algorithm particular to that subclass.
        """
        warnings.warn(
            "reset_stats() is not overwritten by the engine. No statistics will be collected!")
        self.sweeps = get_parameter(self.engine_params, 'sweep_0', 0, 'Sweep')
        self.shelve = False
        self.chi_list = get_parameter(self.engine_params, 'chi_list', None, 'Sweep')
        if self.chi_list is not None:
            chi_max = self.chi_list[max([k for k in self.chi_list.keys() if k <= self.sweeps])]
            self.trunc_params['chi_max'] = chi_max
            if self.verbose >= 1:
                print("Setting chi_max =", chi_max)
        self.time0 = time.time()

    def environment_sweeps(self, N_sweeps):
        """Perform `N_sweeps` sweeps without optimization to update the environment.

        Parameters
        ----------
        N_sweeps : int
            Number of sweeps to run without optimization

        Returns
        -------
        None
            Only if asked for <=0 sweeps.
        """
        if N_sweeps <= 0:
            return
        if self.verbose >= 1:
            print("Updating environment")
        for k in range(N_sweeps):
            self.sweep(optimize=False)
            if self.verbose >= 1:
                print('.', end='', flush=True)
        if self.verbose >= 1:
            print("", flush=True)  # end line

    def sweep(self, optimize=True, meas_E_trunc=False):
        """One 'sweep' of a sweeper algorithm.

        Iteratate over the bond which is optimized, to the right and
        then back to the left to the starting point.
        If optimize=False, don't actually diagonalize the effective hamiltonian,
        but only update the environment.

        Parameters
        ----------
        optimize : bool, optional
            Whether we actually optimize to find the ground state of the effective Hamiltonian.
            (If False, just update the environments).
        meas_E_trunc : bool, optional
            Whether to measure truncation energies.

        Returns
        -------
        max_trunc_err : float
            Maximal truncation error introduced.
        max_E_trunc : ``None`` | float
            ``None`` if meas_E_trunc is False, else the maximal change of the energy due to the
            truncation.
        """
        self.E_trunc_list = []
        self.trunc_err_list = []
        schedule = self.get_sweep_schedule()

        # the actual sweep
        for i0, move_right, update_LP_RP in schedule:
            self.i0 = i0
            self.move_right = move_right
            self.update_LP_RP = update_LP_RP
            update_LP, update_RP = update_LP_RP
            if self.verbose >= 10:
                print("in sweep: i0 =", i0)
            # --------- the main work --------------
            theta, theta_ortho = self.prepare_update()
            update_data = self.update_local(theta, theta_ortho, optimize=optimize)
            if update_LP:
                self.update_LP(update_data['U'])  # (requires updated B)
                for o_env in self.ortho_to_envs:
                    o_env.get_LP(i0 + 1, store=True)
            if update_RP:
                self.update_RP(update_data['VH'])
                for o_env in self.ortho_to_envs:
                    o_env.get_RP(i0, store=True)
            self.post_update_local(update_data, meas_E_trunc)

        if optimize:  # count optimization sweeps
            self.sweeps += 1
            if self.chi_list is not None:
                new_chi_max = self.chi_list.get(self.sweeps, None)
                if new_chi_max is not None:
                    self.trunc_params['chi_max'] = new_chi_max
                    if self.verbose >= 1:
                        print("Setting chi_max =", new_chi_max)
            # update mixer
            if self.mixer is not None:
                self.mixer = self.mixer.update_amplitude(self.sweeps)
        if meas_E_trunc:
            return np.max(self.trunc_err_list), np.max(self.E_trunc_list)
        else:
            return np.max(self.trunc_err_list), None

    def get_sweep_schedule(self):
        """Define the schedule of the sweep.

        One 'sweep' is a full sequence from the leftmost site to the right and
        back. Only those `LP` and `RP` that can be used later should be updated.

        Returns
        -------
        schedule : iterable of (int, bool, (bool, bool))
            Schedule for the sweep. Each entry is ``(i0, move_right, (update_LP, update_RP))``,
            where `i0` is the leftmost of the ``self.EffectiveH.length`` sites to be updated in
            :meth:`update_local`, `move_right` indicates whether the next `i0` in the schedule is
            rigth (`True`) of the current one, and `update_LP`, `update_RP` indicate
            whether it is necessary to update the `LP` and `RP`.
            The latter are chosen such that the environment is growing for infinite systems, but
            we only keep the minimal number of environment tensors in memory.
        """
        L = self.psi.L
        if self.finite:
            n = self.EffectiveH.length
            assert L >= n
            i0s = list(range(0, L - n)) + list(range(L - n, 0, -1))
            move_right = [True] * (L - n) + [False] * (L - n)
            update_LP_RP = [[True, False]] * (L - n) + [[False, True]] * (L - n)
        else:
            assert L >= 2
            i0s = list(range(0, L)) + list(range(L, 0, -1))
            move_right = [True] * L + [False] * L
            update_LP_RP = [[True, True]] * 2 + [[True, False]] * (L-2) + \
                           [[True, True]] * 2 + [[False, True]] * (L-2)
        return zip(i0s, move_right, update_LP_RP)

    def get_theta_ortho(self):
        """Get the n-site wavefunctions to orthogonalize against from :attr:`ortho_to_envs`.

        Returns
        -------
        theta_ortho : list of :class:`~tenpy.linalg.np_conserved.Array`
            States to orthogonalize against, with legs 'vL', 'p0', 'p1', 'vR'
            (for EffectiveH.length=1, the 'p1' label is missing).
        """
        i0 = self.i0
        n = self.EffectiveH.length
        theta_ortho = []
        for o_env in self.ortho_to_envs:
            theta = o_env.ket.get_theta(i0, n=n)  # the environments are of the form <psi|ortho>
            LP = o_env.get_LP(i0, store=True)
            RP = o_env.get_RP(i0 + self.EffectiveH.length - 1, store=True)
            theta = npc.tensordot(LP, theta, axes=('vR', 'vL'))
            theta = npc.tensordot(theta, RP, axes=('vR', 'vL'))
            theta.ireplace_labels(['vR*', 'vL*'], ['vL', 'vR'])
            theta_ortho.append(theta)
        return theta_ortho

    def mixer_cleanup(self):
        """Cleanup the effects of a mixer.

        A :meth:`sweep` with an enabled :class:`Mixer` leaves the MPS `psi` with 2D arrays in `S`.
        To recover the originial form, this function simply performs one sweep with disabled mixer.
        """
        if self.mixer is not None:
            mixer = self.mixer
            self.mixer = None  # disable the mixer
            self.sweep(optimize=False)  # (discard return value)
            self.mixer = mixer  # recover the original mixer

    def mixer_activate(self):
        """Set `self.mixer` to the class specified by `engine_params['mixer']`.

        It is expected that different algorithms have differen ways of implementing
        mixers (with different defaults). Thus, this is algorithm-specific.
        """
        raise NotImplementedError("needs to be overwritten by subclass")

    def prepare_update(self):
        """Prepare everything algorithm-specific to perform a local update."""
        raise NotImplementedError("needs to be overwritten by subclass")

    def update_local(self, theta, **kwargs):
        """Perform algorithm-specific local update."""
        raise NotImplementedError("needs to be overwritten by subclass")

    def post_update_local(self, **kwargs):
        """Algorithm-specific actions to be taken after local update, such as
        collecting statistics.
        """
        raise NotImplementedError("needs to be overwritten by subclass")


class EffectiveH(NpcLinearOperator):
    """Prototype class for local effective Hamiltonians used in sweep algorithms.

    As an example, the local effective Hamiltonian for a two-site (DMRG) algorithm
    looks like::

            |        .---       ---.
            |        |    |   |    |
            |       LP----H0--H1---RP
            |        |    |   |    |
            |        .---       ---.

    where ``H0`` and ``H1`` are MPO tensors.

    Parameters
    ----------
    env : :class:`~tenpy.networks.mpo.MPOEnvironment`
        Environment for contraction ``<psi|H|psi>``.
    i0 : int
        Index of the active site if length=1, or of the left-most active site if length>1.
    combine : bool, optional
        Whether to combine legs into pipes as far as possible. This reduces the overhead of
        calculating charge combinations in the contractions.
    move_right : bool, optional
        Whether the sweeping algorithm that calls for an `EffectiveH` is moving to the right.

    Attributes
    ----------
    length : int
        Number of (MPS) sites the effective hamiltonian covers. NB: Class attribute.
    """
    length = None

    def __init__(self, env, i0, combine=False, move_right=True):
        raise NotImplementedError("This function should be implemented in derived classes")

    def matvec(self, theta):
        r"""Apply the effective Hamiltonian to `theta`.

        This function turns :class:`EffectiveH` to a linear operator, which can be
        used for :func:`~tenpy.linalg.lanczos.lanczos`.

        Parameters
        ----------
        theta : :class:`~tenpy.linalg.np_conserved.Array`
            Wave function to apply the effective Hamiltonian to.

        Returns
        -------
        H_theta : :class:`~tenpy.linalg.np_conserved.Array`
            Result of applying the effective Hamiltonian to `theta`, :math:`H |\theta>`.
        """
        raise NotImplementedError("This function should be implemented in derived classes")


class OneSiteH(EffectiveH):
    r"""Class defining the one-site effective Hamiltonian for Lanczos.

    The effective one-site Hamiltonian looks like this::

            |        .---   ---.
            |        |    |    |
            |       LP----W0---RP
            |        |    |    |
            |        .---   ---.

    If `combine` is True, we define either `LHeff` as contraction of `LP` with `W0` (in the case
    `move_right` is True) or `RHeff` as contraction of `RP` and `W0`.

    .. todo ::
        orthogonal theta's? Johannes: agree, might be usefull to add that here.

    Parameters
    ----------
    env : :class:`~tenpy.networks.mpo.MPOEnvironment`
        Environment for contraction ``<psi|H|psi>``.
    i0 : int
        Index of the active site if length=1, or of the left-most active site if length>1.
    combine : bool
        Whether to combine legs into pipes. This combines the virtual and
        physical leg for the left site (when moving right) or right side (when moving left)
        into pipes. This reduces the overhead of calculating charge combinations in the
        contractions, but one :meth:`matvec` is formally more expensive, :math:`O(2 d^3 \chi^3 D)`.
        Is originally from the wo-site method; unclear if it works well for 1 site.
    move_right : bool
        Wheter the the sweep is moving right or left for the next update.

    Attributes
    ----------
    length : int
        Number of (MPS) sites the effective hamiltonian covers.
    combine, move_right : bool
        See above.
    LHeff, RHeff : :class:`~tenpy.linalg.np_conserved.Array`
        Only set :attr:`combine`, and only one of them depending on :attr:`move_right`.
        If `move_right` was True, `LHeff` is set with labels ``'(vR*.p)', 'wR', '(vR.p*)'``
        for bra, MPO, ket; otherwise `RHeff` is set with labels ``'(p*.vL)', 'wL', '(p, vL*)'``
    LP : :class:`tenpy.linalg.np_conserved.Array`
        Left part of the environment.
    RP : :class:`tenpy.linalg.np_conserved.Array`
        Right part of the environment.
    W : :class:`tenpy.linalg.np_conserved.Array`
        MPO tensor, to be applied to the 'p' leg of theta
    """
    length = 1

    def __init__(self, env, i0, combine=False, move_right=True):
        self.LP = env.get_LP(i0)
        self.RP = env.get_RP(i0)
        self.W = env.H.get_W(i0)
        self.combine = combine
        self.move_right = move_right
        if combine:
            self.combine_Heff()

    def matvec(self, theta):
        """Apply the effective Hamiltonian to `theta`.

        Parameters
        ----------
        theta : :class:`~tenpy.linalg.np_conserved.Array`
            Labels: ``vL, p, vR`` if combine=False, ``(vL.p), vR`` or ``vL, (p.vR)`` if True
            (depending on the direction of movement)

        Returns
        -------
        theta :class:`~tenpy.linalg.np_conserved.Array`
            Product of `theta` and the effective Hamiltonian.
        """
        labels = theta.get_leg_labels()
        if self.combine:
            if self.move_right:
                theta = npc.tensordot(self.LHeff, theta, axes=['(vR.p*)', '(vL.p)'])
                # '(vR*.p)', 'wR', 'vR'
                theta = npc.tensordot(theta, self.RP, axes=[['wR', 'vR'], ['wL', 'vL']])
                theta.ireplace_labels(['(vR*.p)', 'vL*'], ['(vL.p)', 'vR'])
            else:
                theta = npc.tensordot(theta, self.RHeff, axes=['(p.vR)', '(p*.vL)'])
                # 'vL', 'wL', '(p.vL*)'
                theta = npc.tensordot(self.LP, theta, axes=[['vR', 'wR'], ['vL', 'wL']])
                theta.ireplace_labels(['vR*', '(p.vL*)'], ['vL', '(p.vR)'])
        else:
            theta = npc.tensordot(self.LP, theta, axes=['vR', 'vL'])
            theta = npc.tensordot(self.W, theta, axes=[['wL', 'p*'], ['wR', 'p']])
            theta = npc.tensordot(theta, self.RP, axes=[['wR', 'vR'], ['wL', 'vL']])
            theta.ireplace_labels(['vR*', 'vL*'], ['vL', 'vR'])
        theta.itranspose(labels)  # if necessary, transpose
        return theta

    def combine_Heff(self):
        """Combine LP and RP with W to form LHeff and RHeff, depending on the direction.

        In a move to the right, we need LHeff. In a move to the left, we need RHeff. Both contain
        the same W.
        """
        # Always compute both L/R, because we might need them. Could change later.
        LHeff = npc.tensordot(self.LP, self.W, axes=['wR', 'wL'])
        self.pipeL = pipeL = LHeff.make_pipe(['vR*', 'p'], qconj=+1)
        self.LHeff = LHeff.combine_legs([['vR*', 'p'], ['vR', 'p*']],
                                        pipes=[pipeL, pipeL.conj()],
                                        new_axes=[0, 2])
        RHeff = npc.tensordot(self.W, self.RP, axes=['wR', 'wL'])
        self.pipeR = pipeR = RHeff.make_pipe(['p', 'vL*'], qconj=-1)
        self.RHeff = RHeff.combine_legs([['p', 'vL*'], ['p*', 'vL']],
                                        pipes=[pipeR, pipeR.conj()],
                                        new_axes=[-1, 0])


class TwoSiteH(EffectiveH):
    r"""Class defining the two-site effective Hamiltonian for Lanczos.

    The effective two-site Hamiltonian looks like this::

            |        .---       ---.
            |        |    |   |    |
            |       LP----W0--W1---RP
            |        |    |   |    |
            |        .---       ---.

    This class defines `LHeff` and `RHeff`, which are the contractions of `LP` with `W0`, and `RP`
    with `W1`, respectively.

    .. todo ::
        orthogonal theta's.

    Parameters
    ----------
    env : :class:`~tenpy.networks.mpo.MPOEnvironment`
        Environment for contraction ``<psi|H|psi>``.
    i0 : int
        Index of the active site if length=1, or of the left-most active site if length>1.
    combine : bool
        Whether to combine legs into pipes. This combines the virtual and
        physical leg for the left site (when moving right) or right side (when moving left)
        into pipes. This reduces the overhead of calculating charge combinations in the
        contractions, but one :meth:`matvec` is formally more expensive, :math:`O(2 d^3 \chi^3 D)`.
        Is originally from the wo-site method; unclear if it works well for 1 site.
    move_right : bool
        Wheter the the sweep is moving right or left for the next update.

    Attributes
    ----------
    combine : bool
        Whether to combine legs into pipes. This combines the virtual and
        physical leg for the left site and right site into pipes. This reduces
        the overhead of calculating charge combinations in the contractions,
        but one :meth:`matvec` is formally more expensive, :math:`O(2 d^3 \chi^3 D)`.
    length : int
        Number of (MPS) sites the effective hamiltonian covers.
    LHeff : :class:`~tenpy.linalg.np_conserved.Array`
        Left part of the effective Hamiltonian.
        Labels ``'(vR*.p0)', 'wR', '(vR.p0*)'`` for bra, MPO, ket.
    RHeff : :class:`~tenpy.linalg.np_conserved.Array`
        Right part of the effective Hamiltonian.
        Labels ``'(vL.p1*)', 'wL', '(vL*.p1)'`` for ket, MPO, bra.
    LP : :class:`~tenpy.linalg.np_conserved.Array`
        Left part of the environment.
    RP : :class:`~tenpy.linalg.np_conserved.Array`
        Right part of the environment
    W1 : :class:`~tenpy.linalg.np_conserved.Array`
        Left MPO tensor, applied to the 'p0' leg of theta
    W2 : :class:`~tenpy.linalg.np_conserved.Array`
        Right MPO tensor, applied to the 'p1' leg of theta
    """
    length = 2

    def __init__(self, env, i0, combine=False, move_right=None):
        self.LP = env.get_LP(i0)
        self.RP = env.get_RP(i0 + 1)
        self.W1 = env.H.get_W(i0).replace_labels(['p', 'p*'], ['p0', 'p0*'])
        # 'wL', 'wR', 'p0', 'p0*'
        self.W2 = env.H.get_W(i0 + 1).replace_labels(['p', 'p*'], ['p1', 'p1*'])
        # 'wL', 'wR', 'p1', 'p1*'
        self.combine = combine
        if combine:
            self.combine_Heff()

    def matvec(self, theta):
        """Apply the effective Hamiltonian to `theta`.

        Parameters
        ----------
        theta : :class:`~tenpy.linalg.np_conserved.Array`
            Labels: ``vL, p0, p1, vR`` if combine=False, ``(vL.p0), (p1.vR)`` if True

        Returns
        -------
        theta :class:`~tenpy.linalg.np_conserved.Array`
            Product of `theta` and the effective Hamiltonian.
        """
        labels = theta.get_leg_labels()
        if self.combine:
            theta = npc.tensordot(self.LHeff, theta, axes=['(vR.p0*)', '(vL.p0)'])
            theta = npc.tensordot(theta, self.RHeff, axes=[['wR', '(p1.vR)'], ['wL', '(p1*.vL)']])
            theta.ireplace_labels(['(vR*.p0)', '(p1.vL*)'], ['(vL.p0)', '(p1.vR)'])
        else:
            theta = npc.tensordot(self.LP, theta, axes=['vR', 'vL'])
            theta = npc.tensordot(self.W1, theta, axes=[['wL', 'p0*'], ['wR', 'p0']])
            theta = npc.tensordot(theta, self.W2, axes=[['wR', 'p1'], ['wL', 'p1*']])
            theta = npc.tensordot(theta, self.RP, axes=[['wR', 'vR'], ['wL', 'vL']])
            theta.ireplace_labels(['vR*', 'vL*'], ['vL', 'vR'])
        theta.itranspose(labels)  # if necessary, transpose
        # This is where we would truncate. Separate mode from combine?
        return theta

    def combine_Heff(self):
        """Combine LP with W1 and RP with W2 to get the effective parts of the
        Hamiltonian with piped legs.
        """
        LHeff = npc.tensordot(self.LP, self.W1, axes=['wR', 'wL'])
        self.pipeL = pipeL = LHeff.make_pipe(['vR*', 'p0'], qconj=+1)
        self.LHeff = LHeff.combine_legs([['vR*', 'p0'], ['vR', 'p0*']],
                                        pipes=[pipeL, pipeL.conj()],
                                        new_axes=[0, 2])
        RHeff = npc.tensordot(self.RP, self.W2, axes=['wL', 'wR'])
        self.pipeR = pipeR = RHeff.make_pipe(['p1', 'vL*'], qconj=-1)
        self.RHeff = RHeff.combine_legs([['p1', 'vL*'], ['p1*', 'vL']],
                                        pipes=[pipeR, pipeR.conj()],
                                        new_axes=[2, 0])
