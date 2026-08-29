#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_step3_energy.py — 能量后处理：读 step2_static 的总能 + 组分 + step.conf 参考值，
算 E_per_atom / 形成能 / 嵌入能，写 energy_summary.json。

运行位置：超算登录节点，cwd = 材料目录（run: gen 步骤，不提交 SLURM）。

参考能量全部从 step.conf [params] 取（缺失则对应项跳过，E_per_atom 永远可算）：
  CALC_FORMATION     是否算形成能（true/false，默认 true）
  CALC_INTERCALATION 是否算嵌入能（true/false，默认 true）
  MU                 形成能元素化学势，元素:能量(eV/原子)，如  MU = C:-9.0 Li:-1.9
  GUEST_ELEMENT      嵌入能客体元素（如 Li）
  MU_GUEST           客体化学势，eV/原子（如 bcc Li 的每原子总能）
  宿主（未嵌入）——二选一，都会做“骨架一致性校验”，不一致直接报错：
  HOST_DIR           宿主参考材料目录：自动从它的 step2_static 读 E_host + 骨架组分
                     （并软比对泛函）。推荐，最不容易填错。
  HOST_FORMULA       手填 HOST_ENERGY 时用它声明骨架组分做校验，如  HOST_FORMULA = C:60
  HOST_ENERGY        宿主总能 eV（HOST_DIR 缺省时用；两者都给且不一致会告警并以 HOST_DIR 为准）

★ 骨架一致性：本步算的是相对“空宿主”（客体 x 从 0）的平均嵌入能，宿主参考里不应含客体元素。
  校验规则：当前结构去掉全部 GUEST_ELEMENT 后，各元素数目必须与宿主逐一相等，否则终止。

改参数：  tf -tt opt-dft-cpu -p <材料> -j 3 conf --set params.MU="C:-9.0 Li:-1.9"
（参考值只整体平移排名，不影响同一组成下的相对次序；详见 README.md。）
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stepconf

STEP2_DIR = "step2_static"
OUT_DIR = "step3_energy"
OUT_FILE = "energy_summary.json"
METHOD_FILE = "workflow_method.txt"

ENERGY_SPEC = {
    "CALC_FORMATION": ("true", "bool"),
    "CALC_INTERCALATION": ("true", "bool"),
    "MU": (None, "elemmap"),           # 形成能化学势：元素:能量(eV/原子)
    "GUEST_ELEMENT": (None, "str"),    # 嵌入能客体元素
    "MU_GUEST": (None, "float"),       # 客体化学势 eV/原子
    "HOST_ENERGY": (None, "float"),    # 宿主总能 eV（HOST_DIR 缺省时用）
    "HOST_DIR": (None, "str"),         # 宿主参考材料目录：自动读 E_host + 骨架组分并校验
    "HOST_FORMULA": (None, "elemmap"), # 手填 HOST_ENERGY 时声明骨架组分做校验，如 C:60
    # ---- [PATCH-UCONS] 跨步骤键：step1 的参数经三层合并会带进本步，声明但不消费 ----
    # 项目共用的 templates/step.conf 是所有步骤都会读到的那一层，stepconf 对
    # 未声明的键直接 SystemExit —— 不声明的话，把 FUNC 写在共用层就会打死 step3。
    "FUNC": (None, "str"), "CELL_POLICY": (None, "str"), "STD_CELL": (None, "str"),
    "VACUUM_AXIS_POLICY": (None, "str"), "STALL_MINUTES": (None, "int"),
    "MOL_KPOINTS": (None, "str"), "MOL_ISPIN": (None, "str"),
    "MOL_MOMENT": (None, "str"), "MOL_DIPOL": (None, "str"),
    "MOL_ENCUT_FLOOR": (None, "str"), "MOL_ALLOW_3D_TPL": (None, "str"),
    "AUTO_U": (None, "str"), "U_OVERRIDE": (None, "elemmap"),
    "U_ANION_GATE": (None, "bool"), "U_GATE_ANIONS": (None, "words"),
}


def read_poscar(path: Path):
    """读 POSCAR -> (元素符号列表, 各元素原子数)。"""
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    if len(lines) < 7:
        raise SystemExit("[ERROR] %s 行数不足" % path)
    tokens = lines[5].split()
    if tokens and tokens[0].lstrip("-").isdigit():
        raise SystemExit("[ERROR] %s 无元素符号行（VASP4 格式），无法定组分" % path)
    return tokens, [int(x) for x in lines[6].split()]


def read_total_energy(outcar: Path):
    """读 OUTCAR 总能，优先 energy(sigma->0)，回退 free energy TOTEN。"""
    text = outcar.read_text(errors="ignore")
    m = re.findall(r"energy\(sigma->0\)\s*=\s*([-+0-9.Ee]+)", text)
    if m:
        return float(m[-1]), "energy(sigma->0)"
    m = re.findall(r"free\s+energy\s+TOTEN\s*=\s*([-+0-9.Ee]+)", text)
    if m:
        return float(m[-1]), "free energy TOTEN"
    raise SystemExit("[ERROR] %s 里找不到总能" % outcar)


