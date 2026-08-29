# -*- coding: utf-8 -*-
"""report —— 渲染/汇报流（07_render+08_assets+10_summary 合并）。
表格/详情渲染 + 查找 + 技能资源 + status/summary。
对外接口：render_table / render_detail / find_material / find_asset / cmd_summary 等。"""

import os
import sys
import re
import json
import time
import shlex
import hashlib
import base64
import collections
import functools
import itertools
import subprocess
import tempfile
import threading
import socket
import argparse
import glob
import math
import random
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

from tfpkg.bootstrap import REASON_MAX

# ===== 来自 07_render.py =====
# -*- coding: utf-8 -*-
# 07_render —— 状态表/详情渲染 + 材料/步骤查找
#
# 本分片由 tfpkg/__init__.py 装配器在单一命名空间里按顺序执行；
# 函数之间的引用按名字解析（与原单文件一致），分片间无需 import。
# 内容清单（按原文件行号）：
#   L2491  _cell_word
#   L2525  _short_reason
#   L2533  _cell_info
#   L2549  _group_word
#   L2568  _group_info
#   L2581  _step_dirname
#   L2588  _group_dir
#   L2601  _sub_row_cell
#   L2617  render_table
#   L2733  render_detail
#   L2760  find_material
#   L2786  _step_seq_match
#   L2802  _find_by_dotted
#   L2816  find_step

# ===== _cell_word (原 L2491-L2517) =====
def _cell_word(s):
    """步骤第一行状态词：running / pd / error / done / waiting。
    画图步骤（plot）：completed / not started / error。"""
    if s.get("plot"):
        k = s["kind"]
        if k == "OK":
            return "completed"
        if k == "FAIL":
            return "error"
        return "not started"
    j = s.get("job")
    if j:
        return "running" if j["state"] in ("R", "CG", "CF") else "pd"
    k = s["kind"]
    if k == "OK":
        return "done"
    if k == "IMAG":
        return "imaginary"
    if k == "FAIL":
        return "error"
    if k == "SCANCEL":
        return "scancel"   # v1.4：tf stop 取消，auto 不会重跑
    # patch_cell_word：DAG 调度后同时有多个步骤可启动，把"能开火"和
    # "被依赖卡住"分开显示，不然汇总表里全是 waiting，看不出该动哪个。
    if k == "WAIT":
        return "blocked"   # 依赖没齐，轮不到它
    return "ready"         # TODO（输入就绪）/ PREP（未生成）：可立即启动

# ===== _short_reason (原 L2525-L2530) =====
def _short_reason(info, limit=REASON_MAX):
    from tfpkg import REASON_MAX
    """squeue/qstat 给的原因：去外层括号、压掉空白、截断到 limit 个字符。"""
    r = " ".join(str(info or "").split())
    while len(r) >= 2 and r[0] == "(" and r[-1] == ")":
        r = " ".join(r[1:-1].split())
    return r[:limit]

# ===== _cell_info (原 L2533-L2546) =====
def _cell_info(s):
    """步骤第二行：running → "节点 任务号 已跑时长"；pd → "任务号 (原因)"；否则 -。"""
    if s.get("plot"):
        return "-"
    j = s.get("job")
    if not j:
        sc = s.get("scancel")
        if sc:   # v1.4：第二行标出原作业号，一眼看出是被谁取消的
            return "已取消(%s)" % (sc.get("jobid") or "?")
        return "-"
    if j["state"] in ("R", "CG", "CF"):
        return " ".join(x for x in (j.get("info"), j["id"], j.get("time")) if x)
    reason = _short_reason(j.get("info"))
    return "%s (%s)" % (j["id"], reason) if reason else j["id"]

# ===== _group_word (原 L2549-L2565) =====
def _group_word(ms):
    """v1.8：分组列状态词（同 group 的成员聚合，如三段式弛豫的 S1_relax 总列）。
    全 done → done；有作业 → running/pd；有 FAIL → error；否则 waiting。"""
    if all(s["kind"] == "OK" for s in ms):
        return "done"
    for s in ms:
        j = s.get("job")
        if j:
            return "running" if j["state"] in ("R", "CG", "CF") else "pd"
    if any(s["kind"] == "FAIL" for s in ms):
        return "error"
    if any(s["kind"] == "SCANCEL" for s in ms):   # v1.4
        return "scancel"
    # patch_cell_word：组内只要有一步能启动就算 ready，全被卡住才 blocked
    if all(s["kind"] == "WAIT" for s in ms):
        return "blocked"
    return "ready"

# ===== _group_info (原 L2568-L2578) =====
def _group_info(ms):
    """分组列第二行：有作业显示作业实况；未全完成时指明走到哪个组员。"""
    for s in ms:
        if s.get("job"):
            return _cell_info(s)
    if all(s["kind"] == "OK" for s in ms):
        return "-"
    for s in ms:
        if s["kind"] != "OK":
            return s["label"]
    return "-"

# ===== _step_dirname (原 L2581-L2585) =====
def _step_dirname(s):
    """patch_auto2：步骤在超算上的目录名（basename）。dir 缺失时退回 name。
    通用取法，不依赖任何技能的命名约定。"""
    d = s.get("dir") or s.get("name") or ""
    return os.path.basename(str(d).rstrip("/")) or "-"

# ===== _group_dir (原 L2588-L2598) =====
def _group_dir(ms):
    """patch_auto2：分组列第三行——当前落在哪个子步骤（显示其超算目录名）。
    有作业的优先（和第二行的作业号对得上）；否则取第一个未完成的；
    全完成则显示最后一个子步骤。"""
    for s in ms:
        if s.get("job"):
            return _step_dirname(s)
    for s in ms:
        if s["kind"] != "OK":
            return _step_dirname(s)
    return _step_dirname(ms[-1]) if ms else "-"

