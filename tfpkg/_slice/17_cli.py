# -*- coding: utf-8 -*-
# 17_cli —— 状态缓存 / 过滤 / collect_data / main() 入口与命令分发
#
# 本分片由 tfpkg/__init__.py 装配器在单一命名空间里按顺序执行；
# 函数之间的引用按名字解析（与原单文件一致），分片间无需 import。
# 内容清单（按原文件行号）：
#   L6656  _dbg_t
#   L6671  _state_cache_path
#   L6676  _state_cache_sig
#   L6694  _state_cache_save
#   L6718  _state_cache_load
#   L6733  collect_data
#   L6768  apply_exclude
#   L6779  filter_projs
#   L6806  filter_status
#   L6826  status_spec_has_scancel
#   L6833  _snapshot
#   L7078  main

# ===== _dbg_t (原 L6656-L6660) =====
def _dbg_t(label, t0):
    """TF_DEBUG_TIME=1 时向 stderr 打印各阶段耗时。"""
    if os.environ.get("TF_DEBUG_TIME"):
        import time as _t
        print("[计时] %s: %.1fs" % (label, _t.time() - t0), file=sys.stderr)

# ===== _state_cache_path (原 L6671-L6673) =====
def _state_cache_path(cfg):
    return os.path.join(cfg.get("_config_dir") or os.getcwd(),
                        ".tf_state_cache.json")

# ===== _state_cache_sig (原 L6676-L6691) =====
def _state_cache_sig(cfg, types, tt, root):
    """缓存键：主配置 mtime+size + 采集范围指纹。"""
    cst = None
    cp = cfg.get("_config_path")
    if cp and os.path.isfile(cp):
        try:
            _s = os.stat(cp)
            cst = (os.path.realpath(cp), _s.st_mtime_ns, _s.st_size)
        except OSError:
            pass
    import hashlib
    h = hashlib.sha1()
    for t in types:
        h.update(("%s|%s|%s\n" % (t.get("key", ""), t.get("root", ""),
                                  t.get("local_root", ""))).encode("utf-8"))
    return (cst, tt, root, h.hexdigest())

