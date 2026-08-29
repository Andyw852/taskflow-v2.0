# -*- coding: utf-8 -*-
# 14_init —— 项目初始化（init / init_skill / yaml block 编辑）
#
# 本分片由 tfpkg/__init__.py 装配器在单一命名空间里按顺序执行；
# 函数之间的引用按名字解析（与原单文件一致），分片间无需 import。
# 内容清单（按原文件行号）：
#   L5272  cmd_init
#   L5423  _scan_root_dirs
#   L5441  resolve_mat_dir
#   L5490  skill_keys
#   L5497  _init_one
#   L5541  _scope_to_material
#   L5559  _init_one_skill
#   L5795  _yaml_type_block_ensure
#   L5830  _yaml_type_block_set
#   L5878  _yaml_type_block_remove

# ===== cmd_init (原 L5272-L5417) =====
def cmd_init(cfg, types, proj, name=None, tt=None, force=False, yes=False):
    """初始化项目配置。
    -p 指定材料 → 只初始化这些材料（多个用逗号分隔，如 -p Mg2C60,Mo2S3）；
    不带 -p → 当前目录下所有项目批量初始化（cwd 下一层就是材料目录时，
    cwd 本身作为一个项目）。位置参数可指定项目名（tf init 名字）。
    不带 -tt 时对【全部技能】各建一套 project_setting；动手前先列计划并确认（-y 跳过）。"""
    _keys = skill_keys(cfg, tt)
    if not _keys:
        print("错误：tf.yaml 里没有定义任何技能（task_types 为空）。")
        return 1
    if not yes and not tt:   # 明确指定了 -tt 就是明确选择，不再确认
        print("tf init 将为下列技能各建一套项目配置：%s" % "、".join(_keys))
        print("范围：%s" % (("材料 " + "、".join(
            x.strip() for x in str(proj).split(",") if x.strip())) if proj
            else "当前目录下所有材料（%s）" % os.getcwd()))
        print("init 只在本地生成配置和模板，不连超算、不提交任何计算；")
        print("要开算是之后的 tf start。已存在的文件不覆盖（除非加 -f）。")
        print("（只想初始化其中一个技能就加 -tt，如 tf -tt band init）")
        try:
            ans = input("继续？ [y/N] ").strip().lower()
        except EOFError:
            ans = ""
        if ans not in ("y", "yes"):
            print("已取消操作。")
            return 1
    # 预扫一次所有项目配置名（tf_*.yaml）→ {项目名: 路径}，批量 init 时下发给
    # _init_one/_init_one_skill 做 O(1) 查重。否则每个材料都全树重扫一遍，
    # 大体系（成千上万个材料）会退化成 O(N²)，极其缓慢。
    known_names = {n: p for n, p, _d in scan_project_configs(
        cfg.get("project_roots") or [cfg.get("_config_dir")])}
    if proj:
        # v1.9.8：-p 支持逗号分隔的多个材料（-p Mg2C60,Mo2S3）；定位交给
        # resolve_mat_dir，正常发现失败时会扫盘兜底。
        wants = [x.strip() for x in str(proj).split(",") if x.strip()]
        fails = 0
        _targets = []                       # [(want, lpath)]
        for w in wants:
            lp = resolve_mat_dir(cfg, types, tt, w)
            if not lp:
                print("错误：找不到材料 %s —— 当前目录、project_roots 和各技能的 "
                      "local_root 下都没有同名的、带 POSCAR 的目录。" % w)
                fails += 1
                continue
            _targets.append((w, lp))
        _nm = name if len(wants) == 1 else None
        if len(_targets) == 1:              # 单材料：保留原串行路径（name 命名 + 完整输出）
            fails += _init_one(cfg, types, _targets[0][1], _nm,
                               tt=tt, force=force, known_names=known_names)
        elif _targets:                      # 多材料：并行 init（与不带 -p 的批量同策略）
            import concurrent.futures as _cf
            _nw = int(os.environ.get("TF_INIT_WORKERS", "16") or 16)
            _real_out = sys.stdout
            _failed = []

            def _work(item):
                _w, _lp = item
                return _w, _lp, _init_one(cfg, types, _lp, None, tt=tt,
                                          force=force, known_names=known_names)

            with _cf.ThreadPoolExecutor(max_workers=_nw) as _ex:
                _futs = [_ex.submit(_work, it) for it in _targets]
                try:
                    sys.stdout = open(os.devnull, "w")
                    _n = 0
                    for _f in _cf.as_completed(_futs):
                        _w, _lp, _rc = _f.result()
                        _n += 1
                        if _rc:
                            _failed.append((_w, _lp))
                        if _n % 200 == 0 or _n == len(_targets):
                            print("  init %d/%d（失败 %d）"
                                  % (_n, len(_targets), len(_failed)),
                                  file=sys.stderr, flush=True)
                finally:
                    sys.stdout = _real_out
            for _w, _lp in _failed:
                print("== %s == 失败，串行重跑以显示报错：" % _w)
                fails += _init_one(cfg, types, _lp, None, tt=tt, force=force,
                                   known_names=known_names)
        return fails
    import glob as _glob
    cwd = os.getcwd()
    if os.path.isfile(os.path.join(cwd, "POSCAR")):
        return _init_one(cfg, types, cwd, name, tt=tt, force=force,
                         known_names=known_names)  # cwd 本身是材料目录
    matdirs = sorted({os.path.dirname(p)
                      for pat in ("*/POSCAR", "*/*/POSCAR", "*/*/*/POSCAR")
                      for p in _glob.glob(os.path.join(cwd, pat))})
    if not matdirs:
        print("错误：当前目录下没有发现材料目录（含 POSCAR）。")
        return 1
    # v1.5：批量 init 只初始化 project_roots 覆盖范围内的目录——在家目录等
    # 大范围目录下运行时，其他工作流的材料（不在 tf 管理内）跳过不误建。
    roots = [os.path.realpath(os.path.expanduser(r))
             for r in (cfg.get("project_roots") or [])]
    def _managed(d):
        rd = os.path.realpath(d)
        return any(rd == r or rd.startswith(r + os.sep) for r in roots)
    managed, skipped = [], []
    for d in matdirs:
        (managed if _managed(d) else skipped).append(d)
    for d in skipped:
        print("跳过 %s（不在 tf.yaml 的 project_roots 内；要纳入管理，"
              "先把它的根目录加进 project_roots）" % os.path.relpath(d, cwd))
    if not managed:
        print("错误：发现的材料目录都不在 project_roots 内，未初始化任何项目。")
        return 1
    matdirs = managed
    fails, done = 0, 0
    # v1.2/v1.9.4：已初始化项目也进 _init_one 补齐缺的技能段（批量挂新技能）。
    # 大体系并行 init：每个材料只写自己的 project_setting，互不依赖，用线程池
    # 并行纯本地 I/O（配合上面的 O(1) 查重，整体 O(N)）。循环期间 stdout 静默
    # 以免成千上万个材料刷屏交错，进度走 stderr；失败的材料结束后串行重跑回显。
    import concurrent.futures as _cf
    _nw = int(os.environ.get("TF_INIT_WORKERS", "16") or 16)
    _real_out = sys.stdout
    _failed = []

    def _init_work(d):
        return d, _init_one(cfg, types, d, None, tt=tt, force=force,
                            known_names=known_names)

    with _cf.ThreadPoolExecutor(max_workers=_nw) as _ex:
        _futs = [_ex.submit(_init_work, d) for d in matdirs]
        try:
            sys.stdout = open(os.devnull, "w")
            _n = 0
            for _f in _cf.as_completed(_futs):
                _d, _rc = _f.result()
                _n += 1
                if _rc:
                    _failed.append(_d)
                else:
                    done += 1
                if _n % 200 == 0 or _n == len(matdirs):
                    print("  init %d/%d（失败 %d）" % (_n, len(matdirs), len(_failed)),
                          file=sys.stderr, flush=True)
        finally:
            sys.stdout = _real_out
    for d in _failed:
        print("== %s == 失败，串行重跑以显示报错：" % os.path.relpath(d, cwd))
        fails += _init_one(cfg, types, d, None, tt=tt, force=force,
                           known_names=known_names)
    if not fails:
        print("材料初始化就绪（新 %d 个）。tf 查看状态，tf start 全部开始。" % done)
    return fails

