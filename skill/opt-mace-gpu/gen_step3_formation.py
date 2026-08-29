#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_step3_formation.py —— 形成能后处理（step3_formation）。

读 step2_mace_static 的总能 + 组分 + step.conf 的参考化学势 MU，算
E_per_atom / 形成能 E_form = E_tot - Σ n_i·μ_i，写 energy_summary.json。

参考化学势 MU（元素:能量，eV/原子）填 step3 的 step.conf，例如 MU = C:-9.0 Si:-5.4。
不填则 E_per_atom 照常输出、形成能标"未算"。
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stepconf

STEP2_DIR = "step2_mace_static"
OUT_DIR = "step3_formation"
OUT_FILE = "energy_summary.json"

ENERGY_SPEC = {
    # 全局键（本步不用，但 step.conf 三层合并后会带进来，必须声明才能过校验）
    "MACE_MODEL": ("mace-mp:medium", "str"),
    "MACE_MODEL_DIR": ("", "str"),
    "DEVICE": ("cpu", "str"),
    "DTYPE": ("float64", "str"),
    "CONDA_SH": ("/public/home/wangchao/miniconda3/etc/profile.d/conda.sh", "str"),
    "CONDA_ENV": ("mace", "str"),
    # ---- 跨步骤键：step1 的参数经三层合并会带进本步，声明但不消费 ----
    "DIMENSION": ("auto", "str"), "RELAX": (True, "bool"),
    "RELAX_CELL": (True, "bool"), "FMAX": (1e-4, "float"),
    "MAX_STEPS": (2000, "int"), "OPTIMIZER": ("FIRE", "str"),
    "FIX_SYMMETRY": (True, "bool"), "SYMPREC": (1e-4, "float"),
    "CELL_POLICY": ("primitive", "str"), "RESIDUAL_TOL": (2e-3, "float"),
    "STRESS_TOL": (0.05, "float"),
    # 本步
    "MU": (None, "elemmap"),           # 形成能化学势：元素:能量(eV/原子)
    "MU_MODEL": (None, "str"),         # 算 μ 时用的 MACE 模型，用于溯源比对
}


def read_poscar(path: Path):
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    if len(lines) < 7:
        raise SystemExit("[ERROR] %s 行数不足" % path)
    tokens = lines[5].split()
    if tokens and tokens[0].lstrip("-").isdigit():
        raise SystemExit("[ERROR] %s 无元素符号行（VASP4 格式），无法定组分" % path)
    counts = [int(x) for x in lines[6].split()]
    if len(tokens) != len(counts):
        raise SystemExit("[ERROR] %s 元素行 %d 项、计数行 %d 项，对不上"
                         % (path, len(tokens), len(counts)))
    return tokens, counts


def model_identity(desc):
    """把 mace_model.build_calculator 的描述串压成可比对的 (模型名, dtype)。

    'file:/opt/mace_models/MACE-xxx.model device=cpu dtype=float64'
        -> ('MACE-xxx.model', 'float64')
    'mace-mp:medium device=cuda dtype=float64'
        -> ('mace-mp:medium', 'float64')

    MU_MODEL 里只写模型名（不带 device/dtype）也能比 —— 缺的那部分就不比。
    """
    if not desc:
        return None, None
    s = str(desc).strip()
    m = re.search(r"dtype=(\S+)", s)
    dtype = m.group(1) if m else None
    head = re.split(r"\s+device=", s)[0].strip()
    if head.lower().startswith("file:"):
        head = Path(head[5:]).name
    return (head or None), dtype