# ===== _state_cache_save (原 L6694-L6715) =====
def _state_cache_save(cfg, data, types, tt, root):
    try:
        import tempfile as _tf
        p = _state_cache_path(cfg)
        d = os.path.dirname(p) or "."
        os.makedirs(d, exist_ok=True)
        payload = {"ts": time.time(),
                   "sig": _state_cache_sig(cfg, types, tt, root),
                   "data": data}
        _fd, _tmp = _tf.mkstemp(dir=d, prefix=".tf_state.", suffix=".tmp")
        try:
            with os.fdopen(_fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(_tmp, p)
        finally:
            if os.path.exists(_tmp):
                try:
                    os.remove(_tmp)
                except OSError:
                    pass
    except Exception:
        pass

# ===== _state_cache_load (原 L6718-L6730) =====
def _state_cache_load(cfg, types, tt, root, ttl):
    if ttl <= 0:
        return None
    try:
        with open(_state_cache_path(cfg), encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        return None
    if payload.get("sig") != _state_cache_sig(cfg, types, tt, root):
        return None
    if time.time() - float(payload.get("ts") or 0) > ttl:
        return None
    return payload.get("data")

# ===== collect_data (原 L6733-L6765) =====
def collect_data(cfg, types):
    """按类型采集全部材料状态（远端/本地两段路径），供各命令及 watch 循环复用。"""
    data_types = []
    queue_by_host = {}
    v2 = [t for t in types if not t.get("local_root")]
    if v2:
        _d = collect(cfg, v2)
        data_types.extend(_d["types"])
        queue_by_host[cfg.get("host") or "local"] = _d.get("queue") or {}
    segs = [t for t in types if t.get("local_root")]
    if segs:   # v3.20：跨段批量采集（一次 ssh 完成全部材料）
        _te_list, _qbh = collect_v3_batch(cfg, segs)
        queue_by_host.update(_qbh)
        for te in _te_list:
            exist = next((x for x in data_types
                          if x["key"] == te["key"] and x.get("local")), None)
            if exist:
                exist["materials"] = _dedup_segments(exist["materials"]
                                                     + te["materials"])
            else:
                data_types.append(te)
    data = annotate({"host": cfg.get("host") or "local", "types": data_types})
    if queue_by_host:
        data["queue"] = _queue_total(queue_by_host)
    local_by_key = {t["key"]: t for t in types}
    for t in data["types"]:
        lc = local_by_key.get(t["key"], {})
        t["steps_cfg"] = lc.get("steps") or []
        t["gen_dir"] = lc.get("gen_dir")
        t["gen_need"] = lc.get("gen_need")
        t["skill_dir"] = lc.get("skill_dir")
    check_duplicates(data)
    return data

# ===== apply_exclude (原 L6768-L6776) =====
def apply_exclude(data, exclude):
    """-x：跳过指定项目（全名或 basename，逗号分隔）。"""
    if not exclude:
        return
    ex = {x.strip() for x in exclude.split(",") if x.strip()}
    for t in data["types"]:
        t["materials"] = [m for m in t["materials"]
                          if m["name"] not in ex
                          and os.path.basename(m["name"]) not in ex]

# ===== filter_projs (原 L6779-L6787) =====
def filter_projs(data, projs):
    """只保留指定材料（全名或 basename）；空列表 = 不过滤。"""
    if not projs:
        return
    want = set(projs)
    for t in data["types"]:
        t["materials"] = [m for m in t["materials"]
                          if m["name"] in want
                          or os.path.basename(m["name"]) in want]

# ===== filter_status (原 L6806-L6823) =====
def filter_status(data, spec):
    """v1.4 -status：只保留"有步骤处于指定状态"的材料（对任意命令生效：
    status 只看它们，start/retry/rerun/stop 只操作它们）。
    状态词大小写不限、支持别名，逗号分隔多个。"""
    kinds = set()
    for x in str(spec or "").split(","):
        x = x.strip()
        # patch_cell_word：ready 展开成 TODO + PREP 两个 kind
        if x and STATUS_ALIAS.get(x.lower()) == "TODO+PREP":
            kinds.update(("TODO", "PREP"))
            continue
        if x:
            kinds.add(STATUS_ALIAS.get(x.lower(), x.upper()))
    if not kinds:
        return
    for t in data["types"]:
        t["materials"] = [m for m in t["materials"]
                          if any(s["kind"] in kinds for s in m["steps"])]

# ===== status_spec_has_scancel (原 L6826-L6830) =====
def status_spec_has_scancel(spec):
    """-status 里显式含 scancel → start/retry 放行 SCANCEL 步骤。"""
    return any(x.strip().lower() in ("scancel", "scancelled", "cancel",
                                     "cancelled", "canceled")
               for x in str(spec or "").split(","))

# ===== _snapshot (原 L6833-L6839) =====
def _snapshot(data):
    """状态指纹：材料 → 各步骤 (label, kind, 作业状态)，watch 据此判断有无变化。"""
    return json.dumps({m["name"]: [(s["label"], s["kind"],
                                    (s.get("job") or {}).get("state"))
                                   for s in m["steps"]]
                       for t in data["types"] for m in t["materials"]},
                      sort_keys=True)

# ===== main (原 L7078-L7453) =====
def _dry_run_steps_for(cmd, m, jb):
    """--dry-run：某材料在命令 cmd、步骤筛选 jb 下实际会动的步骤。
    返回步骤列表；None 表示"整材料级"（rerun/clean 无 -j 时）。"""
    if jb:
        s = find_step_soft(m, jb)
        return [s] if s is not None else []
    if cmd == "retry":
        return [s for s in m["steps"] if s["kind"] == "FAIL"]
    if cmd == "start":
        ready = m.get("actives")
        if ready is None:
            ready = [m.get("active")] if m.get("active") is not None else []
        return [s for s in ready if s and s["kind"] in ("TODO", "PREP")]
    if cmd == "stop":
        return [s for s in m["steps"] if s.get("job")]
    if cmd == "fetch":
        return [s for s in m["steps"] if s["kind"] == "OK"]
    return None   # rerun / clean：无 -j 时整材料级


def _dry_run_report(cfg, data, cmd, projs, jobs):
    """--dry-run：打印真实目标对象（复用 _dry_run_steps_for 的语义）。"""
    mats = []
    if projs:
        for pj in projs:
            try:
                mats.append(find_material(data, pj)[1])
            except SystemExit:
                print("  材料 %s  （未能解析）" % pj)
    else:
        mats = [m for t in data["types"] for m in t["materials"]]
    shown = 0
    for m in mats:
        for jb in jobs:
            steps = _dry_run_steps_for(cmd, m, jb)
            if steps is None:
                print("  材料 %s  （整材料 %s）" % (m["name"], cmd))
                shown += 1
                continue
            if not steps:
                print("  材料 %s  %s（无需操作）"
                      % (m["name"], ("步骤 %s " % jb) if jb else ""))
                continue
            for s in steps:
                print("  材料 %s  步骤 %s [%s]  ->  %s"
                      % (m["name"], s.get("label", s.get("name", "?")),
                         s.get("kind", "?"), s.get("dir", "?")))
                shown += 1
    if shown == 0:
        print("  （共 %d 个材料，无任何目标）" % len(mats))


def main():
    if any(a in ("-h", "--help") for a in sys.argv[1:]):
        print(USAGE)
        return
    if any(a in ("-V", "--version") for a in sys.argv[1:]):
        print("taskflow (tf) version %s" % TF_VERSION)
        exe = os.path.realpath(__file__)
        pkg = os.path.dirname(os.path.dirname(os.path.dirname(exe)))
        if not os.path.basename(pkg).startswith("taskflow"):  # 非 versions 布局
            pkg = os.path.dirname(exe)
        _prog = os.path.realpath(globals().get("_PROG_PATH") or exe)
        print("程序: %s" % _prog)
        print("包根: %s（setting/ = 默认模板，skill/ = 技能脚本）" % pkg)
        return
    if len(sys.argv) == 1:   # patch_auto：纯 tf = 只报版本，不采集/不提交
        print("taskflow (tf) version %s" % TF_VERSION)
        print("程序: %s" % os.path.realpath(globals().get("_PROG_PATH") or __file__))
        print("")
        print("  tf list      只读总表（不拉取、不提交）")
        print("  tf summary   只读极简汇总（巡检省 token，见 AGENTS.md）")
        print("  tf status    刷新状态 + auto-fetch + auto-advance")
        print("  tf watch     后台监控（-i 秒，-d 放后台）")
        print("  tf -h        全部命令")
        return
    p = argparse.ArgumentParser(prog="tf")
    p.add_argument("-tt", dest="tt")
    p.add_argument("-p", dest="proj")
    p.add_argument("-j", "-job", dest="job")
    p.add_argument("-c", "--config")
    p.add_argument("--host")
    p.add_argument("-u", "--user")
    p.add_argument("-x", "--exclude", dest="exclude",
                   help="跳过指定项目（逗号分隔，全名或 basename）")
    p.add_argument("-status", "--status", dest="status_f", metavar="ST",
                   help="只保留含指定状态步骤的材料：done/running/pd/error/"
                        "waiting/scancel（逗号分隔），对任意命令生效；"
                        "如 -status scancel start 重跑全部被 stop 取消的")
    p.add_argument("-i", "--interval", type=int, default=300,
                   help="watch 刷新间隔秒数（默认 300）")
    p.add_argument("-d", "--daemon", action="store_true",
                   help="watch 放后台运行（日志 .tf_watch.log）")
    p.add_argument("--stop", action="store_true",
                   help="停止后台运行的 tf watch")
    p.add_argument("--install", action="store_true",
                   help="写入 crontab 保活（tf watch 重启后自动恢复）")
    p.add_argument("--uninstall", action="store_true",
                   help="移除 crontab 保活")
    p.add_argument("-f", "--force", action="store_true")
    p.add_argument("--all", dest="all_files", action="store_true",
                   help="fetch 时拉回每个步骤的全部文件（不只 fetch_files 清单）")
    p.add_argument("--hide-done", dest="hide_done", action="store_true",
                   help="状态表隐藏全部步骤都完成的项目")
    p.add_argument("--show-done", dest="show_done", action="store_true",
                   help="取消 hide_done 配置的隐藏效果")
    p.add_argument("--diff", dest="diff", action="store_true",
                   help="summary：与上次快照对比，无变化不输出（省 token，巡检用）")
    p.add_argument("--refresh", dest="refresh", action="store_true",
                   help="list/summary 强制跳过本地状态缓存，重新 ssh 采集")
    p.add_argument("-y", "--yes", action="store_true")
    p.add_argument("--dry-run", dest="dry", action="store_true",
                   help="只打印将影响的对象/计划，不执行（start/stop/retry/rerun/clean/fetch/adopt/migrate-subdir）")
    p.add_argument("--json", dest="json_out", action="store_true",
                   help="list/summary/status/dir 以 JSON 输出（机器可读）")
    p.add_argument("--schema", dest="schema", action="store_true",
                   help="json：打印字段 schema 说明")
    p.add_argument("-clean", "--clean", dest="clean", action="store_true")
    p.add_argument("--purge-config", dest="purge_config", action="store_true",
                   help="clean：连 project_setting 一起删（默认保留，重算需 tf init）")
    p.add_argument("--from-skill", dest="from_skill", action="store_true",
                   help="rerun：忽略项目侧模板/step.conf，只用 skill 库出厂版生成")
    p.add_argument("--set", dest="sets", action="append", metavar="节.键=值",
                   help="conf：改本步 step.conf（如 --set incar.EDIFF=1E-6；"
                        "值留空=删除该键）")
    p.add_argument("args", nargs="*")
    a, unknown = p.parse_known_args()  # v3.14：位置参数可穿插在选项中间
    a.args = list(a.args) + unknown    # （-p A B retry / -p A B -j 1 dir）
    if a.schema:   # v2.0：--schema 打印 json 字段说明（无需配置/采集）
        print(JSON_SCHEMA)
        return

    commands = {"status", "list", "summary", "start", "stop", "retry", "rerun",
                "json", "config", "dir", "fetch", "init", "clean", "watch",
                "help", "auto", "adopt", "migrate-subdir", "hpc", "skills",
                "conf", "level"}   # patch_level
    root, cmd, pos = None, "status", []
    for tok in a.args:  # v3.14：位置参数先收集，之后按"材料名/目录"消歧
        if tok == "help":
            print(USAGE)
            return
        if tok in commands and cmd == "status":
            cmd = tok
        else:
            pos.append(tok)
    # 本地存在的目录 → 旧版 ROOT 语义；其余位置参数留给材料名消歧
    mat_toks = []
    for tok in pos:
        if os.path.isdir(os.path.expanduser(tok)):
            if root is not None:
                sys.exit("错误：只能指定一个 ROOT（收到多个目录）。")
            root = tok
        else:
            mat_toks.append(tok)

    if cmd == "config":
        print(EXAMPLE_CONFIG)
        return
    if a.job and not a.proj and not mat_toks and cmd not in (
            "start", "stop", "retry", "rerun", "clean", "status"):
        sys.exit("错误：-j 必须和 -p 一起用（start/stop/retry/rerun/clean "
                 "支持不带 -p，表示对全部材料只操作该步骤）。")

    cfg, cfg_path = load_config(a.config)
    cfg["_config_dir"] = (os.path.dirname(os.path.abspath(cfg_path))
                          if cfg_path else os.getcwd())
    cfg["_config_path"] = cfg_path
    cfg = apply_skills(cfg, verbose=True)   # v1.2：先装配 skill/*/skill.yaml
    if cmd == "skills":
        return cmd_skills(cfg, tt=a.tt)
    cfg = merge_project_configs(cfg)   # v3.1：合并项目配置 project_setting/tf_*.yaml
    if a.host is not None:
        cfg["host"] = a.host or None
    if a.user:
        cfg["user"] = a.user
    types = get_types(cfg, tt=a.tt,
                      root_override=None if cmd == "init" else root,
                      quiet=(cmd == "init"))
    if cmd != "watch":
        _watch_ensure(cfg)   # v1.10：auto_watch 时顺带确保后台监控在跑
    if cmd in ("status", "json") and not types and not a.tt:
        sys.exit("错误：没有任何任务类型"
                 "（在全局 tf.yaml 或项目 project_setting/tf_*.yaml 里定义）。")
    # v1.1：-tt 指定的类型有骨架但无项目段时 types 为空——前面已打印引导
    # 提示，这里放行，按空表/无目标处理（不算错误）。

    if cmd == "init" and not a.job:  # 项目配置初始化：纯本地，不连超算
        if a.proj and mat_toks:
            print("提示：多个材料要用逗号分隔，如  -p %s,%s"
                  % (a.proj, ",".join(mat_toks)))
        sys.exit(cmd_init(cfg, types, a.proj, tt=a.tt,
                          name=(mat_toks[0] if mat_toks else root),
                          force=a.force, yes=a.yes))

    if cmd == "level":  # patch_level：设/查计算级别（纯本地改 step.conf）
        _arg = mat_toks[0] if mat_toks else None
        sys.exit(cmd_level(cfg, types, a.tt, a.proj, _arg))

    _force_advance = False   # autonow：仅 auto on 这一次允许推进
    if cmd == "auto":   # v1.5：一键开关 auto_advance（纯本地改 tf.yaml）
        # autonow2：从位置参数里挑 on/off 当开关，其余位置参数当材料名，
        # 并标记为已消费。原来固定取 mat_toks[0] 且不消费，导致
        #   `tf auto on`           -> "on" 落到后面被当材料名解析而报错
        #   `tf -tt ke <材料> auto on` -> 材料名被当成了 on/off 参数
        _AUTO_WORDS = ("on", "off", "1", "0", "true", "false",
                       "\u5f00", "\u5173")
        _arg = next((x for x in mat_toks
                     if str(x).strip().lower() in _AUTO_WORDS), None)
        _rest = [x for x in mat_toks
                 if str(x).strip().lower() not in _AUTO_WORDS]
        _proj = a.proj or (",".join(_rest) if _rest else None)
        mat_toks, a.proj = [], _proj
        if _proj:       # v1.9.9：带 -p（或位置参数）就只改这些材料/技能
            _rc = cmd_auto_project(cfg, types, _proj, a.tt, _arg)
        elif a.tt:      # patch_auto：-tt 不带材料 = 该技能下全部材料
            _rc = cmd_auto_skill(cfg, types, a.tt, _arg)
        else:
            _rc = cmd_auto(cfg, _arg)
        # patch_auto_now：原实现三条路都 sys.exit，走不到下面的 status 分支，
        # 而 auto_advance() 只在 status / watch 里调用 —— 于是 auto on 只翻
        # 开关不干活，还得再手敲一次 tf。这里改成：on 且全部成功 -> 不退出，
        # 落到 status 分支跑一轮采集+推进。off / 查询 / 有失败 -> 保持原行为。
        # autonow：on 且全部成功 → 不退出，置标志落到下面的 status 分支，
        # 当场跑一轮采集 + 推进。off / 无参查询 / 有失败 → 原样，只翻开关。
        if _rc != 0 or str(_arg or "").strip().lower() not in (
                "on", "1", "true", "开"):
            sys.exit(_rc)
        print("auto_advance 已开，下面立刻提交可开始的步骤"
              "（只想看不提交：tf list）。")
        _force_advance = True
        cmd = "status"

    if cmd == "adopt":  # v1.5：接管手工整理的技能子目录（内部自做采集）
        sys.exit(cmd_adopt(cfg, types,
                           a.proj or (mat_toks[0] if mat_toks else None),
                           a.yes, a.dry, tt=a.tt))

    if cmd == "hpc":    # v1.7：指定项目分配到指定超算（纯本地改 hpc.yaml）
        sys.exit(cmd_hpc(cfg, types,
                         [x.strip() for x in (a.proj or "").split(",")
                          if x.strip()],
                         mat_toks[0] if mat_toks else None, a.tt, a.yes))

    # v3：含 local_root 的类型走本地发现 + 按项目超算分组采集；其余远端扫描
    # v3.1：同 key 的多个段（项目配置）合并进同一个类型条目
    import time as _time
    _t0 = _time.time()
    # -p 指定了材料时，收集前先把无关的段过滤掉——大体系（数千材料）下全量
    # 采集会逐材料读 yaml + ssh，极慢。skill_subdir 段的材料名 =
    # basename(local_root)，可零成本精确匹配；其余段原样保留（回退全量，行为不变）。
    if a.proj:
        _want = {x.strip() for x in a.proj.split(",") if x.strip()}
        # 共享(体系级)布局：local_root 是项目目录（无 POSCAR），材料名 = 相对路径
        # 前缀，无法用 basename 精确匹配 → 保留该段回退全量收集。
        types = [t for t in types
                 if (not (t.get("skill_subdir") and t.get("local_root"))
                     or os.path.basename(os.path.realpath(t["local_root"])) in _want
                     or not os.path.isfile(os.path.join(
                         os.path.realpath(t["local_root"]), "POSCAR")))]
    # patch_state_cache：list/summary 只读命令优先读本地缓存（跳过 ssh 采集），
    # --refresh 或 TF_CACHE_TTL=0 强制刷新；会改状态的命令一律现采。
    _ttl = int(os.environ.get("TF_CACHE_TTL", "60") or 0)
    _cached = None
    if cmd in ("list", "summary") and not a.refresh and _ttl > 0:
        _cached = _state_cache_load(cfg, types, a.tt, root, _ttl)
    if _cached is not None:
        data = _cached
        if os.environ.get("TF_DEBUG_TIME"):
            print("[缓存] 命中本地状态缓存，跳过 ssh 采集（--refresh 强制刷新）",
                  file=sys.stderr)
    else:
        data = collect_data(cfg, types)
        fill_local_dim(cfg, data, types)
        if cmd in ("list", "summary"):
            _state_cache_save(cfg, data, types, a.tt, root)
    _dbg_t("状态采集（ssh+远端扫描）", _t0)

    apply_exclude(data, a.exclude)   # v3.11：-x 跳过指定项目

    incl_sc = status_spec_has_scancel(a.status_f)   # v1.4
    if a.status_f:   # v1.4：-status 只保留含指定状态步骤的材料
        n0 = sum(len(t["materials"]) for t in data["types"])
        filter_status(data, a.status_f)
        n1 = sum(len(t["materials"]) for t in data["types"])
        print("（-status %s：%d/%d 个材料匹配）" % (a.status_f, n1, n0))

    # v3.14：多项目——-p 支持逗号分隔，位置参数里的材料名自动并入
    # v1.0：多步骤——-j 支持逗号分隔，位置参数里的步骤名/label 自动并入
    projs = [x.strip() for x in (a.proj or "").split(",") if x.strip()]
    jobs = [x.strip() for x in (a.job or "").split(",") if x.strip()]
    if mat_toks:
        names, bases, stepnames = set(), {}, set()
        for t in data["types"]:
            for m in t["materials"]:
                names.add(m["name"])
                bases.setdefault(os.path.basename(m["name"]), m["name"])
                for s in m["steps"]:
                    stepnames.add(s["name"])
                    stepnames.add(s["label"])
        for tok in mat_toks:
            if tok in names or tok in bases:
                projs.append(tok)
            elif tok in stepnames:
                jobs.append(tok)
            else:
                import difflib
                close = difflib.get_close_matches(
                    tok, sorted(names | set(bases) | stepnames | commands),
                    n=1, cutoff=0.6)
                sys.exit("错误：'%s' 不是命令、材料或步骤%s"
                         % (tok, ("，你是不是想 '%s'？" % close[0]) if close else "。"))
    jobs = jobs or [None]

    # v2.0：--dry-run 排练。破坏性/有副作用命令只打印将影响的对象，不执行。
    # 按命令语义打印【真实】目标：retry=FAIL 步，start=就绪步，stop=有作业步，
    # fetch=已完成可拉回步；rerun/clean 无 -j 时是整材料级（不再笼统"全部步骤"）。
    _eff_cmd = "clean" if (a.clean or cmd == "clean") else cmd
    if a.dry and _eff_cmd in ("start", "stop", "retry", "rerun", "clean",
                              "fetch"):
        print("【dry-run】命令 '%s' 将影响以下对象（未执行任何变更、未提交作业）："
              % _eff_cmd)
        _dry_run_report(cfg, data, _eff_cmd, projs, jobs)
        return

    if cmd == "watch":   # v3.15：监控模式（循环 采集→fetch→advance）
        if a.install:                    # v1.10：crontab 保活（重启自动恢复）
            sys.exit(_watch_cron(True))
        if a.uninstall:
            sys.exit(_watch_cron(False))
        if a.stop:
            sys.exit(_watch_stop(cfg))
        if a.daemon:                     # v3.16：后台监控，不占终端
            _watch_daemon(a, mat_toks, root, cfg)
            return
        _ov = {}                        # v1.8：自动重载时命令行覆盖照旧生效
        if a.host is not None:
            _ov["host"] = a.host or None
        if a.user:
            _ov["user"] = a.user
        cmd_watch(cfg, types, projs, a.exclude, a.interval,
                  tt=a.tt, root=root, overrides=_ov)
        return

    if a.clean or cmd == "clean":  # 删除生成物回到 PREP（留 POSCAR）
        fails = 0
        for pj in (projs or [None]):
            for jb in jobs:
                fails += 0 if cmd_clean(cfg, data, pj, jb, a.yes,
                                        purge_config=a.purge_config) == 0 else 1
        sys.exit(0 if fails == 0 else 1)

    if cmd == "init":  # -p MAT -j STEP init：只生成该步骤输入，不提交
        if len(projs) > 1:
            sys.exit("错误：步骤级 init 一次只支持一个材料。")
        fails = 0
        for jb in jobs:
            fails += 0 if cmd_step_init(cfg, data, projs[0] if projs else None,
                                        jb, a.force) == 0 else 1
        sys.exit(fails)

    if cmd == "list":   # v3.23：只读总览表格；不 auto_fetch/auto_advance（绝不提交）
        if a.hide_done or (cfg.get("hide_done") and not a.show_done):
            apply_hide_done(data)
        if a.json_out:
            print(json.dumps(_add_diag_codes(data), ensure_ascii=False, indent=2))
        else:
            render_table(data)
        return
    if cmd == "summary":   # 只读极简汇总；不 auto_fetch/auto_advance（绝不提交）
        if a.hide_done or (cfg.get("hide_done") and not a.show_done):
            apply_hide_done(data)
        _sp = None
        if a.diff:   # 快照按过滤范围分开存，不同 -tt/-status/-x/-p 互不干扰
            import hashlib as _hashlib
            _p = ",".join(sorted(x.strip() for x in (a.proj or "").split(",") if x.strip()))
            _scope = ",".join([a.tt or "", a.status_f or "", a.exclude or "", _p])
            _h = _hashlib.md5(_scope.encode("utf-8")).hexdigest()[:8]
            _sp = os.path.join(cfg.get("_config_dir") or os.getcwd(),
                               ".tf_summary_%s.txt" % _h)
        if a.json_out:
            print(json.dumps(_summary_json(data), ensure_ascii=False, indent=2))
        else:
            cmd_summary(data, diff=a.diff, state_path=_sp)
        return
    if cmd == "status":
        if a.json_out:
            print(json.dumps(_add_diag_codes(data), ensure_ascii=False, indent=2))
            return
        _t1 = _time.time()
        auto_fetch(cfg, data)   # 算完的步骤自动保存到本地 result/
        _dbg_t("auto-fetch 拉回", _t1)
        # autonow：只有从 auto on 落过来时才推进；裸 tf / tf status / tf list
        # 仍是 fixte⑤ 的只读语义（不提交任务）。
        if _force_advance:
            _t1 = _time.time()
            auto_advance(cfg, data)
            _dbg_t("auto-advance 推进", _t1)
        if a.hide_done or (cfg.get("hide_done") and not a.show_done):
            apply_hide_done(data)   # v1.1：隐藏全部完成的项目
        for pj in (projs or [None]):
            for jb in jobs:
                cmd_status(cfg, data, pj, jb)
        if not projs:
            new = find_uninited(cfg)   # v3.11：新材料目录自动检测（v1.2 扫 project_roots）
            if new:
                print("发现 %d 个新材料目录未初始化：%s"
                      % (len(new), ", ".join(new)))
                print("→ tf init 纳入管理；配 auto_advance: true 后下次 tf 自动开算")
    elif cmd == "conf":
        if not projs or not jobs or not jobs[0]:
            sys.exit("错误：conf 需要 -p 材料 -j 步骤（如 tf -tt bd -p Mg2C60 -j 2 conf）。")
        fails = 0
        for pj in projs:
            for jb in jobs:
                fails += cmd_conf(cfg, data, pj, jb, a.sets)
        sys.exit(1 if fails else 0)
    elif cmd == "json":
        if a.schema:
            print(JSON_SCHEMA)
        else:
            _out = {"schema_version": 2, "tf_version": TF_VERSION}
            _out.update(data)
            print(json.dumps(_out, ensure_ascii=False, indent=2))
    elif cmd == "dir":
        # 只输出路径本身，方便拼进 ssh/cd 命令：ssh jzzn "cd $(tf -p X -j 1 dir)"
        if a.json_out:
            _dirs = []
            if projs:
                for pj in projs:
                    t, m = find_material(data, pj)
                    for jb in jobs:
                        _dirs.append(find_step(m, jb)["dir"] if jb else m["path"])
            elif a.tt:
                _dirs.append(data["types"][0]["root"])
            else:
                sys.exit("错误：dir 需要 -p（可配 -j）或 -tt 指定对象。")
            print(json.dumps(_dirs, ensure_ascii=False))
        elif projs:
            for pj in projs:
                t, m = find_material(data, pj)
                for jb in jobs:
                    print(find_step(m, jb)["dir"] if jb else m["path"])
        elif a.tt:
            print(data["types"][0]["root"])
        else:
            sys.exit("错误：dir 需要 -p（可配 -j）或 -tt 指定对象。")
    elif cmd == "migrate-subdir":
        if not a.tt:
            sys.exit("错误：migrate-subdir 需要 -tt 指定迁哪个技能"
                     "（如 tf -tt band migrate-subdir）。")
        fails = 0
        for pj in (projs or [None]):
            fails += cmd_migrate_subdir(cfg, data, pj, a.yes, a.dry)
        sys.exit(0 if fails == 0 else 1)
    else:
        fails = 0
        for pj in (projs or [None]):
            for jb in jobs:
                if cmd == "start":
                    fails += cmd_start(cfg, data, pj, jb, a.force,
                                       incl_scancel=incl_sc)
                elif cmd == "stop":
                    fails += cmd_stop(cfg, data, pj, jb, a.yes)
                elif cmd == "retry":
                    fails += cmd_retry(cfg, data, pj, jb, a.force,
                                       incl_scancel=incl_sc)
                elif cmd == "rerun":
                    fails += cmd_rerun(cfg, data, pj, jb, a.yes, a.force,
                                       from_skill=a.from_skill)
                elif cmd == "fetch":
                    fails += cmd_fetch(cfg, data, pj, all_files=a.all_files)
        if fails:
            sys.exit(1)