def read_func(method_file: Path):
    """从 workflow_method.txt 读 FUNC=；无则 None。"""
    if method_file and method_file.is_file():
        for line in method_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("FUNC="):
                return line.split("=", 1)[1].strip()
    return None


_LDAU_KEYS = ("LDAU", "LDAUTYPE", "LDAUL", "LDAUU", "LDAUJ")


def read_ldau(step_dir):
    """-> (on, 描述, 来源)。on 为 None 表示判断不了。

    优先读该步的 INCAR —— 算什么就是什么；没有 INCAR 时回退读
    workflow_method.txt 的 LDAU= 行（新版 gen_step1 会写）。
    """
    incar = Path(step_dir) / "INCAR"
    if incar.is_file():
        vals = {}
        for line in incar.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = line.split("#", 1)[0].split("!", 1)[0].strip()
            if "=" not in s:
                continue
            k, v = s.split("=", 1)
            k = k.strip().upper()
            if k in _LDAU_KEYS:
                vals[k] = v.strip()
        if not vals.get("LDAU", "").upper().lstrip(".").startswith("T"):
            return False, "off", "INCAR"
        return True, ("LDAUL=[%s] LDAUU=[%s] LDAUTYPE=%s"
                      % (vals.get("LDAUL", "?"), vals.get("LDAUU", "?"),
                         vals.get("LDAUTYPE", "2"))), "INCAR"
    mf = Path(step_dir) / METHOD_FILE
    if mf.is_file():
        for line in mf.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("LDAU="):
                v = line.split("=", 1)[1].strip()
                return (bool(v) and v != "off"), (v or "off"), METHOD_FILE
    return None, "未知（既无 INCAR 也无 workflow_method.txt 的 LDAU 记录）", "无"


def counts_map(symbols, counts):
    """[('C',60),('Li',2)] 之类 -> {'C':60,'Li':2}（同元素累加）。"""
    out = {}
    for s, n in zip(symbols, counts):
        out[s] = out.get(s, 0) + int(n)
    return out


def framework_counts(symbols, counts, guest):
    """当前结构去掉全部 guest 元素后的骨架组分 {元素: 数目}。"""
    return {s: n for s, n in counts_map(symbols, counts).items() if s != guest}


def fmt_counts(d):
    return "".join("%s%d" % (k, d[k]) for k in sorted(d)) or "(空)"


def read_host_reference(host_dir: Path):
    """从宿主参考材料目录读 (E_host, 骨架组分dict, FUNC或None)。
    host_dir 可指向材料目录（含 step2_static/）或直接指向 static 目录。"""
    cand = [host_dir / STEP2_DIR, host_dir]
    base = next((d for d in cand
                 if (d / "OUTCAR").is_file() and (d / "POSCAR").is_file()), None)
    if base is None:
        raise SystemExit("[ERROR] HOST_DIR=%s 里找不到 OUTCAR(+POSCAR)；"
                         "应指向宿主材料目录或其 step2_static/" % host_dir)
    e_host, _ = read_total_energy(base / "OUTCAR")
    hsym, hcnt = read_poscar(base / "POSCAR")
    return e_host, counts_map(hsym, hcnt), read_func(base / METHOD_FILE), base


