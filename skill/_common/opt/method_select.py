#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
method_select.py — 计算泛函/色散方法选择 + 方法卡(指纹) + 物理约束校验（公共池共享模块）
=====================================================================================
本模块给"结构优化/静态"类步骤提供三段职责，供各 gen_stepN / 后续工具共用（写一份）：

  1) default_method(structure, struct_dim, ...) -> (func, reason)
     按【键网络维度 / 组分】决定泛函 FUNC（与 relax_common 的 FUNC 词汇一致：
     pbe-d3 / pbesol / pbe）。决策规则（自上而下，先命中先返回）：
        a) 参照/吸附体系（uses_molecular_reference / has_adsorbate）      -> pbe-d3
        b) 异常保守：结构为空 / pymatgen 缺失 / 键网络分析失败等无法判定     -> pbe-d3
        c) bond_dim < struct_dim（低维键网络埋在更高维胞里，层/链/分子间隙） -> pbe-d3
        d) n_components > 1（多组分 = 弱结合碎片 / 分子晶体）               -> pbe-d3
        e) 其余（键网络维度 == 结构维度 且 单组分）                        -> pbesol
    关于 e) 的措辞：**同维 / 等维连通并不证明体系是纯共价**——它只说明按
    CrystalNN/Larsen 键网络判据没有测到低于胞维的弱结合组分，pbesol 只是
    "未发现需用色散修正证据"时的默认选择；reason 里会写明这一限制，绝不宣称
    "等维 => 纯共价"。若后续出现吸附 / 分子参照 / 层间弱作用证据应改用 pbe-d3。

    键网络维度/组分分析走 pymatgen：
        CrystalNN().get_bonded_structure(structure)
            -> get_structure_components(bonded_structure)   # 各组分维度
            -> get_dimensionality_larsen(bonded_structure)  # 全结构维度(取最高组分)
    这些只在调用时惰性导入；本机没装 pymatgen 时模块照常可用，仅分析路径
    返回 ok=False（测试用 mock 顶替，不要求本机装 pymatgen）。

  2) 方法卡 / 指纹 API
     方法卡 = 一份记录"某目录的计算方法是什么"的 JSON 卡，卡内固化：
        func / reason / struct_dim / bond_dim / n_components
        方法关键 INCAR 标签(GGA IVDW ENCUT PREC LASPH + 杂化参数集)的 typed 快照
        POTCAR 的 SHA1（**流式**算，不整文件读入内存）
    再加一个对"方法身份字段"的指纹(sha256)，用来防篡改：
        make_method_card(directory, func, reason, ...)  生成卡(自动读 INCAR/POTCAR)
        write_method_card(card)                         落盘为 method_card.json
        read_incar(directory)                           读 INCAR 并做类型标准化
        assert_method_consistent(current, upstream, step_name, allow=None)
          对比两张卡（可传 dict / 卡文件路径 / 卡目录）：
            · 每张卡先自检指纹——卡内容被改过(源卡被篡改/手工改过) -> MethodCardTamperError
            · 再逐字段对比方法身份；不一致默认抛 MethodInconsistencyError
            · 只接受"非空理由"豁免：allow 给理由(str 全局 / dict 按字段)才放行，
              豁免会被记录(返回的 waivers + 落盘 method_card_waivers.jsonl)
        make_method_card 里 func 与 INCAR 冲突会直接报错，防止卡片说谎。

  3) validate_physical_constraints(incar, struct_dim=None, vacuum_constrained=False)
     物理参数自洽校验，返回违规清单(list[str]，空=通过)。当前规则：
        a) GGA=PS(PBEsol) 却开了非零 IVDW 且没给 VDW_S8/VDW_A1/VDW_A2
           （PBEsol 没有内置的 D3-BJ 阻尼参数，缺参直接用会静默错配）        -> 拒绝
        b) 0D/1D/2D（struct_dim <= 2）配 ISIF=3 且未声明真空轴约束
           （ISIF=3 会弛豫真空方向，薄层体系必须约束 c/真空）               -> 拒绝
        vacuum_constrained=True 表示调用方已保证真空轴被约束（IOPTCELL/OPTCELL、
        选择性动力学固定真空方向、或固定胞），可放行 b)。
     注：返回值是"清单"而不是抛异常——调用方(gen 脚本)拿到非空清单后自行
     sys.exit/报错，符合池子里 validate_poscar 返回 reason 的风格。

