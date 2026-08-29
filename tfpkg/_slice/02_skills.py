# -*- coding: utf-8 -*-
# 02_skills —— 技能发现 / skill.yaml 清单 / 技能装配
#
# 本分片由 tfpkg/__init__.py 装配器在单一命名空间里按顺序执行；
# 函数之间的引用按名字解析（与原单文件一致），分片间无需 import。
# 内容清单（按原文件行号）：
#   L1104  skill_search_dirs
#   L1124  _load_manifest
#   L1182  discover_skills
#   L1201  _merge_type
#   L1217  apply_skills
#   L1238  _seq_sort_steps
#   L1251  expand_optional_steps
#   L1304  _seq_key
#   L1314  _name_seq
#   L1321  step_seq
#   L1329  skill_checks_for
#   L1346  cmd_skills

# ===== skill_search_dirs (原 L1104-L1121) =====
def skill_search_dirs(cfg):
    """技能搜索路径，靠前优先（同名技能先命中者生效）。"""
    pkg_root = os.path.normpath(os.path.join(
        os.path.dirname(os.path.realpath(__file__)), "..", ".."))
    cdir = cfg.get("_config_dir") or ""
    cands = ["./skill"]
    cands += [os.path.expanduser(str(p)) for p in (cfg.get("skill_paths") or [])]
    if cdir:
        cands += [os.path.join(cdir, "skill"),
                  os.path.normpath(os.path.join(cdir, "..", "skill"))]
    cands += [os.path.join(pkg_root, "skill"), os.path.expanduser("~/.tf/skill")]
    out, seen = [], set()
    for d in cands:
        rd = os.path.realpath(d)
        if rd not in seen and os.path.isdir(rd):
            seen.add(rd)
            out.append(rd)
    return out

# ===== _load_manifest (原 L1124-L1179) =====
def _load_manifest(path):
    """解析单个 skill.yaml；返回 (key, 骨架) 或 (None, 原因)。"""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        return None, "读取失败：%s" % e
    try:
        try:
            import yaml
            man = yaml.safe_load(text) or {}
        except ImportError:
            man = _mini_yaml(text) or {}
    except Exception as e:
        return None, "解析失败：%s" % e
    if not isinstance(man, dict):
        return None, "顶层不是字典"
    try:
        schema = int(man.get("schema") or 1)
    except (TypeError, ValueError):
        return None, "schema 不是整数"
    if schema > SKILL_SCHEMA_MAX:
        return None, ("schema %d 高于本版 tf 支持的 %d，请升级 tf"
                      % (schema, SKILL_SCHEMA_MAX))
    sdir = os.path.dirname(os.path.realpath(path))
    key = str(man.get("name") or os.path.basename(sdir)).strip()
    if not key:
        return None, "缺少 name"
    if man.get("enabled") is False:
        return None, "__disabled__"
    skel = dict(man.get("defaults") or {})
    for k in _MANIFEST_TYPE_KEYS:
        if k in man and man[k] is not None:
            skel[k] = man[k]
    if not skel.get("steps"):
        return None, "没有 steps"
    skel["skill_dir"] = sdir               # 绝对路径，find_asset 直接可用
    skel.setdefault("desc", key)
    skel["_skill_manifest"] = path
    skel["_skill_version"] = man.get("version")
    skel["_skill_requires"] = man.get("requires") or {}
    chk = man.get("checks", "checks.py")
    cp = os.path.join(sdir, str(chk)) if chk else None
    # patch_common_opt：公共判据文件随 _common 重构挪进了公共步骤子目录（opt/ 等），
    # 但老 skill.yaml 仍写重构前路径（../_common/checks_relax.py）。字面路径找不到、
    # 且指向公共池时，按 basename 在 _common/*/ 里兜底一层——与 find_asset /
    # dim_common 加载里 <pool>/*/ 的处理保持一致，skill.yaml 一行都不用改。
    if cp and chk and not os.path.isfile(cp) and COMMON_POOL_DIR in str(chk):
        _pool = os.path.normpath(os.path.join(sdir, os.path.dirname(str(chk))))
        for _hit in sorted(glob.glob(os.path.join(_pool, "*",
                                                   os.path.basename(str(chk))))):
            if os.path.isfile(_hit):
                cp = _hit
                break
    skel["_skill_checks"] = cp if (cp and os.path.isfile(cp)) else None
    return key, skel

