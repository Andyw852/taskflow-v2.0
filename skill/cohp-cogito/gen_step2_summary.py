#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_step2_summary.py —— S2_summary：本地汇总（run: gen，登录节点，秒级）

把 S1_COHP 的原始输出整理成结构化结果 + 中文摘要：
  icohp_table.json        全部键（距离 / ICOHP / ICOBI / 数量）
  icohp_short_long.json   短键-长键对大分化比
  icohp_report.md         中文摘要（可直接并入报告）
  cogito_summary.json     质量指标 + 键统计（供 tf 状态与后续引用）
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import cogito_common as cc  # noqa: E402

import re  # noqa: E402


def build_markdown(rows, pairs, quality, onsite) -> str:
    L = ["# COHP / ICOHP / ICOBI 摘要（COGITO）", ""]
    if quality:
        L += ["## 投影质量", "", "| 指标 | 数值 |", "|---|---|",
              f"| charge spilling | {quality.get('charge_spilling_percent', 'N/A')}% |",
              f"| avg orbital mixing | {quality.get('avg_orbital_mixing_percent', 'N/A')}% |",
              f"| max orbital mixing | {quality.get('max_orbital_mixing_percent', 'N/A')}% |",
              ""]
    if pairs:
        L += ["## 短键 / 长键分化", "",
              "| 键对 | 短键 (Å) | ICOHP 短 (eV) | 长键 (Å) | ICOHP 长 (eV) | 比值 | ΔICOHP (eV) |",
              "|---|---|---|---|---|---|---|"]
        for p in pairs:
            s, l = p["short"], p["long"]
            L.append(f"| {s['atom1']}–{s['atom2']} | {s['dist_ang']:.3f} | "
                     f"{s['icohp_ev_per_bond']:.4f} | {l['dist_ang']:.3f} | "
                     f"{l['icohp_ev_per_bond']:.4f} | {p['ratio']:.2f}× | "
                     f"{p['delta_icohp']:.4f} |")
        L.append("")
    if onsite:
        L += ["## Löwdin 轨道占据（onsite_occup 逐原子平均）", "",
              "| 元素 | n | ns | np | (n−1)d |", "|---|---|---|---|---|"]
        for el in sorted(onsite):
            a = onsite[el]
            L.append(f"| {el} | {a['n']} | {a['s']:.3f} | {a['p']:.3f} | {a['d']:.3f} |")
        L.append("")
    L += ["## 全部成键（ICOHP < 0）", "",
          "| 键对 | 距离 (Å) | ICOHP (eV/bond) | ICOBI (elec/bond) | 数量/胞 |",
          "|---|---|---|---|---|"]
    for r in rows:
        if r["icohp_ev_per_bond"] < 0:
            L.append(f"| {r['atom1']}–{r['atom2']} | {r['dist_ang']:.3f} | "
                     f"{r['icohp_ev_per_bond']:.4f} | {r['icobi_elec_per_bond']:.4f} | "
                     f"{r['count_per_cell']:.0f} |")
    L += ["", "> 负 ICOHP = 成键；正值 = 反键/排斥。跨材料比较比绝对值更可靠。", ""]
    return "\n".join(L)



def make_plot(traces, out):
    if not traces: return False
    try:
        import matplotlib; matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as exc:
        print(f"[警告] 未安装 matplotlib，静态图未生成：{exc}", file=sys.stderr)
        return False
    fig,ax=plt.subplots(figsize=(8.5,6),constrained_layout=True)
    for t in traces: ax.plot(t["x"],t["y"],lw=1.0,label=t["name"] or "COHP")
    ax.axvline(0,color="#555",lw=.8); ax.axhline(0,color="#999",lw=.8)
    ax.set(xlabel="E − E$_F$ (eV)",ylabel="COHP",title="COHP from COGITO numeric output")
    if len(traces)<=12: ax.legend(fontsize=7,ncol=2,frameon=False)
    fig.savefig(out,dpi=220); plt.close(fig); return True