依赖：纯标准库 + （可选）pymatgen（惰性导入）。写本文件时不引入 relax_common 等
兄弟模块，避免循环依赖；FUNC 词汇与 relax_common.FUNC_MAP / opt-dft-cpu 的
SUPPORTED_FUNCS 保持一致（pbe-d3=pbe+D3(BJ)，GGA=PE IVDW=12；pbesol=GGA=PS；
pbe=GGA=PE 无色散）。

典型用法：
    func, reason = method_select.default_method(structure, struct_dim, has_adsorbate=True)
    card = method_select.make_method_card(run_dir, func=func, reason=reason,
                                          struct_dim=struct_dim, bond_dim=bd)
    method_select.write_method_card(card)
    method_select.assert_method_consistent(cur_dir, upstream_dir, "step2_static")
    bad = method_select.validate_physical_constraints(incar_dir, struct_dim="2d")
"""

import hashlib
import math
import json
import re
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# 常量与词汇
# ---------------------------------------------------------------------------
FUNCS = ("pbe-d3", "pbesol", "pbe")          # 与 relax_common.FUNC_MAP 词汇一致
FUNC_DEFAULT = "auto"


def required_encut(potcar, factor=1.5, override=None):
    factor = float(factor)
    if not math.isfinite(factor) or factor < 1.5:
        raise ValueError("ENCUT_FACTOR 必须是有限数且 >= 1.5")
    values = []
    for line in Path(potcar).read_text(errors="ignore").splitlines():
        match = re.search(r"ENMAX\s*=\s*([-+\d.eEdD]+)", line)
        if match:
            values.append(float(match.group(1).replace("D", "E").replace("d", "e")))
    if not values or any(not math.isfinite(value) or value <= 0 for value in values):
        raise ValueError("POTCAR 缺少有效 ENMAX：%s" % potcar)
    minimum = math.ceil(max(values) * factor / 10.0) * 10
    if override not in (None, "", "auto"):
        actual = float(override)
        if not math.isfinite(actual) or actual < minimum:
            raise ValueError("ENCUT=%s 低于 POTCAR 下限 %s eV" % (override, minimum))
        return actual
    return minimum


def inherit_func(requested, upstream):
    previous = str(upstream.get("FUNC", "")).strip().lower()
    wanted = str(requested or "auto").strip().lower()
    if previous not in FUNCS:
        raise ValueError("前序 workflow_method.txt 缺少有效 FUNC；请先核对优化步骤")
    if wanted not in ("auto", previous):
        raise ValueError("FUNC=%s 与前序 FUNC=%s 不一致" % (wanted, previous))
    return previous


def validate_dft_input(incar, func=None):
    incar = Path(incar)
    tags = read_incar(incar)
    if "ENCUT" not in tags:
        raise ValueError("%s 缺少 ENCUT" % incar)
    required_encut(incar.parent / "POTCAR", override=tags["ENCUT"])
    actual = sniff_func_from_tags(tags)
    if actual not in FUNCS or (func is not None and actual != func):
        raise ValueError("%s 泛函标签与预期 %s 不一致" % (incar, func))


def validate_dft_tree(directory):
    for incar in sorted(Path(directory).rglob("INCAR*")):
        if incar.is_file() and incar.name in ("INCAR", "INCAR.relax", "INCAR.static"):
            validate_dft_input(incar)

# func -> 方法身份要求（GGA/IVDW；IVDW None = 禁用色散修正）
FUNC_SPEC = {
    "pbe-d3": {"GGA": "PE", "IVDW": 12},
    "pbesol": {"GGA": "PS", "IVDW": None},
    "pbe":    {"GGA": "PE", "IVDW": None},
}

# 方法卡要固化的 INCAR 标签：泛函/精度/色散 + 杂化参数集。
# "等" 指：以后要锁进方法身份的标签在这里追加即可，卡/指纹/一致性检查全部自动跟随。
METHOD_TAGS = (
    "GGA", "IVDW", "ENCUT", "PREC", "LASPH",
    # 色散阻尼参数（D3-BJ 需要 S8/A1/A2；S6/RADIUS/CNR 是 zero-damping 用）
    "VDW_S6", "VDW_S8", "VDW_A1", "VDW_A2", "VDW_RADIUS", "VDW_CNR",
    # 杂化泛函
    "LHFCALC", "HFSCREEN", "AEXX", "ALDAC", "LMAXFOCK", "LMAXMIX",
    "NKRED", "NKREDXY", "PRECFOCK", "ENCUTGW", "HFRCUT",
)

INCAR_NAME = "INCAR"
POTCAR_NAME = "POTCAR"
CARD_NAME = "method_card.json"
WAIVER_LOG_NAME = "method_card_waivers.jsonl"
CARD_SCHEMA = "method_card/1"

_BOOL_TRUE = {".TRUE.", ".T.", "TRUE", "T"}
_BOOL_FALSE = {".FALSE.", ".F.", "FALSE", "F"}
_INT_RE = re.compile(r"^[+-]?\d+$")
_FLOAT_RE = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------
class MethodCardError(Exception):
    """方法卡 / 方法一致性问题的基类。"""


class MethodCardTamperError(MethodCardError):
    """卡内容与自身指纹不符——源卡被篡改或被人手工改过而未同步指纹。"""


class MethodInconsistencyError(MethodCardError):
    """两张方法卡的方法身份不一致，且没有合法的非空豁免理由。"""


# ---------------------------------------------------------------------------
# 类型标准化（INCAR 值 -> python 类型）
# ---------------------------------------------------------------------------
def standardize_incar_value(raw):
    """把 INCAR 值字符串标准化成 python 类型：
       .TRUE./.T./TRUE/T -> True；.FALSE. 系 -> False；
       整数 -> int；含小数点/指数的数字 -> float；其余 -> 去引号 str。"""
    v = str(raw).strip()
    if not v:
        return ""
    upper = v.upper()
    if upper in _BOOL_TRUE:
        return True
    if upper in _BOOL_FALSE:
        return False
    if _INT_RE.match(v):
        return int(v)
    if _FLOAT_RE.match(v):
        try:
            return float(v)
        except ValueError:
            pass
    # 去掉可能的外层引号（'...' / "..."）
    if len(v) >= 2 and (v[0] == "'" or v[0] == '"') and v[-1] == v[0]:
        return v[1:-1].strip()
    return v


def read_incar(directory):
    """读 <directory>/INCAR，返回 {KEY(大写): typed 值}。

    语法与池内 gen 脚本一致：行首 #/! 整行注释；行内 #/! 截断为行尾注释；
    ';' 分隔同一行的多个赋值；KEY 大小写不敏感（统一大写）。
    返回标准库风格 dict，类型经 standardize_incar_value 标准化。
    """
    path = _resolve_incar_path(directory)
    if path is None:
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
        s = line.strip()
        if not s or s[0] in "#!":
            continue
        for marker in ("#", "!"):          # 行内注释
            if marker in s:
                s = s.split(marker, 1)[0].strip()
        if "=" not in s:
            continue
        for part in s.split(";"):          # 一行多赋值
            if "=" not in part:
                continue
            key, val = part.split("=", 1)
            key = key.strip().upper()
            if key:
                values[key] = standardize_incar_value(val)
    return values


def _resolve_incar_path(directory):
    p = Path(directory)
    if p.is_dir():
        cand = p / INCAR_NAME
        return cand if cand.is_file() else None
    return p if p.is_file() else None


# ---------------------------------------------------------------------------
# POTCAR SHA1（流式） + 卡片指纹
# ---------------------------------------------------------------------------
def potcar_sha1(path, chunk_size=1 << 16):
    """流式计算文件 SHA1（hex 小写）；文件不存在/读不了返回 None。

    流式：每次只读 chunk_size 字节喂给 hashlib，POTCAR 动辄几百 MB 也安全。
    """
    p = Path(path)
    if not p.is_file():
        return None
    h = hashlib.sha1()
    try:
        with open(p, "rb") as fh:
            while True:
                block = fh.read(chunk_size)
                if not block:
                    break
                h.update(block)
    except OSError:
        return None
    return h.hexdigest()


def _canonical_json(obj):
    """确定性 JSON 序列化（键排序、紧凑分隔、非 ascii 保留），用于指纹。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def card_identity(card):
    """提取一张卡的方法身份字段（被指纹/一致性检查保护的字段）。"""
    tags = card.get("tags") or {}
    return {
        "func": card.get("func"),
        "tags": {k: tags.get(k) for k in sorted(METHOD_TAGS) if k in tags},
        "potcar_sha1": card.get("potcar_sha1"),
        "struct_dim": card.get("struct_dim"),
        "bond_dim": card.get("bond_dim"),
        "n_components": card.get("n_components"),
    }


