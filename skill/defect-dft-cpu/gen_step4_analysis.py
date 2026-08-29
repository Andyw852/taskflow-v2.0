#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""defect-dft-cpu step4（run:gen）：一条龙出最终结论。

1) 汇总各 OUTCAR 总能 -> formation_energy_summary.json（done_marker）
2) 凸包 -> 化学势窗口 -> energies.json（需 step0 参考相 + step1 bulk）
3) 形成能/转变能级/自洽 E_F/P-N -> formation_energy_results.json

本脚本在登录节点跑（不提交 SLURM）。
"""
import sys, os, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import formation_energy as FE
import convex_hull as CH
import extract_band_refs as EB

STEP = "step4_analysis"

def main():
    # 1) 汇总能量
    summary = {"step": STEP, "status": "partial"}
    bulk = FE.read_energy("step1_bulk/OUTCAR")
    defects = {}
    for step in ("step2_defects", "step3_charged"):
        if not os.path.isdir(step):
            continue
        for d in sorted(os.listdir(step)):
            if not d.startswith("def-"):
                continue
            e = FE.read_energy(os.path.join(step, d, "OUTCAR"))
            if e is not None:
                defects.setdefault(d, {})[step] = e
    summary["E_bulk"] = bulk
    summary["defects"] = defects
    os.makedirs(STEP, exist_ok=True)
    with open(os.path.join(STEP, "formation_energy_summary.json"), "w") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    print("[OK] 汇总 %d 个缺陷目录能量 -> %s/formation_energy_summary.json"
          % (len(defects), STEP))

    # 2) 凸包 -> 化学势（需 step0 参考相 + step1 bulk）
    if os.path.exists("step0_references/references_energy.json") and os.path.exists("step1_bulk/OUTCAR"):
        try:
            CH.run_from_references()
        except SystemExit as ex:
            print("[警告] 凸包未完成：%s" % ex)
    else:
        print("[跳过凸包] 缺 step0 参考相能量或 step1 bulk")

    # 3) 自动补齐 band 参考量（E_gap/VBM/CBM/mstar/epsilon）
    if os.path.exists("energies.json"):
        try:
            EB.main()
        except SystemExit as ex:
            print("[警告] band 参考量未补齐：%s" % ex)

    # 4) 形成能 -> P/N（需 energies.json 含 mu + E_gap + mstar + epsilon）
    if os.path.exists("energies.json"):
        try:
            FE.analyze()
        except SystemExit as ex:
            print("[警告] 形成能分析未完成：%s" % ex)
    else:
        print("[下一步] 缺 energies.json（凸包/band 参考量未就绪）")

if __name__ == "__main__":
    main()
