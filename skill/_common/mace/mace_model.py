#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mace_model.py —— MACE 模型定位 + ASE calculator 构造。

**在 conda 环境（有 torch / mace-torch）里被 mace_relax.py / mace_forces.py import。**
登录节点的系统 python 不要 import 本文件。

模型定位顺序（MACE_MODEL 的值）：
  1. 绝对路径 / 以 ./ ../ 开头的相对路径 —— 直接用
  2. 在 <搜索目录> 里按文件名找：步骤目录 → 材料目录 → MACE_MODEL_DIR
     （所以把 .model 拷进材料目录或 MACE_MODEL_DIR 都行）
  3. 基座模型名（mace-mp / mace-off + small|medium|large）—— 走 mace_mp / mace_off
     工厂函数。注意它要联网下载，计算节点通常没有外网：**第一次务必在登录节点跑一次
     把权重缓存到 ~/.cache/mace，或者直接下 .model 文件走第 2 条。**

dtype 固定 float64：声子/三阶力常数对力的数值噪声极敏感，float32 的力误差
（~1e-3 eV/Å 量级）足以在声学支上做出几十 cm⁻¹ 的假虚频。这一条不要改。
"""
import os
import sys
from pathlib import Path

FOUNDATION_ALIASES = {
    "mace-mp": "mace_mp", "mace_mp": "mace_mp", "mp": "mace_mp",
    "mace-mp-0": "mace_mp", "mace-mpa": "mace_mp", "mace-omat": "mace_mp",
    "mace-off": "mace_off", "mace_off": "mace_off", "off": "mace_off",
}
SIZES = ("small", "medium", "large", "medium-mpa-0", "medium-omat-0")


def resolve_model(name, search_dirs=()):
    """-> (kind, value)。kind ∈ {'file','mace_mp','mace_off'}。"""
    name = str(name or "").strip()
    if not name:
        sys.exit("[ERROR] MACE_MODEL 为空。写 .model 文件名/路径，或基座名如 "
                 "'mace-mp:medium'。")

    # 基座模型：mace-mp / mace-mp:medium / mace-off:small
    head, _, size = name.partition(":")
    if head.lower() in FOUNDATION_ALIASES and not name.endswith((".model", ".pt")):
        kind = FOUNDATION_ALIASES[head.lower()]
        size = (size or "medium").strip()
        if size not in SIZES:
            print("[WARN] 基座尺寸 %r 不在常见集合 %s，原样传给 mace" % (size, list(SIZES)))
        return kind, size

    p = Path(name)
    if p.is_absolute() or name.startswith(("./", "../", "~")):
        p = Path(os.path.expanduser(name))
        if not p.is_file():
            sys.exit("[ERROR] MACE_MODEL 指向的文件不存在：%s" % p)
        return "file", str(p.resolve())

    for d in search_dirs:
        if not d:
            continue
        cand = Path(os.path.expanduser(str(d))) / name
        if cand.is_file():
            return "file", str(cand.resolve())

    sys.exit("[ERROR] 找不到模型 %r。找过：%s\n"
             "        把 .model 放进材料目录或 MACE_MODEL_DIR，或把 MACE_MODEL 写成"
             "绝对路径 / 基座名（mace-mp:medium）。"
             % (name, ", ".join(str(x) for x in search_dirs if x)))


def pick_device(device="auto"):
    """auto → 有卡用卡；显式写 cuda 却没有卡 → **直接退出**。

    这一条是给 GPU 版本兜底的：在 GPU 队列上悄悄退回 CPU，作业照跑、结果照出，
    但占着卡跑了几十倍的时间，等你发现时机时已经烧掉了。宁可当场失败。
    """
    dev = str(device or "auto").strip().lower()
    try:
        import torch
        avail = torch.cuda.is_available()
    except Exception as e:
        if dev.startswith("cuda"):
            sys.exit("[ERROR] DEVICE=%s 但 torch 都导不进来：%s" % (dev, e))
        return "cpu"
    if dev == "auto":
        return "cuda" if avail else "cpu"
    if dev.startswith("cuda") and not avail:
        sys.exit("[ERROR] DEVICE=%s 但 torch.cuda.is_available() 是 False。\n"
                 "        排查：作业申请了 --gres=gpu:N 吗？分区对吗？torch 是 CUDA 版"
                 "还是 CPU 版（pip show torch 看版本名带不带 +cu）？\n"
                 "        不想要卡就把 DEVICE 设成 cpu 或 auto。" % dev)
    return dev


def build_calculator(model_name, search_dirs=(), device="auto", dtype="float64"):
    """-> (calculator, 描述字符串)。描述里带真实解析到的路径和设备，供 summary 存档。"""
    kind, val = resolve_model(model_name, search_dirs)
    dev = pick_device(device)

    if dev.startswith("cuda") and str(dtype) == "float64":
        try:
            import torch
            name = torch.cuda.get_device_name(0)
            if any(tag in name for tag in ("RTX", "GeForce", "TITAN")):
                print("[WARN] %s 是消费级卡，FP64 吞吐只有 FP32 的 1/32~1/64。"
                      "这类卡上 float64 甚至可能比整节点 CPU 还慢——先小规模测一次"
                      "再决定用哪版技能。（不要为了快改成 float32：力的噪声会造出假虚频）"
                      % name)
        except Exception:
            pass

    if kind == "file":
        from mace.calculators import MACECalculator
        # mace-torch 不同版本参数名不一样（model_paths / model_path），逐个试。
        last = None
        for kw in ("model_paths", "model_path", "model"):
            try:
                calc = MACECalculator(**{kw: val}, device=dev, default_dtype=dtype)
                return calc, "file:%s device=%s dtype=%s" % (val, dev, dtype)
            except TypeError as e:
                last = e
        raise RuntimeError("MACECalculator 构造失败（试过 model_paths/model_path/model）：%s" % last)

    factory = None
    if kind == "mace_mp":
        from mace.calculators import mace_mp as factory
    elif kind == "mace_off":
        from mace.calculators import mace_off as factory
    calc = factory(model=val, device=dev, default_dtype=dtype)
    return calc, "%s:%s device=%s dtype=%s" % (kind, val, dev, dtype)


def to_ase(cell, symbols, scaled_positions):
    from ase import Atoms
    return Atoms(symbols=list(symbols), cell=cell,
                 scaled_positions=scaled_positions, pbc=True)


def phonopy_atoms_to_ase(pa):
    """PhonopyAtoms -> ase.Atoms（跨 phonopy 版本取属性）。"""
    syms = getattr(pa, "symbols", None)
    if syms is None:
        syms = pa.get_chemical_symbols()
    cell = getattr(pa, "cell", None)
    if cell is None:
        cell = pa.get_cell()
    spos = getattr(pa, "scaled_positions", None)
    if spos is None:
        spos = pa.get_scaled_positions()
    return to_ase(cell, syms, spos)

def ase_to_phonopy(atoms):
    """ase.Atoms -> PhonopyAtoms。phonopy >=2.44 的 Phonopy(unitcell=...) 只收
    PhonopyAtoms（构造器没有 atoms= 关键字），按字段显式构造。
    [UPSTREAM-FROM-MLFF] 由 skill/mlff-mace 上游化：mlff-mace 的 fc2_calib/
    benchmark 在 phonopy 2.47 上实测需要。"""
    import numpy as np
    from phonopy.structure.atoms import PhonopyAtoms
    return PhonopyAtoms(symbols=list(atoms.get_chemical_symbols()),
                        cell=np.array(atoms.cell[:], dtype=float),
                        scaled_positions=np.array(atoms.get_scaled_positions(),
                                                  dtype=float))

