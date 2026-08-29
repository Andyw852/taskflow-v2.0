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
        _here = os.path.dirname(os.path.realpath(__file__))
        for _sdir in (os.path.join(_here, "..", "..", "setting"),
                      os.path.join(_here, "..", "setting"),
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
            pkg_root = os.path.normpath(os.path.join(   # （tf.yaml 放 setting/ 时
                os.path.dirname(os.path.realpath(__file__)), "..", ".."))  # 也能找对）
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
        pkg_root = os.path.normpath(os.path.join(
            os.path.dirname(os.path.realpath(__file__)), "..", ".."))
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
            pkg_root = os.path.normpath(os.path.join(
                os.path.dirname(os.path.realpath(__file__)), "..", ".."))
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