def check_mu_provenance(mu_model, desc, result):
    """μ 必须和 E_tot 出自同一个 MACE 势，否则形成能没有意义。

    MACE 的总能带模型自己的能量零点。E_tot 用新模型、μ 还是旧模型算的 ——
    数字看着完全正常，排名却是错的，而且不会有任何报错。所以这里宁可拦住。
    """
    name, dtype = model_identity(desc)
    want_name, want_dtype = model_identity(mu_model)

    if not want_name:
        note = ("μ 来源未记录：step3 的 step.conf 没写 MU_MODEL，无法验证 MU 是不是"
                "用同一个 MACE 势算出来的。换模型重算而忘了同步 μ 时，形成能排名会"
                "安静地错。建议补一行：MU_MODEL = %s" % (name or "<算 μ 时用的模型>"))
        result["mu_provenance"] = note
        print("[WARN] " + note)
        return

    if name and want_name != name:
        sys.exit("[ERROR] MU 与 E_tot 不是同一个 MACE 势算出来的：\n"
                 "        E_tot（S2 实际用的） : %s\n"
                 "        MU_MODEL（step.conf）: %s\n"
                 "        MACE 总能带各自模型的能量零点，混用形成能没有意义。\n"
                 "        修法二选一：\n"
                 "          · 用当前模型重算 μ —— 参考态（石墨 C / hcp Mg 等）各跑\n"
                 "            一遍本技能三步，取 energy_summary.json 的 E_per_atom_eV；\n"
                 "          · 确认 μ 确实是当前模型算的，就把 MU_MODEL 改对。"
                 % (name, want_name))

    if want_dtype and dtype and want_dtype != dtype:
        sys.exit("[ERROR] MU 与 E_tot 的 DTYPE 不一致：S2 用 %s，MU_MODEL 记的是 %s。\n"
                 "        float32 与 float64 的总能差在 meV 量级，够污染形成能排名了。"
                 % (dtype, want_dtype))

    result["mu_provenance"] = "MU_MODEL=%s（已与 S2 实际用的势比对一致）" % mu_model


def main():
    cwd = Path.cwd()
    step2 = cwd / STEP2_DIR
    if not step2.is_dir():
        sys.exit("[ERROR] 找不到 %s —— 先让 S2_static 算完" % STEP2_DIR)
    static_json = step2 / "static_summary.json"
    poscar = step2 / "POSCAR"
    for p in (static_json, poscar):
        if not p.is_file():
            sys.exit("[ERROR] 缺少 %s" % p)

    conf = stepconf.load(ENERGY_SPEC, "step3_formation")
    static = json.loads(static_json.read_text())
    if "energy_eV" not in static:
        sys.exit("[ERROR] %s 里没有 energy_eV —— S2 作业没正常收尾？"
                 % static_json)
    etot = float(static["energy_eV"])
    symbols, counts = read_poscar(poscar)
    natoms = sum(counts)
    # fix-optmace：原子数从 POSCAR 数、能量从 json 取，两者必须对得上。
    # 重跑过 gen step2 但没重投作业时会静默错配，E_per_atom 直接错。
    n_static = static.get("n_atoms")
    if n_static is not None and int(n_static) != natoms:
        sys.exit("[ERROR] %s 是 %d 原子，但 %s 的能量对应 %d 原子——\n"
                 "        结构和能量对不上。修：tf ... rerun S2_static"
                 % (poscar, natoms, static_json, int(n_static)))
    formula = "".join(("%s%d" % (s, n)) if n > 1 else s
                      for s, n in zip(symbols, counts))

    result = {
        "formula": formula,
        "natoms": natoms,
        "E_tot_eV": round(etot, 8),
        "E_per_atom_eV": round(etot / natoms, 8),
        # μ 溯源：MACE 总能是模型自己的零点，这一行是判断"能不能横向比"的依据
        "model": static.get("model") or "未知（static_summary.json 无 model 字段）",
    }

    mu = conf["MU"]
    if not mu or any(s not in mu for s in symbols):
        result["FORMATION_DONE"] = False
        result["E_form_eV"] = None
        result["E_form_per_atom_eV"] = None
        result["E_form_note"] = ("未算：step3 step.conf 缺 MU（或 MU 没覆盖所有元素）。"
                                 "例：MU = C:-9.0 Si:-5.4")
    else:
        e_form = etot - sum(n * mu[s] for s, n in zip(symbols, counts))
        result["FORMATION_DONE"] = True
        result["E_form_eV"] = round(e_form, 8)
        result["E_form_per_atom_eV"] = round(e_form / natoms, 8)
        result["E_form_note"] = "E_tot - Σ n_i·μ_i；μ=%s" % mu
        check_mu_provenance(conf["MU_MODEL"], static.get("model"), result)

    out = cwd / OUT_DIR
    out.mkdir(exist_ok=True)
    (out / OUT_FILE).write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n")
    print("[DONE] %s：%s" % (OUT_FILE, json.dumps(result, ensure_ascii=False)))


if __name__ == "__main__":
    main()
