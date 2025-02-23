"""A collection of tests for (classes in) :mod:`tenpy.models.model`.
"""
# Copyright 2018 TeNPy Developers, GNU GPLv3

import itertools

from tenpy.models import model, lattice
from tenpy.models.xxz_chain import XXZChain
import tenpy.networks.site
import tenpy.linalg.np_conserved as npc
from tenpy.tools.params import get_parameter
from tenpy.algorithms.exact_diag import ExactDiag
import numpy as np
import numpy.testing as npt
import pytest

spin_half_site = tenpy.networks.site.SpinHalfSite('Sz')

fermion_site = tenpy.networks.site.FermionSite('N')

__all__ = ["check_model_sanity", "check_general_model"]


def check_model_sanity(M, hermitian=True):
    """call M.test_sanity() for all different subclasses of M"""
    if isinstance(M, model.CouplingModel):
        if isinstance(M, model.MultiCouplingModel):
            model.MultiCouplingModel.test_sanity(M)
        else:
            model.CouplingModel.test_sanity(M)
    if isinstance(M, model.NearestNeighborModel):
        model.NearestNeighborModel.test_sanity(M)
        if hermitian:
            for i, H in enumerate(M.H_bond):
                if H is not None:
                    err = npc.norm(H - H.conj().transpose(H.get_leg_labels()))
                    if err > 1.e-14:
                        print(H)
                        raise ValueError("H on bond {i:d} not hermitian".format(i=i))
    if isinstance(M, model.MPOModel):
        model.MPOModel.test_sanity(M)
        if hermitian:
            assert M.H_MPO.is_hermitian()


def check_general_model(ModelClass, model_pars={}, check_pars={}, hermitian=True):
    """Create a model for different sets of parameters and check its sanity.

    Parameters
    ----------
    ModelClass :
        We generate models of this class
    model_pars : dict
        Model parameters used.
    check_pars : dict
        pairs (`key`, `list of values`); we update ``model_pars[key]`` with any values of
        ``check_pars[key]`` (in each possible combination!) and create a model for it.
    hermitian : bool
        If True, check that the Hamiltonian is Hermitian.
    """
    for vals in itertools.product(*list(check_pars.values())):
        print("-" * 40)
        params = model_pars.copy()
        for k, v in zip(list(check_pars.keys()), vals):
            params[k] = v
        print("check_model_sanity with following parameters:")
        print(params)
        M = ModelClass(params)
        check_model_sanity(M)


def test_CouplingModel():
    for bc in ['open', 'periodic']:
        spin_half_lat = lattice.Chain(5, spin_half_site, bc=bc, bc_MPS='finite')
        M = model.CouplingModel(spin_half_lat)
        M.add_coupling(1.2, 0, 'Sz', 0, 'Sz', 1)
        M.test_sanity()
        M.calc_H_MPO()
        if bc == 'periodic':
            with pytest.raises(ValueError, match="nearest neighbor"):
                M.calc_H_bond()  # should raise a ValueError
                # periodic bc but finite bc_MPS leads to a long-range coupling
        else:
            M.calc_H_bond()


def test_ext_flux():
    Lx, Ly = 3, 4
    lat = lattice.Square(Lx, Ly, fermion_site, bc=['periodic', 'periodic'], bc_MPS='infinite')
    M = model.CouplingModel(lat)
    strength = 1.23
    strength_array = np.ones((Lx, Ly)) * strength
    for phi in [0, 2 * np.pi]:  # flux shouldn't do anything
        print("phi = ", phi)
        for dx in [1, 0], [0, 1], [0, 2], [1, -1], [-2, 2]:
            print("dx = ", dx)
            strength_flux = M.coupling_strength_add_ext_flux(strength, [1, 0], [0, phi])
            npt.assert_array_almost_equal_nulp(strength_flux, strength_array, 10)
    for phi in [np.pi / 2, 0.123]:
        print("phi = ", phi)
        strength_hop_x = M.coupling_strength_add_ext_flux(strength, [1, 0], [0, phi])
        npt.assert_array_almost_equal_nulp(strength_hop_x, strength_array, 10)
        expect_y_1 = np.array(strength_array, dtype=np.complex128)
        expect_y_1[:, -1:] = strength * np.exp(1.j * phi)
        for dx in [[0, 1], [0, -1], [1, -1], [1, 1]]:
            print("dx = ", dx)
            strength_hop_y_1 = M.coupling_strength_add_ext_flux(strength, dx, [0, phi])
            if dx[1] < 0:
                npt.assert_array_almost_equal_nulp(strength_hop_y_1, expect_y_1, 10)
            else:
                npt.assert_array_almost_equal_nulp(strength_hop_y_1, np.conj(expect_y_1), 10)
        expect_y_2 = np.array(strength_array, dtype=np.complex128)
        expect_y_2[:, -2:] = strength * np.exp(1.j * phi)
        for dx in [[0, 2], [0, -2], [1, 2], [3, 2]]:
            print("dx = ", dx)
            strength_hop_y_2 = M.coupling_strength_add_ext_flux(strength, dx, [0, phi])
            if dx[1] < 0:
                npt.assert_array_almost_equal_nulp(strength_hop_y_2, expect_y_2, 10)
            else:
                npt.assert_array_almost_equal_nulp(strength_hop_y_2, np.conj(expect_y_2), 10)


