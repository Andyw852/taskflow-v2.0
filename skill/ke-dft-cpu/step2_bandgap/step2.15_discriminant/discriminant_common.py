#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""discriminant_common.py —— 体系判别的物理核心（被 check / decide / 下游 gen 共用）

判据（必须用这一条，不能用 OUTCAR 的 fundamental gap 行）
--------------------------------------------------------------------------
    nocc = NELECT // 2
    gap  = min_k E[nocc](k) - max_k E[nocc-1](k)      （列表 0-based 下标）
    gap <= 0            -> SEMIMETAL   （带重叠）
    0 < gap < 0.05 eV   -> METAL       （近金属/窄隙）
    gap >= 0.05 eV      -> SEMICONDUCTOR

实测同一份 OUTCAR（Mg4C60_relax3 step2.1_static, V=1228.1221, 4x4x2,
NBANDS=324, NELECT=496）：
    带指标法            = -0.0765 eV   <- 真值
    占据数法(VASP 原生) = +0.0512 eV   <- 有带重叠时该估计量失效
机理：Gamma 点上只有 247 个带满占据、1 个带部分占据，E[248]@Gamma 被占据阈值
划成"空态"，而 E[249]@(0.5,0,0.5) 更低却是占据的。凡读那一行的下游脚本都会
把半金属误报成有隙。

泛函标签与单侧失效（重要）
--------------------------------------------------------------------------
标签必须带泛函：**PBE 判 METAL 不等于材料是金属。**
    PBE 判 SEMICONDUCTOR      -> 真实几乎必定也是（PBE 只会低估带隙）-> 可作结
    PBE 判 SEMIMETAL / METAL  -> **待定，不是结论**
