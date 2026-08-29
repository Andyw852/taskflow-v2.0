# -*- coding: utf-8 -*-
# 03_projects —— 项目配置扫描与合并 / 任务类型解析
#
# 本分片由 tfpkg/__init__.py 装配器在单一命名空间里按顺序执行；
# 函数之间的引用按名字解析（与原单文件一致），分片间无需 import。
# 内容清单（按原文件行号）：
#   L1371  scan_project_configs
#   L1415  _stepconf_param_from_file
#   L1434  resolve_stepconf_flags
#   L1445  merge_project_configs
#   L1492  _filter_run_steps
#   L1530  get_types
#   L1595  step_cfg

# ===== scan_project_configs (原 L1371-L1412) =====
def scan_project_configs(roots):
    """扫描项目根下 project_setting/tf_*.yaml。
    返回 [(配置名, 路径, 项目目录)]；配置名（tf_<名>.yaml 的 <名>）全局唯一，重复即报错。
    v-perf：单次 scandir 遍历（只下探目录、不枚举文件），深度≤6，跳过
    result/log/隐藏目录——比旧实现 7 个 glob（各遍历整棵树）更快，且不枚举
    数据文件（os.walk 在 WSL DrvFS 等慢盘上枚举文件极慢）。"""
    seen, found = {}, []
    for r in roots:
        r = os.path.realpath(os.path.expanduser(str(r)))
        if not os.path.isdir(r):
            print("警告：project_roots 里的 %s 不存在，跳过。" % r, file=sys.stderr)
            continue
        base_depth = r.rstrip(os.sep).count(os.sep)
        stack = [r]
        while stack:
            d = stack.pop()
            if d.count(os.sep) - base_depth > 6:
                continue
            try:
                it = os.scandir(d)
            except OSError:
                continue
            ps_entries, subdirs = [], []
            with it:
                for e in it:
                    if not e.is_dir(follow_symlinks=False):
                        continue
                    if e.name == "project_setting":
                        ps_entries.append(e.path)
                    elif e.name not in ("result", "log") and not e.name.startswith("."):
                        subdirs.append(e.path)
            for ps in ps_entries:
                for p in sorted(glob.glob(os.path.join(ps, "tf_*.yaml"))):
                    name = os.path.basename(p)[len("tf_"):-len(".yaml")]
                    if name in seen and seen[name] != p:
                        sys.exit("错误：项目配置名重复 tf_%s.yaml：\n  %s\n  %s\n"
                                 "命名规则 tf_<项目名>.yaml，全局唯一，请改其中一个的名字。"
                                 % (name, seen[name], p))
                    seen[name] = p
                    found.append((name, p, os.path.dirname(os.path.dirname(p))))
            stack.extend(subdirs)
    return found

# ===== _stepconf_param_from_file (原 L1415-L1431) =====
def _stepconf_param_from_file(path, key):
    """极简读取 step.conf 里某 [params] 键的值（行尾 # / ! 注释剥掉）。
    找不到文件/键返回 None。只用于驱动装配阶段读 BANDGAP 这类步骤图开关，
    不替代 gen 脚本侧的 stepconf.py 完整合并。"""
    try:
        with open(path, encoding="utf-8-sig") as fh:
            for ln in fh:
                s = re.sub(r"\s+[#!].*$", "", ln).strip()
                if not s or s.startswith(("#", "!")):
                    continue
                if "=" in s:
                    k, v = s.split("=", 1)
                    if k.strip() == key:
                        return v.strip()
    except OSError:
        pass
    return None

# ===== resolve_stepconf_flags (原 L1434-L1442) =====
def resolve_stepconf_flags(seg, ps_dir):
    """把项目共用 step.conf 里的步骤图开关映射成可选步骤组的开/关，注入 seg。
    目前一条：BANDGAP = pbe|hse  ->  bandgap_hse（pbe 关掉整段 HSE）。
    仅在 seg 未显式写该开关时生效（项目配置里手写 bandgap_hse 优先级更高）。"""
    scp = os.path.join(ps_dir, "templates", "step.conf")
    if "bandgap_hse" not in seg:
        bg = _stepconf_param_from_file(scp, "BANDGAP")
        if bg is not None:
            seg["bandgap_hse"] = (bg.lower() != "pbe")

