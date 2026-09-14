#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fc_fit_driver.py -- compute-node driver for the fc-fit skill (S1_fit).

This file is the force-constant fitting branch extracted from
kl-dft-cpu/kl_fc_backends.py, generalised into a standalone skill and extended
with a third fitting engine (hiphive).  submit.sh calls its sub-commands in
order; every sub-command runs inside the step directory (step1_fit/):

  prep    Normalise the input dataset into a canonical set of files:
            POSCAR, SPOSCAR                       unit cell / supercell
            dataset_disps.npy, dataset_forces.npy (n+1, Nsc, 3) Cartesian
                                                  displacements / forces, the
                                                  trailing frame being the zero
                                                  equilibrium reference
            disp_matrix.pkl, force_matrix.pkl     (n, Nsc, 3) equilibrium-
                                                  subtracted, for pheasy
            phono3py_disp.yaml / phono3py_params.yaml  copied through when the
                                                  source has them (phono3py engine)
            fc_dataset.json                       provenance / dataset audit
          Supported input signatures, tried in this order inside the dataset dir:
            1. phono3py_params.yaml          (forces embedded)
            2. phono3py_disp.yaml + FORCES_FC3
            3. phono3py_disp.yaml            (forces embedded)
            4. dataset_disps.npy + dataset_forces.npy (+ POSCAR/SPOSCAR)
            5. disp_matrix.pkl + force_matrix.pkl (+ POSCAR/SPOSCAR)
            6. disp-*/vasprun.xml            (+ phono3py_disp.yaml or SPOSCAR)
          Sources 1-3 and 6 need phono3py; sources 4-5 need only phonopy.

  fit     Dispatch on FIT_ENGINE and write fc2.hdf5 (+ fc3.hdf5):
            phono3py  phono3py + symfc/alm least squares
            pheasy    pheasy CLI, four steps (cluster space -> symmetry
                      constraints -> sensing matrix -> fit)
            hiphive   hiphive cluster space + linear regression, optional
                      rotational-sum-rule projection

  post    Export (optional) + imaginary-frequency gate:
            shengbte/FORCE_CONSTANTS_2ND, _3RD, POSCAR   (EXPORT_SHENGBTE=true)
            band-dft-cpu.yaml                             (best effort)
            fc_fit_summary.json      <- S1 marker: "FIT_DONE": true
          Exit status: a genuine imaginary frequency exits 0 (the marker is
          simply not satisfied, so downstream steps are held back); a tool
          error (mesh could not be computed at all) exits non-zero so tf shows
          the step as error.