# ===== _sub_row_cell (原 L2601-L2614) =====
def _sub_row_cell(ms):
    """状态表第三行：分组列显示当前子步骤目录名；单步列默认 -。
    若该列步骤依赖了被 optional_steps 关掉的步骤（_missing_deps 非空），
    附加提醒（如 '缺 S2.3_hseplot'），提示下游任务缺这份数据。"""
    base = _group_dir(ms) if len(ms) > 1 else "-"
    rems = []
    for s in ms:
        for d in (s.get("_missing_deps") or []):
            if d not in rems:
                rems.append(d)
    if not rems:
        return base
    rem = "缺" + ",".join(rems)
    return rem if base in ("", "-") else "%s（%s）" % (base, rem)

# ===== render_table (原 L2617-L2730) =====
def render_table(data):
    types = data["types"]
    # v1.3.3：多技能按类型分表——此前全局列 = 所有技能步骤的并集，
    # band 材料空挂 elastic 三列、elastic 材料空挂 band 八列，表宽爆炸。
    # 每个技能一张表，列 = 该技能自己的步骤；同材料双技能两张表各出现一次。
    if len(types) > 1:
        for t in types:
            print("== %s（%s，%d 个材料）==" % (
                t["key"], t.get("desc") or t["key"], len(t["materials"])))
            sub = dict(data)
            sub["types"] = [t]
            render_table(sub)
            print()
        return
    # v1.8：步骤分组列——步骤配置写 group: 列名 时同组步骤合并为一列
    # （如三段式弛豫 step1a/b/c 合成 S1_relax 总列；-j 仍按原名/label 指组员）。
    # 段合并后类型条目不带 steps，完整步骤配置在每材料的 _seg.steps_cfg。
    gmap = {}   # (id(m), step_name) -> group 列名
    for t in types:
        for m in t["materials"]:
            for sc in ((m.get("_seg") or {}).get("steps_cfg") or []):
                if sc.get("group"):
                    gmap[(id(m), sc["name"])] = str(sc["group"])

    def col_of(m, s):
        return gmap.get((id(m), s["name"])) or s["label"]

    labels = []
    for t in types:
        for m in t["materials"]:
            for s in m["steps"]:
                c = col_of(m, s)
                if c not in labels:
                    labels.append(c)
    all_mats = [(t, m) for t in types for m in t["materials"]]
    if not all_mats:
        print("没有找到任何材料目录。")
        return
    headers = ["Material", "tt", "hpc", "dim"] + labels   # v1.1：去掉总体 Status 列
    # patch_auto2：row3 = 分组列当前子步骤的超算目录名
    pairs = []  # (t, row1, row2, row3)
    for t, m in all_mats:
        cols = {}
        for s in m["steps"]:
            cols.setdefault(col_of(m, s), []).append(s)
        word, act, sub = {}, {}, {}
        for c, ms in cols.items():
            if len(ms) == 1:
                word[c] = _cell_word(ms[0])
                act[c] = _cell_info(ms[0])
            else:
                word[c] = _group_word(ms)
                act[c] = _group_info(ms)
            sub[c] = _sub_row_cell(ms)
        hpc = m.get("hpc_name") or data.get("host") or "-"
        dim = m.get("dim") or "-"
        row1 = [m["name"], t["key"], hpc, dim] + [word.get(x, "") for x in labels]
        row2 = ["", "", "", ""] + [act.get(x, "") for x in labels]
        row3 = ["", "", "", ""] + [sub.get(x, "") for x in labels]
        pairs.append((t, row1, row2, row3))
    rows = [r for _, r1, r2, r3 in pairs for r in (r1, r2, r3)]
    widths = [max(len(h), *(len(str(r[i])) for r in rows)) for i, h in enumerate(headers)]

    def line(ch="-"):
        return "+" + "+".join(ch * (w + 2) for w in widths) + "+"

    def fmt(cells):
        return "|" + "|".join(" %s " % str(c).ljust(w) for c, w in zip(cells, widths)) + "|"

    print(line())
    print(fmt(headers))
    print(line("="))
    prev_tt = None
    for t, r1, r2, r3 in pairs:
        if prev_tt is not None and t["key"] != prev_tt:
            print(line())
        print(fmt(r1))
        print(fmt(r2))
        if any(c not in ("", "-") for c in r3):   # patch_auto2：没内容就不占行
            print(fmt(r3))
        prev_tt = t["key"]
    print(line())

    n_done = sum(1 for _, m in all_mats if all(s["kind"] == "OK" for s in m["steps"]))
    n_wait = sum(1 for _, m in all_mats if any(s["kind"] in ("R", "PD", "OTHER")
                                               for s in m["steps"]))
    n_act = sum(1 for _, m in all_mats if m["action"] != "-")
    n_sc = sum(1 for _, m in all_mats if any(s["kind"] == "SCANCEL"
                                           for s in m["steps"]))
    print("Total: %d | Done: %d | Waiting: %d | Actionable: %d%s"
          % (len(all_mats), n_done, n_wait, n_act,
             " | Scancel: %d" % n_sc if n_sc else ""))
    for t in types:
        if not t["materials"]:
            print("（%s: %s 下没有找到材料）" % (t["key"], t["root"]))
    attn = [(m["tt"], m["name"], s["label"], s["diag"]) for _, m in all_mats
            for s in m["steps"] if s["kind"] == "FAIL"]
    if attn:
        print("\nFAILED / NEEDS ATTENTION:")
        for tt, name, lab, diag in attn:
            print("  [%-3s] %-22s %-10s %s" % (tt, name, lab, diag))
    imag = [(m["tt"], m["name"], s["label"], s["diag"]) for _, m in all_mats
            for s in m["steps"] if s["kind"] == "IMAG"]
    if imag:
        print("\nIMAGINARY / DYNAMICALLY UNSTABLE（声子虚频，S6 合理不启动，非错误）:")
        for tt, name, lab, diag in imag:
            print("  [%-3s] %-22s %-10s %s" % (tt, name, lab, diag))
    scl = [(m["tt"], m["name"], s["label"]) for _, m in all_mats
           for s in m["steps"] if s["kind"] == "SCANCEL"]
    if scl:
        print("\nSCANCELLED（tf stop 取消，auto 不会重跑；重跑用 "
              "-status scancel start / rerun）:")
        for tt, name, lab in scl:
            print("  [%-3s] %-22s %s" % (tt, name, lab))