# ===== merge_project_configs (原 L1445-L1489) =====
def merge_project_configs(cfg):
    """把项目配置（project_setting/tf_*.yaml）的 task_types 合并进全局配置。
    同 key：项目配置作为该类型的一个"段"（独立 local_root，缺省字段继承全局/主定义）。
    local_root 缺省 = 该 project_setting 的父目录（项目自包含，可不写）。"""
    roots = cfg.get("project_roots") or []
    if not roots:
        roots = [t.get("local_root") for t in (cfg.get("task_types") or {}).values()
                 if isinstance(t, dict) and t.get("local_root")]
    found = scan_project_configs(roots)
    if not found:
        return cfg
    tt = cfg.setdefault("task_types", {})
    for name, path, proj_dir in found:
        try:
            pc = _load_yaml_file(path)
        except OSError as e:
            sys.exit("错误：项目配置 %s 读取失败：%s" % (path, e))
        for key, seg in (pc.get("task_types") or {}).items():
            seg = dict(seg or {})
            seg["_base_dir"] = os.path.dirname(path)
            lr = seg.get("local_root")
            if lr:
                lr = str(lr)
                if not os.path.isabs(lr):
                    lr = os.path.normpath(os.path.join(proj_dir, lr))
                seg["local_root"] = os.path.expanduser(lr)
            elif os.path.isfile(os.path.join(proj_dir, "POSCAR")):
                # 材料级 project_setting（qHPC20/project_setting）：
                # 缺省 local_root = 上一级（材料名 = 目录名，如 qHPC20）
                seg["local_root"] = os.path.dirname(proj_dir)
            elif os.path.isfile(os.path.join(os.path.dirname(proj_dir), "POSCAR")):
                # v1.7：技能子目录级 project_setting（Mg2C60/band/project_setting）：
                # proj_dir(=Mg2C60/band) 不是材料，其父(=Mg2C60)才是材料 →
                # local_root = 材料的父目录，材料才能被发现。
                seg["local_root"] = os.path.dirname(os.path.dirname(proj_dir))
            else:
                # 项目/体系级（C20/project_setting）：缺省 local_root = 父目录
                seg["local_root"] = proj_dir
            seg["_from"] = os.path.realpath(path)
            resolve_stepconf_flags(seg, os.path.dirname(path))  # step.conf 的 BANDGAP -> bandgap_hse
            if key in tt and tt[key] is not seg:
                tt[key].setdefault("_segments", []).append(seg)
            else:
                tt[key] = seg
    return cfg

# ===== _filter_run_steps (原 L1492-L1527) =====
def _filter_run_steps(t):
    """run_steps: 只保留指定步骤（子集，顺序不变）。
    元素写法：序号 1/2/3/4（第 N 个计算步骤；三段式弛豫时 1 = step1a/b/c
    三段全含，只跑某一段就写步骤名或 label，如 step1a_PBE_opt / S1a_ion）、
    3.1/4.1（画图步骤）。未配置 = 全部步骤。"""
    rs = t.get("run_steps")
    if not rs:
        return
    steps = t.get("steps") or []
    toks = rs if isinstance(rs, list) else [rs]

    def match(s, tok):
        ts = str(tok).strip()
        if ts == str(s.get("name", "")) or ts == str(s.get("label", "")):
            return True
        # v1.8：seq 优先，其次名字里的点号序号；点号精确比较，不做前缀近似
        want = _seq_key(ts)
        if want is None:
            return False
        sq = _seq_key(step_seq(s))
        if sq is None:
            sq = _name_seq(s.get("name"))
        return sq is not None and abs(sq - want) < 1e-9

    kept, kept_ids = [], set()
    for tok in toks:
        for s in steps:
            if match(s, tok) and id(s) not in kept_ids:
                kept.append(s)
                kept_ids.add(id(s))
    if not kept:
        sys.exit("错误：%s 的 run_steps 没有匹配到任何步骤（可用：%s）。"
                 % (t["key"], ", ".join(str(s.get("name")) for s in steps)))
    order = {id(s): i for i, s in enumerate(steps)}
    kept.sort(key=lambda s: order[id(s)])
    t["steps"] = kept

