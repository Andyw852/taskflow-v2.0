#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_step1_graph_data.py —— 生成 Uni-HamGNN 所需的 graph_data.npz（submit 模式）。

本步只准备输入 + 写 submit.sh，真正的计算在计算节点作业里跑：
  POSCAR -> OpenMX .dat -> openmx_postprocess(overlap.scfout) -> graph_data.npz

产出（作业跑完后）：
  step1_graph_data/graph_data_non_soc/graph_data.npz       （non-SOC）
  step1_graph_data/graph_data_soc/graph_data.npz           （SOC=true 时）
  step1_graph_data/graph_data_summary.json                 （完成标记）
判据：graph_data_summary.json 里 "GRAPH_DATA_DONE": true。
"""
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dim_common import require_dim, validate_poscar  # noqa: E402
import stepconf  # noqa: E402
import unihamgnn_common as uc  # noqa: E402

OUTDIR = "step1_graph_data"
STEP = "step1_graph_data"

NON_SOC_DIR = "non_soc"
SOC_DIR = "soc"
SAVE_NON_SOC = "graph_data_non_soc"
SAVE_SOC = "graph_data_soc"


def render_driver(openmx_pp, mpirun, nproc, soc, mkl_lib=""):
    mkl_export = ('export LD_LIBRARY_PATH="%s:${LD_LIBRARY_PATH:-}"' % mkl_lib
                  if mkl_lib else
                  "# MKL_LIB 未设置：openmx_postprocess 走系统默认库搜索路径")
    soc_block = ""
    if soc:
        soc_block = """
# ---------- SOC ----------
poscar2openmx --config poscar2openmx_soc.yaml
DAT=$(ls {soc}/openmx_*.dat 2>/dev/null | head -1)
[ -n "$DAT" ] && mv "$DAT" {soc}/openmx.dat
run_omp {soc}
graph_data_gen --config graph_data_gen_soc.yaml
""".format(soc=SOC_DIR)
    soc_summary = ("python write_step1_summary.py --soc"
                   if soc else "python write_step1_summary.py")
    return """#!/bin/bash
# run_graph_data.sh —— step1 计算节点驱动（在 conda 环境里由 submit.sh 调用）
set -euo pipefail
cd "$(dirname "$0")"
export CUDA_VISIBLE_DEVICES=""
{mkl_export}

# openmx_postprocess 算完会写 overlap.scfout，但个别体系 MPI 收尾会卡住不退出；
# 这里后台跑 + 轮询 scfout，拿到 scfout 就 SIGKILL 掉 mpirun 继续。
run_omp() {{
  local d="$1"
  cd "$d" || exit 1
  # openmx_postprocess 写完 scfout 后打印 "Finish calculating S & H0"，
  # 但个别体系 MPI 收尾卡住不退出；前台跑 + 硬超时，超时就 SIGKILL。
  timeout -s KILL 240 {mpirun} -np {nproc} {pp} openmx.dat > openmx.log 2>&1 || true
  grep -q "Finish calculating S & H0" openmx.log || {{
    echo "[ERROR] $d: openmx_postprocess 未完成（看 openmx.log）"
    exit 1
  }}
  cd ..
}}

