import ctypes

import numpy as np
from gpaw.atom.radialgd import EquidistantRadialGridDescriptor
from gpaw.sphere.lebedev import R_nv, Y_nL, weight_n
from gpaw.xc.pawcorrection import rnablaY_nLv
from gpaw.xc.vdw import spline
from scipy.interpolate import interp1d
from scipy.linalg import cho_factor, cho_solve

from ciderpress.data import get_hgbs_max_exps
from ciderpress.dft.density_util import get_sigma
from ciderpress.dft.grids_indexer import AtomicGridsIndexer
from ciderpress.dft.lcao_convolutions import (
    ATCBasis,
    ConvolutionCollection,
    get_etb_from_expnt_range,
    get_gamma_lists_from_etb_list,
)
from ciderpress.dft.plans import NLDFAuxiliaryPlan, get_ccl_settings, libcider
from ciderpress.gpaw.atom_utils import AtomPASDWSlice, SBTGridContainer
from ciderpress.gpaw.fit_paw_gauss_pot import (
    construct_full_p_matrices,
    get_delta_lpg,
    get_delta_lpk,
    get_dv,
    get_dvk,
    get_ffunc3,
    get_p11_matrix,
    get_p12_p21_matrix,
    get_p22_matrix,
    get_pfunc2_norm,
    get_pfuncs_k,
    get_phi_iabg,
    get_phi_iabk,
)
from ciderpress.gpaw.interp_paw import (
    DiffPAWXCCorrection,
    calculate_cider_paw_correction,
)

USE_GAUSSIAN_PAW_CONV = False


def _interpc(func, ref_rgd, r_g):
    return interp1d(
        np.arange(ref_rgd.r_g.size),
        func,
        kind="cubic",
        axis=-1,
    )(ref_rgd.r2g(r_g))


def get_ag_indices(fft_obj, gd, shape, spos_c, rmax, buffer=0, get_global_disps=False):
    center = np.round(spos_c * shape).astype(int)
    disp = np.empty(3, dtype=int)
    lattice = gd.cell_cv
    vol = np.abs(np.linalg.det(lattice))
    for i in range(3):
        res = np.cross(lattice[(i + 1) % 3], lattice[(i + 2) % 3])
        # TODO unnecessarily conservative buffer?
        disp[i] = np.ceil(np.linalg.norm(res) * rmax / vol * shape[i]) + 1 + buffer
    indices = [
        np.arange(center[i] - disp[i], center[i] + disp[i] + 1) for i in range(3)
    ]
    fdisps = []
    for i in range(3):
        fdisps.append(indices[i].astype(np.float64) / shape[i] - spos_c[i])
    indices = [ind % s for ind, s in zip(indices, shape)]
    if get_global_disps:
        return indices, fdisps
    lbound_inds, ubound_inds = fft_obj.get_bound_inds()
    conds = []
    for i in range(3):
        conds.append(
            np.logical_and(indices[i] >= lbound_inds[i], indices[i] < ubound_inds[i])
        )
    local_indices = [indices[i][conds[i]] - lbound_inds[i] for i in range(3)]
    local_fdisps = [fdisps[i][conds[i]] for i in range(3)]
    return local_indices, local_fdisps


class FastAtomPASDWSlice(AtomPASDWSlice):
    def __init__(self, *args, **kwargs):
        super(FastAtomPASDWSlice, self).__init__(*args, **kwargs)
        self.indset = np.ascontiguousarray(self.indset.astype(np.int64))

    @classmethod
    def from_gd_and_setup(
        cls,
        gd,
        spos_c,
        psetup,
        fft_obj,
        rmax=0,
        sphere=True,
        ovlp_fit=False,
        store_funcs=False,
        is_global=False,
    ):
        if ovlp_fit and not is_global:
            raise ValueError("Need global grid set for overlap fitting")
        rgd = psetup.interp_rgd
        if rmax == 0:
            rmax = psetup.rcut
        shape = gd.get_size_of_global_array()
        indices, fdisps = get_ag_indices(
            fft_obj, gd, shape, spos_c, rmax, buffer=0, get_global_disps=is_global
        )
        # NOTE: If is_global, indset is incorrect and will cause problems.
        # When is_global is True, this object should only be used for calculating
        # the overlap fitting, not for going between atoms and FFT grids
        indset, rad_g, rhat_gv = fft_obj.get_radial_info_for_atom(indices, fdisps)
        if sphere:
            cond = rad_g <= rmax
            indset = indset[cond]
            rad_g = rad_g[cond]
            rhat_gv = rhat_gv[cond]

        h = rgd.r_g[1] - rgd.r_g[0]
        dg = rad_g / h  # TODO only for equidist grid
        g = np.floor(dg).astype(np.int32)
        g = np.minimum(g, rgd.r_g.size - 1)
        dg -= g

        return cls(
            indset,
            g,
            dg,
            rad_g,
            rhat_gv,
            rmax,
            psetup,
            gd.dv,
            h,
            ovlp_fit=ovlp_fit,
            store_funcs=store_funcs,
        )


