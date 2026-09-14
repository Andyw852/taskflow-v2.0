# -*- coding: utf-8 -*-
"""skillspec —— 技能自描述扩展段（io_schema / flow / corrections）的规范、校验与渲染。

为什么有这个模块（2026-09-14 与 wangchao 讨论的"加技能友好"改造）：
  老 skill.yaml 只描述"怎么跑"（steps / gen_need / templates），不描述
    · 这个技能**吃什么、吐什么、有哪些旋钮** —— 建议 1.3 io_schema
    · 这个技能**整条流程在干什么、产物能喂给谁** —— 建议 1.2 flow
    · 这个技能**遇到典型失败该怎么纠** —— 建议 1.1 corrections
  于是"加一个技能"只能靠读代码 + 问作者；新成员无法照着一个目录复制改 30 分钟上线。

本模块只做三件事，**纯函数、不读盘、不连超算、不改状态**：
  1. 定义三段的字段规范（哪些键合法、哪些必填）；
  2. 校验一份 skill.yaml 的这三段，产出人类可读的问题清单（错误/警告分级）；
  3. 把三段渲染成 tf schema 的控制台文本 / 结构化 dict。

调用方：
  · tfpkg/bootstrap.py  发现技能时算一份轻量 issues（tf skills 显示）
  · tfpkg/cli.py        tf schema [<技能>] 子命令（cmd_schema）

命名与 atomate2 的对应（供熟悉 atomate2 的人对照）：
  io_schema.inputs/outputs  ≈ Maker 的输入结构/输出文档模型
  io_schema.params          ≈ Maker 的 **kwargs（可调旋钮）
  flow.stages/next_skills   ≈ Maker 之上的一层 "flow"（多个 Maker 串起来）
"""

# 扩展段：本模块管的三段。skill.yaml 里出现任意一段就要求 schema >= 2。
SPEC_SECTIONS = ("io_schema", "flow", "corrections")
SPEC_SCHEMA_MIN = 2

# ---- io_schema 字段规范 -----------------------------------------------------
_IO_TOP_KEYS = ("inputs", "outputs", "params", "steps", "notes")
_IO_LIST_KEYS = ("inputs", "outputs", "params")
# name 必填；其余可选。desc 建议写（tf schema 直接展示给人看）。
_IO_ITEM_KEYS = {
    "inputs": ("name", "from", "required", "desc", "type"),
    "outputs": ("name", "path", "step", "desc", "type", "consumers"),
    "params": ("name", "values", "default", "desc", "where"),
}
# io_schema.steps[]：**按步**的 I/O 与工具（tf skill show 渲染成"技能卡片"，
# 对应论文里的 Generator / Tool / Validator / Output 四行）。
#   step       必填，步骤引用（name/label 都行）
#   title      卡片标题（短英文短语，图里直接显示）
#   tool       这一步实际用什么算/算什么（VASP / MACE / Pheasy GPU / phono3py / ShengBTE…）
#   generator  生成器脚本名（可省：默认取 steps[].gen）
#   validator  判据的人话说明（可省：默认按 steps[].check 渲染）
#   inputs     本步吃哪些东西（名字即可）
#   outputs    本步吐哪些东西（名字即可，含中间产物）
#   note       补充说明（可选）
_IO_STEP_KEYS = ("step", "title", "tool", "generator", "validator",
                 "inputs", "outputs", "note")

# ---- flow 字段规范 ----------------------------------------------------------
_FLOW_KEYS = ("summary", "stages", "next_skills", "requires", "ref", "notes")
_FLOW_STAGE_KEYS = ("name", "steps", "produces", "desc")
_FLOW_NEXT_KEYS = ("skill", "via", "desc")

_ERR, _WARN = "错误", "警告"


def _issue(level, fmt, *args):
    return "[%s] %s" % (level, (fmt % args) if args else fmt)


def issues_fatal(issues):
    """问题清单里是否有致命项（tf schema --strict 据此返回非零）。"""
    return any(str(i).startswith("[%s]" % _ERR) for i in (issues or []))


def split_issues(issues):
    """把问题清单拆成 (errors, warnings)。"""
    errs = [i for i in (issues or []) if str(i).startswith("[%s]" % _ERR)]
    warns = [i for i in (issues or []) if not str(i).startswith("[%s]" % _ERR)]
    return errs, warns


# =============================================================================
# 步骤名表：把技能步骤集合做成可查集合，用于校验 from: step:xxx / stages[].steps
# =============================================================================
def all_steps(skel):
    """技能的**全部**步骤：主步骤 + optional_steps 各开关组里的可选步骤。
    io_schema / flow 里引用的步骤两者都可能出现（如 S3.1_plot 就在可选组里），
    校验与展示都必须把它们算进来，否则会误报"步骤不存在"。"""
    skel = skel or {}
    steps = [s for s in (skel.get("steps") or []) if isinstance(s, dict)]
    for _flag, spec in (skel.get("optional_steps") or {}).items():
        if isinstance(spec, dict):
            steps += [s for s in (spec.get("steps") or []) if isinstance(s, dict)]
    return steps