Engine-independent fit quality: FIT_RMSE_FRAMES > 0 evaluates the fitted force
constants with hiphive's ForceConstantCalculator on that many training frames
and records RMSE / relative RMSE in fc_fit_summary.json.
"""
import glob
import json
import os
import pickle
import re
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np

OUTDIR = "step1_fit"
SB_SUB = "shengbte"
P3_SUB = "phono3py"

# Recognised dataset signatures, most specific first.
SIGNATURES = (
    ("phono3py_params", ("phono3py_params.yaml",)),
    ("phono3py_disp_forces", ("phono3py_disp.yaml", "FORCES_FC3")),
    ("phono3py_disp", ("phono3py_disp.yaml",)),
    ("arrays", ("dataset_disps.npy", "dataset_forces.npy")),
    ("pkl", ("disp_matrix.pkl", "force_matrix.pkl")),
    ("vasprun", ("phono3py_disp.yaml",)),   # validated further by disp-* below
)


# ==========================================================================
# Small helpers
# ==========================================================================
def load_cfg(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def run(cmd, **kw):
    """Run a command in the foreground; exit on a non-zero return code."""
    print("[cmd] %s" % (cmd if isinstance(cmd, str) else " ".join(cmd)), flush=True)
    r = subprocess.run(cmd, shell=isinstance(cmd, str), **kw)
    if r.returncode != 0:
        sys.exit("[ERROR] command failed (rc=%d): %s" % (r.returncode, cmd))
    return r


def _rel_or_abs(p, base):
    p = Path(p).resolve()
    try:
        return str(p.relative_to(Path(base).resolve()))
    except ValueError:
        return str(p)


def _is_none(v):
    return v in (None, "", "None", "none", "null", "False", "false")


def _as_float_or_none(v):
    return None if _is_none(v) else float(v)


def _truthy(v, default=False):
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "1", "yes", "y", "on")


# ==========================================================================
# Reading a phonopy/phono3py-style YAML without importing phono3py
# ==========================================================================
def _atoms_from_phonopy_block(block):
    """Build an ase.Atoms from a phonopy YAML 'cell' block (lattice + points)."""
    from ase import Atoms
    lat = np.asarray(block["lattice"], float)
    syms = [p["symbol"] for p in block["points"]]
    frac = np.asarray([p["coordinates"] for p in block["points"]], float)
    return Atoms(symbols=syms, cell=lat, scaled_positions=frac, pbc=True)


def _load_yaml(path):
    """Load a phono3py YAML file. Returns None when neither PyYAML nor the file
    is usable."""
    try:
        import yaml
    except ImportError:
        return None
    try:
        return yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def _dataset_from_yaml(y):
    """Extract the displacement dataset from a parsed phono3py YAML.

    Returns (kind, disps, forces) where kind is 'random' (type-2, an
    (n, Nsc, 3) pair of arrays) or 'findiff' (type-1, first_atoms)."""
    if not isinstance(y, dict):
        return None, None, None
    for key in ("dataset", "displacement_dataset"):
        ds = y.get(key)
        if not isinstance(ds, dict):
            continue
        if ds.get("displacements") is not None:
            disps = np.asarray(ds["displacements"], float)
            forces = ds.get("forces")
            forces = None if forces is None else np.asarray(forces, float)
            return "random", disps, forces
        if ds.get("first_atoms"):
            return "findiff", None, None
    return None, None, None


# ==========================================================================
# prep: normalise the dataset
# ==========================================================================
def _resolve_dataset_dir(cfg, cwd):
    """Return (path, signature) for the dataset directory, or exit with help."""
    raw = str(cfg.get("dataset_dir") or "").strip()
    if not raw:
        sys.exit("[ERROR] fit_config.json has no dataset_dir -- rerun the gen step")
    cands = [Path(raw)] if raw else []
    for c in cands:
        if c.is_dir():
            kind = _detect_signature(c)
            if kind:
                return c, kind
    sys.exit("[ERROR] dataset dir %s does not exist or holds no recognised "
             "displacement dataset (looked for %s)"
             % (raw, ", ".join(s[0] for s in SIGNATURES)))


def _detect_signature(d):
    """Classify a directory by the dataset files it contains (None if unknown)."""
    d = Path(d)
    if (d / "phono3py_params.yaml").is_file():
        return "phono3py_params"
    if (d / "phono3py_disp.yaml").is_file() and (d / "FORCES_FC3").is_file():
        return "phono3py_disp_forces"
    if list(d.glob("disp-*/vasprun.xml")):
        return "vasprun"
    if (d / "dataset_disps.npy").is_file() and (d / "dataset_forces.npy").is_file():
        return "arrays"
    if (d / "disp_matrix.pkl").is_file() and (d / "force_matrix.pkl").is_file():
        return "pkl"
    if (d / "phono3py_disp.yaml").is_file():
        return "phono3py_disp"
    return None


def _write_poscar(path, atoms, comment="generated by fc-fit"):
    """Minimal VASP POSCAR writer (keeps us independent of phonopy for this)."""
    lat = np.asarray(atoms.cell, float)
    fr = np.asarray(atoms.get_scaled_positions(wrap=True), float)
    syms = list(atoms.get_chemical_symbols())
    order, counts = [], []
    for s in syms:
        if s not in order:
            order.append(s)
            counts.append(syms.count(s))
    lines = [comment, "   1.0"]
    for v in lat:
        lines.append("  %20.16f %20.16f %20.16f" % tuple(v))
    lines.append("  " + "  ".join(order))
    lines.append("  " + "  ".join(str(c) for c in counts))
    lines.append("Direct")
    for s in order:
        for i, si in enumerate(syms):
            if si != s:
                continue
            lines.append("  %20.16f %20.16f %20.16f" % tuple(fr[i]))
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def _read_poscar(path):
    from ase.io import read as ase_read
    return ase_read(str(path), format="vasp")


def _pheasy_supercell_order(uc, sc, dim):
    """Permutation from this dataset's supercell order to pheasy's own.

    pheasy does not read SPOSCAR: it rebuilds the supercell from POSCAR and
    SUPERCELL as per-primitive-atom blocks (all periodic images of primitive
    atom 0 first, then atom 1, ...), each block in the ndindex(dim[::-1])
    translation order.  phonopy/phono3py happen to agree with that layout, but
    ASE Atoms.repeat() interleaves the images and does not -- every atom except
    the first is then scrambled, the fit silently lands near 50% residual with
    a force correlation around 0.7, and pheasy still exits 0.

    Returns the index array 'perm' such that row i of the pheasy-ordered array
    is row perm[i] of the dataset array, or None when the two supercells cannot
    be matched (different shape, rotated cell, missing atoms).
    """
    dim = [int(x) for x in dim]
    if len(dim) != 3 or len(uc) * int(np.prod(dim)) != len(sc):
        return None
    # Cartesian positions of pheasy's supercell: (trans + pos_uc) . cell_uc
    cell = np.asarray(uc.cell, float)
    sc_cell = np.asarray(sc.cell, float)
    try:
        inv = np.linalg.inv(sc_cell)
    except np.linalg.LinAlgError:
        return None
    frac_sc = np.asarray(sc.get_scaled_positions(wrap=True), float) % 1.0
    trans = [list(t)[::-1] for t in np.ndindex(*dim[::-1])]
    perm = []
    for pos in np.asarray(uc.get_scaled_positions(wrap=True), float):
        for t in trans:
            cart = (np.asarray(t, float) + pos) @ cell
            f = (cart @ inv) % 1.0
            d = frac_sc - f
            d -= np.round(d)
            j = int(np.argmin(np.abs(d).sum(axis=1)))
            if np.abs(d[j]).max() > 1e-5:
                return None
            perm.append(j)
    if sorted(perm) != list(range(len(sc))):
        return None
    return np.asarray(perm, int)


def _recorded_pheasy_perm(out):
    """The permutation prep froze in fc_dataset.json, or None."""
    try:
        rec = json.loads((Path(out) / "fc_dataset.json").read_text(
            encoding="utf-8")).get("pheasy_atom_order")
    except Exception:
        return None
    if not isinstance(rec, dict) or not rec.get("permutation"):
        return None
    perm = np.asarray(rec["permutation"], int)
    return None if np.array_equal(perm, np.arange(len(perm))) else perm


def apply_pheasy_order(cfg, out):
    """Put disp_matrix.pkl / force_matrix.pkl into pheasy's atom order.

    Only the two pickles are touched -- they are the pheasy-specific inputs --
    so hiphive (which aligns the supercell internally) and phono3py (which uses
    the YAML/SPOSCAR that prep wrote, in the dataset order) are unaffected.
    Idempotent: the decision is recorded in .pheasy_order.json.
    """
    out = Path(out)
    rec = out / ".pheasy_order.json"
    if rec.is_file():
        return json.loads(rec.read_text(encoding="utf-8")).get("applied", False)
    dp, fp = out / "disp_matrix.pkl", out / "force_matrix.pkl"
    if not (dp.is_file() and fp.is_file()):
        return False
    # The permutation was frozen by prep, while SPOSCAR still described the
    # dataset: pheasy overwrites SPOSCAR with its own atom order, so reading
    # it back here would compare pheasy against pheasy and always say "fine".
    rec_order = None
    try:
        rec_order = json.loads((out / "fc_dataset.json").read_text(
            encoding="utf-8")).get("pheasy_atom_order")
    except Exception:
        rec_order = None
    if isinstance(rec_order, dict) and rec_order.get("permutation"):
        perm = np.asarray(rec_order["permutation"], int)
    else:
        # older prep output (or a hand-built step dir): fall back to the files
        scm, _pm = _cells_cfg(cfg, out)
        if scm is None or np.shape(scm) != (3, 3):
            scm = np.asarray(_diag_supercell_matrix(out), float)
        d = np.diag(scm)
        if np.abs(scm - np.diag(d)).max() > 1e-8:
            print("[WARN] the dataset supercell matrix is not diagonal; pheasy's "
                  "--dim cannot express it, so the atom order cannot be checked",
                  flush=True)
            perm = None
        else:
            perm = _pheasy_supercell_order(_read_poscar(out / "POSCAR"),
                                           _read_poscar(out / "SPOSCAR"),
                                           [int(round(x)) for x in d])
    applied = False
    if perm is None:
        print("[WARN] could not verify the supercell atom order against the one "
              "pheasy builds from POSCAR + SUPERCELL -- if the fit residual is "
              "near 50%% with a force correlation around 0.7, the dataset was "
              "written in ASE repeat() order instead", flush=True)
    elif not np.array_equal(perm, np.arange(len(perm))):
        with open(dp, "rb") as fh:
            disps = pickle.load(fh)
        with open(fp, "rb") as fh:
            forces = pickle.load(fh)
        with open(dp, "wb") as fh:
            pickle.dump(np.asarray(disps, float)[:, perm, :], fh)
        with open(fp, "wb") as fh:
            pickle.dump(np.asarray(forces, float)[:, perm, :], fh)
        applied = True
        print("[WARN] the dataset supercell is not in the atom order pheasy "
              "rebuilds from POSCAR + SUPERCELL, which would have scrambled "
              "every atom but the first.  disp_matrix.pkl / force_matrix.pkl "
              "were permuted into pheasy's order (npy files, SPOSCAR and every "
              "other engine are untouched).", flush=True)
    rec.write_text(json.dumps({"applied": applied,
                               "permutation": perm.tolist() if perm is not None else None},
                              indent=2), encoding="utf-8", newline="\n")
    return applied

def _phonopy_unitcell(path):
    """Unit cell in the form phonopy 2.x accepts.

    phonopy wants its own Atoms (or an explicit (lattice, scaled_positions,
    numbers) tuple); handing it an ASE Atoms fails later with the misleading
    "'Atoms' object has no attribute 'scaled_positions'"."""
    from phonopy.structure.atoms import PhonopyAtoms
    a = _read_poscar(path)
    return PhonopyAtoms(symbols=a.get_chemical_symbols(),
                        cell=np.asarray(a.cell, float),
                        scaled_positions=a.get_scaled_positions(wrap=True))


def _equilibrium_from_vasprun(disp_dir, dirstr):
    """Return (files, missing, equilibrium_file) for disp-*/vasprun.xml."""
    base = Path(disp_dir)
    subs = sorted(glob.glob(str(base / "disp-*")),
                  key=lambda p: int(re.search(r"disp-(\d+)", p).group(1)))
    files, missing, eq = [], [], None
    for d in subs:
        num = re.search(r"disp-(\d+)", d).group(1)
        vr = Path(d) / "vasprun.xml"
        ok = vr.is_file() and vr.stat().st_size
        rel = "%s/disp-%s/vasprun.xml" % (dirstr, num)
        if int(num) == 0:
            eq = rel if ok else None
        elif ok:
            files.append(rel)
        else:
            missing.append(num)
    return files, missing, eq


def _stage_yaml_cells(src, out):
    """Copy POSCAR/SPOSCAR and the phonopy YAMLs from a yaml-based source.

    Returns a dict with the cell matrices when they could be recovered."""
    info = {}
    for f in ("POSCAR", "SPOSCAR", "phono3py_disp.yaml", "phono3py_params.yaml",
              "FORCES_FC3", "BORN"):
        if (src / f).is_file():
            shutil.copyfile(str(src / f), str(out / f))
    y = None
    for f in ("phono3py_params.yaml", "phono3py_disp.yaml"):
        if (out / f).is_file():
            y = _load_yaml(out / f)
            if y:
                break
    if y:
        for key, mat in (("supercell_matrix", "supercell_matrix"),
                         ("primitive_matrix", "primitive_matrix")):
            if y.get(key) is not None:
                info[mat] = [[float(x) for x in row] for row in np.asarray(y[key], float)]
        # Synthesise the cells from the YAML only when the source did not ship
        # them as POSCAR/SPOSCAR (the phonopy YAMLs call them unit_cell /
        # supercell and store fractional coordinates).
        for blk, dst in (("unit_cell", "POSCAR"), ("supercell", "SPOSCAR")):
            if (out / dst).is_file() or not isinstance(y.get(blk), dict):
                continue
            try:
                _write_poscar(out / dst, _atoms_from_phonopy_block(y[blk]))
                print("[..] %s written from the YAML block %s" % (dst, blk), flush=True)
            except Exception as e:
                print("[WARN] could not write %s from %s: %s" % (dst, blk, e))
    return info


def cmd_prep(cfg, out):
    out = Path(out)
    src, kind = _resolve_dataset_dir(cfg, out)
    print("[..] dataset source: %s (%s)" % (src, kind), flush=True)

    info = {"dataset_dir": str(src), "signature": kind}
    disps = forces = None
    eq_forces = None

    if kind in ("phono3py_params", "phono3py_disp", "phono3py_disp_forces"):
        info.update(_stage_yaml_cells(src, out))
        y = None
        for f in ("phono3py_params.yaml", "phono3py_disp.yaml"):
            if (out / f).is_file():
                y = _load_yaml(out / f)
                if y:
                    break
        dkind, disps, forces = _dataset_from_yaml(y)
        info["dataset_type"] = dkind
        if disps is None:
            # Either a finite-displacement dataset (phono3py rebuilds it from
            # FORCES_FC3) or forces missing entirely -- the phono3py engine can
            # still consume the YAML directly, so this is not fatal for it.
            print("[WARN] no type-2 (random) displacement array in the YAML; "
                  "FIT_ENGINE=pheasy / hiphive need one", flush=True)
    elif kind == "vasprun":
        info.update(_stage_yaml_cells(src, out))
        dirstr = _rel_or_abs(src, out)
        files, missing, eqf = _equilibrium_from_vasprun(src, dirstr)
        if not files:
            sys.exit("[ERROR] no disp-*/vasprun.xml under %s" % src)
        if eqf is None:
            sys.exit("[ERROR] missing equilibrium frame disp-00000/vasprun.xml "
                     "under %s -- it anchors the residual-force subtraction" % src)
        from phonopy.interface.vasp import parse_set_of_forces
        sc = _read_poscar(out / "SPOSCAR") if (out / "SPOSCAR").is_file() else None
        if sc is None:
            sys.exit("[ERROR] no SPOSCAR next to the disp-* directories")
        nsc = len(sc)
        rd = parse_set_of_forces(nsc, [str(Path(f)) for f in files + [eqf]],
                                 verbose=False)
        allf = np.asarray(rd["forces"], float)
        forces, eq_forces = allf[:-1], allf[-1]
        # Displacements come from the phono3py YAML when present.
        y = _load_yaml(out / "phono3py_disp.yaml") if (out / "phono3py_disp.yaml").is_file() else None
        dkind, d, _ = _dataset_from_yaml(y)
        if d is None:
            sys.exit("[ERROR] the vasprun source needs phono3py_disp.yaml with a "
                     "type-2 displacement array (finite-displacement datasets are "
                     "rebuilt by the phono3py engine, not here)")
        idx = [int(re.search(r"disp-(\d+)", f).group(1)) - 1 for f in files]
        disps = np.asarray(d, float)[idx]
        if missing:
            print("[WARN] %d frame(s) missing (disp-%s); random-displacement "
                  "regression tolerates this" % (len(missing), ",".join(missing[:8])))
        info["missing_frames"] = missing
        info["equilibrium_source"] = "disp-00000/vasprun.xml"
    elif kind == "arrays":
        y = None
        for f in ("phono3py_params.yaml", "phono3py_disp.yaml"):
            if (src / f).is_file():
                shutil.copyfile(str(src / f), str(out / f))
        for f in ("POSCAR", "SPOSCAR", "BORN", "FORCES_FC3"):
            if (src / f).is_file():
                shutil.copyfile(str(src / f), str(out / f))
        d = np.load(str(src / "dataset_disps.npy"))
        f_ = np.load(str(src / "dataset_forces.npy"))
        if d.ndim != 3 or f_.ndim != 3:
            sys.exit("[ERROR] dataset_disps/forces.npy must be (n, natom, 3)")
        if len(d) != len(f_):
            sys.exit("[ERROR] dataset_disps.npy has %d frames but "
                     "dataset_forces.npy has %d" % (len(d), len(f_)))
        coords = str(cfg.get("coords") or "cartesian").strip().lower()
        if coords == "fractional":
            # dataset_disps.npy holds fractional coordinates, not displacements
            # (the convention prepare_dataset.py consumes with --frac): subtract
            # the trailing reference frame, minimum-image wrap, convert to
            # Cartesian with the supercell lattice, then drop the reference.
            sc = _read_poscar(out / "SPOSCAR")
            cell = np.asarray(sc.cell, float)
            ref = np.asarray(d[-1], float)
            u = np.asarray(d, float) - ref[None]
            u -= np.round(u)                       # minimum image (fractional)
            disps = (u @ cell)[:-1]                # drop the reference frame
            forces = np.asarray(f_[:-1], float)
            eq_forces = np.asarray(f_[-1], float).copy()
            info["equilibrium_source"] = "reference frame (fractional coords)"
        elif np.allclose(d[-1], 0.0):
            # A trailing all-zero frame is the equilibrium reference (kl convention).
            eq_forces = f_[-1].copy()
            info["equilibrium_source"] = "trailing zero-displacement frame"
            disps, forces = d[:-1], f_[:-1]
        else:
            disps, forces = d, f_
            if float(np.abs(d).max()) < 1.0:
                print("[WARN] dataset_disps.npy has no trailing zero-displacement "
                      "frame and every value lies in [0,1) -- it may hold fractional "
                      "coordinates rather than Cartesian displacements.  If so set "
                      "COORDS=fractional, else the fit treats positions as "
                      "displacements.", flush=True)
        info["dataset_type"] = "random"
    else:   # pkl
        for f in ("POSCAR", "SPOSCAR", "BORN", "phono3py_params.yaml",
                  "phono3py_disp.yaml"):
            if (src / f).is_file():
                shutil.copyfile(str(src / f), str(out / f))
        disps = np.asarray(pickle.loads((src / "disp_matrix.pkl").read_bytes()), float)
        forces = np.asarray(pickle.loads((src / "force_matrix.pkl").read_bytes()), float)
        info["dataset_type"] = "random"

    if disps is None or forces is None:
        n_frames = 0
    else:
        disps = np.asarray(disps, float)
        forces = np.asarray(forces, float)
        n_frames = len(disps)
        if disps.shape != forces.shape:
            sys.exit("[ERROR] displacement/force shapes differ: %s vs %s"
                     % (disps.shape, forces.shape))

    # ---- structure: require POSCAR + SPOSCAR for every engine ----
    if not (out / "POSCAR").is_file() or not (out / "SPOSCAR").is_file():
        sys.exit("[ERROR] could not obtain POSCAR/SPOSCAR from %s (signature %s). "
                 "The fit needs both the unit cell and the supercell." % (src, kind))
    sc = _read_poscar(out / "SPOSCAR")
    nsc = len(sc)
    if n_frames and disps.shape[1] != nsc:
        sys.exit("[ERROR] dataset has %d atoms per frame but SPOSCAR has %d -- the "
                 "displacement set and the supercell do not belong together"
                 % (disps.shape[1], nsc))

    # ---- freeze the dataset's own supercell atom order -------------------
    # pheasy rebuilds the supercell from POSCAR + SUPERCELL in its own atom
    # order and *overwrites SPOSCAR* while fitting, so the order has to be
    # captured now, while SPOSCAR still describes the dataset.  Everything that
    # needs to know whether the dataset agrees with pheasy's convention reads
    # this record instead of the file.
    try:
        _scm_guess = info.get("supercell_matrix") or _diag_supercell_matrix(out)
        _d = np.diag(np.asarray(_scm_guess, float))
        _uc, _sp = _read_poscar(out / "POSCAR"), _read_poscar(out / "SPOSCAR")
        if np.abs(np.asarray(_scm_guess, float)
                  - np.diag(_d)).max() <= 1e-8:
            _perm = _pheasy_supercell_order(_uc, _sp, [int(round(x)) for x in _d])
        else:
            _perm = None
        info["pheasy_atom_order"] = (
            None if _perm is None else {
                "is_pheasy_order": bool(np.array_equal(_perm, np.arange(len(_perm)))),
                "permutation": [int(x) for x in _perm]})
        if _perm is not None and not np.array_equal(_perm, np.arange(len(_perm))):
            print("[WARN] the dataset supercell is not in the atom order pheasy "
                  "rebuilds from POSCAR + SUPERCELL (ASE repeat() interleaves the "
                  "images, pheasy blocks them per primitive atom).  The pheasy "
                  "engine will permute its input into pheasy's order; hiphive and "
                  "phono3py are unaffected.", flush=True)
    except Exception as e:
        info["pheasy_atom_order"] = None
        print("[WARN] could not determine the supercell atom order: %s" % e,
              flush=True)

    # ---- equilibrium reference -------------------------------------------
    # Priority: EQUILIBRIUM_FORCES_NPY, the reference frame the dataset itself
    # carried (disp-00000 or a trailing zero-displacement frame), then a
    # reference frame shipped next to the dataset.  A zero-displacement frame is
    # never a training sample: it is dropped and only used as the reference.
    subtract = _truthy(cfg.get("subtract_equilibrium"), True)
    eq_applied = False
    if subtract and eq_forces is None and n_frames:
        eq_forces = _read_equilibrium_npy(cfg, out, nsc)
        if eq_forces is not None:
            info["equilibrium_source"] = "EQUILIBRIUM_FORCES_NPY"

    zero_idx = [i for i in range(n_frames) if np.allclose(disps[i], 0.0)] if n_frames else []
    if zero_idx:
        if eq_forces is None:
            eq_forces = np.asarray(forces, float)[zero_idx[0]].copy()
            info["equilibrium_source"] = "zero-displacement frame in the dataset"
        keep = [i for i in range(n_frames) if i not in zero_idx]
        disps = np.asarray(disps, float)[keep]
        forces = np.asarray(forces, float)[keep]
        n_frames = len(keep)
        info["dropped_reference_frames"] = zero_idx
        print("[..] %d zero-displacement frame(s) kept as the equilibrium "
              "reference and excluded from training" % len(zero_idx), flush=True)

    if subtract and eq_forces is None and n_frames:
        eq_forces = _equilibrium_from_npy_pair(src, nsc)
        if eq_forces is not None:
            info["equilibrium_source"] = "reference frame in dataset_disps/forces.npy"

    if subtract and eq_forces is not None and n_frames:
        forces = forces - np.asarray(eq_forces, float)[None]
        eq_applied = True
        info["equilibrium_max_force"] = float(
            np.linalg.norm(np.asarray(eq_forces, float), axis=1).max())
        print("[OK] equilibrium residual subtracted from %s (max|F_eq| = %.4f eV/A)"
              % (info.get("equilibrium_source", "reference"), info["equilibrium_max_force"]),
              flush=True)
    elif n_frames:
        print("[..] no equilibrium reference available -- forces used as given; "
              "set EQUILIBRIUM_FORCES_NPY if the reference cell is not force-free",
              flush=True)

    # ---- write the canonical dataset ----
    if n_frames:
        zeros = np.zeros((1, nsc, 3))
        np.save(out / "dataset_disps.npy", np.concatenate([disps, zeros], axis=0))
        np.save(out / "dataset_forces.npy",
                np.concatenate([forces, np.zeros((1, nsc, 3))], axis=0))
        # Always rewrite the pickles in the step directory: they are what
        # pheasy reads through --disp_file, and they must hold exactly the
        # equilibrium-subtracted frames used for every other engine.
        with open(out / "disp_matrix.pkl", "wb") as fh:
            pickle.dump(disps, fh)
        with open(out / "force_matrix.pkl", "wb") as fh:
            pickle.dump(forces, fh)
        rms = float(np.sqrt((disps ** 2).mean()))
        if not (1e-8 < rms < 1.0):
            sys.exit("[ERROR] displacement RMS %.3e A is not physical -- check the "
                     "dataset units (Cartesian A expected)" % rms)
    else:
        rms = None

    # ---- unit cell / supercell bookkeeping for the gate ----
    scm = info.get("supercell_matrix")
    if scm is None:
        scm = _diag_supercell_matrix(out)
        info["supercell_matrix"] = scm
        info["supercell_matrix_guess"] = True
    info.update({"n_frames": int(n_frames), "natom_super": int(nsc),
                 "natom_unit": int(len(_read_poscar(out / "POSCAR"))),
                 "displacement_rms": rms,
                 "equilibrium_subtracted": bool(eq_applied)})
    (out / "fc_dataset.json").write_text(
        json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8", newline="\n")
    print("[OK] dataset normalised: %d frames x %d atoms, RMS displacement %s"
          % (n_frames, nsc, ("%.4f A" % rms) if rms else "n/a"), flush=True)
    print("[DONE] prep", flush=True)


def _equilibrium_from_npy_pair(src, nsc):
    """Reference-frame forces from dataset_disps/forces.npy next to the dataset.

    Those files end with an all-zero displacement frame by convention (the kl
    skills write it that way); its forces are the equilibrium residual.  Returns
    None when the pair is absent, mismatched, or has no such frame."""
    try:
        d = np.load(str(Path(src) / "dataset_disps.npy"))
        f = np.load(str(Path(src) / "dataset_forces.npy"))
    except Exception:
        return None
    if d.ndim != 3 or f.ndim != 3 or len(d) != len(f) or len(d) == 0:
        return None
    if d.shape[1] != nsc or not np.allclose(d[-1], 0.0):
        return None
    return np.asarray(f[-1], float).copy()


def _read_equilibrium_npy(cfg, out, nsc):
    p = cfg.get("equilibrium_forces_npy")
    if not p:
        return None
    p = Path(p)
    if not p.is_absolute():
        p = out / p
    if not p.is_file():
        return None
    arr = np.load(str(p))
    if arr.shape != (nsc, 3):
        print("[WARN] equilibrium_forces_npy %s has shape %s, expected %s"
              % (p, arr.shape, (nsc, 3)))
        return None
    return np.asarray(arr, float)


def _diag_supercell_matrix(out):
    """Fall back to the diagonal ratio of SPOSCAR/POSCAR lengths."""
    try:
        uc = _read_poscar(out / "POSCAR")
        sc = _read_poscar(out / "SPOSCAR")
        ru = np.linalg.norm(uc.cell, axis=1)
        rs = np.linalg.norm(sc.cell, axis=1)
        reps = [int(round(rs[i] / ru[i])) for i in range(3)]
        print("[WARN] supercell_matrix not recorded in the source YAML -- guessing "
              "diagonal %s from the POSCAR/SPOSCAR edge lengths" % reps, flush=True)
        return [[reps[0], 0, 0], [0, reps[1], 0], [0, 0, reps[2]]]
    except Exception as e:
        print("[WARN] could not guess the supercell matrix: %s" % e)
        return [[1, 0, 0], [0, 1, 0], [0, 0, 1]]


def _load_dataset(out):
    """Read the canonical dataset written by prep."""
    d = np.load(out / "dataset_disps.npy")
    f = np.load(out / "dataset_forces.npy")
    return np.asarray(d[:-1], float), np.asarray(f[:-1], float)


# ==========================================================================
# fit: phono3py + symfc / alm
# ==========================================================================
def _load_ph3(out):
    """Build a phono3py object carrying the forces.

    phono3py_params.yaml (written by prep or supplied by the source) embeds the
    forces and is preferred; phono3py_disp.yaml + FORCES_FC3 is the fallback."""
    import phono3py
    pm = out / "phono3py_params.yaml"
    if pm.is_file():
        try:
            ph3 = phono3py.load(str(pm), produce_fc=False, is_nac=False, log_level=0)
            ds = ph3.dataset or {}
            if (ds.get("forces") is not None
                    or (ds.get("first_atoms") and "forces" in ds["first_atoms"][0])):
                print("[..] forces read from %s" % pm.name, flush=True)
                return ph3
        except Exception as e:
            print("[WARN] phono3py.load(%s) failed: %s" % (pm.name, e), flush=True)
    dy = out / "phono3py_disp.yaml"
    if not dy.is_file():
        sys.exit("[ERROR] FIT_ENGINE=phono3py needs phono3py_disp.yaml or "
                 "phono3py_params.yaml in the dataset")
    ph3 = phono3py.load(str(dy), produce_fc=False, is_nac=False, log_level=1)
    ds = ph3.dataset or {}
    has = (ds.get("forces") is not None
           or (ds.get("first_atoms") and "forces" in ds["first_atoms"][0]))
    if not has:
        ph3 = phono3py.load(str(dy), forces_fc3_filename="FORCES_FC3",
                            produce_fc=False, is_nac=False, log_level=1)
        ds = ph3.dataset or {}
        has = ds.get("forces") is not None or bool(ds.get("first_atoms"))
    if not has:
        sys.exit("[ERROR] no forces available for the phono3py engine "
                 "(neither embedded in the YAML nor readable from FORCES_FC3)")
    return ph3


def cmd_fit_phono3py(cfg, out):
    import phono3py                                    # noqa: F401
    from phono3py.file_IO import write_fc2_to_hdf5, write_fc3_to_hdf5

    calc = str(cfg.get("fc_calc") or "symfc").lower()
    if calc not in ("symfc", "alm"):
        sys.exit("[ERROR] FC_CALC must be symfc or alm")
    enable = int(cfg.get("enable_fc") or 3)
    cutoff = _as_float_or_none(cfg.get("fc3_cutoff"))

    ph3 = _load_ph3(out)
    print("[..] phono3py fit: calculator=%s fc3_cutoff=%s" % (calc, cutoff), flush=True)
    ph3.produce_fc2(fc_calculator=calc, is_compact_fc=False)
    opts = None if cutoff is None else "cutoff = %s" % cutoff
    ph3.produce_fc3(fc_calculator=calc, fc_calculator_options=opts,
                    is_compact_fc=False)
    write_fc2_to_hdf5(ph3.fc2, filename=str(out / "fc2.hdf5"))
    write_fc3_to_hdf5(ph3.fc3, filename=str(out / "fc3.hdf5"))
    for f in ("fc2.hdf5", "fc3.hdf5"):
        if not (out / f).is_file():
            sys.exit("[ERROR] phono3py produced no %s" % f)
    # NAC from a BORN file, then a self-describing params file for the gate and
    # for whoever picks the force constants up next (matches kl-dft-cpu S5_fc).
    born = out / "BORN"
    if born.is_file():
        try:
            from phonopy.file_IO import parse_BORN
            nac = parse_BORN(ph3.primitive, filename=str(born))
            if isinstance(nac, dict) and not nac.get("factor"):
                nac["factor"] = 14.399652
            ph3.nac_params = nac
            print("[OK] BORN -> nac_params", flush=True)
        except Exception as e:
            print("[WARN] could not read BORN, saving without nac_params: %s" % e,
                  flush=True)
    try:
        ph3.save(str(out / P3_SUB / "phono3py_params.yaml"))
    except Exception:
        (out / P3_SUB).mkdir(exist_ok=True)
        try:
            ph3.save(str(out / P3_SUB / "phono3py_params.yaml"))
        except Exception as e:
            print("[WARN] could not save phono3py_params.yaml: %s" % e, flush=True)
    _write_fit_metrics(out, {"phono3py_fc_calculator": calc,
                             "phono3py_fc3_cutoff": cutoff})
    print("[DONE] fit_phono3py: fc2.hdf5 + fc3.hdf5", flush=True)


# ==========================================================================
# fit: pheasy
# ==========================================================================
PHEASY_METHODS = ("OLS", "LASSO", "ALASSO", "RFE", "RFE-OLS-TSQR", "RIDGE")


def _pheasy_env(method, phase, ncpu, natom_super, tuning="safe", ols_ridge=None,
                ols_maxiter=None, cv_max_iter=None, cv_tol=None):
    """Environment for one pheasy sub-step.

    'phase' is 'setup' for -s/-c, 'displacement' for -d and 'fit' for -f: the
    phases want opposite thread settings (the displacement matrix is built with
    many independent jobs and one BLAS thread each; the fit wants one job with
    all the BLAS threads).

    tuning='safe' (default) sets only what affects memory layout, threading and
    cross-validation grouping -- nothing that can move the solution.
    tuning='kl' additionally reproduces the solver settings hard-coded in the
    kl-dft-cpu submit_fit_pheasy.tpl production template.

    Measured on the local test dataset (BaS, 250 atoms, 10 frames, fc2+fc3,
    c3=4.0, OLS, --rasr BHH), end to end through this driver:

        safe   relative error 0.0216, worst force correlation 0.9997 -> gate stable
        kl     relative error 0.5760, worst force correlation 0.8910 -> gate imaginary

    Bisected to a single variable: PHEASY_OLS_RIDGE=1e-4.  Everything else in
    the kl block (MAXITER, ATOL, BTOL, ILP64, TWOLEVEL) is harmless here.

    Root cause (pheasy core/optimizer.py:_ols_lsmr): the value is not a
    normalised sklearn ridge, it is Tikhonov damping
    damp = sqrt(ridge * n_samples) appended to the LSMR system, so its effect
    grows with the number of equations.  At ndata=7500 that is damp = 0.87,
    which swamps the design matrix.  Dose-response on one dataset, one SM
    cache, one seed:

        ridge      LSMR iters   relative error   correlation
        0               509         0.0216          0.9997
        1e-10           508         0.0216          0.9997
        1e-8            342         0.0306          0.9995
        1e-6             72         0.0676          0.9977
        1e-4             18         0.5805          0.8991

    ridge > 0 also disables pheasy's resident GPU OLS (it falls back to CPU).
    The four production templates that carried it (jzzn / a800 / 3090 /
    hanhai25 submit_fit_pheasy*.tpl) were corrected to PHEASY_OLS_RIDGE=0 on
    2026-09-11, so both tuning profiles now leave it at pheasy's default 0.
    PHEASY_OLS_RIDGE is still honoured as an explicit override -- do not raise
    it without reading the correlation pheasy reports at the end of the fit.
