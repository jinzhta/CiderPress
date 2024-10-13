#!/usr/bin/env python
# CiderPress: Machine-learning based density functional theory calculations
# Copyright (C) 2024 The President and Fellows of Harvard College
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>
#
# Author: Kyle Bystrom <kylebystrom@gmail.com>
#

import yaml
from gpaw.calculator import GPAW
from gpaw.xc.libxc import LibXC

from ciderpress.dft.model_utils import load_cider_model
from ciderpress.gpaw import fast_paw
from ciderpress.gpaw.cider_fft import (
    CiderGGA,
    CiderGGAHybridKernel,
    CiderMGGA,
    CiderMGGAHybridKernel,
)
from ciderpress.gpaw.cider_paw import (
    CiderGGAPASDW,
    CiderMGGAPASDW,
    DiffGGA,
    DiffMGGA,
    SLCiderGGA,
    SLCiderGGAHybridWrapper,
    SLCiderMGGA,
    SLCiderMGGAHybridWrapper,
)


def get_cider_functional(
    mlfunc,
    xmix=1.00,
    xkernel="GGA_X_PBE",
    ckernel="GGA_C_PBE",
    mlfunc_format=None,
    use_paw=True,
    pasdw_ovlp_fit=True,
    pasdw_store_funcs=False,
    Nalpha=None,
    qmax=300,
    lambd=1.8,
    no_paw_atom_kernel=False,
    fast=False,
    _force_nonlocal=False,
):
    """
    Initialize a CIDER surrogate hybrid XC functional of the form
    E_xc = E_x^sl * (1 - xmix) + E_c^sl + xmix * E_x^CIDER
    where E_x^sl is given by the xkernel parameter, E_c^sl is given by the ckernel
    parameter, and E_x^CIDER is the ML exchange energy contains in mlfunc.

    NOTE: Do not use CIDER with ultrasoft pseudopotentials (PAW only).
    At your own risk, you can use CIDER with norm-conserving pseudopotentials,
    but correctness is not guaranteed because the nonlocal features will
    not be correct due to the lack of core electrons.

    NOTE: If the mlfunc is determined to be semilocal, all the
    internal settings are ignored, and a simpler, more efficient
    class is returned to evaluate the semilocal functional.

    Args:
        mlfunc (MappedXC, MappedXC2, str): An ML functional object or a str
            corresponding to the file name of a yaml or joblib file
            containing it.
        xmix (float, 1.00): Mixing parameter for CIDER exchnange.
        xkernel (str, GGA_X_PBE): libxc code str for semi-local X functional
        ckernel (str, GGA_C_PBE): libxc code str for semi-local C functional
        mlfunc_format (str, None): 'joblib' or 'yaml', specifies the format
            of mlfunc if it is a string corresponding to a file name.
            If unspecified, infer from file extension and raise error
            if file type cannot be determined.
        use_paw (bool, True): Whether to compute PAW corrections. This
            should be True unless you plan to use norm-conserving
            pseudopotentials (NCPPs). Note, the use of NCPPs is allowed
            but not advised, as large errors might arise due to the use
            of incorrect nonlocal features.

    The following parameters are only used if use_paw is True. They
    control internal behavior of the PASDW algorithm used to
    evaluate the nonlocal density features in CIDER, but users might
    want to set them to address numerical stability or memory/performance
    tradeoff issues.

    PASDW args:
        pasdw_ovlp_fit (bool, True): Whether to use overlap fitting to
            attempt to improve numerical precision of PASDW projections.
            Default to True. Impact of this parameter should be minor.
        pasdw_store_funcs (bool, False): Whether to store projector
            functions used in the PASDW routine. Defaults to False
            because this can be memory intensive, but if you have the
            space, the computational cost of the atomic corrections
            can be greatly reduced by setting to True.

    The following additional arguments can be set but are considered
    internal parameters and should usually be kept as their defaults,
    which should be reasonable for most cases.

    Internal args:
        Nalpha (int, None):
            Number of interpolation points for the nonlocal feature kernel.
            If None, set automatically based on lambd, qmax, and qmin.
        qmax (float, 300):
            Maximum value of q to use for kernel interpolation on FFT grid.
            Default should be fine for most cases.
        * qmin (not currently a parameter, set automatically):
            Mininum value of q to use for kernel interpolation.
            Currently, qmin is set automatically based
            on the minimum regularized value of the kernel exponent.
        lambd (float, 1.8):
            Density of interpolation points. q_alpha=q_0 * lambd**alpha.
            Smaller lambd is more expensive and more precise.

    The following options are for debugging only. Do not set them unless
    you want to test numerical stability issues, as the energies will
    not be variational. Only supported for GGA-type functionals.

    Debug args:
        _force_nonlocal (bool, False): Use nonlocal kernel even if nonlocal
            features are not required. For debugging use.

    Returns:
        A _CiderBase object (specific class depends on the parameters
        provided above, specifically on whether use_paw is True,
        whether the functional is GGA or MGGA form, and whether the
        functional is semi-local or nonlocal.)
    """

    mlfunc = load_cider_model(mlfunc, mlfunc_format)
    if mlfunc.settings.sl_settings.level == "MGGA":
        mlfunc.desc_version = "b"
    else:
        mlfunc.desc_version = "d"

    if mlfunc.desc_version == "b":
        no_nldf = mlfunc.settings.nldf_settings.is_empty
        if no_nldf and not _force_nonlocal:
            # functional is semi-local MGGA
            cider_kernel = SLCiderMGGAHybridWrapper(mlfunc, xmix, xkernel, ckernel)
            return SLCiderMGGA(cider_kernel)
        cider_kernel = CiderMGGAHybridKernel(mlfunc, xmix, xkernel, ckernel)
        if use_paw and fast:
            cls = fast_paw.CiderMGGAPASDW
        elif use_paw:
            cls = CiderMGGAPASDW
        elif fast:
            cls = fast_paw.CiderMGGA
        else:
            cls = CiderMGGA
    elif mlfunc.desc_version == "d":
        no_nldf = mlfunc.settings.nldf_settings.is_empty
        if no_nldf and not _force_nonlocal:
            # functional is semi-local GGA
            cider_kernel = SLCiderGGAHybridWrapper(mlfunc, xmix, xkernel, ckernel)
            return SLCiderGGA(cider_kernel)
        cider_kernel = CiderGGAHybridKernel(mlfunc, xmix, xkernel, ckernel)
        if use_paw and fast:
            cls = fast_paw.CiderGGAPASDW
        elif use_paw:
            cls = CiderGGAPASDW
        elif fast:
            cls = fast_paw.CiderGGA
        else:
            cls = CiderGGA
    else:
        msg = "Only implemented for b and d version, found {}"
        raise ValueError(msg.format(mlfunc.desc_version))

    if use_paw:
        xc = cls(
            cider_kernel,
            Nalpha=Nalpha,
            lambd=lambd,
            encut=qmax,
            pasdw_ovlp_fit=pasdw_ovlp_fit,
            pasdw_store_funcs=pasdw_store_funcs,
        )
    else:
        xc = cls(cider_kernel, Nalpha=Nalpha, lambd=lambd, encut=qmax)

    return xc