# ===== discover_skills (原 L1182-L1198) =====
def discover_skills(cfg, verbose=False):
    """扫描所有搜索路径，返回 {key: 骨架}；靠前路径优先，同名不覆盖。"""
    found, bad = {}, []
    for base in skill_search_dirs(cfg):
        for mp in sorted(glob.glob(os.path.join(base, "*", SKILL_MANIFEST))):
            key, skel = _load_manifest(mp)
            if key is None:
                if skel != "__disabled__":
                    bad.append((mp, skel))
                continue
            if key in found:
                continue
            found[key] = skel
    if bad and verbose:
        for mp, why in bad:
            sys.stderr.write("警告：技能清单 %s 已忽略（%s）\n" % (mp, why))
    return found

# ===== _merge_type (原 L1201-L1214) =====
def _merge_type(skel, over):
    """技能骨架 + 用户覆盖。标量/列表整体覆盖，一层字典递归合并；
    用户显式写 steps 就完全接管（保留手改逃生口）。"""
    out = dict(skel)
    for k, v in (over or {}).items():
        if v is None:
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            m = dict(out[k])
            m.update(v)
            out[k] = m
        else:
            out[k] = v
    return out

# ===== apply_skills (原 L1217-L1235) =====
def apply_skills(cfg, verbose=False):
    """把发现到的技能并进 cfg['task_types']；已有同名段作为覆盖层。
    必须在 merge_project_configs 之前调用（项目段要叠在骨架之上）。"""
    skills = discover_skills(cfg, verbose=verbose)
    white = cfg.get("enabled_skills")
    black = set(cfg.get("disabled_skills") or [])
    tt = dict(cfg.get("task_types") or {})
    for key, skel in skills.items():
        if white and key not in white:
            continue
        if key in black:
            tt.pop(key, None)
            continue
        tt[key] = _merge_type(skel, tt.get(key) or {})
    for key in black:      # 黑名单也能关掉纯 tf.yaml 定义的类型
        tt.pop(key, None)
    cfg["task_types"] = tt
    cfg["_skills"] = skills
    return cfg

# ===== _seq_sort_steps (原 L1238-L1248) =====
def _seq_sort_steps(steps):
    """按 seq 稳定重排步骤（seq 解析不出来的保持原相对位置，不打乱老技能）。"""
    _keyed, _last = [], -1.0
    for _i, _s in enumerate(steps):
        _v = _seq_key(step_seq(_s))
        if _v is None:
            _v = _last + 1e-6 * (_i + 1)
        else:
            _last = _v
        _keyed.append((_v, _i, _s))
    return [_x[2] for _x in sorted(_keyed, key=lambda _x: (_x[0], _x[1]))]