"""
    env = dict(os.environ)
    nblas = min(int(ncpu), 32)
    env.update({
        "PHEASY_SVD_THRESHOLD": "500",
        "PHEASY_ASR_COMBINED": "1",
        "PHEASY_ASR_LWORK_LIMIT": "1500000000",
        "PHEASY_SM_DTYPE": "float32",
        "PHEASY_SM_THR": "1e-12",
        "PHEASY_ASR_SPARSE": "1",
        "PHEASY_ASR_SPARSE_THR": "1e-10",
        "PHEASY_ASR_COL_BLOCK": "5000",
        "PHEASY_BLAS_THREADS": str(nblas),
        "PHEASY_USE_CELER": "1",
        # group the cross-validation folds by configuration, otherwise the
        # 3*natom rows of one frame leak between training and validation
        "PHEASY_CV_GROUP_SIZE": str(3 * int(natom_super)),
    })
    if method == "RFE":
        env.update({
            "PHEASY_USE_RFE": "1", "PHEASY_RFE_TWOLEVEL": "1",
            "MKL_INTERFACE_LAYER": "ILP64", "PHEASY_RFE_MKL": "1",
            "PHEASY_RFE_STEP": "0.1", "PHEASY_RFE_RIDGE_ALPHA": "1e-11",
            "PHEASY_RFE_CV": "5", "PHEASY_RFE_LSMR_MAXITER": "60000",
            "PHEASY_RFE_WARM_START": "1", "PHEASY_COLNORM_FRAMES": "24",
            "PHEASY_COLNORM_EXACT": "0", "PHEASY_RFE_ONE_SE": "1",
        })
    elif method == "OLS" and tuning == "kl":
        env.update({
            "MKL_INTERFACE_LAYER": "ILP64",
            "PHEASY_OLS_ATOL": "1e-6", "PHEASY_OLS_BTOL": "1e-6",
        })
    if method == "OLS":
        if ols_ridge is not None:
            env["PHEASY_OLS_RIDGE"] = str(ols_ridge)
        else:
            # Never inherit a stray ridge: it is sqrt(ridge*ndata) damping, not
            # a normalised ridge, and 1e-4 silently wrecked a production fit.
            env.pop("PHEASY_OLS_RIDGE", None)
        if ols_maxiter is not None:
            env["PHEASY_OLS_MAXITER"] = str(ols_maxiter)
    elif method == "RIDGE":
        env["PHEASY_USE_CELER"] = "0"
    if phase == "displacement":
        env.update({"OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1",
                    "MKL_NUM_THREADS": "1", "PHEASY_N_JOBS": str(int(ncpu))})
    if cv_max_iter is not None:
        # The LASSO/ALASSO CV is the most expensive part of a pheasy fit and
        # pheasy caps it at min(max_iter, 800) with tol >= 1e-3; on a large
        # system every CV solve then stops at the cap (relative KKT ~2e-2) and
        # the alpha ranking is a convergence artefact.
        env["PHEASY_CV_MAX_ITER"] = str(int(cv_max_iter))
    if cv_tol is not None:
        env["PHEASY_CV_TOL"] = str(float(cv_tol))
    if phase == "fit":
        env.update({"OPENBLAS_NUM_THREADS": str(nblas), "OMP_NUM_THREADS": str(nblas),
                    "MKL_NUM_THREADS": str(nblas), "PHEASY_N_JOBS": "1",
                    "LOKY_MAX_CPU_COUNT": "1", "PHEASY_DOT_THREADS": str(int(ncpu)),
                    "OMP_NESTED": "FALSE", "MKL_DYNAMIC": "FALSE"})
    else:
        env.update({"OPENBLAS_NUM_THREADS": str(nblas), "OMP_NUM_THREADS": str(nblas),
                    "MKL_NUM_THREADS": str(nblas)})
    return env


def _apply_cv_knobs(env, cfg):
    """PHEASY_CV_MAX_ITER / PHEASY_CV_TOL from step.conf.

    pheasy's cross-validation stops every alpha at min(max_iter, 400) iterations
    (resident LASSO backend) with tol = max(tol, 1e-3).  At that cap the CV curve
    is not resolved, so the selected alpha can sit on the grid boundary and the
    fit looks far worse than the model is -- measured on the 3090 (Mg4C60,
    512 atoms, 128 frames): 400 iterations, relative KKT ~5e-3,
    alpha_opt = grid minimum, 8.2 % relative error.  pheasy's own warning tells
    the user to raise/lower exactly these two; this makes them reachable from
    step.conf instead of the submit template.
    """
    for key, env_key in (("pheasy_cv_max_iter", "PHEASY_CV_MAX_ITER"),
                         ("pheasy_cv_tol", "PHEASY_CV_TOL")):
        v = str(cfg.get(key) or "").strip()
        if v:
            env[env_key] = v


def _run_streaming(cmd, env):
    """Run a command, echoing its output line by line while collecting it.

    The fit is the one step that can run for an hour and print only at the end;
    capture_output=True hid even those lines until the process exited, so a
    running job looked dead (measured on the 3090: a 40-minute dense-LASSO fit
    with an empty queue.out).  Returns (returncode, text).
    """
    proc = subprocess.Popen(cmd, shell=True, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1)
    chunks = []
    try:
        for line in proc.stdout:
            chunks.append(line)
            sys.stdout.write(line)
            sys.stdout.flush()
    finally:
        proc.stdout.close()
        rc = proc.wait()
    return rc, "".join(chunks)


def _reconcile_gpu_env(env, method, cfg):
    """Keep the requested GPU knobs consistent with the sensing matrix pheasy builds.

    PHEASY_GPU_LASSO_RESIDENT (pheasy's initial spelling:
    PHEASY_GPU_TWOLEVEL_LASSO) exists only for a TwoLevelSM input.  With the
    dense/CSR matrix pheasy builds for LASSO by default it raises

        NotImplementedError: Resident GPU LASSO/ALASSO requires TwoLevelSM input

    and it raises it *after* the sensing matrix has been materialised, i.e. tens
    of minutes into the job (measured on the 3090: the run died 2 min into the
    fit, after a 10.8 GB sm_dense.npy).  run_pheasy.py only builds a TwoLevelSM
    when PHEASY_LASSO_TWOLEVEL=1 (default 0 for LASSO, 1 for OLS via
    PHEASY_OLS_TWOLEVEL), so asking for the GPU on a LASSO fit has to select the
    two-level path as well -- otherwise the request is dropped with a warning
    instead of killing the job.

    Returns the keys that belong in fit_metrics.json.
    """
    info = {"pheasy_gpu_requested": False, "pheasy_gpu_resident": False,
            "pheasy_lasso_twolevel": False}
    ngpu = str(cfg.get("pheasy_ngpu") or "").strip()
    if ngpu:
        try:
            n = int(ngpu)
        except ValueError:
            sys.exit("[ERROR] PHEASY_NGPU must be an integer (GPU count), got %r"
                     % ngpu)
        if n < 1:
            sys.exit("[ERROR] PHEASY_NGPU must be >= 1, got %d" % n)
        # The explicit count wins over whatever the submit template exported, and
        # it is the same number the job's --gres was rendered from (the gen writes
        # that from PHEASY_NGPU).  1..6 devices is what pheasy's own acceptance
        # suite covers; the ordinals are CUDA ordinals inside the allocation,
        # which is what PHEASY_GPU_SM_DEVICES expects.
        env["PHEASY_GPU_SM_NGPU"] = str(n)
        env["PHEASY_GPU_SM_DEVICES"] = ",".join(str(i) for i in range(n))
        info["pheasy_ngpu"] = n
        print("[..] pheasy GPU: %d device(s), PHEASY_GPU_SM_DEVICES=%s"
              % (n, env["PHEASY_GPU_SM_DEVICES"]), flush=True)
    info["pheasy_gpu_requested"] = any(
        _truthy(env.get(k)) for k in ("PHEASY_USE_GPU", "PHEASY_GPU_SM"))
    # The *resident* flag is LASSO/ALASSO-only (pheasy raises
    # NotImplementedError for it elsewhere), but PHEASY_LASSO_TWOLEVEL is
    # honoured for the whole LASSO family: run_pheasy.py accepts it for
    # LASSO / ALASSO / RIDGE.  Gating both on ("LASSO", "ALASSO") silently
    # dropped the two-level request for RIDGE, which then materialised the dense
    # sensing matrix instead of wrapping SM_prime/NS in a TwoLevelSM -- measured
    # at c2=6.5/c3=4.5 that is a 89.5 GB dense SM plus a 64 GB dense NS, no
    # two-level matvec and therefore no GPU path at all.
    resident = False
    if method in ("LASSO", "ALASSO"):
        _res = str(cfg.get("pheasy_gpu_lasso_resident") or "").strip().lower()
        if _res in ("1", "true", "yes", "y", "on"):
            env["PHEASY_GPU_LASSO_RESIDENT"] = "1"
        elif _res in ("0", "false", "no", "n", "off"):
            # Explicitly off also clears pheasy's initial spelling of the flag.
            for k in ("PHEASY_GPU_LASSO_RESIDENT", "PHEASY_GPU_TWOLEVEL_LASSO"):
                env.pop(k, None)
        resident = any(_truthy(env.get(k)) for k in
                       ("PHEASY_GPU_LASSO_RESIDENT", "PHEASY_GPU_TWOLEVEL_LASSO"))
    raw = str(cfg.get("pheasy_lasso_twolevel") or "").strip().lower()
    explicit = raw in ("1", "true", "yes", "y", "on", "0", "false", "no", "n", "off")
    want_tl = _truthy(cfg.get("pheasy_lasso_twolevel"), False)
    if resident and explicit and not want_tl:
        for k in ("PHEASY_GPU_LASSO_RESIDENT", "PHEASY_GPU_TWOLEVEL_LASSO"):
            env.pop(k, None)
        print("[WARN] PHEASY_GPU_LASSO_RESIDENT dropped: pheasy's resident LASSO "
              "needs a TwoLevelSM input, and PHEASY_LASSO_TWOLEVEL is explicitly "
              "off.  Drop the flag or set PHEASY_LASSO_TWOLEVEL=true.",
              flush=True)
        return info
    if resident and not want_tl:
        want_tl = True
        print("[..] PHEASY_GPU_LASSO_RESIDENT is set: selecting the two-level "
              "sensing matrix as well (PHEASY_LASSO_TWOLEVEL=1), otherwise the "
              "resident backend refuses the dense/CSR matrix.", flush=True)
    if want_tl:
        env["PHEASY_LASSO_TWOLEVEL"] = "1"
        info["pheasy_lasso_twolevel"] = True
        info["pheasy_gpu_resident"] = bool(resident)
    return info


def cmd_fit_pheasy(cfg, out):
    """Four pheasy CLI steps: cluster space / symmetry constraints / sensing
    matrix / fit.  Mirrors templates in kl-dft-cpu and _common/mace."""
    method = str(cfg.get("pheasy_method") or "RFE").upper()
    if method not in PHEASY_METHODS:
        sys.exit("[ERROR] PHEASY_FIT_METHOD must be one of %s"
                 % ", ".join(PHEASY_METHODS))
    binary = str(cfg.get("pheasy_bin") or "pheasy")
    if not shutil.which(binary):
        sys.exit("[ERROR] %r is not on PATH -- check CONDA_ENV/CONDA_SH in "
                 "step.conf (the submit template activates it) and PHEASY_BIN "
                 "(%s)" % (binary, "pheasy | pheasy-gpu"))
    dim = str(cfg.get("supercell") or "").strip()
    if not dim:
        scm = np.asarray((cfg.get("supercell_matrix") or []), float)
        if scm.size == 9:
            dim = " ".join(str(int(round(x))) for x in np.diag(scm))
    if not dim:
        sys.exit("[ERROR] unknown supercell for pheasy -- set SUPERCELL in step.conf")
    enable = int(cfg.get("enable_fc") or 3)
    tuning = str(cfg.get("pheasy_tuning") or "safe").strip().lower()
    if tuning not in ("safe", "kl"):
        sys.exit("[ERROR] PHEASY_TUNING must be 'safe' or 'kl'")
    ols_ridge = _as_float_or_none(cfg.get("pheasy_ols_ridge"))
    ols_maxiter = None
    _om = str(cfg.get("pheasy_ols_maxiter") or "").strip()
    if _om:
        try:
            ols_maxiter = int(_om)
        except ValueError:
            sys.exit("[ERROR] PHEASY_OLS_MAXITER must be an integer, got %r" % _om)
    if tuning == "kl":
        print("[WARN] PHEASY_TUNING=kl: reproducing the kl-dft-cpu production "
              "solver settings (MAXITER/ATOL/BTOL/ILP64/TWOLEVEL). The "
              "PHEASY_OLS_RIDGE=1e-4 that used to come with them is NOT set: "
              "it is sqrt(ridge*ndata) LSMR damping that measurably degraded "
              "the fit, and the production templates were corrected on "
              "2026-09-11.", flush=True)
    eps = float(cfg.get("null_space_eps") or 0.001)
    apply_pheasy_order(cfg, out)
    disps, forces = _load_dataset(out)

    c2 = _as_float_or_none(cfg.get("pheasy_c2_cutoff"))
    c3 = _as_float_or_none(cfg.get("pheasy_c3_cutoff"))
    flags = []
    if c2 is not None:
        flags += ["--c2", str(c2)]
    if c3 is not None and enable >= 3:
        flags += ["--c3", str(c3)]
    cflag = " ".join(flags)
    wflag = "-w %d" % enable

    natom_super = len(_read_poscar(out / "SPOSCAR"))
    try:
        ncpu = int(os.environ.get("SLURM_CPUS_PER_TASK")
                   or len(os.sched_getaffinity(0)) or 4)
    except Exception:
        ncpu = int(os.environ.get("SLURM_CPUS_PER_TASK") or 4)

    base = "%s --dim %s %s %s --eps %s" % (binary, dim, wflag, cflag, eps)

    # fit step: -l LASSO for RFE is deliberate -- the RFE strategy itself is
    # selected through PHEASY_USE_RFE in the environment
    fit_flags = ["--full_ifc", "-l", ("LASSO" if method == "RFE" else method),
                 "--hdf5"]
    rasr = str(cfg.get("pheasy_rasr") or "").strip()
    if rasr and rasr.lower() not in ("none", "false", "off"):
        fit_flags += ["--rasr", rasr]
    if _truthy(cfg.get("pheasy_std"), False):
        fit_flags.append("--std")
    seed = cfg.get("pheasy_seed")
    if seed is not None:
        fit_flags += ["--seed", str(int(seed))]
    # RIDGE belongs here too.  Without it RIDGE silently fell back to
    # run_pheasy's own defaults (CV=5, NALPHA=50, a 4-decade default grid), so
    # every pheasy_cv / pheasy_nmu / pheasy_mu_min / pheasy_mu_max in
    # fit_config.json was ignored -- measured: a ridge sweep logged
    # "[RIDGE-CV] alpha 27/50" with 5 folds while fit_config.json said
    # nmu=1, mu=4.052069, cv=2.  Everything downstream (which alpha the solver
    # ever sees, and therefore whether the fit is regularised at all) followed
    # from that one missing string.
    if method in ("LASSO", "ALASSO", "RFE", "RFE-OLS-TSQR", "RIDGE"):
        # pheasy parses these with argparse type=int, so "-8.0" is rejected
        # outright -- coerce here as well so a hand-written fit_config.json
        # cannot get past the gen step with a float and fail on the node.
        def _gi(key, dflt):
            v = cfg.get(key)
            if v in (None, ""):
                return int(dflt)
            try:
                return int(str(v).strip())
            except ValueError:
                sys.exit("[ERROR] %s must be an integer (alpha exponent for "
                         "pheasy's argparse), got %r" % (key, v))
        fit_flags += ["--cv", str(_gi("pheasy_cv", 5)),
                      "--nmu", str(_gi("pheasy_nmu", 40)),
                      "--mu_min", str(_gi("pheasy_mu_min", -8)),
                      "--mu_max", str(_gi("pheasy_mu_max", -5)),
                      "--max_iter", str(_gi("pheasy_max_iter", 100000)),
                      "--tol", str(float(cfg.get("pheasy_tol") or 1e-5))]
    fit_step = "%s -f --ndata %d %s" % (base, len(disps), " ".join(fit_flags))
    disp_step = "%s -d --ndata %d --disp_file" % (base, len(disps))

    run_steps = (("cluster space", "setup", base + " -s"),
                 ("symmetry constraints", "setup", base + " -c"),
                 ("displacement matrix", "displacement", disp_step))
    for label, phase, cmd in run_steps:
        print("[pheasy] %s: %s" % (label, cmd), flush=True)
        r = subprocess.run(cmd, shell=True,
                           env=_pheasy_env(method, phase, ncpu, natom_super,
                                           tuning, ols_ridge, ols_maxiter))
        if r.returncode != 0:
            sys.exit("[ERROR] pheasy %s failed (rc=%d)" % (label, r.returncode))
    print("[pheasy] fit: %s" % fit_step, flush=True)
    env = _pheasy_env(method, "fit", ncpu, natom_super, tuning, ols_ridge,
                      ols_maxiter,
                      cv_max_iter=(str(cfg.get("pheasy_cv_max_iter") or "").strip() or None),
                      cv_tol=(str(cfg.get("pheasy_cv_tol") or "").strip() or None))
    _apply_cv_knobs(env, cfg)
    gpu_info = _reconcile_gpu_env(env, method, cfg)
    rc, log_txt = _run_streaming(fit_step, env)
    if rc != 0:
        sys.exit("[ERROR] pheasy fit failed (rc=%d)" % rc)

    # pheasy checks the per-configuration force correlation itself and warns
    # when it drops.  Interpret it carefully:
    #   corr < ~0.5   the mapping between the dataset and pheasy's supercell is
    #                 broken -- usually the atom order (pheasy blocks the images
    #                 per primitive atom, ASE repeat() interleaves them), so the
    #                 fit is worthless even though pheasy exits 0.  Gate failure.
    #   ~0.5 .. 0.98  ordinary model error: an fc2-only fit of anharmonic data,
    #                 or cutoffs too small.  Recorded, not fatal -- otherwise
    #                 every legitimate ENABLE_FC=2 run would be rejected.
    metrics = _pheasy_metrics(log_txt)
    metrics["pheasy_method"] = method
    metrics.update(gpu_info)
    if gpu_info.get("pheasy_gpu_requested") and not metrics.get("pheasy_gpu_used"):
        print("[WARN] PHEASY_USE_GPU/PHEASY_GPU_SM were requested but pheasy's "
              "log shows no GPU code path: this fit ran on the CPU.  The GPU "
              "backend only accelerates the two-level sparse matvec, so for "
              "LASSO it needs PHEASY_LASSO_TWOLEVEL=true (see the skill README).",
              flush=True)
    if method == "OLS" and metrics.get("pheasy_lsmr_istop") == 7:
        print("[..] OLS solver stopped at the iteration limit (istop=7, itn=%s): "
              "the residual is at the cap, not converged to atol.  Raise "
              "PHEASY_OLS_MAXITER to keep iterating (it leaves the fit otherwise "
              "unchanged)." % metrics.get("pheasy_lsmr_itn"), flush=True)
    mort = metrics.get("pheasy_worst_force_correlation")
    if mort is not None and mort < 0.5:
        print("[FAIL] pheasy's per-configuration force correlation collapsed to "
              "%.3f: the fitted constants do not reproduce the training forces, so "
              "the dataset and pheasy's supercell almost certainly disagree on the "
              "atom order (pheasy blocks the periodic images per primitive atom, "
              "while ASE repeat() interleaves them).\n"
              "        Compare POSCAR + SUPERCELL against the ordering the dataset "
              "was produced with before trusting anything else." % mort, flush=True)
        (out / ".fit_gate_fail").write_text(
            "pheasy force correlation %.3f (atom-order mismatch?)\n" % mort)
    elif mort is not None and mort < 0.98:
        print("[..] pheasy force correlation %.3f -- ordinary model error (fc2 "
              "only, or cutoffs too small); widen PHEASY_C2_CUTOFF / "
              "PHEASY_C3_CUTOFF or fit fc3 as well if it matters" % mort, flush=True)
    _write_fit_metrics(out, metrics)

    # LASSO alpha on the grid boundary is a red flag: the selection is bogus.
    if method in ("LASSO", "ALASSO"):
        a = metrics.get("pheasy_best_alpha")
        if a:
            lo = float(cfg.get("pheasy_mu_min", -8))
            hi = float(cfg.get("pheasy_mu_max", -2))
            lg = np.log10(a) if a > 0 else -99
            if lg <= lo + 0.05 or lg >= hi - 0.05:
                print("[FAIL] best alpha=%.3e sits on the grid boundary [%g,%g] -- "
                      "the LASSO selection is not trustworthy; add frames or widen "
                      "the alpha grid" % (a, lo, hi), flush=True)
                (out / ".fit_gate_fail").write_text("alpha on grid boundary\n")

    for f in ("fc2.hdf5",):
        if not (out / f).is_file():
            sys.exit("[ERROR] pheasy produced no %s" % f)
    if enable >= 3 and not (out / "fc3.hdf5").is_file():
        sys.exit("[ERROR] pheasy produced no fc3.hdf5 (ENABLE_FC=3 requested)")
    print("[DONE] fit_pheasy: method=%s ndata=%d dim=%s"
          % (method, len(disps), dim), flush=True)


def _pheasy_metrics(log_txt):
    """Pull the numbers pheasy reports out of its log."""
    m = {}
    for key, pat, cast in (
            ("pheasy_rmse_eV_per_A", r"\bRMSE:\s*([\d.eE+-]+)", float),
            ("pheasy_relative_error", r"Relative error:\s*([\d.eE+-]+)", float),
            ("pheasy_worst_force_correlation", r"worst corr=([\d.]+)", float),
            ("pheasy_free_ifcs", r"Free IFC terms:\s*(\d+)", int),
            ("pheasy_best_alpha", r"best alpha=\s*([\d.eE+-]+)", float),
            # pheasy prints "- alpha_opt: <x>" (the dense path prints
            # "best alpha="), so without these two the boundary gate below and
            # the recorded metric both saw nothing for FIT_ENGINE=pheasy.
            ("pheasy_alpha_opt", r"alpha_opt:[ ]*([0-9.eE+-]+)", float),
            ("pheasy_alpha_min", r"alpha_min:[ ]*([0-9.eE+-]+)", float),
            ("pheasy_alpha_max", r"alpha_max:[ ]*([0-9.eE+-]+)", float),
            ("pheasy_lsmr_istop", r"\[LSMR\] istop=(\d+)", int),
            ("pheasy_lsmr_itn", r"\[LSMR\] istop=\d+ itn=(\d+)", int)):
        mm = re.search(pat, log_txt)
        if mm:
            try:
                m[key] = cast(mm.group(1))
            except ValueError:
                pass
    if "pheasy_best_alpha" not in m and "pheasy_alpha_opt" in m:
        m["pheasy_best_alpha"] = m["pheasy_alpha_opt"]
    # A LASSO alpha pinned to the grid edge is only meaningful when the CV
    # solver converged; pheasy says so itself when FISTA hits its cap.
    m["pheasy_cv_hit_cap"] = "FISTA did not converge" in log_txt
    m["pheasy_cv_warning"] = "[CV] WARNING" in log_txt
    # Which code path pheasy actually took.  The GPU backend only accelerates the
    # two-level sparse matvec, so a GPU request without one of these markers is a
    # CPU fit -- silent until now (an hour went into discovering that on the 3090,
    # where PHEASY_USE_GPU=1 + 4 allocated cards still produced a CPU fit).
    ev = []
    for name, pat in (
            ("gpu_sm_matvec", r"\[GPU-SM\]\s*SpMV on"),
            ("gpu_resident_lasso", r"\[gpu_resident\]"),
            ("gpu_cv_folds", r"\[GPU\]\s*RIDGE CV"),
            ("twolevel_sm", r"\[SM-twolevel\]")):
        if re.search(pat, log_txt):
            ev.append(name)
    if re.search(r"\[GPU-SM\]\s*SpMV unavailable", log_txt):
        ev.append("gpu_sm_unavailable")
    m["pheasy_gpu_evidence"] = ev
    m["pheasy_gpu_used"] = any(n.startswith("gpu_") and n != "gpu_sm_unavailable"
                               for n in ev)
    return m


def _write_fit_metrics(out, metrics):
    """Merge engine-specific numbers into step1_fit/fit_metrics.json.

    cmd_post folds this file into phonon_summary.json, so the fit quality is
    visible next to the gate verdict instead of buried in queue.out."""
    if not metrics:
        return
    p = out / "fit_metrics.json"
    old = {}
    if p.is_file():
        try:
            old = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            old = {}
    old.update(metrics)
    p.write_text(json.dumps(old, indent=2, ensure_ascii=False), encoding="utf-8",
                 newline="\n")


# ==========================================================================
# fit: hiphive
# ==========================================================================
def _hiphive_fit(A, y, method, alpha):
    """Fit the hiphive design matrix.  Works with either hiphive generation:
    the old 'hiphive.fitting.Optimizer' API or a plain scikit-learn regressor
    (hiphive >= 1.5 dropped the fitting module)."""
    method = method.lower()
    try:
        from hiphive.fitting import Optimizer            # hiphive < 1.5
        return np.asarray(Optimizer((A, y)).train().parameters, float), "hiphive.Optimizer"
    except ImportError:
        pass
    from sklearn.linear_model import LinearRegression, Ridge, Lasso, BayesianRidge
    if method in ("ols", "linear"):
        reg = LinearRegression(fit_intercept=False)
    elif method in ("ridge", "bayes", "bayesian"):
        reg = BayesianRidge(fit_intercept=False) if method in ("bayes", "bayesian") \
            else Ridge(alpha=float(alpha), fit_intercept=False)
    elif method == "lasso":
        reg = Lasso(alpha=float(alpha), fit_intercept=False, max_iter=20000)
    elif method in ("ard", "ardregression"):
        from sklearn.linear_model import ARDRegression
        reg = ARDRegression(fit_intercept=False)
    else:
        sys.exit("[ERROR] HIPHIVE_FIT_METHOD must be ols | ridge | lasso | ard | bayes")
    reg.fit(A, y)
    return np.asarray(reg.coef_, float).ravel(), "sklearn." + type(reg).__name__


def cmd_fit_hiphive(cfg, out):
    """Fit fc2/fc3 from the displaced configurations with hiphive.

    hiphive determines its own (cutoff-bounded) atom list; the configurations
    handed to it can be any supercell of the primitive cell, because hiphive
    aligns each structure internally.  We therefore feed the full dataset
    supercell with the documented per-atom 'displacements' and 'forces' arrays
    and lift the fitted parameters back onto the same supercell."""
    from hiphive import ClusterSpace, StructureContainer, ForceConstantPotential
    import hiphive

    enable = int(cfg.get("enable_fc") or 3)
    c2 = cfg.get("hiphive_cutoff2")
    c3 = cfg.get("hiphive_cutoff3")
    if c2 in (None, ""):
        sys.exit("[ERROR] HIPHIVE_CUTOFF2 is required for FIT_ENGINE=hiphive")
    cutoffs = [float(c2)] + ([float(c3)] if enable >= 3 else [])
    symprec = float(cfg.get("hiphive_symprec") or 1e-5)
    disps, forces = _load_dataset(out)
    uc = _read_poscar(out / "POSCAR")
    sc = _read_poscar(out / "SPOSCAR")
    nsample = int(cfg.get("hiphive_n_configs") or 0)
    if nsample and nsample < len(disps):
        idx = np.linspace(0, len(disps) - 1, nsample).round().astype(int)
        disps, forces = disps[idx], forces[idx]
        print("[..] hiphive uses %d of the available frames" % len(disps), flush=True)

    # Reference structure for the cluster space: the primitive cell derived from
    # the unit cell (identity when they coincide).
    import spglib
    from ase import Atoms
    prim_matrix = cfg.get("primitive_matrix")
    if prim_matrix and np.allclose(np.asarray(prim_matrix, float), np.eye(3)):
        prim = uc
    else:
        lat = np.asarray(uc.cell, float)
        std = spglib.standardize_cell((lat, uc.get_scaled_positions(),
                                       uc.get_atomic_numbers()), to_primitive=True,
                                      no_idealize=True, symprec=symprec)
        if std is None:
            print("[WARN] spglib could not find a primitive cell -- using POSCAR",
                  flush=True)
            prim = uc
        else:
            prim = Atoms(numbers=std[2], cell=std[0], scaled_positions=std[1], pbc=True)
    print("[..] hiphive: primitive=%d atoms, supercell=%d atoms, cutoffs=%s"
          % (len(prim), len(sc), cutoffs), flush=True)

    cs = ClusterSpace(prim, cutoffs, symprec=symprec)
    container = StructureContainer(cs)
    for u, f in zip(disps, forces):
        a = sc.copy()                      # ideal supercell: hiphive reads the
        a.new_array("displacements", u)    # displacement/force arrays, positions
        a.new_array("forces", f)           # are only used for alignment
        container.add_structure(a)
    A, y = container.get_fit_data()
    print("[..] hiphive design matrix %s, target %s, %d parameters"
          % (A.shape, y.shape, cs.n_dofs), flush=True)

    params, backend = _hiphive_fit(A, y, str(cfg.get("hiphive_fit_method") or "ridge"),
                                   cfg.get("hiphive_alpha", 1e-10))
    print("[OK] fitted with %s" % backend, flush=True)

    if _truthy(cfg.get("hiphive_enforce_asr"), True):
        try:
            params = hiphive.enforce_rotational_sum_rules(
                cs, params, sum_rules=["Huang", "Born-Huang"])
            print("[OK] rotational sum rules projected onto the parameters", flush=True)
        except Exception as e:
            print("[WARN] rotational-sum-rule projection skipped: %s" % e, flush=True)

    fcp = ForceConstantPotential(cs, params)
    fcs = fcp.get_force_constants(sc)

    # fc2: dense array is small; write phonopy hdf5 (dataset 'force_constants')
    # and the phonopy text file for maximum compatibility.
    fc2 = np.asarray(fcs.get_fc_array(order=2, format="phonopy"), float)
    _write_fc2_hdf5(fc2, out / "fc2.hdf5")
    try:
        fcs.write_to_phonopy(str(out / "FORCE_CONSTANTS"), format="text")
    except Exception as e:
        print("[WARN] could not write FORCE_CONSTANTS: %s" % e, flush=True)

    if enable >= 3:
        # fc3 can be huge (n^3 * 27); use hiphive's streaming writer instead of
        # materialising the dense array through get_fc_array(order=3).
        try:
            fcs.write_to_phono3py(str(out / "fc3.hdf5"))
            print("[OK] fc3.hdf5 written by hiphive", flush=True)
        except Exception as e:
            print("[WARN] hiphive write_to_phono3py failed: %s" % e, flush=True)
        try:
            (out / SB_SUB).mkdir(exist_ok=True)
            fcs.write_to_shengBTE(str(out / SB_SUB / "FORCE_CONSTANTS_3RD"), prim)
            print("[OK] shengbte/FORCE_CONSTANTS_3RD written by hiphive", flush=True)
        except Exception as e:
            print("[WARN] hiphive write_to_shengBTE failed: %s" % e, flush=True)

    np.save(out / "fc2_parameters.npy", params)
    if not (out / "fc2.hdf5").is_file():
        sys.exit("[ERROR] hiphive produced no fc2.hdf5")
    _write_fit_metrics(out, {"hiphive_backend": backend,
                             "hiphive_parameters": int(cs.n_dofs),
                             "hiphive_design_matrix": "%d x %d" % (A.shape[0], A.shape[1]),
                             "hiphive_cutoffs": list(cutoffs),
                             "hiphive_sum_rules_enforced":
                                 bool(_truthy(cfg.get("hiphive_enforce_asr"), True))})
    print("[DONE] fit_hiphive", flush=True)


def _write_fc2_hdf5(fc2, path):
    """Write fc2 in the phono3py/phonopy hdf5 layouts (dataset 'fc2' plus the
    phonopy-compatible 'force_constants')."""
    try:
        from phono3py.file_IO import write_fc2_to_hdf5
        write_fc2_to_hdf5(fc2, filename=str(path))
        return
    except Exception:
        pass
    import h5py
    with h5py.File(str(path), "w") as h:
        h.create_dataset("fc2", data=fc2, compression="gzip")
        h.create_dataset("force_constants", data=fc2, compression="gzip")


def cmd_fit(cfg, out):
    engine = str(cfg.get("engine") or "phono3py").lower()
    print("[..] FIT_ENGINE=%s" % engine, flush=True)
    if engine == "phono3py":
        cmd_fit_phono3py(cfg, out)
    elif engine == "pheasy":
        cmd_fit_pheasy(cfg, out)
    elif engine == "hiphive":
        cmd_fit_hiphive(cfg, out)
    else:
        sys.exit("[ERROR] FIT_ENGINE must be phono3py | pheasy | hiphive")


# ==========================================================================
# post: export + imaginary-frequency gate
# ==========================================================================
def _read_fc2_any(out):
    """Read fc2 as a numpy array from fc2.hdf5 / FORCE_CONSTANTS / fc2.npy."""
    import h5py
    p = out / "fc2.hdf5"
    if p.is_file():
        with h5py.File(str(p), "r") as h:
            for k in ("fc2", "force_constants"):
                if k in h:
                    return np.asarray(h[k][()], float)
    p = out / "fc2.npy"
    if p.is_file():
        return np.asarray(np.load(str(p)), float)
    p = out / "FORCE_CONSTANTS"
    if p.is_file():
        from phonopy.file_IO import parse_FORCE_CONSTANTS
        return np.asarray(parse_FORCE_CONSTANTS(str(p)), float)
    sys.exit("[ERROR] no fc2 artifact found (fc2.hdf5 / FORCE_CONSTANTS / fc2.npy)")


def _read_fc3_any(out):
    import h5py
    p = out / "fc3.hdf5"
    if not p.is_file():
        return None
    with h5py.File(str(p), "r") as h:
        if "fc3" in h:
            return np.asarray(h["fc3"][()], float)
    return None


def _fc3_load_bytes(out):
    """Byte count of the fc3 array if fully materialised (no load performed).

    pheasy and phono3py both write a dense (N,N,N,3,3,3) array (gzip on disk),
    which is ~8*N^3*27 bytes once read -- ~29 GB for a 512-atom supercell.
    """
    import h5py
    p = out / "fc3.hdf5"
    if not p.is_file():
        return None
    with h5py.File(str(p), "r") as h:
        if "fc3" in h:
            d = h["fc3"]
            return int(d.size) * int(d.dtype.itemsize)
    return None


def _wrap_scaled_positions(atoms, eps=1e-9):
    """Wrap atoms into the cell; hiphive's ShengBTE writer wants strictly
    in-cell fractional positions (0 <= s < 1)."""
    fr = atoms.get_scaled_positions(wrap=True)
    fr = np.where(fr >= 1.0 - eps, 0.0, fr)
    fr = np.where(fr < eps, 0.0, fr)
    atoms.set_scaled_positions(fr)
    return atoms


def _export_shengbte(cfg, out):
    """Export fc2/fc3 to the ShengBTE text formats.

    pheasy already writes FORCE_CONSTANTS_2ND / FORCE_CONSTANTS_3RD from its
    compact cluster representation (no dense fc3 materialisation), so those
    native files are reused directly.  phono3py / hiphive go through the
    hiphive writer, guarded by FC3_LOAD_GB_LIMIT.
    """
    want = _truthy(cfg.get("export_shengbte"), True)
    if not want:
        print("[..] EXPORT_SHENGBTE=false -- skipping", flush=True)
        return None
    sbdir = out / SB_SUB
    sbdir.mkdir(exist_ok=True)
    f2, f3 = sbdir / "FORCE_CONSTANTS_2ND", sbdir / "FORCE_CONSTANTS_3RD"

    # 1) pheasy writes these directly from compact IFCs.  Require the complete
    #    set for the requested order; never reuse a partial/stale file from
    #    another engine or from a failed previous fit.
    engine = str(cfg.get("engine") or "").strip().lower()
    n2, n3 = out / "FORCE_CONSTANTS_2ND", out / "FORCE_CONSTANTS_3RD"
    enable = int(cfg.get("enable_fc") or 3)
    native_ok = (engine == "pheasy" and n2.is_file() and
                 (enable < 3 or n3.is_file()))
    if native_ok:
        shutil.copyfile(str(n2), str(f2))
        if enable >= 3:
            shutil.copyfile(str(n3), str(f3))
        try:
            uc = _read_poscar(out / "POSCAR")
            _wrap_scaled_positions(uc)
            from ase.io import write as ase_write
            ase_write(str(sbdir / "POSCAR"), uc, format="vasp", direct=True,
                      sort=False)
        except Exception as e:
            print("[WARN] could not write shengbte/POSCAR: %s" % e, flush=True)
            return False
        ok = f2.is_file() and (enable < 3 or f3.is_file())
        print("[%s] ShengBTE export reused native FORCE_CONSTANTS_2ND/_3RD "
              "(written from compact IFCs)" % ("OK" if ok else "WARN"), flush=True)
        return ok

    if f2.is_file() and f3.is_file():
        print("[OK] shengbte/ already written by the hiphive engine", flush=True)
        return True
    try:
        from hiphive import ForceConstants
    except Exception as e:
        print("[WARN] hiphive unavailable -- skipping the ShengBTE export: %s" % e,
              flush=True)
        return False
    try:
        uc = _read_poscar(out / "POSCAR")
        sc = _read_poscar(out / "SPOSCAR")
        fc2 = _read_fc2_any(out)
        fc3 = None
        _fc3_bytes = _fc3_load_bytes(out)
        _fc3_limit = float(cfg.get("fc3_load_gb_limit") or 8.0) * 1e9
        if _fc3_bytes is not None and _fc3_bytes > _fc3_limit:
            print("[WARN] fc3.hdf5 would need %.1f GB in memory (limit %.1f GB); "
                  "skipping the ShengBTE fc3 export.  Raise FC3_LOAD_GB_LIMIT if "
                  "this node has the memory." % (_fc3_bytes / 1e9, _fc3_limit / 1e9),
                  flush=True)
        else:
            fc3 = _read_fc3_any(out)
        if fc2.shape[0] != len(sc):
            print("[WARN] fc2 is not a full supercell array (%s vs %d atoms) -- "
                  "ShengBTE export needs full force constants; skipping"
                  % (fc2.shape, len(sc)), flush=True)
            return False
        arrays = {"fc2_array": fc2}
        if fc3 is not None:
            arrays["fc3_array"] = fc3
        fcs = ForceConstants.from_arrays(sc, **arrays)

        _wrap_scaled_positions(uc)
        try:
            _wrap_scaled_positions(fcs._supercell)
        except Exception:
            pass
        fcs.write_to_phonopy(str(f2), format="text")
        if fc3 is not None:
            fcs.write_to_shengBTE(str(f3), uc)
        from ase.io import write as ase_write
        ase_write(str(sbdir / "POSCAR"), uc, format="vasp", direct=True, sort=False)
        ok = f2.is_file() and (f3.is_file() if fc3 is not None else True)
        print("[%s] ShengBTE export %s" % ("OK" if ok else "WARN",
                                           "done" if ok else "incomplete"), flush=True)
        return ok
    except Exception as e:
        print("[WARN] ShengBTE export failed (the phono3py artifacts are unaffected): "
              "%s" % e, flush=True)
        return False


def _parse_min_freq(band_yaml):
    p = Path(band_yaml)
    if not p.is_file():
        return None
    fr = []
    for ln in p.read_text(errors="ignore").splitlines():
        m = re.match(r"\s*frequency:\s*([-+]?\d*\.?\d+(?:[eEdD][-+]?\d+)?)\s*$", ln)
        if m:
            try:
                fr.append(float(m.group(1).replace("d", "e").replace("D", "e")))
            except ValueError:
                pass
    return min(fr) if fr else None


def _cells_cfg(cfg, out):
    """(supercell_matrix, primitive_matrix) for the phonon machinery.

    fc_dataset.json wins: prep resolves the supercell there (from the dataset
    YAML when it records one, otherwise from the POSCAR/SPOSCAR edge ratio), so
    fit_config.json may legitimately hold no matrix at all.  Returns
    (None, None) when neither source knows it."""
    scm = pm = None
    p = out / "fc_dataset.json"
    if p.is_file():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            d = {}
        scm, pm = d.get("supercell_matrix"), d.get("primitive_matrix")
    if scm is None:
        scm = cfg.get("supercell_matrix")
    if pm is None:
        pm = cfg.get("primitive_matrix")
    if not scm:
        return None, None
    return (np.asarray(scm, float),
            None if pm is None else np.asarray(pm, float))


def _build_phonopy(scm, pm, uc, fc2):
    """Phonopy object for the stability gate / band structure."""
    from phonopy import Phonopy
    ph = Phonopy(uc, supercell_matrix=np.asarray(scm, float),
                 primitive_matrix=pm)
    ph.force_constants = fc2
    return ph


def _apply_nac(out, ph):
    born = out / "BORN"
    if not born.is_file():
        return False
    try:
        from phonopy.file_IO import parse_BORN
        nac = parse_BORN(ph.primitive, filename=str(born))
        if isinstance(nac, dict) and not nac.get("factor"):
            nac["factor"] = 14.399652
        ph.nac_params = nac
        return True
    except Exception as e:
        print("[WARN] could not read BORN (%s) -- running without NAC" % e, flush=True)
        return False


def _stability_gate(cfg, out):
    """q-mesh minimum frequency.  2D systems use the no-NAC verdict (the 3D
    Coulomb kernel produces spurious imaginary frequencies near Gamma in a
    strictly 2D material), matching kl-dft-cpu."""
    dim = str(cfg.get("dim") or "3d").lower()
    is2d = dim.startswith("2")

    def _bad(note):
        return {"tool_ok": False, "stable": False, "min_freq": None,
                "min_freq_nonac": None, "min_freq_nac": None, "nac_used": False,
                "is_2d": is2d, "status": "tool_error", "note": note}

    scm, pm = _cells_cfg(cfg, out)
    if scm is None:
        return _bad("no supercell matrix in fc_dataset.json or fit_config.json")
    try:
        fc2 = _read_fc2_any(out)
        uc = _phonopy_unitcell(out / "POSCAR")
    except Exception as e:
        return _bad("could not prepare the phonopy input: %s" % e)

    def _minfreq(with_nac):
        p = _build_phonopy(scm, pm, uc, fc2)
        got = _apply_nac(out, p) if with_nac else False
        p.run_mesh(mesh=60.0, with_eigenvectors=False, is_mesh_symmetry=True)
        return float(np.min(p.get_mesh_dict()["frequencies"])), p, got

    try:
        mf_nonac, ph_nonac, _ = _minfreq(False)
    except Exception as e:
        return _bad("phonopy mesh (no NAC) failed: %s" % e)

    mf_nac = None
    if (out / "BORN").is_file():
        try:
            mf_nac, _, _ = _minfreq(True)
        except Exception as e:
            print("[WARN] mesh with NAC failed, using the no-NAC verdict: %s" % e,
                  flush=True)

    try:
        ph_nonac.auto_band_structure(plot=False, write_yaml=True,
                                     npoints=int(cfg.get("band_points") or 51),
                                     filename=str(out / "band-dft-cpu.yaml"))
        bf = _parse_min_freq(out / "band-dft-cpu.yaml")
        if bf is not None:
            mf_nonac = min(mf_nonac, bf)
    except Exception as e:
        print("[..] band structure skipped (does not affect the verdict): %s" % e,
              flush=True)

    if is2d or mf_nac is None:
        mf_used, nac_used = mf_nonac, False
    else:
        mf_used, nac_used = mf_nac, True
    thr = float(cfg.get("imag_thr", 0.10))
    stable = mf_used >= -thr
    parts = ["min_freq(no-NAC)=%.3f THz" % mf_nonac]
    if mf_nac is not None:
        parts.append("min_freq(NAC)=%.3f THz" % mf_nac)
    parts.append("verdict uses %s, threshold -%.2f THz"
                 % ("no-NAC" if not nac_used else "NAC", thr))
    note = "; ".join(parts) + " -> " + ("no significant imaginary frequency"
                                       if stable else "imaginary frequency present")
    return {"tool_ok": True, "stable": stable, "min_freq": mf_used,
            "min_freq_nonac": mf_nonac, "min_freq_nac": mf_nac, "nac_used": nac_used,
            "is_2d": is2d, "status": "stable" if stable else "imaginary", "note": note}


def _fit_rmse(cfg, out):
    """Engine-independent training residual: predict the forces of a few frames
    from the fitted force constants and compare with the dataset."""
    n = int(cfg.get("fit_rmse_frames") or 0)
    if n <= 0:
        return {}
    try:
        from hiphive import ForceConstants
        from hiphive.calculators import ForceConstantCalculator
        disps, forces = _load_dataset(out)
        if len(disps) == 0:
            return {}
        # A pheasy run leaves both SPOSCAR and fc2.hdf5 in pheasy's own atom
        # order, while dataset_*.npy stays in the dataset's order.  Put the
        # arrays into pheasy's order as well, otherwise this check compares
        # forces atom by atom across two different orderings.
        if str(cfg.get("engine") or "").lower() == "pheasy":
            perm = _recorded_pheasy_perm(out)
            if perm is not None:
                disps, forces = disps[:, perm, :], forces[:, perm, :]
        idx = np.linspace(0, len(disps) - 1, min(n, len(disps))).round().astype(int)
        sc = _read_poscar(out / "SPOSCAR")
        fc2 = _read_fc2_any(out)
        fc3 = None
        _fc3_bytes = _fc3_load_bytes(out)
        _fc3_limit = float(cfg.get("fc3_load_gb_limit") or 8.0) * 1e9
        if _fc3_bytes is not None and _fc3_bytes > _fc3_limit:
            print("[..] fc3.hdf5 too large to materialise (%.1f GB > %.1f GB); "
                  "evaluating the fc2-only residual" % (_fc3_bytes / 1e9, _fc3_limit / 1e9),
                  flush=True)
        else:
            fc3 = _read_fc3_any(out)
        arrays = {"fc2_array": fc2}
        if fc3 is not None:
            arrays["fc3_array"] = fc3
        fcs = ForceConstants.from_arrays(sc, **arrays)
        calc = ForceConstantCalculator(fcs)
        se = ss = 0.0
        cnt = 0
        for i in idx:
            a = sc.copy()
            a.positions += disps[i]
            a.calc = calc
            p = a.get_forces()
            se += float(((p - forces[i]) ** 2).sum())
            ss += float((forces[i] ** 2).sum())
            cnt += p.size
        rmse = float(np.sqrt(se / max(cnt, 1)))
        rel = float(np.sqrt(se / ss)) if ss > 0 else None
        print("[OK] fit residual over %d frame(s): RMSE %.5f eV/A (relative %.4f)"
              % (len(idx), rmse, rel if rel is not None else float("nan")), flush=True)
        return {"fit_rmse_eV_per_A": rmse, "fit_rmse_relative": rel,
                "fit_rmse_frames_used": int(len(idx))}
    except Exception as e:
        print("[..] fit-residual evaluation skipped: %s" % e, flush=True)
        return {}


def cmd_post(cfg, out):
    # prep records the dataset audit; fit_config.json only carries the request.
    ds = {}
    p = out / "fc_dataset.json"
    if p.is_file():
        try:
            ds = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            ds = {}
    cfg = dict(cfg)
    for k, src_key in (("n_frames", "n_frames"), ("natom_super", "natom_super")):
        if ds.get(src_key) is not None:
            cfg[k] = ds[src_key]
    sb_ok = _export_shengbte(cfg, out)
    rmse = _fit_rmse(cfg, out)
    g = _stability_gate(cfg, out)
    stable, tool_ok = g["stable"], g["tool_ok"]
    print("[%s] %s" % ("OK" if stable else "FAIL", g["note"]), flush=True)

    gate_fail = (out / ".fit_gate_fail").is_file()
    if gate_fail:
        stable = False
        print("[FAIL] a fit-quality gate flagged this run -- see the log above",
              flush=True)

    summary = {
        "FIT_DONE": True,
        "engine": cfg.get("engine"),
        "fit_method": {"phono3py": cfg.get("fc_calc"),
                       "pheasy": cfg.get("pheasy_method"),
                       "hiphive": cfg.get("hiphive_fit_method")}.get(
                           str(cfg.get("engine") or "").lower()),
        "enable_fc": cfg.get("enable_fc"),
        "n_frames": cfg.get("n_frames"),
        "natom_super": cfg.get("natom_super"),
        "stable": bool(stable),
        "status": g["status"],
        "imaginary_frequency": bool(tool_ok and not stable),
        "min_frequency_THz": g["min_freq"],
        "min_freq_nonac_THz": g["min_freq_nonac"],
        "min_freq_nac_THz": g["min_freq_nac"],
        "nac_used_for_verdict": g["nac_used"],
        "is_2d": g["is_2d"],
        "shengbte_export": sb_ok,
        "fit_quality_gate_failed": bool(gate_fail),
        "tool_ok": tool_ok,
        "note": g["note"],
    }
    summary.update(rmse)
    try:
        summary.update(json.loads((out / "fit_metrics.json").read_text(encoding="utf-8")))
    except Exception:
        pass
    text = json.dumps(summary, indent=2, ensure_ascii=False)
    # phonon_summary.json is what tf's built-in "phonon" judge reads for this
    # step (tool_ok / stable / min_frequency_THz, giving the three-way
    # stable | imaginary | error verdict); fc_fit_summary.json is the skill's
    # own report and carries the same content.
    for name in ("phonon_summary.json", "fc_fit_summary.json"):
        (out / name).write_text(text, encoding="utf-8", newline="\n")
    print("[DONE] post: phonon_summary.json + fc_fit_summary.json ready "
          "(stable=%s, status=%s)" % (str(stable).lower(), g["status"]), flush=True)

    # A tool error means the gate itself could not run: fail the job so tf marks
    # the step as error.  A genuine imaginary frequency exits 0 on purpose --
    # the marker is simply unsatisfied and downstream steps stay held back.
    if not tool_ok:
        sys.exit("[ERROR] stability gate could not be evaluated -- see the log above")


# ==========================================================================
COMMANDS = {"prep": cmd_prep, "fit": cmd_fit, "post": cmd_post}


def main():
    if len(sys.argv) < 3 or sys.argv[1] not in COMMANDS:
        sys.exit("usage: fc_fit_driver.py <%s> fit_config.json"
                 % "|".join(COMMANDS))
    cfg = load_cfg(sys.argv[2])
    out = Path.cwd()
    COMMANDS[sys.argv[1]](cfg, out)


if __name__ == "__main__":
    main()