class CiderGPAW(GPAW):
    """
    This class is equivalent to the GPAW calculator object
    provded in the gpaw pacakage, except that it is able to load
    and save CIDER calculations. The GPAW object can run CIDER but
    not save/load CIDER calculations.

    One can also provide CIDER XC functional in dictionary format,
    but this is not advised since one also has to explicitly provide
    by the mlfunc_data parameter, which is the str of the yaml file
    representing the mlfunc object.
    """

    def __init__(
        self,
        restart=None,
        *,
        label=None,
        timer=None,
        communicator=None,
        txt="?",
        parallel=None,
        **kwargs
    ):
        if isinstance(kwargs.get("xc"), dict) and "_cider_type" in kwargs["xc"]:
            if "mlfunc_data" not in kwargs:
                raise ValueError("Must provide mlfunc_data for CIDER.")
            self._mlfunc_data = kwargs.pop("mlfunc_data")
        super(CiderGPAW, self).__init__(
            restart=restart,
            label=label,
            timer=timer,
            communicator=communicator,
            txt=txt,
            parallel=parallel,
            **kwargs,
        )

    def _write(self, writer, mode):
        writer = super(CiderGPAW, self)._write(writer, mode)
        if hasattr(self.hamiltonian.xc, "get_mlfunc_data"):
            mlfunc_data = self.hamiltonian.xc.get_mlfunc_data()
            writer.child("mlfunc").write(mlfunc_data=mlfunc_data)
        return writer

    def initialize(self, atoms=None, reading=False):
        xc = self.parameters.xc
        if isinstance(xc, dict) and "_cider_type" in xc:
            is_cider = "Cider" in xc["_cider_type"]
            if is_cider:
                if reading:
                    self._mlfunc_data = self.reader.mlfunc.get("mlfunc_data")
                xc["mlfunc"] = self._mlfunc_data
            self.parameters.xc = cider_functional_from_dict(xc)
            if is_cider:
                del self._mlfunc_data
        GPAW.initialize(self, atoms, reading)


