#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_step1_fit.py -- prepare the force-constant fitting job (S1_fit).

Runs on the login node in the material's skill directory (the step's own
directory is ./step1_fit).  It does three things and nothing heavy:

  1. Locate and sanity-check the displacement+force dataset (FIT_INPUT_DIR, or
     an automatic search of the usual sibling step directories).
  2. Parse step.conf into step1_fit/fit_config.json -- the single input the
     compute-node driver reads.
  3. Copy fc_fit_driver.py into step1_fit/ and render the engine-specific
     submit.sh, then let stepconf apply the [submit] overrides.

The fitting itself happens in the submitted job (fc_fit_driver.py prep|fit|post).
"""
import glob
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fc_common as fc
import stepconf

OUTDIR = "step1_fit"
STEP = "step1_fit"

# Dataset directory search order, relative to the skill directory (the gen cwd).
# Sibling skills keep their step directories next to ours under the material
# directory, so the cross-skill entries are the common case.
AUTO_DIRS = (
    "step4_disp", "step2_disp_force", "step2_disp", "step3_disp",
    "../kl-dft-cpu/step4_disp",
    "../kl-mace-cpu/step2_disp_force",
    "../kl-mace-gpu/step2_disp_force",
    "../phonon-dft-cpu/step2_disp",
    "../phonon-mace-cpu/step2_disp_force",
    "../phonon-mace-gpu/step2_disp_force",
    ".", "..", "../..",
)

SIGNATURES = (
    ("phono3py_params", ("phono3py_params.yaml",)),
    ("phono3py_disp_forces", ("phono3py_disp.yaml", "FORCES_FC3")),
    ("arrays", ("dataset_disps.npy", "dataset_forces.npy")),
    ("pkl", ("disp_matrix.pkl", "force_matrix.pkl")),
)

SPEC = {
    # ---- dataset ----
    "FIT_INPUT_DIR": ("auto", "str"),       # dataset dir: auto | path (skill-dir or absolute)
    "SUBTRACT_EQUILIBRIUM": (True, "bool"),  # subtract the equilibrium residual forces
    "EQUILIBRIUM_FORCES_NPY": ("", "str"),   # optional explicit (natom,3) npy
    "COORDS": ("cartesian", "str"),          # cartesian | fractional (displacement input)
    "SUPERCELL": ("", "str"),                # 对角 "n n n"；或一般矩阵 9 个数
                                             # "n11 n12 n13 n21 n22 n23 n31 n32 n33"
                                             # （行主序，与 phonopy/phono3py --dim 同义）
    # ---- engine ----
    "FIT_ENGINE": ("phono3py", "str"),       # phono3py | pheasy | hiphive
    "ENABLE_FC": (3, "int"),                 # 2 | 3 (highest order to fit)
    "DIM": ("auto", "str"),                  # auto | 2d | 3d (NAC verdict only)
    # ---- phono3py ----
    "FC_CALC": ("symfc", "str"),             # symfc | alm
    "FC3_CUTOFF": ("", "str"),               # fc3 cutoff in A; empty = no cutoff
    # ---- pheasy ----
    "PHEASY_FIT_METHOD": ("RFE", "str"),     # OLS|LASSO|ALASSO|RFE|RFE-OLS-TSQR|RIDGE
    "PHEASY_BIN": ("pheasy", "str"),         # pheasy | pheasy-gpu
    "PHEASY_C2_CUTOFF": ("", "str"),         # fc2 cutoff in A; empty = none
    "PHEASY_C3_CUTOFF": ("", "str"),         # fc3 cutoff in A; empty = none
    "NULL_SPACE_EPS": (0.001, "float"),
    "PHEASY_RASR": ("BHH", "str"),           # BH | H | BHH | none
    "PHEASY_STD": (False, "bool"),           # standardise the training data
    # '' = auto: on when the resident GPU LASSO is requested (that backend only
    # accepts a TwoLevelSM, and pheasy builds a dense/CSR matrix for LASSO unless
    # this is on).  true/false force it.
    "PHEASY_LASSO_TWOLEVEL": ("", "str"),    # '' | true | false
    # How many GPUs the job gets (--gres=gpu:N) and how many pheasy splits the
    # two-level sparse matvec across (PHEASY_GPU_SM_NGPU + SM_DEVICES=0..N-1).
    # Empty = keep whatever the submit template says.  The two must agree, so the
    # gen writes gres from this value unless [submit] already sets one -- a job
    # that holds fewer cards than pheasy is told to use fails at runtime, and one
    # that holds more just wastes them.
    "PHEASY_NGPU": ("", "str"),
    # pheasy's resident GPU backend for LASSO/ALASSO ('' = whatever the submit
    # template exports).  It requires the two-level sensing matrix, which the
    # driver turns on automatically when this is true.
    "PHEASY_GPU_LASSO_RESIDENT": ("", "str"),   # '' | true | false
    # pheasy's cross-validation iteration budget.  The CV stops each alpha at
    # min(max_iter, 400) iterations (resident LASSO) with tol = max(tol, 1e-3);
    # at that cap the CV curve is unresolved and alpha can end up on the grid
    # boundary.  Empty = pheasy's own defaults.
    "PHEASY_CV_MAX_ITER": ("", "str"),       # e.g. 2000
    "PHEASY_CV_TOL": ("", "str"),            # e.g. 1e-4
    # safe = only memory/threading/CV-grouping knobs; kl = the production solver
    # settings from submit_fit_pheasy.tpl, whose PHEASY_OLS_RIDGE=1e-4 measurably
    # degraded the local test fit (58% vs 1.3% relative error).  See
    # fc_fit_driver._pheasy_env for the measurements.
    "PHEASY_TUNING": ("safe", "str"),        # safe | kl
    "PHEASY_OLS_RIDGE": ("", "str"),         # override the OLS ridge (e.g. 0)
    "PHEASY_OLS_MAXITER": ("", "str"),       # OLS LSMR iteration cap; empty = pheasy default (5000)
    # The LASSO grid and the RFE grid want different ranges; empty means "use
    # the method default" (see PHEASY_GRID_DEFAULTS), which reproduces the
    # values the pheasy author's own runs use.
    "PHEASY_CV": (5, "int"),
    "PHEASY_NMU": ("", "str"),
    # alpha exponents: integers for pheasy's argparse (see _int_str)
    "PHEASY_MU_MIN": (-8, "int"),
    "PHEASY_MU_MAX": ("", "str"),
    "PHEASY_MAX_ITER": ("", "str"),
    "PHEASY_TOL": ("", "str"),
    "PHEASY_SEED": (666666, "int"),
    # ---- hiphive ----
    "HIPHIVE_CUTOFF2": (6.0, "float"),       # fc2 cutoff in A (required)
    "HIPHIVE_CUTOFF3": (6.0, "float"),       # fc3 cutoff in A (ENABLE_FC=3)
    "HIPHIVE_FIT_METHOD": ("ridge", "str"),  # ols | ridge | lasso | ard | bayes
    "HIPHIVE_ALPHA": (1e-10, "float"),
    "HIPHIVE_SYMPREC": (1e-5, "float"),
    "HIPHIVE_ENFORCE_ASR": (True, "bool"),   # project rotational sum rules
    "HIPHIVE_N_CONFIGS": (0, "int"),         # 0 = use every frame
    # ---- output / gate ----
    "EXPORT_SHENGBTE": (True, "bool"),
    "FC3_LOAD_GB_LIMIT": (8.0, "float"),     # skip materialising fc3 above this (ShengBTE/RMSE)
    "BAND_POINTS": (51, "int"),
    "IMAG_THR": (0.10, "float"),             # imaginary-frequency threshold (THz)
    "FIT_RMSE_FRAMES": (0, "int"),           # 0 = off; else frames used for the residual
    # ---- environment ----
    "CONDA_SH": ("/public/home/wangchao/miniconda3/etc/profile.d/conda.sh", "str"),
    "CONDA_ENV": ("atomate2_p_a", "str"),
}

ENGINES = ("phono3py", "pheasy", "hiphive")
PHEASY_METHODS = ("OLS", "LASSO", "ALASSO", "RFE", "RFE-OLS-TSQR", "RIDGE")
HIPHIVE_METHODS = ("ols", "ridge", "lasso", "ard", "bayes")

# Per-method defaults for the regularisation grid: (nmu, tol, max_iter).  A
# sparser penalty grid is enough for RFE because it refits on a shrinking
# active set, while plain LASSO needs to resolve the alpha that minimises the
# CV error.  These reproduce the settings the pheasy author uses.
PHEASY_GRID_DEFAULTS = {
    "LASSO": (40, 1e-5, 100000),
    "ALASSO": (40, 1e-5, 100000),
    "RFE": (5, 1e-3, 1000),
    "RFE-OLS-TSQR": (5, 1e-3, 1000),
}
PHEASY_MU_MAX_DEFAULT = {2: 0, 3: -5}   # upper bound of the alpha grid

# pheasy's --mu_min/--mu_max/--nmu/--max_iter are argparse ints: "-8.0" is
# rejected outright ("invalid int value"), which is how the first LASSO run
# failed.  Keep them as plain integer strings all the way to the command line.
def _int_str(value, key):
    try:
        return str(int(str(value).strip()))
    except (TypeError, ValueError):
        sys.exit("[ERROR] %s must be an integer (alpha exponent), got %r"
                 % (key, value))


def pheasy_grid(conf, method, enable_fc):
    """Resolve nmu / tol / max_iter / mu_max, honouring explicit settings."""
    nmu_d, tol_d, mi_d = PHEASY_GRID_DEFAULTS.get(
        method, PHEASY_GRID_DEFAULTS["RFE" if method.startswith("RFE") else "LASSO"])

    def pick(key, default, cast):
        v = conf[key]
        return default if v in (None, "") else cast(v)
    mu_max = pick("PHEASY_MU_MAX", PHEASY_MU_MAX_DEFAULT.get(enable_fc, -5), int)
    mu_min = int(conf["PHEASY_MU_MIN"])
    nmu = pick("PHEASY_NMU", nmu_d, int)
    tol = pick("PHEASY_TOL", tol_d, float)
    max_iter = pick("PHEASY_MAX_ITER", mi_d, int)
    if mu_max <= mu_min:
        sys.exit("[ERROR] PHEASY_MU_MAX=%d must exceed PHEASY_MU_MIN=%d"
                 % (mu_max, mu_min))
    return nmu, tol, max_iter, mu_max


# --------------------------------------------------------------------------
def detect_signature(d):
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


def _yaml_read(path):
    try:
        import yaml
        return yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def supercell_diag(dataset_dir):
    """Diagonal supercell repetitions recorded in the dataset, if any."""
    for name in ("phono3py_params.yaml", "phono3py_disp.yaml"):
        p = Path(dataset_dir) / name
        if not p.is_file():
            continue
        y = _yaml_read(p)
        if not isinstance(y, dict):
            continue
        scm = y.get("supercell_matrix")
        if scm is None:
            continue
        try:
            rows = [[float(x) for x in row] for row in scm]
            if len(rows) == 3 and all(abs(rows[i][j]) < 1e-8
                                      for i in range(3) for j in range(3) if i != j):
                return " ".join(str(int(round(rows[i][i]))) for i in range(3)), rows
        except Exception:
            pass
    return None, None


def primitive_matrix(dataset_dir):
    """primitive_matrix recorded in the dataset YAML ('auto'/identity -> None).

    Phonopy/phono3py need it to rebuild the primitive cell from the unit cell;
    carrying it through keeps the gate and the plots faithful to the dataset."""
    for name in ("phono3py_params.yaml", "phono3py_disp.yaml"):
        p = Path(dataset_dir) / name
        if not p.is_file():
            continue
        y = _yaml_read(p)
        if not isinstance(y, dict) or y.get("primitive_matrix") is None:
            continue
        pm = y["primitive_matrix"]
        if isinstance(pm, str):
            return None if pm.strip().lower() in ("auto", "none", "") else None
        try:
            rows = [[float(x) for x in row] for row in pm]
        except Exception:
            return None
        if len(rows) != 3:
            return None
        if all(abs(rows[i][j] - (1.0 if i == j else 0.0)) < 1e-8
               for i in range(3) for j in range(3)):
            return None
        return rows
    return None


def resolve_dim(param, dataset_dir, material_dir):
    """DIM=auto -> inherit from a sibling stream's bookkeeping, else detect."""
    mode = str(param or "auto").lower()
    if mode in ("2d", "3d"):
        return mode
    for base in (dataset_dir, material_dir):
        for f in ("kl_params.txt", "workflow_method.txt", "klmace_params.txt"):
            p = Path(base) / f if base else None
            if p and p.is_file():
                for ln in p.read_text(errors="ignore").splitlines():
                    if ln.strip().upper().startswith("DIM"):
                        v = ln.split("=", 1)[-1].strip().lower()
                        if v in ("2d", "3d"):
                            print("[OK] DIM=%s inherited from %s" % (v, p))
                            return v
    poscar = Path(dataset_dir) / "POSCAR" if dataset_dir else None
    if poscar and poscar.is_file():
        try:
            import dim_common
            dim, axis, _ = dim_common.detect_dimension(str(poscar))
            print("[OK] DIM=%s detected from %s (vacuum axis %s)"
                  % (dim, poscar, axis))
            return dim
        except Exception as e:
            print("[..] dimensional detection unavailable (%s)" % e)
    return "3d"


