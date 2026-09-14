#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fc_plot_phonon.py -- phonon band plot for the fc-fit skill (S2_plot).

Login-node step (run: gen, no job submitted).  Reads the force constants fitted
by S1_fit and draws the dispersion:

  <skill>/step1_fit/fc2.hdf5              fitted second-order force constants
  <skill>/step1_fit/POSCAR                unit cell
  <skill>/step1_fit/fc_dataset.json       supercell / primitive matrices
  <skill>/step1_fit/BORN                  optional; enables the NAC correction

Outputs (in <skill>/phonon_band_plot/):
  phonon_band_full.png        full frequency range
  phonon_band_lowfreq.png     0..10 THz zoom, where soft modes show up
  band-dft-cpu.yaml           the band structure phonopy computed
  phonon_band_summary.json    done marker + min/max frequencies

The imaginary-frequency gate in S1_fit already records the q-mesh minimum; this
step exists so a human can see where a soft mode lives.
"""
import json
import os
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

SRC = "step1_fit"
OUTDIR = "phonon_band_plot"
LOWF_MAX = 10.0
FULL_PAD = 3.0


def _ensure_phonopy():
    """Re-run this script with an interpreter that actually has phonopy.

    The step runs wherever tf's remote PATH points, and that is not always a
    python that can plot: on the 3090 the gen python is the amset env
    (numpy/pymatgen/ase, *no* phonopy) while the mace-gpu env next to it has
    phonopy.  Without this the plot fails for an environment reason that has
    nothing to do with the fit.  FC_PLOT_PYTHON overrides the search and the
    FC_PLOT_REEXEC marker stops the recursion (a second failure surfaces as the
    plain ImportError below).
    """
    try:
        import phonopy  # noqa: F401
        return
    except Exception:
        pass
    if os.environ.get("FC_PLOT_REEXEC"):
        return
    import glob
    import subprocess
    home = os.path.expanduser("~")
    cands = [os.environ.get("FC_PLOT_PYTHON") or ""]
    for pat in ("miniconda3/envs/*/bin/python", "anaconda3/envs/*/bin/python",
                "miniforge3/envs/*/bin/python", "mambaforge/envs/*/bin/python",
                "/opt/miniconda3/envs/*/bin/python"):
        full = pat if pat.startswith("/") else os.path.join(home, pat)
        cands.extend(sorted(glob.glob(full)))
    seen = set()
    for p in cands:
        if not p or p in seen or not os.path.exists(p):
            continue
        seen.add(p)
        try:
            if os.path.realpath(p) == os.path.realpath(sys.executable):
                continue
            ok = subprocess.run([p, "-c", "import phonopy"],
                                capture_output=True, timeout=180).returncode == 0
        except Exception:
            continue
        if ok:
            print("[..] phonopy missing in %s; re-running the plot with %s"
                  % (sys.executable, p), flush=True)
            os.execve(p, [p, os.path.abspath(__file__)] + sys.argv[1:],
                      dict(os.environ, FC_PLOT_REEXEC="1"))


def _emit(result, code):
    print(json.dumps(result, ensure_ascii=False), flush=True)
    sys.exit(code)


def _read_fc2(path):
    import h5py
    with h5py.File(str(path), "r") as h:
        for k in ("fc2", "force_constants"):
            if k in h:
                return np.asarray(h[k][()], float)
    _emit({"status": "error", "reason": "%s has no fc2/force_constants dataset" % path}, 40)


def _matrices(src):
    """Supercell / primitive matrices recorded by the gen or by prep."""
    scm = pm = None
    for name in ("fc_dataset.json", "fit_config.json"):
        p = src / name
        if not p.is_file():
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if scm is None and d.get("supercell_matrix"):
            scm = np.asarray(d["supercell_matrix"], float)
        if pm is None and d.get("primitive_matrix"):
            pm = np.asarray(d["primitive_matrix"], float)
    return scm, pm


def main():
    _ensure_phonopy()
    root = Path.cwd().resolve()
    src = root / SRC
    if not src.is_dir():
        _emit({"status": "error", "reason": "missing %s -- run S1_fit first" % src}, 40)
    fc2p = src / "fc2.hdf5"
    if not fc2p.is_file():
        _emit({"status": "error", "reason": "missing %s (S1_fit did not finish)" % fc2p}, 40)

    scm, pm = _matrices(src)
    if scm is None:
        _emit({"status": "error",
               "reason": "no supercell_matrix in fc_dataset.json / fit_config.json"}, 40)

    from ase.io import read as ase_read
    from phonopy import Phonopy
    from phonopy.structure.atoms import PhonopyAtoms
    a = ase_read(str(src / "POSCAR"), format="vasp")
    # phonopy 2.x needs its own Atoms type, not an ASE Atoms
    uc = PhonopyAtoms(symbols=a.get_chemical_symbols(),
                      cell=np.asarray(a.cell, float),
                      scaled_positions=a.get_scaled_positions(wrap=True))
    ph = Phonopy(uc, supercell_matrix=scm,
                 primitive_matrix=None if pm is None else pm)
    ph.force_constants = _read_fc2(fc2p)
    nac = False
    born = src / "BORN"
    if born.is_file():
        try:
            from phonopy.file_IO import parse_BORN
            params = parse_BORN(ph.primitive, filename=str(born))
            if isinstance(params, dict) and not params.get("factor"):
                params["factor"] = 14.399652
            ph.nac_params = params
            nac = True
        except Exception as e:
            print("[WARN] BORN present but unusable (%s); plotting without NAC" % e)

    try:
        ph.auto_band_structure(plot=False, write_yaml=True, npoints=51,
                               filename=str(src / "band-dft-cpu.yaml"))
        bands = ph.get_band_structure_dict()
    except Exception as e:
        _emit({"status": "error", "reason": "phonopy band structure failed: %s" % e}, 40)

    freqs = np.asarray(bands["frequencies"], dtype=object)
    dists = np.asarray(bands["distances"], dtype=object)
    labels = list(bands.get("labels") or [])
    ticks = []
    for i, lb in enumerate(labels):
        if lb:
            ticks.append((float(dists[i][0]), str(lb).replace("$", "")))
    allf = np.concatenate([np.asarray(f, float).ravel() for f in freqs])
    fmin, fmax = float(allf.min()), float(allf.max())

    outdir = root / OUTDIR
    outdir.mkdir(parents=True, exist_ok=True)

    def _draw(fname, lo, hi):
        fig, ax = plt.subplots(figsize=(6.4, 4.8))
        for q, f in zip(dists, freqs):
            # phonopy returns per-segment (n_qpoints, n_branches) arrays
            q = np.asarray(q, float)
            f = np.asarray(f, float)
            for ib in range(f.shape[1]):
                ax.plot(q, f[:, ib], "-", lw=1.0, color="#1f4e79")
        ax.axhline(0.0, color="0.6", lw=0.8, ls="--")
        for x, lb in ticks:
            ax.axvline(x, color="0.7", lw=0.8)
        if len(ticks) > 1:
            ax.set_xticks([t[0] for t in ticks])
            ax.set_xticklabels([t[1] for t in ticks])
        ax.set_xlim(float(dists[0][0]), float(dists[-1][-1]))
        ax.set_ylim(lo, hi)
        ax.set_ylabel("Frequency (THz)")
        ax.set_title("Phonon dispersion%s" % (" (NAC)" if nac else ""), fontsize=11)
        fig.tight_layout()
        fig.savefig(str(outdir / fname), dpi=200)
        plt.close(fig)

    _draw("phonon_band_full.png", fmin - FULL_PAD, fmax + FULL_PAD)
    _draw("phonon_band_lowfreq.png", min(fmin - FULL_PAD, -1.0), LOWF_MAX)

    summary = {
        "status": "ok",
        "min_frequency_THz": fmin,
        "max_frequency_THz": fmax,
        "nac": nac,
        "n_qpoints": int(len(allf) // max(len(freqs[0][0]), 1)),
        "figures": ["phonon_band_full.png", "phonon_band_lowfreq.png"],
        "band_yaml": str((src / "band-dft-cpu.yaml").relative_to(root)),
        "stable_here": bool(fmin >= -0.10),
    }
    (outdir / "phonon_band_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8", newline="\n")
    print("[DONE] phonon band: min %.4f THz, max %.4f THz%s"
          % (fmin, fmax, " (NAC)" if nac else ""), flush=True)
    _emit(summary, 0)


if __name__ == "__main__":
    main()