def test_MultiCouplingModel_shift(Lx=3, Ly=3, shift=1):
    bc = ['periodic', shift]
    spin_half_square = lattice.Square(Lx, Ly, spin_half_site, bc=bc, bc_MPS='infinite')
    M = model.MultiCouplingModel(spin_half_square)
    M.add_coupling(1.2, 0, 'Sz', 0, 'Sz', [1, 0])
    M.add_multi_coupling(0.8, 0, 'Sz', [(0, 'Sz', [0, 1]), (0, 'Sz', [1, 0])])
    M.test_sanity()
    H = M.calc_H_MPO()
    dims = [W.shape[0] for W in H._W]
    # check translation invariance of the MPO: at least the dimensions should fit
    # (the states are differently ordered, so the matrices differ!)
    for i in range(1, Lx):
        assert dims[:Lx] == dims[Lx:2 * Lx]


def test_CouplingModel_fermions():
    for bc, bc_MPS in zip(['open', 'periodic'], ['finite', 'infinite']):
        fermion_lat = lattice.Chain(5, fermion_site, bc=bc, bc_MPS=bc_MPS)
        M = model.CouplingModel(fermion_lat)
        M.add_coupling(1.2, 0, 'Cd', 0, 'C', 1, 'JW')
        M.add_coupling(1.2, 0, 'Cd', 0, 'C', -1, 'JW')
        M.test_sanity()
        M.calc_H_MPO()
        M.calc_H_bond()


def test_CouplingModel_explicit():
    fermion_lat_cyl = lattice.Square(1, 2, fermion_site, bc='periodic', bc_MPS='infinite')
    M = model.CouplingModel(fermion_lat_cyl)
    M.add_onsite(0.125, 0, 'N')
    M.add_coupling(0.25, 0, 'Cd', 0, 'C', (0, 1), None)  # auto-determine JW-string!
    M.add_coupling(0.25, 0, 'Cd', 0, 'C', (0, -1), None)
    M.add_coupling(1.5, 0, 'Cd', 0, 'C', (1, 0), None)
    M.add_coupling(1.5, 0, 'Cd', 0, 'C', (-1, 0), None)
    M.add_coupling(4., 0, 'N', 0, 'N', (-2, -1), None)  # a full unit cell inbetween!
    H_mpo = M.calc_H_MPO()
    W0_new = H_mpo.get_W(0)
    W1_new = H_mpo.get_W(1)
    Id, JW, N = fermion_site.Id, fermion_site.JW, fermion_site.N
    Cd, C = fermion_site.Cd, fermion_site.C
    CdJW = Cd.matvec(JW)  # = Cd
    CJW = C.matvec(JW)  # = -C
    # yapf: disable
    W0_ex = [[Id,   CJW,  CdJW, N,    None, None, None, None, None, N*0.125],
             [None, None, None, None, None, None, None, None, None, Cd*-1.5],
             [None, None, None, None, None, None, None, None, None, C*1.5],
             [None, None, None, None, Id,   None, None, None, None, None],
             [None, None, None, None, None, Id,   None, None, None, None],
             [None, None, None, None, None, None, JW,   None, None, None],
             [None, None, None, None, None, None, None, JW,   None, None],
             [None, None, None, None, None, None, None, None, Id,   None],
             [None, None, None, None, None, None, None, None, None, N*4.0],
             [None, None, None, None, None, None, None, None, None, Id]]
    W1_ex = [[Id,   None, None, None, None, CJW,  CdJW, N,    None, N*0.125],
             [None, JW,   None, None, None, None, None, None, None, Cd*-0.5],
             [None, None, JW,   None, None, None, None, None, None, C*0.5],
             [None, None, None, Id,   None, None, None, None, None, None],
             [None, None, None, None, Id,   None, None, None, None, None],
             [None, None, None, None, None, None, None, None, None, N*4.0],
             [None, None, None, None, None, None, None, None, None, Cd*-1.5],
             [None, None, None, None, None, None, None, None, None, C*1.5],
             [None, None, None, None, None, None, None, None, Id,   None],
             [None, None, None, None, None, None, None, None, None, Id]]

    # yapf: enable
    W0_ex = npc.grid_outer(W0_ex, W0_new.legs[:2])
    W1_ex = npc.grid_outer(W1_ex, W1_new.legs[:2])
    assert npc.norm(W0_new - W0_ex) == 0.  # coupling constants: no rounding errors
    assert npc.norm(W1_new - W1_ex) == 0.  # coupling constants: no rounding errors