def all_steps_optional(skel):
    """同 all_steps，但带"是否可选"标记：[(step, 是否来自 optional_steps), ...]。
    返回的是原字典对象（不改动、不复制），调用方只读。"""
    skel = skel or {}
    out = [(s, False) for s in (skel.get("steps") or []) if isinstance(s, dict)]
    for _flag, spec in (skel.get("optional_steps") or {}).items():
        if isinstance(spec, dict):
            out += [(s, True) for s in (spec.get("steps") or []) if isinstance(s, dict)]
    return out


def step_tokens(skel):
    """技能里所有可被引用的步骤标识：name / label / seq（字符串化）。"""
    out = set()
    for s in all_steps(skel):
        if not isinstance(s, dict):
            continue
        for k in ("name", "label", "seq"):
            v = s.get(k)
            if v is not None and str(v).strip():
                out.add(str(v).strip())
    return out


def _step_ref_ok(tok, known):
    """步骤引用是否命中：支持完整名/label/序号，也支持 'step3_PBE_static' 前缀写法。"""
    t = str(tok or "").strip()
    if not t or not known:
        return False
    if t in known:
        return True
    return any(k.startswith(t) or t.startswith(k) for k in known)


def _refs_from_from_field(val):
    """io_schema.inputs[].from 支持 'user' / 'step:xxx' / 'skill:key' / 'file:path'。
    返回 (kind, payload)。"""
    s = str(val or "").strip()
    if not s:
        return "user", ""
    if ":" in s:
        k, _p = s.split(":", 1)
        k = k.strip().lower()
        if k in ("user", "step", "skill", "file", "param"):
            return k, _p.strip()
    return "user", s


# =============================================================================
# 校验
# =============================================================================
def validate_skill_spec(key, man, skel=None, known_skills=None, handler_names=None):
    """校验一份 skill.yaml 的扩展段，返回人类可读的问题清单（可为空）。
    分级：[错误] 会让 tf schema --strict 失败（结构不合法）；[警告] 只提示（多半是笔误）。

    handler_names=None 时跳过"corrections 名是否已注册"这一条（tf 启动期的
    轻量校验用；tf schema 会带上真正的 handler 注册表做完整校验）。"""
    man = man if isinstance(man, dict) else {}
    issues = []
    try:
        schema = int(man.get("schema") or 1)
    except (TypeError, ValueError):
        schema = 1
    present = [s for s in SPEC_SECTIONS if man.get(s) is not None]
    if present and schema < SPEC_SCHEMA_MIN:
        issues.append(_issue(_WARN,
                             "用了 %s 段但 schema=%s；建议写成 schema: %d"
                             "（老 tf 会把不认识的段原样忽略，但版本号该提上来）",
                             "/".join(present), schema, SPEC_SCHEMA_MIN))
    known_steps = step_tokens(skel or man)
    if "io_schema" in present:
        issues += _validate_io(key, man.get("io_schema"), known_steps, known_skills)
    if "flow" in present:
        issues += _validate_flow(key, man.get("flow"), known_steps, known_skills)
    if "corrections" in present:
        issues += _validate_corrections(key, man.get("corrections"), handler_names)
    return issues


def _validate_io(key, io, known_steps, known_skills):
    issues = []
    if not isinstance(io, dict):
        return [_issue(_ERR, "io_schema 必须是字典（键：%s）", "/".join(_IO_TOP_KEYS))]
    for k in io:
        if k not in _IO_TOP_KEYS:
            issues.append(_issue(_WARN, "io_schema 有不认识的键 '%s'（可用：%s）",
                                 k, "/".join(_IO_TOP_KEYS)))
    out_names = set()
    for sect in _IO_LIST_KEYS:
        v = io.get(sect)
        if v is None:
            continue
        if not isinstance(v, list):
            issues.append(_issue(_ERR, "io_schema.%s 必须是列表", sect))
            continue
        for i, item in enumerate(v):
            tag = "io_schema.%s[%d]" % (sect, i)
            if not isinstance(item, dict):
                issues.append(_issue(_ERR, "%s 必须是字典（至少要有 name）", tag))
                continue
            nm = str(item.get("name") or "").strip()
            if not nm:
                issues.append(_issue(_ERR, "%s 缺少 name", tag))
            else:
                tag = "io_schema.%s[%s]" % (sect, nm)
            for k in item:
                if k not in _IO_ITEM_KEYS[sect]:
                    issues.append(_issue(_WARN, "%s 有不认识的键 '%s'（可用：%s）",
                                         tag, k, "/".join(_IO_ITEM_KEYS[sect])))
            # 输入来源 / 输出来源的步骤引用
            if sect == "inputs" and item.get("from") is not None:
                kind, payload = _refs_from_from_field(item.get("from"))
                if kind == "step" and not _step_ref_ok(payload, known_steps):
                    issues.append(_issue(_WARN,
                                         "%s 的 from=step:%s 在本技能步骤里找不到"
                                         "（可用：%s）", tag, payload,
                                         ", ".join(sorted(known_steps)[:6]) or "无"))
                if kind == "skill" and known_skills and payload not in known_skills:
                    issues.append(_issue(_WARN, "%s 的 from=skill:%s 不是已发现的技能",
                                         tag, payload))
            if sect == "outputs":
                out_names.add(nm)
                st = item.get("step")
                if st is not None and not _step_ref_ok(st, known_steps):
                    issues.append(_issue(_WARN, "%s 的 step=%s 在本技能步骤里找不到",
                                         tag, st))
                cons = item.get("consumers")
                if cons is not None and not isinstance(cons, list):
                    issues.append(_issue(_WARN, "%s 的 consumers 建议写成列表", tag))
            if sect == "params":
                vals = item.get("values")
                if vals is not None and not isinstance(vals, list):
                    issues.append(_issue(_WARN, "%s 的 values 建议写成列表（可取值集合）", tag))
                if isinstance(vals, list) and item.get("default") is not None \
                        and item.get("default") not in vals:
                    issues.append(_issue(_WARN, "%s 的 default=%s 不在 values 里",
                                         tag, item.get("default")))
    issues += _validate_io_steps(io.get("steps"), known_steps)
    return issues