def main():
    cwd = Path.cwd()
    step2 = cwd / STEP2_DIR
    if not step2.is_dir():
        sys.exit("[ERROR] 找不到 %s —— 先让 S2_static 算完" % STEP2_DIR)
    outcar = step2 / "OUTCAR"
    poscar = step2 / "POSCAR"
    for p in (outcar, poscar):
        if not p.is_file():
            sys.exit("[ERROR] 缺少 %s" % p)

    conf = stepconf.load(ENERGY_SPEC, "step3_energy")
    etot, src = read_total_energy(outcar)
    symbols, counts = read_poscar(poscar)
    natoms = sum(counts)
    formula = "".join(("%s%d" % (s, n)) if n > 1 else s
                      for s, n in zip(symbols, counts))

    result = {
        "formula": formula,
        "natoms": natoms,
        "E_tot_eV": round(etot, 8),
        "E_tot_source": src,
        "E_per_atom_eV": round(etot / natoms, 8),
    }

    # ---- [PATCH-UCONS] 本体系实际用的 U（决定各种参考能量可不可比）----
    u_on, u_desc, u_src = read_ldau(step2)
    result["LDAU"] = u_desc
    result["LDAU_source"] = u_src

    # ---- 形成能 E_form = E_tot - Σ n_i·μ_i ----
    if conf["CALC_FORMATION"]:
        mu = conf["MU"]
        if not mu or any(s not in mu for s in symbols):
            result["E_form_eV"] = None
            result["E_form_per_atom_eV"] = None
            result["E_form_note"] = ("未算：step3 step.conf 缺 MU（或 MU 没覆盖所有元素）。"
                                     "例：MU = C:-9.0 Li:-1.9")
        else:
            e_form = etot - sum(n * mu[s] for s, n in zip(symbols, counts))
            result["E_form_eV"] = round(e_form, 8)
            result["E_form_per_atom_eV"] = round(e_form / natoms, 8)
            result["E_form_note"] = "E_tot - Σ n_i·μ_i；μ=%s" % mu
            if u_on:
                warn = ("本体系带 DFT+U（%s）。E_form 只在 μ_i 也用【同一套 U】"
                        "算出来时才成立——元素单质通常不含 O/F，阴离子门控会让它"
                        "不加 U，两者能量零点不同，误差可达每个过渡金属原子 ~1 eV。"
                        "正确做法是用 Wang/Maxisch/Ceder PRB 73,195107 那套拟合过的"
                        "元素参考态能量（MP 用的就是它），而不是裸单质 PBE 总能。"
                        % u_desc)
                print("[WARN] " + warn)
                result["E_form_warning"] = warn
            elif u_on is None:
                result["E_form_warning"] = (
                    "判断不了本体系有没有加 U（step2_static 里既无 INCAR 也无 "
                    "workflow_method.txt）；若加了 U，E_form 与不带 U 的 μ 不可比。")

    # ---- 嵌入能 E_embed = E_tot - E_host - n_g·μ_g ----
    if conf["CALC_INTERCALATION"]:
        guest = conf["GUEST_ELEMENT"]
        mu_g = conf["MU_GUEST"]
        e_host = conf["HOST_ENERGY"]
        n_g = counts[symbols.index(guest)] if (guest and guest in symbols) else None

        # 宿主参考：HOST_DIR 优先（自动读能量+组分+泛函），否则用手填 HOST_ENERGY
        host_counts = None       # 宿主骨架组分（不含客体），用于一致性校验
        host_func = None
        host_src = None
        host_base = None         # [PATCH-UCONS] 宿主 static 目录，供比对 U
        if conf["HOST_DIR"]:
            (e_host_read, host_counts, host_func,
             host_base) = read_host_reference(Path(conf["HOST_DIR"]))
            if e_host is not None and abs(e_host - e_host_read) > 1e-6:
                print("[WARN] HOST_ENERGY(%.6f) 与 HOST_DIR 读到的(%.6f) 不一致，"
                      "以 HOST_DIR 为准。" % (e_host, e_host_read))
            e_host, host_src = e_host_read, "HOST_DIR"
            host_counts = {k: v for k, v in host_counts.items() if k != guest}
        elif conf["HOST_FORMULA"]:
            host_counts = {s: int(round(v)) for s, v in conf["HOST_FORMULA"].items()
                           if s != guest}
            host_src = "HOST_FORMULA"

        if not guest or e_host is None or mu_g is None or n_g is None:
            result["E_embed_eV"] = None
            result["E_embed_per_guest_eV"] = None
            result["E_embed_note"] = (
                "未算：缺 GUEST_ELEMENT / MU_GUEST / (HOST_DIR 或 HOST_ENERGY)，或客体不在结构里")
        else:
            # ★ 骨架一致性校验：当前结构去掉全部 guest 后，须与宿主逐元素相等
            fw = framework_counts(symbols, counts, guest)
            if host_counts is not None:
                if fw != host_counts:
                    sys.exit(
                        "[ERROR] 宿主骨架不一致，嵌入能无意义，已终止：\n"
                        "        当前结构去掉 %s 后 = %s\n"
                        "        宿主参考(%s)       = %s\n"
                        "        —— E_host 必须是同一宿主骨架、同一套设置"
                        "（泛函/赝势/ENCUT/k 网格）算的总能；宿主里不应含客体 %s。"
                        % (guest, fmt_counts(fw), host_src, fmt_counts(host_counts), guest))
                frame_note = "骨架已校验(%s)" % host_src
                # 软校验：泛函是否一致（不一致不终止，仅告警）
                if host_func:
                    my_func = read_func(step2 / METHOD_FILE)
                    if my_func and host_func != my_func:
                        print("[WARN] 宿主泛函(%s) 与本体系(%s) 不一致，嵌入能不可比。"
                              % (host_func, my_func))
                # [PATCH-UCONS] U 也要一致——原来只比泛函，U 不同一样不可比
                if host_base is not None:
                    h_on, h_desc, _ = read_ldau(host_base)
                    if (h_on is not None and u_on is not None
                            and (h_on != u_on or (h_on and h_desc != u_desc))):
                        msg = ("宿主 U 设置(%s) 与本体系(%s) 不一致，嵌入能不可比"
                               % (h_desc, u_desc))
                        print("[WARN] " + msg)
                        result["E_embed_warning"] = msg
            else:
                frame_note = "骨架未校验（没填 HOST_DIR/HOST_FORMULA）"

            e_embed = etot - e_host - n_g * mu_g
            result["E_embed_eV"] = round(e_embed, 8)
            result["E_embed_per_guest_eV"] = round(e_embed / n_g, 8)
            result["E_host_eV"] = round(e_host, 8)
            result["E_embed_note"] = ("E_tot - E_host - %d·μ_%s；%s"
                                      % (n_g, guest, frame_note))

    out = cwd / OUT_DIR
    out.mkdir(exist_ok=True)
    (out / OUT_FILE).write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n")
    print("[DONE] %s 已生成：%s" % (OUT_FILE, json.dumps(result, ensure_ascii=False)))


if __name__ == "__main__":
    main()