# ===== get_types (原 L1530-L1592) =====
def get_types(cfg, tt=None, root_override=None, quiet=False):
    """把配置归一化成类型列表；应用 -tt 过滤和 ROOT 覆盖。
    项目配置合并进来的同 key 段在此展开为多个类型实例（key 相同，local_root 不同）。"""
    raw = cfg.get("task_types")
    types = []
    if raw:
        for k, tc in raw.items():
            t = dict(tc or {})
            t["key"] = str(k)
            segs = t.pop("_segments", []) or []
            for s in segs:                       # 段 = 主定义 + 覆盖字段
                s2 = dict(t)
                s2.update({k2: v for k2, v in s.items() if v is not None})
                s2["key"] = str(k)
                types.append(s2)
            # 主定义自身无 local_root/root 时只作骨架（发现交给各段；
            # 暂时没有段也不报错——比如刚 init 一个新项目之前）
            if t.get("local_root") or t.get("root"):
                types.append(t)
    else:
        t = {k: cfg[k] for k in ("root", "steps", "gen_dir", "materials", "desc")
             if k in cfg}
        t["key"] = "-"
        types.append(t)
    # v1.1：all_keys 取全局定义的骨架全集——只定义了骨架、还没有项目挂段的
    # 类型（如新加的 elastic）此前不在 types 里，-tt 会误报"没有任务类型"
    all_keys = [str(k) for k in (raw or {}).keys()] or [t["key"] for t in types]
    for t in types:
        if not t.get("root"):
            t["root"] = cfg.get("root")
        t.setdefault("desc", t["key"])
    if tt:
        if tt not in all_keys:
            sys.exit("错误：没有任务类型 '%s'（已定义：%s）。"
                     % (tt, ", ".join(all_keys)))
        types = [t for t in types if t["key"] == tt]
        if not types and not quiet:   # init 时这条是废话（正要去生成），不打
            print("（%s：类型已在全局 tf.yaml 定义，但还没有项目挂这个类型。\n"
                  "  在材料的 project_setting/tf_<项目名>.yaml 里加一段  %s:  ，\n"
                  "  或用  tf -tt %s -p <材料> init  生成项目配置。）" % (tt, tt, tt))
    if root_override:
        if len(types) != 1:
            sys.exit("错误：配置了多个任务类型时，命令行指定 ROOT 必须同时用 -tt 指定类型。")
        types[0]["root"] = root_override
    for t in types:
        expand_optional_steps(t)   # v1.2：按技能清单展开可选步骤组
        _filter_run_steps(t)    # v1.0：run_steps 自定义步骤子集
    for t in types:
        if not t.get("root") and not t.get("local_root"):
            sys.exit("错误：任务类型 %s 没有 root/local_root"
                     "（在配置里写 local_root 或 root，或命令行指定）。" % t["key"])
        if t.get("local_root") and t.get("steps") is None:
            skels = [k for k, tc in (cfg.get("task_types") or {}).items()
                     if tc and tc.get("steps")]
            src = t.get("_from") or "project_setting/tf_*.yaml"
            sys.exit("错误：类型 %s 在全局 tf.yaml 里没有对应定义（继承不到 steps）。\n"
                     "来源文件：%s\n"
                     "通常是该文件的类型名和全局 tf.yaml 不一致（如 bd 改名 band 后没同步）。\n"
                     "全局可用的类型名：%s。修正命令：\n"
                     "  sed -i 's/^  %s:/  %s:/' %s"
                     % (t["key"], src, ", ".join(skels) or "（无）",
                        t["key"], skels[0] if skels else "band", src))
    return types

# ===== step_cfg (原 L1595-L1602) =====
def step_cfg(t, sname, m=None):
    """步骤配置；v3.1 起材料可携带所属段（_seg），段配置优先于类型条目。"""
    steps = ((m or {}).get("_seg") or {}).get("steps_cfg") \
        or t.get("steps_cfg") or t.get("steps") or []
    for s in steps:
        if s.get("name") == sname:
            return s
    return {}