def _validate_io_steps(steps, known_steps):
    """校验 io_schema.steps[]（按步 I/O，v1.0 第二批新增；tf skill show 用它画卡片）。"""
    issues = []
    if steps is None:
        return issues
    if not isinstance(steps, list):
        return [_issue(_ERR, "io_schema.steps 必须是列表（每项一个步骤的 I/O 声明）")]
    seen = set()
    for i, st in enumerate(steps):
        tag = "io_schema.steps[%d]" % i
        if not isinstance(st, dict):
            issues.append(_issue(_ERR, "%s 必须是字典", tag))
            continue
        ref = str(st.get("step") or "").strip()
        if not ref:
            issues.append(_issue(_ERR, "%s 缺少 step（步骤名或 label）", tag))
        else:
            tag = "io_schema.steps[%s]" % ref
            if ref in seen:
                issues.append(_issue(_WARN, "%s 重复声明了同一个步骤", tag))
            seen.add(ref)
            if not _step_ref_ok(ref, known_steps):
                issues.append(_issue(_WARN, "%s 不是本技能的步骤（可用：%s）",
                                     tag, ", ".join(sorted(known_steps)[:8]) or "无"))
        for k in st:
            if k not in _IO_STEP_KEYS:
                issues.append(_issue(_WARN, "%s 有不认识的键 '%s'（可用：%s）",
                                     tag, k, "/".join(_IO_STEP_KEYS)))
        for k in ("inputs", "outputs"):
            v = st.get(k)
            if v is not None and not isinstance(v, list):
                issues.append(_issue(_WARN, "%s 的 %s 建议写成列表", tag, k))
    return issues


def step_io_map(spec):
    """io_schema.steps[] → {步骤引用: 声明 dict}（tf skill show 用）。"""
    out = {}
    io = (spec or {}).get("io_schema")
    if not isinstance(io, dict):
        return out
    for st in (io.get("steps") or []):
        if isinstance(st, dict) and str(st.get("step") or "").strip():
            out[str(st["step"]).strip()] = st
    return out


def _validate_flow(key, flow, known_steps, known_skills):
    issues = []
    if not isinstance(flow, dict):
        return [_issue(_ERR, "flow 必须是字典（键：%s）", "/".join(_FLOW_KEYS))]
    for k in flow:
        if k not in _FLOW_KEYS:
            issues.append(_issue(_WARN, "flow 有不认识的键 '%s'（可用：%s）",
                                 k, "/".join(_FLOW_KEYS)))
    stages = flow.get("stages")
    if stages is not None:
        if not isinstance(stages, list):
            issues.append(_issue(_ERR, "flow.stages 必须是列表"))
        else:
            for i, st in enumerate(stages):
                if not isinstance(st, dict):
                    issues.append(_issue(_ERR, "flow.stages[%d] 必须是字典", i))
                    continue
                if not str(st.get("name") or "").strip():
                    issues.append(_issue(_ERR, "flow.stages[%d] 缺少 name", i))
                for k in st:
                    if k not in _FLOW_STAGE_KEYS:
                        issues.append(_issue(_WARN,
                                             "flow.stages[%s] 有不认识的键 '%s'（可用：%s）",
                                             st.get("name") or i, k,
                                             "/".join(_FLOW_STAGE_KEYS)))
                for ref in (st.get("steps") or []):
                    if not _step_ref_ok(ref, known_steps):
                        issues.append(_issue(_WARN,
                                             "flow.stages[%s].steps 里的 '%s' 不是本技能的步骤",
                                             st.get("name") or i, ref))
    nxt = flow.get("next_skills")
    if nxt is not None:
        if not isinstance(nxt, list):
            issues.append(_issue(_ERR, "flow.next_skills 必须是列表"))
        else:
            for i, n in enumerate(nxt):
                if isinstance(n, str):
                    nm = n
                    n = {"skill": nm}
                if not isinstance(n, dict):
                    issues.append(_issue(_ERR, "flow.next_skills[%d] 必须是字典或技能名", i))
                    continue
                for k in n:
                    if k not in _FLOW_NEXT_KEYS:
                        issues.append(_issue(_WARN,
                                             "flow.next_skills[%s] 有不认识的键 '%s'（可用：%s）",
                                             n.get("skill") or i, k,
                                             "/".join(_FLOW_NEXT_KEYS)))
                sk = str(n.get("skill") or "").strip()
                if not sk:
                    issues.append(_issue(_ERR, "flow.next_skills[%d] 缺少 skill", i))
                elif known_skills and sk not in known_skills:
                    issues.append(_issue(_WARN,
                                         "flow.next_skills 指向的技能 '%s' 没有发现（可能还没写）",
                                         sk))
    return issues