def test_MultiCouplingModel_explicit():
    fermion_lat_cyl = lattice.Square(1, 2, fermion_site, bc='periodic', bc_MPS='infinite')
    M = model.MultiCouplingModel(fermion_lat_cyl)
    # create a wired fermionic model with 3-body interactions
    M.add_onsite(0.125, 0, 'N')
    M.add_coupling(0.25, 0, 'Cd', 0, 'C', (0, 1))
    M.add_coupling(0.25, 0, 'Cd', 0, 'C', (0, -1), 'JW')
    M.add_coupling(1.5, 0, 'Cd', 0, 'C', (1, 0), 'JW')
    M.add_coupling(1.5, 0, 'Cd', 0, 'C', (-1, 0), 'JW')
    M.add_multi_coupling(4., 0, 'N', [(0, 'N', (-2, -1))], 'Id')  # a full unit cell inbetween!
    # some wired mediated hopping along the diagonal
    M.add_multi_coupling(1.125, 0, 'N', other_ops=[(0, 'Cd', (0, 1)), (0, 'C', (1, 0))])
    H_mpo = M.calc_H_MPO()
    W0_new = H_mpo.get_W(0)
    W1_new = H_mpo.get_W(1)
    W2_new = H_mpo.get_W(2)
    Id, JW, N = fermion_site.Id, fermion_site.JW, fermion_site.N
    Cd, C = fermion_site.Cd, fermion_site.C
    CdJW = Cd.matvec(JW)  # = Cd
    CJW = C.matvec(JW)  # = -C
    NJW = N.matvec(JW)
    # print(M.H_MPO_graph._build_grids())
    # yapf: disable
    W0_ex = [[Id,   CJW,  CdJW, None, N,    None, None, None, None, None, N*0.125],
             [None, None, None, None, None, None, None, None, None, None, Cd*-1.5],
             [None, None, None, None, None, None, None, None, None, None, C*1.5],
             [None, None, None, JW,   None, None, None, None, None, None, None],
             [None, None, None, None, None, Id,   None, None, None, None, None],
             [None, None, None, None, None, None, None, None, None, None, C*1.125],
             [None, None, None, None, None, None, Id,   None, None, None, None],
             [None, None, None, None, None, None, None, JW,   None, None, None],
             [None, None, None, None, None, None, None, None, JW,   None, None],
             [None, None, None, None, None, None, None, None, None, Id,   None],
             [None, None, None, None, None, None, None, None, None, None, N*4.0],
             [None, None, None, None, None, None, None, None, None, None, Id]]
    W1_ex = [[Id,   None, None, None, None, None, None, CJW,  CdJW, N,    None, N*0.125],
             [None, JW,   None, None, None, None, None, None, None, None, None, Cd*-0.5],
             [None, None, JW,   NJW,  None, None, None, None, None, None, None, C*0.5],
             [None, None, None, None, None, None, None, None, None, None, None, C*1.125],
             [None, None, None, None, Id,   CdJW, None, None, None, None, None, None],
             [None, None, None, None, None, None, Id,   None, None, None, None, None],
             [None, None, None, None, None, None, None, None, None, None, None, N*4.0],
             [None, None, None, None, None, None, None, None, None, None, None, Cd*-1.5],
             [None, None, None, None, None, None, None, None, None, None, None, C*1.5],
             [None, None, None, None, None, None, None, None, None, None, Id,   None],
             [None, None, None, None, None, None, None, None, None, None, None, Id]]
    # yapf: enable
    W0_ex = npc.grid_outer(W0_ex, W0_new.legs[:2])
    W1_ex = npc.grid_outer(W1_ex, W1_new.legs[:2])
    assert npc.norm(W0_new - W0_ex) == 0.  # coupling constants: no rounding errors
    assert npc.norm(W1_new - W1_ex) == 0.  # coupling constants: no rounding errors