def resolve_dataset(cfg_dir, step_dir, want):
    """Return (abs_path, signature) for the dataset directory."""
    want = str(want or "auto").strip()
    if want and want.lower() != "auto":
        p = Path(want).expanduser()
        if not p.is_absolute():
            p = (Path(cfg_dir) / p)
        if not p.is_dir():
            sys.exit("[ERROR] FIT_INPUT_DIR=%s is not a directory" % p)
        sig = detect_signature(p)
        if not sig:
            sys.exit("[ERROR] FIT_INPUT_DIR=%s holds no recognised displacement "
                     "dataset (looked for %s)"
                     % (p, ", ".join(s[1][0] for s in SIGNATURES)))
        return p.resolve(), sig
    # The fixed list covers the skills that exist today; the glob covers any
    # other sibling skill whose displacement step is named differently, so a
    # new producer does not need this file to be edited.
    cands = [Path(cfg_dir) / rel for rel in AUTO_DIRS]
    matdir = Path(cfg_dir).resolve().parent
    for pat in ("*/step*disp*", "*/step*force*", "*/step*fit*"):
        for hit in sorted(glob.glob(str(matdir / pat))):
            cands.append(Path(hit))
    seen, tried = set(), []
    for p in cands:
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        if not p.is_dir():
            continue
        sig = detect_signature(p)
        if sig:
            print("[OK] dataset auto-detected: %s (%s)" % (p.resolve(), sig))
            return p.resolve(), sig
        tried.append(str(p))
    sys.exit("[ERROR] no displacement+force dataset found.  Searched:\n  %s\n"
             "        Point FIT_INPUT_DIR at the dataset directory explicitly:\n"
             "        tf -tt fc-fit -p <material> -j step1_fit conf --set "
             "params.FIT_INPUT_DIR=../kl-dft-cpu/step4_disp"
             % "\n  ".join(tried))