# ===== _scan_root_dirs (原 L5423-L5435) =====
def _scan_root_dirs(root):
    """扫一个根下所有带 POSCAR 的目录（缓存）。批量 auto/clean 反复按名解析时，
    串行逐材料 glob 整个 project_roots（/mnt/d 这种 9p 挂载极慢）会退化成 O(N*树)，
    这里扫一次缓存、后面 O(1) 查表。"""
    if root in _MAT_DIR_CACHE:
        return _MAT_DIR_CACHE[root]
    out = []
    for pat in ("*/POSCAR", "*/*/POSCAR", "*/*/*/POSCAR"):
        for pp in sorted(glob.glob(os.path.join(root, pat))):
            d = os.path.dirname(pp)
            out.append((os.path.relpath(d, root), os.path.basename(d), d))
    _MAT_DIR_CACHE[root] = out
    return out

# ===== resolve_mat_dir (原 L5441-L5487) =====
def resolve_mat_dir(cfg, types, tt, want, cwd=None):
    """按名字定位材料的本地目录，找不到返回 None。
    先走正常发现（local_root -> discover_local）；再扫盘兜底——clean 删光
    project_setting 后 local_root 也跟着没了，只能靠扫盘自举回来。
    批量 -p 时同一 local_root 的 discover_local 结果做进程内缓存，避免
    M 材料 × T 类型 重复扫盘。"""
    tries = [types or []]
    if tt:
        try:
            tries.append(get_types(cfg, tt=None, quiet=True))
        except SystemExit:
            pass
    for tlist in tries:
        for t0 in tlist:
            if not t0.get("local_root"):
                continue
            _key = os.path.realpath(os.path.expanduser(t0["local_root"]))
            mats = _RESOLVE_DISC_CACHE.get(_key)
            if mats is None:
                try:
                    _r, mats = discover_local(t0["local_root"])
                except Exception:
                    _RESOLVE_DISC_CACHE[_key] = []   # 发现失败也缓存空，避免重试
                    continue
                _RESOLVE_DISC_CACHE[_key] = mats
            for m in mats:
                if m["name"] == want or os.path.basename(m["name"]) == want:
                    return m["lpath"]
    roots = [cwd or os.getcwd()]
    for r in (cfg.get("project_roots") or []):
        roots.append(str(r))
    for t0 in (types or []):
        if t0.get("local_root"):
            roots.append(t0["local_root"])
    seen = set()
    for r in roots:
        r = os.path.abspath(os.path.expanduser(str(r)))
        if r in seen or not os.path.isdir(r):
            continue
        seen.add(r)
        if os.path.basename(r) == want and os.path.isfile(
                os.path.join(r, "POSCAR")):
            return r
        for rel, base, d in _scan_root_dirs(r):
            if rel == want or base == want:
                return d
    return None