def _validate_corrections(key, corr, handler_names):
    issues = []
    if not isinstance(corr, list):
        return [_issue(_ERR, "corrections 必须是列表（每项是 handler 名或"
                             "{name: ..., ...} 字典）")]
    for i, c in enumerate(corr):
        if isinstance(c, str):
            nm = c.strip()
            if not nm:
                issues.append(_issue(_ERR, "corrections[%d] 是空字符串", i))
                continue
        elif isinstance(c, dict):
            nm = str(c.get("name") or "").strip()
            if not nm:
                issues.append(_issue(_ERR, "corrections[%d] 缺少 name", i))
                continue
            for k in ("name", "desc", "steps", "params", "severity", "ref"):
                pass
            for k in c:
                if k not in ("name", "desc", "steps", "params", "severity", "ref"):
                    issues.append(_issue(_WARN,
                                         "corrections[%s] 有不认识的键 '%s'", nm, k))
        else:
            issues.append(_issue(_ERR, "corrections[%d] 必须是字符串或字典", i))
            continue
        if handler_names is not None and nm not in handler_names:
            issues.append(_issue(_WARN,
                                 "corrections 里的 '%s' 在 _corrections/ 库里没找到"
                                 "（已注册：%s）", nm,
                                 ", ".join(sorted(handler_names)) or "无"))
    return issues


# =============================================================================
# 抽取 + 统计（tf skills 一行摘要 / tf schema 表头都用）
# =============================================================================
def spec_of(man):
    """从 skill.yaml 原始字典里抽出三段（没有的段给 None）。"""
    man = man if isinstance(man, dict) else {}
    return {s: man.get(s) for s in SPEC_SECTIONS}


def spec_stats(spec):
    """三段的规模统计，供一行摘要显示。"""
    spec = spec or {}
    io = spec.get("io_schema") if isinstance(spec.get("io_schema"), dict) else {}
    fl = spec.get("flow") if isinstance(spec.get("flow"), dict) else {}
    corr = spec.get("corrections") if isinstance(spec.get("corrections"), list) else []
    n_in = len(io.get("inputs") or [])
    n_out = len(io.get("outputs") or [])
    n_par = len(io.get("params") or [])
    return {
        "has_io": bool(io), "n_in": n_in, "n_out": n_out, "n_params": n_par,
        "has_flow": bool(fl),
        "n_stages": len(fl.get("stages") or []) if isinstance(fl.get("stages"), list) else 0,
        "n_next": len(fl.get("next_skills") or []) if isinstance(fl.get("next_skills"), list) else 0,
        "n_corr": len(corr),
    }


# =============================================================================
# 渲染：tf schema
# =============================================================================
def _kv_lines(pairs, indent="    "):
    out = []
    for k, v in pairs:
        if v in (None, "", [], {}):
            continue
        out.append("%s%-12s %s" % (indent, k, v))
    return out


