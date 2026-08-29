#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""defect-dft-cpu step2：枚举本征缺陷并各建子目录做中性弛豫（fanout: def-*）。"""
import sys, os, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import defects_common as D

STEP = "step2_defects"

def main():
    conf = D.load_stepconf()
    src = None
    for cand in ["step1_bulk/CONTCAR", "step1_bulk/POSCAR"]:
        if os.path.exists(cand):
            src = cand; break
    if src is None:
        raise SystemExit("[错误] 找不到 step1_bulk/CONTCAR —— 先跑完 step1 再 gen step2")
    sc = D.parse_poscar(src)
    defects = D.enumerate_defects(sc)
    os.makedirs(STEP, exist_ok=True)
    manifest = []
    for i, (suffix, disp, struct) in enumerate(defects):
        ddir = "def-%03d_%s" % (i, suffix)
        D.build_job(os.path.join(STEP, ddir), struct, conf, {
            "SYSTEM": "defect-dft-cpu step2 %s" % disp,
            "IBRION": "2", "ISIF": "2", "NSW": "100",
            "EDIFFG_LINE": "EDIFFG = -0.02",
        }, ddir)
        order = []
        for a in struct["atoms"]:
            if a not in order:
                order.append(a)
        manifest.append({"dir": ddir, "name": suffix, "disp": disp,
                         "counts": {a: struct["atoms"].count(a) for a in order}})
    with open(os.path.join(STEP, "defects_manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    print("[OK] 生成 %s/ 下 %d 个缺陷子目录（def-*）" % (STEP, len(defects)))

if __name__ == "__main__":
    main()