class _PAWCiderContribs:

    nspin: int
    ccl: ConvolutionCollection
    grids_indexer: AtomicGridsIndexer
    plan: NLDFAuxiliaryPlan

    def __init__(self, plan, cider_kernel, ccl, xcc):
        self.plan = plan
        self.cider_kernel = cider_kernel
        self.ccl = ccl
        self.ccl.compute_integrals_()
        self.ccl.solve_projection_coefficients()
        self.w_g = (xcc.rgd.dv_g[:, None] * weight_n).ravel()
        self.r_g = xcc.rgd.r_g
        self.xcc = xcc
        self.grids_indexer = AtomicGridsIndexer.make_single_atom_indexer(
            Y_nL[:, : xcc.Lmax], self.r_g
        )
        self.grids_indexer.set_weights(self.w_g)
        from gpaw.utilities.timing import Timer

        self._atco_recip = self.ccl.atco_inp.get_reciprocal_atco()

        self.timer = Timer()

    @classmethod
    def from_plan(cls, plan, cider_kernel, Z, xcc, beta=2.2):
        lmax = int(np.sqrt(xcc.Lmax + 1e-8)) - 1
        rmax = np.max(xcc.rgd.r_g)
        # TODO tune this and adjust size
        # min_exps = 0.5 / (rmax * rmax) * np.ones(lp1)
        min_exps = 0.125 / (rmax * rmax) * np.ones(4)
        max_exps = get_hgbs_max_exps(Z)
        etb = get_etb_from_expnt_range(
            lmax, beta, min_exps, max_exps, 0.0, 1e99, lower_fac=1.0, upper_fac=1.0
        )
        inputs_to_atco = get_gamma_lists_from_etb_list([etb])
        atco = ATCBasis(*inputs_to_atco)
        has_vj, ifeat_ids = get_ccl_settings(plan)
        ccl = ConvolutionCollection(
            atco, atco, plan.alphas, plan.alpha_norms, has_vj, ifeat_ids
        )
        # TODO need to define w_g
        return cls(plan, cider_kernel, ccl, xcc)

    @property
    def is_mgga(self):
        return self.plan.nldf_settings.sl_level == "MGGA"

    @property
    def nspin(self):
        return self.plan.nspin

    def get_kinetic_energy(self, ae):
        nspins = self._D_sp.shape[0]
        xcc = self.xcc
        if ae:
            tau_pg = xcc.tau_pg
            tauc_g = xcc.tauc_g / (np.sqrt(4 * np.pi) * nspins)
        else:
            tau_pg = xcc.taut_pg
            tauc_g = xcc.tauct_g / (np.sqrt(4 * np.pi) * nspins)
        nn = tau_pg.shape[-1] // tauc_g.shape[0]
        tau_sg = np.dot(self._D_sp, tau_pg)
        tau_sg.shape = (tau_sg.shape[0], -1, nn)
        tau_sg[:] += tauc_g[:, None]
        tau_sg.shape = (tau_sg.shape[0], -1)
        return tau_sg

    def contract_kinetic_potential(self, dedtau_sg, ae):
        xcc = self.xcc
        if ae:
            tau_pg = xcc.tau_pg
            sign = 1.0
        else:
            tau_pg = xcc.taut_pg
            sign = -1.0
        wt = self.w_g * sign
        self._dH_sp += np.dot(dedtau_sg * wt, tau_pg.T)

    def vec_radial_vars(self, n_sLg, Y_nL, dndr_sLg, rnablaY_nLv, ae):
        nspin = len(n_sLg)
        ngrid = n_sLg.shape[-1] * weight_n.size
        nx = 5 if self.is_mgga else 4
        rho_sxg = np.empty((nspin, nx, ngrid))
        rho_sxg[:, 0] = np.dot(Y_nL, n_sLg).transpose(1, 2, 0).reshape(nspin, -1)
        b_vsgn = np.dot(rnablaY_nLv.transpose(0, 2, 1), n_sLg).transpose(1, 2, 3, 0)
        b_vsgn[..., 1:, :] /= self.xcc.rgd.r_g[1:, None]
        b_vsgn[..., 0, :] = b_vsgn[..., 1, :]
        a_sgn = np.dot(Y_nL, dndr_sLg).transpose(1, 2, 0)
        b_vsgn += R_nv.T[:, None, None, :] * a_sgn[None, :, :]
        N = Y_nL.shape[0]
        e_g = self.xcc.rgd.empty(N).reshape(-1)
        e_g[:] = 0
        rho_sxg[:, 1:4] = b_vsgn.transpose(1, 0, 2, 3).reshape(nspin, 3, -1)
        if self.is_mgga:
            rho_sxg[:, 4] = self.get_kinetic_energy(ae)
        vrho_sxg = np.zeros_like(rho_sxg)
        return e_g, rho_sxg, vrho_sxg

    @property
    def atco_feat(self):
        # TODO this will need to be different for vi and vij
        return self.ccl.atco_out

    def convert_rad2orb_(
        self, nspin, x_sgLq, x_suq, rad2orb=True, inp=True, indexer=None
    ):
        if inp:
            atco = self.ccl.atco_inp
        else:
            atco = self.atco_feat
        if indexer is None:
            indexer = self.grids_indexer
        for s in range(nspin):
            atco.convert_rad2orb_(
                x_sgLq[s],
                x_suq[s],
                indexer,
                indexer.rad_arr,
                rad2orb=rad2orb,
                offset=0,
            )

    def set_D_sp(self, D_sp, xcc):
        self._dH_sp = np.zeros_like(D_sp)
        self._D_sp = D_sp

    def get_dH_sp(self):
        return self._dH_sp

    def get_paw_atom_contribs(self, rho_sxg):
        nspin = self.nspin
        assert len(rho_sxg) == nspin
        x_gq = self.grids_indexer.empty_gq(nalpha=self.plan.nalpha)
        x_srLq = np.stack(
            [
                self.grids_indexer.empty_rlmq(nalpha=self.plan.nalpha)
                for s in range(nspin)
            ]
        )
        for s in range(nspin):
            rho_tuple = self.plan.get_rho_tuple(rho_sxg[s])
            arg_g = self.plan.get_interpolation_arguments(rho_tuple, i=-1)[0]
            fun_g = self.plan.get_function_to_convolve(rho_tuple)[0]
            fun_g[:] *= self.w_g
            p_gq = self.plan.get_interpolation_coefficients(arg_g.ravel(), i=-1)[0]
            x_gq[:] = p_gq * fun_g[:, None]
            self.grids_indexer.reduce_angc_ylm_(x_srLq[s], x_gq, a2y=True, offset=0)
        return x_srLq

    def get_paw_atom_contribs_en(self, e_g, rho_sxg, vrho_sxg, f_srLq, feat_only=False):
        # NOTE that grid index order is different than previous implementation
        # gn instead of previous ng
        nspin = rho_sxg.shape[0]
        ngrid = rho_sxg.shape[2]
        f_srLq = np.ascontiguousarray(f_srLq)
        vf_srLq = np.empty_like(f_srLq)
        feat_sig = np.zeros((nspin, self.plan.nldf_settings.nfeat, ngrid))
        dfeat_sig = np.zeros((nspin, self.plan.num_vj, ngrid))
        f_gq = self.grids_indexer.empty_gq(nalpha=self.plan.nalpha)
        for s in range(nspin):
            self.grids_indexer.reduce_angc_ylm_(f_srLq[s], f_gq, a2y=False, offset=0)
            self.plan.eval_rho_full(
                f_gq,
                rho_sxg[s],
                spin=s,
                feat=feat_sig[s],
                dfeat=dfeat_sig[s],
                cache_p=True,
                # TODO might need to be True for gaussian interp
                apply_transformation=False,
                # coeff_multipliers=self.plan.alpha_norms,
            )
        if feat_only:
            return feat_sig
        sigma_xg = get_sigma(rho_sxg[:, 1:4])
        dedsigma_xg = np.zeros_like(sigma_xg)
        nspin = feat_sig.shape[0]
        dedn_sg = np.zeros_like(vrho_sxg[:, 0])
        e_g[:] = 0.0
        args = [
            e_g,
            rho_sxg[:, 0].copy(),
            dedn_sg,
            sigma_xg,
            dedsigma_xg,
        ]
        if rho_sxg.shape[1] == 4:
            args.append(feat_sig)
        elif rho_sxg.shape[1] == 5:
            dedtau_sg = np.zeros_like(vrho_sxg[:, 0])
            args += [
                rho_sxg[:, 4].copy(),
                dedtau_sg,
                feat_sig,
            ]
        else:
            raise ValueError
        vfeat_sig = self.cider_kernel.calculate(*args)
        vrho_sxg[:, 0] = dedn_sg
        if rho_sxg.shape[1] == 5:
            vrho_sxg[:, 4] = dedtau_sg
        vrho_sxg[:, 1:4] = 2 * dedsigma_xg[::2, None, :] * rho_sxg[:, 1:4]
        if nspin == 2:
            vrho_sxg[:, 1:4] += dedsigma_xg[1, None, :] * rho_sxg[::-1, 1:4]
        for s in range(nspin):
            f_gq[:] = self.plan.eval_vxc_full(
                vfeat_sig[s],
                vrho_sxg[s],
                dfeat_sig[s],
                rho_sxg[s],
                spin=s,
            )
            f_gq[:] *= self.w_g[:, None]
            self.grids_indexer.reduce_angc_ylm_(vf_srLq[s], f_gq, a2y=True, offset=0)
        return vf_srLq

    def get_paw_atom_contribs_pot(self, rho_sxg, vrho_sxg, vx_srLq):
        nspin = self.nspin
        vx_gq = self.grids_indexer.empty_gq(nalpha=self.plan.nalpha)
        for s in range(nspin):
            self.grids_indexer.reduce_angc_ylm_(vx_srLq[s], vx_gq, a2y=False, offset=0)
            rho_tuple = self.plan.get_rho_tuple(rho_sxg[s])
            arg_g, darg_g = self.plan.get_interpolation_arguments(rho_tuple, i=-1)
            # TODO need to account for dfunc
            fun_g, dfun_g = self.plan.get_function_to_convolve(rho_tuple)
            p_gq, dp_gq = self.plan.get_interpolation_coefficients(arg_g, i=-1)
            dedarg_g = np.einsum("gq,gq->g", dp_gq, vx_gq) * fun_g
            dedfun_g = np.einsum("gq,gq->g", p_gq, vx_gq)
            vrho_sxg[s, 0] += dedarg_g * darg_g[0] + dedfun_g * dfun_g[0]
            tmp = dedarg_g * darg_g[1] + dedfun_g * dfun_g[1]
            vrho_sxg[s, 1:4] += 2 * rho_sxg[s, 1:4] * tmp
            if len(darg_g) == 3:
                vrho_sxg[s, 4] += dedarg_g * darg_g[2] + dedfun_g * dfun_g[2]

    def get_paw_conv_feature_terms(self, xt_sgLq, dx_sgLq, rgd):
        raise NotImplementedError

    def get_paw_conv_potential_terms(self, vyt_sgLq, vdy_skLq, rgd):
        raise NotImplementedError


class PAWCiderContribsRecip(_PAWCiderContribs):
    def r2k(self, in_sxLq, rgd, fwd=True):
        if fwd:
            atco_inp = self.ccl.atco_inp
            atco_out = self._atco_recip
            in_g = self.grids_indexer.rad_arr
            out_g = rgd.k_g
        else:
            atco_inp = self._atco_recip
            atco_out = self.ccl.atco_inp
            in_g = rgd.k_g
            out_g = self.grids_indexer.rad_arr
        in_sxLq = np.ascontiguousarray(in_sxLq)
        nspin, nx, nlm, nq = in_sxLq.shape
        nx = out_g.size
        shape = (nspin, nx, nlm, nq)
        out_sxLq = np.zeros(shape)
        y = Y_nL[:, :nlm]
        in_indexer = AtomicGridsIndexer.make_single_atom_indexer(y, in_g)
        out_indexer = AtomicGridsIndexer.make_single_atom_indexer(y, out_g)
        assert in_sxLq.flags.c_contiguous
        assert out_sxLq.flags.c_contiguous
        for s in range(nspin):
            tmp_uq = np.zeros((atco_inp.nao, nq))
            atco_inp.convert_rad2orb_(
                in_sxLq[s],
                tmp_uq,
                in_indexer,
                in_indexer.rad_arr,
                rad2orb=True,
                offset=0,
            )
            libcider.solve_atc_coefs_arr_ccl(
                self.ccl._ccl,
                tmp_uq.ctypes.data_as(ctypes.c_void_p),
                ctypes.c_int(tmp_uq.shape[-1]),
                ctypes.c_int(0),
            )
            atco_out.convert_rad2orb_(
                out_sxLq[s],
                tmp_uq,
                out_indexer,
                out_indexer.rad_arr,
                rad2orb=False,
                offset=0,
            )
        return out_sxLq

    def calc_conv_skLq(self, in_skLq, rgd):
        in_skLq = np.ascontiguousarray(in_skLq)
        out_skLq = np.empty_like(in_skLq)
        k_g = np.ascontiguousarray(rgd.k_g)
        nspin, nk, nlm, nq = in_skLq.shape
        assert nk == k_g.size
        assert nq == self.plan.alphas.size
        libcider.atc_reciprocal_convolution(
            in_skLq.ctypes.data_as(ctypes.c_void_p),
            out_skLq.ctypes.data_as(ctypes.c_void_p),
            k_g.ctypes.data_as(ctypes.c_void_p),
            self.plan.alphas.ctypes.data_as(ctypes.c_void_p),
            self.plan.alpha_norms.ctypes.data_as(ctypes.c_void_p),
            ctypes.c_int(nspin),
            ctypes.c_int(nk),
            ctypes.c_int(nlm),
            ctypes.c_int(nq),
        )
        return out_skLq

    def get_paw_conv_feature_terms(self, xt_sgLq, dx_sgLq, rgd):
        xt_skLq = self.r2k(xt_sgLq, rgd, fwd=True)
        dx_skLq = self.r2k(dx_sgLq, rgd, fwd=True)
        yt_skLq = self.calc_conv_skLq(xt_skLq, rgd)
        yt_skLq[:] *= get_dvk(rgd)[:, None, None]
        yt_sgLq = self.r2k(yt_skLq, rgd, fwd=False)
        dy_skLq = self.calc_conv_skLq(dx_skLq, rgd)
        return yt_sgLq, dy_skLq

    def get_paw_conv_potential_terms(self, vyt_sgLq, vdy_skLq, rgd):
        # rgd = setup.nlxc_correction.big_rgd
        vyt_skLq = self.r2k(vyt_sgLq, rgd, fwd=True)
        vyt_skLq[:] *= get_dvk(rgd)[:, None, None]
        vdx_skLq = self.calc_conv_skLq(vdy_skLq, rgd)
        vxt_skLq = self.calc_conv_skLq(vyt_skLq, rgd)
        vxt_sgLq = self.r2k(vxt_skLq, rgd, fwd=False)
        vdx_sgLq = self.r2k(vdx_skLq, rgd, fwd=False)
        return vxt_sgLq, vdx_sgLq