# ===== render_detail (原 L2733-L2754) =====
def render_detail(m):
    extra = ""
    if m.get("hpc_name"):
        extra += "\nHPC: %s" % m["hpc_name"]
    if m.get("dim"):
        extra += "\nDim: %s" % m["dim"]
    if m.get("lpath"):
        extra += "\nLocal: %s" % m["lpath"]
    if m.get("result_dir"):
        extra += "\nResult: %s" % m["result_dir"]
    print("Material: %s  (tt=%s, %s)\nDir: %s%s"
          % (m["name"], m["tt"], m["desc"], m["path"], extra))
    for s in m["steps"]:
        j = s.get("job")
        job_txt = ""
        if j:
            job_txt = "  job=%s %s %s" % (j["id"], j["state"],
                                           ("@" + j["info"]) if j["state"] == "R" else j["info"])
        active = "  <== active" if m["active"] is s else ""
        print("  [%-9s] %-18s %-10s %s%s%s" % (
            s["label"], s["name"], s["label_txt"], s["diag"], job_txt, active))
    print("Action: %s" % m["action"])

# ===== find_material (原 L2760-L2783) =====
def find_material(data, name):
    """-p 解析：精确名 > basename > 子串；跨类型命中多个时提示补 -tt。"""
    for mode in ("exact", "base", "sub"):
        hits = []
        for t in data["types"]:
            for m in t["materials"]:
                if mode == "exact" and m["name"] == name:
                    hits.append((t, m))
                elif mode == "base" and os.path.basename(m["name"]) == name:
                    hits.append((t, m))
                elif mode == "sub" and name in m["name"]:
                    hits.append((t, m))
        if hits:
            break
    if not hits:
        sys.exit("错误：找不到材料 '%s'。" % name)
    tts = {t["key"] for t, _ in hits}
    if len(hits) > 1:
        if len(tts) > 1:
            sys.exit("错误：'%s' 同时属于多个任务类型（%s），请用 -tt 指定。"
                     % (name, "/".join(sorted(tts))))
        sys.exit("错误：'%s' 匹配到多个材料：%s，请写完整名。"
                 % (name, ", ".join(m["name"] for _, m in hits)))
    return hits[0]

# ===== _step_seq_match (原 L2786-L2799) =====
def _step_seq_match(s, n):
    from tfpkg import _seq_key
    """v1.8：整数 -j N 匹配逻辑步骤号 N。
    - seq 恰为 N（整数）→ 命中；
    - 名字 stepN 开头，但**排除 stepN.M 子步**（step2 命中，step2.1 不命中）；
    - band_plot 画图步除外（历史行为）。
    带点号的子步用 -j N.M 或 label 指定，不会被整数 N 顺带选上。"""
    nm = str(s.get("name", ""))
    if "band_plot" in nm:
        return False
    sk = _seq_key(s.get("seq"))
    if sk is not None and abs(sk - n) < 1e-9:
        return True
    # 名字前缀：stepN 后面不能紧跟小数点（否则是 stepN.M 子步）
    return bool(re.match(r"step%d(?!\.\d)" % n, nm))

# ===== _find_by_dotted (原 L2802-L2813) =====
def _find_by_dotted(steps, jname):
    from tfpkg import _name_seq, _seq_key, step_seq
    """v1.8：-j 2.1 这类点号 token，按 seq / 名字序号精确匹配。命中返回步骤，否则 None。"""
    want = _seq_key(jname)
    if want is None:
        return None
    for s in steps:
        sq = _seq_key(step_seq(s))
        if sq is None:
            sq = _name_seq(s.get("name"))
        if sq is not None and abs(sq - want) < 1e-9:
            return s
    return None

# ===== find_step (原 L2816-L2833) =====
def find_step(m, jname):
    steps = m["steps"]
    if jname.isdigit():
        for s in steps:
            if _step_seq_match(s, int(jname)):
                return s
        sys.exit("错误：没有序号 %s 对应的步骤（现有：%s）。"
                 % (jname, ", ".join("%s|%s" % (s["label"], s["name"])
                                     for s in steps)))
    for s in steps:
        if s["name"] == jname or s["label"] == jname:
            return s
    _d = _find_by_dotted(steps, jname)      # v1.8：-j 2.1 点号序号
    if _d is not None:
        return _d
    sys.exit("错误：%s 没有步骤 '%s'（现有：%s）。"
             % (m["name"], jname,
                ", ".join("%s|%s" % (s["label"], s["name"]) for s in steps)))

# ===== 来自 08_assets.py =====
# -*- coding: utf-8 -*-
# 08_assets —— 技能资源定位 / step.conf 构建
#
# 本分片由 tfpkg/__init__.py 装配器在单一命名空间里按顺序执行；
# 函数之间的引用按名字解析（与原单文件一致），分片间无需 import。
# 内容清单（按原文件行号）：
#   L2839  _skill_asset_dirs
#   L2907  _same_file
#   L2918  find_asset
#   L3000  _stepconf_mod
#   L3016  step_conf_sources
#   L3058  _cluster_conda_step_conf
#   L3095  build_step_conf
#   L3132  _dim_mod
#   L3176  fill_local_dim
#   L3213  cmd_conf