所以 effective.decisive 只在"泛函非 PBE"或"判为 SEMICONDUCTOR"时为 True；
阻断措辞必须体现"待定"。跑完杂化后用 --functional PBE0 重判即可覆盖。
"""

import json
import re
from pathlib import Path

OUT_JSON = "discriminant.json"
GAP_SEMI_MAX = 0.05      # < 此值 -> 非 SEMICONDUCTOR
NARROW_GAP = 0.30        # SEMICONDUCTOR 且 < 此值 -> 追加"同一段连续能带"检查
# 阻断名单：**只挡"半导体框架下才有意义"的步骤**。
#   step5_dielect : DFPT 介电/极性光学声子 —— 金属里静态介电无定义，算了也没用
#   step8_amset   : AMSET 要求有隙（REQUIRE_BANDGAP=True），金属体系根本跑不了
# ★ 不能挡 step8.1_boltztrap —— 它正是金属该走的出路（CRTA，固定 tau 估 S/sigma/kappa_e）。
#   挡了它等于挡了自己推荐的路线。
# 也不挡 step6_elastic / step7_deform —— 弹性常数对金属照样有意义（且 kappa_L 与
#   声子线独立），形变势在金属里虽不用于 AMSET 但跑了无害。
BLOCKED_STEPS = ["step5_dielect", "step8_amset"]

KPAT = re.compile(r"^\s*k-point\s+(\d+)\s*:\s*([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s*$")
BPAT = re.compile(r"^\s*(\d+)\s+(-?\d+\.\d+)\s+(\d+\.\d+)\s*$")


def parse_outcar_meta(outcar):
    txt = Path(outcar).read_text(errors="ignore")
    meta = {}
    for key, pat in (("nelect", r"NELECT\s*=\s*([-\d.]+)"),
                     ("nbands", r"NBANDS=\s*(\d+)"),
                     ("encut", r"ENCUT\s*=\s*([-\d.]+)"),
                     ("efermi", r"E-fermi\s*:\s*([-\d.]+)")):
        m = re.search(pat, txt)
        if m:
            meta[key] = float(m.group(1))
    m = re.search(r"GGA\s*=\s*(\S+)", txt)
    meta["gga"] = m.group(1) if m else "PE"
    meta["lhfcalc"] = bool(re.search(r"LHFCALC\s*=\s*T", txt))
    m = re.search(r"HFSCREEN\s*=\s*([-\d.]+)", txt)
    meta["hfscreen"] = float(m.group(1)) if m else None
    m = re.search(r"AEXX\s*=\s*([-\d.]+)", txt)
    meta["aexx"] = float(m.group(1)) if m else None
    meta["soc"] = bool(re.search(r"LSORBIT\s*=\s*T", txt))
    # 非共线（SOC 属于非共线）：每个 KS 态是二分量旋量，只装 1 个电子，
    # 所以占据带数 = NELECT，**不是 NELECT/2**。实测 vasp_ncl 的 SOC 静态：
    # NELECT=68 而 NBANDS=96（VASP 自动把 NBANDS 翻倍），占据带数就是 68。
    # 用 NELECT//2 会去量两条深占据带之间的间隔，SOC 带隙全错。
    meta["noncollinear"] = bool(meta["soc"]) or bool(
        re.search(r"LNONCOLLINEAR\s*=\s*T", txt))
    return meta, txt


class DiscriminantParseError(RuntimeError):
    """解析不出带结构时的**硬错误**（不许静默返回 None）。

    本链路上已经出现两次同性质的静默失败：
      1) OUTCAR 的 "fundamental gap" 行在带重叠时给正值，下游照用 -> 半金属报成有隙；
      2) parse_kblocks 的过滤阈值写成 >=100 条带，把 NBANDS=96 的 Pb2Sb2Te5 整个滤掉，
         调用方拿到 None 就当"没有判别结果"继续走。
    所以解析失败必须带诊断信息抛出来，由调用方**显式**决定要不要降级
    （gen_step4_HSE 的向后兼容降级是显式 catch，不是默默继续）。
    """


def parse_kblocks(txt):
    """取最后一组连续 k 点本征值块（每块 >=100 条带）。"""
    grp, cur = [], None
    for ln in txt.splitlines():
        m = KPAT.match(ln)
        if m:
            cur = (int(m.group(1)),
                   (float(m.group(2)), float(m.group(3)), float(m.group(4))), [])
            grp.append(cur)
            continue
        if cur is not None:
            b = BPAT.match(ln)
            if b:
                cur[2].append((int(b.group(1)), float(b.group(2)), float(b.group(3))))
    # 阈值只用来排除"头部 k 点列表"这种没有带行的块；KPAT 本身就要求行尾紧接
    # 三个浮点（头部那行后面跟 "plane waves: NN"，匹配不上），所以这里用 5 就够。
    # ★ 不能用大阈值：9 原子小胞的 NBANDS 只有 40~120，阈值 100 会把
    #   Pb2Sb2Te5(NBANDS=96) 这类体系整个滤掉 —— 判别器恰好在最需要它的体系上失效。
    good = [g for g in grp if len(g[2]) >= 5]
    # 只取**最后一组**连续 k 点（最后一段完整的 SCF 本征值列表）。同一 OUTCAR 里
    # 可能有多段（每次电子迭代一段），取最后一组即可；k 点编号在同一组内是 1..N。
    seen, out = set(), []
    for g in reversed(good):
        if g[0] in seen:
            break
        seen.add(g[0])
        out.append(g)
    return list(reversed(out))


def functional_label(meta, force=None):
    if force:
        return force
    if meta.get("lhfcalc"):
        hs = meta.get("hfscreen")
        if hs is not None and abs(hs) < 1e-9:
            return "PBE0"
        if hs is not None and abs(hs - 0.2) < 1e-6:
            return "HSE06"
        return "HSE%.3f" % (hs or 0.0)
    return "PBE"


def decide_from_outcar(outcar, mesh=None, functional=None):
    """返回判定 dict；解析不出本征值块时返回 None。"""
    try:
        meta, txt = parse_outcar_meta(outcar)
    except Exception:
        return None
    blocks = parse_kblocks(txt)
    if not blocks:
        _hits = sum(1 for ln in txt.splitlines() if KPAT.match(ln))
        _msg = ("从 %s 解析不出带结构本征值块。诊断：KPAT 命中 %d 个 k 点块，"
                "其中带行数>=5 的有 0 个；OUTCAR 报 NBANDS=%s、NELECT=%s。"
                "常见原因：① 过滤阈值过大（9 原子小胞 NBANDS 常只有 40~120）；"
                "② OUTCAR 还没写到本征值段（SCF 未收敛）。"
                % (outcar, _hits, meta.get("nbands"), meta.get("nelect")))
        raise DiscriminantParseError(_msg)
    _ne = int(meta.get("nelect", 0))
    noncol = bool(meta.get("noncollinear"))
    nocc = _ne if noncol else _ne // 2      # 非共线：占据带数 = NELECT
    nb = len(blocks[0][2])
    if nocc <= 0 or nocc >= nb:
        raise DiscriminantParseError(
            "非共线占据带数 %d 不合理（NBANDS=%d，noncollinear=%s）。"
            "非共线时占据带数应等于 NELECT；共线时应等于 NELECT//2。"
            "不一致说明 NBANDS 没被正确翻倍，或本轮解析用了错的 nocc 规则。"
            % (nocc, nb, noncol))
    vb = max(g[2][nocc-1][1] for g in blocks)
    cb = min(g[2][nocc][1] for g in blocks)
    gap = cb - vb
    kb = max(blocks, key=lambda g: g[2][nocc-1][1])
    kc = min(blocks, key=lambda g: g[2][nocc][1])
    local = min(g[2][nocc][1] - g[2][nocc-1][1] for g in blocks)
    label = "SEMIMETAL" if gap <= 0 else ("METAL" if gap < GAP_SEMI_MAX else "SEMICONDUCTOR")
    d = {
        "label": label,
        "functional": functional_label(meta, functional),
        "gap_eV": round(gap, 6),
        "metric": "min_k E[nocc](k) - max_k E[nocc-1](k), nocc=NELECT//2 "
                  "(带指标法；不是 OUTCAR 的 fundamental gap 行)",
        "nocc": nocc, "nbands": nb, "nkpts": len(blocks),
        "nocc_rule": ("NELECT（非共线/SOC，旋量每态 1 电子）" if noncol
                      else "NELECT//2（共线，每态 2 电子）"),
        "nelect": meta.get("nelect"), "encut_eV": meta.get("encut"),
        "mesh": list(mesh) if mesh else None,
        "efermi_eV": meta.get("efermi"),
        "soc": meta.get("soc"),
        "vbm": {"E_eV": round(vb, 6), "k_frac": [round(x, 6) for x in kb[1]]},
        "cbm": {"E_eV": round(cb, 6), "k_frac": [round(x, 6) for x in kc[1]]},
        "thresholds": {"semicond_min_eV": GAP_SEMI_MAX, "narrow_gap_eV": NARROW_GAP},
        "outcar": str(outcar),
    }
    if label == "SEMICONDUCTOR" and gap < NARROW_GAP:
        same = local > 0
        d["narrow_gap_segment_check"] = {
            "same_continuous_segment": bool(same),
            "min_local_gap_eV": round(local, 6),
            "note": ("VBM 与 CBM 落在同一段连续能带上（逐 k 局部隙恒正）" if same else
                     "逐 k 局部隙出现非正 -> 带序在某个 k 上交换（带反转），"
                     "VBM/CBM 不属同一段连续能带，'隙'要谨慎解读"),
        }
    return d


def build_payload(verdicts):
    """把多条判定合并成 discriminant.json 的内容。"""
    eff = verdicts[0]
    for v in verdicts[1:]:
        if v.get("functional", "PBE") != "PBE" and v.get("label") == "SEMICONDUCTOR":
            eff = v
    decisive = not (eff.get("functional", "PBE") == "PBE"
                    and eff.get("label") != "SEMICONDUCTOR")
    label_full = "%s@%s" % (eff["label"], eff["functional"])
    blocked = (not decisive) and eff["label"] in ("SEMIMETAL", "METAL")
    block = {"blocked": blocked, "steps": BLOCKED_STEPS, "forced": False,
             "decisive": bool(decisive), "hint": ""}
    if blocked:
        block["hint"] = (
            "%s (gap = %+.3f eV, mesh %s)\n"
            "step5_dielect / step8_amset / step8.1_boltztrap 已阻断"
            "（半导体框架不适用）\n"
            "注意：这是 %s 级判别，单侧失效 —— PBE 只会低估带隙：\n"
            "  PBE 判 SEMICONDUCTOR -> 真实几乎必定也是（可作结）\n"
            "  PBE 判 SEMIMETAL/METAL -> **待定，不是结论**\n"
            "→ 若杂化泛函开出隙，重跑判定步并以 @杂化 结果为准：\n"
            "    python decide_discriminant.py --hybrid-outcar <杂化步>/OUTCAR\n"
            "→ 若确认为金属：**改走 step8.1_boltztrap**（CRTA，固定 tau 估 "
            "S / sigma / kappa_e）—— 注意它不在阻断名单里，可以直接跑：\n"
            "    step8.1_boltztrap 的 gen 不设 SCISSOR（金属没有隙可剪）\n"
            "→ 强制继续走半导体框架：在对应 gen 里设 FORCE_TRANSPORT = True\n"
            "   （金属走 AMSET 的结果不可用：无隙 -> 双极输运主导 + 散射模型失效）"
            % (label_full, eff["gap_eV"], eff.get("mesh"), eff.get("functional")))
    return {
        "step": "step2bandgap.discriminant",
        "label": label_full,
        "effective": {"label": eff["label"], "functional": eff.get("functional"),
                      "gap_eV": eff.get("gap_eV"), "decisive": bool(decisive)},
        "algo_recommendation": "All" if eff["label"] == "SEMICONDUCTOR" else "Damped",
        "verdicts": verdicts,
        "block": block,
        "note": ("algo_recommendation 供 step2.3 杂化步使用：SEMICONDUCTOR -> All，"
                 "SEMIMETAL/METAL -> Damped。block.blocked 只是默认拦截，不是硬停："
                 "下游 gen 的 FORCE_TRANSPORT=True 可强制继续。"),
    }


def read_discriminant(cwd, step_dir="step2_bandgap/step2.15_discriminant"):
    """下游 gen 用这个取判定；读不到返回 None（调用方应退回现状行为）。"""
    p = Path(cwd) / step_dir / OUT_JSON
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


GATE_STEPS = set(BLOCKED_STEPS)


def gate(cwd, step_name, force=False, step_dir="step2_bandgap/step2.15_discriminant"):
    """下游 gen 的统一闸门。返回 True=放行，False=拦截（调用方应 sys.exit）。

    语义（重要，别改成硬停）：
      * 读不到 discriminant.json -> **放行**（判别步没启用/没跑完时，行为与改动前
        完全一致，向后兼容）；
      * 判为 SEMICONDUCTOR，或判定为"可作结"（非 PBE 泛函下的任意结论）-> 放行；
      * SEMIMETAL/METAL 且只到 PBE 级 -> **默认拦截**，但 force=True
        （各 gen 顶部的 FORCE_TRANSPORT）可覆盖。
      * 拦截时打印带出路的提示（含"待定"措辞，因为 PBE 判金属是单侧失效）。
    """
    import sys as _sys
    p = Path(cwd) / step_dir / OUT_JSON
    if not p.is_file():
        print("[..] 无 %s：跳过体系判别阻断（向后兼容）" % p, file=_sys.stderr)
        return True
    try:
        d = json.loads(p.read_text())
    except Exception as e:
        print("[WARN] %s 解析失败，跳过阻断：%s" % (p, e), file=_sys.stderr)
        return True
    blk = d.get("block", {}) or {}
    if not blk.get("blocked"):
        print("[OK] 体系判别 %s（decisive=%s）-> %s 放行"
              % (d.get("label"), blk.get("decisive"), step_name), file=_sys.stderr)
        return True
    hint = blk.get("hint") or ("体系判别 %s：%s 已阻断" % (d.get("label"), step_name))
    if force:
        print("[WARN] FORCE_TRANSPORT=True —— 忽略体系判别阻断，强制继续。"
              "注意结果里应注明这是金属/半金属走半导体框架算的。", file=_sys.stderr)
        print(hint, file=_sys.stderr)
        return True
    print(hint, file=_sys.stderr)
    return False


def read_mesh(dirp):
    kp = Path(dirp) / "KPOINTS"
    if not kp.is_file():
        return None
    for ln in kp.read_text(errors="ignore").splitlines():
        t = ln.split()
        if len(t) == 3 and all(re.fullmatch(r"-?\d+(\.0+)?", x) for x in t):
            return [int(float(x)) for x in t]
    return None