class MyMod(model.CouplingMPOModel, model.NearestNeighborModel):
    def __init__(self, model_params):
        model.CouplingMPOModel.__init__(self, model_params)

    def init_sites(self, model_params):
        return tenpy.networks.site.SpinHalfSite('parity')

    def init_terms(self, model_params):
        x = get_parameter(model_params, 'x', 1., self.name)
        self.add_onsite_term(0.25, 0, 'Sz')
        self.add_onsite_term(0.25, 4, 'Sz')
        self.add_coupling_term(x, 0, 1, 'Sx', 'Sx')
        self.add_coupling_term(2. * x, 1, 2, 'Sy', 'Sy')
        self.add_coupling_term(3. * x, 3, 4, 'Sy', 'Sy')


def test_CouplingMPOModel_group():
    m = MyMod(dict(x=0.5, L=5, bc_MPS='finite'))
    assert m.H_MPO.max_range == 1
    # test grouping sites
    ED = ExactDiag(m)
    #  ED.build_full_H_from_mpo()
    ED.build_full_H_from_bonds()
    m.group_sites(n=2)
    assert m.H_MPO.max_range == 1
    ED_gr = ExactDiag(m)
    ED_gr.build_full_H_from_mpo()
    H = ED.full_H.split_legs().to_ndarray()
    Hgr = ED_gr.full_H.split_legs()
    Hgr.idrop_labels()
    Hgr = Hgr.split_legs().to_ndarray()
    assert np.linalg.norm(H - Hgr) == 0
    ED_gr.full_H = None
    ED_gr.build_full_H_from_bonds()
    Hgr = ED_gr.full_H.split_legs()
    Hgr.idrop_labels()
    Hgr = Hgr.split_legs().to_ndarray()
    assert np.linalg.norm(H - Hgr) == 0


def test_model_H_conversion(L=6):
    bc = 'finite'
    model_params = {'L': L, 'hz': np.random.random([L]), 'bc_MPS': bc}
    m = XXZChain(model_params)
    # can we run the conversion?
    # conversion from bond to MPO in NearestNeighborModel
    H_MPO = m.calc_H_MPO_from_bond()
    # conversion from MPO to bond in MPOModel
    H_bond = m.calc_H_bond_from_MPO()
    # compare: did we get the correct result?
    ED = ExactDiag(m)
    ED.build_full_H_from_bonds()
    H0 = ED.full_H  # this should be correct
    ED.full_H = None
    m.H_MPO = H_MPO
    ED.build_full_H_from_mpo()
    full_H_mpo = ED.full_H  # the one generated by NearstNeighborModel.calc_H_MPO_from_bond()
    print("npc.norm(H0 - full_H_mpo) = ", npc.norm(H0 - full_H_mpo))
    assert npc.norm(H0 - full_H_mpo) < 1.e-14  # round off errors on order of 1.e-15
    m.H_bond = H_bond
    ED.full_H = None
    ED.build_full_H_from_bonds()
    full_H_bond = ED.full_H  # the one generated by NearstNeighborModel.calc_H_MPO_from_bond()
    print("npc.norm(H0 - full_H_bond) = ", npc.norm(H0 - full_H_bond))
    assert npc.norm(H0 - full_H_bond) < 1.e-14  # round off errors on order of 1.e-15
