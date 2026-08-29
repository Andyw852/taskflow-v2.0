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

