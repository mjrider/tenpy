"""Density Matrix Renormalization Group (DMRG).

Although it was originally not formulated with tensor networks,
the DMRG algorithm (invented by Steven White in 1992 [White1992]_) opened the whole field
with its enormous success in finding ground states in 1D.

We implement DMRG in the modern formulation of matrix product states [Schollwoeck2011]_,
both for finite systems (``'finite'`` or ``'segment'`` boundary conditions)
and in the thermodynamic limit (``'infinite'`` b.c.).

The function :func:`run` - well - runs one DMRG simulation.
Internally, it generates an instance of an :class:`Sweep`.
This class implements the common functionality like defining a `sweep`,
but leaves the details of the contractions to be performed to the derived classes.

Currently, there are two derived classes implementing the contractions: :class:`SingleSiteDMRGEngine`
and :class:`TwoSiteDMRGEngine`. They differ (as their name implies) in the number of sites which
are optimized simultaneously.
They should both give the same results (up to rounding errors). However, if started from a product
state, :class:`SingleSiteDMRGEngine` depends critically on the use of a :class:`Mixer`, while
:class:`TwoSiteDMRGEngine` is in principle more computationally expensive to run and has
occasionally displayed some convergence issues..
Which one is preffered in the end is not obvious a priori and might depend on the used model.
Just try both of them.

A :class:`Mixer` should be used initially to avoid that the algorithm gets stuck in local energy
minima, and then slowly turned off in the end. For :class:`SingleSiteDMRGEngine`, using a mixer is
crucial, as the one-site algorithm cannot increase the MPS bond dimension by itself.

.. todo ::
    Write UserGuide!!!
"""
# Copyright 2018 TeNPy Developers, GNU GPLv3

import numpy as np
import time
import warnings

from ..linalg import np_conserved as npc
from ..networks.mps import MPSEnvironment
from ..networks.mpo import MPOEnvironment
from ..linalg.lanczos import lanczos
from ..linalg.sparse import NpcLinearOperator
from .truncation import truncate, svd_theta
from ..tools.params import get_parameter, unused_parameters
from ..tools.process import memory_usage
from .mps_sweeps import Sweep, OneSiteH, TwoSiteH

__all__ = [
    'run', 'DMRGEngine', 'SingleSiteDMRGEngine', 'TwoSiteDMRGEngine', 'Engine', 'EngineCombine',
    'EngineFracture', 'Mixer', 'SingleSiteMixer', 'TwoSiteMixer', 'DensityMatrixMixer', 'chi_list'
]


def run(psi, model, DMRG_params):
    r"""Run the DMRG algorithm to find the ground state of the given model.

    Parameters
    ----------
    psi : :class:`~tenpy.networks.mps.MPS`
        Initial guess for the ground state, which is to be optimized in-place.
    model : :class:`~tenpy.models.MPOModel`
        The model representing the Hamiltonian for which we want to find the ground state.
    DMRG_params : dict
        Further optional parameters as described in the following table.
        Use ``verbose>0`` to print the used parameters during runtime.

        ============== ========= ===============================================================
        key            type      description
        ============== ========= ===============================================================
        LP             npc.Array Initial left-most `LP` and right-most `RP` ('left/right part')
        RP                       of the environment. By default (``None``) generate trivial,
                                 see :class:`~tenpy.networks.mpo.MPOEnvironment` for details.
        -------------- --------- ---------------------------------------------------------------
        LP_age         int       The 'age' (i.e. number of physical sites invovled into the
        RP_age                   contraction) of the left-most `LP` and right-most `RP`
                                 of the environment.
        -------------- --------- ---------------------------------------------------------------
        mixer          str |     Chooses the :class:`Mixer` to be used.
                       class |   A string stands for one of the mixers defined in this module,
                       bool      a class is used as custom mixer.
                                 Default (``None``) uses no mixer, ``True`` uses
                                 :class:`DensityMatrixMixer` for the 2-site case and
                                 :class:`SingleSiteMixer` for the 1-site case.
        -------------- --------- ---------------------------------------------------------------
        mixer_params   dict      Non-default initialization arguments of the mixer.
                                 Options may be custom to the specified mixer, so they're
                                 documented in the class doc-string of the mixer.
        -------------- --------- ---------------------------------------------------------------
        orthogonal_to  list of   List of other matrix produc states to orthogonalize against.
                       MPS       Works only for finite systems.
                                 This parameter can be used to find (a few) excited states as
                                 follows. First, run DMRG to find the ground state and then
                                 run DMRG again while orthogonalizing against the ground state,
                                 which yields the first excited state (in the same symmetry
                                 sector), and so on.
        -------------- --------- ---------------------------------------------------------------
        combine        bool      Whether to combine legs into pipes. This combines the virtual and
                                 physical leg for the left site (when moving right) or right side
                                 (when moving left) into pipes. This reduces the overhead of
                                 calculating charge combinations in the contractions, but one
                                 :meth:`matvec` is formally more expensive,
                                 :math:`O(2 d^3 \chi^3 D)`.
        -------------- --------- ---------------------------------------------------------------
        trunc_params   dict      Truncation parameters as described in
                                 :func:`~tenpy.algorithms.truncation.truncate`
        -------------- --------- ---------------------------------------------------------------
        chi_list       dict |    A dictionary to gradually increase the `chi_max` parameter of
                       None      `trunc_params`. The key defines starting from which sweep
                                 `chi_max` is set to the value, e.g. ``{0: 50, 20: 100}`` uses
                                 ``chi_max=50`` for the first 20 sweeps and ``chi_max=100``
                                 afterwards. Overwrites `trunc_params['chi_list']``.
                                 By default (``None``) this feature is disabled.
        -------------- --------- ---------------------------------------------------------------
        lanczos_params dict      Lanczos parameters as described in
                                 :func:`~tenpy.linalg.lanczos.lanczos`
        -------------- --------- ---------------------------------------------------------------
        N_sweeps_check int       Number of sweeps to perform between checking convergence
                                 criteria and giving a status update.
        -------------- --------- ---------------------------------------------------------------
        sweep_0        int       The number of sweeps already performed. (Useful for re-start).
        -------------- --------- ---------------------------------------------------------------
        start_env      int       Number of initial sweeps performed without bond optimizaiton to
                                 initialize the environment.
        -------------- --------- ---------------------------------------------------------------
        update_env     int       Number of sweeps without bond optimizaiton to update the
                                 environment for infinite boundary conditions,
                                 performed every `N_sweeps_check` sweeps.
        -------------- --------- ---------------------------------------------------------------
        norm_tol       float     After the DMRG run, update the environment with at most
                                 `norm_tol_iter` sweeps until
                                 ``np.linalg.norm(psi.norm_err()) < norm_tol``.
        -------------- --------- ---------------------------------------------------------------
        norm_tol_iter  float     Perform at most `norm_tol_iter`*`update_env` sweeps to
                                 converge the norm error below `norm_tol`.
                                 If the state is not converged after that, call
                                 :meth:`~tenpy.networks.mps.canonical_form` instead.
        -------------- --------- ---------------------------------------------------------------
        max_sweeps     int       Maximum number of sweeps to be performed.
        -------------- --------- ---------------------------------------------------------------
        min_sweeps     int       Minimum number of sweeps to be performed.
                                 Defaults to 1.5*N_sweeps_check.
        -------------- --------- ---------------------------------------------------------------
        max_E_err      float     Convergence if the change of the energy in each step
                                 satisfies ``-Delta E / max(|E|, 1) < max_E_err``. Note that
                                 this is also satisfied if ``Delta E > 0``,
                                 i.e., if the energy increases (due to truncation).
        -------------- --------- ---------------------------------------------------------------
        max_S_err      float     Convergence if the relative change of the entropy in each step
                                 satisfies ``|Delta S|/S < max_S_err``
        -------------- --------- ---------------------------------------------------------------
        max_hours      float     If the DMRG took longer (measured in wall-clock time),
                                 'shelve' the simulation, i.e. stop and return with the flag
                                 ``shelve=True``.
        -------------- --------- ---------------------------------------------------------------
        P_tol_to_trunc float     It's reasonable to choose the Lanczos convergence criteria
        P_tol_max                ``'P_tol'`` not many magnitudes lower than the current
        P_tol_min                truncation error. Therefore, if `P_tol_to_trunc` is not
                                 ``None``, we update `P_tol` of `lanczos_params` to
                                 ``max_trunc_err*P_tol_to_trunc``,
                                 restricted to the interval [`P_tol_min`, `P_tol_max`],
                                 where ``max_trunc_err`` is the maximal truncation error
                                 (discarded weight of the Schmidt values) due to truncation
                                 right after each Lanczos optimization during the sweeps.
        -------------- --------- ---------------------------------------------------------------
        E_tol_to_trunc float     It's reasonable to choose the Lanczos convergence criteria
        E_tol_max                ``'E_tol'`` not many magnitudes lower than the current
        E_tol_min                truncation error. Therefore, if `E_tol_to_trunc` is not
                                 ``None``, we update `E_tol` of `lanczos_params` to
                                 ``max_E_trunc*E_tol_to_trunc``,
                                 restricted to the interval [`E_tol_min`, `E_tol_max`],
                                 where ``max_E_trunc`` is the maximal energy difference due to
                                 truncation right after each Lanczos optimization during the
                                 sweeps.
        -------------- --------- ---------------------------------------------------------------
        active_sites   int       The number of active sites to be used by DMRG.
                                 If set to 1, :class:`SingleSiteDMRGEngine` is used.
                                 If set to 2, DMRG is handled by :class:`TwoSiteDMRGEngine`.
        ============== ========= ===============================================================

    Returns
    -------
    info : dict
        A dictionary with keys ``'E', 'shelve', 'bond_statistics', 'sweep_statistics'``
    """
    # initialize the engine
    active_sites = get_parameter(DMRG_params, 'active_sites', 2, 'DMRG')
    if active_sites == 1:
        engine = SingleSiteDMRGEngine(psi, model, DMRG_params)
    elif active_sites == 2:
        engine = TwoSiteDMRGEngine(psi, model, DMRG_params)
    else:
        raise ValueError("For DMRG, can only use 1 or 2 active sites, not {}".format(active_sites))
    E, _ = engine.run()
    return {
        'E': E,
        'shelve': engine.shelve,
        'bond_statistics': engine.update_stats,
        'sweep_statistics': engine.sweep_stats
    }