class PAWCiderContribsOrb(_PAWCiderContribs):
    def perform_convolution(
        self,
        theta_sgLq,
        fwd=True,
        out_sgLq=None,
        return_orbs=False,
        take_orbs=False,
        indexer_in=None,
        indexer_out=None,
    ):
        theta_sgLq = np.ascontiguousarray(theta_sgLq)
        nspin = self.nspin
        nalpha = self.ccl.nalpha
        nbeta = self.ccl.nbeta
        is_not_vi = self.plan.nldf_settings.nldf_type != "i"
        inp_shape = (nspin, self.ccl.atco_inp.nao, nalpha)
        out_shape = (nspin, self.ccl.atco_out.nao, nbeta)
        if indexer_in is None:
            indexer_in = self.grids_indexer
        if indexer_out is None:
            indexer_out = self.grids_indexer
        if out_sgLq is None and not return_orbs:
            if fwd:
                nout = nbeta
            else:
                nout = nalpha
            out_sgLq = np.stack(
                [indexer_out.empty_rlmq(nalpha=nout) for s in range(nspin)]
            )
            out_sgLq[:] = 0.0
        if fwd:
            i_inp = -1
            i_out = 0
            if take_orbs:
                theta_suq = theta_sgLq
                assert theta_suq.shape == inp_shape
            else:
                theta_suq = np.zeros(inp_shape, order="C")
            conv_svq = np.zeros(out_shape, order="C")
        else:
            i_inp = 0
            i_out = -1
            if take_orbs:
                theta_suq = theta_sgLq
                assert theta_suq.shape == out_shape
            else:
                theta_suq = np.zeros(out_shape, order="C")
            conv_svq = np.zeros(inp_shape, order="C")
        if not take_orbs:
            self.convert_rad2orb_(
                nspin, theta_sgLq, theta_suq, rad2orb=True, inp=fwd, indexer=indexer_in
            )
        for s in range(nspin):
            if (fwd) or (is_not_vi):
                self.plan.get_transformed_interpolation_terms(
                    theta_suq[s, :, : self.plan.nalpha], i=i_inp, fwd=fwd, inplace=True
                )
            self.ccl.multiply_atc_integrals(theta_suq[s], output=conv_svq[s], fwd=fwd)
            if (not fwd) or (is_not_vi):
                self.plan.get_transformed_interpolation_terms(
                    conv_svq[s, :, : self.plan.nalpha],
                    i=i_out,
                    fwd=not fwd,
                    inplace=True,
                )
        if return_orbs:
            return conv_svq
        # TODO this should be a modified atco for l1 interpolation
        self.convert_rad2orb_(
            nspin, out_sgLq, conv_svq, rad2orb=False, inp=not fwd, indexer=indexer_out
        )
        return out_sgLq

    def get_paw_conv_feature_terms(self, xt_sgLq, dx_sgLq, rgd):
        nspin = xt_sgLq.shape[0]
        Lmax = xt_sgLq.shape[2]
        yt_sgLq = self.perform_convolution(xt_sgLq, fwd=True, return_orbs=False)
        dy_svq = self.perform_convolution(
            dx_sgLq,
            fwd=True,
            return_orbs=True,
        )
        shape = list(yt_sgLq.shape)
        shape[1] = rgd.r_g.size
        dy_skLq = np.zeros(shape)
        indexerk = AtomicGridsIndexer.make_single_atom_indexer(Y_nL[:, :Lmax], rgd.k_g)
        new_atco = self.atco_feat.get_reciprocal_atco()
        for s in range(nspin):
            new_atco.convert_rad2orb_(
                dy_skLq[s],
                dy_svq[s],
                indexerk,
                indexerk.rad_arr,
                rad2orb=False,
                offset=0,
            )
        return yt_sgLq, dy_skLq

    def get_paw_conv_potential_terms(self, vyt_sgLq, vdy_skLq, rgd):
        nspin = vyt_sgLq.shape[0]
        Lmax = vyt_sgLq.shape[2]
        new_atco = self.atco_feat.get_reciprocal_atco()
        vdy_svq = np.zeros((nspin, new_atco.nao, self.plan.nalpha))
        indexerk = AtomicGridsIndexer.make_single_atom_indexer(Y_nL[:, :Lmax], rgd.k_g)
        for s in range(nspin):
            new_atco.convert_rad2orb_(
                vdy_skLq[s],
                vdy_svq[s],
                indexerk,
                indexerk.rad_arr,
                rad2orb=True,
                offset=0,
            )
        # TODO would be faster to stay in orbital space for
        # get_f_realspace_contribs and get_df_only
        vdx_sgLq = self.perform_convolution(
            vdy_svq,
            fwd=False,
            take_orbs=True,
        )
        vxt_sgLq = self.perform_convolution(vyt_sgLq, fwd=False, take_orbs=False)
        return vxt_sgLq, vdx_sgLq


if USE_GAUSSIAN_PAW_CONV:
    PAWCiderContribs = PAWCiderContribsOrb
else:
    PAWCiderContribs = PAWCiderContribsRecip


class CiderRadialFeatureCalculator:
    def __init__(self, xc):
        self.xc = xc
        self.mode = "feature"

    def __call__(self, rgd, n_sLg, Y_nL, dndr_sLg, rnablaY_nLv, ae=True):
        e_g, rho_sxg, vrho_sxg = self.xc.vec_radial_vars(
            n_sLg, Y_nL, dndr_sLg, rnablaY_nLv, ae
        )
        return self.xc.get_paw_atom_contribs(rho_sxg)


class CiderRadialEnergyCalculator:
    def __init__(self, xc):
        self.xc = xc
        self.mode = "energy"

    def __call__(self, rgd, n_sLg, Y_nL, dndr_sLg, rnablaY_nLv, f_srLq, ae=True):
        e_g, rho_sxg, vrho_sxg = self.xc.vec_radial_vars(
            n_sLg, Y_nL, dndr_sLg, rnablaY_nLv, ae
        )
        vf_srLq = self.xc.get_paw_atom_contribs_en(
            e_g, rho_sxg, vrho_sxg, f_srLq, feat_only=False
        )
        if rho_sxg.shape[1] == 5:
            self.xc.contract_kinetic_potential(vrho_sxg[:, 4], ae)
        return e_g, vrho_sxg[:, 0], vrho_sxg[:, 1:4], vf_srLq


class CiderRadialPotentialCalculator:
    def __init__(self, xc):
        self.xc = xc
        self.mode = "potential"

    def __call__(self, rgd, n_sLg, Y_nL, dndr_sLg, rnablaY_nLv, vx_srLq, ae=True):
        e_g, rho_sxg, vrho_sxg = self.xc.vec_radial_vars(
            n_sLg, Y_nL, dndr_sLg, rnablaY_nLv, ae
        )
        self.xc.get_paw_atom_contribs_pot(rho_sxg, vrho_sxg, vx_srLq)
        if rho_sxg.shape[1] == 5:
            self.xc.contract_kinetic_potential(vrho_sxg[:, 4], ae)
        return vrho_sxg[:, 0], vrho_sxg[:, 1:4]


class CiderRadialExpansion:
    def __init__(self, rcalc, ft_srLq=None, df_srLq=None):
        self.rcalc = rcalc
        self.ft_srLq = ft_srLq
        self.df_srLq = df_srLq
        assert self.rcalc.mode in [
            "feature",
            "energy",
            "potential",
        ]

    def __call__(self, rgd, D_sLq, n_qg, dndr_qg, nc0_sg, dnc0dr_sg, ae=True):
        n_sLg = np.dot(D_sLq, n_qg)
        n_sLg[:, 0] += nc0_sg
        dndr_sLg = np.dot(D_sLq, dndr_qg)
        dndr_sLg[:, 0] += dnc0dr_sg
        nspins, Lmax, nq = D_sLq.shape
        if self.rcalc.mode == "feature":
            # We call it wx_srLq because it is multiplied
            # by the grid weights already
            wx_srLq = self.rcalc(
                rgd, n_sLg, Y_nL[:, :Lmax], dndr_sLg, rnablaY_nLv[:, :Lmax], ae=ae
            )
            return wx_srLq
        elif self.rcalc.mode == "energy":
            dEdD_sqL = np.zeros((nspins, nq, Lmax))
            f_srLq = self.ft_srLq + self.df_srLq if ae else self.ft_srLq
            e_g, dedn_sg, dedgrad_svg, vf_srLq = self.rcalc(
                rgd,
                n_sLg,
                Y_nL[:, :Lmax],
                dndr_sLg,
                rnablaY_nLv[:, :Lmax],
                f_srLq,
                ae=ae,
            )
            nn = Y_nL.shape[0]
            nq = f_srLq.shape[-1]
            dedn_sgn = dedn_sg.reshape(nspins, -1, nn)
            dedgrad_svgn = dedgrad_svg.reshape(nspins, 3, -1, nn)
            dedgrad_sgn = np.einsum("svgn,nv->sgn", dedgrad_svgn, R_nv)
            dEdD_sqn = np.einsum("qg,sgn->sqn", n_qg * rgd.dv_g, dedn_sgn)
            dEdD_sqn += np.einsum("qg,sgn->sqn", dndr_qg * rgd.dv_g, dedgrad_sgn)
            dEdD_sqL = np.einsum(
                "sqn,nL->sqL", dEdD_sqn, weight_n[:, None] * Y_nL[:, :Lmax]
            )
            tmp = rgd.dr_g * rgd.r_g
            B_svqn = np.einsum("qg,svgn->svqn", n_qg * tmp, dedgrad_svgn)
            dEdD_sqL += np.einsum(
                "nLv,svqn->sqL",
                (4 * np.pi) * weight_n[:, None, None] * rnablaY_nLv[:, :Lmax],
                B_svqn,
            )
            E = rgd.integrate(e_g.reshape(-1, nn).dot(weight_n))
            return E, dEdD_sqL, vf_srLq
        else:
            f_srLq = self.ft_srLq + self.df_srLq if ae else self.ft_srLq
            dedn_sg, dedgrad_svg = self.rcalc(
                rgd,
                n_sLg,
                Y_nL[:, :Lmax],
                dndr_sLg,
                rnablaY_nLv[:, :Lmax],
                f_srLq,
                ae=ae,
            )
            nn = Y_nL.shape[0]
            nq = f_srLq.shape[-1]
            dedn_sgn = dedn_sg.reshape(nspins, -1, nn)
            dedgrad_svgn = dedgrad_svg.reshape(nspins, 3, -1, nn)
            dedgrad_sgn = np.einsum("svgn,nv->sgn", dedgrad_svgn, R_nv)
            dEdD_sqn = np.einsum("qg,sgn->sqn", n_qg * rgd.dv_g, dedn_sgn)
            dEdD_sqn += np.einsum("qg,sgn->sqn", dndr_qg * rgd.dv_g, dedgrad_sgn)
            # dEdD_sqn = np.einsum("qg,sgn->sqn", n_qg, dedn_sgn)
            # dEdD_sqn += np.einsum("qg,sgn->sqn", dndr_qg, dedgrad_sgn)
            dEdD_sqL = np.einsum(
                "sqn,nL->sqL", dEdD_sqn, weight_n[:, None] * Y_nL[:, :Lmax]
            )
            tmp = rgd.dr_g * rgd.r_g
            B_svqn = np.einsum("qg,svgn->svqn", n_qg * tmp, dedgrad_svgn)
            dEdD_sqL += np.einsum(
                "nLv,svqn->sqL",
                (4 * np.pi) * weight_n[:, None, None] * rnablaY_nLv[:, :Lmax],
                B_svqn,
            )
            return dEdD_sqL


