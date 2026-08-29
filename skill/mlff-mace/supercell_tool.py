#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""supercell_tool.py —— step2_supercell 引擎（在 venv 里跑，需要 mace/torch）。

cwd = 材料目录。干两件事：
    1. 从基座 .model 读 r_max 并校验基座模型覆盖体系全部元素（z_table 不含就报错）
    2. 按 §5.1 定训练/基准超胞（每方向 ≥ 2·r_max；2D 真空方向锁 1 + 真空厚度检查；
       原子数 ∈ [MIN_ATOMS, MAX_ATOMS]），写 supercell_summary.json

退出码 0 成功；非 0 = [ERROR]。
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mlff_common as mc  # noqa: E402


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--prim", default="step1_relax/CONTCAR")
    p.add_argument("--model", required=True)
    p.add_argument("--model-dir", default="")
    p.add_argument("--min-atoms", type=int, default=60)
    p.add_argument("--max-atoms", type=int, default=150)
    p.add_argument("--min-vacuum", type=float, default=15.0)
    p.add_argument("--out", default="step2_supercell/supercell_summary.json")
    return p.parse_args()


def read_model_rmax_and_elements(model_path):
    """加载 .model，返回 (r_max, z_table)。优先轻量读法，兜底整模型加载。"""
    import torch
    loaded = None
    # 轻量：torch.load 找 r_max / atomic_numbers
    obj = torch.load(model_path, map_location="cpu", weights_only=False)
    rmax, zs = None, None

    def _find(o, key):
        if isinstance(o, dict):
            for k, v in o.items():
                if str(k).lower() == key.lower():
                    return v
                r = _find(v, key)
                if r is not None:
                    return r
        elif isinstance(o, (list, tuple)):
            for v in o:
                r = _find(v, key)
                if r is not None:
                    return r
        return None

    for key in ("r_max", "r_max_foundation"):
        v = _find(obj, key)
        if v is not None:
            try:
                rmax = float(v)
                break
            except (TypeError, ValueError):
                pass
    zs = _find(obj, "atomic_numbers")
    if zs is not None:
        try:
            zs = sorted(int(z) for z in zs)
        except (TypeError, ValueError):
            zs = None
    if rmax is None or zs is None:
        # 兜底：直接构造 MACECalculator（慢，但一定能拿到）
        import mace_model as mm
        calc, _ = mm.build_calculator(model_path, [Path.cwd()], "cpu", "float64")
        model = calc.models[0]
        if rmax is None:
            rmax = float(getattr(model, "r_max", None) or
                         getattr(model, "r_max_foundation", None))
        if zs is None:
            an = getattr(model, "atomic_numbers", None)
            if an is None:
                an = getattr(model, "z_table", None)
            if an is None:
                try:
                    an = [i for i in range(len(model.atomic_energies_fn.atomic_energies))]
                except Exception:
                    an = None
            if an is None:
                sys.exit("[ERROR] 读不出基座模型的元素表（atomic_numbers）。")
            zs = sorted(int(z) for z in an)
    del obj
    return float(rmax), [int(z) for z in zs]


def main():
    a = parse_args()
    cwd = Path.cwd()
    from ase.data import chemical_symbols

    prim_path = cwd / a.prim
    if not prim_path.is_file():
        sys.exit("[ERROR] 找不到 %s —— step1_relax 还没算完？" % a.prim)
    model_path = a.model if Path(os.path.expanduser(a.model)).is_file() else (
        Path(os.path.expanduser(a.model_dir)) / a.model if a.model_dir else None)
    if model_path is None or not Path(model_path).is_file():
        sys.exit("[ERROR] 找不到基座模型 %r（找过 MACE_MODEL_DIR=%s）。"
                 "把 .model 拷进材料目录/MACE_MODEL_DIR，或把 MACE_MODEL 写成绝对路径。"
                 % (a.model, a.model_dir))

    rmax, zs = read_model_rmax_and_elements(str(model_path))
    print("[..] 基座模型 %s：r_max = %.4f Å，元素 Z = %s"
          % (Path(model_path).name, rmax, zs))

    # ---- 元素覆盖校验 ----
    from dim_common import read_poscar_cell_frac
    lat, frac = read_poscar_cell_frac(str(prim_path))
    lines = Path(prim_path).read_text(encoding="utf-8-sig").splitlines()
    idx = 5
    tokens = lines[idx].split()
    import re
    if re.fullmatch(r"[+-]?\d+", tokens[0]):
        sys.exit("[ERROR] POSCAR 没有元素符号行（VASP4 格式），无法校验元素覆盖。")
    syms = tokens
    # 注意：chemical_symbols[0] 是占位 'X'，不能用 enumerate(...,1)
    z_of = {sym: chemical_symbols.index(sym) for sym in syms}
    missing = [s for s in syms if z_of.get(s, 0) not in zs]
    if missing:
        sys.exit("[ERROR] 基座模型 %s 不覆盖元素 %s（z_table=%s）。\n"
                 "         换一个覆盖这些元素的基座模型（MACE_MODEL）。"
                 % (Path(model_path).name, ",".join(sorted(set(missing))), zs))

    # ---- 维度 + 超胞 ----
    method = mc.read_kv(cwd / "step1_relax" / mc.METHOD_FILE)
    dim, vac_axis = mc.resolve_dim(prim_path, method.get("DIM", "auto"))
    reps = mc.supercell_reps_for(str(prim_path), dim, rmax,
                                 a.min_atoms, a.max_atoms,
                                 a.min_vacuum, vac_axis if vac_axis is not None else 2)
    n = len(frac) * reps[0] * reps[1] * reps[2]
    print("[..] 维度=%s，超胞 %s（%d 原子），2·r_max=%.2f Å"
          % (dim.upper(), " ".join(map(str, reps)), n, 2 * rmax))

    summary = {
        "dim": dim,
        "vac_axis": int(vac_axis) if vac_axis is not None else None,
        "r_max_A": round(rmax, 4),
        "foundation_model": Path(model_path).name,
        "foundation_z_table": zs,
        "supercell_reps": reps,
        "n_atoms_supercell": n,
        "n_atoms_primitive": len(frac),
        "min_atoms": a.min_atoms,
        "max_atoms": a.max_atoms,
        "criterion": "每个非真空方向胞长 ≥ 2·r_max；原子数 ∈ [MIN_ATOMS, MAX_ATOMS]",
    }
    out = cwd / a.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8", newline="\n")
    mc.write_kv(cwd / mc.MLFF_PARAMS, DIM=dim.upper(),
                SUPERCELL=" ".join(map(str, reps)), RMAX="%.4f" % rmax)
    print("[DONE] supercell_summary.json：%s %s（%d 原子，r_max=%.3f Å）"
          % (dim.upper(), " ".join(map(str, reps)), n, rmax))


if __name__ == "__main__":
    main()