def render_schema(key, spec, skel=None, issues=None, extra_head=None):
    """把单个技能的三段渲染成控制台文本（tf schema <技能>）。"""
    skel = skel or {}
    spec = spec or {}
    st = spec_stats(spec)
    L = []
    L.append("技能   %s   版本 %s" % (key, skel.get("_skill_version") or "-"))
    if skel.get("desc"):
        L.append("说明   %s" % skel.get("desc"))
    if skel.get("_skill_manifest"):
        L.append("清单   %s" % skel.get("_skill_manifest"))
    main_n = len([s for s in (skel.get("steps") or []) if isinstance(s, dict)])
    steps = all_steps(skel)
    L.append("步骤   %d 个（+%d 可选）：%s" % (
        main_n, len(steps) - main_n,
        " → ".join(str(s.get("label") or s.get("name")) for s in steps)
        or "（无）"))
    if extra_head:
        L += list(extra_head)

    io = spec.get("io_schema")
    L.append("")
    L.append("─" * 72)
    if not isinstance(io, dict) or not io:
        L.append("io_schema   （未声明 —— 建议补：吃什么/吐什么/有哪些旋钮）")
    else:
        L.append("io_schema   输入 %d · 输出 %d · 参数 %d" % (st["n_in"], st["n_out"], st["n_params"]))
        for sect, title in (("inputs", "输入"), ("outputs", "输出"), ("params", "参数")):
            items = io.get(sect) or []
            if not items:
                continue
            L.append("  %s (%d)" % (title, len(items)))
            for it in items:
                if not isinstance(it, dict):
                    L.append("    %s" % it)
                    continue
                nm = str(it.get("name") or "?")
                bits = []
                if sect == "inputs":
                    bits += _kv_lines([("来自", it.get("from")),
                                       ("必需", "" if it.get("required") is None
                                        else ("是" if it.get("required") else "否")),
                                       ("类型", it.get("type"))])
                elif sect == "outputs":
                    bits += _kv_lines([("步骤", it.get("step")), ("路径", it.get("path")),
                                       ("类型", it.get("type")),
                                       ("下一步用", ", ".join(it.get("consumers") or [])
                                        if isinstance(it.get("consumers"), list)
                                        else it.get("consumers"))])
                else:
                    vals = it.get("values")
                    bits += _kv_lines([("取值", "|".join(str(v) for v in vals)
                                        if isinstance(vals, list) else vals),
                                       ("默认", it.get("default")),
                                       ("写在哪", it.get("where"))])
                L.append("    %-22s %s" % (nm, (bits[0].strip() if bits else "")))
                for b in bits[1:]:
                    L.append("      %s" % b.strip())
                if it.get("desc"):
                    L.append("      %s" % it.get("desc"))
        if io.get("notes"):
            L.append("  备注: %s" % io.get("notes"))

    fl = spec.get("flow")
    L.append("")
    L.append("─" * 72)
    if not isinstance(fl, dict) or not fl:
        L.append("flow        （未声明 —— 建议补：整条流程在干什么、产物能喂给谁）")
    else:
        L.append("flow")
        if fl.get("summary"):
            L.append("  %s" % fl.get("summary"))
        for i, s2 in enumerate(fl.get("stages") or [], 1):
            if not isinstance(s2, dict):
                continue
            prod = s2.get("produces")
            L.append("  阶段%d  %-16s 步骤 %s%s"
                     % (i, s2.get("name") or "?",
                        ", ".join(str(x) for x in (s2.get("steps") or [])) or "-",
                        ("  → 产出 " + ", ".join(str(x) for x in prod))
                        if isinstance(prod, list) and prod else ""))
            if s2.get("desc"):
                L.append("         %s" % s2.get("desc"))
        for n in (fl.get("next_skills") or []):
            n = {"skill": n} if isinstance(n, str) else (n or {})
            L.append("  可接   %-16s%s" % (n.get("skill") or "?",
                                          ("  （用 %s）" % n.get("via")) if n.get("via") else ""))
        if fl.get("requires"):
            L.append("  前置   %s" % fl.get("requires"))
        if fl.get("ref"):
            L.append("  参考   %s" % fl.get("ref"))

    corr = spec.get("corrections") or []
    L.append("")
    L.append("─" * 72)
    if not corr:
        L.append("corrections （未声明 —— 失败纠错走全局 handler 库）")
    else:
        L.append("corrections  %d 个" % len(corr))
        for c in corr:
            if isinstance(c, str):
                L.append("  %s" % c)
            elif isinstance(c, dict):
                L.append("  %-18s %s" % (c.get("name") or "?",
                                         c.get("desc") or ""))
    errs, warns = split_issues(issues)
    L.append("")
    L.append("─" * 72)
    if not errs and not warns:
        L.append("校验   ✓ 无问题")
    else:
        L.append("校验   %d 错误 / %d 警告" % (len(errs), len(warns)))
        for i in errs + warns:
            L.append("  %s" % i)
    return "\n".join(L)


def render_schema_table(rows):
    """tf schema（不带技能名）：全部技能一览。rows = [(key, skel, spec, issues)]"""
    L = []
    L.append("%-14s %-18s %-6s %-6s %s" % ("技能", "io_schema(入/出/参)", "flow",
                                           "纠错", "问题"))
    for key, skel, spec, issues in rows:
        st = spec_stats(spec)
        io_txt = ("%d/%d/%d" % (st["n_in"], st["n_out"], st["n_params"])) if st["has_io"] else "-"
        errs, warns = split_issues(issues)
        prob = ("%d 错 %d 警" % (len(errs), len(warns))) if (errs or warns) else "✓"
        L.append("%-14s %-18s %-6s %-6s %s" % (
            key, io_txt, "✓" if st["has_flow"] else "-",
            st["n_corr"] or "-", prob))
    n_all = len(rows)
    n_io = sum(1 for _k, _s, sp, _i in rows if spec_stats(sp)["has_io"])
    n_fl = sum(1 for _k, _s, sp, _i in rows if spec_stats(sp)["has_flow"])
    L.append("")
    L.append("共 %d 个技能：io_schema %d · flow %d" % (n_all, n_io, n_fl))
    L.append("看单个技能：tf schema <技能名>     机器可读：tf schema --json")
    return "\n".join(L)


def schema_dict(key, skel, spec, issues):
    """tf schema --json 的单技能结构（稳定字段名，供工具/AI 判读）。"""
    return {
        "skill": key,
        "version": skel.get("_skill_version"),
        "desc": skel.get("desc"),
        "manifest": skel.get("_skill_manifest"),
        "steps": [{"name": s.get("name"), "label": s.get("label"), "seq": s.get("seq")}
                  for s in (skel.get("steps") or []) if isinstance(s, dict)],
        "io_schema": spec.get("io_schema"),
        "flow": spec.get("flow"),
        "corrections": spec.get("corrections"),
        "stats": spec_stats(spec),
        "issues": list(issues or []),
    }