# ===== _skill_asset_dirs (原 L2839-L2904) =====
def _skill_asset_dirs(t, m, base, sname=None):
    from tfpkg import COMMON_POOL_DIR
    """一个技能根目录下的查找顺序（模板目录布局，v1.3）。

    template_layout: shared（缺省）
        <技能>/templates/<文件>   →  <技能>/<文件>
        所有步骤共用同一套模板。

    template_layout: per_step
        <技能>/templates/<步骤名>/<文件>  →  <技能>/templates/<文件>
                                          →  <技能>/<文件>
        每个步骤先找自己的目录；步骤目录里没有才回落到公共模板。
        不知道是哪个步骤时（如 tf hpc 查模板齐不齐），所有步骤目录都算命中。

    两种布局都保留最后的平铺兜底，所以模板直接摊在技能根目录下依然能用。
    """
    seg = (m.get("_seg") or {})
    tdir = str(seg.get("template_dir") or t.get("template_dir") or "templates")
    layout = str(seg.get("template_layout") or t.get("template_layout")
                 or "shared").strip().lower()
    troot = os.path.join(base, tdir)
    out = []
    # v1.5 src：步骤在清单里声明的源子路径（相对技能根），大步骤套小步骤时用。
    # steps_cfg 经 _seg 下发到采集器，本地则从 t["steps"] 取。
    steps_cfg = list(seg.get("steps_cfg") or t.get("steps_cfg")
                     or t.get("steps") or [])
    for _grp in (t.get("optional_steps") or {}).values():   # 可选组里的步骤也带 src
        steps_cfg += (_grp or {}).get("steps") or []
    src_map = {}
    for _s in steps_cfg:
        if _s.get("src"):
            src_map[_s.get("name")] = str(_s["src"]).strip("/")
    if layout == "per_step":
        if sname:
            if sname in src_map:
                out.append(os.path.join(base, src_map[sname]))
            out.append(os.path.join(troot, str(sname)))
        else:
            for _v in src_map.values():          # 不指定步骤：所有 src 都算命中
                out.append(os.path.join(base, _v))
            out.extend(sorted(d for d in glob.glob(os.path.join(troot, "*"))
                              if os.path.isdir(d)))
    out.append(troot)
    out.append(base)
    # v1.10：公共技能池兜底——<技能搜索根>/_common/templates → /_common
    # 技能自己目录里有同名文件时优先用自己的（迁移期可以逐个技能切换）。
    pool = os.path.join(os.path.dirname(os.path.normpath(base)), COMMON_POOL_DIR)
    out.append(os.path.join(pool, tdir))
    out.append(pool)
    # patch_common_opt：_common/ 按【公共步骤】分目录（opt/ 等），每个步骤目录
    # 自带 templates/。查找链补上 <pool>/*/ 和 <pool>/*/<tdir> 两级——后者是
    # 关键，0D 模板现在在 _common/opt/templates/ 而不是 _common/templates/。
    # gen_need 里仍然只写文件名，skill.yaml 一行都不用改。
    try:
        for _d in sorted(d for d in glob.glob(os.path.join(pool, "*"))
                         if os.path.isdir(d)):
            out.append(_d)
            out.append(os.path.join(_d, tdir))
    except OSError:
        pass
    seen, uniq = set(), []
    for d in out:
        rd = os.path.normpath(d)
        if rd not in seen:
            seen.add(rd)
            uniq.append(d)
    return uniq

# ===== _same_file (原 L2907-L2912) =====
def _same_file(a, b):
    try:
        with open(a, "rb") as fa, open(b, "rb") as fb:
            return fa.read() == fb.read()
    except OSError:
        return False

# ===== find_asset (原 L2918-L2993) =====
def find_asset(cfg, t, m, fname, sname=None):
    from tfpkg import _PKG_DIR, _PKG_ROOT, _SKILL_ONLY, step_cfg
    """v3 资源查找链：材料/<技能>/逻辑名（v1.6 最优先）→ project_setting/逻辑名
    → project_setting/映射名 → skill_dir 内按 _skill_asset_dirs 的顺序（v1.3）。
    命中返回本地路径，否则 None。
    映射来自 hpc.yaml 的 template_map（如 submit_std.tpl → submit_jzzn_vaspstd.tpl），
    目标文件名始终是逻辑名；换超算改 project_setting/hpc.yaml，
    单个技能单独换超算放 <技能>/hpc.yaml（v1.6）。"""
    cands = []
    ps = (m.get("ps") or {}).get("dir")
    real = (m.get("template_map") or {}).get(fname)
    sld = m.get("_skill_dir_local")
    tdir = (m.get("template_subdir") or "templates")   # v1.7：项目内模板子文件夹名
    # v1.9：项目侧也按步骤分目录。优先级（越具体越优先）：
    #   <根>/templates/<步骤名>/ > <根>/templates/ > <根>/
    # 根依次为 材料/<技能>/（sld）、project_setting/（ps）。
    def _proj_dirs(root):
        dirs, troot = [], os.path.join(root, tdir)
        if sname:
            dirs.append(os.path.join(troot, str(sname)))
        else:      # 不指定步骤（如 tf hpc 查模板齐不齐）：所有步骤目录都算命中
            dirs.extend(sorted(d for d in glob.glob(os.path.join(troot, "*"))
                               if os.path.isdir(d)))
        dirs.append(troot)
        dirs.append(root)
        return dirs

    _roots = [] if _SKILL_ONLY else (([sld] if sld else []) + ([ps] if ps else []))
    for _root in _roots:
        for _d in _proj_dirs(_root):
            cands.append(os.path.join(_d, fname))
            if real:
                cands.append(os.path.join(_d, real))
    # v2.x：集中式 HPC 提交模板 setting/<hpc_name>/templates/（含步骤子目录）。
    # 优先级：项目内覆盖 > 这里 > 技能目录兜底。逻辑名即文件名，换超算 = 换
    # hpc_name，提交模板自动切到 setting/<新hpc>/templates/，技能逻辑零改动。
    # v1.12：步骤可指定超算（step 配置 hpc 字段）——该步的提交模板从
    # setting/<步骤hpc>/templates 取，而不是材料默认集群。
    hpc_key = m.get("hpc_name")
    if sname:
        _shpc = step_cfg(t, sname, m).get("hpc")
        if _shpc:
            hpc_key = str(_shpc)
    if hpc_key:
        for _sdir in (os.path.join(_PKG_ROOT, "setting"),
                      os.path.join(_PKG_DIR, "setting"),
                      os.path.expanduser("~/.config/taskflow/setting")):
            _hroot = os.path.join(_sdir, str(hpc_key))
            for _d in _proj_dirs(_hroot):
                cands.append(os.path.join(_d, fname))
                if real:
                    cands.append(os.path.join(_d, real))
    seg = (m.get("_seg") or {})
    sd = seg.get("skill_dir") or t.get("skill_dir")
    sdirs = []
    if sd:
        if os.path.isabs(sd):
            sdirs = [sd]
        else:  # 相对路径查找顺序：项目配置目录 → 全局配置目录 → 软件根目录
            pkg_root = _PKG_ROOT   # （tf.yaml 放 setting/ 时也能找对）
            for base in (seg.get("_base_dir") or t.get("_base_dir"),
                         cfg.get("_config_dir"), pkg_root):
                if base:
                    d = os.path.normpath(os.path.join(base, sd))
                    if d not in sdirs:
                        sdirs.append(d)
    for base in sdirs:
        for d in _skill_asset_dirs(t, m, base, sname):   # v1.3：模板目录布局
            cands.append(os.path.join(d, fname))
            if real:
                cands.append(os.path.join(d, real))
    for c in cands:
        if os.path.isfile(c):
            return c
    return None

