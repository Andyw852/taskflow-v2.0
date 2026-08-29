#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_step2_predict.py —— Uni-HamGNN 通用模型预测 hamiltonian.npy（submit 模式）。

本步只准备 Input.yaml + 写 submit.sh，预测在计算节点作业里跑（单进程多线程 torch）。
读 step1 的 graph_data.npz，用通用模型预测哈密顿量，产出 hamiltonian.npy。
判据：predict_summary.json 里 "PREDICT_DONE": true。
"""
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stepconf  # noqa: E402
import unihamgnn_common as uc  # noqa: E402

OUTDIR = "step2_predict"
STEP = "step2_predict"
STEP1 = "step1_graph_data"


def main():
    cwd = Path.cwd()
    out = cwd / OUTDIR
    out.mkdir(exist_ok=True)
    conf = stepconf.load(uc.COMMON_SPEC, STEP)
    soc = bool(conf["SOC"])

    # 结构接力：step1 的 graph_data.npz 必须已经存在
    graph_dir = STEP1 + ("/graph_data_soc" if soc else "/graph_data_non_soc")
    non_soc_npz = (cwd / STEP1 / "graph_data_non_soc" / "graph_data.npz").resolve()
    if not non_soc_npz.is_file():
        sys.exit("[ERROR] 缺 %s —— step1 必须先跑完" % non_soc_npz)
    soc_npz = ""
    if soc:
        soc_npz = (cwd / STEP1 / "graph_data_soc" / "graph_data.npz").resolve()
        if not soc_npz.is_file():
            sys.exit("[ERROR] 缺 %s —— step1（SOC）必须先跑完" % soc_npz)

    uni_model = (conf["UNI_MODEL"] or "").strip()
    if not uni_model:
        sys.exit("[ERROR] 未设置 UNI_MODEL（通用模型 .pkl 路径）。 "
                 "用 tf -tt unihamgnn -p <材料> -j 2 conf --set params.UNI_MODEL=... 设置。")

    hd = (conf["HAMGNN_DIR"] or "").strip()
    if not hd:
        sys.exit("[ERROR] 未设置 HAMGNN_DIR（HamGNN 仓库目录）。")
    predictor = Path(os.path.expanduser(str(hd))) / "Uni-HamGNN" / "Uni-HamiltonianPredictor.py"
    if not predictor.is_file():
        sys.exit("[ERROR] 找不到 %s —— 请核对 HAMGNN_DIR。" % predictor)

    here = Path(__file__).resolve().parent
    if not (here / "input_predict.tpl").is_file():
        sys.exit("[ERROR] 缺模板 templates/input_predict.tpl —— gen_need 里漏了它？")

    uc.render_tpl(here / "input_predict.tpl",
                  {"UNI_MODEL": uni_model,
                   "NON_SOC_DATA": str(non_soc_npz),
                   "SOC_DATA": str(soc_npz) if soc else "",
                   "DEVICE": conf["DEVICE"] or "cpu"},
                  out / "Input.yaml")

    for f in ("write_step2_summary.py",):
        if not (here / f).is_file():
            sys.exit("[ERROR] 缺 %s —— gen_need 里漏了它？" % f)
        shutil.copyfile(str(here / f), str(out / f))

    driver = """#!/bin/bash
# run_predict.sh —— step2 计算节点驱动（在 conda 环境里由 submit.sh 调用）
set -euo pipefail
cd "$(dirname "$0")"
export CUDA_VISIBLE_DEVICES=""

# 3090 上偶发 transient SIGTERM，预测没出 hamiltonian.npy 就重试几次。
for i in 1 2 3; do
  python {predictor} --config Input.yaml && break
  echo "[retry] predict 第 $i 次失败，重试..."
  sleep 3
done
python write_step2_summary.py
""".format(predictor=predictor)
    (out / "run_predict.sh").write_text(driver, encoding="utf-8")

    tpl = uc.resolve_submit(here, "submit_hamgnn")
    uc.write_submit(tpl, out / "submit.sh", {
        "JOBNAME": uc.new_jobname(cwd, "S2predict"),
        "CONDA_SH": conf["CONDA_SH"] or uc.DEFAULT_CONDA_SH,
        "CONDA_ENV": conf["CONDA_ENV"] or uc.DEFAULT_CONDA_ENV,
        "NTHREADS": conf["NTHREADS"],
        "CMD": "bash run_predict.sh",
    })
    stepconf.apply_submit(out / "submit.sh", conf.submit)

    method = uc.read_method(cwd / STEP1 / uc.METHOD_FILE)
    uc.write_method(out / uc.METHOD_FILE,
                    FUNC=method.get("FUNC", "unihamgnn"),
                    DIM=method.get("DIM"),
                    SOC=("true" if soc else "false"),
                    NAO_MAX=conf["NAO_MAX"])
    print("[DONE] %s：submit.sh 就绪（作业跑完写 hamiltonian.npy + predict_summary.json）"
          % OUTDIR)


if __name__ == "__main__":
    main()