# ===== skill_keys (原 L5490-L5494) =====
def skill_keys(cfg, tt=None):
    """要初始化哪些技能。给了 -tt 就只有它；否则 tf.yaml 里定义的全部技能。"""
    if tt:
        return [tt]
    return [k for k in (cfg.get("task_types") or {}) if k]

# ===== _init_one (原 L5497-L5538) =====
def _init_one(cfg, types, target, name=None, tt=None, force=False, brief=None,
              known_names=None):
    """给 target 材料目录初始化【全部技能】（或 -tt 指定的那一个）。
    brief=None 时自动判断：一次建多个技能就每个技能只打一行摘要，避免刷屏。
    known_names：批量 init 预扫好的 {项目名: tf_*.yaml 路径}，用于 O(1) 查重
    （避免每个材料都全树重扫一遍；大体系下这是 O(N²) → O(N) 的关键）。"""
    import contextlib as _ctx
    import io as _io
    keys = skill_keys(cfg, tt)
    if brief is None:
        brief = len(keys) > 1
    fails = 0
    for k in keys:
        if not brief:
            fails += _init_one_skill(cfg, types, target, name, tt=k, force=force,
                                     known_names=known_names)
            continue
        buf = _io.StringIO()
        with _ctx.redirect_stdout(buf):
            rc = _init_one_skill(cfg, types, target, name, tt=k, force=force,
                                 known_names=known_names)
        fails += rc
        out = buf.getvalue()
        n_new = out.count("已生成 ")
        n_skip = out.count("已存在，跳过")
        mt = re.search(r"已(?:复制|覆盖) (\d+) 个模板/配置", out)
        if n_new:
            msg = "新建 %d 个配置" % n_new
        elif n_skip:
            msg = "已存在，未改动"
        else:
            msg = "就绪"
        if mt:
            msg += "，模板 %s 个" % mt.group(1)
        print("  %-9s %s" % (k, msg))
        for ln in out.splitlines():      # 错误和需要人工处理的提示照常显示
            if ln.startswith(("错误", "提示：skill_dir")):
                print("    " + ln)
    if brief:
        print("  → 配置在 %s/<技能>/project_setting/（改这里只影响本材料）"
              % os.path.basename(os.path.abspath(target)))
    return fails