def card_fingerprint(card):
    """卡指纹 = sha256(方法身份字段的确定性 JSON)。改动身份字段必改指纹。"""
    return hashlib.sha256(
        _canonical_json(card_identity(card)).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 泛函推断 / 键网络分析（pymatgen 惰性）
# ---------------------------------------------------------------------------
def _ivdw_int(value):
    """把 IVDW 值（int/float/'12'/'NONE'/None）归一成 int；0/空/未知 -> None。
    便于直接吃 dict（字符串型 INCAR 值）而不只是 read_incar 的 typed 结果。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if value != 0 else None
    s = str(value).strip().lower()
    if s in ("", "none", "off", "false", "0"):
        return None
    try:
        n = int(s)
    except ValueError:
        try:
            n = int(float(s))
        except ValueError:
            return None
    return n if n != 0 else None


def sniff_func_from_tags(tags):
    """按方法标签快照反推 func（与 FUNC_SPEC 匹配）；推不出返回 None。
    tags 可为 typed dict（read_incar / 卡内 tags）或字符串值 dict。"""
    gga = str(tags.get("GGA", "")).upper()
    ivdw = _ivdw_int(tags.get("IVDW"))
    for name, spec in FUNC_SPEC.items():
        if spec["GGA"] == gga and spec["IVDW"] == ivdw:
            return name
    return None


def _pm_crystalnn():
    """惰性返回 CrystalNN 类；没装 pymatgen 返回 (None, 原因)。"""
    try:
        from pymatgen.analysis.local_env import CrystalNN
        return CrystalNN, None
    except Exception as e:                       # ImportError 等
        return None, "pymatgen 不可用: %s" % e


def analyze_bond_network(structure, crystalnn=None):
    """CrystalNN 键网络分析：返回 dict。

    dict:
        ok           是否成功给出 bond_dim / n_components
        bond_dim     键网络维度(0..3 int；= Larsen 定义的"全结构维度"，最高组分维度)
        n_components 键网络组分个数（>1 = 弱结合碎片/分子晶体，需色散）
        note         说明（成功注明算法；失败给出原因）
    实现（pymatgen 惰性，兼容新老 get_structure_components 两种返回形态）：
        CrystalNN().get_bonded_structure(structure)
            -> get_structure_components(bonded_structure)
            -> get_dimensionality_larsen(bonded_structure)
    老 pymatgen 的 get_structure_components 返回 list[dict]，每个含
    "dimensionality"；新 pymatgen 返回对象列表，元素有 .dim / .dimensionality。
    两形态都兼容。pymatgen 缺失 / 结构不可分析时 ok=False，交给上层走"异常保守"。
    """
    if structure is None:
        return {"ok": False, "bond_dim": None, "n_components": None,
                "note": "structure 为 None，无法分析键网络"}
    if crystalnn is None:
        cls, err = _pm_crystalnn()
        if cls is None:
            return {"ok": False, "bond_dim": None, "n_components": None,
                    "note": err}
        try:
            crystalnn = cls()
        except Exception as e:
            return {"ok": False, "bond_dim": None, "n_components": None,
                    "note": "CrystalNN 初始化失败: %s" % e}
    try:
        bonded = crystalnn.get_bonded_structure(structure)
        from pymatgen.analysis.dimensionality import (
            get_dimensionality_larsen, get_structure_components)
        components = get_structure_components(bonded)
    except Exception as e:
        return {"ok": False, "bond_dim": None, "n_components": None,
                "note": "键网络分析异常: %r" % (e,)}

    dims = []
    for c in components:
        if isinstance(c, dict):
            d = c.get("dimensionality")
        else:
            d = getattr(c, "dim", None)
            if d is None:
                d = getattr(c, "dimensionality", None)
        if d is not None:
            try:
                dims.append(int(d))
            except (TypeError, ValueError):
                pass
    try:
        larsen_dim = int(get_dimensionality_larsen(bonded))
    except Exception:
        larsen_dim = max(dims) if dims else None
    bond_dim = larsen_dim if larsen_dim is not None else (max(dims) if dims else None)
    n_components = len(components)
    if bond_dim is None or n_components is None:
        return {"ok": False, "bond_dim": None, "n_components": None,
                "note": "键网络分析未得到维度/组分"}
    return {"ok": True, "bond_dim": bond_dim, "n_components": n_components,
            "note": "CrystalNN + get_structure_components/get_dimensionality_larsen"}


# ---------------------------------------------------------------------------
# struct_dim 归一化
# ---------------------------------------------------------------------------
def _norm_dim(value):
    """把 '0d'/'2d'/'3d'/'2D' 或 int 归一到 int；不认识/None 返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value <= 3 else None
    s = str(value).strip().lower().rstrip("d")
    if s in ("0", "1", "2", "3"):
        return int(s)
    return None


# ---------------------------------------------------------------------------
# 1) default_method —— 泛函决策
# ---------------------------------------------------------------------------
# 键网络分析的注入点：测试可用 _set_analyze_hook(mock) 顶替，不要求本机 pymatgen。
_analyze_hook = analyze_bond_network


def _set_analyze_hook(fn):
    """（测试辅助）替换 default_method 的键网络分析实现；fn=None 还原。"""
    global _analyze_hook
    _analyze_hook = fn if fn is not None else analyze_bond_network


def default_method(structure, struct_dim, has_adsorbate=False,
                   uses_molecular_reference=False):
    """按键网络维度/组分决定 FUNC。返回 (func, reason)。

    规则（自上而下，先命中先返回；reason 记录决策依据）：
      a) uses_molecular_reference 或 has_adsorbate      -> pbe-d3
      b) 异常保守：structure 空 / pymatgen 缺失 / 分析失败 / 维度缺失 -> pbe-d3
      c) bond_dim < struct_dim                           -> pbe-d3
      d) n_components > 1                                -> pbe-d3
      e) 其余（bond_dim == struct_dim 且单组分）          -> pbesol
         reason 刻意不宣称"等维 => 纯共价"：等维只说明未测到需 D3 的弱结合组分。

    struct_dim 接受 "0d"/"1d"/"2d"/"3d"/"2D" 或 0..3 int。
    """
    sd = _norm_dim(struct_dim)
    if has_adsorbate or uses_molecular_reference:
        bits = []
        if uses_molecular_reference:
            bits.append("能量以孤立分子/片段为参照")
        if has_adsorbate:
            bits.append("体系含吸附物/界面")
        return "pbe-d3", ("%s：分子或界面间的 vdW 必须显式处理，参照与体相需在"
                          "同一色散描述下比较 -> pbe-d3" % "、".join(bits))

    if structure is None:
        return "pbe-d3", ("异常保守：structure 为空，无法分析键网络维度/组分，"
                          "无法排除层/链/分子弱结合 -> pbe-d3")
    analysis = _analyze_hook(structure)
    if not analysis.get("ok"):
        return "pbe-d3", ("异常保守：键网络分析不可用（%s），无法判定组分/低维"
                          "弱结合 -> pbe-d3" % analysis.get("note", "未知原因"))
    bd = analysis.get("bond_dim")
    nc = analysis.get("n_components")
    if bd is None or nc is None or sd is None:
        why = []
        if sd is None:
            why.append("struct_dim 缺失")
        if bd is None:
            why.append("bond_dim 缺失")
        if nc is None:
            why.append("n_components 缺失")
        return "pbe-d3", ("异常保守：键网络信息不完整（%s），无法判定是否等维/"
                          "单组分 -> pbe-d3" % "、".join(why))

    if bd < sd:
        return "pbe-d3", ("键网络维度 %dD 低于结构维度 %dD：胞内含低维(层/链/分子)"
                          "弱结合组分，需色散修正 -> pbe-d3" % (bd, sd))
    if nc > 1:
        return "pbe-d3", ("键网络含 %d 个独立组分：多组分体系靠弱作用/色散结合"
                          "-> pbe-d3" % nc)
    if bd > sd:
        # 键网络维度高于结构维度 = 与 struct_dim 判定冲突（真空轴方向仍有成键），
        # 不硬套"等维"结论，走异常保守。
        return "pbe-d3", ("异常保守：键网络维度 %dD 高于结构维度 %dD（struct_dim "
                          "判定与键网络不一致），无法安全给等维结论 -> pbe-d3"
                          % (bd, sd))

    # bd == sd 且 nc == 1：无低于胞维的弱结合证据。
    return "pbesol", ("键网络 %dD 单组分，与结构维度 %dD 一致：按 CrystalNN/"
                      "Larsen 判据未检测到低于胞维的弱结合组分。注意——等维连通"
                      "并不证明体系是纯共价，这里 pbesol 只是【未发现需色散修正】"
                      "时的默认；若后续出现吸附/分子参照/层间弱作用证据，"
                      "应改用 pbe-d3" % (bd, sd))


# ---------------------------------------------------------------------------
# 2) 方法卡 / 指纹 API
# ---------------------------------------------------------------------------
def make_method_card(directory, func=None, reason=None, struct_dim=None,
                     bond_dim=None, n_components=None, **metadata):
    """在 directory 现场生成方法卡 dict（自动读 INCAR/POTCAR）。

    入参：
        directory   运行目录（含 INCAR / 可选 POTCAR）。没 INCAR 时按纯声明建卡
                    （tags/potcar_sha1 为空，只声明 func/reason/dims）。
                    func 必须显式给，或给 None 且 INCAR 能反推出 func；
                    都做不到时报错，防止做出一张空头卡。
        func        可选；None 时尝试按 INCAR 的 GGA/IVDW 反推（sniff_func_from_tags）。
                    给了 func 且 INCAR 里有冲突的 GGA/IVDW 时直接 MethodCardError，
                    防止卡片与方法文件对不上（卡片说谎）。
        reason      决策理由（default_method 的 reason），可为空 str。
        struct_dim / bond_dim / n_components  分析结果，可空。
        **metadata  自由元数据（job 名、步骤、备注…），不进指纹。

    返回 dict（schema/func/reason/dims/tags/potcar_sha1/metadata/fingerprint）。
    不含写盘——写盘用 write_method_card。
    """
    directory = str(Path(directory))
    incar = read_incar(directory)
    tags = {k: incar[k] for k in METHOD_TAGS if k in incar}

    if func is None:
        func = sniff_func_from_tags(tags)
        if func is None:
            raise MethodCardError(
                "[ERROR] %s 无法从 INCAR 反推 func（GGA/IVDW 不匹配 %s），"
                "且未显式传 func。请显式 func= 或核对 INCAR。"
                % (directory, "/".join(FUNCS)))
    if func not in FUNCS:
        raise MethodCardError(
            "[ERROR] func=%r 不在受支持列表 %s 内" % (func, "/".join(FUNCS)))

    # func 与 INCAR 冲突检查（防卡片说谎）
    gga = str(incar.get("GGA", "")).upper() if incar.get("GGA") is not None else ""
    ivdw = _ivdw_int(incar.get("IVDW"))
    spec = FUNC_SPEC[func]
    if gga and gga != spec["GGA"]:
        raise MethodCardError(
            "[ERROR] %s：func=%r 要求 GGA=%s，但 INCAR 里是 GGA=%s。卡片拒绝说谎。"
            % (directory, func, spec["GGA"], gga or "(无)"))
    if ivdw is not None and spec["IVDW"] is not None and ivdw != spec["IVDW"]:
        raise MethodCardError(
            "[ERROR] %s：func=%r 要求 IVDW=%s，但 INCAR 里是 IVDW=%s。卡片拒绝说谎。"
            % (directory, func, spec["IVDW"], ivdw))
    if ivdw is not None and spec["IVDW"] is None:
        raise MethodCardError(
            "[ERROR] %s：func=%r 不应启用色散修正，但 INCAR 有 IVDW=%s。卡片拒绝说谎。"
            % (directory, func, ivdw))

    potcar = Path(directory) / POTCAR_NAME
    sha1 = potcar_sha1(potcar) if potcar.is_file() else None

    card = {
        "schema": CARD_SCHEMA,
        "directory": directory,
        "func": func,
        "reason": reason or "",
        "struct_dim": _norm_dim(struct_dim),
        "bond_dim": _norm_dim(bond_dim) if bond_dim is not None else None,
        "n_components": n_components,
        "tags": tags,
        "potcar_sha1": sha1,
        "metadata": dict(metadata),
        "fingerprint": "",
    }
    card["fingerprint"] = card_fingerprint(card)
    return card


def write_method_card(card, out_path=None):
    """把方法卡落盘为 JSON（默认 <card.directory>/method_card.json）。
    返回写入路径。写入前自动重算指纹，保证落盘卡与内容一致。"""
    if not out_path:
        out_path = Path(card.get("directory", ".")) / CARD_NAME
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    card["fingerprint"] = card_fingerprint(card)
    out_path.write_text(
        json.dumps(card, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8", newline="\n")
    return str(out_path)


def _coerce_card(value):
    """把 dict / 卡文件路径 / 含 method_card.json 的目录 统一成卡 dict。"""
    if isinstance(value, dict):
        return value
    p = Path(value)
    if p.is_dir():
        p = p / CARD_NAME
    if not p.is_file():
        raise MethodCardError("[ERROR] 找不到方法卡文件: %s" % p)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        raise MethodCardError("[ERROR] 方法卡 JSON 解析失败 %s: %s" % (p, e))
    if not isinstance(data, dict):
        raise MethodCardError("[ERROR] 方法卡内容不是对象: %s" % p)
    return data


def _verify_card_fingerprint(card, label):
    """自检一张卡的指纹：卡上指纹与按身份字段重算的一致才通过。
    不一致 => 源卡被篡改（被人手工改过 func/tags/potcar/dims）。"""
    stored = card.get("fingerprint")
    if stored is None:                       # 内存里手拼的卡，无指纹可比
        return
    if stored != card_fingerprint(card):
        raise MethodCardTamperError(
            "[ERROR] %s 方法卡指纹不符：卡内身份字段(func/tags/potcar_sha1/dims)"
            "被改动而未同步指纹——源卡可能被篡改，拒绝继续。" % label)


def _identity_field_diffs(cur, up):
    """逐字段对比两张卡的方法身份（card_identity），返回差异清单
    [{field, current, upstream}, ...]（以 cur 视角命名，值可为 None）。"""
    ci, ui = card_identity(cur), card_identity(up)
    diffs = []
    for field in sorted(set(ci) | set(ui)):
        cv, uv = ci.get(field), ui.get(field)
        # None 与空容器视作"没写"，跳过（身份里缺项不构成不一致）
        if cv in (None, {}) and uv in (None, {}):
            continue
        if cv == uv:
            continue
        diffs.append({"field": field, "current": cv, "upstream": uv})
    return diffs


def assert_method_consistent(current, upstream, step_name, allow=None):
    """断言 current 与 upstream 两张方法卡的方法身份一致。

    入参：current/upstream 可为卡 dict / 卡 JSON 路径 / 含 method_card.json 的目录。
    allow：豁免规则（None=不允许任何豁免，默认）。
        - str：非空理由，豁免本步所有差异（统一记一个理由）
        - dict：{field: 非空理由} 按字段豁免；未列出的字段差异仍报错
        空串/空白理由一律视为没有豁免（只接受非空理由豁免）。
    行为：
        1) 各自先做指纹自检——被篡改的卡抛 MethodCardTamperError；
        2) 无差异 => 返回 []（一致）；
        3) 有差异：
             - 没有合法豁免 => 抛 MethodInconsistencyError（列出 step + 差异字段）；
             - 有合法豁免   => 把豁免记录进返回 waivers，并追加写进
               <卡目录>/method_card_waivers.jsonl（记录留痕）。
    返回：waivers 列表（每项 {step, field, current, upstream, reason, ts}）。
    """
    cur = _coerce_card(current)
    up = _coerce_card(upstream)
    _verify_card_fingerprint(cur, "current")
    _verify_card_fingerprint(up, "upstream")

    diffs = _identity_field_diffs(cur, up)
    if not diffs:
        return []

    # ---- 决定豁免 ----
    if isinstance(allow, str):
        allow_map = {"*": allow}
    elif isinstance(allow, dict):
        allow_map = dict(allow)
    elif allow is None:
        allow_map = {}
    else:
        raise MethodCardError(
            "[ERROR] assert_method_consistent 的 allow 须为 None/str/dict，"
            "得到 %r" % (type(allow).__name__,))

    waivers, unwaived = [], []
    for d in diffs:
        field = d["field"]
        reason = allow_map.get(field)
        if reason is None:
            reason = allow_map.get("*")
        if isinstance(reason, str) and reason.strip():
            waivers.append({
                "step": step_name, "field": field,
                "current": d["current"], "upstream": d["upstream"],
                "reason": reason.strip(),
                "ts": int(time.time()),
            })
        else:
            unwaived.append(d)

    if unwaived:
        names = ", ".join(sorted(x["field"] for x in unwaived))
        raise MethodInconsistencyError(
            "[ERROR] %s：current 与 upstream 方法不一致（%s），且未给非空豁免理由。"
            "如需放行请给 allow={'<字段>': '理由'}（理由不能为空）。"
            % (step_name, names))

    _record_waivers(cur, waivers)
    return waivers


def _record_waivers(card, waivers):
    """把豁免记录追加写进卡目录下的 method_card_waivers.jsonl（留痕）。"""
    if not waivers:
        return
    directory = card.get("directory")
    if not directory or not Path(directory).is_dir():
        return                       # 纯内存卡无落盘目录，记录只随返回值
    log = Path(directory) / WAIVER_LOG_NAME
    with open(log, "a", encoding="utf-8") as fh:
        for w in waivers:
            fh.write(json.dumps(w, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# 3) validate_physical_constraints —— 物理约束校验
# ---------------------------------------------------------------------------
def validate_physical_constraints(incar, struct_dim=None, vacuum_constrained=False):
    """校验 INCAR 的物理自洽性，返回违规清单（list[str]，空=通过）。

    incar：dict（read_incar 结果）或 INCAR 所在目录 / INCAR 文件路径。
    规则：
      a) GGA=PS(PBEsol) + 非零 IVDW 却缺 VDW_S8/VDW_A1/VDW_A2 -> 拒绝
         （PBEsol 没有 D3-BJ 的内置阻尼参数；直接用会按 PBE 默认阻尼算，
           静默错配。要么去掉 IVDW，要么把 S8/A1/A2 显式给全。）
      b) 0D/1D/2D（struct_dim <= 2）配 ISIF=3 且 vacuum_constrained=False -> 拒绝
         （ISIF=3 弛豫全部格矢；薄层/链/分子体系的真空方向会被压塌或乱弛豫，
            必须由调用方约束真空轴并声明 vacuum_constrained=True。）
    """
    if not isinstance(incar, dict):
        incar = read_incar(incar)
    problems = []

    gga = str(incar.get("GGA", "")).upper() if incar.get("GGA") is not None else ""
    ivdw = _ivdw_int(incar.get("IVDW"))

    if gga == "PS" and ivdw not in (None,):
        missing = [k for k in ("VDW_S8", "VDW_A1", "VDW_A2")
                   if incar.get(k) is None]
        if missing:
            problems.append(
                "[物理约束] GGA=PS(PBEsol) 搭配 IVDW=%s，但缺少 %s。"
                "PBEsol 无内置 D3-BJ 阻尼参数，缺参直接跑会静默错配；"
                "请删除 IVDW 或显式给全 VDW_S8/VDW_A1/VDW_A2。"
                % (ivdw, "/".join(missing)))

    dim = _norm_dim(struct_dim)
    isif = _ivdw_int(incar.get("ISIF"))
    if dim is not None and dim <= 2 and isif == 3 and not vacuum_constrained:
        problems.append(
            "[物理约束] %dD 体系配 ISIF=3 且未声明真空轴约束"
            "(vacuum_constrained=False)。ISIF=3 会弛豫真空方向；"
            "请约束真空轴(IOPTCELL/OPTCELL/固定胞/选择性动力学)并传 "
            "vacuum_constrained=True，或改用 ISIF=2。" % dim)
    return problems