# =============================================================================
# 命令入口：tf schema
# =============================================================================
def cmd_schema(cfg, tt=None, json_out=False, strict=False):
    """tf schema [<技能>] —— 打印技能的自描述（io_schema / flow / corrections）+ 校验。

    纯本地、不采集、不提交、不改任何文件（安全，可随便跑）。
    退出码：0 正常；--strict 且存在 [错误] 级问题时返回 1。"""
    import json as _json
    skills = cfg.get("_skills") or {}
    if not skills:
        print("没有发现任何技能（skill/*/skill.yaml）。")
        return 0
    wanted = (tt or "").strip()
    if wanted and wanted not in skills:
        import difflib
        close = difflib.get_close_matches(wanted, sorted(skills), n=3, cutoff=0.4)
        print("错误：没有技能 '%s'%s" % (wanted,
                                     ("，你是不是想：%s" % ", ".join(close)) if close else "。"))
        print("已发现的技能：%s" % ", ".join(sorted(skills)))
        return 1
    handler_names = None
    try:                      # 纠错 handler 注册表（可能有多个候选目录，取并集）
        from tfpkg import correction_handler_names
        handler_names = correction_handler_names(cfg)
    except Exception:
        handler_names = None
    keys = [wanted] if wanted else sorted(skills)

    # 完整校验：带上 handler 注册表 + 全技能名表（比启动期的轻量校验更严）
    full = {}
    for k in keys:
        skel = skills[k] or {}
        man = _manifest_of(skel)
        full[k] = validate_skill_spec(k, man, skel=skel,
                                     known_skills=set(skills),
                                     handler_names=handler_names)
    if json_out:
        items = [schema_dict(k, skills[k] or {}, spec_of(_manifest_of(skills[k] or {})),
                             full[k]) for k in keys]
        print(_json.dumps({"schema": SPEC_SCHEMA_MIN, "count": len(items),
                           "skills": items}, ensure_ascii=False, indent=2))
        return 1 if (strict and any(issues_fatal(full[k]) for k in keys)) else 0
    if wanted:
        skel = skills[wanted] or {}
        spec = spec_of(_manifest_of(skel))
        head = []
        if handler_names is not None:
            head.append("纠错库 %d 个 handler：%s"
                        % (len(handler_names), ", ".join(sorted(handler_names)) or "无"))
        print(render_schema(wanted, spec, skel, full[wanted], extra_head=head))
    else:
        rows = [(k, skills[k] or {}, spec_of(_manifest_of(skills[k] or {})), full[k])
                for k in keys]
        print(render_schema_table(rows))
    if strict:
        bad = [k for k in keys if issues_fatal(full[k])]
        if bad:
            print("\n--strict：以下技能有 [错误] 级问题：%s" % ", ".join(bad))
            return 1
    return 0


def _manifest_of(skel):
    """取技能骨架里缓存的原始清单扩展段（供校验用）。
    bootstrap._load_manifest 会把 skill.yaml 的 schema + 三段原样存进
    skel["_skill_spec"]；拿不到时（老路径/测试构造的骨架）回退成空字典——
    空字典只让校验"看不到东西"，不会误报。"""
    spec = (skel or {}).get("_skill_spec")
    if isinstance(spec, dict):
        return spec
    return {}


# =============================================================================
# 技能卡片：tf skill show —— 渲染成论文图 2 那种
#   ┌ 1 Relax structure ┐ ──▶ ┌ 2 Generate forces ┐ …
#   │ Generator / Tool / Validator / Output │
# 数据来源：steps[]（gen/check/marker）+ io_schema.steps[]（tool/title/outputs）。
# 没写 io_schema.steps 的技能也能渲染（Tool 显示 "—"，并在尾注里提示补）。
# =============================================================================
_CARD_LABELS = ("Generator", "Tool", "Validator", "Output")


def _disp_width(s):
    """终端显示宽度（中文/全角算 2）。"""
    w = 0
    for ch in str(s):
        o = ord(ch)
        if (0x1100 <= o <= 0x115F or 0x2E80 <= o <= 0xA4CF or 0xAC00 <= o <= 0xD7A3
                or 0xF900 <= o <= 0xFAFF or 0xFE30 <= o <= 0xFE6F
                or 0xFF00 <= o <= 0xFF60 or 0xFFE0 <= o <= 0xFFE6
                or 0x1F300 <= o <= 0x1FAFF):
            w += 2
        else:
            w += 1
    return w


def _fit(s, w):
    """按显示宽度截断（超出用 … 收尾）。"""
    s = str(s)
    if _disp_width(s) <= w:
        return s
    out, cur = "", 0
    for ch in s:
        cw = _disp_width(ch)
        if cur + cw > w - 1:
            break
        out += ch
        cur += cw
    return out + "…"


def _pad(s, w):
    s = _fit(s, w)
    return s + " " * max(0, w - _disp_width(s))