class DMRGEngine(Sweep):
    """ Generic 'Engine' for the single-site DMRG algorithm.

    This engine is implemented as a subclass of
    :class:`~tenpy.algorithms.mps_sweeps.Sweep`. It contains all methods that
    are generic between :class:`SingleSiteDMRGEngine` and :class:`TwoSiteDMRGEngine`.

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
    EffectiveH : class type
        Class for the effective Hamiltonian (i.e., a subclass of
        :class:`~tenpy.algorithms.mps_sweeps.EffectiveH`. Has a `length` class attribute which
        specifies the number of sites updated at once (e.g., whether we do single-site vs. two-site
        DMRG).
    chi_list : dict | ``None``
        A dictionary to gradually increase the `chi_max` parameter of `trunc_params`. The key
        defines starting from which sweep `chi_max` is set to the value, e.g. ``{0: 50, 20: 100}``
        uses ``chi_max=50`` for the first 20 sweeps and ``chi_max=100`` afterwards. Overwrites
        `trunc_params['chi_list']``. By default (``None``) this feature is disabled.
    eff_H : :class:`~tenpy.algorithms.mps_sweeps.EffectiveH`
        Effective two-site Hamiltonian.
    mixer : :class:`Mixer` | ``None``
        If ``None``, no mixer is used (anymore), otherwise the mixer instance.
    shelve : bool
        If a simulation runs out of time (`time.time() - start_time > max_seconds`), the run will
        terminate with `shelve = True`.
    sweep_stats : dict
        Statistics at sweep-level.
    sweeps : int
        The number of sweeps already performed. (Useful for re-start).
    time0 : float
        Time marker for the start of the run.
    update_stats : dict
        Statistics at local update-level.
    """

    def run(self):
        """Run the DMRG simulation to find the ground state.

        Returns
        -------
        E : float
            The energy of the resulting ground state MPS.
        psi : :class:`~tenpy.networks.mps.MPS`
            The MPS representing the ground state after the simluation,
            i.e. just a reference to :attr:`psi`.
        """
        DMRG_params = self.engine_params
        start_time = self.time0
        self.shelve = False
        # parameters for lanczos
        p_tol_to_trunc = get_parameter(DMRG_params, 'P_tol_to_trunc', 0.05, 'DMRG')
        if p_tol_to_trunc is not None:
            p_tol_min = get_parameter(DMRG_params, 'P_tol_min', 5.e-16, 'DMRG')
            p_tol_max = get_parameter(DMRG_params, 'P_tol_max', 1.e-4, 'DMRG')
        e_tol_to_trunc = get_parameter(DMRG_params, 'E_tol_to_trunc', None, 'DMRG')
        if e_tol_to_trunc is not None:
            e_tol_min = get_parameter(DMRG_params, 'E_tol_min', 5.e-16, 'DMRG')
            e_tol_max = get_parameter(DMRG_params, 'E_tol_max', 1.e-4, 'DMRG')

        # parameters for DMRG convergence criteria
        N_sweeps_check = get_parameter(DMRG_params, 'N_sweeps_check', 10, 'DMRG')
        min_sweeps = get_parameter(DMRG_params, 'min_sweeps', int(1.5 * N_sweeps_check), 'DMRG')
        max_sweeps = get_parameter(DMRG_params, 'max_sweeps', 1000, 'DMRG')
        max_E_err = get_parameter(DMRG_params, 'max_E_err', 1.e-8, 'DMRG')
        max_S_err = get_parameter(DMRG_params, 'max_S_err', 1.e-5, 'DMRG')
        max_seconds = 3600 * get_parameter(DMRG_params, 'max_hours', 24 * 365, 'DMRG')
        norm_tol = get_parameter(DMRG_params, 'norm_tol', 1.e-5, 'DMRG')
        if not self.finite:
            update_env = get_parameter(DMRG_params, 'update_env', N_sweeps_check // 2, 'DMRG')
            norm_tol_iter = get_parameter(DMRG_params, 'norm_tol_iter', 5, 'DMRG')
        E_old, S_old = np.nan, np.nan  # initial dummy values
        E, Delta_E, Delta_S = 1., 1., 1.

        self.mixer_activate()
        # loop over sweeps
        while True:
            # check convergence criteria
            if self.sweeps >= max_sweeps:
                break
            if (self.sweeps > min_sweeps and -Delta_E < max_E_err * max(abs(E), 1.)
                    and abs(Delta_S) < max_S_err):
                if self.mixer is None:
                    break
                else:
                    if self.verbose >= 1:
                        print("Convergence criterium reached with enabled mixer.\n"
                              "disable mixer and continue")
                        self.mixer = None
            if time.time() - start_time > max_seconds:
                self.shelve = True
                warnings.warn("DMRG: maximum time limit reached. Shelve simulation.")
                break
            # --------- the main work --------------
            for i in range(N_sweeps_check - 1):
                self.sweep(meas_E_trunc=False)
            max_trunc_err, max_E_trunc = self.sweep(meas_E_trunc=True)
            # --------------------------------------
            # update lancos_params depending on truncation error(s)
            if p_tol_to_trunc is not None and max_trunc_err > p_tol_min:
                self.lanczos_params['P_tol'] = max(p_tol_min,
                                                   min(p_tol_max, max_trunc_err * p_tol_to_trunc))
            if e_tol_to_trunc is not None and max_E_trunc > e_tol_min:
                self.lanczos_params['E_tol'] = max(e_tol_min,
                                                   min(e_tol_max, max_E_trunc * e_tol_to_trunc))
            # update environment
            if not self.finite:
                self.environment_sweeps(update_env)

            # update values for checking the convergence
            try:
                S = np.average(self.psi.entanglement_entropy())
                Delta_S = (S - S_old) / N_sweeps_check
            except ValueError:
                # with a mixer, psi._S can be 2D arrays s.t. entanglement_entropy() fails
                S = np.nan
                Delta_S = 0.
            S_old = S
            if not self.finite:  # iDMRG: need energy density
                Es = self.update_stats['E_total']
                age = self.update_stats['age']
                delta = min(1 + 2 * self.env.L, len(age))
                growth = (age[-1] - age[-delta])
                E = (Es[-1] - Es[-delta]) / growth
            else:
                E = self.update_stats['E_total'][-1]
            Delta_E = (E - E_old) / N_sweeps_check
            E_old = E
            norm_err = np.linalg.norm(self.psi.norm_test())

            # update statistics
            self.sweep_stats['sweep'].append(self.sweeps)
            self.sweep_stats['E'].append(E)
            self.sweep_stats['S'].append(S)
            self.sweep_stats['time'].append(time.time() - start_time)
            self.sweep_stats['max_trunc_err'].append(max_trunc_err)
            self.sweep_stats['max_E_trunc'].append(max_E_trunc)
            self.sweep_stats['max_chi'].append(np.max(self.psi.chi))
            self.sweep_stats['norm_err'].append(norm_err)

            # print status update
            if self.verbose >= 1:
                print("=" * 80)
                msg = ("sweep {sweep:d}, age = {age:d}\n"
                       "Energy = {E:.16f}, S = {S:.16f}, norm_err = {norm_err:.1e}\n"
                       "Current memory usage {mem:.1f} MB, time elapsed: {time:.1f} s\n"
                       "Delta E = {DE:.4e}, Delta S = {DS:.4e} (per sweep)\n"
                       "max_trunc_err = {trerr:.4e}, max_E_trunc = {Eerr:.4e}\n"
                       "MPS bond dimensions: {chi!s}")
                msg = msg.format(sweep=self.sweeps,
                                 mem=memory_usage(),
                                 time=time.time() - start_time,
                                 chi=self.psi.chi,
                                 age=self.update_stats['age'][-1],
                                 E=E,
                                 S=S,
                                 DE=Delta_E,
                                 DS=Delta_S,
                                 trerr=max_trunc_err,
                                 Eerr=max_E_trunc,
                                 norm_err=norm_err)
                print(msg, flush=True)

        # clean up from mixer
        self.mixer_cleanup()
        # update environment until norm_tol is reached
        if norm_tol is not None and norm_err > norm_tol:
            msg = "final DMRG state not in canonical form within `norm_tol` = {nt:.2e}"
            warnings.warn(msg.format(nt=norm_tol))
            if self.verbose >= 1:
                print("norm_tol={nt:.2e} not reached, norm_err={ne:.2e}".format(nt=norm_tol,
                                                                                ne=norm_err))
            if self.finite:
                self.psi.canonical_form()
            else:
                for _ in range(norm_tol_iter):
                    self.environment_sweeps(update_env)
                    norm_err = np.linalg.norm(self.psi.norm_test())
                    if norm_err <= norm_tol:
                        break
                else:
                    if self.verbose >= 1:
                        msg = ("DMRG: norm_tol {nt:.2e} not reached by updating the environment, "
                               "current norm_err = {ne:.2e}\n"
                               "Call psi.canonical_form()").format(nt=norm_tol, ne=norm_err)
                        print(msg)
                    self.psi.canonical_form()
        if self.verbose >= 1:
            print("=" * 80)
            msg = ("DMRG finished after {sweep:d} sweeps.\n"
                   "total size = {age:d}, maximum chi = {chimax:d}")
            print(
                msg.format(sweep=self.sweeps,
                           age=self.update_stats['age'][-1],
                           chimax=np.max(self.psi.chi)))
            print("=" * 80)
        return E, self.psi

    def reset_stats(self):
        """Reset the statistics. Useful if you want to start a new Sweep run.
        """
        self.sweeps = get_parameter(self.engine_params, 'sweep_0', 0, 'Sweep')
        self.update_stats = {
            'i0': [],
            'age': [],
            'E_total': [],
            'N_lanczos': [],
            'time': [],
            'err': [],
            'E_trunc': []
        }
        self.sweep_stats = {
            'sweep': [],
            'E': [],
            'S': [],
            'time': [],
            'max_trunc_err': [],
            'max_E_trunc': [],
            'max_chi': [],
            'norm_err': []
        }
        self.chi_list = get_parameter(self.engine_params, 'chi_list', None, 'Sweep')
        if self.chi_list is not None:
            chi_max = self.chi_list[max([k for k in self.chi_list.keys() if k <= self.sweeps])]
            self.trunc_params['chi_max'] = chi_max
            if self.verbose >= 1:
                print("Setting chi_max =", chi_max)
        self.time0 = time.time()

    def post_update_local(self, update_data, meas_E_trunc=False):
        """Perform post-update actions.

        Compute truncation energy, remove `LP`/`RP` that are no longer needed and collect
        statistics.

        Parameters
        ----------
        update_data : dict
            Data computed during the local update, as described in the following list.
        meas_E_trunc : bool, optional
            Wheter to measure the energy after truncation.
        """
        E0 = update_data['E0']
        i0 = self.i0
        E_trunc = None
        if meas_E_trunc or E0 is None:
            E_trunc = self.env.full_contraction(i0).real  # uses updated LP/RP (if calculated)
            if E0 is None:
                E0 = E_trunc
            E_trunc = E_trunc - E0
        # now we can also remove the LP and RP on outer bonds, which we don't need any more
        if self.EffectiveH.length == 2:
            # TODO: Do we need those for single site DMRG? In infinite case?
            update_LP, update_RP = self.update_LP_RP
            if update_RP:  # we move to the left -> delete left LP
                self.env.del_LP(i0)
                for o_env in self.ortho_to_envs:
                    o_env.del_LP(i0)
            if update_LP:  # we move to the right -> delete right RP
                self.env.del_RP(i0 + 1)  # Always +1, even in single site.
                for o_env in self.ortho_to_envs:
                    o_env.del_RP(i0 + 1)

        # collect statistics
        self.update_stats['i0'].append(i0)
        self.update_stats['age'].append(update_data['age'])
        self.update_stats['E_total'].append(E0)
        self.update_stats['E_trunc'].append(E_trunc)
        self.update_stats['N_lanczos'].append(update_data['N'])
        self.update_stats['err'].append(update_data['err'])
        self.update_stats['time'].append(time.time() - self.time0)
        self.trunc_err_list.append(update_data['err'].eps)
        self.E_trunc_list.append(E_trunc)

    def diag(self, theta_guess, theta_ortho):
        """Diagonalize the effective Hamiltonian represented by self.

        Parameters
        ----------
        theta_guess : :class:`~tenpy.linalg.np_conserved.Array`
            Initial guess for the ground state of the effective Hamiltonian.
        theta_ortho : list of :class:`~tenpy.linalg.np_conserved.Array`
            States to orthogonalize against, with same tensor structure as `theta_guess`.

        Returns
        -------
        E0 : float
            Energy of the found ground state.
        theta : :class:`~tenpy.linalg.np_conserved.Array`
            Ground state of the effective Hamiltonian.
        N : int
            Number of Lanczos iterations used.
        """
        E, theta, N = lanczos(self.eff_H, theta_guess, self.lanczos_params, theta_ortho)
        return E, theta, N

    def plot_update_stats(self, axes, xaxis='time', yaxis='E', y_exact=None, **kwargs):
        """Plot :attr:`update_stats` to display the convergence during the sweeps.

        Parameters
        ----------
        axes : :class:`matplotlib.axes.Axes`
            The axes to plot into. Defaults to :func:`matplotlib.pyplot.gca()`
        xaxis : ``'index' | 'sweep'`` | keys of :attr:`update_stats`
            Key of :attr:`update_stats` to be used for the x-axis of the plots.
            ``'index'`` is just enumerating the number of bond updates,
            and ``'sweep'`` corresponds to the sweep number (including environment sweeps).
        yaxis : ``'E'`` | keys of :attr:`update_stats`
            Key of :attr:`update_stats` to be used for the y-axisof the plots.
            For 'E', use the energy (per site for infinite systems).
        y_exact : float
            Exact value for the quantity on the y-axis for comparison.
            If given, plot ``abs((y-y_exact)/y_exact)`` on a log-scale yaxis.
        **kwargs :
            Further keyword arguments given to ``axes.plot(...)``.
        """
        if axes is None:
            import matplotlib.pyplot as plt
            axes = plt.gca()
        stats = self.update_stats
        L = self.psi.L
        kwargs.setdefault('marker', 'x')
        kwargs.setdefault('linestyle', '-')

        E = np.array(stats['E_total'])
        schedule, _ = self._get_sweep_schedule()
        N = len(schedule)  # bond updates per sweep
        if xaxis is None or xaxis == 'index':
            xaxis = 'index'
            x = np.arange(len(E))
        elif xaxis == 'sweep':
            x = np.arange(1, len(E) + 1) / N
        else:
            x = np.array(stats[xaxis])
        if yaxis == 'E':
            if not self.psi.finite:
                # use energy per site instead of total energy
                age = np.array(stats['age'])
                d_age = age[N:] - age[:-N]
                d_E = E[N:] - E[:-N]
                y = d_E / d_age
                x = x[N:]
            else:
                y = E
        else:
            y = np.array(stats[yaxis])
        if y_exact is not None:
            y = np.abs(y - y_exact) / np.abs(y_exact)
            axes.set_yscale('log')
        axes.plot(x, y, **kwargs)
        axes.set_xlabel(xaxis)
        axes.set_ylabel(yaxis)

    def plot_sweep_stats(self, axes=None, xaxis='time', yaxis='E', y_exact=None, **kwargs):
        """Plot :attr:`sweep_stats` to display the convergence with the sweeps.

        Parameters
        ----------
        axes : :class:`matplotlib.axes.Axes`
            The axes to plot into. Defaults to :func:`matplotlib.pyplot.gca()`
        xaxis, yaxis : key of :attr:`sweep_stats`
            Key of :attr:`sweep_stats` to be used for the x-axis and y-axis of the plots.
        y_exact : float
            Exact value for the quantity on the y-axis for comparison.
            If given, plot ``abs((y-y_exact)/y_exact)`` on a log-scale yaxis.
        **kwargs :
            Further keyword arguments given to ``axes.plot(...)``.
        """
        if axes is None:
            import matplotlib.pyplot as plt
            axes = plt.gca()
        stats = self.sweep_stats
        L = self.psi.L
        kwargs.setdefault('marker', 'x')
        kwargs.setdefault('linestyle', '-')

        x = np.array(stats[xaxis])
        y = np.array(stats[yaxis])
        if y_exact is not None:
            y = np.abs(y - y_exact) / np.abs(y_exact)
            axes.set_yscale('log')
        axes.plot(x, y, **kwargs)
        axes.set_xlabel(xaxis)
        axes.set_ylabel(yaxis)


class TwoSiteDMRGEngine(DMRGEngine):
    """'Engine' for the two-site DMRG algorithm.

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
    EffectiveH : class type
        Class for the effective Hamiltonian (i.e., a subclass of
        :class:`~tenpy.algorithms.mps_sweeps.EffectiveH`. Has a `length` class attribute which
        specifies the number of sites updated at once (e.g., whether we do single-site vs. two-site
        DMRG).
    chi_list : dict | ``None``
        A dictionary to gradually increase the `chi_max` parameter of `trunc_params`. The key
        defines starting from which sweep `chi_max` is set to the value, e.g. ``{0: 50, 20: 100}``
        uses ``chi_max=50`` for the first 20 sweeps and ``chi_max=100`` afterwards. Overwrites
        `trunc_params['chi_list']``. By default (``None``) this feature is disabled.
    eff_H : :class:`~tenpy.algorithms.mps_sweeps.EffectiveH`
        Effective two-site Hamiltonian.
    mixer : :class:`Mixer` | ``None``
        If ``None``, no mixer is used (anymore), otherwise the mixer instance.
    shelve : bool
        If a simulation runs out of time (`time.time() - start_time > max_seconds`), the run will
        terminate with `shelve = True`.
    sweeps : int
        The number of sweeps already performed. (Useful for re-start).
    time0 : float
        Time marker for the start of the run.
    update_stats : dict
        A dictionary with detailed statistics of the convergence.
        For each key in the following table, the dictionary contains a list where one value is
        added each time :meth:`Engine.update_bond` is called.

        =========== ===================================================================
        key         description
        =========== ===================================================================
        i0          An update was performed on sites ``i0, i0+1``.
        ----------- -------------------------------------------------------------------
        age         The number of physical sites involved in the simulation.
        ----------- -------------------------------------------------------------------
        E_total     The total energy before truncation.
        ----------- -------------------------------------------------------------------
        N_lanczos   Dimension of the Krylov space used in the lanczos diagonalization.
        ----------- -------------------------------------------------------------------
        time        Wallclock time evolved since :attr:`time0` (in seconds).
        =========== ===================================================================

    sweep_stats : dict
        A dictionary with detailed statistics of the convergence.
        For each key in the following table, the dictionary contains a list where one value is
        added each time :meth:`Engine.sweep` is called (with ``optimize=True``).

        ============= ===================================================================
        key           description
        ============= ===================================================================
        sweep         Number of sweeps performed so far.
        ------------- -------------------------------------------------------------------
        E             The energy *before* truncation (as calculated by Lanczos).
        ------------- -------------------------------------------------------------------
        S             Maximum entanglement entropy.
        ------------- -------------------------------------------------------------------
        time          Wallclock time evolved since :attr:`time0` (in seconds).
        ------------- -------------------------------------------------------------------
        max_trunc_err The maximum truncation error in the last sweep
        ------------- -------------------------------------------------------------------
        max_E_trunc   Maximum change or Energy due to truncation in the last sweep.
        ------------- -------------------------------------------------------------------
        max_chi       Maximum bond dimension used.
        ------------- -------------------------------------------------------------------
        norm_err      Error of canonical form ``np.linalg.norm(psi.norm_test())``.
        ============= ===================================================================

    """

    def __init__(self, psi, model, engine_params):
        self.EffectiveH = TwoSiteH
        super(TwoSiteDMRGEngine, self).__init__(psi, model, engine_params)

    def prepare_update(self):
        """Prepare `self` to represent the effective Hamiltonian on sites ``(i0, i0+1)``.

        Returns
        -------
        theta : :class:`~tenpy.linalg.np_conserved.Array`
            Current best guess for the ground state, which is to be optimized.
            Labels ``'vL', 'p0', 'vR', 'p1'``.
        theta_ortho : list of :class:`~tenpy.linalg.np_conserved.Array`
            States (also with labels ``'vL', 'p0', 'vR', 'p1'``) to orthogonalize against,
            c.f. see :meth:`get_theta_ortho`.
        """
        EffectiveH = self.EffectiveH
        env = self.env
        eff_H = EffectiveH(env, self.i0, self.combine)  # eff_H has attributes LP, RP, W1, W2.
        self.eff_H = eff_H

        # make theta
        cutoff = 1.e-16 if self.mixer is None else 1.e-8
        theta = self.psi.get_theta(self.i0, n=2, cutoff=cutoff)  # 'vL', 'p0', 'p1', 'vR'
        theta_ortho = self.get_theta_ortho()
        if self.combine:
            theta = theta.combine_legs([['vL', 'p0'], ['p1', 'vR']],
                                       pipes=[eff_H.pipeL, eff_H.pipeR])
            theta_ortho = [
                th_o.combine_legs([['vL', 'p0'], ['p1', 'vR']], pipes=[eff_H.pipeL, eff_H.pipeR])
                for th_o in theta_ortho
            ]
        else:
            theta.itranspose(['vL', 'p0', 'p1', 'vR'])
            for th_o in theta_ortho:
                th_o.itranspose(['vL', 'p0', 'p1', 'vR'])
        return theta, theta_ortho

    def update_local(self, theta, theta_ortho, optimize=True, meas_E_trunc=False):
        """Perform bond-update on the sites ``(i0, i0+1)``.

        Parameters
        ----------
        theta : :class:`~tenpy.linalg.np_conserved.Array`
            Initial guess for the ground state of the effective Hamiltonian.
        theta_ortho : list of :class:`~tenpy.linalg.np_conserved.Array`
            States to orthogonalize against, with same tensor structure as `theta`.
        optimize : bool, optional
            Wheter we actually optimize to find the ground state of the effective Hamiltonian.
            (If False, just update the environments).
        meas_E_trunc : bool, optional
            Wheter to measure the energy after truncation.

        Returns
        -------
        update_data : dict
            Data computed during the local update, as described in the following list.

            E_total : float
                Total energy, obtained *before* truncation (if ``optimize=True``),
                or *after* truncation (if ``optimize=False``) (but never ``None``).
            E_trunc : float | ``None``
                The energy difference of the total energy after minus before truncation,
                ``E_truncated - E_total``. ``None`` if ``meas_E_trunc=False``.
            err : :class:`~tenpy.algorithms.truncation.TruncationError`
                The truncation error introduced after bond optimization.
            N_lanczos : int
                Dimension of the Krylov space used for optimization in the lanczos algorithm.
                0 if ``optimize=False``.
            age : int
                Current size of the DMRG simulation: number of physical sites involved
                into the contraction.
        """
        i0 = self.i0
        age = self.env.get_LP_age(i0) + 2 + self.env.get_RP_age(i0 + 1)
        if optimize:
            E0, theta, N = self.diag(theta, theta_ortho)
        else:
            E0, N = None, 0
        theta = self.prepare_svd(theta)
        U, S, VH, err = self.mixed_svd(theta)
        self.set_B(U, S, VH)
        update_data = {
            'E0': E0,
            'err': err,
            'N': N,
            'age': age,
            'U': U,
            'VH': VH,
        }
        return update_data

    def prepare_svd(self, theta):
        """Transform theta into matrix for svd."""
        if self.combine:
            return theta  # Theta is already combined.
        else:
            return theta.combine_legs([['vL', 'p0'], ['p1', 'vR']],
                                      new_axes=[0, 1],
                                      qconj=[+1, -1])

    def mixed_svd(self, theta):
        """Get (truncated) `B` from the new theta (as returned by diag).

        The goal is to split theta and truncate it::

            |   -- theta --   ==>    -- U -- S --  VH -
            |      |   |                |          |

        Without a mixer, this is done by a simple svd and truncation of Schmidt values.

        With a mixer, the state is perturbed before the SVD.
        The details of the perturbation are defined by the :class:`Mixer` class.

        Note that the returned `S` is a general (not diagonal) matrix, with labels ``'vL', 'vR'``.

        Parameters
        ----------
        theta : :class:`~tenpy.linalg.np_conserved.Array`
            The optimized wave function, prepared for svd.

        Returns
        -------
        U : :class:`~tenpy.linalg.np_conserved.Array`
            Left-canonical part of `theta`. Labels ``'(vL.p0)', 'vR'``.
        S : 1D ndarray | 2D :class:`~tenpy.linalg.np_conserved.Array`
            Without mixer just the singluar values of the array; with mixer it might be a general
            matrix with labels ``'vL', 'vR'``; see comment above.
        VH : :class:`~tenpy.linalg.np_conserved.Array`
            Right-canonical part of `theta`. Labels ``'vL', '(p1.vR)'``.
        err : :class:`~tenpy.algorithms.truncation.TruncationError`
            The truncation error introduced.
        """
        i0 = self.i0
        # get qtotal_LR from i0
        if self.mixer is None:
            # simple case: real svd, defined elsewhere.
            qtotal_i0 = self.env.bra.get_B(i0, form=None).qtotal
            U, S, VH, err, _ = svd_theta(theta,
                                         self.trunc_params,
                                         qtotal_LR=[qtotal_i0, None],
                                         inner_labels=['vR', 'vL'])
            return U, S, VH, err
        update_LP, update_RP = self.update_LP_RP
        return self.mixer.perturb_svd(self, theta, self.i0, update_LP, update_RP)

    def set_B(self, U, S, VH):
        """Update the MPS with the ``U, S, VH`` returned by `self.mixed_svd`.

        Parameters
        ----------
        U, VH : :class:`~tenpy.linalg.np_conserved.Array`
            Left and Right-canonical matrices as returned by the SVD.
        S : 1D array | 2D :class:`~tenpy.linalg.np_conserved.Array`
            The middle part returned by the SVD, ``theta = U S VH``.
            Without a mixer just the singular values, with enabled `mixer` a 2D array.
        """
        B0 = U.split_legs(['(vL.p0)']).replace_label('p0', 'p')
        B1 = VH.split_legs(['(p1.vR)']).replace_label('p1', 'p')
        i0 = self.i0
        self.psi.set_B(i0, B0, form='A')  # left-canonical
        self.psi.set_B(i0 + 1, B1, form='B')  # right-canonical
        self.psi.set_SR(i0, S)
        # the old stored environments are now invalid
        # => delete them to ensure that they get calculated again in :meth:`update_LP` / RP
        for o_env in self.ortho_to_envs:
            o_env.del_LP(i0 + 1)
            o_env.del_RP(i0)
        self.env.del_LP(i0 + 1)
        self.env.del_RP(i0)

    def mixer_activate(self):
        """Set `self.mixer` to the class specified by `engine_params['mixer']`.
        """
        Mixer_class = get_parameter(self.engine_params, 'mixer', None, 'Sweep')
        if Mixer_class:
            if Mixer_class is True:
                Mixer_class = DensityMatrixMixer
            if isinstance(Mixer_class, str):
                if Mixer_class == "Mixer":
                    msg = ('Use `True` or `"DensityMatrixMixer"` instead of "Mixer" '
                           'for Sweep parameter "mixer"')
                    warnings.warn(msg, FutureWarning)
                    Mixer = "DensityMatrixMixer"
                Mixer_class = globals()[Mixer_class]
            mixer_params = get_parameter(self.engine_params, 'mixer_params', {}, 'Sweep')
            mixer_params.setdefault('verbose', self.verbose / 10)  # reduced verbosity
            self.mixer = Mixer_class(mixer_params)

    def update_LP(self, U):
        """Update left part of the environment.

        We always update the environment at site i0 + 1: this environment then contains the site
        where we just performed a local update (when sweeping right).

        Parameters
        ----------
        U : :class:`~tenpy.linalg.np_conserved.Array`
            The U as returned by the SVD, with combined legs, labels ``'vL.p0', 'vR'``.
        """
        i0 = self.i0
        if self.combine:
            LP = npc.tensordot(self.eff_H.LHeff, U, axes=['(vR.p0*)', '(vL.p0)'])
            LP = npc.tensordot(U.conj(), LP, axes=['(vL*.p0*)', '(vR*.p0)'])
            self.env.set_LP(i0 + 1, LP, age=self.env.get_LP_age(i0) + 1)  # Always i0 + 1
        else:  # as implemented directly in the environment
            self.env.get_LP(i0 + 1, store=True)

    def update_RP(self, VH):
        """Update right part of the environment.

        We always update the environment at site i0: this environment then contains the site
        where we just performed a local update (when sweeping left).

        Parameters
        ----------
        VH : :class:`~tenpy.linalg.np_conserved.Array`
            The VH as returned by SVD, with combined legs, labels ``'vL', '(vR.p1)'``.
        """
        i0 = self.i0
        if self.combine:
            RP = npc.tensordot(VH, self.eff_H.RHeff, axes=['(p1.vR)', '(p1*.vL)'])
            RP = npc.tensordot(RP, VH.conj(), axes=['(p1.vL*)', '(p1*.vR*)'])
            self.env.set_RP(i0, RP, age=self.env.get_RP_age(i0 + self.EffectiveH.length - 1) + 1)
        else:  # as implemented directly in the environment
            self.env.get_RP(i0, store=True)


class SingleSiteDMRGEngine(DMRGEngine):
    """'Engine' for the single-site DMRG algorithm.

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
    EffectiveH : class type
        Class for the effective Hamiltonian (i.e., a subclass of
        :class:`~tenpy.algorithms.mps_sweeps.EffectiveH`. Has a `length` class attribute which
        specifies the number of sites updated at once (e.g., whether we do single-site vs. two-site
        DMRG).
    chi_list : dict | ``None``
        A dictionary to gradually increase the `chi_max` parameter of `trunc_params`. The key
        defines starting from which sweep `chi_max` is set to the value, e.g. ``{0: 50, 20: 100}``
        uses ``chi_max=50`` for the first 20 sweeps and ``chi_max=100`` afterwards. Overwrites
        `trunc_params['chi_list']``. By default (``None``) this feature is disabled.
    eff_H : :class:`~tenpy.algorithms.mps_sweeps.EffectiveH`
        Effective two-site Hamiltonian.
    mixer : :class:`Mixer` | ``None``
        If ``None``, no mixer is used (anymore), otherwise the mixer instance.
    shelve : bool
        If a simulation runs out of time (`time.time() - start_time > max_seconds`), the run will
        terminate with `shelve = True`.
    sweeps : int
        The number of sweeps already performed. (Useful for re-start).
    time0 : float
        Time marker for the start of the run.
    update_stats : dict
        A dictionary with detailed statistics of the convergence.
        For each key in the following table, the dictionary contains a list where one value is
        added each time :meth:`Engine.update_bond` is called.

        =========== ===================================================================
        key         description
        =========== ===================================================================
        i0          An update was performed on sites ``i0, i0+1``.
        ----------- -------------------------------------------------------------------
        age         The number of physical sites involved in the simulation.
        ----------- -------------------------------------------------------------------
        E_total     The total energy before truncation.
        ----------- -------------------------------------------------------------------
        N_lanczos   Dimension of the Krylov space used in the lanczos diagonalization.
        ----------- -------------------------------------------------------------------
        time        Wallclock time evolved since :attr:`time0` (in seconds).
        =========== ===================================================================

    sweep_stats : dict
        A dictionary with detailed statistics of the convergence.
        For each key in the following table, the dictionary contains a list where one value is
        added each time :meth:`Engine.sweep` is called (with ``optimize=True``).

        ============= ===================================================================
        key           description
        ============= ===================================================================
        sweep         Number of sweeps performed so far.
        ------------- -------------------------------------------------------------------
        E             The energy *before* truncation (as calculated by Lanczos).
        ------------- -------------------------------------------------------------------
        S             Maximum entanglement entropy.
        ------------- -------------------------------------------------------------------
        time          Wallclock time evolved since :attr:`time0` (in seconds).
        ------------- -------------------------------------------------------------------
        max_trunc_err The maximum truncation error in the last sweep
        ------------- -------------------------------------------------------------------
        max_E_trunc   Maximum change or Energy due to truncation in the last sweep.
        ------------- -------------------------------------------------------------------
        max_chi       Maximum bond dimension used.
        ------------- -------------------------------------------------------------------
        norm_err      Error of canonical form ``np.linalg.norm(psi.norm_test())``.
        ============= ===================================================================

    """

    def __init__(self, psi, model, engine_params):
        self.EffectiveH = OneSiteH
        super(SingleSiteDMRGEngine, self).__init__(psi, model, engine_params)

    def prepare_update(self):
        """Prepare `self` to represent the effective Hamiltonian on site ``i0``.

        Returns
        -------
        theta : :class:`~tenpy.linalg.np_conserved.Array`
            Current best guess for the ground state, which is to be optimized.
            Labels ``'vL', 'p0', 'vR', 'p1'``.
        theta_ortho : list of :class:`~tenpy.linalg.np_conserved.Array`
            States (also with labels ``'vL', 'p0', 'vR', 'p1'``) to orthogonalize against,
            see :meth:`get_theta_ortho`.
        """
        EffectiveH = self.EffectiveH
        env = self.env
        self.eff_H = eff_H = EffectiveH(env, self.i0, self.combine, self.move_right)
        # eff_H has attributes LP, RP, W.

        # make theta
        cutoff = 1.e-16 if self.mixer is None else 1.e-8
        theta = self.psi.get_theta(self.i0, n=1, cutoff=cutoff).replace_label('p0', 'p')
        # 'vL', 'p', 'vR'
        theta_ortho = self.get_theta_ortho()
        for th_o in theta_ortho:
            th_o.ireplace_label('p0', 'p')
        if self.combine:
            if self.move_right:
                theta = theta.combine_legs(['vL', 'p'], pipes=[eff_H.pipeL])
                theta_ortho = [
                    th_o.combine_legs(['vL', 'p'], pipes=[eff_H.pipeL]) for th_o in theta_ortho
                ]
            else:
                theta = theta.combine_legs(['p', 'vR'], pipes=[eff_H.pipeR])
                theta_ortho = [
                    th_o.combine_legs(['p', 'vR'], pipes=[eff_H.pipeR]) for th_o in theta_ortho
                ]
        else:
            theta.itranspose(['vL', 'p', 'vR'])
            for th_o in theta_ortho:
                th_o.ireplace_label('p0', 'p').itranspose(['vL', 'p', 'vR'])
        return theta, theta_ortho

    def update_local(self, theta, theta_ortho, optimize=True, meas_E_trunc=False):
        """Perform site-update on the site ``i0``.

        Parameters
        ----------
        theta : :class:`~tenpy.linalg.np_conserved.Array`
            Initial guess for the ground state of the effective Hamiltonian.
        theta_ortho : list of :class:`~tenpy.linalg.np_conserved.Array`
            States to orthogonalize against, with same tensor structure as `theta`.
        optimize : bool
            Wheter we actually optimize to find the ground state of the effective Hamiltonian.
            (If False, just update the environments).
        meas_E_trunc : bool
            Wheter to measure the energy after truncation.

        Returns
        -------
        E_total : float
            Total energy, obtained *before* truncation (if ``optimize=True``),
            or *after* truncation (if ``optimize=False``) (but never ``None``).
        E_trunc : float | ``None``
            The energy difference of the total energy after minus before truncation,
            ``E_truncated - E_total``. ``None`` if ``meas_E_trunc=False``.
        err : :class:`~tenpy.algorithms.truncation.TruncationError`
            The truncation error introduced after bond optimization.
        N_lanczos : int
            Dimension of the Krylov space used for optimization in the lanczos algorithm.
            0 if ``optimize=False``.
        age : int
            Current size of the DMRG simulation: number of physical sites involved
            into the contraction.
        """
        i0 = self.i0
        age = self.env.get_LP_age(i0) + 2 + self.env.get_RP_age(i0)
        if optimize:
            E0, theta, N = self.diag(theta, theta_ortho)
        else:
            E0, N = None, 0
        theta = self.prepare_svd(theta)
        if self.move_right:
            next_B = self.env.bra.get_B(i0 + 1, form='B')
        else:
            next_B = self.env.bra.get_B(i0 - 1, form='A')
        U, S, VH, err = self.mixed_svd(theta, next_B)
        self.set_B(U, S, VH)

        update_data = {
            'E0': E0,
            'err': err,
            'N': N,
            'age': age,
            'U': U,
            'VH': VH,
        }

        return update_data

    def prepare_svd(self, theta):
        """Transform theta into matrix for svd.

        In contrast with the 2-site engine, the matrix here depends on the direction we move, as we
        need `'p'` to point away from the direction we are going in.
        """
        if self.combine:
            if self.move_right:
                theta.itranspose(['(vL.p)', 'vR'])  # ensure the order.
            else:
                theta.itranspose(['vL', '(p.vR)'])  # ensure the order.
        else:
            if self.move_right:
                theta = theta.combine_legs(['vL', 'p'], qconj=+1, new_axes=0)
            else:
                theta = theta.combine_legs(['p', 'vR'], qconj=-1, new_axes=1)
        return theta

    def mixed_svd(self, theta, next_B):
        """Get (truncated) `B` from the new theta (as returned by diag).

        The goal is to split theta and truncate it. For a move to the right::

            |   -- theta -- next_B --    ==>    -- U -- S -- VH -- next_B --
            |        |      |                      |               |

        For a move to the left::

            |   -- next_B -- theta -- ==>    -- next_B -- U -- S -- VH --
            |      |         |                  |                   |

        The `VH` for right-move or `U` for left-move is absorebed into the `next_B`.

        Without a mixer, this is done by a simple svd and truncation of Schmidt values of theta
        followed by the absorption of VH/U.

        With a mixer, the state is perturbed before the SVD.
        The details of the perturbation are defined by the :class:`Mixer` class.

        Parameters
        ----------
        theta : :class:`~tenpy.linalg.np_conserved.Array`
            The optimized wave function, prepared for svd with :meth:`prepare_svd`,
            i.e. with combined legs.
        nextB : :class:`~tenpy.linalg.np_conserved.Array`
            MPS tensor at the site that will be visited next.

        Returns
        -------
        U : :class:`~tenpy.linalg.np_conserved.Array`
            Left-canonical part of `theta`. Labels ``'(vL.p)', 'vR'``.
        S : 1D ndarray | 2D :class:`~tenpy.linalg.np_conserved.Array`
            Without mixer just the singluar values of the array; with mixer it might be a general
            matrix with labels ``'vL', 'vR'``; see comment above.
        VH : :class:`~tenpy.linalg.np_conserved.Array`
            Right-canonical part of `theta`. Labels ``'vL', '(p.vR)'``.
        err : :class:`~tenpy.algorithms.truncation.TruncationError`
            The truncation error introduced.
        """
        # get qtotal_LR from i0
        if self.mixer is None:
            # simple case: real svd, defined elsewhere.
            qtotal = [theta.qtotal, None] if self.move_right else [None, theta.qtotal]
            U, S, VH, err, _ = svd_theta(theta,
                                         self.trunc_params,
                                         qtotal_LR=qtotal,
                                         inner_labels=['vR', 'vL'])
            if self.move_right:
                VH = npc.tensordot(VH, next_B, axes=['vR', 'vL'])
            else:
                U = npc.tensordot(next_B, U, axes=['vR', 'vL'])
            return U, S, VH, err
        else:  # we have a mixer
            U, S, VH, err = self.mixer.perturb_svd(self, theta, self.i0, self.move_right, next_B)
            # Enforce normalization:
            if self.move_right:
                VH = VH.combine_legs(['p', 'vR'], qconj=-1)
                U_VH, S_VH, VH = npc.svd(VH, inner_labels=['vR', 'vL'])
                VH = VH.split_legs('(p.vR)')
                S = U_VH.iscale_axis(S, 'vL').iscale_axis(S_VH, 'vR')
            else:
                U = U.combine_legs(['vL', 'p'], qconj=+1)
                U, S_U, VH_U = npc.svd(U, inner_labels=['vR', 'vL'])
                U = U.split_legs(['(vL.p)'])
                S = VH_U.iscale_axis(S, 'vR').iscale_axis(S_U, 'vL')
            return U, S, VH, err

    def set_B(self, U, S, VH):
        """Update the MPS with the ``U, S, VH`` returned by `self.mixed_svd`.

        Parameters
        ----------
        U, VH : :class:`~tenpy.linalg.np_conserved.Array`
            Left and Right-canonical matrices as returned by the SVD.
        S : 1D array | 2D :class:`~tenpy.linalg.np_conserved.Array`
            The middle part returned by the SVD, ``theta = U S VH``.
            Without a mixer just the singular values, with enabled `mixer` a 2D array.
        """
        i0 = self.i0
        if self.move_right:
            B0 = U.split_legs(['(vL.p)'])
            self.psi.set_B(i0, B0, form='A')  # left-canonical
            self.psi.set_B(i0 + 1, VH, form='B')  # right-canonical
            self.psi.set_SR(i0, S)
            for o_env in self.ortho_to_envs:
                o_env.del_LP(i0 + 1)
                o_env.del_RP(i0)
            self.env.del_LP(i0 + 1)
            self.env.del_RP(i0)
        else:
            B1 = VH.split_legs(['(p.vR)'])
            self.psi.set_B(i0 - 1, U, form='A')  # left-canonical
            self.psi.set_B(i0, B1, form='B')  # right-canonical
            self.psi.set_SL(i0, S)
            for o_env in self.ortho_to_envs:  # TODO indexing here
                o_env.del_LP(i0)
                o_env.del_RP(i0 - 1)
            self.env.del_LP(i0)
            self.env.del_RP(i0 - 1)

    def mixer_activate(self):
        """Set `self.mixer` to the class specified by `engine_params['mixer']`.
        """
        Mixer_class = get_parameter(self.engine_params, 'mixer', None, 'Sweep')
        if Mixer_class:
            if Mixer_class is True:
                Mixer_class = SingleSiteMixer
            if isinstance(Mixer_class, str):
                if Mixer_class == "Mixer":
                    msg = ('Use `True` or `"DensityMatrixMixer"` instead of "Mixer" '
                           'for Sweep parameter "mixer"')
                    warnings.warn(msg, FutureWarning)
                    Mixer = "DensityMatrixMixer"  # TODO not for 1-site.
                Mixer_class = globals()[Mixer_class]
            mixer_params = get_parameter(self.engine_params, 'mixer_params', {}, 'Sweep')
            mixer_params.setdefault('verbose', self.verbose / 10)  # reduced verbosity
            self.mixer = Mixer_class(mixer_params)

    def update_LP(self, U):
        """Update left part of the environment.

        The site at which to update the environment depends on the direction of the sweep. If we
        are sweeping right, update the invironment at `i0+1`. If we are sweeping left, update the
        environment at `i0`

        Parameters
        ----------
        U : :class:`~tenpy.linalg.np_conserved.Array`
            The U as returned by SVD, with combined legs,
            labels ``'(vL.p)', 'vR'`` if self.move_right or ``'vL', '(p.vR)'`` if self.move_left.
        """
        i0 = self.i0
        if self.combine and self.move_right:
            LP = npc.tensordot(self.eff_H.LHeff, U, axes=['(vR.p*)', '(vL.p)'])
            LP = npc.tensordot(U.conj(), LP, axes=['(vL*.p*)', '(vR*.p)'])
            self.env.set_LP(i0 + 1, LP, age=self.env.get_LP_age(i0) + 1)
        else:  # as implemented directly in the environment
            if self.move_right:
                self.env.get_LP(i0 + 1, store=True)
            else:
                self.env.get_LP(i0, store=True)

    def update_RP(self, VH):
        """Update right part of the environment.

        The site at which to update the environment depends on the direction of the sweep. If we
        are sweeping right, update the invironment at `i0`. If we are sweeping left, update the
        environment at `i0-1`

        Parameters
        ----------
        VH : :class:`~tenpy.linalg.np_conserved.Array`
            The VH as returned by SVD, with combined legs,
            labels ``'(vL.p)', 'vR'`` if self.move_right or ``'vL', '(p.vR)'`` if self.move_left.
        """
        i0 = self.i0
        if self.combine and not self.move_right:
            RP = npc.tensordot(VH, self.eff_H.RHeff, axes=['(p.vR)', '(p*.vL)'])
            RP = npc.tensordot(RP, VH.conj(), axes=['(p.vL*)', '(p*.vR*)'])
            self.env.set_RP(i0 - 1, RP, age=self.env.get_RP_age(i0) + 1)
        else:  # as implemented directly in the environment
            if self.move_right:
                self.env.get_RP(i0, store=True)
            else:
                self.env.get_RP(i0 - 1, store=True)


class Engine(NpcLinearOperator):
    """Prototype for an DMRG 'Engine'.

    This class is deprecated with the arrival of the new `Sweep`-based classes.

    The old docstring:

    This class is the working horse of DMRG. It implements the :meth:`sweep` and large
    parts of the (two-site) optimization.
    During the diagonalization (i.e. after calling :meth:`prepare_diag`), the class represents
    the effective two-site Hamiltonian, which looks like this::

        |        .---            ----.
        |        |     |      |      |
        |        LP----W[i0]--W[i1]--RP
        |        |     |      |      |
        |        .---            ----.

    `LP` and `RP` are left and right parts of the :class:`~tenpy.networks.mpo.MPOEnvironment`,
    `W[i0]` and `W[i1]` are the MPO matrices of the Hamiltonian at the two sites ``i0, i1=i0+1``.
    How this network is then actually contracted in detail is left to derived classes.

    Parameters
    ----------
    psi : :class:`~tenpy.networks.mps.MPS`
        Initial guess for the ground state, which is to be optimized in-place.
    model : :class:`~tenpy.models.MPOModel`
        The model representing the Hamiltonian for which we want to find the ground state.
    DMRG_params : dict
        Further optional parameters. See :func:`run` for more details.

    Attributes
    ----------
    verbose : int
        Level of verbosity (i.e. how much status information to print); higher=more output.
    psi : :class:`~tenpy.networks.mps.MPS`
        The MPS to be optimized. After :meth:`run`, this should be (close to) the ground state.
    sweeps : int
        The number of performed sweeps (with ``optimize=True``, i.e. not counting the
        environment updates).
    env : :class:`~tenpy.networks.mpo.MPOEnvironment`
        Environment for contraction ``<psi|H|psi>``.
    ortho_to_envs : list of :class:`~tenpy.networks.mps.MPSEnvironment`
        Environments of the form ``<psi|orhto>`` for states `ortho` of the `orthogonal_to`
        parameter; needed to find excited states.
    mixer : :class:`Mixer` | ``None``
        If ``None``, no mixer is used (anymore), otherwise the mixer instance.
    DMRG_params : dict
        Parameters used for the DMRG. See :func:`run` for more details.
    lanczos_params : dict
        Parameters for :func:`~tenpy.linalg.lanczos.lanczos`.
    trunc_params : dict
        Parameters for :func:`~tenpy.algorithms.truncation.truncate`.
    finite : bool
        Wheter we perform DMRG for an MPS with finite/segment (True) or infinite boundary
        conditions; same as `psi.finite`.
    shelve : bool
        True when the DMRG stopped due to the time limit set by `max_hours`, otherwise `False`.
    chi_list : dict | None
        See DMRG_params `chi_list`. ``None`` (default) disables this feature.
    update_stats : dict
        A dictionary with detailed statistics of the convergence.
        For each key in the following table, the dictionary contains a list where one value is
        added each time :meth:`Engine.update_bond` is called.

        =========== ===================================================================
        key         description
        =========== ===================================================================
        i0          An update was performed on sites ``i0, i0+1``.
        ----------- -------------------------------------------------------------------
        age         The number of physical sites involved in the simulation.
        ----------- -------------------------------------------------------------------
        E_total     The total energy before truncation.
        ----------- -------------------------------------------------------------------
        N_lanczos   Dimension of the Krylov space used in the lanczos diagonalization.
        ----------- -------------------------------------------------------------------
        time        Wallclock time evolved since :attr:`time0` (in seconds).
        =========== ===================================================================

    sweep_stats : dict
        A dictionary with detailed statistics of the convergence.
        For each key in the following table, the dictionary contains a list where one value is
        added each time :meth:`Engine.sweep` is called (with ``optimize=True``).

        ============= ===================================================================
        key           description
        ============= ===================================================================
        sweep         Number of sweeps performed so far.
        ------------- -------------------------------------------------------------------
        E             The energy *before* truncation (as calculated by Lanczos).
        ------------- -------------------------------------------------------------------
        S             Maximum entanglement entropy.
        ------------- -------------------------------------------------------------------
        time          Wallclock time evolved since :attr:`time0` (in seconds).
        ------------- -------------------------------------------------------------------
        max_trunc_err The maximum truncation error in the last sweep
        ------------- -------------------------------------------------------------------
        max_E_trunc   Maximum change or Energy due to truncation in the last sweep.
        ------------- -------------------------------------------------------------------
        max_chi       Maximum bond dimension used.
        ------------- -------------------------------------------------------------------
        norm_err      Error of canonical form ``np.linalg.norm(psi.norm_test())``.
        ============= ===================================================================

    time0 : float
        Start time of the simulation, set in :meth:`reset_stats`.
    """


class EngineCombine(TwoSiteDMRGEngine):
    r"""Engine which combines legs into pipes as far as possible.

    This engine combines the virtual and physical leg for the left site and right site into pipes.
    This reduces the overhead of calculating charge combinations in the contractions,
    but one :meth:`matvec` is formally more expensive, :math:`O(2 d^3 \chi^3 D)`.

    Attributes
    ----------
    LHeff : :class:`~tenpy.linalg.np_conserved.Array`
        Left part of the effective Hamiltonian.
        Labels ``'(vR*.p0)', 'wR', '(vR.p0*)'`` for bra, MPO, ket.
    RHeff : :class:`~tenpy.linalg.np_conserved.Array`
        Right part of the effective Hamiltonian.
        Labels ``'(vL.p1*)', 'wL', '(vL*.p1)'`` for ket, MPO, bra.
    """

    def __init__(self, psi, model, DMRG_params):
        msg = ("Old-style engines are deprecated in favor of `Sweep` subclasses.\n"
               "Use `TwoSiteDMRGEngine` with parameter `combine=True` "
               "instead of `EngineCombine`.")
        warnings.warn(msg, category=FutureWarning, stacklevel=2)
        DMRG_params['combine'] = True  # to reproduces old-style engine
        super().__init__(psi, model, DMRG_params)


class EngineFracture(Engine):
    r"""Engine which keeps the legs separate.

    Due to a different contraction order in :meth:`matvec`, this engine might be faster than
    :class:`EngineCombine`, at least for large physical dimensions and if the MPO is sparse.
    One :meth:`matvec` is :math:`O(2 \chi^3 d^2 W + 2 \chi^2 d^3 W^2 )`.

    Attributes
    ----------
    LP : :class:`~tenpy.linalg.np_conserved.Array`
        Left part of the effective Hamiltonian. Labels ``'vR*', 'wR', 'vR'``.
    RP : :class:`~tenpy.linalg.np_conserved.Array`
        Right part of the effective Hamiltonian. Labels ``'vL*', 'wL', 'vL'``.
    H0, H1 : :class:`~tenpy.linalg.np_conserved.Array`
        MPO on the two sites to be optimized.
        Labels ``'wL, 'wR', 'p0', 'p0*'`` and ``'wL, 'wR', 'p1', 'p1*'``.
    """

    def __init__(self, psi, model, DMRG_params):
        msg = ("Old-style engines are deprecated in favor of `Sweep` subclasses.\n"
               "Use `TwoSiteDMRGEngine` with parameter `combine=False` "
               "instead of `EngineFracture`.")
        warnings.warn(msg, category=FutureWarning, stacklevel=2)
        DMRG_params['combine'] = False  # to reproduces old-style engine
        super().__init__(psi, model, DMRG_params)


class Mixer:
    """Base class of a general Mixer.

    Since DMRG performs only local updates of the state, it can get stuck in "local minima",
    in particular if the Hamiltonain is long-range -- which is the case if one
    maps a 2D system ("infinite cylinder") to 1D -- or if one wants to do single-site updates
    (currently not implemented in TeNPy).
    The idea of the mixer is to perturb the state with the terms of the Hamiltonian
    which have contributions in both the "left" and "right" side of the system.
    In that way, it adds fluctuation of the quantum numbers and non-zero contributions of the
    long-range terms - leading to a significantly improved convergence of DMRG.

    The strength of the perturbation is given by the `amplitude` of the mixer.
    A good strategy is to choose an initially significant amplitude and let it decay until
    the perturbation becomes completely irrelevant and the mixer gets disabled.

    This original idea of the mixer was introduced in [White2005]_.
    [Hubig2015]_ discusses the mixer and provides an improved version.

    Parameters
    ----------
    env : :class:`~tenpy.networks.mpo.MPOEnvironment`
        Environment for contraction ``<psi|H|psi>`` for later
    mixer_params : dict
        Optional parameters as described in the following table.
        Use ``verbose>0`` to print the used parameters during runtime.

        ============== ========= ===============================================================
        key            type      description
        ============== ========= ===============================================================
        amplitude      float     Initial strength of the mixer. (Should be << 1.)
        -------------- --------- ---------------------------------------------------------------
        decay          float     To slowly turn off the mixer, we divide `amplitude` by `decay`
                                 after each sweep. (Should be >= 1.)
        -------------- --------- ---------------------------------------------------------------
        disable_after  int       We disable the mixer completely after this number of sweeps.
        ============== ========= ===============================================================

    Attributes
    ----------
    amplitude : float
        Current amplitude for mixing.
    decay : float
        Factor by which `amplitude` is divided after each sweep.
    disable_after : int
        The number of sweeps after which the mixer should be disabled.
    verbose : int
        Level of output vebosity.
    """

    def __init__(self, mixer_params):
        self.amplitude = get_parameter(mixer_params, 'amplitude', 1.e-5, 'Mixer')
        assert self.amplitude <= 1.
        self.decay = get_parameter(mixer_params, 'decay', 2., 'Mixer')
        assert self.decay >= 1.
        if self.decay == 1.:
            warnings.warn("Mixer with decay=1. doesn't decay")
        self.disable_after = get_parameter(mixer_params, 'disable_after', 15, 'Mixer')
        self.verbose = mixer_params.get('verbose', 0)

    def update_amplitude(self, sweeps):
        """Update the amplitude, possibly disable the mixer.

        Parameters
        ----------
        sweeps : int
            The number of performed sweeps, to check if we need to disable the mixer.

        Returns
        -------
        mixer : :class:`Mixer` | None
            Returns `self` if we should continue mixing, or ``None``, if the mixer
            should be disabled.
        """
        self.amplitude /= self.decay
        if sweeps >= self.disable_after or self.amplitude <= np.finfo('float').eps:
            if self.verbose >= 0.1:  # increased verbosity: the same level as DMRG
                print("disable mixer after {0:d} sweeps, final amplitude {1:.2e}".format(
                    sweeps, self.amplitude))
            return None  # disable mixer
        return self

    def perturb_svd(self, engine, theta, i0, update_LP, update_RP):
        """Perturb the wave function and perform an SVD with truncation.

        Parameters
        ----------
        engine : :class:`Engine`
            The DMRG engine calling the mixer.
        theta : :class:`~tenpy.linalg.np_conserved.Array`
            The optimized wave function, prepared for svd.
        i0 : int
            Site index; `theta` lives on ``i0, i0+1``.
        update_LP : bool
            Whether to calculate the next ``env.LP[i0+1]``.
        update_RP : bool
            Whether to calculate the next ``env.RP[i0]``.

        Returns
        -------
        U : :class:`~tenpy.linalg.np_conserved.Array`
            Left-canonical part of `theta`. Labels ``'(vL.p0)', 'vR'``.
        S : 1D ndarray | 2D :class:`~tenpy.linalg.np_conserved.Array`
            Without mixer just the singluar values of the array; with mixer it might be a general
            matrix; see comment above.
        VH : :class:`~tenpy.linalg.np_conserved.Array`
            Right-canonical part of `theta`. Labels ``'vL', '(vR.p1)'``.
        err : :class:`~tenpy.algorithms.truncation.TruncationError`
            The truncation error introduced.
        """
        raise NotImplementedError("This function should be implemented in derived classes")


class SingleSiteMixer(Mixer):
    """Mixer for single-site DMRG.

    Performs a subspace expansion following [Hubig2015]_.
    """

    def perturb_svd(self, engine, theta, i0, move_right, next_B):
        """Mix extra terms to theta and perform an SVD.

        We calculate the left and right reduced density matrix using the mixer
        (which might include applications of `H`).
        These density matrices are diagonalized and truncated such that we effectively perform
        a svd for the case ``mixer.amplitude=0``.

        Parameters
        ----------
        engine : :class:`Engine`
            The DMRG engine calling the mixer.
        theta : :class:`~tenpy.linalg.np_conserved.Array`
            The optimized wave function, prepared for svd.
        i0 : int
            The site index where `theta` lives.
        move_right : bool
            Whether we move to the right (``True``) or left (``False``).
        next_B : :class:`~tenpy.linalg.np_conserved.Array`
            The subspace expansion requires to change the tensor on the next site as well.
            If `move_right`, it should correspond to ``engine.psi.get_B(i0+1, form='B')``.
            If not `move_right`, it should correspond to ``engine.psi.get_B(i0-1, form='A')``.

        Returns
        -------
        U : :class:`~tenpy.linalg.np_conserved.Array`
            Left-canonical part of `tensordot(theta, next_B)`. Labels ``'(vL.p0)', 'vR'``.
        S : 1D ndarray
            (Perturbed) singular values on the new bond (between `theta` and `next_B`).
        VH : :class:`~tenpy.linalg.np_conserved.Array`
            Right-canonical part of `tensordot(theta, next_B)`. Labels ``'vL', '(p1.vR)'``.
        err : :class:`~tenpy.algorithms.truncation.TruncationError`
            The truncation error introduced.
        """
        theta, next_B = self.subspace_expand(engine, theta, i0, move_right, next_B)
        qtotal_LR = [theta.qtotal, None] if move_right else [None, theta.qtotal]
        U, S, VH, err, _ = svd_theta(theta,
                                     engine.trunc_params,
                                     qtotal_LR=qtotal_LR,
                                     inner_labels=['vR', 'vL'])
        if move_right:
            VH = npc.tensordot(VH, next_B, axes=['vR', 'vL'])
        else:
            U = npc.tensordot(next_B, U, axes=['vR', 'vL'])
        return U, S, VH, err

    def subspace_expand(self, engine, theta, i0, move_right, next_B):
        """Expand the MPS subspace, to allow the bond dimension to increase.

        This is the subspace expansion following [Hubig2015]_.

        Parameters
        ----------
        engine : :class:`SingleSiteDMRGEngine` | :class:`TwoSiteDMRGEngine`
            'Engine' for the DMRG algorithm
        theta : :class:`~tenpy.linalg.np_conserved.Array`
            Optimized guess for the ground state of the effective local Hamiltonian.
        i0 : int
            Site index at which the local update has taken place.
        move_right : bool
            Whether the next `i0` of the sweep will be right or left of the current one.
        next_B : :class:`~tenpy.linalg.np_conserved.Array`
            The subspace expansion requires to change the tensor on the next site as well.
            If `move_right`, it should correspond to ``engine.psi.get_B(i0+1, form='B')``.
            If not `move_right`, it should correspond to ``engine.psi.get_B(i0-1, form='A')``.

        Returns
        -------
        theta :
            Local MPS tensor at site `i0` after subspace expansion.
        next_B :
            MPS tensor at site `i0+1` or `i0-1` (depending on sweep direction) after subspace
            expansion.
        """
        if not engine.combine:  # Need to get Heff's even if combine=False
            engine.eff_H.combine_Heff()

        if move_right:  # theta has legs (vL.p), vR
            LHeff = engine.eff_H.LHeff
            expand = npc.tensordot(LHeff, theta, axes=['(vR.p*)', '(vL.p)'])
            expand = expand.combine_legs(['wR', 'vR'], qconj=-1, new_axes=1)
            expand *= self.amplitude
            theta = npc.concatenate([theta, expand], axis=1, copy=False)
            next_B = next_B.extend('vL', expand.legs[1].conj())
        else:  # theta has legs vL, (p.vR)
            RHeff = engine.eff_H.RHeff
            expand = npc.tensordot(theta, RHeff, axes=['(p.vR)', '(p*.vL)'])
            expand = expand.combine_legs(['vL', 'wL'], qconj=+1)
            expand *= self.amplitude
            theta = npc.concatenate([theta, expand], axis=0, copy=False)
            next_B = next_B.extend('vR', expand.legs[0].conj())
        return theta, next_B


class TwoSiteMixer(SingleSiteMixer):
    """Mixer for two-site DMRG.

    This is the two-site version of the mixer described in [Hubig2015]_.
    Equivalent to the :class:`DensityMatrixMixer`, but never construct the full density matrix.

    .. todo :
        This is still under development.
        Works only with :class:`TwoSiteDMRGEngine`.
        Has not been ported to `Sweep`-based setup yet. Do we need to?
    """

    def perturb_svd(self, engine, theta, i0, move_right):
        """Mix extra terms to theta and perform an SVD.

        Parameters
        ----------
        engine : :class:`Engine`
            The DMRG engine calling the mixer.
        theta : :class:`~tenpy.linalg.np_conserved.Array`
            The optimized wave function, prepared for svd.
        i0 : int
            Site index; `theta` lives on ``i0, i0+1``.
        update_LP : bool
            Whether to calculate the next ``env.LP[i0+1]``.
        update_RP : bool
            Whether to calculate the next ``env.RP[i0]``.

        Returns
        -------
        U : :class:`~tenpy.linalg.np_conserved.Array`
            Left-canonical part of `theta`. Labels ``'(vL.p0)', 'vR'``.
        S : 1D ndarray | 2D :class:`~tenpy.linalg.np_conserved.Array`
            Without mixer just the singluar values of the array; with mixer it might be a general
            matrix; see comment above.
        VH : :class:`~tenpy.linalg.np_conserved.Array`
            Right-canonical part of `theta`. Labels ``'vL', '(vR.p1)'``.
        err : :class:`~tenpy.algorithms.truncation.TruncationError`
            The truncation error introduced.
        """
        # first perform an SVD as if the mixer didn't exist
        qtotal_i0 = engine.psi.get_B(i0, form=None).qtotal
        U, S, VH, err, _ = svd_theta(theta,
                                     engine.trunc_params,
                                     qtotal_LR=[qtotal_i0, None],
                                     inner_labels=['vR', 'vL'])
        if move_right:  # move to the right
            U, S, VH, err2 = SingleSiteMixer.perturb_svd(self, engine, U.iscale_axis(S, 1), i0,
                                                         move_right, VH)
        else:  # update_RP is True
            U, S, VH, err2 = SingleSiteMixer.perturb_svd(self, engine, VH.iscale_axis(S, 0), i0,
                                                         move_right, U)
        return U, S, VH, err + err2


class DensityMatrixMixer(Mixer):
    """Mixer based on density matrices.

    This mixer constructs density matrices as described in the original paper [White2005]_.
    """

    def perturb_svd(self, engine, theta, i0, update_LP, update_RP):
        """Mix extra terms to theta and perform an SVD.

        We calculate the left and right reduced density using the mixer
        (which might include applications of `H`).
        These density matrices are diagonalized and truncated such that we effectively perform
        a svd for the case ``mixer.amplitude=0``.

        Parameters
        ----------
        engine : :class:`SingleSiteDMRGEngine` | :class:`TwoSiteDMRGEngine`
            The DMRG engine calling the mixer.
        theta : :class:`~tenpy.linalg.np_conserved.Array`
            The optimized wave function, prepared for svd.
        i0 : int
            Site index; `theta` lives on ``i0, i0+1``.
        update_LP : bool
            Whether to calculate the next ``env.LP[i0+1]``.
        update_RP : bool
            Whether to calculate the next ``env.RP[i0]``.

        Returns
        -------
        U : :class:`~tenpy.linalg.np_conserved.Array`
            Left-canonical part of `theta`. Labels ``'(vL.p0)', 'vR'``.
        S : 1D ndarray | 2D :class:`~tenpy.linalg.np_conserved.Array`
            Without mixer just the singluar values of the array; with mixer it might be a general
            matrix; see comment above.
        VH : :class:`~tenpy.linalg.np_conserved.Array`
            Right-canonical part of `theta`. Labels ``'vL', '(p1.vR)'``.
        err : :class:`~tenpy.algorithms.truncation.TruncationError`
            The truncation error introduced.
        """
        rho_L = self.mix_rho_L(engine, theta, i0, update_LP)
        # don't mix left parts, when we're going to the right
        rho_L.itranspose(['(vL.p0)', '(vL*.p0*)'])  # just to be sure of the order
        rho_R = self.mix_rho_R(engine, theta, i0, update_RP)
        rho_R.itranspose(['(p1.vR)', '(p1*.vR*)'])  # just to be sure of the order

        # consider the SVD `theta = U S V^H` (with real, diagonal S>0)
        # rho_L ~=  theta theta^H = U S V^H V S U^H = U S S U^H  (for mixer -> 0)
        # Thus, rho_L U = U S S, i.e. columns of U are the eigenvectors of rho_L,
        # eigenvalues are S^2.
        val_L, U = npc.eigh(rho_L)
        U.legs[1] = U.legs[1].to_LegCharge()  # explicit conversion: avoid warning in `iproject`
        U.iset_leg_labels(['(vL.p0)', 'vR'])
        val_L[val_L < 0.] = 0.  # for stability reasons
        val_L /= np.sum(val_L)
        keep_L, _, errL = truncate(np.sqrt(val_L), engine.trunc_params)
        U.iproject(keep_L, axes='vR')  # in place
        U = U.gauge_total_charge(1, engine.psi.get_B(i0, form=None).qtotal)
        # rho_R ~=  theta^T theta^* = V^* S U^T U* S V^T = V^* S S V^T  (for mixer -> 0)
        # Thus, rho_L V^* = V^* S S, i.e. columns of V^* are eigenvectors of rho_L
        val_R, Vc = npc.eigh(rho_R)
        Vc.legs[1] = Vc.legs[1].to_LegCharge()
        Vc.iset_leg_labels(['(p1.vR)', 'vL'])
        VH = Vc.itranspose(['vL', '(p1.vR)'])
        val_R[val_R < 0.] = 0.  # for stability reasons
        val_R /= np.sum(val_R)
        keep_R, _, err_R = truncate(np.sqrt(val_R), engine.trunc_params)
        VH.iproject(keep_R, axes='vL')
        VH = VH.gauge_total_charge(0, engine.psi.get_B(i0 + 1, form=None).qtotal)

        # calculate S = U^H theta V
        theta = npc.tensordot(U.conj(), theta, axes=['(vL*.p0*)', '(vL.p0)'])  # axes 0, 0
        theta = npc.tensordot(theta, VH.conj(), axes=['(p1.vR)', '(p1*.vR*)'])  # axes 1, 1
        theta.ireplace_labels(['vR*', 'vL*'], ['vL', 'vR'])  # for left/right
        # normalize `S` (as in svd_theta) to avoid blowing up numbers
        theta /= np.linalg.norm(npc.svd(theta, compute_uv=False))
        return U, theta, VH, errL + err_R

    def mix_rho_L(self, engine, theta, i0, mix_enabled):
        """Calculated mixed reduced density matrix for left site.

        Pictorially::

            |     mix_enabled=False           mix_enabled=True
            |
            |    .---theta---.            .---theta-------.
            |    |   |   |   |            |   |   |       |
            |            |   |           LP---H0--H1--.   |
            |    |   |   |   |            |   |   |   |   |
            |    .---theta*--.                    |   xR  |
            |                             |   |   |   |   |
            |                            LP*--H0*-H1*-.   |
            |                             |   |   |       |
            |                             .---theta*------.

        Parameters
        ----------
        engine : :class:`Engine`
            The DMRG engine calling the mixer.
        theta : :class:`~tenpy.linalg.np_conserved.Array`
            Ground state of the effective Hamiltonian, prepared for svd.
        i0 : int
            Site index; `theta` lives on ``i0, i0+1``.
        mix_enabled : bool
            Whether we should perturb the density matrix.

        Returns
        -------
        rho_L : :class:`~tenpy.linalg.np_conserved.Array`
            A (hermitian) square array with labels ``'(vL.p0)', '(vL*.p0*)'``,
            Mainly the reduced density matrix of the left part, but with some additional mixing.
        """
        if not mix_enabled:
            return npc.tensordot(theta, theta.conj(), axes=['(p1.vR)', '(p1*.vR*)'])
        H = engine.env.H
        try:
            LHeff = engine.LHeff
        except AttributeError:
            H0 = H.get_W(i0).replace_labels(['p', 'p*'], ['p0', 'p0*'])
            LP = engine.env.get_LP(i0, store=False)
            LHeff = npc.tensordot(LP, H0, axes=['wR', 'wL'])
            pipeL = theta.get_leg('(vL.p0)')
            LHeff = LHeff.combine_legs([['vR*', 'p0'], ['vR', 'p0*']],
                                       pipes=[pipeL, pipeL.conj()],
                                       new_axes=[0, -1])
        rho = npc.tensordot(LHeff, theta, axes=['(vR.p0*)', '(vL.p0)']).split_legs('(p1.vR)')
        rho_c = rho.conj()
        H1 = H.get_W(i0 + 1).replace_labels(['p', 'p*'], ['p1', 'p1*'])
        mixer_xR, add_separate_Id = self.get_xR(H1.get_leg('wR'), H.get_IdL(i0 + 2),
                                                H.get_IdR(i0 + 1))
        H1m = npc.tensordot(H1, mixer_xR, axes=['wR', 'wL'])
        H1m = npc.tensordot(H1m, H1.conj(), axes=[['p1', 'wL*'], ['p1*', 'wR*']])
        rho = npc.tensordot(rho, H1m, axes=[['wR', 'p1'], ['wL', 'p1*']])
        rho = npc.tensordot(rho, rho_c, axes=(['p1', 'wL*', 'vR'], ['p1*', 'wR*', 'vR*']))
        rho.ireplace_labels(['(vR*.p0)', '(vR.p0*)'], ['(vL.p0)', '(vL*.p0*)'])
        if add_separate_Id:
            rho = rho + npc.tensordot(theta, theta.conj(), axes=['(p1.vR)', '(p1*.vR*)'])
        return rho

    def mix_rho_R(self, engine, theta, i0, mix_enabled):
        """Calculated mixed reduced density matrix for left site.

        Pictorially::

            |     mix_enabled=False           mix_enabled=True
            |
            |    .---theta---.           .------theta---.
            |    |   |   |   |           |      |   |   |
            |    |   |                   |   .--H0--H1--RP
            |    |   |   |   |           |   |  |   |   |
            |    .---theta*--.           |  wL  |
            |                            |   |  |   |   |
            |                            |   .--H0*-H1*-RP*
            |                            |      |   |   |
            |                            .------theta*--.

        Parameters
        ----------
        engine : :class:`Engine`
            The DMRG engine calling the mixer.
        theta : :class:`~tenpy.linalg.np_conserved.Array`
            Ground state of the effective Hamiltonian, prepared for svd.
        i0 : int
            Site index; `theta` lives on ``i0, i0+1``.
        mix_enabled : bool
            Whether we should perturb the density matrix.

        Returns
        -------
        rho_R : :class:`~tenpy.linalg.np_conserved.Array`
            A (hermitian) square array with labels ``'(p1.vR)', '(p1*.vR*)'``.
            Mainly the reduced density matrix of the right part, but with some additional mixing.
        """
        if not mix_enabled:
            return npc.tensordot(theta, theta.conj(), axes=[['(vL.p0)'], ['(vL*.p0*)']])
        H = engine.env.H

        try:
            RHeff = engine.RHeff
        except AttributeError:
            H1 = H.get_W(i0 + 1).replace_labels(['p', 'p*'], ['p1', 'p1*'])
            RP = engine.env.get_RP(i0 + 1, store=False)
            RHeff = npc.tensordot(RP, H1, axes=['wL', 'wR'])
            pipeR = theta.get_leg('(p1.vR)')
            RHeff = RHeff.combine_legs([['p1', 'vL*'], ['p1*', 'vL']],
                                       pipes=[pipeR, pipeR.conj()],
                                       new_axes=[-1, 0])
        rho = npc.tensordot(RHeff, theta, axes=['(p1*.vL)', '(p1.vR)']).split_legs('(vL.p0)')
        rho_c = rho.conj()

        H0 = H.get_W(i0).replace_labels(['p', 'p*'], ['p0', 'p0*'])
        mixer_xL, add_separate_Id = self.get_xL(H0.get_leg('wL'), H.get_IdL(i0), H.get_IdR(i0 - 1))
        H0m = npc.tensordot(mixer_xL, H0, axes=['wR', 'wL'])
        H0m = npc.tensordot(H0m, H0.conj(), axes=[['wR*', 'p0'], ['wL*', 'p0*']])
        rho = npc.tensordot(H0m, rho, axes=[['p0*', 'wR'], ['p0', 'wL']])
        rho = npc.tensordot(rho, rho_c, axes=(['p0', 'wR*', 'vL'], ['p0*', 'wL*', 'vL*']))
        rho.ireplace_labels(['(p1.vL*)', '(p1*.vL)'], ['(p1.vR)', '(p1*.vR*)'])
        if add_separate_Id:
            rho = rho + npc.tensordot(theta, theta.conj(), axes=['(vL.p0)', '(vL*.p0*)'])
        return rho

    def get_xR(self, wR_leg, Id_L, Id_R):
        """Generate the coupling of the MPO legs for the reduced density matrix.

        Parameters
        ----------
        wR_leg : :class:`~tenpy.linalg.charges.LegCharge`
            LegCharge to be connected to.
        IdL : int | ``None``
            Index within the leg for which the MPO has only identities to the left.
        IdR : int | ``None``
            Index within the leg for which the MPO has only identities to the right.

        Returns
        -------
        mixed_xR : :class:`~tenpy.linalg.np_conserved.Array`
            Connection of the MPOs on the right for the reduced density matrix `rhoL`.
            Labels ``('wL', 'wL*')``.
        add_separate_Id : bool
            If Id_L is ``None``, we can't include the identity into `mixed_xR`,
            so it has to be added directly in :meth:`mix_rho_L`.
        """
        x = self.amplitude * np.ones(wR_leg.ind_len, dtype=np.float)
        separate_Id = Id_L is None
        if not separate_Id:
            x[Id_L] = 1.
        if Id_R is not None:
            x[Id_R] = 0.
        x = npc.diag(x, wR_leg)
        x.iset_leg_labels(['wL*', 'wL'])
        return x, separate_Id

    def get_xL(self, wL_leg, Id_L, Id_R):
        """Generate the coupling of the MPO legs for the reduced density matrix.

        Parameters
        ----------
        wL_leg : :class:`~tenpy.linalg.charges.LegCharge`
            LegCharge to be connected to.
        Id_L : int | ``None``
            Index within the leg for which the MPO has only identities to the left.
        Id_R : int | ``None``
            Index within the leg for which the MPO has only identities to the right.

        Returns
        -------
        mixed_xL : :class:`~tenpy.linalg.np_conserved.Array`
            Connection of the MPOs on the left for the reduced density matrix `rhoR`.
            Labels ``('wR', 'wR*')``.
        add_separate_Id : bool
            If Id_R is ``None``, we can't include the identity into `mixed_xL`,
            so it has to be added directly in :meth:`mix_rho_R`.
        """
        x = self.amplitude * np.ones(wL_leg.ind_len, dtype=np.float)
        separate_Id = Id_R is None
        if not separate_Id:
            x[Id_R] = 1.
        if Id_L is not None:
            x[Id_L] = 0.
        x = npc.diag(x, wL_leg)
        x.iset_leg_labels(['wR*', 'wR'])
        return x, separate_Id


def chi_list(chi_max, dchi=20, nsweeps=20):
    """Compute a 'ramping-up' chi_list.

    The resulting chi_list allows to increases `chi` by `dchi` every `nsweeps` sweeps up to a given
    maximal `chi_max`.

    Parameters
    ----------
    chi_max : int
        Final value for the bond dimension.
    dchi :int
        Step size how to increase chi
    nsweeps : int
        Step size for sweeps

    Returns
    -------
    chi_list : dict
        To be used as `chi_list` parameter for DMRG, see :func:`run`.
        Keys increase by `nsweeps`, values by `dchi`, until a maximum of `chi_max` is reached.
    """
    chi_max = int(chi_max)
    nsweeps = int(nsweeps)
    if chi_max < dchi:
        return {0: chi_max}
    chi_list = {}
    for i in range(chi_max // dchi):
        chi = int(dchi * (i + 1))
        chi_list[nsweeps * i] = chi
    if chi < chi_max:
        chi_list[nsweeps * (i + 1)] = chi_max
    return chi_list
