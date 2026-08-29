#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fc_fit_driver.py —— 力常数拟合 + 虚频闸 + phonon_summary.json。

klmace S3_fc 的 submit 模式驱动脚本，在计算节点作业里跑（conda/venv 里）。
读 fit_config.json（gen_step3 写好），依次：拟合（phono3py symfc/alm 或 pheasy）
→ phonopy 虚频闸 → 写 phonon_summary.json。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np


def main():
    cfg = json.loads(Path("fit_config.json").read_text(encoding="utf-8"))
    yaml = cfg["yaml"]
    software = cfg.get("software", "phono3py")
    method = cfg.get("method", "random")
    imag_thr = float(cfg.get("imag_thr", 0.10))
    supercell = cfg.get("supercell")
    fc2_supercell = cfg.get("fc2_supercell")
    use_nac = bool(cfg.get("nac", False))

    # ---- 1. 拟合 ----
    if software == "pheasy":
        p_method = str(cfg.get("pheasy_method", "OLS")).upper()
        c3 = str(cfg.get("c3_cutoff", "6.0"))
        rc = subprocess.run("python _pheasy_fit.py %s '%s'" % (p_method, c3),
                            shell=True).returncode
        if rc != 0:
            sys.exit("[ERROR] pheasy 拟合失败(rc=%d)，看 fc_build.log" % rc)
        fit = "pheasy-" + p_method.lower()
    else:
        fit = cfg.get("fit", "symfc")
        flag = {"sym-fc": "", "symfc": "--fc-calc symfc",
                "alm": "--fc-calc alm"}.get(fit)
        if flag is None:
            sys.exit("[ERROR] FIT 只允许 auto/sym-fc/symfc/alm")
        rc = subprocess.run("phono3py-load %s %s" % (yaml, flag), shell=True).returncode
        if rc != 0 and fit == "symfc":
            print("[WARN] symfc 拟合失败（看 fc_build.log；大超胞/numpy 2.x 下可能 segfault），"
                  "退回 alm 再试一次。alm 对超大超胞很慢（半小时起）——想避免就缩超胞"
                  "（MIN_SC_LEN 或显式 SUPERCELL），或项目级把 S3 的 FIT_SOFTWARE 设为 pheasy。")
            rc = subprocess.run("phono3py-load %s --fc-calc alm" % yaml,
                                shell=True).returncode
        if rc != 0:
            sys.exit("[ERROR] 力常数拟合失败(rc=%d)，看 fc_build.log" % rc)
    for f in ("fc2.hdf5", "fc3.hdf5"):
        if not os.path.isfile(f):
            sys.exit("[ERROR] 没生成 %s" % f)
    print("[OK] fc2.hdf5 / fc3.hdf5")

    # ---- 2. 虚频闸（phonopy API）----
    rc = subprocess.run("python _phonon_gate.py", shell=True,
                        capture_output=True, text=True)
    mf = None
    for ln in (rc.stdout or "").splitlines():
        if ln.startswith("MIN_FREQ_THZ"):
            try:
                mf = float(ln.split()[1])
            except (ValueError, IndexError):
                mf = None
    if rc.returncode != 0 or mf is None:
        # 兜底读 band-dft-cpu.yaml
        try:
            import re
            fr = [float(m.group(1))
                  for ln in Path("band-dft-cpu.yaml").read_text(errors="ignore").splitlines()
                  for m in [re.match(r"\s*frequency:\s*(-?[\d.Ee+]+)", ln)] if m]
            mf = min(fr) if fr else None
        except Exception:
            mf = None
    if mf is None:
        stable, note = False, "phonopy 虚频闸失败（看 fc_build.log）"
        print("[FAIL] " + note)
    else:
        stable = mf >= -imag_thr
        note = "最小声子频率 %.4f THz（阈值 -%.2f）：%s" % (
            mf, imag_thr,
            "无明显虚频" if stable else "存在虚频，本势下动力学不稳定，κ 无物理意义")
        print("[%s] %s" % ("OK" if stable else "FAIL", note))

    # pheasy 质量门禁（alpha 撞边界）
    if os.path.isfile(".fit_gate_fail"):
        stable = False
        note = ("pheasy 拟合质量门禁未通过（alpha 撞网格边界），κ 已阻止。"
                "加大 N_RANDOM/OVERSAMPLE 重跑。")
        print("[FAIL] " + note)

    Path("phonon_summary.json").write_text(json.dumps(
        {"stable": bool(stable), "min_frequency_THz": mf, "method": method,
         "fit": fit, "nac": use_nac, "input_yaml": yaml,
         "supercell": supercell, "fc2_supercell": fc2_supercell, "note": note},
        ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    print("[DONE] %s：stable=%s" % ("step3_fc", str(stable).lower()))


if __name__ == "__main__":
    main()