# ===== expand_optional_steps (原 L1251-L1301) =====
def expand_optional_steps(t):
    """可选步骤组展开（取代写死的 PLOT_STEP_DEFS / _inject_plot_steps）。
    optional_steps.<开关名>.{default, steps[]}；步骤里的 after 是锚点名前缀，
    命中则插在最后一个匹配项之后，锚点不存在就不注入。
    先按名字剔除再插入，顺带修掉「段之间浅拷贝共享 steps 列表」的老问题。
    被关掉的组记到 t['_optional_off']（flag -> defs）与 t['_optional_off_flat']
    （name/label/seq -> (flag, def)），供 -j <步骤> start 按需启用。"""
    steps = t.get("steps")
    if not isinstance(steps, list):
        return
    steps = list(steps)
    off = {}
    for flag, spec in (t.get("optional_steps") or {}).items():
        spec = spec or {}
        defs = spec.get("steps") or []
        names = {d.get("name") for d in defs}
        steps = [s for s in steps if s.get("name") not in names]
        if t.get(flag, spec.get("default", True)) is False:
            off[flag] = [dict(d) for d in defs]
            continue
        for d in defs:
            anchor = d.get("after")
            d2 = {k: v for k, v in d.items() if k != "after"}
            pos = None
            if anchor:
                for i, s in enumerate(steps):
                    if str(s.get("name", "")).startswith(str(anchor)):
                        pos = i
                if pos is None:
                    continue
            steps.insert(pos + 1 if pos is not None else len(steps), d2)
    # patch_seq_order：各可选组按"声明顺序 + 插在锚点紧后面"注入，会互相挤位
    # （band 声明 plot_steps -> vacuum_align -> bandgap_hse，结果 seq 4 的
    # step4_HSE_band 锚在 step3_PBE_WAVECAR 上，把已插好的 3.1/3.2 挤到 4.x
    # 后面）。这既让表头乱序，也让 _dag_needs 的"上一步"回退连错依赖 ——
    # S3.1_plot 会挂在 step4_vacuum 后面，PBE 能带图白等整条 HSE 链。
    # 这里按 seq 稳定重排；seq 解析不出来的保持原相对位置，不打乱老技能。
    t["steps"] = _seq_sort_steps(steps)
    if off:
        t["_optional_off"] = off
        flat = {}
        for flag, defs in off.items():
            for d in defs:
                for k in ("name", "label", "seq"):
                    v = d.get(k)
                    if v is not None:
                        flat[str(v)] = (flag, d)
        t["_optional_off_flat"] = flat
    else:
        t.pop("_optional_off", None)
        t.pop("_optional_off_flat", None)

# ===== _seq_key (原 L1304-L1311) =====
def _seq_key(v):
    """把 seq / -j token 归一成可比较的数：'2'->2.0，'2.1'->2.1，非数->None。"""
    if v is None:
        return None
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None

# ===== _name_seq (原 L1314-L1318) =====
def _name_seq(name):
    """从步骤名抽序号：step2 -> 2.0，step2.1_static -> 2.1，step1a_opt -> 1.0。
    抓 'step' 后面的数字（可带一位小数），字母后缀（a/b/c）忽略。"""
    m = re.match(r"step(\d+(?:\.\d+)?)", str(name or ""))
    return float(m.group(1)) if m else None

# ===== step_seq (原 L1321-L1326) =====
def step_seq(s):
    """步骤序号：优先清单里的 seq，缺省从 stepN 名字推。"""
    if s.get("seq") is not None:
        return str(s["seq"])
    m = re.match(r"step(\d+)", str(s.get("name", "")))
    return m.group(1) if m else None

# ===== skill_checks_for (原 L1329-L1343) =====
def skill_checks_for(cfg, keys):
    """收集这些技能的私有判据源码 {key: 源码}，随采集器 payload 下发。"""
    out = {}
    for k in keys:
        if k in out:
            continue
        p = ((cfg.get("_skills") or {}).get(k) or {}).get("_skill_checks")
        if not p:
            continue
        try:
            with open(p, encoding="utf-8") as f:
                out[k] = f.read()
        except OSError as e:
            sys.exit("错误：技能 %s 的判据文件 %s 读取失败（%s）。" % (k, p, e))
    return out

# ===== cmd_skills (原 L1346-L1368) =====
def cmd_skills(cfg, tt=None):
    """tf skills —— 列出已发现的技能。"""
    skills = cfg.get("_skills") or discover_skills(cfg, verbose=True)
    if not skills:
        print("没有发现任何技能清单（skill/*/skill.yaml）。搜索路径：")
        for d in skill_search_dirs(cfg):
            print("  " + d)
        return 0
    black = set(cfg.get("disabled_skills") or [])
    white = cfg.get("enabled_skills")
    print("%-10s %-8s %-6s %-6s %s" % ("技能", "版本", "步骤", "状态", "清单"))
    for k in sorted(skills):
        if tt and k != tt:
            continue
        s = skills[k]
        st = "关闭" if (k in black or (white and k not in white)) else "启用"
        print("%-10s %-8s %-6d %-6s %s"
              % (k, s.get("_skill_version") or "-", len(s.get("steps") or []),
                 st, s.get("_skill_manifest")))
    print("\n搜索路径（靠前优先）：")
    for d in skill_search_dirs(cfg):
        print("  " + d)
    return 0