# ===== _stepconf_mod (原 L3000-L3013) =====
def _stepconf_mod(cfg, t, m):
    from tfpkg import _STEPCONF_MOD
    """按技能加载该技能目录里的 stepconf.py（与 dim_common.py 一样每技能一份）。"""
    key = t.get("key")
    if key in _STEPCONF_MOD:
        return _STEPCONF_MOD[key]
    path = find_asset(cfg, t, m, "stepconf.py")
    mod = None
    if path:
        import importlib.util as _ilu
        spec = _ilu.spec_from_file_location("stepconf_%s" % key, path)
        mod = _ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)
    _STEPCONF_MOD[key] = mod
    return mod

# ===== step_conf_sources (原 L3016-L3055) =====
def step_conf_sources(cfg, t, m, sname):
    from tfpkg import STEP_CONF, _PKG_ROOT, _SKILL_ONLY
    """收集该步骤的 step.conf 分层来源，低优先级在前。
    顺序：skill 出厂默认 → 项目 templates/step.conf → 项目 templates/<步骤>/step.conf
    （材料/<技能>/ 下的同名文件优先级最高，最后叠加）。"""
    out, seen = [], set()

    def _add(path, note):
        if path and os.path.isfile(path) and path not in seen:
            seen.add(path)
            out.append((path, note))

    seg = (m.get("_seg") or {})
    sd = seg.get("skill_dir") or t.get("skill_dir")
    if sd and not os.path.isabs(sd):
        pkg_root = _PKG_ROOT
        for base in (seg.get("_base_dir") or t.get("_base_dir"),
                     cfg.get("_config_dir"), pkg_root):
            if base and os.path.isdir(os.path.join(base, sd)):
                sd = os.path.normpath(os.path.join(base, sd))
                break
    # v1.9.3：skill 侧用 template_dir（ke 是 "."），项目侧用 template_subdir。
    # 两者是不同的键，早先都当成 "templates" 会让 ke 的项目级 step.conf 失效。
    sk_tdir = str(seg.get("template_dir") or t.get("template_dir") or "templates")
    pj_tdir = str(m.get("template_subdir") or "templates")
    if sd:                                            # skill 出厂默认
        _add(os.path.normpath(os.path.join(sd, sk_tdir, STEP_CONF)), "skill 默认")
        if sname:
            _add(os.path.normpath(os.path.join(sd, sk_tdir, str(sname),
                                               STEP_CONF)), "skill 默认(本步)")
    for root, tag in ([] if _SKILL_ONLY else
                      [((m.get("ps") or {}).get("dir"), "project_setting"),
                       (m.get("_skill_dir_local"), "材料/<技能>")]):
        if not root:
            continue
        _add(os.path.join(root, pj_tdir, STEP_CONF), "%s 共用" % tag)
        if sname:
            _add(os.path.join(root, pj_tdir, str(sname), STEP_CONF),
                 "%s 本步" % tag)
    return out

# ===== _cluster_conda_step_conf (原 L3058-L3092) =====
def _cluster_conda_step_conf(m, hpc_name=None):
    from tfpkg import _load_yaml_file, pkg_setting_path
    """从集群 setting/<name>.yaml 读 conda_sh/conda_env/mace_model_dir，拼成 step.conf
    片段（最低优先级；仅 MACE 技能调用，因为只有它们的 gen 脚本声明 CONDA_SH/CONDA_ENV/
    MACE_MODEL_DIR）。切超算 = 改 hpc.yaml 的 name，这些键自动跟着集群走；项目
    project_setting/templates/step.conf 可覆盖（优先级更高）。hpc_name 供步骤级超算
    覆盖（step 配置 hpc 字段）时传入，缺省用材料 hpc_name。"""
    name = hpc_name or m.get("hpc_name")
    if not name:
        return None
    p = pkg_setting_path(str(name) + ".yaml")
    if not p:
        return None
    hpc = _load_yaml_file(p) or {}
    sh = hpc.get("conda_sh")
    env = hpc.get("conda_env")
    mdir = hpc.get("mace_model_dir")
    aenv = hpc.get("amset_env")   # amset 专用环境名（如 amset / amset_clean）
    pdir = hpc.get("potcar_dir")          # VASP POTCAR 库根目录（VASP 技能）
    rdir = hpc.get("references_dir")      # 凸包参考相共享目录（defect 技能）
    if not sh and not env and not mdir and not aenv and not pdir and not rdir:
        return None
    lines = ["[params]"]
    if sh:
        lines.append("CONDA_SH = %s" % sh)
    if env:
        lines.append("CONDA_ENV = %s" % env)
    if mdir:
        lines.append("MACE_MODEL_DIR = %s" % mdir)
    if aenv:
        lines.append("AMSET_ENV = %s" % aenv)
    if pdir:
        lines.append("POTCAR_DIR = %s" % pdir)
    if rdir:
        lines.append("REFERENCES_DIR = %s" % rdir)
    return "\n".join(lines) + "\n"