class FastPASDWCiderKernel:
    is_cider_functional = True
    """
    This class is a bit confusing because there are a lot of intermediate
    terms and contributions for the pseudo and all-electron parts of the
    feature convolutions. Here is a summary of the terms:
        xt  : The theta functions for the pseudo-density
        x   : The theta functions for the all-electron density
        dx  : x - xt
        yt  : Raw convolutions xt
        dy  : Raw convolutions of dx
        c   : Coefficients for functions to smoothly fit the (non-smooth)
              dx such that dy and dfr match outside the core radius
        dfr : The values of the convolutions of the PASDW projector functions,
              weighted by c
        fr  : dfr + yt
        ft  : fr + contributions to the convolutions from other atoms, which
              are computed based on the projections of the FFT convolutions
              onto smooth atomic functions.
        df  : Projection of (dy - dfr) onto some basis that decays outside
              the core radius. The idea is that df holds all the high-frequency
              parts of the convolutions and (approximately) vanishes
              outside the core radius.
        f   : df + ft, should accurately approximate the "true" all-electron
              features in the core radius
        v*  : Derivative of the XC energy with respect to *.
              vf, vdf, vft, vfr, etc... are all defined like this.

    After the underscore on each array, the indices are indicated:
        a : Atom
        L : l,m spherical harmonic index
        i : Projector function index
        u : Dense atomic density fitting basis set index
        q : Index for the theta functions or convolutions
        g : Radial grid index
        s : Spin index
    """

    fr_asgLq: dict
    df_asgLq: dict
    # TODO below vfr and vdf not needed, can reuse fr and df to save memory
    vfr_asgLq: dict
    vdf_asgLq: dict
    rho_asxg: dict
    vrho_asxg: dict
    rhot_asxg: dict
    vrhot_asxg: dict
    # TODO This can be reused for D_asiq and vc_asiq
    c_asiq: dict

    def __init__(self, cider_kernel, plan, gd, cut_xcgrid, timer=None):
        if timer is None:
            from gpaw.utilities.timing import Timer

            self.timer = Timer()
        self.gd = gd
        self.bas_exp_fit = plan.alphas
        self.cider_kernel = cider_kernel
        self.Nalpha_small = plan.nalpha
        self.cut_xcgrid = cut_xcgrid
        self._amin = np.min(self.bas_exp_fit)
        self.plan = plan
        self.is_mgga = plan.nldf_settings.sl_level == "MGGA"

    @property
    def lambd(self):
        return self.plan.lambd

    def get_D_asp(self):
        return self.atomdist.to_work(self.dens.D_asp)

    def initialize(self, density, atomdist, atom_partition, setups):
        self.dens = density
        self.atomdist = atomdist
        self.atom_partition = atom_partition
        self.setups = setups

    def initialize_more_things(self, setups=None):
        self.nspin = self.dens.nt_sg.shape[0]
        if setups is None:
            setups = self.dens.setups
        for setup in setups:
            if not hasattr(setup, "cider_contribs"):
                setup.xc_correction = DiffPAWXCCorrection.from_setup(
                    setup, build_kinetic=self.is_mgga, ke_order_ng=False
                )
                # TODO it would be good to remove this
                setup.nlxc_correction = SBTGridContainer.from_setup(
                    # setup, rmin=setup.xc_correction.rgd.r_g[0]+1e-5, N=1024, encut=1e6, d=0.018
                    setup,
                    rmin=1e-4,
                    N=512,
                    encut=5e4,
                    d=0.02,
                )
                encut = setup.Z**2 * 20
                if encut - 1e-7 <= np.max(self.bas_exp_fit):
                    encut0 = np.max(self.bas_exp_fit)
                    Nalpha = self.bas_exp_fit.size
                else:
                    Nalpha = (
                        int(np.ceil(np.log(encut / self._amin) / np.log(self.lambd)))
                        + 1
                    )
                    encut0 = self._amin * self.lambd ** (Nalpha - 1)
                    assert encut0 >= encut - 1e-6, "Math went wrong {} {}".format(
                        encut0, encut
                    )
                    assert (
                        encut0 / self.lambd < encut
                    ), "Math went wrong {} {} {} {}".format(
                        encut0, encut, self.lambd, encut0 / self.lambd
                    )
                atom_plan = self.plan.new(nalpha=Nalpha)
                setup.cider_contribs = PAWCiderContribs.from_plan(
                    atom_plan, self.cider_kernel, setup.Z, setup.xc_correction, beta=1.8
                )
                if setup.cider_contribs.plan is not None:
                    assert (
                        abs(np.max(setup.cider_contribs.plan.alphas) - encut0) < 1e-10
                    )
                setup.ps_setup = PSmoothSetup.from_setup(
                    setup,
                    self.bas_exp_fit,
                    self.bas_exp_fit,
                    alpha_norms=self.plan.alpha_norms,
                    pmax=encut0,
                )
                setup.pa_setup = PAugSetup.from_setup(setup)

    def calculate_paw_cider_features(self, setups, D_asp):
        """
        Computes the contributions to the convolutions F_beta arising
        from the PAW contributions. Projects them onto
        the PAW grids, and also computes the compensation function
        coefficients c_sia that reproduce the F_beta contributions
        outside the augmentation region.

        Args:
            setups: GPAW atomic setups
            D_asp: Atomic PAW density matrices
        """
        if len(D_asp.keys()) == 0:
            return {}, {}
        a0 = list(D_asp.keys())[0]
        nspin = D_asp[a0].shape[0]
        assert (nspin == 1) or self.is_cider_functional
        self.fr_asgLq = {}
        self.df_asgLq = {}
        self.c_asiq = {}

        for a, D_sp in D_asp.items():
            setup = setups[a]
            psetup = setup.ps_setup
            Nalpha_sm = psetup.phi_jabg.shape[1]
            rgd = setup.nlxc_correction.big_rgd
            slgd = setup.xc_correction.rgd
            RCUT = np.max(setup.rcut_j)

            self.timer.start("coefs")
            rcalc = CiderRadialFeatureCalculator(setup.cider_contribs)
            expansion = CiderRadialExpansion(rcalc)
            dx_sgLq, xt_sgLq = calculate_cider_paw_correction(
                expansion, setup, D_sp, separate_ae_ps=True
            )
            dx_sgLq -= xt_sgLq
            self.timer.stop()

            rcut = RCUT
            rmax = slgd.r_g[-1]
            fcut = 0.5 + 0.5 * np.cos(np.pi * (slgd.r_g - rcut) / (rmax - rcut))
            fcut[slgd.r_g < rcut] = 1.0
            fcut[slgd.r_g > rmax] = 0.0
            xt_sgLq *= fcut[:, None, None]

            self.timer.start("transform and convolve")
            yt_sgLq, dy_skLq = setup.cider_contribs.get_paw_conv_feature_terms(
                xt_sgLq, dx_sgLq, rgd
            )
            self.timer.stop()
            self.timer.start("separate long and short-range")
            # TODO between here and the block comment should be replaced
            # with the code in the block comment after everything else works.
            dy_sqLk = dy_skLq.transpose(0, 3, 2, 1)

            c_siq, df_sLpq = psetup.get_c_and_df(
                dy_sqLk[:, :Nalpha_sm], realspace=False
            )
            dfr_sgLq = psetup.get_f_realspace_contribs(c_siq, sl=True).transpose(
                0, 3, 2, 1
            )
            df_sLpq = np.append(
                df_sLpq,
                psetup.get_df_only(
                    dy_sqLk[:, Nalpha_sm:], rgd, sl=False, realspace=False
                ),
                axis=-1,
            )
            df_sgLq = psetup.get_df_realspace_contribs(df_sLpq, sl=True).transpose(
                0, 3, 2, 1
            )
            """
            c_siq, df_suq = psetup.get_c_and_df(
                dy_sgLq[..., :Nalpha_sm], realspace=True
            )
            dfr_sgLq = psetup.get_f_realspace_contribs(c_siq, sl=True)
            df_suq = np.append(
                df_suq,
                psetup.get_df_only(
                    dy_sgLq[..., Nalpha_sm:], rgd, sl=False, realspace=True
                ),
                axis=-1,
            )
            df_sgLq = psetup.get_df_realspace_contribs(df_suq, sl=True)
            """
            self.df_asgLq[a] = df_sgLq
            self.fr_asgLq[a] = yt_sgLq
            self.fr_asgLq[a][..., :Nalpha_sm] += dfr_sgLq
            self.timer.stop()
            self.c_asiq[a] = c_siq
        return self.c_asiq, self.df_asgLq

    def calculate_paw_cider_energy(self, setups, D_asp, D_asiq):
        """
        Taking the projections of the pseudo-convolutions F_beta onto the PASDW
        projectors (coefficients given by D_sabi), as well as the
        reference F_beta contributions from part (1), computes the PAW
        XC energy and potential, as well as the functional derivative
        with respect to D_sabi

        Args:
            setups: GPAW atomic setups
            D_asp: Atomic PAW density matrices
            D_asiq: Atomic PAW PASDW projections
        """
        deltaE = {}
        deltaV = {}
        if len(D_asp.keys()) == 0:
            return {}, {}, {}
        a0 = list(D_asp.keys())[0]
        nspin = D_asp[a0].shape[0]
        dvD_asiq = {}
        self.vfr_asgLq = {}
        self.vdf_asgLq = {}

        for a, D_sp in D_asp.items():
            setup = setups[a]
            psetup = setup.pa_setup
            ni = psetup.ni
            Nalpha_sm = D_asiq[a].shape[-1]
            dv_g = get_dv(setup.xc_correction.rgd)
            df_sgLq = self.df_asgLq[a]
            ft_sgLq = np.zeros_like(df_sgLq)
            fr_sgLq = self.fr_asgLq[a]
            # TODO C function for this loop? can go in pa_setup
            for i in range(ni):
                L = psetup.lmlist_i[i]
                n = psetup.nlist_i[i]
                Dref_sq = (psetup.pfuncs_ng[n] * dv_g).dot(fr_sgLq[:, :, L, :Nalpha_sm])
                j = psetup.jlist_i[i]
                for s in range(nspin):
                    ft_sgLq[s, :, L, :Nalpha_sm] += (
                        D_asiq[a][s, i] - Dref_sq[s]
                    ) * psetup.ffuncs_jg[j][:, None]
            ft_sgLq[:] += fr_sgLq
            rcalc = CiderRadialEnergyCalculator(setup.cider_contribs)
            expansion = CiderRadialExpansion(
                rcalc,
                ft_sgLq,
                df_sgLq,
            )
            # The vf_sgLq and vft_sgLq returned by the function below have
            # already been multiplied by the real-space quadrature weights,
            # so dv_g is not applied to them again.
            deltaE[a], deltaV[a], vf_sgLq, vft_sgLq = calculate_cider_paw_correction(
                expansion,
                setup,
                D_sp,
                separate_ae_ps=True,
            )
            vfr_sgLq = vf_sgLq - vft_sgLq
            vft_sgLq[:] = vfr_sgLq
            dvD_asiq[a] = np.zeros((nspin, ni, Nalpha_sm))
            # TODO C function for this loop? can go in pa_setup
            for i in range(ni):
                L = psetup.lmlist_i[i]
                n = psetup.nlist_i[i]
                j = psetup.jlist_i[i]
                dvD_sq = np.einsum(
                    "sgq,g->sq",
                    vft_sgLq[:, :, L, :Nalpha_sm],
                    psetup.ffuncs_jg[j],
                )
                dvD_asiq[a][:, i] = dvD_sq
                # We need to multiply the term here by dv_g because vfr_sgLq
                # has already been multiplied by the quadrature weights.
                vfr_sgLq[:, :, L, :Nalpha_sm] -= (
                    dvD_sq[:, None, :] * psetup.pfuncs_ng[n][:, None]
                ) * dv_g[:, None]
            self.vfr_asgLq[a] = vfr_sgLq
            self.vdf_asgLq[a] = vf_sgLq
        return dvD_asiq, deltaE, deltaV

    def calculate_paw_cider_potential(self, setups, D_asp, vc_asiq):
        """
        Given functional derivatives with respect to the augmentation function
        coefficients (c_sabi), as well as other atom-centered functional
        derivative contributions stored from part (2), compute the XC potential
        arising from the nonlocal density dependence of the CIDER features.

        Args:
            setups: GPAW atomic setups
            D_asp: Atomic PAW density matrices
            vc_asiq: functional derivatives with respect to coefficients c_asiq
        """
        dH_asp = {}
        if len(D_asp.keys()) == 0:
            return {}
        a0 = list(D_asp.keys())[0]
        nspin = D_asp[a0].shape[0]
        assert (nspin == 1) or self.is_cider_functional

        for a, D_sp in D_asp.items():
            setup = setups[a]
            psetup = setup.ps_setup
            slgd = setup.xc_correction.rgd
            Nalpha_sm = psetup.phi_jabg.shape[1]
            RCUT = np.max(setup.rcut_j)
            vc_siq = vc_asiq[a]
            vdf_sgLq = self.vdf_asgLq[a]
            vdfr_sgLq = self.vfr_asgLq[a][..., :Nalpha_sm]
            vyt_sgLq = self.vfr_asgLq[a]
            """
            vc_siq += psetup.get_vf_realspace_contribs(vyt_sgLq, slgd, sl=True)
            vdf_suq = psetup.get_vdf_realspace_contribs(vdf_sgLq, slgd, sl=True)
            vdy_sgLq = psetup.get_v_from_c_and_df(
                vc_siq, vdf_suq[..., :Nalpha_sm], realspace=True
            )
            vdy_sgLq = np.append(
                vdy_sgLq, psetup.get_vdf_only(vdf_sgLq[..., Nalpha_sm:], realspace=True)
            )
            """
            # TODO remove below code block and replace with comment block above
            vc_siq += psetup.get_vf_realspace_contribs(
                vdfr_sgLq.transpose(0, 3, 2, 1), None, sl=True
            )
            vdf_sLpq = psetup.get_vdf_realspace_contribs(
                vdf_sgLq.transpose(0, 3, 2, 1), None, sl=True
            )
            vdy_sqLk = psetup.get_v_from_c_and_df(
                vc_siq, vdf_sLpq[..., :Nalpha_sm], realspace=False
            )
            vdy_sqLk = np.append(
                vdy_sqLk,
                psetup.get_vdf_only(vdf_sLpq[..., Nalpha_sm:], realspace=False),
                axis=1,
            )
            vdy_sgLq = np.ascontiguousarray(vdy_sqLk.transpose(0, 3, 2, 1))
            vdy_sgLq[:] *= get_dvk(setup.nlxc_correction.big_rgd)[
                :, None, None
            ]  # * 2 / np.pi
            vxt_sgLq, vdx_sgLq = setup.cider_contribs.get_paw_conv_potential_terms(
                vyt_sgLq, vdy_sgLq, setup.nlxc_correction.big_rgd
            )

            rcut = RCUT
            rmax = slgd.r_g[-1]
            fcut = 0.5 + 0.5 * np.cos(np.pi * (slgd.r_g - rcut) / (rmax - rcut))
            fcut[slgd.r_g < rcut] = 1.0
            fcut[slgd.r_g > rmax] = 0.0
            vxt_sgLq *= fcut[:, None, None]

            F_sbLg = vdx_sgLq - vxt_sgLq
            y_sbLg = vxt_sgLq.copy()

            rcalc = CiderRadialPotentialCalculator(setup.cider_contribs)
            expansion = CiderRadialExpansion(
                rcalc,
                F_sbLg,
                y_sbLg,
            )
            dH_asp[a] = calculate_cider_paw_correction(
                expansion, setup, D_sp, separate_ae_ps=True
            )
        return dH_asp

    def calculate_paw_feat_corrections(
        self,
        setups,
        D_asp,
        D_asiq=None,
        df_asgLq=None,
        vc_asiq=None,
    ):
        """
        This function performs one of three PAW correction routines,
        depending on the inputs.
            1) If D_sabi is None and vc_sabi is None: Computes the
            contributions to the convolutions F_beta arising
            from the PAW contributions. Projects them onto
            the PAW grids, and also computes the compensation function
            coefficients c_sia that reproduce the F_beta contributions
            outside the augmentation region.
            2) If D_sabi is NOT None (df_asbLg must also be given): Taking the
            projections of the pseudo-convolutions F_beta onto the PASDW
            projectors (coefficients given by D_sabi), as well as the
            reference F_beta contributions from part (1), computes the PAW
            XC energy and potential, as well as the functional derivative
            with respect to D_sabi
            3) If D_sabi is None and vc_sabi is NOT None: Given functional
            derivatives with respect to the augmentation function coefficients
            (c_sabi), as well as other atom-centered functional derivative
            contributions stored from part (2), compute the XC potential arising
            from the nonlocal density dependence of the CIDER features.

        Args:
            setups: GPAW atomic setups
            D_asp: Atomic PAW density matrices
            D_sabi: Atomic PAW PASDW projections
            df_asbLg: delta-F_beta convolutions
            vc_sabi: functional derivatives with respect to coefficients c_sabi
        """
        if D_asiq is None and vc_asiq is None:
            return self.calculate_paw_cider_features(setups, D_asp)
        elif D_asiq is not None:
            assert df_asgLq is not None
            return self.calculate_paw_cider_energy(setups, D_asp, D_asiq)
        else:
            assert vc_asiq is not None
            return self.calculate_paw_cider_potential(setups, D_asp, vc_asiq)


