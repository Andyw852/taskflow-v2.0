# -*- coding: utf-8 -*-
"""fc_common.py -- shared helpers for the fc-fit skill.

Deliberately tiny and dependency-free (standard library only) so the login-node
gen script runs under a plain system python.  The heavy lifting (dataset
parsing, fitting, gates) lives in fc_fit_driver.py, which runs on the compute
node inside the fitting job.

Placed next to the gen script by gen_need (the skill directory wins over the
common pool in tf's asset lookup chain).
"""
import math
import re
import sys
from pathlib import Path

# Cross-step bookkeeping files written by the phonon / thermal-conductivity
# skills; read for inheritance only (dimension, supercell, method).
METHOD_FILE = "workflow_method.txt"
KL_PARAMS = "kl_params.txt"


# ==========================================================================
# Simple KEY = VALUE files
# ==========================================================================
def read_keyval(path):
    """Read a KEY=VALUE file into an upper-cased dict (missing file -> {})."""
    d = {}
    p = Path(path)
    if not p.is_file():
        return d
    for ln in p.read_text(errors="ignore").splitlines():
        s = ln.strip()
        if s and not s.startswith("#") and "=" in s:
            k, v = s.split("=", 1)
            d[k.strip().upper()] = v.strip()
    return d


# ==========================================================================
# Structures (no numpy / ase on the login node)
# ==========================================================================
def poscar_lattice_lengths(path):
    """(|a1|, |a2|, |a3|) of a VASP POSCAR, scale factor applied.

    Stdlib-only: the gen script may run under a bare system python, and the
    only thing it needs from a structure is the edge-length ratio between
    POSCAR and SPOSCAR (to recover the supercell repetitions when the dataset
    ships no phonopy YAML)."""
    lines = Path(path).read_text(errors="ignore").splitlines()
    if len(lines) < 5:
        return None
    try:
        scale = [float(x) for x in lines[1].split()]
        lat = [[float(x) for x in lines[2 + i].split()[:3]] for i in range(3)]
    except (ValueError, IndexError):
        return None
    if len(scale) == 3:                      # VASP allows a per-axis scale
        lat = [[lat[i][k] * scale[k] for k in range(3)] for i in range(3)]
    elif scale:
        lat = [[x * scale[0] for x in v] for v in lat]
    return [math.sqrt(sum(c * c for c in v)) for v in lat]


def supercell_reps(uc_path, sc_path, tol=1e-3):
    """Diagonal supercell repetitions from the POSCAR/SPOSCAR edge ratio."""
    ru = poscar_lattice_lengths(uc_path)
    rs = poscar_lattice_lengths(sc_path)
    if not ru or not rs:
        return None
    reps = []
    for i in range(3):
        if ru[i] <= 0:
            return None
        ratio = rs[i] / ru[i]
        n = int(round(ratio))
        if n < 1 or abs(ratio - n) > tol:
            return None
        reps.append(n)
    return reps


# ==========================================================================
# Job naming / template rendering
# ==========================================================================
def new_jobname(cwd, step_label):
    return "%s-fc-fit-%s" % (Path(cwd).name, step_label)


def _strip_doc_placeholders(text):
    """Neutralise {{X}} tokens that appear in explanatory comments.

    A multi-line substitution landing inside a comment would be emitted before
    the first #SBATCH line, and SLURM stops parsing directives at the first
    executable statement -- partition / cpus / time / output would all be
    silently ignored.
    """
    out = []
    for ln in text.split("\n"):
        s = ln.lstrip()
        if s.startswith("#") and not s.startswith("#SBATCH") and "{{" in ln:
            ln = re.sub(r"\{\{(\w+)\}\}", r"\1", ln)
        out.append(ln)
    return "\n".join(out)


def _check_sbatch_order(path):
    """Every #SBATCH directive must precede the first executable statement."""
    lines = Path(path).read_text(encoding="utf-8").split("\n")
    first = None
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s and not s.startswith("#"):
            first = i
            break
    if first is None:
        return
    bad = [i + 1 for i, ln in enumerate(lines[first + 1:], start=first + 1)
           if ln.strip().startswith("#SBATCH")]
    if bad:
        sys.exit("[ERROR] %s: #SBATCH on line(s) %s appear after the first "
                 "executable statement (line %d); SLURM ignores them silently. "
                 "This usually means a multi-line placeholder leaked into a "
                 "header comment -- check the template."
                 % (Path(path).name, ",".join(str(b) for b in bad), first + 1))


def write_submit(tpl_path, out_path, subs):
    """Render a submit template ({{KEY}} substitution) and validate the result."""
    text = _strip_doc_placeholders(Path(tpl_path).read_text(encoding="utf-8"))
    for k, v in subs.items():
        text = text.replace("{{%s}}" % k, str(v))
    left = sorted(set(re.findall(r"\{\{([A-Z_]+)\}\}", text)))
    if left:
        sys.exit("[ERROR] template %s still has placeholders: %s"
                 % (Path(tpl_path).name, ", ".join(left)))
    Path(out_path).write_text(text, encoding="utf-8", newline="\n")
    _check_sbatch_order(out_path)
    print("[OK] %s" % Path(out_path).name)


def resolve_submit(base_dir, kind):
    """Locate <kind>.tpl either in <base_dir>/templates/ or next to the gen
    script (gen_need may have flattened the skill's template directory)."""
    base = Path(base_dir)
    for cand in (base / "templates" / ("%s.tpl" % kind),
                 base / ("%s.tpl" % kind),
                 base / "templates" / ("%s_step1_fit.tpl" % kind)):
        if cand.is_file():
            return cand
    sys.exit("[ERROR] cannot find the submit template %s.tpl (looked in %s and "
             "%s) -- is it listed in gen_need?" % (kind, base / "templates", base))
