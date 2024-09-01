"""Compute Fourier shell correlation between two volumes, applying an optional mask.

Instead of giving two volumes, you can give three, in which case the first volume is
assumed to be the full volume whereas the other two are corresponding half-maps
The full volume will be used to create a loose and a tight mask using dilation and
cosine edges; the latter mask will be corrected using phase randomization as implemented
in cryoSPARC.

See also
--------
`cryodrgn.commands_utils.plot_fsc` — for just plotting already-calculated FSCs
`cryodrgn.commands_utils.gen_mask` — for generating custom dilation + cosine edge masks

Example usage
-------------
$ cryodrgn_utils fsc volume1.mrc volume2.mrc

# Save FSC values to file and produce an FSC plot
$ cryodrgn_utils fsc vol1.mrc vol2.mrc -o fsc.txt -p

# Also apply a mask before computing FSCs, and also produce a plot of FSC curves.
$ cryodrgn_utils fsc vol1.mrc vol2.mrc --mask test-mask.mrc -o fsc.txt -p

# Also apply phase randomization at Fourier shells for resolutions < 10 angstroms
$ cryodrgn_utils fsc vol1.mrc vol2.mrc --mask test-mask.mrc -o fsc.txt -p fsc-plot.png
                                       --corrected 10

# Do cryoSPARC-style phase randomization with a tight mask; create an FSC plot with
# curves for no mask, spherical mask, loose mask, tight mask, and corrected tight mask,
# with loose and tight masks generated using the (first-given) full volume.
$ cryodrgn_utils fsc fullvol.mrc vol1.mrc vol2.mrc -p fsc-plot.png

# Do phase randomization as above using the given mask instead of the default tight
# mask, which will also be used to calculate corrected FSCs.
$ cryodrgn_utils fsc backproject.mrc half_vol_a.mrc half_vol_b.mrc \
                     -p tighter-mask.png mask=tighter-mask.mrc

"""
import os
import argparse
import logging
import numpy as np
import pandas as pd
import torch
import random
from typing import Optional, Union
from cryodrgn import fft
from cryodrgn.source import ImageSource
from cryodrgn.mrcfile import parse_mrc
from cryodrgn.commands_utils.plot_fsc import create_fsc_plot
from cryodrgn.masking import spherical_window_mask, cosine_dilation_mask

logger = logging.getLogger(__name__)


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("volumes", nargs="+", help="volumes to compare")

    parser.add_argument(
        "--mask",
        type=os.path.abspath,
        help="if given, apply the mask in this file before calculating half-map FSCs, "
        "or use it for the correction step if calculating phase-randomized FSCs",
    )
    parser.add_argument(
        "--corrected",
        type=float,
        help="use cryoSPARC-style high resolution phase randomization beyond this "
        "resolution to correct for possible effects of tight masking",
    )
    parser.add_argument(
        "--plot",
        "-p",
        type=os.path.abspath,
        nargs="?",
        const=True,
        default=None,
        help="also plot the FSC curve: optionally supply a .png file name instead of "
        "generating one automatically",
    )
    parser.add_argument(
        "--Apix",
        type=float,
        help="Ang/pixels to use when printing the resolutions at thresholds to "
        "override those found in the volumes, or replace them if missing in volumes",
    )
    parser.add_argument(
        "--outtxt",
        "-o",
        type=os.path.abspath,
        help=(
            "if given, a file to save the FSC values, "
            "with each space-delimited row as <resolution> <fsc_val>; "
            "otherwise print these to screen"
        ),
    )