# ---------- non-SOC ----------
poscar2openmx --config poscar2openmx.yaml
DAT=$(ls {non_soc}/openmx_*.dat 2>/dev/null | head -1)
[ -n "$DAT" ] && mv "$DAT" {non_soc}/openmx.dat
run_omp {non_soc}
graph_data_gen --config graph_data_gen.yaml
{soc_block}
# ---------- 完成标记 ----------
{soc_summary}
""".format(non_soc=NON_SOC_DIR, mpirun=mpirun, nproc=nproc, pp=openmx_pp,
           soc_block=soc_block, soc_summary=soc_summary, mkl_export=mkl_export)


def main():
    cwd = Path.cwd()
    out = cwd / OUTDIR
    out.mkdir(exist_ok=True)
    conf = stepconf.load(uc.COMMON_SPEC, STEP)

    src = cwd / "POSCAR"
    if not src.is_file():
        sys.exit("[ERROR] 材料目录下没有 POSCAR")
    bad = validate_poscar(src)
    if bad:
        sys.exit("[ERROR] POSCAR 不完整：%s" % bad)
    shutil.copyfile(str(src), str(out / "POSCAR"))

    dim, vac_axis = uc.resolve_dim(out / "POSCAR", conf["DIMENSION"] or "auto")
    require_dim(dim, ("2d", "3d"), STEP, why="能带需要周期性（孤立分子无倒空间路径）")
    print("[..] 维度=%s%s"
          % (dim.upper(), "（真空轴 %d）" % vac_axis if dim == "2d" else ""))

    here = Path(__file__).resolve().parent
    for f in ("poscar2openmx.tpl", "poscar2openmx_soc.tpl", "graph_data_gen.tpl"):
        if not (here / f).is_file():
            sys.exit("[ERROR] 缺模板 templates/%s —— 本步 gen_need 里漏了它？" % f)

    openmx_pp = uc.resolve_bin(conf, "OPENMX_POSTPROCESS",
                               "DFT_interfaces/openmx/openmx_postprocess/openmx_postprocess")
    read_openmx = uc.resolve_bin(conf, "READ_OPENMX",
                                 "DFT_interfaces/openmx/openmx_postprocess/read_openmx")
    dft_data = os.path.expanduser((conf["DFT_DATA"] or "").strip())
    data_path_line = ("DATA.PATH           %s" % dft_data) if dft_data else         "# DATA.PATH 未设置 —— 用 OpenMX 默认 ../DFT_DATA19"

    soc = bool(conf["SOC"])
    subs_common = {
        "SYSTEM_NAME": "openmx",
        "POSCAR_PATH": "./POSCAR",
        "DATA_PATH_LINE": data_path_line,
        "XC": conf["XC"],
        "ENERGY_CUTOFF": conf["ENERGY_CUTOFF"],
        "KGRID": conf["KGRID"],
        "ELECTRONIC_TEMP": conf["ELECTRONIC_TEMP"],
        "SCF_CRITERION": conf["SCF_CRITERION"],
        "MAX_SCF_ITER": conf["MAX_SCF_ITER"],
    }

    uc.render_tpl(here / "poscar2openmx.tpl",
                  dict(subs_common, FILEPATH="./%s" % NON_SOC_DIR),
                  out / "poscar2openmx.yaml")
    uc.render_tpl(here / "graph_data_gen.tpl",
                  {"NAO_MAX": conf["NAO_MAX"], "SAVE_PATH": SAVE_NON_SOC,
                   "READ_OPENMX": read_openmx, "SCFOUT_DIR": "./%s" % NON_SOC_DIR,
                   "SOC": "False"},
                  out / "graph_data_gen.yaml")

    if soc:
        uc.render_tpl(here / "poscar2openmx_soc.tpl",
                      dict(subs_common, FILEPATH="./%s" % SOC_DIR),
                      out / "poscar2openmx_soc.yaml")
        uc.render_tpl(here / "graph_data_gen.tpl",
                      {"NAO_MAX": conf["NAO_MAX"], "SAVE_PATH": SAVE_SOC,
                       "READ_OPENMX": read_openmx, "SCFOUT_DIR": "./%s" % SOC_DIR,
                       "SOC": "True"},
                      out / "graph_data_gen_soc.yaml")

    for f in ("write_step1_summary.py",):
        if not (here / f).is_file():
            sys.exit("[ERROR] 缺 %s —— 本步 gen_need 里漏了它？" % f)
        shutil.copyfile(str(here / f), str(out / f))

    driver = render_driver(openmx_pp, conf["MPIRUN"] or "mpirun", conf["NPROC"], soc,
                            conf["MKL_LIB"] or "")
    (out / "run_graph_data.sh").write_text(driver, encoding="utf-8")
    os.chmod(str(out / "run_graph_data.sh"), 0o755)

    tpl = uc.resolve_submit(here, "submit_hamgnn_mpi")
    uc.write_submit(tpl, out / "submit.sh", {
        "JOBNAME": uc.new_jobname(cwd, "S1graph"),
        "CONDA_SH": conf["CONDA_SH"] or uc.DEFAULT_CONDA_SH,
        "CONDA_ENV": conf["CONDA_ENV"] or uc.DEFAULT_CONDA_ENV,
        "NPROC": conf["NPROC"],
        "CMD": "bash run_graph_data.sh",
    })
    stepconf.apply_submit(out / "submit.sh", conf.submit)

    uc.write_method(out / uc.METHOD_FILE, FUNC="unihamgnn", DIM=dim.upper(),
                    SOC=("true" if soc else "false"), NAO_MAX=conf["NAO_MAX"])
    print("[DONE] %s：submit.sh 就绪（作业跑完写 graph_data_non_soc/graph_data.npz%s"
          " + graph_data_summary.json）"
          % (OUTDIR, " 与 graph_data_soc/graph_data.npz" if soc else ""))


if __name__ == "__main__":
    main()