class PASDWData:
    def __init__(
        self,
        pfuncs_ng,
        pfuncs_ntp,
        interp_rgd,
        slrgd,
        nlist_j,
        llist_j,
        lmlist_i,
        jlist_i,
        sbt_rgd,
        alphas,
        nbas_loc,
        rcut_func,
        rcut_feat,
        Z=None,
        alphas_ae=None,
        alpha_norms=None,
        pmax=None,
    ):
        self.nn = pfuncs_ng.shape[0]
        self.ng = pfuncs_ng.shape[1]
        self.nt = pfuncs_ntp.shape[1]
        self.ni = lmlist_i.shape[0]
        self.nj = len(nlist_j)
        self.lmax = np.max(llist_j)
        self.Z = Z

        self.pfuncs_ng = pfuncs_ng
        self.pfuncs_ntp = pfuncs_ntp

        self.nbas_loc = nbas_loc
        self.nlist_j = np.array(nlist_j).astype(np.int32)
        self.llist_j = np.array(llist_j).astype(np.int32)
        self.nlist_i = self.nlist_j[jlist_i]
        self.llist_i = self.llist_j[jlist_i]
        self.lmlist_i = np.array(lmlist_i).astype(np.int32)
        self.jlist_i = np.array(jlist_i).astype(np.int32)

        self.interp_rgd = interp_rgd
        self.slrgd = slrgd
        self.sbt_rgd = sbt_rgd
        self.rmax = self.slrgd.r_g[-1]
        self.rcut_func = rcut_func
        self.rcut_feat = rcut_feat

        self.alphas = alphas
        self.alphas_ae = alphas_ae
        if alpha_norms is None:
            alpha_norms = np.ones_like(alphas_ae)
        self.alpha_norms = alpha_norms
        self.pmax = pmax
        self.nalpha = len(self.alphas)