# ===== build_step_conf (原 L3095-L3126) =====
def build_step_conf(cfg, t, m, sname):
    from tfpkg import step_cfg
    """把分层 step.conf 合并成一份带来源注释的文本。无任何来源时返回 None。"""
    mod = _stepconf_mod(cfg, t, m)
    srcs = step_conf_sources(cfg, t, m, sname)
    if not mod or not srcs:
        return None, []
    tagged, legend = [], []
    for i, (path, note) in enumerate(srcs, 1):
        tag = "[%d]" % i
        legend.append((tag, path, note))
        with open(path, encoding="utf-8-sig") as fh:
            tagged.append((tag, fh.read()))
    # v1.11：注入集群默认 conda（最低优先级，可被项目 step.conf 覆盖）。
    # 原来只对 MACE 技能注入；现在所有技能都注入 CONDA_SH/CONDA_ENV/AMSET_ENV，
    # gen 脚本需要时从 step.conf 读（stepconf RESERVED_PARAMS 已放行），
    # 避免脚本里硬编码个人集群路径。setting/<集群>.yaml 里按需写 conda_sh/conda_env/amset_env。
    shpc_name = m.get("hpc_name")
    if sname:
        _shpc = step_cfg(t, sname, m).get("hpc")
        if _shpc:
            shpc_name = str(_shpc)
    cluster = _cluster_conda_step_conf(m, shpc_name)
    if cluster:
        tag = "[cluster:%s]" % shpc_name
        tagged.insert(0, (tag, cluster))
        legend.insert(0, (tag, "<setting/%s.yaml>" % shpc_name, "集群默认"))
    merged, prov = mod.merge(tagged)
    header = ["# ===== 本文件由 tf 自动合成，勿手改 —— 要改请改下面列出的上游 =====",
              "# 步骤 %s   材料 %s" % (sname, m.get("name")),
              "# 来源（越靠下优先级越高）："]
    header += ["#   %s %s   (%s)" % (tg, pt, nt) for tg, pt, nt in legend]
    return mod.dumps(merged, prov, header), legend

# ===== _dim_mod (原 L3132-L3173) =====
def _dim_mod(cfg, t):
    from tfpkg import COMMON_POOL_DIR, _DIM_MOD, _PKG_ROOT
    """按技能加载 dim_common.py（band 在技能根，ke 在 step1_opt/ 等子目录）。"""
    key = t.get("key")
    if key in _DIM_MOD:
        return _DIM_MOD[key]
    sd = t.get("skill_dir")
    mod = None
    if sd:
        bases = []
        if os.path.isabs(sd):
            bases = [sd]
        else:
            pkg_root = _PKG_ROOT
            for b in (t.get("_base_dir"), cfg.get("_config_dir"), pkg_root):
                if b:
                    bases.append(os.path.normpath(os.path.join(b, sd)))
        # patch_common_opt：技能目录里找不到时回落到公共池 _common/
        # （及其步骤子目录）。dim_common.py 现在只有 _common 一份，
        # 不加这一段的话这个函数永远返回 None。
        for b in list(bases):
            pool = os.path.join(os.path.dirname(os.path.normpath(b)),
                                COMMON_POOL_DIR)
            if pool not in bases:
                bases.append(pool)
        hits = []
        for b in bases:
            hits = sorted(glob.glob(os.path.join(b, "dim_common.py"))
                          + glob.glob(os.path.join(b, "*", "dim_common.py"))
                          + glob.glob(os.path.join(b, "*", "*", "dim_common.py")))
            if hits:
                break
        if hits:
            import importlib.util as _ilu
            spec = _ilu.spec_from_file_location("dim_common_%s" % key, hits[0])
            mod = _ilu.module_from_spec(spec)
            try:
                spec.loader.exec_module(mod)
            except Exception:
                mod = None
    _DIM_MOD[key] = mod
    return mod

# ===== fill_local_dim (原 L3176-L3210) =====
def fill_local_dim(cfg, data, types=None):
    """v1.9.5：dim 原本只来自远端 workflow_method.txt，也就是 gen 跑过才有。
    还没 gen 的材料改用本地 POSCAR 现算一个，结果加 * 表示是预判值
    （gen 会先做原胞化/标准化，最终以 workflow_method.txt 为准）。"""
    # 采集结果里的 type 只有 key/desc/root/materials，local_root 和 skill_dir
    # 得从原始 types（get_types 的返回）里按 key 取回来。
    tmap = {}
    for t0 in (types or []):
        tmap.setdefault(t0.get("key"), t0)
    for td in data.get("types") or []:
        t = tmap.get(td.get("key")) or td
        mod = None
        if not t.get("local_root"):
            continue
        # v-perf：采集结果里每材料已带回 lpath（本地 POSCAR 目录），直接建索引，
        # 不再重复 discover_local 扫树。
        mats = {m0["name"]: m0.get("lpath")
                for m0 in (td.get("materials") or []) if m0.get("lpath")}
        for m in td.get("materials") or []:
            if m.get("dim") or not mats.get(m.get("name")):
                continue
            pos = os.path.join(mats[m["name"]], "POSCAR")
            if not os.path.isfile(pos):
                continue
            if mod is None:
                mod = _dim_mod(cfg, t)
                if mod is None:
                    break
            try:
                dim = mod.detect_dimension(pos)[0]
                m["dim"] = str(dim).upper() + "*"
            except SystemExit:
                m["dim"] = "?"     # 检测到 1D/0D，gen 会拒绝
            except Exception:
                pass

# ===== cmd_conf (原 L3213-L3250) =====
def cmd_conf(cfg, data, proj, jname, sets=None):
    from tfpkg import STEP_CONF
    """查看/修改某步骤的 step.conf。--set 一律写进【本步的项目文件】。"""
    t, m = find_material(data, proj)
    s = find_step(m, jname)
    sname = s["name"]
    mod = _stepconf_mod(cfg, t, m)
    if not mod:
        print("错误：该技能目录里没有 stepconf.py。")
        return 1
    if sets:
        ps = (m.get("ps") or {}).get("dir")
        if not ps:
            print("错误：该材料还没有 project_setting，先跑 tf -p %s init。" % m["name"])
            return 1
        dst = os.path.join(ps, m.get("template_subdir") or "templates",
                           sname, STEP_CONF)
        print("写入 %s" % dst)
        for kv in sets:
            if "=" not in kv:
                print("  跳过 %r（应为 节.键=值）" % kv)
                continue
            path, val = kv.split("=", 1)
            sec, key = (path.rsplit(".", 1) if "." in path else ("params", path))
            old, _ = mod.set_value(dst, sec, key, val if val != "" else None)
            print("  [%s] %-18s %s -> %s" % (sec, key, old if old is not None
                                             else "（无）", val or "（删除）"))
    text, legend = build_step_conf(cfg, t, m, sname)
    if text is None:
        print("该步骤没有任何 step.conf。")
        return 1
    print("\n步骤 %s (%s)   材料 %s   超算 %s"
          % (sname, s.get("label"), m.get("name"), m.get("host_eff") or "-"))
    print("来源（越靠下优先级越高）：")
    for tg, pt, nt in legend:
        print("  %s %s   (%s)" % (tg, pt, nt))
    print()
    print("\n".join(l for l in text.splitlines() if not l.startswith("#")))
    return 0

