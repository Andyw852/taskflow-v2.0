# -*- coding: utf-8 -*-
# 04_discover —— 本地材料与目录发现 / 解析
#
# 本分片由 tfpkg/__init__.py 装配器在单一命名空间里按顺序执行；
# 函数之间的引用按名字解析（与原单文件一致），分片间无需 import。
# 内容清单（按原文件行号）：
#   L1608  _natkey
#   L1612  discover_local
#   L1643  find_ps_dir
#   L1674  _load_yaml_file
#   L1681  load_project_settings
#   L1692  pkg_setting_path
#   L1706  resolve_material_local
#   L1801  log_action

# ===== _natkey (原 L1608-L1609) =====
def _natkey(s):
    return [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", s)]

# ===== discover_local (原 L1612-L1637) =====
def discover_local(local_root):
    """本地项目根下发现有 POSCAR 的目录（root 自身 + ≤2 层嵌套；
    project_setting/result/log 天然无 POSCAR）。
    v1.1：root 自身也算——"一材料一项目"（local_root 直指材料目录，
    如 Ela1 挂 project_roots 根下）此前发现不到材料；与远端 discover
    的 cands=[root] 行为对齐。"""
    import glob as _glob
    root = os.path.realpath(os.path.expanduser(local_root))
    mats, picked = [], []
    # v1.3.1：已被识别为材料的目录，其内部不再嵌套发现材料——否则在材料
    # 目录里建个带 POSCAR 的 test/ 备份目录都会被当成新材料自动开算
    def _under_picked(d):
        rd = os.path.realpath(d)
        return any(rd == p or rd.startswith(p + os.sep) for p in picked)
    if os.path.isfile(os.path.join(root, "POSCAR")):
        mats.append({"name": os.path.basename(root), "lpath": root})
        picked.append(root)
    for pat in ("*", "*/*"):
        for d in sorted(_glob.glob(os.path.join(root, pat))):
            if (os.path.isdir(d)
                    and os.path.isfile(os.path.join(d, "POSCAR"))
                    and not _under_picked(d)):
                mats.append({"name": os.path.relpath(d, root), "lpath": d})
                picked.append(os.path.realpath(d))
    mats.sort(key=lambda m: _natkey(m["name"]))
    return root, mats

# ===== find_ps_dir (原 L1643-L1671) =====
def find_ps_dir(matdir, rootstop, subdir=None):
    """找最近的 project_setting/。优先级（v1.7 自包含布局）：
      1) <matdir>/<subdir>/project_setting —— 指定技能子目录时最优先
         （band/project_setting、elastic/project_setting 各自独立）；
      2) <matdir>/project_setting 起向上到 rootstop —— 老的就近向上
         （材料级 / 体系级共享配置，向后兼容）；
      3) 未指定 subdir（如孤儿检查，无技能上下文）时，兜底扫
         <matdir>/*/project_setting，任一技能子目录有即算已初始化。
    找不到返回 None。"""
    import glob as _glob
    d0 = os.path.realpath(matdir)
    rootstop = os.path.realpath(rootstop)
    if subdir:   # v1.7：技能级 project_setting 最优先
        cand = os.path.join(d0, str(subdir), "project_setting")
        if os.path.isdir(cand):
            return cand
    d = d0
    while True:
        cand = os.path.join(d, "project_setting")
        if os.path.isdir(cand):
            return cand
        if d == rootstop or not d.startswith(rootstop):
            break
        d = os.path.dirname(d)
    if not subdir:   # v1.7：无技能上下文时，任一技能子目录的 project_setting 都算数
        for h in sorted(_glob.glob(os.path.join(d0, "*", "project_setting"))):
            if os.path.isdir(h):
                return h
    return None

# ===== _load_yaml_file (原 L1674-L1678) =====
def _load_yaml_file(path):
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return _mini_yaml(f.read()) or {}

# ===== load_project_settings (原 L1681-L1689) =====
def load_project_settings(ps_dir):
    """读取 project_setting 下的 setting.yaml 和 hpc.yaml（带缓存）。"""
    if ps_dir in _PS_CACHE:
        return _PS_CACHE[ps_dir]
    st = _load_yaml_file(os.path.join(ps_dir, "setting.yaml")) if ps_dir else {}
    hpc = _load_yaml_file(os.path.join(ps_dir, "hpc.yaml")) if ps_dir else {}
    ps = {"dir": ps_dir, "setting": st, "hpc": hpc}
    _PS_CACHE[ps_dir] = ps
    return ps

# ===== pkg_setting_path (原 L1692-L1700) =====
def pkg_setting_path(name):
    """taskflow 包内 setting/<name>.yaml 的位置（兼容 versions/vX 与平铺布局）。"""
    here = os.path.dirname(os.path.realpath(__file__))
    for cand in (os.path.join(here, "..", "..", "setting", name),
                 os.path.join(here, "..", "setting", name),
                 os.path.expanduser("~/.config/taskflow/setting/" + name)):
        if os.path.isfile(cand):
            return cand
    return None

# ===== resolve_material_local (原 L1706-L1798) =====
def resolve_material_local(t, root, m):
    """给本地发现的材料补齐：project_setting、hpc、路径、远端目录、有效 host。"""
    # v1.1：skill_subdir 子目录名（band/elastic…）。v1.7：先算出来，find_ps_dir
    # 要用它定位技能级 project_setting（材料/<技能>/project_setting）。
    _subdir = (str(t.get("dir_name") or t["key"]) if t.get("skill_subdir") else None)
    ps = load_project_settings(find_ps_dir(m["lpath"], root, _subdir))
    st, hpc = ps["setting"], ps["hpc"]
    # 项目没有 hpc.yaml（或字段缺失）时回退到类型 hpc 配置：
    # v1.0 起 hpc 可写内联字典（把 setting/jzzn.yaml 的内容直接写进 tf.yaml），
    # 字符串仍是包内 setting/<hpc>.yaml 文件名
    thpc = t.get("hpc")
    if isinstance(thpc, dict):
        dhpc = dict(thpc)
    else:
        dhpc = {}
        dflt = pkg_setting_path(str(thpc or "jzzn") + ".yaml")
        if dflt:
            dhpc = _load_yaml_file(dflt)
    fmt = {"matdir": m["lpath"], "mat": m["name"], "root": root}

    def expand(v, default):
        v = v or default
        return v.format(**fmt) if isinstance(v, str) else v

    m["ps"] = ps
    # v1.1：skill_subdir——材料目录下按技能建子目录（如 elastic/、band/）。
    # 远端步骤目录 = work_dir/材料/<subdir>/stepN；本地 result = 材料/<subdir>/result。
    # 子目录名 dir_name 缺省 = 类型 key。老项目平铺结构不开此开关即可。
    m["_subdir"] = _subdir
    m["_skill_dir_local"] = (os.path.join(m["lpath"], m["_subdir"])
                             if m["_subdir"] else None)
    # v1.6：技能子目录里可放私有 hpc.yaml——同一材料的不同技能跑不同超算。
    # 字段级覆盖，优先级：材料/<技能>/hpc.yaml ＞ project_setting/hpc.yaml
    # ＞ 段级 hpc（内联 dict 或包内 setting/<名>.yaml）。
    if m["_skill_dir_local"]:
        shpc = _load_yaml_file(os.path.join(m["_skill_dir_local"], "hpc.yaml"))
        if shpc:
            merged = dict(hpc)
            merged.update(shpc)
            if hpc.get("template_map") or shpc.get("template_map"):
                tm = dict(hpc.get("template_map") or {})
                tm.update(shpc.get("template_map") or {})   # 映射级合并
                merged["template_map"] = tm
            hpc = merged
    # fix: hpc_name 统一转 str——项目 yaml 里 name: 3090 未加引号会被 YAML
    # 解析成 int，进而 render_table 的 len() 对 int 报 "has no len()"。
    m["hpc_name"] = str(hpc.get("name") or dhpc.get("name")
                        or (t.get("hpc") if isinstance(t.get("hpc"), str) else None)
                        or "jzzn")
    m["host_eff"] = hpc.get("ssh_host") or dhpc.get("ssh_host") or None
    # v1.11：work_dir 回退链——项目 setting.yaml > 项目 hpc.yaml >
    # 集群 setting/<hpc_name>.yaml 的 work_dir > 技能默认 > root。
    # 三个集群（jzzn/3090/a800）都在 setting/<name>.yaml 里自描述 work_dir。
    _cluster_work_dir = None
    _cp = pkg_setting_path(str(m["hpc_name"]) + ".yaml")
    if _cp:
        _cluster_work_dir = (_load_yaml_file(_cp) or {}).get("work_dir")
    m["work_dir_eff"] = (st.get("work_dir") or hpc.get("work_dir")
                         or _cluster_work_dir or t.get("work_dir")
                         or t.get("root"))
    # v1.11：用户没显式指定 work_dir 时提示（按技能去重，避免刷屏）
    if not (st.get("work_dir") or hpc.get("work_dir")):
        _wk = t.get("key")
        if _wk not in _WARN_WORKDIR:
            _WARN_WORKDIR.add(_wk)
            print("提示：技能 %s 未在项目里显式指定 work_dir，回退到 %s"
                  "（如需改，在 project_setting/setting.yaml 写 work_dir）"
                  % (_wk, m["work_dir_eff"] or "(无)"), file=sys.stderr)
    # v1.3：skill_subdir 开启时 result/log 默认进技能子目录；setting.yaml 里
    # 仍是 init 模板默认值（{matdir}/result）的视为"未定制"一并升级，
    # 定制过路径的尊重原值（elastic init 后不用再手改 setting）
    rd, ld = st.get("result_dir"), st.get("log_dir")
    if m["_subdir"]:
        if not rd or rd == "{matdir}/result":
            rd = "{matdir}/%s/result" % m["_subdir"]
        if not ld or ld == "{matdir}/log":
            ld = "{matdir}/%s/log" % m["_subdir"]
    m["result_dir"] = expand(rd, "{matdir}/%s/result" % m["_subdir"]
                             if m["_subdir"] else "{matdir}/result")
    m["log_dir"] = expand(ld, "{matdir}/%s/log" % m["_subdir"]
                          if m["_subdir"] else "{matdir}/log")
    # v1.11：fetch_files 三级回退——项目 setting.yaml > 技能 skill.yaml > VASP 默认。
    m["fetch_files"] = (st.get("fetch_files") or t.get("fetch_files") or [
        "INCAR", "POSCAR", "POTCAR", "KPOINTS", "KPOINTS_OPT", "kpath.json",
        "submit.sh", "OUTCAR", "CONTCAR", "EIGENVAL", "vasprun.xml", "queue.out"])
    tmap = dict(dhpc.get("template_map") or {})   # 映射级合并：包内默认补缺，
    tmap.update(hpc.get("template_map") or {})    # 项目 hpc.yaml 覆盖同名项
    m["template_map"] = tmap
    m["rpath"] = (os.path.join(m["work_dir_eff"], m["name"], m["_subdir"] or "")
                  if m["work_dir_eff"] else None)
    if m["rpath"]:
        m["rpath"] = os.path.normpath(m["rpath"])
    return m

# ===== log_action (原 L1801-L1813) =====
def log_action(m, text):
    """往该材料的 log_dir/tf.log 追加一行操作日志（本地模式才有）。"""
    ld = m.get("log_dir")
    if not ld:
        return
    try:
        os.makedirs(ld, exist_ok=True)
        import datetime
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(os.path.join(ld, "tf.log"), "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (ts, text))
    except OSError:
        pass