def get_fftn_dists(resolution: int) -> np.array:
    x = np.arange(-resolution // 2, resolution // 2)
    x2, x1, x0 = np.meshgrid(x, x, x, indexing="ij")
    coords = np.stack((x0, x1, x2), -1)
    dists = (coords**2).sum(-1) ** 0.5
    assert dists[resolution // 2, resolution // 2, resolution // 2] == 0.0

    return dists


def calculate_fsc(
    vol1: torch.Tensor,
    vol2: torch.Tensor,
    initial_mask: Optional[torch.Tensor] = None,
    out_file: Optional[str] = None,
) -> pd.DataFrame:
    if initial_mask is not None:
        vol1 *= initial_mask
        vol2 *= initial_mask

    D = vol1.shape[0]
    dists = get_fftn_dists(D)
    vol1 = fft.fftn_center(vol1)
    vol2 = fft.fftn_center(vol2)

    # logger.info(r[D//2, D//2, D//2:])
    prev_mask = np.zeros((D, D, D), dtype=bool)
    fsc = [1.0]
    for i in range(1, D // 2):
        mask = dists < i
        shell = np.where(mask & np.logical_not(prev_mask))
        v1 = vol1[shell]
        v2 = vol2[shell]
        p = np.vdot(v1, v2) / (np.vdot(v1, v1) * np.vdot(v2, v2)) ** 0.5
        fsc.append(float(p.real))
        prev_mask = mask

    fsc_vals = pd.DataFrame(dict(pixres=np.arange(D // 2) / D, fsc=fsc), dtype=float)
    if out_file is not None:
        logger.info(f"Saving FSC values to {out_file}")
        fsc_vals.to_csv(out_file, sep=" ", header=True, index=False)

    return fsc_vals


def get_fsc_thresholds(
    fsc_vals: pd.DataFrame, apix: float, verbose: bool = True
) -> tuple[float, float]:
    if ((fsc_vals.pixres > 0) & (fsc_vals.fsc >= 0.5)).any():
        res_05 = fsc_vals.pixres[fsc_vals.fsc >= 0.5].max()
        if verbose:
            logger.info("res @ FSC=0.5: {:.4g} ang".format((1 / res_05) * apix))
    else:
        res_05 = None
        if verbose:
            logger.warning("res @ FSC=0.5: N/A")

    if ((fsc_vals.pixres > 0) & (fsc_vals.fsc >= 0.143)).any():
        res_143 = fsc_vals.pixres[fsc_vals.fsc >= 0.143].max()
        if verbose:
            logger.info("res @ FSC=0.143: {:.4g} ang".format((1 / res_143) * apix))
    else:
        res_143 = None
        if verbose:
            logger.warning("res @ FSC=0.143: N/A")

    return res_05, res_143


def correct_fsc(
    fsc_vals: pd.DataFrame,
    vol1: torch.Tensor,
    vol2: torch.Tensor,
    randomization_threshold: float,
    initial_mask: Optional[torch.Tensor] = None,
) -> pd.DataFrame:
    """Apply phase-randomization null correction to given FSC volumes past a resolution.

    This function implements cryoSPARC-style correction to an FSC curve to account for
    the boost in FSCs that can be attributed to a mask that too tightly fits the
    volumes and thus introduces an artificial source of correlation.

    """
    if initial_mask is not None:
        vol1 *= initial_mask
        vol2 *= initial_mask

    D = vol1.shape[0]
    if fsc_vals.shape[0] != (D // 2):
        raise ValueError(
            f"Given FSC values must have (D // 2) + 1 = {(D // 2) + 1} entries, "
            f"instead have {fsc_vals.shape[0]}!"
        )
    dists = get_fftn_dists(D)
    vol1 = fft.fftn_center(vol1)
    vol2 = fft.fftn_center(vol2)

    # apply phase-randomization past the given resolution by shuffling the phases
    phase_D = int(randomization_threshold * vol1.shape[0])
    phase_mask = dists > phase_D
    phase_inds = list(range(phase_mask.sum()))
    random.shuffle(phase_inds)
    vol1[phase_mask] = vol1[phase_mask][phase_inds]
    random.shuffle(phase_inds)
    vol2[phase_mask] = vol2[phase_mask][phase_inds]

    # re-calculate the FSCs past the resolution using the phase-randomized volumes
    prev_mask = np.zeros((D, D, D), dtype=bool)
    fsc = fsc_vals.fsc.tolist()
    for i in range(1, D // 2):
        mask = dists < i
        shell = np.where(mask & np.logical_not(prev_mask))

        if i > phase_D:
            v1 = vol1[shell]
            v2 = vol2[shell]
            p_raw = np.vdot(v1, v2) / (np.vdot(v1, v1) * np.vdot(v2, v2)) ** 0.5
            p = float(p_raw.real)

            # normalize the original FSC value using the phase-randomized value
            if p == 1.0:
                fsc[i] = 0.0
            elif not np.isnan(p):
                fsc[i] = np.clip((fsc[i] - p) / (1 - p), 0, 1.0)

        prev_mask = mask

    return pd.DataFrame(dict(pixres=np.arange(D // 2) / D, fsc=fsc), dtype=float)


def calculate_cryosparc_fscs(
    full_vol: np.ndarray,
    half_vol1: np.ndarray,
    half_vol2: np.ndarray,
    sphere_mask: Optional[np.ndarray] = None,
    loose_mask: tuple[int, int] = (25, 15),
    tight_mask: Union[tuple[int, int], np.ndarray] = (6, 6),
    Apix: float = 1.0,
    out_file: Optional[str] = None,
    plot_file: Optional[str] = None,
) -> pd.DataFrame:
    """Calculating cryoSPARC-style FSC curves with phase randomization correction."""
    if sphere_mask is None:
        sphere_mask = spherical_window_mask(D=full_vol.shape[0])

    masks = {
        "No Mask": None,
        "Spherical": sphere_mask,
        "Loose": cosine_dilation_mask(
            full_vol, dilation=loose_mask[0], edge_dist=loose_mask[1], apix=Apix
        ),
    }
    if isinstance(tight_mask, tuple):
        masks["Tight"] = cosine_dilation_mask(
            full_vol, dilation=tight_mask[0], edge_dist=tight_mask[1], apix=Apix
        )
    elif isinstance(tight_mask, (np.ndarray, torch.Tensor)):
        masks["Tight"] = tight_mask
    else:
        raise TypeError(
            f"`tight_mask` must be an array or a tuple giving dilation and cosine edge "
            f"size in pixels, instead given {type(tight_mask).__name__}!"
        )

    fsc_vals = {
        mask_lbl: calculate_fsc(half_vol1, half_vol2, initial_mask=mask)
        for mask_lbl, mask in masks.items()
    }
    fsc_thresh = {
        mask_lbl: get_fsc_thresholds(fsc_df, Apix, verbose=False)[1]
        for mask_lbl, fsc_df in fsc_vals.items()
    }

    if fsc_thresh["Tight"] is not None:
        fsc_vals["Corrected"] = correct_fsc(
            fsc_vals["Tight"],
            half_vol1,
            half_vol2,
            randomization_threshold=0.75 * fsc_thresh["Tight"],
            initial_mask=masks["Tight"],
        )
        fsc_thresh["Corrected"] = get_fsc_thresholds(
            fsc_vals["Corrected"], Apix, verbose=False
        )[1]
    else:
        fsc_vals["Corrected"] = fsc_vals["Tight"]
        fsc_thresh["Corrected"] = fsc_thresh["Tight"]

    # Report corrected FSCs by printing FSC=0.5 and FSC=0.143 threshold values to screen
    get_fsc_thresholds(fsc_vals["Corrected"], Apix)

    if plot_file is not None:
        fsc_angs = {
            mask_lbl: ((1 / fsc_val) * Apix) for mask_lbl, fsc_val in fsc_thresh.items()
        }
        fsc_plot_vals = {
            f"{mask_lbl}  ({fsc_angs[mask_lbl]:.2f}Å)": fsc_df
            for mask_lbl, fsc_df in fsc_vals.items()
        }
        create_fsc_plot(fsc_vals=fsc_plot_vals, outfile=plot_file, Apix=Apix)

    pixres_index = {tuple(vals.pixres.values) for vals in fsc_vals.values()}
    assert len(pixres_index) == 1
    pixres_index = tuple(pixres_index)[0]

    fsc_vals = pd.DataFrame(
        {k: vals.fsc.values for k, vals in fsc_vals.items()}, index=list(pixres_index)
    )
    if out_file is not None:
        logger.info(f"Saving FSC values to {out_file}")
        fsc_vals.to_csv(out_file, sep=" ", header=True)

    return fsc_vals


def main(args: argparse.Namespace) -> None:
    """Calculate FSC curves based on command-line arguments (see `add_args()` above)."""

    if len(args.volumes) not in {2, 3}:
        raise ValueError(
            f"Must provide two or three volume files, "
            f"given {len(args.volumes)} instead!"
        )
    volumes = [ImageSource.from_file(vol_file) for vol_file in args.volumes]
    mask = parse_mrc(args.mask)[0] if args.mask is not None else None

    if args.Apix:
        apix = args.Apix
    else:
        vol_apixs = {vol.apix for vol in volumes if vol.apix is not None}

        if len(vol_apixs) == 0:
            apix = 1.0
        elif len(vol_apixs) == 1:
            apix = tuple(vol_apixs)[0]
        else:
            raise ValueError(f"These volumes have different A/px values: {vol_apixs}")

    if args.plot:
        if isinstance(args.plot, bool):
            if args.outtxt:
                plot_file = "".join([os.path.splitext(args.outtxt)[0], ".png"])
            else:
                plot_file = "fsc-plot.png"
        else:
            plot_file = str(args.plot)
    else:
        plot_file = None

    if len(args.volumes) == 2:
        logger.info(
            f"Calculating FSC curve between `{args.volumes[0]}` "
            f"and `{args.volumes[1]}`..."
        )
        if args.corrected is not None:
            if args.corrected >= 1:
                args.corrected = (args.corrected / apix) ** -1

        fsc_vals = calculate_fsc(
            volumes[0].images(),
            volumes[1].images(),
            mask,
            args.corrected,
            out_file=args.outtxt,
        )
        _ = get_fsc_thresholds(fsc_vals, apix)

        if not args.outtxt:
            fsc_str = fsc_vals.round(4).to_csv(sep="\t", index=False)
            logger.info(f"\n{fsc_str}")
        if args.plot:
            create_fsc_plot(fsc_vals=fsc_vals, outfile=plot_file, Apix=apix)

    elif len(args.volumes) == 3:
        logger.info(
            f"Calculating FSC curve between `{args.volumes[1]}` and `{args.volumes[2]}`"
            f" using `{args.volumes[0]}` as the reference full volume..."
        )
        if args.corrected is not None:
            raise ValueError(
                "Cannot provide your own phase randomization threshold as this will "
                "be computed for you when providing three volumes "
                "{{fullvol, halfvol1, halfvol2}}!"
            )

        if mask is None:
            mask = (6, 6)
        fsc_vals = calculate_cryosparc_fscs(
            volumes[0].images(),
            volumes[1].images(),
            volumes[2].images(),
            tight_mask=mask,
            Apix=apix,
            out_file=args.outtxt,
            plot_file=plot_file,
        )
        if not args.outtxt:
            fsc_str = fsc_vals.round(4).to_csv(sep="\t", index=False)
            logger.info(f"\n{fsc_str}")