# ===== 来自 10_summary.py =====
# -*- coding: utf-8 -*-
# 10_summary —— status / summary 命令
#
# 本分片由 tfpkg/__init__.py 装配器在单一命名空间里按顺序执行；
# 函数之间的引用按名字解析（与原单文件一致），分片间无需 import。
# 内容清单（按原文件行号）：
#   L3614  find_uninited
#   L3638  _mat_all_done
#   L3642  apply_hide_done
#   L3657  cmd_status
#   L3665  _step_status_word
#   L3683  _summary_snapshot
#   L3695  _snapshot_diff
#   L3711  _summary_lines
#   L3754  cmd_summary

# ===== find_uninited (原 L3614-L3635) =====
def find_uninited(cfg):
    from tfpkg import discover_local, find_ps_dir
    """project_roots 下有 POSCAR 但向上找不到 project_setting/tf_*.yaml 的材料目录
    （= 新加进来还没 init 的材料）。
    v1.2 之前只扫已有段的 local_root：新建体系（如 Mg2C60 直接放项目根下）
    不在任何段覆盖范围内，永远扫不到；改为直接扫 project_roots。"""
    import glob as _glob
    out, seen = [], set()
    for proot in (cfg.get("project_roots") or []):
        try:
            root, mats = discover_local(proot)
        except Exception:
            continue
        for m in mats:
            lp = os.path.realpath(m["lpath"])
            if lp in seen:
                continue
            seen.add(lp)
            ps = find_ps_dir(lp, root)
            if ps and _glob.glob(os.path.join(ps, "tf_*.yaml")):
                continue
            out.append(m["name"])
    return out

# ===== _mat_all_done (原 L3638-L3639) =====
def _mat_all_done(m):
    return bool(m["steps"]) and all(s["kind"] == "OK" for s in m["steps"])

# ===== apply_hide_done (原 L3642-L3654) =====
def apply_hide_done(data):
    """v1.1 --hide-done / hide_done: true：状态表隐藏全部步骤都完成的项目。"""
    n = 0
    for t in data["types"]:
        keep = []
        for m in t["materials"]:
            if _mat_all_done(m):
                n += 1
            else:
                keep.append(m)
        t["materials"] = keep
    if n:
        print("（已隐藏 %d 个全部完成的项目；加 --show-done 显示）" % n)

# ===== cmd_status (原 L3657-L3662) =====
def cmd_status(cfg, data, mname, jname):
    if mname:
        t, m = find_material(data, mname)
        render_detail(m)
    else:
        render_table(data)

# ===== _step_status_word (原 L3665-L3680) =====
def _step_status_word(s):
    """步骤状态短词（供快照 diff 用）。刻意不含动态诊断——压力值/力值等每轮
    都会波动的数字不该触发"有变化"，否则巡检每轮都被误报刷屏。"""
    kind = s.get("kind")
    j = s.get("job") or {}
    if kind == "PD":
        r = (j.get("info") or "").strip()
        return "PD(%s)" % r if r else "PD"
    if kind == "FAIL":
        return "FAIL"
    if kind == "R":
        return "R"
    if kind == "OTHER":
        return "OTHER(%s)" % (j.get("state") or "?")
    return {"OK": "done", "WAIT": "wait", "TODO": "todo", "PREP": "prep",
            "SCANCEL": "scancel", "IMAG": "imaginary"}.get(kind, str(kind))

# ===== _summary_snapshot (原 L3683-L3692) =====
def _summary_snapshot(data):
    """结构化快照：type → material → step_label → 状态短词。
    供 summary --diff 做步骤级对比——能精确告诉 agent "谁从什么变到什么"。"""
    snap = {}
    for t in data["types"]:
        tm = {}
        for m in t["materials"]:
            tm[m["name"]] = {s["label"]: _step_status_word(s) for s in m["steps"]}
        snap[t["key"]] = tm
    return snap

# ===== _snapshot_diff (原 L3695-L3708) =====
def _snapshot_diff(old, new):
    """返回变更列表 [(type, material, label, 旧词, 新词)]（按名字排序）。"""
    changes = []
    for tkey in sorted(set(old) | set(new)):
        om = old.get(tkey) or {}
        nm = new.get(tkey) or {}
        for mname in sorted(set(om) | set(nm)):
            os_ = om.get(mname) or {}
            ns_ = nm.get(mname) or {}
            for lab in sorted(set(os_) | set(ns_)):
                ow, nw = os_.get(lab), ns_.get(lab)
                if ow != nw:
                    changes.append((tkey, mname, lab, ow or "-", nw or "-"))
    return changes