# ===== _scope_to_material (原 L5541-L5556) =====
def _scope_to_material(content, tkey):
    """kls7-scope：skill_subdir 布局下把项目配置的发现范围锁到本材料。

    <材料>/<技能>/project_setting 的缺省 local_root 是"材料的父目录"，
    discover_local 会把同级所有带 POSCAR 的目录都扫成本技能的材料 ——
    在一个材料下 init 某技能，兄弟材料全被拉进该技能的表。
    显式写 local_root: ".."（相对 project_setting 的父目录 = <材料>/<技能>）
    即指向材料目录本身，只发现这一个材料。要整批管就到上级目录 tf init。
    """
    if re.search(r"(?m)^\s+local_root:", content):
        return content
    m = re.search(r"(?m)^(\s*)%s:\s*$" % re.escape(str(tkey or "")), content)
    if not m:
        return content
    line = '%s  local_root: ".."   # 只发现本材料；整批管请到上级目录 tf init\n' % m.group(1)
    return content[:m.end()] + "\n" + line + content[m.end() + 1:]

# ===== _init_one_skill (原 L5559-L5792) =====
def _init_one_skill(cfg, types, target, name=None, tt=None, force=False,
                    known_names=None):
    """在 target 目录生成 project_setting/（tf_<项目名>.yaml + setting.yaml +
    hpc.yaml + 映射模板）。已存在的文件不覆盖；项目配置名全局唯一，重复即报错。
    known_names：批量 init 预扫好的 {项目名: tf_*.yaml 路径}，传入则查重 O(1)
    （不再每个材料全树重扫），新建成功后同步加入该集合。"""
    cands = [x for x in types if x.get("local_root")]
    if tt:   # v1.9.4：锁定到该技能，否则总是拿到排在最前面的 band
        cands = [x for x in cands if x.get("key") == tt]
    t = next((tt for tt in cands if not tt.get("_from")), None)
    tkey = (t or {}).get("key")
    if t is None:  # 模板字段优先继承全局骨架（无 local_root 的主定义）
        # v1.2：-tt 过滤后 types 只剩目标类型的段时，骨架也要锁定该类型
        # （否则 tf -tt elastic init 会拿到排在前面的 band 骨架，追加错段）
        want = {tt2.get("key") for tt2 in types} - {None}
        # v1.3.2：types 为空（该技能还没有任何项目段）时 want 也空，
        # 锁定会失效退回拿 band 骨架——用 -tt 的 key 兜底
        if tt:
            want = {tt}
        for k, tc in (cfg.get("task_types") or {}).items():
            if want and k not in want:
                continue
            if tc and not tc.get("local_root"):
                t, tkey = dict(tc), k
                break
    if t is None and cands:
        t = cands[0]
        tkey = tkey or t.get("key")
    # v1.7：开 skill_subdir 时 project_setting 进技能子目录
    # （材料/<技能>/project_setting），每技能一套、完全自包含；
    # 配置名带技能后缀保证全局唯一（scan_project_configs 要求名字唯一）。
    _sub = (str((t or {}).get("dir_name") or tkey)
            if (t and t.get("skill_subdir")) else None)
    ps = (os.path.join(target, _sub, "project_setting") if _sub
          else os.path.join(target, "project_setting"))
    os.makedirs(ps, exist_ok=True)
    # 项目配置 tf_<项目名>.yaml（命名全局唯一，禁止重复）
    pname = name or re.sub(r"\W+", "_", os.path.basename(os.path.abspath(target)))
    if _sub and tkey and not pname.endswith("_" + str(tkey)):
        pname = "%s_%s" % (pname, tkey)   # v1.7：技能级配置名带技能后缀
    f0 = os.path.join(ps, "tf_%s.yaml" % pname)
    if os.path.exists(f0):
        # v1.2：配置已存在且带了 -tt——缺该类型段就追加（已有项目挂新技能，
        # 如 band 项目追加 elastic），不再只"跳过"。空段 = 字段全继承全局骨架。
        existing = _load_yaml_file(f0)
        if tkey and tkey not in (existing.get("task_types") or {}):
            with open(f0, encoding="utf-8") as f:
                lines = f.readlines()
            tt_idx = next((i for i, l in enumerate(lines)
                           if l.startswith("task_types:")), None)
            if tt_idx is not None:
                end = len(lines)
                for i in range(tt_idx + 1, len(lines)):
                    l = lines[i]
                    if l.strip() and not l.startswith((" ", "\t", "#")):
                        end = i
                        break
                lines.insert(end, "  %s:\n" % tkey)
                with open(f0, "w", encoding="utf-8") as f:
                    f.writelines(lines)
                print("已追加 %s 段 → %s（字段全继承全局骨架，可按项目改）"
                      % (tkey, f0))
            else:
                print("已存在，跳过 %s（未找到 task_types 块，请手工加 %s 段）"
                      % (f0, tkey))
        else:
            print("已存在，跳过 %s" % f0)
    else:
        if known_names is not None:
            # 批量 init 预扫过的名字表 → O(1) 查重，避免每个材料全树重扫
            if pname in known_names:
                print("错误：项目配置名 tf_%s.yaml 已被 %s 占用，"
                      "请换个名字（tf init <名字>）。" % (pname, known_names[pname]))
                return 1
        else:
            roots = cfg.get("project_roots") or [cfg.get("_config_dir")]
            for n2, p2, _ in scan_project_configs(roots):
                if n2 == pname:
                    print("错误：项目配置名 tf_%s.yaml 已被 %s 占用，"
                          "请换个名字（tf init <名字>）。" % (pname, p2))
                    return 1
        src = pkg_setting_path("tf_default.yaml")
        content = None
        if src:
            with open(src, encoding="utf-8") as f:
                content = f.read()
        if not content:
            content = DEFAULT_PROJECT_CONFIG
        if t and t.get("work_dir"):
            content = re.sub(r"(?m)^(\s*)work_dir:.*$",
                             r"\1work_dir: " + t["work_dir"], content, count=1)
        # v1.9.5：模板里写死的 desc 会盖掉 skill.yaml 的（elastic/ke 也显示"能带计算"）。
        # 直接删掉这一行，desc 一律由技能自己声明。
        content = re.sub(r"(?m)^\s*desc:.*\n", "", content, count=1)
        if tkey:  # v1.3：类型名同步成继承骨架的真实 key——模板可能还是旧名
                  # （如全局已 bd→band，模板没跟上），直接生成会断链报错
            mk = re.search(r"(?m)^(task_types:\s*\n\s*)[^\s:#]+:", content)
            if mk:
                content = (content[:mk.start()] + mk.group(1) + tkey + ":"
                           + content[mk.end():])
        if _sub:   # kls7-scope：技能子目录布局 = 一材料一项目
            content = _scope_to_material(content, tkey)
        with open(f0, "w", encoding="utf-8") as f:
            f.write(content)
        if known_names is not None:
            known_names[pname] = f0   # 批量 init：新名字同步进预扫集合，后续材料继续 O(1) 查重
        print("已生成 %s（项目配置：步骤/超算/路径按项目改它）" % f0)
    f1 = os.path.join(ps, "setting.yaml")
    if os.path.exists(f1):
        print("已存在，跳过 %s" % f1)
    else:
        with open(f1, "w", encoding="utf-8") as f:
            f.write(DEFAULT_PROJECT_SETTING)
        print("已生成 %s" % f1)
    f2 = os.path.join(ps, "hpc.yaml")
    if os.path.exists(f2):
        print("已存在，跳过 %s" % f2)
    else:
        hpc_name = (t or {}).get("hpc") or "jzzn"
        src = pkg_setting_path(hpc_name + ".yaml")
        if src:
            shutil.copyfile(src, f2)
        else:
            with open(f2, "w", encoding="utf-8") as f:
                f.write(DEFAULT_HPC_SETTING.replace("name: jzzn",
                                                    "name: " + hpc_name))
        print("已生成 %s" % f2)
    # 按 hpc.yaml 的 template_map 把映射到的提交模板复制进项目（可再按项目改）
    hpc_cfg = _load_yaml_file(f2)
    sd = (t or {}).get("skill_dir")
    if sd and not os.path.isabs(sd):
        # v1.3：相对路径先按配置目录找，找不到再按软件根找
        # （tf.yaml 放 setting/ 时，skill/ 在软件根下，只按配置目录会找偏）
        pkg_root = os.path.normpath(os.path.join(
            os.path.dirname(os.path.realpath(__file__)), "..", ".."))
        for base in (cfg.get("_config_dir"), pkg_root):
            cand = os.path.normpath(os.path.join(base or ".", sd))
            if os.path.isdir(cand):
                sd = cand
                break
        else:
            sd = os.path.normpath(os.path.join(cfg.get("_config_dir") or ".", sd))
    # v1.3：只复制该技能实际引用的模板——步骤级 gen_need 都声明了的技能
    # （如 elastic 全程 std），按步骤清单并集复制；没声明的沿用全量（band）。
    # 否则 elastic init 会对用不到的 ncl 模板报"请手动放入"误导用户
    # v1.7：shared 布局改由下方"整套 templates/"复制统一负责，这里的平铺复制
    # 只在 per_step（无 templates/ 子文件夹）时才需要，避免 project_setting 根下
    # 与 templates/ 里重复放一份 submit_*。
    _need = set()
    for _sc in ((t or {}).get("steps") or []):
        _need.update(_sc.get("gen_need") or [])
    _tmpl_need = {x for x in _need if str(x).endswith(".tpl")} or None
    _shared_full = (str((t or {}).get("template_layout") or "shared").lower()
                    != "per_step") and bool(sd)
    for logical, real in ((hpc_cfg.get("template_map") or {}).items()
                          if not _shared_full else []):
        if _tmpl_need is not None and logical not in _tmpl_need:
            continue
        dst = os.path.join(ps, real)
        if os.path.exists(dst):
            print("已存在，跳过 %s" % dst)
            continue
        _layout = str((t or {}).get("template_layout") or "shared").lower()
        if _layout == "per_step":
            # 每步一套模板：不能复制到 project_setting（那里一份会盖住所有步骤）。
            # 模板留在 skill/<技能>/templates/<步骤名>/，要按项目改就放
            # 材料/<技能>/ 下（优先级仍高于技能目录）。
            continue
        srcf = None
        for _d in (_skill_asset_dirs(t or {}, {}, sd) if sd else []):
            _c = os.path.join(_d, real)
            if os.path.isfile(_c):
                srcf = _c
                break
        if srcf:
            shutil.copyfile(srcf, dst)
            print("已复制模板 %s" % dst)
        else:
            print("提示：skill_dir 里找不到 %s，请手动放入 %s" % (real, dst))
    # v1.7：把技能的整套 templates/（含 incar_*.tpl，template_map 未覆盖的也一并）
    # 复制进项目的 templates/ 子文件夹——按项目/材料手改只动这里，不影响其它材料。
    # 目的地：开 skill_subdir 时进 材料/<技能>/templates/，否则 project_setting/templates/。
    # find_asset 会优先读这里（v1.7 查找链已含 <技能>/templates 与 ps/templates）。
    # v1.9：把技能的整套 templates/ 递归复制进项目（保留 <步骤名>/ 子目录结构），
    # 含 *.tpl 与 *.conf。改这里只影响本项目/材料，不动 skill 库。
    if sd:
        _tsrc = os.path.join(sd, str((t or {}).get("template_dir") or "templates"))
        _tdst = os.path.join(ps, "templates")
        _copied = 0
        _jobs = []
        if os.path.isdir(_tsrc):
            for _dp, _dn, _fns in os.walk(_tsrc):
                _rel = os.path.relpath(_dp, _tsrc)
                _jobs.append((_dp, _tdst if _rel == "." else
                              os.path.join(_tdst, _rel), _fns))
        if os.path.isdir(sd):    # 技能根下平铺的老模板也收进 templates/
            _jobs.append((sd, _tdst, os.listdir(sd)))
        for _sdir, _ddir, _fns in _jobs:
            for _fn in sorted(_fns):
                if not _fn.endswith((".tpl", ".conf")):
                    continue
                _s, _d = os.path.join(_sdir, _fn), os.path.join(_ddir, _fn)
                if not os.path.isfile(_s):
                    continue
                if os.path.exists(_d):
                    if not force:
                        continue
                    if _same_file(_s, _d):
                        continue
                    shutil.copyfile(_d, _d + ".bak")   # v1.9：-f 覆盖前先备份
                    print("  备份 %s -> %s.bak" % (_d, os.path.basename(_d)))
                os.makedirs(_ddir, exist_ok=True)
                shutil.copyfile(_s, _d)
                _copied += 1
        if _copied:
            print("已%s %d 个模板/配置 -> %s/（按步骤分子目录；改这里只影响本材料）"
                  % ("覆盖" if force else "复制", _copied, _tdst))
        else:
            print("templates 就绪：%s/（%s）"
                  % (_tdst, "与 skill 出厂版一致" if force else
                     "已存在，未覆盖；要用 skill 出厂版刷新加 -f"))
    # v1.2：技能开 skill_subdir 时本地建技能子目录（本地镜像超算结构：
    # 材料/<技能>/{result,log} ↔ 超算 work/材料/<技能>/stepN）
    if (t or {}).get("skill_subdir"):
        sdname = str(t.get("dir_name") or tkey)
        sddir = os.path.join(target, sdname)
        if not os.path.isdir(sddir):
            os.makedirs(sddir, exist_ok=True)
            print("已创建技能目录 %s/（result/log 都在里面）" % sddir)
        print("提示：%s 要与其它技能用不同超算时，把 project_setting/hpc.yaml "
              "复制为 %s 再改字段即可（ssh_host/template_map/队列）"
              % (tkey, os.path.join(sddir, "hpc.yaml")))
    print("project_setting 就绪：%s（换超算改 hpc.yaml，调目录/结果改 setting.yaml）" % ps)
    return 0