def _card_steps(skel, spec):
    """→ [(seq_txt, title, generator, tool, validator, outputs, optional)]（按 seq 排序）"""
    io_map = step_io_map(spec)
    rows = []
    for s, is_opt in all_steps_optional(skel or {}):
        ref = str(s.get("name") or "")
        ent = io_map.get(ref) or io_map.get(str(s.get("label") or "")) or {}
        label = s.get("label") or ref
        gen = str(ent.get("generator") or s.get("gen") or "—")
        gen = gen.split()[0] if gen else "—"
        title = str(ent.get("title") or s.get("label") or ref)
        vd = ent.get("validator")
        if not vd:
            ck = str(s.get("check") or "—")
            vd = ck
            mk = s.get("marker")
            if mk:
                vd = "%s（%s）" % (ck, _fit(str(mk).split(":")[-1].strip('"'), 18))
            if s.get("run") == "gen":
                vd = "%s（登录节点跑，判 %s）" % (ck, s.get("done_marker") or "产物")
        outs = ent.get("outputs") or []
        outs = ", ".join(str(x) for x in outs) if isinstance(outs, list) else str(outs)
        rows.append((s.get("seq"), str(title), gen, str(ent.get("tool") or "—"),
                     str(vd), outs or "—", bool(is_opt)))
    rows.sort(key=lambda r: (r[0] if isinstance(r[0], (int, float)) else 99, r[1]))
    out = []
    for i, r in enumerate(rows, 1):
        seq = r[0]
        seq_txt = ("%g" % seq) if isinstance(seq, (int, float)) else str(seq or i)
        out.append((seq_txt, r[1], r[2], r[3], r[4], r[5], r[6]))
    return out


def _card_box(seq_txt, title, gen, tool, vd, outs, opt, box_w):
    inner = box_w - 2
    head = _fit(" %s  %s%s" % (seq_txt, title, "  (可选)" if opt else ""), inner)
    lines = ["┌" + "─" * inner + "┐",
             "│" + _pad(head, inner) + "│",
             "├" + "─" * inner + "┤"]
    vals = (_fit(gen, inner), _fit(tool, inner), _fit(vd, inner), _fit(outs, inner))
    for lab, val in zip(_CARD_LABELS, vals):
        lines.append("│" + _pad(lab, inner) + "│")
        lines.append("│" + _pad(val, inner) + "│")
    lines.append("└" + "─" * inner + "┘")
    return lines


def _join_boxes(boxes, arrow_row=1):
    """并排拼盒子；在 arrow_row 那一行用 ──▶ 连接（对应图 2 的横向箭头）。"""
    h = max(len(b) for b in boxes)
    boxes = [b + [" " * _disp_width(b[0])] * (h - len(b)) for b in boxes]
    lines = []
    for i in range(h):
        parts = []
        for j, b in enumerate(boxes):
            parts.append(b[i])
            if j < len(boxes) - 1:
                parts.append(" ──▶ " if i == arrow_row else "     ")
        lines.append("".join(parts).rstrip())
    return lines


def render_skill_card(key, skel, spec, width=118, issues=None):
    """tf skill show <技能>：把技能渲染成一张卡片（Generator/Tool/Validator/Output）。"""
    spec = spec or {}
    steps = _card_steps(skel, spec)
    io = spec.get("io_schema") if isinstance(spec.get("io_schema"), dict) else {}
    fl = spec.get("flow") if isinstance(spec.get("flow"), dict) else {}
    corr = spec.get("corrections") or []
    L = []
    title = "%s — %s  v%s" % (key, skel.get("desc") or "", skel.get("_skill_version") or "?")
    L.append("=" * min(width, 110))
    L.append("Skill: %s" % title)
    tag = "Validated workflow · configurable templates"
    if any(r[3] and r[3] != "—" and ("MACE" in r[3] or "MACE" in r[2]) for r in steps):
        tag += " · replaceable force backend (VASP / MACE)"
    L.append(tag)
    if fl.get("summary"):
        L.append("流程: %s" % fl["summary"])
    L.append("=" * min(width, 110))
    if not steps:
        L.append("（本技能没有声明任何步骤）")
        return "\n".join(L)

    # 盒子宽度：所有步骤取同一个宽度（好看），上限 26
    need = 0
    for sq, ti, ge, to, vd, ou, op in steps:
        need = max(need, _disp_width(" %s  %s%s" % (sq, ti, "  (可选)" if op else "")),
                   _disp_width(ge), _disp_width(to), _disp_width(vd), _disp_width(ou))
    box_w = max(19, min(34, need + 2))

    # 贪心分行：一行放不下就换行
    rows, cur, cur_w = [], [], 0
    for st in steps:
        w = box_w + (5 if cur else 0)
        if cur and cur_w + w > width:
            rows.append(cur)
            cur, cur_w = [], 0
            w = box_w
        cur.append(st)
        cur_w += w
    if cur:
        rows.append(cur)

    for row in rows:
        boxes = [_card_box(*st, box_w=box_w) for st in row]
        L += _join_boxes(boxes)
        L.append("")

    # 尾部：输入 / 参数 / 可接技能 / 纠错
    if io.get("inputs"):
        L.append("输入: %s" % ", ".join(str(i.get("name")) for i in io["inputs"]
                                      if isinstance(i, dict)))
    if io.get("params"):
        L.append("参数: %s" % ", ".join(str(p.get("name")) for p in io["params"]
                                      if isinstance(p, dict)))
    if io.get("outputs"):
        L.append("产物: %s" % ", ".join(str(o.get("name")) for o in io["outputs"]
                                      if isinstance(o, dict)))
    if fl.get("next_skills"):
        nxt = []
        for n in fl["next_skills"]:
            n = {"skill": n} if isinstance(n, str) else (n or {})
            nxt.append("%s%s" % (n.get("skill"), ("（用 %s）" % n.get("via")) if n.get("via") else ""))
        L.append("可接: %s" % ", ".join(nxt))
    if corr:
        L.append("纠错: %s" % ", ".join(c.get("name") if isinstance(c, dict) else str(c)
                                      for c in corr))
    if not io.get("steps"):
        L.append("提示: 本技能还没写 io_schema.steps（Tool 显示 —）。补上以后这张卡片"
                 "就能直接当论文图 2 用：见 skill/_template/skill.yaml。")
    if issues:
        errs, warns = split_issues(issues)
        if errs or warns:
            L.append("校验: %d 错误 / %d 警告（细节看 tf schema %s）" % (len(errs), len(warns), key))
    return "\n".join(L)