class PAugSetup(PASDWData):
    def initialize_g2a(self):
        nn = self.nn
        nj = self.nj
        ng = self.ng
        r_g = self.slrgd.r_g
        dr_g = self.slrgd.dr_g
        nlist_j = self.nlist_j

        pfuncs_ng = self.pfuncs_ng
        pfuncs_ntp = self.pfuncs_ntp
        ffuncs_jg = np.zeros((nj, ng))
        ffuncs_jt = np.zeros((nj, self.interp_rgd.r_g.size))
        dv_g = r_g * r_g * dr_g
        filt = self.get_filt(r_g)
        for l in range(self.lmax + 1):
            jmin, jmax = self.nbas_loc[l], self.nbas_loc[l + 1]
            ovlp = np.einsum(
                "ig,jg,g->ij",
                pfuncs_ng[nlist_j[jmin:jmax]],
                pfuncs_ng[nlist_j[jmin:jmax]],
                dv_g * filt,
            )
            c_and_l = cho_factor(ovlp)
            ffuncs_jg[jmin:jmax] = cho_solve(c_and_l, pfuncs_ng[nlist_j[jmin:jmax]])
            ffuncs_jt[jmin:jmax] = cho_solve(
                c_and_l, pfuncs_ntp[nlist_j[jmin:jmax], :, 0]
            )
        self.ffuncs_jtp = np.zeros((nj, self.interp_rgd.r_g.size, 4))
        for j in range(nj):
            self.ffuncs_jtp[j] = spline(
                np.arange(self.interp_rgd.r_g.size).astype(np.float64),
                ffuncs_jt[j],
            )
        self.exact_ovlp_pf = np.identity(self.ni)
        self.ffuncs_jg = ffuncs_jg

        self.pfuncs_ng *= self.get_filt(self.slrgd.r_g)
        for n in range(nn):
            self.pfuncs_ntp[n] = spline(
                np.arange(self.interp_rgd.r_g.size).astype(np.float64),
                get_ffunc3(n, self.interp_rgd.r_g, self.rcut_func)
                * self.get_filt(self.interp_rgd.r_g),
            )
        # TODO should work but dangerous, make cleaner way to set rcut_func
        self.rcut_func = self.rcut_feat

    def get_filt(self, r_g):
        filt = 0.5 + 0.5 * np.cos(np.pi * r_g / self.rcut_feat)
        filt[r_g > self.rcut_feat] = 0.0
        return filt

    def get_fcut(self, r):
        R = self.rcut_feat
        fcut = np.ones_like(r)
        fcut[r > R] = 0.5 + 0.5 * np.cos(np.pi * (r[r > R] - R) / R)
        fcut[r > 2 * R] = 0.0
        return fcut

    @classmethod
    def from_setup(cls, setup):
        rgd = setup.xc_correction.rgd
        RCUT_FAC = 1.0  # TODO was 1.3
        rcut_feat = np.max(setup.rcut_j) * RCUT_FAC
        # rcut_func = rcut_feat * 2.0
        rmax = np.max(setup.xc_correction.rgd.r_g)
        rcut_func = rmax * 0.8

        nt = 100
        interp_rgd = EquidistantRadialGridDescriptor(h=rcut_func / (nt - 1), N=nt)
        interp_r_g = interp_rgd.r_g
        ng = rgd.r_g.size

        if setup.Z > 1000:
            nlist_j = [0, 2, 4, 6, 8, 1, 3, 5, 7, 2, 4, 6, 8, 3, 5, 7, 4, 6, 8]
            llist_j = [0, 0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 4, 4, 4]
            nbas_lst = [5, 4, 4, 3, 3]
            nbas_loc = np.append([0], np.cumsum(nbas_lst)).astype(np.int32)
            lmlist_i = []
            jlist_i = []
        elif setup.Z > 18:
            nlist_j = [0, 2, 4, 6, 1, 3, 5, 2, 4, 6, 3, 5, 4, 6]
            llist_j = [0, 0, 0, 0, 1, 1, 1, 2, 2, 2, 3, 3, 4, 4]
            nbas_lst = [4, 3, 3, 2, 2]
            nbas_loc = np.append([0], np.cumsum(nbas_lst)).astype(np.int32)
            lmlist_i = []
            jlist_i = []
        elif setup.Z > 0:
            nlist_j = [0, 2, 4, 1, 3, 2, 4, 3, 4]
            llist_j = [0, 0, 0, 1, 1, 2, 2, 3, 4]
            nbas_lst = [3, 2, 2, 1, 1]
            nbas_loc = np.append([0], np.cumsum(nbas_lst)).astype(np.int32)
            lmlist_i = []
            jlist_i = []
        else:
            nlist_j = [0, 2, 1, 2]
            llist_j = [0, 0, 1, 2]
            nbas_lst = [2, 1, 1]
            nbas_loc = np.append([0], np.cumsum(nbas_lst)).astype(np.int32)
            lmlist_i = []
            jlist_i = []

        nn = np.max(nlist_j) + 1

        pfuncs_ng = np.zeros((nn, ng), dtype=np.float64, order="C")
        pfuncs_ntp = np.zeros((nn, nt, 4), dtype=np.float64, order="C")
        for n in range(nn):
            pfunc_t = get_ffunc3(n, interp_r_g, rcut_func)
            pfuncs_ng[n, :] = get_ffunc3(n, rgd.r_g, rcut_func)
            pfuncs_ntp[n, :, :] = spline(
                np.arange(interp_r_g.size).astype(np.float64), pfunc_t
            )

        i = 0
        for j, n in enumerate(nlist_j):
            l = llist_j[j]
            for m in range(2 * l + 1):
                lm = l * l + m
                lmlist_i.append(lm)
                jlist_i.append(j)
                i += 1
        lmlist_i = np.array(lmlist_i, dtype=np.int32)
        jlist_i = np.array(jlist_i, dtype=np.int32)

        psetup = cls(
            pfuncs_ng,
            pfuncs_ntp,
            interp_rgd,
            setup.xc_correction.rgd,
            nlist_j,
            llist_j,
            lmlist_i,
            jlist_i,
            setup.nlxc_correction.big_rgd,
            [],
            nbas_loc,
            rcut_func,
            rcut_feat,
            Z=setup.Z,
        )
        psetup.initialize_g2a()

        return psetup