def main():
    cwd = Path.cwd()
    out = cwd / OUTDIR
    out.mkdir(exist_ok=True)
    conf = stepconf.load(SPEC, STEP)

    engine = str(conf["FIT_ENGINE"] or "phono3py").lower()
    if engine not in ENGINES:
        sys.exit("[ERROR] FIT_ENGINE must be one of %s" % " | ".join(ENGINES))
    enable = int(conf["ENABLE_FC"] or 3)
    if enable not in (2, 3):
        sys.exit("[ERROR] ENABLE_FC must be 2 or 3 (this skill fits fc2/fc3)")
    p_bin = str(conf["PHEASY_BIN"] or "pheasy").lower()
    if p_bin not in ("pheasy", "pheasy-gpu"):
        sys.exit("[ERROR] PHEASY_BIN must be pheasy or pheasy-gpu")
    p_ngpu = str(conf["PHEASY_NGPU"] or "").strip()
    if p_ngpu:
        try:
            _n = int(p_ngpu)
        except ValueError:
            sys.exit("[ERROR] PHEASY_NGPU must be an integer (GPU count), got %r"
                     % p_ngpu)
        if _n < 1:
            sys.exit("[ERROR] PHEASY_NGPU must be >= 1, got %d" % _n)
        p_ngpu = str(_n)
    fc_calc = str(conf["FC_CALC"] or "symfc").lower()
    if fc_calc not in ("symfc", "alm"):
        sys.exit("[ERROR] FC_CALC must be symfc or alm")
    p_method = str(conf["PHEASY_FIT_METHOD"] or "RFE").upper()
    if engine == "pheasy" and p_method not in PHEASY_METHODS:
        sys.exit("[ERROR] PHEASY_FIT_METHOD must be one of %s"
                 % " | ".join(PHEASY_METHODS))
    h_method = str(conf["HIPHIVE_FIT_METHOD"] or "ridge").lower()
    if engine == "hiphive" and h_method not in HIPHIVE_METHODS:
        sys.exit("[ERROR] HIPHIVE_FIT_METHOD must be one of %s"
                 % " | ".join(HIPHIVE_METHODS))
    coords = str(conf["COORDS"] or "cartesian").lower()
    if coords not in ("cartesian", "fractional"):
        sys.exit("[ERROR] COORDS must be cartesian or fractional")

    src, sig = resolve_dataset(cwd, out, conf["FIT_INPUT_DIR"])

    # Random-displacement (type-2) datasets are required by the regression
    # engines; finite-difference datasets only carry enough information for the
    # phono3py rebuild.
    sc_str, sc_rows = supercell_diag(src)
    if not sc_str:
        # A dataset without a phonopy YAML (the npy / pkl layouts) still ships
        # POSCAR + SPOSCAR, so the repetitions are recoverable from the edge
        # ratio -- pheasy needs them explicitly.
        reps = fc.supercell_reps(src / "POSCAR", src / "SPOSCAR")
        if reps:
            sc_str = " ".join(str(x) for x in reps)
            sc_rows = [[float(reps[0]), 0, 0], [0, float(reps[1]), 0],
                       [0, 0, float(reps[2])]]
            print("[OK] supercell %s deduced from the POSCAR/SPOSCAR edge ratio"
                  % sc_str)
    if str(conf["SUPERCELL"] or "").strip():
        # 3 个数=对角扩胞；9 个数=3×3 矩阵（行主序，与 phono3py --dim 同义，
        # hiphive/pheasy 直接吃这个 3×3；phono3py 那一路按原样透传 --dim）
        _sc_vals = [int(x) for x in str(conf["SUPERCELL"]).split()]
        if len(_sc_vals) == 9:
            sc_str = " ".join(str(x) for x in _sc_vals)
            sc_rows = [[float(x) for x in _sc_vals[0:3]],
                       [float(x) for x in _sc_vals[3:6]],
                       [float(x) for x in _sc_vals[6:9]]]
        elif len(_sc_vals) == 3:
            sc_str = " ".join(str(x) for x in _sc_vals)
            sc_rows = [[float(_sc_vals[0]), 0.0, 0.0],
                       [0.0, float(_sc_vals[1]), 0.0],
                       [0.0, 0.0, float(_sc_vals[2])]]
        else:
            sys.exit("[ERROR] SUPERCELL 要写 3 个（对角，如 \"3 3 3\"）或 9 个"
                     "（3×3 矩阵，行主序，如 \"2 1 0 -1 2 0 0 0 1\"）整数，"
                     "收到 %r" % conf["SUPERCELL"])
    if engine in ("pheasy", "hiphive") and sig in ("vasprun", "phono3py_disp_forces"):
        print("[WARN] the dataset looks like a finite-displacement set; "
              "%s fits random-displacement (rattled) data.  The driver will stop "
              "unless the YAML actually carries a type-2 displacement array."
              % engine)
    if engine == "pheasy" and not sc_str:
        sys.exit("[ERROR] FIT_ENGINE=pheasy needs the supercell repetitions; set "
                 "SUPERCELL=\"n n n\" in step.conf (the dataset records none)")

    p_nmu, p_tol, p_max_iter, p_mu_max = pheasy_grid(conf, p_method, enable)
    dim = resolve_dim(conf["DIM"], src, src.parent)

    cfg = {
        "engine": engine,
        "dataset_dir": os.path.relpath(str(src), str(out)),
        "dataset_dir_abs": str(src),
        "dataset_signature": sig,
        "supercell": sc_str,
        "supercell_matrix": sc_rows,
        "primitive_matrix": primitive_matrix(src),
        "dim": dim,
        "enable_fc": enable,
        "subtract_equilibrium": bool(conf["SUBTRACT_EQUILIBRIUM"]),
        "equilibrium_forces_npy": str(conf["EQUILIBRIUM_FORCES_NPY"] or ""),
        "coords": coords,
        # phono3py
        "fc_calc": fc_calc,
        "fc3_cutoff": str(conf["FC3_CUTOFF"] or ""),
        # pheasy
        "pheasy_method": p_method,
        "pheasy_bin": p_bin,
        "pheasy_c2_cutoff": str(conf["PHEASY_C2_CUTOFF"] or ""),
        "pheasy_c3_cutoff": str(conf["PHEASY_C3_CUTOFF"] or ""),
        "null_space_eps": float(conf["NULL_SPACE_EPS"] or 0.001),
        "pheasy_rasr": str(conf["PHEASY_RASR"] or ""),
        "pheasy_std": bool(conf["PHEASY_STD"]),
        "pheasy_lasso_twolevel": str(conf["PHEASY_LASSO_TWOLEVEL"] or ""),
        "pheasy_ngpu": p_ngpu,
        "pheasy_gpu_lasso_resident": str(conf["PHEASY_GPU_LASSO_RESIDENT"] or ""),
        "pheasy_cv_max_iter": str(conf["PHEASY_CV_MAX_ITER"] or ""),
        "pheasy_cv_tol": str(conf["PHEASY_CV_TOL"] or ""),
        "pheasy_tuning": str(conf["PHEASY_TUNING"] or "safe"),
        "pheasy_ols_ridge": str(conf["PHEASY_OLS_RIDGE"] or ""),
        "pheasy_ols_maxiter": str(conf["PHEASY_OLS_MAXITER"] or ""),
        "pheasy_cv": int(conf["PHEASY_CV"] or 5),
        "pheasy_nmu": p_nmu,
        "pheasy_mu_min": int(conf["PHEASY_MU_MIN"]),
        "pheasy_mu_max": p_mu_max,
        "pheasy_max_iter": p_max_iter,
        "pheasy_tol": p_tol,
        "pheasy_seed": int(conf["PHEASY_SEED"]),
        # hiphive
        "hiphive_cutoff2": float(conf["HIPHIVE_CUTOFF2"]),
        "hiphive_cutoff3": float(conf["HIPHIVE_CUTOFF3"]),
        "hiphive_fit_method": h_method,
        "hiphive_alpha": float(conf["HIPHIVE_ALPHA"]),
        "hiphive_symprec": float(conf["HIPHIVE_SYMPREC"]),
        "hiphive_enforce_asr": bool(conf["HIPHIVE_ENFORCE_ASR"]),
        "hiphive_n_configs": int(conf["HIPHIVE_N_CONFIGS"] or 0),
        # output / gate
        "export_shengbte": bool(conf["EXPORT_SHENGBTE"]),
        "fc3_load_gb_limit": float(conf["FC3_LOAD_GB_LIMIT"] or 8.0),
        "band_points": int(conf["BAND_POINTS"]),
        "imag_thr": float(conf["IMAG_THR"]),
        "fit_rmse_frames": int(conf["FIT_RMSE_FRAMES"] or 0),
    }
    (out / "fit_config.json").write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8", newline="\n")

    # The driver runs on the compute node: gen_need only places it next to this
    # gen script, so copy it into the step directory explicitly.
    here = Path(__file__).resolve().parent
    if not (here / "fc_fit_driver.py").is_file():
        sys.exit("[ERROR] fc_fit_driver.py missing -- is it listed in gen_need?")
    shutil.copyfile(str(here / "fc_fit_driver.py"), str(out / "fc_fit_driver.py"))

    kind = "submit_fcfit_%s" % ("p3py" if engine == "phono3py" else engine)
    if engine == "pheasy" and p_bin == "pheasy-gpu":
        kind = "submit_fcfit_pheasy_gpu"
    try:
        tpl = fc.resolve_submit(here, kind)
    except SystemExit:
        if kind == "submit_fcfit_pheasy_gpu":
            sys.exit("[ERROR] PHEASY_BIN=pheasy-gpu needs the GPU template "
                     "submit_fcfit_pheasy_gpu.tpl.\n"
                     "        If this cluster has no GPU nodes, use "
                     "PHEASY_BIN=pheasy, or drop a cluster-specific copy into "
                     "setting/<hpc>/templates/.")
        raise
    subs = {
        "JOBNAME": fc.new_jobname(cwd, "S1fit"),
        "CONDA_SH": str(conf["CONDA_SH"] or ""),
        "CONDA_ENV": str(conf["CONDA_ENV"] or ""),
        "ENGINE": engine,
    }
    fc.write_submit(tpl, out / "submit.sh", subs)
    sub = dict(conf.submit or {})
    if p_ngpu and p_bin == "pheasy-gpu":
        # Keep --gres=gpu:N in step with the pheasy device count (an explicit
        # [submit] gres still wins).
        sub.setdefault("gres", "gpu:%d" % int(p_ngpu))
    stepconf.apply_submit(out / "submit.sh", sub)

    print("[..] engine=%s enable_fc=%d dim=%s supercell=%s"
          % (engine, enable, dim.upper(), sc_str or "?"))
    print("[DONE] %s: fit_config.json + submit.sh + fc_fit_driver.py ready.  "
          "tf submits the job; the compute node writes fc2.hdf5 (+ fc3.hdf5), "
          "shengbte/ and phonon_summary.json, which the built-in phonon judge "
          "reads (stable | imaginary frequency | tool error)." % OUTDIR)


if __name__ == "__main__":
    main()
