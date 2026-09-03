#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""reuse_structure.py —— kl-dft-cpu 结构复用检测（本地）。

用途：kl 链 init 后、start 前，检测本地是否已有其它技能优化好的结构。
      有 → 复用其 CONTCAR 作 kl 的起始 POSCAR；没有 → kl 的 S1 从原始 POSCAR 从头优化。

重要：kl 的 S1 无论复用与否都会重新优化（DFT 链 EDIFF=1E-7 + EDIFFG=-0.001、MACE 链 FMAX=1e-4，
      都比电子链更严），复用只是给一个更接近极小点的起始结构、省离子步数，不是跳过 S1。

用法：
    python3 reuse_structure.py <材料名> [目标技能]   # 默认 kl-dft-cpu；可传 kl-mace-cpu / kl-mace-gpu
    python3 reuse_structure.py                      # 用当前目录反推材料（材料目录或 kl 技能子目录）

候选源（按优先级，遍历 _SKILLS × _STEP1_CANDS，存在且非空即命中）：
    技能：ke-dft-cpu → opt-dft-cpu → band-dft-cpu → elastic-dft-cpu
    步骤：step1_opt → step1_std_opt → step1c_PBE_opt → step1b_PBE_opt → step1a_PBE_opt

产物：<目标技能>/POSCAR（覆盖前先把原始 POSCAR 备份成 POSCAR_raw，绝不覆盖已有备份）。
"""
import shutil
import sys
from pathlib import Path

# 数据根：本地项目统一根目录（含 jzz/jap 等子结构）
DATA_ROOTS = [
    "/mnt/d/tf_data/jzz/jap",
    "/mnt/d/tf_data",
]

# 候选源：技能子目录 -> result 下的相对路径
# step1 目录名与 ke 链的 _STEP1_CANDS 一致（step1_opt/step1_std_opt/step1{c,b,a}_PBE_opt），
# 避免某材料用的是分段 PBE 优化（step1c_PBE_opt）时复用静默落空。
_STEP1_CANDS = ("step1_opt", "step1_std_opt",
                "step1c_PBE_opt", "step1b_PBE_opt", "step1a_PBE_opt")
_SKILLS = ("ke-dft-cpu", "opt-dft-cpu", "band-dft-cpu", "elastic-dft-cpu")
CANDIDATES = [(s, [name, "CONTCAR"]) for s in _SKILLS for name in _STEP1_CANDS]


def find_material_dir(name: str) -> Path:
    """按材料名在数据根下找材料本地目录（优先精确匹配）。"""
    for root in DATA_ROOTS:
        base = Path(root)
        if not base.is_dir():
            continue
        for sub in ("jzz/jap", ""):  # 先深后浅
            d = base / sub / name if sub else base / name
            if d.is_dir():
                return d
    return None


def find_relaxed(mat: Path):
    """返回 (CONTCAR 路径, 来源技能) 或 (None, None)。"""
    for skill, rel in CANDIDATES:
        p = mat / skill / "result"
        for part in rel:
            p = p / part
        if p.is_file() and p.stat().st_size > 0:
            return p, skill
    return None, None


def main():
    mat = None
    if len(sys.argv) > 1:
        mat = find_material_dir(sys.argv[1])
        if mat is None:
            print("[ERROR] 找不到材料目录：%s（数据根 %s）" % (sys.argv[1], DATA_ROOTS))
            return 2
    else:
        cwd = Path.cwd().resolve()
        mat = cwd.parent if cwd.name in ("kl-dft-cpu", "kl-mace-cpu", "kl-mace-gpu") else cwd

    src, skill = find_relaxed(mat)
    if src is None:
        print("[..] 本地未找到已优化的 CONTCAR（ke/opt/band/elastic 都没算过）")
        print("     各技能 S1 将从原始 POSCAR 从头优化")
        return 0

    # 产物 = 材料根 POSCAR（tf 的 gen 一律从材料根取初始结构；技能子目录里的 POSCAR 不被使用）
    mat_poscar = mat / "POSCAR"
    if not mat_poscar.is_file():
        print("[WARN] 命中 %s 的 %s，但材料根 POSCAR 不存在（%s）" % (skill, src, mat_poscar))
        print("       跳过复用")
        return 1

    # 备份原始 POSCAR（只备一次，绝不覆盖已有备份）
    raw = mat_poscar.with_name("POSCAR_raw")
    if not raw.is_file():
        shutil.copyfile(mat_poscar, raw)
        print("[..] 原始 POSCAR 备份为 %s" % raw.name)
    shutil.copyfile(src, mat_poscar)
    # provenance 标记：追加式记录每次替换（换成了什么/什么时候/来源），
    # 让"材料根 POSCAR 被复用结构覆盖过"这件事可追溯（备份只证明"换过"，provenance 说明"换成什么"）
    import datetime
    prov = mat_poscar.with_name("POSCAR.provenance")
    with open(prov, "a", encoding="utf-8") as fh:
        fh.write("%s  替换为 %s（原始结构见 POSCAR_raw）\n"
                 % (datetime.date.today().isoformat(), src))
    print("[OK] 复用 %s 的 CONTCAR（%s）" % (skill, src))
    print("     → %s（provenance 追加到 %s）" % (mat_poscar, prov.name))
    print("     注意：各技能 S1 仍会重新优化，复用只省离子步；覆盖影响所有未跑 S1 的技能")
    return 0


if __name__ == "__main__":
    sys.exit(main())