def card_dict(key, skel, spec, issues=None):
    """tf skill show --json：卡片的结构化形式（工具/AI 判读用，字段名稳定）。"""
    steps = _card_steps(skel, spec)
    return {
        "skill": key,
        "desc": skel.get("desc"),
        "version": skel.get("_skill_version"),
        "manifest": skel.get("_skill_manifest"),
        "flow_summary": ((spec or {}).get("flow") or {}).get("summary")
        if isinstance((spec or {}).get("flow"), dict) else None,
        "steps": [{"seq": sq, "title": ti, "generator": ge, "tool": to,
                   "validator": vd, "outputs": ou, "optional": op}
                  for sq, ti, ge, to, vd, ou, op in steps],
        "inputs": [i.get("name") for i in ((spec or {}).get("io_schema") or {}).get("inputs") or []
                   if isinstance(i, dict)],
        "params": [p.get("name") for p in ((spec or {}).get("io_schema") or {}).get("params") or []
                   if isinstance(p, dict)],
        "outputs": [o.get("name") for o in ((spec or {}).get("io_schema") or {}).get("outputs") or []
                    if isinstance(o, dict)],
        "corrections": (spec or {}).get("corrections") or [],
        "issues": list(issues or []),
    }


def cmd_skill_show(cfg, which=None, json_out=False, width=118):
    """tf skill show [<技能>] —— 渲染技能卡片（论文图 2 的机器可读来源）。

    纯本地、不采集、不提交、不改文件。不给技能名 = 列出全部技能的一行摘要。
    退出码：0 正常；1 技能不存在。"""
    import json as _json
    skills = cfg.get("_skills") or {}
    if not skills:
        print("没有发现任何技能（skill/*/skill.yaml）。")
        return 0
    wanted = (which or "").strip()
    if not wanted:
        L = ["%-14s %-5s %-8s %s" % ("技能", "步骤", "自描述", "工具链 / 说明")]
        for k in sorted(skills):
            skel = skills[k] or {}
            spec = spec_of(_manifest_of(skel))
            steps = _card_steps(skel, spec)
            tools = []
            for _sq, _ti, _ge, to, _vd, _ou, _op in steps:
                for t in str(to).split("/"):
                    t = t.strip()
                    if t and t != "—" and t not in tools:
                        tools.append(t)
            st = spec_stats(spec)
            spec_txt = "%s%s%s" % ("I" if st["has_io"] else "-",
                                   "F" if st["has_flow"] else "-",
                                   "C" if st["n_corr"] else "-")
            L.append("%-14s %-5d %-8s %s" % (k, len(steps), spec_txt,
                                             _fit(", ".join(tools) or (skel.get("desc") or ""), 64)))
        L.append("")
        L.append("看单个技能：tf skill show <技能名>     机器可读：tf skill show <技能名> --json")
        print("\n".join(L))
        return 0
    if wanted not in skills:
        import difflib
        close = difflib.get_close_matches(wanted, sorted(skills), n=3, cutoff=0.4)
        print("错误：没有技能 '%s'%s" % (wanted,
                                     ("，你是不是想：%s" % ", ".join(close)) if close else "。"))
        print("已发现的技能：%s" % ", ".join(sorted(skills)))
        return 1
    skel = skills[wanted] or {}
    spec = spec_of(_manifest_of(skel))
    issues = None
    try:
        from tfpkg import correction_handler_names
        issues = validate_skill_spec(wanted, _manifest_of(skel), skel=skel,
                                     known_skills=set(skills),
                                     handler_names=correction_handler_names(cfg))
    except Exception:
        issues = None
    if json_out:
        print(_json.dumps(card_dict(wanted, skel, spec, issues), ensure_ascii=False, indent=2))
        return 0
    print(render_skill_card(wanted, skel, spec, width=width or 118, issues=issues))
    return 0

