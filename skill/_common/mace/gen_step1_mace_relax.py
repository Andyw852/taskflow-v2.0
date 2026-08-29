#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_step1_mace_relax.py —— MACE 结构优化（step1_mace_relax），submit 模式。

本步只准备输入 + 写 submit.sh，MACE 弛豫放到计算节点作业里跑（批量材料秒级提交、
不阻塞登录节点）。作业跑完写 step1_mace_relax/{CONTCAR, relax_summary.json}，
workflow_method.txt（FUNC/DIM/MODEL/DTYPE）在 gen 时就写好，供 step2 接力。
判据：relax_summary.json 的 "converged": true。
"""
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import klmace_common as kc
from dim_common import require_dim  # noqa: E402
import stepconf

OUTDIR = "step1_mace_relax"
STEP = "step1_mace_relax"

SPEC = {
    # ---- 全局（templates/step.conf）----
    "MACE_MODEL": ("mace-mp:medium", "str"),
    "MACE_MODEL_DIR": ("", "str"),
    "DEVICE": ("auto", "str"),
    "DTYPE": ("float64", "str"),
    "CONDA_SH": (kc.DEFAULT_CONDA_SH, "str"),
    "CONDA_ENV": (kc.DEFAULT_CONDA_ENV, "str"),
    # ---- 本步 ----
    "DIMENSION": ("auto", "str"),
    "RELAX": (True, "bool"),
    "RELAX_CELL": (True, "bool"),
    "FMAX": (1e-4, "float"),
    "MAX_STEPS": (2000, "int"),
    "OPTIMIZER": ("FIRE", "str"),
    "FIX_SYMMETRY": (True, "bool"),
    "SYMPREC": (1e-4, "float"),
    "CELL_POLICY": ("primitive", "str"),
    "RESIDUAL_TOL": (2e-3, "float"),
}


def main():
    cwd = Path.cwd()
    out = cwd / OUTDIR
    out.mkdir(exist_ok=True)
    conf = stepconf.load(SPEC, STEP)

    src = cwd / "POSCAR"
    if not src.is_file():
        sys.exit("[ERROR] 材料目录下没有 POSCAR")
    shutil.copyfile(str(src), str(out / "POSCAR"))

    dim, vac_axis = kc.resolve_dim(out / "POSCAR", conf["DIMENSION"] or "auto")
    require_dim(dim, ("2d", "3d"), "step1_mace_relax",
                why="晶格热导需要声子群速度和布里渊区积分，孤立分子只有分立振动模式")
    print("[..] 维度=%s%s  模型=%s"
          % (dim.upper(), ("（真空轴 %d）" % vac_axis) if dim == "2d" else "",
             conf["MACE_MODEL"]))

    here = Path(__file__).resolve().parent
    for f in ("mace_relax.py", "mace_model.py"):
        if not (here / f).is_file():
            sys.exit("[ERROR] 缺 %s —— 本步 gen_need 里漏了它？" % f)
        shutil.copyfile(str(here / f), str(out / f))

    # workflow_method.txt 现在就写（DIM/MODEL/DTYPE 在 gen 时已知），step2 接力读它
    kc.write_method(out / kc.METHOD_FILE, FUNC="mace", DIM=dim.upper(),
                    MODEL=conf["MACE_MODEL"], DTYPE=conf["DTYPE"] or "float64")

    cmd = ("python mace_relax.py "
           "--model %s --model-dir '%s' --device %s --dtype %s "
           "--dim %s --vac-axis %d "
           "--relax %s --relax-cell %s --fmax %g --steps %d --opt %s "
           "--fix-symmetry %s --symprec %g --cell-policy %s --residual-tol %g"
           % (conf["MACE_MODEL"], conf["MACE_MODEL_DIR"] or "",
              conf["DEVICE"] or "auto", conf["DTYPE"] or "float64",
              dim, (vac_axis if vac_axis is not None else 2),
              str(bool(conf["RELAX"])).lower(), str(bool(conf["RELAX_CELL"])).lower(),
              conf["FMAX"], conf["MAX_STEPS"], conf["OPTIMIZER"] or "FIRE",
              str(bool(conf["FIX_SYMMETRY"])).lower(), conf["SYMPREC"],
              conf["CELL_POLICY"] or "primitive", conf["RESIDUAL_TOL"]))

    tpl = kc.resolve_submit(here, "submit_mace_relax")
    kc.write_submit(tpl, out / "submit.sh",
                    {"JOBNAME": kc.new_jobname(cwd, "S1relax"),
                     "CONDA_SH": conf["CONDA_SH"] or kc.DEFAULT_CONDA_SH,
                     "CONDA_ENV": conf["CONDA_ENV"] or kc.DEFAULT_CONDA_ENV,
                     "MACE_CMD": cmd,
                     "LOG": "mace_relax.log"})
    stepconf.apply_submit(out / "submit.sh", conf.submit)
    print("[DONE] %s：submit.sh 就绪（作业跑完写 relax_summary.json + CONTCAR）"
          % OUTDIR)


if __name__ == "__main__":
    main()