# ===== _summary_lines (原 L3711-L3751) =====
def _summary_lines(data):
    """把 data 规约成 summary 的文本行（打印与 diff 共用）。"""
    lines = []
    if not data.get("types"):
        lines.append("（没有任何任务类型）")
        return lines
    for t in data["types"]:
        mats = t["materials"]
        n = len(mats)
        if not n:
            lines.append("%s: 无材料" % t["key"])
            continue
        cnt = {"done": 0, "run": 0, "pd": 0, "err": 0, "sc": 0, "wait": 0}
        fails = []   # (材料名, 步骤label, diag)
        for m in mats:
            kinds = [s["kind"] for s in m["steps"]]
            if all(k == "OK" for k in kinds):
                cnt["done"] += 1
            elif any(k == "FAIL" for k in kinds):
                cnt["err"] += 1
                for s in m["steps"]:
                    if s["kind"] == "FAIL":
                        fails.append((m["name"], s["label"], s.get("diag") or ""))
            elif any(k in ("R", "OTHER") for k in kinds):
                cnt["run"] += 1
            elif any(k == "PD" for k in kinds):
                cnt["pd"] += 1
            elif any(k == "SCANCEL" for k in kinds):
                cnt["sc"] += 1
            else:
                cnt["wait"] += 1
        lines.append("%s: %d 材料 done=%d run=%d pd=%d err=%d scancel=%d wait=%d"
                     % (t["key"], n, cnt["done"], cnt["run"], cnt["pd"],
                        cnt["err"], cnt["sc"], cnt["wait"]))
        for name, lab, diag in fails:
            lines.append("  FAIL %s %s %s" % (name, lab, diag))
    q = data.get("queue")
    if q and (q.get("R") or q.get("PD") or q.get("total")):
        lines.append("队列(全部作业): R=%d PD=%d 共 %d"
                     % (q.get("R", 0), q.get("PD", 0), q.get("total", 0)))
    return lines

# ===== cmd_summary (原 L3754-L3787) =====
def cmd_summary(data, diff=False, state_path=None):
    """只读极简汇总（省 token）：每类型一行计数（run/pd 分开）+ FAIL 清单（含
    诊断原因）+ 全局队列。供 agent 巡检用。

    diff=True 时：与 state_path 里的结构化快照对比，无变化则不输出（token≈0）；
    有变化（或首次）才输出汇总 + 「变更:」步骤级清单（谁从什么变到什么），并写回
    快照——agent 无需再跑 tf list / squeue 去猜哪里变了。
    """
    if diff and state_path:
        new_snap = _summary_snapshot(data)
        try:
            prev_snap = json.loads(open(state_path, encoding="utf-8").read())
        except Exception:
            prev_snap = None
        if prev_snap == new_snap:
            return  # 无变化，静默
        changes = _snapshot_diff(prev_snap, new_snap) if isinstance(prev_snap, dict) else []
        try:
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump(new_snap, f, ensure_ascii=False, sort_keys=True)
        except OSError:
            pass
        lines = _summary_lines(data)
        if changes:
            print("\n".join(lines))
            print("变更:")
            for _tkey, _mname, _lab, _o, _n in changes[:40]:
                print("  %s %s: %s → %s" % (_mname, _lab, _o, _n))
            if len(changes) > 40:
                print("  … 还有 %d 项变更" % (len(changes) - 40))
        else:
            print("\n".join(lines))
        return
    print("\n".join(_summary_lines(data)))


# ===== 结构化错误码（v2.0：诊断文本 → 稳定 code，供 --json 机器判读）=====
_DIAG_PATTERNS = [
    ("relax_summary_missing", "relax_summary.json missing"),
    ("relax_summary_incomplete", "relax_summary.json incomplete"),
    ("force_not_converged", "force not converged"),
    ("outcar_missing", "OUTCAR missing"),
    ("dir_missing", "dir missing"),
    ("not_started", "not started"),
    ("node_fail", "NODE_FAIL"),
    ("gen_error", "gen 失败"),
    ("stepconf_unknown_params", "不认识的键"),
    ("stepconf_missing", "缺少 step.conf"),
    ("imaginary_freq", "虚频"),
]
_RELAX_VERDICTS = ("electronic", "oscillating", "stalled", "thrown", "nsw",
                   "progressing")


def _diag_code(diag):
    """诊断文本 → 稳定结构化错误码；未命中返回 "unknown"，空串返回 "none"。
    让 agent 无需 grep：直接看 --json 的 step.diag_code / fails[].code。"""
    d = (diag or "").strip()
    if not d:
        return "none"
    for code, pat in _DIAG_PATTERNS:
        if pat in d:
            return code
    if d.startswith("未收敛 ["):
        for v in _RELAX_VERDICTS:
            if "[%s]" % v in d:
                return "relax_" + v
    if d.startswith("job "):
        return "job"
    return "unknown"


def _add_diag_codes(data):
    """给每个步骤 dict 就地补 diag_code 字段（--json 用）。返回 data。"""
    for t in data.get("types", []):
        for m in t.get("materials", []):
            for s in m.get("steps", []):
                s["diag_code"] = _diag_code(s.get("diag") or "")
    return data


def _summary_json(data):
    """summary 的结构化版本（-o json 用）。每类型一行计数 + FAIL 清单 + 队列。"""
    out = {"types": []}
    for t in data["types"]:
        mats = t["materials"]
        n = len(mats)
        if not n:
            out["types"].append({"key": t["key"], "materials": 0, "counts": {},
                                 "fails": []})
            continue
        cnt = {"done": 0, "run": 0, "pd": 0, "err": 0, "scancel": 0, "wait": 0}
        fails = []
        for m in mats:
            kinds = [s["kind"] for s in m["steps"]]
            if all(k == "OK" for k in kinds):
                cnt["done"] += 1
            elif any(k == "FAIL" for k in kinds):
                cnt["err"] += 1
                for s in m["steps"]:
                    if s["kind"] == "FAIL":
                        fails.append({"material": m["name"], "step": s["label"],
                                      "diag": s.get("diag") or "",
                                      "code": _diag_code(s.get("diag") or "")})
            elif any(k in ("R", "OTHER") for k in kinds):
                cnt["run"] += 1
            elif any(k == "PD" for k in kinds):
                cnt["pd"] += 1
            elif any(k == "SCANCEL" for k in kinds):
                cnt["scancel"] += 1
            else:
                cnt["wait"] += 1
        out["types"].append({"key": t["key"], "materials": n, "counts": cnt,
                             "fails": fails})
    q = data.get("queue")
    if q:
        out["queue"] = {"R": q.get("R", 0), "PD": q.get("PD", 0),
                        "total": q.get("total", 0)}
    return out

