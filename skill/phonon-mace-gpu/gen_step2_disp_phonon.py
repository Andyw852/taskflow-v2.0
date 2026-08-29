#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_step2_disp_phonon.py —— 超胞 + 随机位移帧数 + submit.sh（phonon-mace-cpu S2）。

只算 2 阶（声子）：帧数 N 用 ALM 数 2 阶自由力常数反推
（N = max(10, ceil(nfree_fc2/(3*N_sc)) × OVERSAMPLE)），位移振幅 0.01 Å。
计算节点作业里由 mace_forces_phonon.py 生成位移 + 取力。
"""
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import klmace_common as kc
from dim_common import require_dim  # noqa: E402
import stepconf

OUTDIR = "step2_disp_force"
STEP = "step2_disp_force"
PREV = ["step1_mace_relax"]

SPEC = {
    "MACE_MODEL": ("mace-mp:medium", "str"),
    "MACE_MODEL_DIR": ("", "str"),
    "DEVICE": ("cpu", "str"),
    "DTYPE": ("float64", "str"),
    "CONDA_SH": (kc.DEFAULT_CONDA_SH, "str"),
    "CONDA_ENV": (kc.DEFAULT_CONDA_ENV, "str"),
    "SUPERCELL": (None, "str"),
    "MIN_SC_LEN": (15.0, "float"),
    "MAX_MULTIPLE": (6, "int"),
    "N_DISP": ("auto", "str"),        # auto=按 ALM 2 阶反推；整数=固定
    "OVERSAMPLE": (3, "int"),
    "DISP_DISTANCE": (0.01, "float"),  # MC-rattle 目标位移 RMS(Å)
    "MC_DMIN_SCALE": (0.85, "float"),  # d_min = 最近邻 × 此系数
    "MC_NITER": (10, "int"),           # MC 迭代数
    "RANDOM_SEED": (2025, "int"),
}


# ALM 2 阶自由力常数 → 随机位移帧数（在 venv 里跑，需要 alm/ase/phonopy）
_ALM_N2 = r'''import sys, math
import numpy as np
from phonopy.interface.calculator import read_crystal_structure
from phonopy import Phonopy
from ase import Atoms
from alm import ALM

poscar, reps_s, ov_s = sys.argv[1:4]
reps = [int(x) for x in reps_s.split()]
oversample = int(ov_s)
cell, _ = read_crystal_structure(poscar, interface_mode="vasp")
ph = Phonopy(cell, supercell_matrix=np.diag(np.array(reps, dtype=int)))
sc = ph.supercell
atoms = Atoms(numbers=sc.numbers, positions=sc.positions, cell=sc.cell, pbc=True)
n_sc = len(atoms)
nkd = len(set(atoms.numbers))
cut = np.full((1, nkd, nkd), -1.0, dtype=float)   # 2 阶不截断
with ALM(np.array(atoms.cell), atoms.get_scaled_positions(),
         atoms.get_atomic_numbers(), verbosity=0) as a:
    a.define(1, cutoff_radii=cut)                  # maxorder=1 → 只到 2 阶
    a.suggest()
    nfree = int(a._get_number_of_irred_fc_elements(1))   # 1=二阶
dof = 3 * n_sc
n_struct = max(10, math.ceil(nfree / dof) * oversample)
print("N_DISP %d" % n_struct)
print("NFREE_FC2 %d DOF %d N_SC %d OVERSAMPLE %d" % (nfree, dof, n_sc, oversample))
'''


def resolve_ndisp(conf, out, reps):
    val = str(conf["N_DISP"] or "").strip()
    if val.lower() not in ("", "auto", "none"):
        try:
            return int(val)
        except ValueError:
            sys.exit("[ERROR] N_DISP=%r 不是整数也不是 auto" % val)
    (out / "_alm_n2.py").write_text(_ALM_N2, encoding="utf-8")
    cmd = ("python _alm_n2.py POSCAR '%s' %d"
           % (kc.dim_str(reps), int(conf["OVERSAMPLE"])))
    rc, so = kc.run_capture(cmd, out, conf)
    for ln in (so or "").splitlines():
        if ln.startswith("N_DISP "):
            try:
                return int(ln.split()[1])
            except (ValueError, IndexError):
                pass
    tail = (so or "").strip()[-600:]
    sys.exit("[ERROR] ALM 2 阶反推帧数失败（rc=%d）。%s"
             % (rc, ("stdout 尾部：%s" % tail) if tail else "无输出，查 venv 里 alm/phonopy"))


def main():
    cwd = Path.cwd()
    out = cwd / OUTDIR
    out.mkdir(exist_ok=True)
    conf = stepconf.load(SPEC, STEP)

    prev = kc.find_prev_dir(cwd, PREV)
    if prev is None:
        sys.exit("[ERROR] 找不到 step1_mace_relax 的结构")
    kc.relay_poscar(prev / "CONTCAR", out / "POSCAR", "step1_mace_relax")
    for f in (kc.METHOD_FILE,):
        if (prev / f).is_file():
            shutil.copyfile(str(prev / f), str(out / f))

    meth = kc.read_method(out / kc.METHOD_FILE)
    dim = (meth.get("DIM", "").lower() or kc.resolve_dim(out / "POSCAR")[0])
    _, vac_axis = kc.resolve_dim(out / "POSCAR", dim)
    ax = vac_axis if vac_axis is not None else 2
    require_dim(dim, ("2d", "3d"), "step2_disp_force", why="声子需要周期性")

    reps = (kc.parse_reps(conf["SUPERCELL"], dim, ax) if conf["SUPERCELL"]
            else kc.supercell_matrix(out / "POSCAR", dim, conf["MIN_SC_LEN"],
                                     conf["MAX_MULTIPLE"], ax))
    n_disp = resolve_ndisp(conf, out, reps)
    print("[..] 维度=%s 超胞=%s 随机位移帧数=%d 振幅=%.3f Å"
          % (dim.upper(), kc.dim_str(reps), n_disp, conf["DISP_DISTANCE"]))

    kc.write_kl_params(out / kc.KL_PARAMS, DIM=dim.upper(),
                       SUPERCELL=kc.dim_str(reps), VAC_AXIS=ax)

    here = Path(__file__).resolve().parent
    for f in ("mace_forces_phonon.py", "mace_model.py", "mc_rattle.py"):
        if not (here / f).is_file():
            sys.exit("[ERROR] 缺 %s —— 本步 gen_need 里漏了它？" % f)
        shutil.copyfile(str(here / f), str(out / f))

    cmd = ("python mace_forces_phonon.py --model %s --model-dir '%s' "
           "--device %s --dtype %s --dim '%s' --ndisp %d --amplitude %g "
           "--dmin-scale %g --n-iter %d --seed %d"
           % (conf["MACE_MODEL"], conf["MACE_MODEL_DIR"] or "",
              conf["DEVICE"] or "cpu", conf["DTYPE"] or "float64",
              kc.dim_str(reps), n_disp, conf["DISP_DISTANCE"],
              conf["MC_DMIN_SCALE"], conf["MC_NITER"], conf["RANDOM_SEED"]))

    tpl = kc.resolve_submit(here, "submit_mace_relax")
    kc.write_submit(tpl, out / "submit.sh",
                    {"JOBNAME": kc.new_jobname(cwd, "S2force"),
                     "CONDA_SH": conf["CONDA_SH"] or kc.DEFAULT_CONDA_SH,
                     "CONDA_ENV": conf["CONDA_ENV"] or kc.DEFAULT_CONDA_ENV,
                     "MACE_CMD": cmd,
                     "LOG": "mace_forces.log"})
    stepconf.apply_submit(out / "submit.sh", conf.submit)
    print("[DONE] %s：%d 帧待取力，submit.sh 就绪" % (OUTDIR, n_disp))


if __name__ == "__main__":
    main()
