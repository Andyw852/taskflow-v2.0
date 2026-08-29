#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""convex_hull.py —— 第0步：目标化合物的化学势稳定性窗口（凸包）。

纯标准库，登录节点可跑。读各相总能（元素相 + 竞争二元相 + 目标化合物），
算出目标化合物能稳定存在的化学势窗口，产出 chemical_potential_window.json，
并可写 energies.json（形成能脚本的输入）。

输入 phases.json：
  { "target": {"formula": {"Sn":2,"Sb":1,"Te":5}, "E": -xx.xx},   # 目标化合物(每胞)总能
    "elements": {"Sn": -a, "Sb": -b, "Te": -c},                   # 元素相总能(每原子)
    "phases": [ {"formula": {"Sn":1,"Te":1}, "E": -yy}, ... ] }   # 竞争相(每式量)总能

方法（Δμ_i = μ_i − E_i(元素相)，Δμ_i ≤ 0）：
  平衡：      Σ_i n_i(目标)·Δμ_i = ΔH_f(目标)
  竞争相稳定：Σ_i n_i(相)·Δμ_i ≤ ΔH_f(相)
三元体系消去一个变量 → 2D 凸多边形顶点枚举。
"""
import sys, os, json
from pathlib import Path

TOL = 1e-7

def load_phases(path="phases.json"):
    if not os.path.exists(path):
        raise SystemExit("[错误] 找不到 %s —— 请先算好各相总能（README §0）" % path)
    return json.load(open(path, encoding="utf-8"))

def formation_energy(formula, E, elements):
    """ΔH_f = E(相) − Σ_i n_i·E_i(元素相每原子)。"""
    return E - sum(n * elements.get(el, 0.0) for el, n in formula.items())

def solve_2x2(a1, b1, c1, a2, b2, c2):
    """解 a1*x+b1*y=c1, a2*x+b2*y=c2；无解返回 None。"""
    det = a1*b2 - a2*b1
    if abs(det) < 1e-12:
        return None
    x = (c1*b2 - c2*b1) / det
    y = (a1*c2 - a2*c1) / det
    return x, y

def convex_hull_window(target, elements, phases):
    """三元化学势窗口：返回 (顶点列表[(x,y,Δμdict)], 约束列表)。"""
    els = list(elements.keys())
    if len(els) != 3:
        raise SystemExit("[错误] 当前只支持三元体系（%d 元），二元/四元暂未实现" % len(els))
    A, B, C = els[0], els[1], els[2]
    nA, nB, nC = (target["formula"].get(A, 0), target["formula"].get(B, 0),
                  target["formula"].get(C, 0))
    if nC == 0:
        raise SystemExit("[错误] 第三个元素在目标中原子数为 0，请调整元素顺序")
    dH_target = formation_energy(target["formula"], target["E"], elements)
    # 约束 (α, β, γ): α*x + β*y ≤ γ，x=Δμ_A, y=Δμ_B
    cons = []
    # Δμ_A ≤ 0, Δμ_B ≤ 0
    cons.append(("A≤0", 1.0, 0.0, 0.0))
    cons.append(("B≤0", 0.0, 1.0, 0.0))
    # Δμ_C ≤ 0  =>  nA*x + nB*y ≥ dH_target  =>  -nA*x - nB*y ≤ -dH_target
    cons.append(("C≤0", -nA, -nB, -dH_target))
    # 竞争相:  a*x + b*y + c*Δμ_C ≤ dH_phase
    #   Δμ_C = (dH_target - nA*x - nB*y)/nC
    #   => (a - c*nA/nC)*x + (b - c*nB/nC)*y ≤ dH_phase - c*dH_target/nC
    for ph in phases:
        fa, fb, fc = (ph["formula"].get(A, 0), ph["formula"].get(B, 0),
                      ph["formula"].get(C, 0))
        dH = formation_energy(ph["formula"], ph["E"], elements)
        alpha = fa - fc*nA/nC
        beta = fb - fc*nB/nC
        gamma = dH - fc*dH_target/nC
        cons.append((ph.get("name", "phase"), alpha, beta, gamma))
    # 顶点枚举：两两约束边界求交，检查可行性
    verts = []
    for i in range(len(cons)):
        for j in range(i+1, len(cons)):
            _, a1, b1, c1 = cons[i]
            _, a2, b2, c2 = cons[j]
            sol = solve_2x2(a1, b1, c1, a2, b2, c2)
            if sol is None:
                continue
            x, y = sol
            if all(a*x + b*y <= c + TOL for _, a, b, c in cons):
                verts.append((x, y))
    if len(verts) < 3:
        raise SystemExit("[错误] 凸包窗口为空/退化（%d 个顶点）—— 检查相总能是否有误" % len(verts))
    # 按重心角排序成凸多边形
    cx = sum(v[0] for v in verts)/len(verts)
    cy = sum(v[1] for v in verts)/len(verts)
    import math
    verts = sorted(verts, key=lambda v: math.atan2(v[1]-cy, v[0]-cx))
    # 每个顶点回代 Δμ_C，得到完整 Δμ dict
    out = []
    for x, y in verts:
        dC = (dH_target - nA*x - nB*y)/nC
        dmu = {A: x, B: y, C: dC}
        mu = {el: elements[el] + dmu[el] for el in els}
        out.append({"dmu": {el: round(v, 6) for el, v in dmu.items()},
                    "mu": {el: round(v, 6) for el, v in mu.items()}})
    return out, dH_target

def main():
    ph = load_phases()
    verts, dH_target = convex_hull_window(ph["target"], ph["elements"], ph["phases"])
    els = list(ph["elements"].keys())
    print("[OK] 目标化合物 ΔH_f = %.4f eV/式量" % dH_target)
    print("     化学势窗口（%d 个顶点，μ 为绝对值 eV/原子）:" % len(verts))
    for k, v in enumerate(verts):
        mu = "  ".join("%s=%.4f" % (el, v["mu"][el]) for el in els)
        print("       顶点%d: %s" % (k, mu))
    json.dump({"target": ph["target"]["formula"], "dH_f": round(dH_target, 6),
               "window_vertices": verts},
              open("chemical_potential_window.json", "w"), indent=2, ensure_ascii=False)
    print("     窗口已写 chemical_potential_window.json")
    # 默认取第一个顶点（可用 --vertex N 覆盖）写入 energies.json 供形成能脚本用
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--vertex", type=int, default=0, help="用哪个顶点作为默认化学势")
    args, _ = ap.parse_known_args()
    if 0 <= args.vertex < len(verts):
        ref = {"mu": verts[args.vertex]["mu"]}
        if os.path.exists("energies.json"):
            ref.update(json.load(open("energies.json", encoding="utf-8")))
        json.dump(ref, open("energies.json", "w"), indent=2, ensure_ascii=False)
        print("     已写 energies.json（mu 取顶点%d；E_gap/epsilon 请手动补）" % args.vertex)


def read_energy(outcar):
    """取 OUTCAR 最后 without entropy（E0）。"""
    if not os.path.exists(outcar):
        return None
    e = None
    for line in open(outcar, errors="ignore"):
        if "without entropy" in line:
            try:
                e = float(line.split("=")[1].split()[0])
            except (ValueError, IndexError):
                pass
    return e


def build_phases_from_references(ref_json="references_energy.json",
                                 bulk_poscar="step1_bulk/POSCAR",
                                 bulk_outcar="step1_bulk/OUTCAR"):
    """由 step0 的 references_energy.json + step1_bulk 组装凸包输入。

    目标化合物式量 = 超胞成分 / gcd（如 Sn18 Sb18 Te45 -> Sn2 Sb2 Te5），
    每式量总能 = 超胞总能 × (式量原子数 / 超胞原子数)。
    只取与本材料相关的元素/二元相。"""
    ref = json.load(open(ref_json, encoding="utf-8"))
    import defects_common as D2
    st = D2.parse_poscar(bulk_poscar)
    counts = {}
    for a in st["atoms"]:
        counts[a] = counts.get(a, 0) + 1
    from math import gcd
    from functools import reduce
    g = reduce(gcd, counts.values())
    formula = {el: n // g for el, n in counts.items()}
    n_super = sum(counts.values())
    n_prim = sum(formula.values())
    E_super = read_energy(bulk_outcar)
    if E_super is None:
        raise SystemExit("[错误] step1_bulk/OUTCAR 无能量（先跑完 step1）")
    E_per_fu = E_super * (n_prim / n_super)
    els = set(formula)
    elements = {el: v for el, v in ref["elements"].items() if el in els}
    phases = [{"name": n, "formula": i["formula"], "E": i["E_per_fu"]}
              for n, i in ref["binaries"].items() if set(i["formula"]).issubset(els)]
    return {"target": {"formula": formula, "E": E_per_fu},
            "elements": elements, "phases": phases}


def run_from_references():
    """S4 调用：references_energy.json + step1_bulk -> 化学势窗口 -> energies.json。"""
    ref_json = None
    for cand in ("step0_references/references_energy.json", "references_energy.json"):
        if os.path.exists(cand):
            ref_json = cand
            break
    if ref_json is None:
        raise SystemExit("[错误] 找不到 references_energy.json（step0 参考相未完成）")
    ph = build_phases_from_references(ref_json=ref_json)
    verts, dH_target = convex_hull_window(ph["target"], ph["elements"], ph["phases"])
    els = list(ph["elements"].keys())
    print("[OK] %s ΔH_f = %.4f eV/式量，化学势窗口 %d 个顶点"
          % ("".join("%s%d" % (e, ph["target"]["formula"].get(e, 0)) for e in els),
             dH_target, len(verts)))
    for k, v in enumerate(verts):
        print("   顶点%d: %s" % (k, "  ".join("%s=%.4f" % (el, v["mu"][el]) for el in els)))
    json.dump({"target": ph["target"]["formula"], "dH_f": round(dH_target, 6),
               "window_vertices": verts},
              open("chemical_potential_window.json", "w"), indent=2, ensure_ascii=False)
    # 取 Te 最富的顶点（min Δμ_Te = μ_Te 最接近元素 Te）作为默认化学势
    # 实际形成能应在多个极端化学势下算（Te 富/Te 贫），这里默认取第一个顶点，用户可改
    ref = json.load(open("energies.json", encoding="utf-8")) if os.path.exists("energies.json") else {}
    ref["mu"] = verts[0]["mu"]
    json.dump(ref, open("energies.json", "w"), indent=2, ensure_ascii=False)
    print("     已写 energies.json（mu 取顶点0；E_gap/epsilon/mstar 若缺请补）")


if __name__ == "__main__":
    main()
