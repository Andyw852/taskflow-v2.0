#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_step3_band.py —— Uni-HamGNN 能带计算 + 画图（run: gen，登录节点）。

读 step2 预测的 hamiltonian.npy + step1 的 graph_data.npz，在 conda 环境里跑
band_cal，抓"band gap"，写 band_summary.json。

本脚本本身只用标准库（登录节点系统 python 跑得动），band_cal 通过 run_in_env
丢进 conda 环境执行。判据：plot + done_marker=band_summary.json。
"""
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stepconf  # noqa: E402
import unihamgnn_common as uc  # noqa: E402

OUTDIR = "step3_band"
STEP = "step3_band"
STEP1 = "step1_graph_data"
STEP2 = "step2_predict"


def structure_label(poscar):
    lines = Path(poscar).read_text(encoding="utf-8-sig").splitlines()
    if lines and lines[0].strip():
        token = lines[0].split()[0]
        return re.sub(r"[^A-Za-z0-9_.-]+", "_", token).strip("_.-") or "material"
    return "material"


def parse_gap(text):
    m = re.search(r"band gap\s*=\s*(-?[\d.]+)\s*eV", text)
    return float(m.group(1)) if m else None


def main():
    cwd = Path.cwd()
    out = cwd / OUTDIR
    out.mkdir(exist_ok=True)
    conf = stepconf.load(uc.COMMON_SPEC, STEP)
    soc = bool(conf["SOC"])

    graph_dir = STEP1 + ("/graph_data_soc" if soc else "/graph_data_non_soc")
    graph_npz = (cwd / graph_dir / "graph_data.npz").resolve()
    ham = (cwd / STEP2 / "hamiltonian.npy").resolve()
    if not graph_npz.is_file():
        sys.exit("[ERROR] 缺 %s —— step1 必须先跑完" % graph_npz)
    if not ham.is_file():
        sys.exit("[ERROR] 缺 %s —— step2 必须先跑完" % ham)

    here = Path(__file__).resolve().parent
    if not (here / "band_cal.tpl").is_file():
        sys.exit("[ERROR] 缺模板 templates/band_cal.tpl —— gen_need 里漏了它？")

    sys_name = structure_label(cwd / "POSCAR")
    save_dir = out / "band"
    uc.render_tpl(here / "band_cal.tpl",
                  {"NAO_MAX": conf["NAO_MAX"],
                   "GRAPH_DATA_PATH": str(graph_npz),
                   "HAMILTONIAN_PATH": str(ham),
                   "NK": conf["NK"],
                   "SAVE_DIR": str(save_dir),
                   "SYSTEM_NAME": sys_name,
                   "SOC": "True" if soc else "False"},
                  out / "band_cal.yaml")

    # band_cal 偶发「算完但进程收尾被 SIGTERM」（rc=143）——以产物 band_1.png 为准，
    # 没出 png 就重试几次（3090 上的 transient SIGTERM）。纯 CPU，禁掉 CUDA。
    rc = 0
    png_ok = False
    for attempt in range(3):
        rc = uc.run_in_env('export CUDA_VISIBLE_DEVICES=""; band_cal --config band_cal.yaml',
                           out, conf, logname="band_cal.log")
        png_ok = (save_dir / "band_1.png").is_file()
        if png_ok:
            break
        print("[..] band_cal 第 %d 次未出 png（rc=%d），重试..." % (attempt + 1, rc))
    log_path = out / "band_cal.log"
    log_text = log_path.read_text(errors="ignore") if log_path.is_file() else ""
    gap = parse_gap(log_text)
    done = png_ok
    summary = {
        "BAND_DONE": done,
        "band_gap_eV": gap,
        "soc": soc,
        "band_cal_rc": rc,
        "save_dir": str(save_dir),
        "checked_at": datetime.now().isoformat(timespec="seconds"),
    }
    (out / "band_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    if not done:
        sys.exit("[ERROR] band_cal 失败（rc=%d，band_1.png=%s）。日志尾部：\n%s"
                 % (rc, png_ok, log_text[-2000:]))
    print(json.dumps(summary, ensure_ascii=False))
    print("[DONE] %s：band/band_*.png + band/band_*.dat + band_summary.json"
          % OUTDIR)


if __name__ == "__main__":
    main()