# ===== _yaml_type_block_ensure (原 L5795-L5827) =====
def _yaml_type_block_ensure(path, tkey, kv_line):
    """确保项目配置 task_types.<tkey> 段内有 kv_line（如 "    skill_subdir: true"）。
    段缺失 → 追加新段；段存在且已有该键 → 不动。返回 True=有改动。"""
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    tt = next((i for i, l in enumerate(lines) if l.startswith("task_types:")),
              None)
    if tt is None:
        return False
    end = len(lines)   # task_types 块范围 [tt+1, end)
    for i in range(tt + 1, len(lines)):
        l = lines[i]
        if l.strip() and not l.startswith((" ", "\t", "#")):
            end = i
            break
    seg = next((i for i in range(tt + 1, end)
                if re.match(r"^  %s\s*:" % re.escape(tkey), lines[i])), None)
    if seg is None:
        lines.insert(end, "  %s:\n%s\n" % (tkey, kv_line))
    else:
        seg_end = end   # 段范围 [seg+1, seg_end)：≥4 空格缩进/注释/空行
        for i in range(seg + 1, end):
            if lines[i].strip() and re.match(r"^  [^ \t#]", lines[i]):
                seg_end = i
                break
        key = kv_line.split(":", 1)[0].strip()
        for i in range(seg + 1, seg_end):
            if re.match(r"^\s+%s\s*:" % re.escape(key), lines[i]):
                return False   # 已有该键
        lines.insert(seg + 1, kv_line + "\n")
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)
    return True