def cider_functional_from_dict(d):
    """
    This is a function for reading CIDER functionals (and also
    standard functionals using the DiffPAW approach) from a dictionary
    that can be stored in a .gpw file. This is primarily a helper function
    for internal use, as it just reads the functional type and calls
    the from_dict() function of the respective class.

    Args:
        d: XC data

    Returns:
        XC Functional object for use in GPAW.
    """
    cider_type = d.pop("_cider_type")
    is_fast = d.pop("_cider_fast", False)
    if cider_type == "CiderGGAPASDW":
        cls = fast_paw.CiderGGAPASDW if is_fast else CiderGGAPASDW
        kcls = CiderGGAHybridKernel
    elif cider_type == "CiderMGGAPASDW":
        cls = fast_paw.CiderMGGAPASDW if is_fast else CiderMGGAPASDW
        kcls = CiderMGGAHybridKernel
    elif cider_type == "CiderGGA":
        cls = fast_paw.CiderGGA if is_fast else CiderGGA
        kcls = CiderGGAHybridKernel
    elif cider_type == "CiderMGGA":
        cls = fast_paw.CiderMGGA if is_fast else CiderMGGA
        kcls = CiderMGGAHybridKernel
    elif cider_type == "SLCiderGGA":
        cls = SLCiderGGA
        kcls = SLCiderGGAHybridWrapper
    elif cider_type == "SLCiderMGGA":
        cls = SLCiderMGGA
        kcls = SLCiderMGGAHybridWrapper
    elif cider_type == "DiffGGA":
        cls = DiffGGA
        kcls = None
    elif cider_type == "DiffMGGA":
        cls = DiffMGGA
        kcls = None
    else:
        raise ValueError("Unsupported functional type")

    if "Cider" in cider_type:
        mlfunc = yaml.load(d["mlfunc"], Loader=yaml.CLoader)
        # kernel_params should be xmix, xkernel, ckernel
        cider_kernel = kcls(mlfunc, **(d["kernel_params"]))
        if "SLCider" in cider_type:
            xc = cls(cider_kernel)
        else:
            # xc_params should have Nalpha, lambd, encut.
            # For PAW, it should also have pasdw_ovlp_fit, pasdw_store_funcs.
            xc = cls(cider_kernel, **(d["xc_params"]))
    else:
        xc = cls(LibXC(d["name"]))

    return xc