class PSmoothSetup(PASDWData):
    def _initialize_ffunc(self):
        rgd = self.slrgd
        nj = self.nj
        ng = self.ng
        nlist_j = self.nlist_j
        pfuncs_ng = self.pfuncs_ng

        dv_g = get_dv(rgd)
        ffuncs_jg = np.zeros((nj, ng))
        self.exact_ovlp_pf = np.zeros((self.ni, self.ni))
        self.ffuncs_jtp = np.zeros((nj, self.interp_rgd.r_g.size, 4))
        ilist = np.arange(self.ni)
        for j in range(self.nj):
            n = nlist_j[j]
            l = self.llist_j[j]
            ffuncs_jg[j] = pfuncs_ng[n]
            self.ffuncs_jtp[j] = self.pfuncs_ntp[n]
            i0s = ilist[self.jlist_i == j]
            for j1 in range(self.nj):
                if self.llist_j[j1] == l:
                    i1s = ilist[self.jlist_i == j1]
                    n1 = nlist_j[j1]
                    ovlp = np.dot(pfuncs_ng[n] * pfuncs_ng[n1], dv_g)
                    for i0, i1 in zip(i0s.tolist(), i1s.tolist()):
                        self.exact_ovlp_pf[i0, i1] = ovlp
        self.ffuncs_jg = ffuncs_jg

    def initialize_a2g(self):
        self.w_b = np.ones(self.alphas_ae.size) / self.alpha_norms**2
        if self.pmax is None:
            pmax = 4 * np.max(self.alphas_ae)
        else:
            pmax = 2 * self.pmax
        pmin = 2 / self.rcut_feat**2
        lambd = 1.8
        N = int(np.ceil(np.log(pmax / pmin) / np.log(lambd))) + 1
        dd = np.log(pmax / pmin) / (N - 1)
        alphas_bas = pmax * np.exp(-dd * np.arange(N))
        self.delta_lpg = get_delta_lpg(
            alphas_bas, self.rcut_feat, self.sbt_rgd, thr=pmin, lmax=self.lmax
        )
        self.delta_lpk = get_delta_lpk(self.delta_lpg, self.sbt_rgd)
        self.deltasl_lpg = get_delta_lpg(
            alphas_bas, self.rcut_feat, self.slrgd, thr=pmin, lmax=self.lmax
        )
        rgd = self.sbt_rgd
        pfuncs_jg = np.stack([self.pfuncs_ng2[n] for n in self.nlist_j])
        pfuncs_k = get_pfuncs_k(pfuncs_jg, self.llist_j, rgd, ns=self.nlist_j)
        phi_jabk = get_phi_iabk(pfuncs_k, rgd.k_g, self.alphas, betas=self.alphas_ae)
        phi_jabk[:] *= self.alpha_norms[:, None, None]
        phi_jabk[:] *= self.alpha_norms[:, None]
        REG11 = 1e-6
        REG22 = 1e-5
        FAC22 = 1e-2
        phi_jabg = get_phi_iabg(phi_jabk, self.llist_j, rgd)
        p11_lpp = get_p11_matrix(self.delta_lpg, rgd, reg=REG11)
        p12_pbja, p21_japb = get_p12_p21_matrix(
            self.delta_lpg, phi_jabg, rgd, self.llist_j, self.w_b
        )
        p22_ljaja = []
        for l in range(self.lmax + 1):
            p22_ljaja.append(
                get_p22_matrix(
                    phi_jabg,
                    rgd,
                    self.rcut_feat,
                    l,
                    self.w_b,
                    self.nbas_loc,
                    reg=REG22,
                )
            )
            p22_ljaja[-1] += FAC22 * get_p22_matrix(
                phi_jabg,
                rgd,
                self.rcut_feat,
                l,
                self.w_b,
                self.nbas_loc,
                cut_func=True,
            )
        p_l_ii = construct_full_p_matrices(
            p11_lpp,
            p12_pbja,
            p21_japb,
            p22_ljaja,
            self.w_b,
            self.nbas_loc,
        )
        self.phi_jabk = phi_jabk
        self.phi_jabg = phi_jabg
        self.p_l_ii = p_l_ii
        self.pinv_l_ii = []
        self.p_cl_l_ii = []
        for l in range(self.lmax + 1):
            c_and_l = cho_factor(p_l_ii[l])
            idn = np.identity(p_l_ii[l].shape[0])
            self.p_cl_l_ii.append(c_and_l)
            self.pinv_l_ii.append(cho_solve(c_and_l, idn))
        self.phi_sr_jabg = _interpc(self.phi_jabg, rgd, self.slrgd.r_g)

    def get_c_and_df(self, y_sbLg, realspace=True):
        nspin, nb, Lmax, ng = y_sbLg.shape
        rgd = self.sbt_rgd
        Nalpha_sm = self.alphas.size
        NP = self.delta_lpg.shape[1]
        c_sib = np.zeros((nspin, self.ni, Nalpha_sm))
        df_sLpb = np.zeros((nspin, Lmax, NP, nb))
        for s in range(nspin):
            for l in range(self.lmax + 1):
                Lmin = l * l
                dL = 2 * l + 1
                for L in range(Lmin, Lmin + dL):
                    if realspace:
                        b1 = self.get_b1_pb(y_sbLg[s, :, L, :], l, rgd)
                        b2 = self.get_b2_ja(y_sbLg[s, :, L, :], l, rgd)
                    else:
                        b1 = self.get_b1_pb_recip(y_sbLg[s, :, L, :], l, rgd)
                        b2 = self.get_b2_ja_recip(y_sbLg[s, :, L, :], l, rgd)
                    b = self.cat_b_vecs(b1, b2)
                    # x = self.pinv_l_ii[l].dot(b)
                    x = cho_solve(self.p_cl_l_ii[l], b)
                    N1 = b1.size
                    df_sLpb[s, L] += x[:N1].reshape(NP, nb)
                    ilist = self.lmlist_i == L
                    c_sib[s, ilist, :] += x[N1:].reshape(-1, Nalpha_sm)
        return c_sib, df_sLpb

    def get_v_from_c_and_df(self, vc_sib, vdf_sLpb, realspace=True):
        nspin, Lmax, NP, nb = vdf_sLpb.shape
        rgd = self.sbt_rgd
        Nalpha_sm = self.alphas.size
        ng = rgd.r_g.size
        vy_sbLg = np.zeros((nspin, nb, Lmax, ng))
        for s in range(nspin):
            for l in range(self.lmax + 1):
                Lmin = l * l
                dL = 2 * l + 1
                for L in range(Lmin, Lmin + dL):
                    ilist = self.lmlist_i == L
                    x1 = vdf_sLpb[s, L].flatten()
                    x2 = vc_sib[s, ilist, :].flatten()
                    x = self.cat_b_vecs(x1, x2)
                    # b = self.pinv_l_ii[l].dot(x)
                    b = cho_solve(self.p_cl_l_ii[l], x)
                    N1 = x1.size
                    if realspace:
                        vy_sbLg[s, :, L, :] += self.get_vb1_pb(
                            b[:N1].reshape(NP, nb), l
                        )
                        vy_sbLg[s, :, L, :] += self.get_vb2_ja(
                            b[N1:].reshape(-1, Nalpha_sm), l
                        )
                    else:
                        vy_sbLg[s, :, L, :] += self.get_vb1_pb_recip(
                            b[:N1].reshape(NP, nb), l
                        )
                        vy_sbLg[s, :, L, :] += self.get_vb2_ja_recip(
                            b[N1:].reshape(-1, Nalpha_sm), l
                        )
        return vy_sbLg

    def get_f_realspace_contribs(self, c_sib, sl=False):
        Nalpha = self.alphas_ae.size
        phi_jabg = self.phi_sr_jabg if sl else self.phi_jabg
        ng = phi_jabg.shape[-1]
        nspin, _, nb = c_sib.shape
        Lmax = 25  # np.max(self.lmlist_i) + 1
        yt_sbLg = np.zeros((nspin, Nalpha, Lmax, ng))
        for i in range(self.ni):
            L = self.lmlist_i[i]
            j = self.jlist_i[i]
            yt_sbLg[:, :, L, :] += np.einsum(
                "abg,sa->sbg",
                phi_jabg[j],
                c_sib[:, i, :],
            )
        return yt_sbLg

    def get_vf_realspace_contribs(self, vyt_sbLg, rgd, sl=False):
        if rgd is None:
            dv_g = np.ones(vyt_sbLg.shape[-1])
        else:
            dv_g = get_dv(rgd)
        phi_jabg = self.phi_sr_jabg if sl else self.phi_jabg
        nspin, nb = vyt_sbLg.shape[:2]
        vc_sib = np.zeros((nspin, self.ni, nb))
        for i in range(self.ni):
            L = self.lmlist_i[i]
            j = self.jlist_i[i]
            vc_sib[:, i, :] += np.einsum(
                "abg,sbg,g->sa",
                phi_jabg[j],
                vyt_sbLg[:, :, L, :],
                dv_g,
            )
        return vc_sib

    def get_df_realspace_contribs(self, df_sLpb, sl=False):
        df_sbLp = df_sLpb.transpose(0, 3, 1, 2)
        delta_lpg = self.deltasl_lpg if sl else self.delta_lpg
        ng = delta_lpg.shape[-1]
        nspin, Lmax, NP, nb = df_sLpb.shape
        y_sbLg = np.zeros((nspin, nb, Lmax, ng))
        for s in range(nspin):
            for l in range(self.lmax + 1):
                for L in range(l * l, (l + 1) * (l + 1)):
                    y_sbLg[s, :, L, :] += df_sbLp[s, :, L, :].dot(delta_lpg[l])
        return y_sbLg

    def get_df_only(self, y_sbLg, rgd, sl=False, realspace=True):
        nspin, nb, Lmax, ng = y_sbLg.shape
        NP = self.delta_lpg.shape[1]
        if realspace:
            delta_lpg = self.deltasl_lpg if sl else self.delta_lpg
            dv_g = get_dv(rgd)
        else:
            assert not sl
            delta_lpg = self.delta_lpk
            dv_g = get_dvk(rgd)
        df_sLpb = np.zeros((nspin, Lmax, NP, nb))
        for l in range(self.lmax + 1):
            L0 = l * l
            dL = 2 * l + 1
            for L in range(L0, L0 + dL):
                df_sLpb[:, L, :, :] += np.einsum(
                    "sbg,pg,g->spb", y_sbLg[:, :, L, :], delta_lpg[l], dv_g
                )
        return df_sLpb

    def get_vdf_only(self, df_sLpb, sl=False, realspace=True):
        nspin, Lmax, NP, nb = df_sLpb.shape
        if realspace:
            delta_lpg = self.deltasl_lpg if sl else self.delta_lpg
        else:
            assert not sl
            delta_lpg = self.delta_lpk
        ng = delta_lpg[0].shape[-1]
        y_sbLg = np.zeros((nspin, nb, Lmax, ng))
        for l in range(self.lmax + 1):
            L0 = l * l
            dL = 2 * l + 1
            for L in range(L0, L0 + dL):
                y_sbLg[:, :, L, :] += np.einsum(
                    "spb,pg->sbg", df_sLpb[:, L, :, :], delta_lpg[l]
                )
        return y_sbLg

    def get_vdf_realspace_contribs(self, vy_sbLg, rgd, sl=False):
        if rgd is None:
            dv_g = np.ones(vy_sbLg.shape[-1])
        else:
            dv_g = get_dv(rgd)
        delta_lpg = self.deltasl_lpg if sl else self.delta_lpg
        NP = delta_lpg.shape[1]
        nspin, nb, Lmax, ng = vy_sbLg.shape
        vdf_sbLp = np.zeros((nspin, nb, Lmax, NP))
        for s in range(nspin):
            for l in range(self.lmax + 1):
                for L in range(l * l, (l + 1) * (l + 1)):
                    vdf_sbLp[s, :, L, :] += np.einsum(
                        "bg,pg,g->bp",
                        vy_sbLg[s, :, L, :],
                        delta_lpg[l],
                        dv_g,
                    )
        return vdf_sbLp.transpose(0, 2, 3, 1)

    def get_b1_pb(self, FL_bg, l, rgd):
        dv_g = get_dv(rgd)
        return np.einsum("bg,pg,g->pb", FL_bg, self.delta_lpg[l], dv_g) * self.w_b

    def get_b2_ja(self, FL_bg, l, rgd):
        dv_g = get_dv(rgd)
        return np.einsum(
            "bg,jabg,b,g->ja",
            FL_bg,
            self.phi_jabg[self.nbas_loc[l] : self.nbas_loc[l + 1]],
            self.w_b,
            dv_g,
        )

    def get_vb1_pb(self, b_pb, l):
        return np.einsum("pb,pg->bg", b_pb, self.delta_lpg[l]) * self.w_b[:, None]

    def get_vb2_ja(self, b_ja, l):
        return (
            np.einsum(
                "ja,jabg->bg",
                b_ja,
                self.phi_jabg[self.nbas_loc[l] : self.nbas_loc[l + 1]],
            )
            * self.w_b[:, None]
        )

    def get_b1_pb_recip(self, FL_bg, l, rgd):
        dv_g = get_dvk(rgd)
        return np.einsum("bg,pg,g->pb", FL_bg, self.delta_lpk[l], dv_g) * self.w_b

    def get_b2_ja_recip(self, FL_bg, l, rgd):
        dv_g = get_dvk(rgd)
        return np.einsum(
            "bg,jabg,b,g->ja",
            FL_bg,
            self.phi_jabk[self.nbas_loc[l] : self.nbas_loc[l + 1]],
            self.w_b,
            dv_g,
        )

    def get_vb1_pb_recip(self, b_pb, l):
        return np.einsum("pb,pg->bg", b_pb, self.delta_lpk[l]) * self.w_b[:, None]

    def get_vb2_ja_recip(self, b_ja, l):
        return (
            np.einsum(
                "ja,jabg->bg",
                b_ja,
                self.phi_jabk[self.nbas_loc[l] : self.nbas_loc[l + 1]],
            )
            * self.w_b[:, None]
        )

    def cat_b_vecs(self, b1, b2):
        return np.append(b1.flatten(), b2.flatten())

    @classmethod
    def from_setup(cls, setup, alphas, alphas_ae, alpha_norms=None, pmax=None):

        rgd = setup.xc_correction.rgd
        rcut_feat = np.max(setup.rcut_j)
        rcut_func = rcut_feat * 1.00

        if setup.Z > 100:
            nlist_j = [0, 2, 4, 6, 1, 3, 5, 2, 4, 6, 3, 5, 4, 6]
            llist_j = [0, 0, 0, 0, 1, 1, 1, 2, 2, 2, 3, 3, 4, 4]
            nbas_lst = [4, 3, 3, 2, 2]
            nbas_loc = np.append([0], np.cumsum(nbas_lst)).astype(np.int32)
            lmlist_i = []
            jlist_i = []
        elif setup.Z > 0:
            nlist_j = [0, 2, 4, 1, 3, 2, 4, 3, 4]
            llist_j = [0, 0, 0, 1, 1, 2, 2, 3, 4]
            nbas_lst = [3, 2, 2, 1, 1]
            nbas_loc = np.append([0], np.cumsum(nbas_lst)).astype(np.int32)
            lmlist_i = []
            jlist_i = []
        else:
            nlist_j = [0, 2, 1, 2]
            llist_j = [0, 0, 1, 2]
            nbas_lst = [2, 1, 1]
            nbas_loc = np.append([0], np.cumsum(nbas_lst)).astype(np.int32)
            lmlist_i = []
            jlist_i = []

        nt = 100
        interp_rgd = EquidistantRadialGridDescriptor(h=rcut_func / (nt - 1), N=nt)
        interp_r_g = interp_rgd.r_g
        ng = rgd.r_g.size
        sbt_rgd = setup.nlxc_correction.big_rgd
        ng2 = sbt_rgd.r_g.size
        nn = np.max(nlist_j) + 1

        pfuncs_ng = np.zeros((nn, ng), dtype=np.float64, order="C")
        pfuncs_ng2 = np.zeros((nn, ng2), dtype=np.float64, order="C")
        pfuncs_ntp = np.zeros((nn, nt, 4), dtype=np.float64, order="C")
        pfuncs_nt = np.zeros((nn, nt), dtype=np.float64, order="C")
        for n in range(nn):
            pfuncs_nt[n, :] = get_pfunc2_norm(n, interp_r_g, rcut_func)
            pfuncs_ng[n, :] = get_pfunc2_norm(n, rgd.r_g, rcut_func)
            pfuncs_ng2[n, :] = get_pfunc2_norm(n, sbt_rgd.r_g, rcut_func)

        """
        # THIS CODE BLOCK orthogonalizes the basis functions,
        # but does not seem to be important currently.
        def _helper(tmp_ng):
            ovlp = np.einsum('ng,mg,g->nm', tmp_ng, tmp_ng, get_dv(rgd))
            L = np.linalg.cholesky(ovlp)
            tmp2_ng = np.linalg.solve(L, tmp_ng)
            ovlp2 = np.einsum('ng,mg,g->nm', tmp_ng, tmp2_ng, get_dv(rgd))
            ovlp3 = np.einsum('ng,mg,g->nm', tmp2_ng, tmp2_ng, get_dv(rgd))
            rval = np.linalg.solve(L, np.identity(tmp_ng.shape[0]))
            return rval
        C_nn = np.zeros((nn,nn))
        C_nn[-1::-2,-1::-2] = _helper(pfuncs_ng[-1::-2])
        C_nn[-2::-2,-2::-2] = _helper(pfuncs_ng[-2::-2])
        pfuncs_nt = C_nn.dot(pfuncs_nt)
        pfuncs_ng = C_nn.dot(pfuncs_ng)
        pfuncs_ng2 = C_nn.dot(pfuncs_ng2)
        """

        for n in range(nn):
            pfuncs_ntp[n, :, :] = spline(
                np.arange(interp_r_g.size).astype(np.float64), pfuncs_nt[n]
            )

        i = 0
        for j, n in enumerate(nlist_j):
            l = llist_j[j]
            for m in range(2 * l + 1):
                lm = l * l + m
                lmlist_i.append(lm)
                jlist_i.append(j)
                i += 1
        lmlist_i = np.array(lmlist_i, dtype=np.int32)
        jlist_i = np.array(jlist_i, dtype=np.int32)

        psetup = cls(
            pfuncs_ng,
            pfuncs_ntp,
            interp_rgd,
            rgd,
            nlist_j,
            llist_j,
            lmlist_i,
            jlist_i,
            setup.nlxc_correction.big_rgd,
            alphas,
            nbas_loc,
            rcut_func,
            rcut_feat,
            Z=setup.Z,
            alphas_ae=alphas_ae,
            alpha_norms=alpha_norms,
            pmax=pmax,
        )

        psetup.pfuncs_ng2 = pfuncs_ng2
        psetup._initialize_ffunc()
        psetup.initialize_a2g()

        return psetup