# ===== _yaml_type_block_set (原 L5830-L5875) =====
def _yaml_type_block_set(path, tkey, key, value):
    """在项目配置 task_types.<tkey> 段内写 key: value（无则插入，有则改值），
    保留注释与其它键。返回 True=有改动。用于按需启用可选组（写 bandgap_hse: true）。"""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return False
    tt = next((i for i, l in enumerate(lines) if l.startswith("task_types:")),
              None)
    if tt is None:
        return False
    end = len(lines)
    for i in range(tt + 1, len(lines)):
        l = lines[i]
        if l.strip() and not l.startswith((" ", "\t", "#")):
            end = i
            break
    seg = next((i for i in range(tt + 1, end)
                if re.match(r"^  %s\s*:" % re.escape(tkey), lines[i])), None)
    if seg is None:
        seg = end
        lines.insert(end, "  %s:\n" % tkey)
        end = seg + 1
    seg_end = end
    for i in range(seg + 1, end):
        if lines[i].strip() and re.match(r"^  [^ \t#]", lines[i]):
            seg_end = i
            break
    if value is True:
        vtxt = "true"
    elif value is False:
        vtxt = "false"
    else:
        vtxt = str(value)
    val_line = "    %s: %s\n" % (key, vtxt)
    for i in range(seg + 1, seg_end):
        if re.match(r"^\s+%s\s*:" % re.escape(key), lines[i]):
            lines[i] = val_line
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(lines)
            return True
    lines.insert(seg + 1, val_line)
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)
    return True

# ===== _yaml_type_block_remove (原 L5878-L5905) =====
def _yaml_type_block_remove(path, tkey):
    """从项目配置 task_types 下删除 tkey 段（段头到下一个两空格键/块尾）。
    返回 True=有删除。与 _yaml_type_block_ensure 对称。"""
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    tt = next((i for i, l in enumerate(lines) if l.startswith("task_types:")),
              None)
    if tt is None:
        return False
    end = len(lines)
    for i in range(tt + 1, len(lines)):
        l = lines[i]
        if l.strip() and not l.startswith((" ", "\t", "#")):
            end = i
            break
    seg = next((i for i in range(tt + 1, end)
                if re.match(r"^  %s\s*:" % re.escape(tkey), lines[i])), None)
    if seg is None:
        return False
    seg_end = end
    for i in range(seg + 1, end):
        if lines[i].strip() and re.match(r"^  [^ \t#]", lines[i]):
            seg_end = i
            break
    del lines[seg:seg_end]
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)
    return True

