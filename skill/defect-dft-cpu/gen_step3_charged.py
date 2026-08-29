#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""defect-dft-cpu step3：对 step2 各缺陷的中性弛豫结构做带电态单点（fanout: def-*）。"""
import sys, os, json, shutil
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import defects_common as D

STEP = "step3_charged"

def charge_states(name):
    """各缺陷族的电荷态清单。反位按价电子数定施主/受主（Te=6 > Sb/Bi=5 > Pb/Sn=4）：
    X 占 Y 位，X 价电子比 Y 多 → 施主(正电荷态)，少 → 受主(负电荷态)。"""
    if "pair" in name:
        return [0]
    if "_i_" in name:
        return [0, -1, -2] if name.startswith("Te") else [0, 1, 2]  # Te间隙受主, 阳离子间隙施主
    if name.startswith("v_Te"):
        return [0, 1, 2]                       # Te 空位：施主（缺阴离子）
    if name.startswith("v_"):
        return [0, -1, -2]                     # 阳离子空位：受主
    if name.endswith("_Te"):
        # 阳离子占 Te 位：受主（Sb/Bi 少 1 电子 → 0/-1；Pb/Sn 少 2 → 0/-1/-2）
        return [0, -1] if name.startswith(("Sb_", "Bi_")) else [0, -1, -2]
    if name.startswith("Te_"):
        # Te 占阳离子位：施主（对 Sb/Bi 多 1 → 0/+1；对 Pb/Sn 多 2 → 0/+1/+2）
        return [0, 1] if name.endswith(("_Sb", "_Bi")) else [0, 1, 2]
    if name.endswith(("_Pb", "_Sn")):
        return [0, 1]                          # Sb/Bi(5) 占 Pb/Sn(4)：施主
    if name.endswith(("_Sb", "_Bi")):
        return [0, -1]                         # Pb/Sn(4) 占 Sb/Bi(5)：受主
    return [0]

def nelect_neutral(order, counts, potcar_path):
    """由 POTCAR ZVAL 求中性态总电子数。"""
    zvals = D.read_nelect_from_potcar(potcar_path)
    if len(zvals) != len(order):
        raise SystemExit("[错误] POTCAR 元素数(%d) 与 POSCAR 元素数(%d) 不一致" % (len(zvals), len(order)))
    return int(round(sum(z * counts[el] for z, el in zip(zvals, order))))

def main():
    conf = D.load_stepconf()
    if not os.path.isdir("step2_defects"):
        raise SystemExit("[错误] 找不到 step2_defects/ —— 先跑完 step2")
    # 中性态 NELECT 从 step2 任一 POTCAR 算
    manifest = json.load(open("step2_defects/defects_manifest.json"))
    os.makedirs(STEP, exist_ok=True)
    n_jobs = 0
    for item in manifest:
        ddir = item["dir"]
        src = None
        for cand in ["step2_defects/%s/CONTCAR" % ddir, "step2_defects/%s/POSCAR" % ddir]:
            if os.path.exists(cand):
                src = cand; break
        if src is None:
            print("[跳过] %s 缺 CONTCAR" % ddir); continue
        struct = D.parse_poscar(src)
        order = [a for a in dict.fromkeys(struct["atoms"])]
        counts = {a: struct["atoms"].count(a) for a in order}
        pot = "step2_defects/%s/POTCAR" % ddir
        n0 = nelect_neutral(order, counts, pot)
        for q in charge_states(item["name"]):
            qdir = "%s_q%+d" % (ddir, q)
            ne = n0 - q          # q=+1 -> 去一个电子 -> NELECT-1
            outdir = os.path.join(STEP, qdir)
            # 从中性 CHGCAR 续算（ICHARG=1）；旧流水线无 CHGCAR 时退回 ICHARG=2 从头算
            chg_src = "step2_defects/%s/CHGCAR" % ddir
            icharg = "ICHARG = 2"
            if os.path.exists(chg_src) and os.path.getsize(chg_src) > 0:
                icharg = "ICHARG = 1"
            # 奇数电子数带电态：seed 磁矩打破自旋对称（否则被算成非磁闭壳）
            natoms = len(struct["atoms"])
            magmom = D.spin_seed_magmom(natoms) if (ne % 2 != 0) else "%d*0" % (3 * natoms)
            D.build_job(outdir, struct, conf, {
                "SYSTEM": "defect-dft-cpu step3 %s q=%+d" % (item["disp"], q),
                "IBRION": "-1", "ISIF": "0", "NSW": "0",
                "EDIFFG_LINE": "",
                "LVHAR_LINE": "LVHAR = .TRUE." if conf.get("LVHAR_CHARGED", "1") == "1" else "",
                "NELECT_LINE": "NELECT = %d" % ne,
                "ICHARG_LINE": icharg,
                "SIGMA_LINE": "SIGMA = 0.01",
                "MAGMOM": magmom,
            }, qdir)
            if icharg == "ICHARG = 1":
                shutil.copy(chg_src, os.path.join(outdir, "CHGCAR"))
            n_jobs += 1
    print("[OK] 生成 %s/ 下 %d 个带电态子目录" % (STEP, n_jobs))

if __name__ == "__main__":
    main()