def read_onsite(directory: Path) -> dict:
    """从 all_atoms.json 汇总 onsite_occup（Löwdin 轨道占据）。"""
    p = directory / "all_atoms.json"
    if not p.is_file():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    agg: dict[str, list] = {}
    for v in d.values():
        el = v.get("elem")
        oc = v.get("onsite_occup", {})
        s = oc["s"][0] if isinstance(oc.get("s"), list) and oc["s"] else 0.0
        pp = sum(oc["p"]) if isinstance(oc.get("p"), list) else 0.0
        dl = oc.get("d", [])
        dd = sum(dl) if dl and isinstance(dl[0], (int, float)) else 0.0
        agg.setdefault(el, []).append((s, pp, dd))
    return {el: {"n": len(v), "s": sum(x[0] for x in v) / len(v),
                 "p": sum(x[1] for x in v) / len(v),
                 "d": sum(x[2] for x in v) / len(v)}
            for el, v in agg.items() if el}


def main() -> int:
    # run:gen 步骤在技能目录下执行。输入在 S1 的 step1_cohp/，
    # 输出必须写到与步骤同名的 step2_summary/（tf 按步骤目录找 done_marker）。
    src = Path(os.getcwd()) / "step1_cohp"
    if not src.is_dir():
        src = Path(os.getcwd())
    out = Path(os.getcwd()) / "step2_summary"
    out.mkdir(parents=True, exist_ok=True)
    rows = cc.parse_bond_info(src)
    if not rows:
        print("[错误] 未找到 bond_info.txt；S1_COHP 可能未成功", file=sys.stderr)
        return 1

    pairs = []
    seen = set()
    for r in rows:
        a, b = r["atom1"], r["atom2"]
        if "Te" not in (a, b):
            continue
        key = tuple(sorted((a, b)))
        if key in seen:
            continue
        seen.add(key)
        sl = cc.find_short_long(rows, (a, b))
        if not sl:
            continue
        s, l = sl["short"], sl["long"]
        ratio = (s["icohp_ev_per_bond"] / l["icohp_ev_per_bond"]
                 if l["icohp_ev_per_bond"] else float("nan"))
        pairs.append({"pair": f"{a}-{b}", "short": s, "long": l,
                      "ratio": ratio,
                      "delta_icohp": s["icohp_ev_per_bond"] - l["icohp_ev_per_bond"]})

    quality = cc.read_quality(src)
    onsite = read_onsite(src)
    traces = cc.parse_cohp_html(src)
    n_points = cc.write_cohp_csv(traces, out / "cohp_traces.csv") if traces else 0
    if traces: make_plot(traces, out / "cohp_plot.png")

    (out / "icohp_table.json").write_text(
        json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    (out / "icohp_short_long.json").write_text(
        json.dumps(pairs, indent=2, ensure_ascii=False), encoding="utf-8")
    (out / "icohp_report.md").write_text(
        build_markdown(rows, pairs, quality, onsite), encoding="utf-8")

    summary = {"step": "step2_summary", "status": "done",
               "quality": quality, "n_bonds": len(rows),
               "n_cohp_traces": len(traces), "n_cohp_points": n_points,
               "onsite_occup": onsite,
               "short_long": [{"pair": p["pair"], "ratio": p["ratio"],
                               "delta_icohp": p["delta_icohp"]} for p in pairs]}
    (out / "cogito_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"[OK] 键数={len(rows)}，短/长键对={len(pairs)}，元素={len(onsite)}")
    for p in pairs:
        print(f"     {p['pair']}: {p['short']['dist_ang']:.3f}Å "
              f"{p['short']['icohp_ev_per_bond']:.4f} eV  vs  "
              f"{p['long']['dist_ang']:.3f}Å {p['long']['icohp_ev_per_bond']:.4f} eV "
              f"（比值 {p['ratio']:.2f}×）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