class PSmoothSetup2(PSmoothSetup):
    def initialize_a2g(self):
        self.w_b = np.ones(self.alphas_ae.size) / self.alpha_norms**2
        self.delta_lpg = get_delta_lpg_v2(betas_lv, self.rcut_feat, self.slrgd, pmin)
        nalpha = self.alphas.size
        rgd = self.sbt_rgd
        pfuncs_jg = np.stack([self.pfuncs_ng2[n] for n in self.nlist_j])
        pfuncs_k = get_pfuncs_k(pfuncs_jg, self.llist_j, rgd, ns=self.nlist_j)
        p21_l_javb = []
        p22_l_jaja = []
        for l in range(lp1):
            nj = self.nbas_loc[l + 1] - self.nbas_loc[l]
            nv = betas_lv[l].size
            p21_l_javb.append(np.zeros((nj, nalpha, nv, nalpha)))
            p22_l_jaja.append(np.zeros((nj, nalpha, nj, nalpha)))
        for j, pfunc_g in enumerate(pfuncs_jg):
            l = self.llist_j[j]
            # make sure conv accounts for alpha norms
            phi_abx = self.conv_fwd(pfunc_g, l)
            phi_abx[:] *= self.w_b[:, None]
            phi_abg = self.x2g(phi_abx) * dv_g
            phisr_abg = phi_abg * sr_part
            phisr_abx = self.g2x(phisr_abg)
            p21_l_javb[l][j] = np.einsum("abg,pg->apb", phi_abg, self.delta_l_pg[l])
            for a in range(nalpha):
                tmp_ag = self.conv_bwd(phi_abx[a], l)
                tmp_ja = ...
                p22_l_jaja[l][j - self.nbas_loc[j], a] = tmp_ja
                tmp_ag = self.conv_bwd(phisr_abx[a], l)
                tmp_ja = ...
                p22_l_jaja[j][j - self.nbas_loc[j], a] += FAC22 * tmp_ja
        phi_jabk = get_phi_iabk(pfuncs_k, rgd.k_g, self.alphas, betas=self.alphas_ae)
        phi_jabk[:] *= self.alpha_norms[:, None, None]
        phi_jabk[:] *= self.alpha_norms[:, None]
        REG11 = 1e-6
        REG22 = 1e-5
        FAC22 = 1e-2
        phi_jabg = get_phi_iabg(phi_jabk, self.llist_j, rgd)
        p11_lpp = get_p11_matrix(self.delta_lpg, rgd, reg=REG11)
        p12_pbja, p21_japb = get_p12_p21_matrix(
            self.delta_lpg, phi_jabg, rgd, self.llist_j, self.w_b
        )
        p22_ljaja = []
        for l in range(self.lmax + 1):
            p22_ljaja.append(
                get_p22_matrix(
                    phi_jabg,
                    rgd,
                    self.rcut_feat,
                    l,
                    self.w_b,
                    self.nbas_loc,
                    reg=REG22,
                )
            )
            p22_ljaja[-1] += FAC22 * get_p22_matrix(
                phi_jabg,
                rgd,
                self.rcut_feat,
                l,
                self.w_b,
                self.nbas_loc,
                cut_func=True,
            )
        p_l_ii = construct_full_p_matrices(
            p11_lpp,
            p12_pbja,
            p21_japb,
            p22_ljaja,
            self.w_b,
            self.nbas_loc,
        )
        self.phi_jabk = phi_jabk
        self.phi_jabg = phi_jabg
        self.p_l_ii = p_l_ii
        self.pinv_l_ii = []
        self.p_cl_l_ii = []
        for l in range(self.lmax + 1):
            c_and_l = cho_factor(p_l_ii[l])
            idn = np.identity(p_l_ii[l].shape[0])
            self.p_cl_l_ii.append(c_and_l)
            self.pinv_l_ii.append(cho_solve(c_and_l, idn))
        self.phi_sr_jabg = _interpc(self.phi_jabg, rgd, self.slrgd.r_g)

    def get_c_and_df(self, y_sgLb, realspace=True):
        nspin, ng, Lmax, nb = y_sgLb.shape
        rgd = self.sbt_rgd
        Nalpha_sm = self.alphas.size
        NP = self.delta_lpg.shape[1]
        c_sib = np.zeros((nspin, self.ni, Nalpha_sm))
        nao = ...
        df_sub = np.zeros((nspin, nao, nb))
        lloc_bas = ...
        lloc_ao = ...
        for s in range(nspin):
            for l in range(self.lmax + 1):
                Lmin = l * l
                dL = 2 * l + 1
                for L in range(Lmin, Lmin + dL):
                    b1 = self.get_b1_pb(y_sgLb[s, :, L, :], l, rgd)
                    b2 = self.get_b2_ja(y_sgLb[s, :, L, :], l, rgd)
                    b = self.cat_b_vecs(b1, b2)
                    x = cho_solve(self.p_cl_l_ii[l], b)
                    N1 = b1.size
                    df_sLpb[s, L] += x[:N1].reshape(NP, nb)
                    ilist = self.lmlist_i == L
                    c_sib[s, ilist, :] += x[N1:].reshape(-1, Nalpha_sm)
        return c_sib, df_sLpb

    def get_v_from_c_and_df(self, vc_sib, vdf_sLpb, realspace=True):
        nspin, Lmax, NP, nb = vdf_sLpb.shape
        rgd = self.sbt_rgd
        Nalpha_sm = self.alphas.size
        ng = rgd.r_g.size
        vy_sbLg = np.zeros((nspin, nb, Lmax, ng))
        for s in range(nspin):
            for l in range(self.lmax + 1):
                Lmin = l * l
                dL = 2 * l + 1
                for L in range(Lmin, Lmin + dL):
                    ilist = self.lmlist_i == L
                    x1 = vdf_sLpb[s, L].flatten()
                    x2 = vc_sib[s, ilist, :].flatten()
                    x = self.cat_b_vecs(x1, x2)
                    # b = self.pinv_l_ii[l].dot(x)
                    b = cho_solve(self.p_cl_l_ii[l], x)
                    N1 = x1.size
                    if realspace:
                        vy_sbLg[s, :, L, :] += self.get_vb1_pb(
                            b[:N1].reshape(NP, nb), l
                        )
                        vy_sbLg[s, :, L, :] += self.get_vb2_ja(
                            b[N1:].reshape(-1, Nalpha_sm), l
                        )
                    else:
                        vy_sbLg[s, :, L, :] += self.get_vb1_pb_recip(
                            b[:N1].reshape(NP, nb), l
                        )
                        vy_sbLg[s, :, L, :] += self.get_vb2_ja_recip(
                            b[N1:].reshape(-1, Nalpha_sm), l
                        )
        return vy_sbLg

    def get_b1_pb_recip(self, FL_bg, l, rgd):
        dv_g = get_dvk(rgd)
        return np.einsum("bg,pg,g->pb", FL_bg, self.delta_lpk[l], dv_g) * self.w_b

    def get_b2_ja_recip(self, FL_bg, l, rgd):
        dv_g = get_dvk(rgd)
        return np.einsum(
            "bg,jabg,b,g->ja",
            FL_bg,
            self.phi_jabk[self.nbas_loc[l] : self.nbas_loc[l + 1]],
            self.w_b,
            dv_g,
        )
