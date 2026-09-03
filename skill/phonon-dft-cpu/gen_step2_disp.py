#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_step2_disp.py —— phonopy 2 阶对称有限位移 + 超胞单点取力（扇出 disp-*）。

从 step1 弛豫结构接力，扩超胞，phonopy 生成 2 阶位移超胞，每个位移一个
disp-NNNNN 子目录单点取力。产出 phonopy_disp.yaml + disp-00001..N。
"""
import glob
import os
import shutil
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kl_common as kc
from dim_common import require_dim
import stepconf

OUTDIR = "step2_disp"
STEP   = "step2_disp"
PREV   = ["step1_std_opt"]

SPEC = {
    "FUNC":         ("pbesol", "str"),
    "KSPACING":     ("0.04",  "str"),
    "KSCHEME":      ("2",     "str"),
    "ENCUT":        (None,    "int"),
    "ENCUT_FACTOR": (1.5,     "float"),
    "VASPKIT_EXE":  ("vaspkit", "str"),
    "SUPERCELL":    (None,    "words"),
    "MIN_SC_LEN":   (12.0,    "float"),
    "MAX_MULTIPLE": (6,       "int"),
    "FD_DISTANCE":  (0.01,    "float"),
    "PHONON_MESH":  ("20 20 20", "str"),
    "MAX_DISP":     (200,     "int"),
}


def main():
    cwd = Path.cwd()
    out = cwd / OUTDIR
    out.mkdir(exist_ok=True)
    conf = stepconf.load(SPEC, STEP)

    prev = kc.find_prev_dir(cwd, PREV)
    if prev is None:
        sys.exit("[ERROR] 找不到 step1_std_opt 的结构")
    kc.relay_poscar(prev / "CONTCAR", out / "POSCAR", "step1_std_opt")

    meth = kc.read_method(prev / kc.METHOD_FILE)
    dim = (meth.get("DIM", "").lower() or kc.resolve_dim(out / "POSCAR")[0])
    _, vac_axis = kc.resolve_dim(out / "POSCAR", dim)
    require_dim(dim, ("2d", "3d"), "step2_disp", why="声子谱需要周期性边界")
    func = (conf["FUNC"] if conf["FUNC"] not in (None, "", "auto")
            else meth.get("FUNC", "pbesol").lower())

    if conf["SUPERCELL"]:
        reps = [int(x) for x in conf["SUPERCELL"]]
        if dim == "2d":
            reps[vac_axis if vac_axis is not None else 2] = 1
    else:
        reps = kc.supercell_matrix(out / "POSCAR", dim, conf["MIN_SC_LEN"],
                                   conf["MAX_MULTIPLE"],
                                   vac_axis if vac_axis is not None else 2)
    mesh = kc.mesh_str(conf["PHONON_MESH"].split(), dim,
                       vac_axis if vac_axis is not None else 2)
    print("[..] 维度=%s 超胞=%s mesh=%s FUNC=%s" % (dim.upper(), kc.dim_str(reps), mesh, func))
    kc.write_kl_params(out / kc.KL_PARAMS, DIM=dim.upper(), SUPERCELL=kc.dim_str(reps),
                       MESH=mesh, FUNC=func, FD_DISTANCE=conf["FD_DISTANCE"])

    if (out / "phonopy_disp.yaml").is_file() and glob.glob(str(out / "POSCAR-*")):
        print("[..] 已有位移，跳过生成（幂等）")
    else:
        try:
            from phonopy import Phonopy
            from phonopy.interface.vasp import read_vasp
        except Exception as e:
            sys.exit("[ERROR] 无法 import phonopy（%s）—— step2 需要 phonopy 环境" % e)
        cell = read_vasp(str(out / "POSCAR"))
        ph = Phonopy(cell, supercell_matrix=np.diag(np.array(reps, dtype=int)),
                     primitive_matrix="auto")
        ph.generate_displacements(distance=float(conf["FD_DISTANCE"]))
        n = len(ph.supercells_with_displacements)
        if n > int(conf["MAX_DISP"]):
            sys.exit("[ERROR] 位移帧数 %d > MAX_DISP=%d" % (n, int(conf["MAX_DISP"])))
        from phonopy.interface.vasp import write_vasp
        write_vasp(str(out / "SPOSCAR"), ph.supercell, direct=True)
        for i, s in enumerate(ph.supercells_with_displacements, 1):
            write_vasp(str(out / ("POSCAR-%05d" % i)), s, direct=True)
        ph.save(filename=str(out / "phonopy_disp.yaml"))
        print("[OK] phonopy 2 阶位移：%d 帧（FD_DISTANCE=%s）" % (n, conf["FD_DISTANCE"]))

    poscars = sorted(glob.glob(str(out / "POSCAR-*")))
    if not poscars:
        sys.exit("[ERROR] 没有产出 POSCAR-* 位移超胞")

    here = Path(__file__).resolve().parent
    incar_tpl = kc.resolve_submit(here, dim, "incar_force")
    submit_tpl = kc.resolve_submit(here, dim, "submit_std")
    encut = None
    for p in poscars:
        num = os.path.basename(p).split("-", 1)[1]
        d = out / ("disp-%s" % num)
        d.mkdir(exist_ok=True)
        if (d / "INCAR").is_file() and (d / "POSCAR").is_file():
            continue
        shutil.copyfile(p, d / "POSCAR")
        kc.vaspkit_kpoints(d, conf["KSCHEME"], conf["KSPACING"],
                           conf["VASPKIT_EXE"], dim, vac_axis)
        kc.vaspkit_potcar(d, conf["VASPKIT_EXE"])
        if encut is None:
            encut = conf["ENCUT"] or kc.encut_from_potcar(d / "POTCAR", conf["ENCUT_FACTOR"])
        kc.render_tpl(incar_tpl,
                      {"SYSTEM": "%s force %s" % (cwd.name, num),
                       "ENCUT": encut,
                       "GGA": kc.GGA_MAP.get(func, "PS"),
                       "VDW_LINE": ("IVDW = %s" % kc.VDW_MAP[func])
                                   if kc.VDW_MAP.get(func) else "# no vdW"},
                      d / "INCAR")
        kc.write_submit(submit_tpl, d / "submit.sh",
                        {"JOBNAME": "%s-phonon-dft-cpu-S2-%s" % (cwd.name, num)})
        stepconf.apply_submit(d / "submit.sh", conf.submit)
    print("[DONE] %s：%d 个位移子目录就绪（fanout disp-*）" % (OUTDIR, len(poscars)))


if __name__ == "__main__":
    main()
