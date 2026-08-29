# -*- coding: utf-8 -*-
# 13_advance —— auto_advance / auto_fetch / clean / step init
#
# 本分片由 tfpkg/__init__.py 装配器在单一命名空间里按顺序执行；
# 函数之间的引用按名字解析（与原单文件一致），分片间无需 import。
# 内容清单（按原文件行号）：
#   L4776  auto_advance
#   L4888  cmd_step_init
#   L4930  cmd_clean
#   L5091  _fetch_stamp_clear
#   L5101  _relay_prev_across_host
#   L5145  fetch_material
#   L5193  auto_fetch
#   L5256  cmd_fetch

# ===== auto_advance (原 L4776-L4885) =====
def auto_advance(cfg, data):
    """status 时自动推进可开始的步骤（全局 tf.yaml 写 auto_advance: true 开启；
    项目 setting.yaml 里 auto_advance: false 可单独关闭）。
    只推进 TODO/PREP（输入就绪/未生成）的活跃步骤；error 不自动重试；
    v1.4：SCANCEL（tf stop 打了标记的）同样不推进，须显式 start/retry/rerun。"""
    if not cfg.get("auto_advance"):
        return
    # fixte⑬：磁盘复核 —— watch 是长驻进程，内存里的 cfg 可能是启动时的旧值
    # （热重载失败会被 except 吞掉，进程继续按旧配置提交作业）。这里每轮直接
    # 读一次 tf.yaml：磁盘上关着就立刻返回，绝不提交。
    _cp = cfg.get("_config_path")
    if _cp and os.path.isfile(_cp):
        try:
            if (_load_yaml_file(_cp) or {}).get("auto_advance") is False:
                return
        except Exception:
            pass
    _skipped = []
    _gate = _SkillGate(cfg, data)   # patch_max_jobs：按技能卡并发提交上限
    for t in data["types"]:
        _sfull = False   # 本技能已到 max_jobs 上限 → 本轮不再提交它的任何材料
        for m in t["materials"]:
            st = (m.get("ps") or {}).get("setting") or {}
            # 未为本技能初始化的材料（无 project_setting -> ps.dir 为空）不自动推进：
            # 否则 band/ke 共用数据根时，ke 会把只 init 了 band 的材料也自动跑起来（误提交）。
            if not (m.get("ps") or {}).get("dir"):
                _skipped.append("%s[%s]（未 init 本技能）" % (m["name"], m["tt"]))
                continue
            if st.get("auto_advance") is not True:   # 显式 true 才推进
                _skipped.append("%s[%s]" % (m["name"], m["tt"]))
                continue
            if _sfull:
                # 本技能已达 max_jobs：只预生成输入（不提交），等有空位自动补交。
                # max_jobs 只卡 sbatch，不卡本地生成输入。
                _pregenerate_ready(cfg, t, m)
                continue
            # --- patch_auto: 级联推进 ------------------------------------
            # 原实现每轮每材料只推一步。画图/读取这类 run:gen 的本地即时步
            # 几秒就完成，却要白等一整个 watch 周期才轮到下一步。这里改成：
            # 交了 SLURM 就停（等作业），本地即时步成功就接着往下推。
            # --- patch_ke_dag: 并行推进 -----------------------------------
            # 原实现只推 m["active"] 一条线。改成遍历 needs 算出的就绪集：
            # 互不依赖的分支（ke 的 S2链 / S3->S4 / S5 / S6 / S7->S7.1）同轮全交。
            # run:gen 的本地即时步（画图、deform read）就地标完成后重算就绪集，
            # 可能立刻解锁下游。max_inflight 兜住 SLURM 的每用户作业数上限。
            _done_now = []
            _cap = _dag_max_inflight(cfg, m)
            _fired = set()
            for _ in range(_AUTO_CASCADE_MAX):
                _busy = sum(1 for x in m["steps"] if x["kind"] in _BUSY_KINDS)
                _ready = [s for s in (m.get("actives") or [])
                          if s["kind"] in ("TODO", "PREP")
                          and s["name"] not in _fired]
                if not _ready:
                    break
                _progress = False
                for s in _ready:
                    sc = step_cfg(t, s["name"], m)
                    _is_gen = sc.get("run") == "gen"
                    # patch_max_jobs：技能级并发提交上限（先于材料级 max_inflight）。
                    # 只卡「提交超算」，不卡本地生成输入：达上限时把本材料剩余
                    # 就绪步骤的输入先生成好（变 TODO），等有空位下一轮直接 sbatch。
                    if not _is_gen and not _gate.try_acquire(t["key"]):
                        print("auto-advance：技能 %s 已提交 %d 个作业，达上限 %d；"
                              "其余就绪步骤先本地生成输入（不提交），等有空位自动补交"
                              "（改 task_types.%s.max_jobs 或 TF_MAX_JOBS 调整）。"
                              % (t["key"], _gate.busy(t["key"]),
                                 _gate.cap(t["key"]), t["key"]))
                        _pregenerate_ready(cfg, t, m, _fired)
                        _sfull = True
                        break
                    if _busy >= _cap:
                        print("auto-advance：%s[%s] 在跑 %d 个已达上限 %d，"
                              "本轮不再提交（改 max_inflight 或 "
                              "TF_MAX_INFLIGHT 调整）"
                              % (m["name"], m["tt"], _busy, _cap))
                        break
                    _fired.add(s["name"])
                    _ok = do_submit(cfg, t, m, s, False, True,
                                    sc.get("contcar_to_poscar"),
                                    "auto %s[%s|%s]"
                                    % (m["name"], m["tt"], s["label"]))
                    if not _ok:
                        if not _is_gen:   # 提交失败：把占的槽还回去
                            _gate.release(t["key"])
                        continue
                    _progress = True
                    if _is_gen:
                        # 本地即时步：就地标完成，下一轮重算就绪集
                        s["done"], s["exists"] = True, True
                        s["kind"], s["label_txt"] = "OK", "OK"
                        if "diag" in s:
                            s["diag"] = "completed"
                        _done_now.append(s["name"])
                    else:
                        _busy += 1
                if _sfull:
                    break
                if not _progress:
                    break
                m["actives"] = _dag_recompute(t, m)
            # patch_auto2：产物已在 do_run_gen_step 里当场拉回，这里不再重复
            # --- patch_auto end ------------------------------------------


    if _skipped:
        print("auto-advance 跳过 %d 个（项目级 auto_advance: false）：%s"
              "  → tf -tt <技能> -p <材料> auto on"
              % (len(_skipped), ", ".join(_skipped[:6])
                 + ("…" if len(_skipped) > 6 else "")))

# ===== cmd_step_init (原 L4888-L4927) =====
def cmd_step_init(cfg, data, proj, job, force):
    """tf -p MAT -j STEP init：只生成该步骤的输入文件（gen），不提交。
    已有输入时不覆盖（要推倒重来用 rerun）；前序未完成需 -f。"""
    if not proj or not job:
        print("错误：步骤级 init 需要 -p 材料 和 -j 步骤。")
        return 1
    t, m = find_material(data, proj)
    s = find_step(m, job)
    tag = "init %s[%s|%s]" % (m["name"], m["tt"], s["label"])
    if s["kind"] == "OK" and not force:
        print("%s: 该步骤已完成，无需操作（要强制重新生成输入加 -f）。" % tag)
        return 0
    if s.get("job"):
        if not force:
            print("%s: 已有作业 %s(%s)。要杀掉重新生成请加 -f。"
                  % (tag, s["job"]["id"], s["job"]["state"]))
            return 1
        if not kill_if_queued(cfg, s, True, tag):   # v1.9：-f 自动 scancel
            return 1
        s = dict(s)
        s["job"] = None
    if not guard_predecessors(m, s, force):
        return 1
    if s["has_incar"] and not force:
        print("%s: 输入文件已存在（%s）。检查后直接 tf -p %s -j %s start 提交；"
              "要覆盖重新生成加 -f，要连目录一起删掉重来用 rerun。"
              % (tag, s["dir"], proj, job))
        return 0
    sc = step_cfg(t, s["name"], m)
    ok, out = remote_gen(cfg, t, m, s["name"], host=s.get("_host"))
    if not ok:
        print("%s: gen 失败。%s" % (tag, out))
        return 1
    print("%s: gen 完成。%s" % (tag, out.strip().splitlines()[-1] if out.strip() else ""))
    if sc.get("contcar_to_poscar"):
        run_remote(cfg, "cd %s && [ -f CONTCAR ] && cp CONTCAR POSCAR || true"
                   % shlex.quote(s["dir"]), host=s.get("_host") or "__default__")
    log_action(m, "init %s（只生成输入）" % s["label"])
    print("%s: 输入就绪 → %s（检查后用 start 提交）" % (tag, s["dir"]))
    return 0

# ===== cmd_clean (原 L4930-L5085) =====
def cmd_clean(cfg, data, proj, job, yes, purge_config=False):
    """删除生成物，回到 PREP（材料级保留 POSCAR）。
    无 -p      → 全部材料；-p 材料 → 该材料；-p 体系目录（如 C20）→ 其下所有材料；
    -p -j      → 单个步骤目录；-j 不带 -p → 全部材料的该步骤目录。
    关联作业一并 scancel。默认要确认，-y 跳过。"""
    if job and not proj:  # v3.11：跨材料只 clean 指定步骤
        tgts = [(m, s) for _, m, s in step_targets(data, job)
                if s.get("exists") or s.get("job")]
        if not tgts:
            print("该步骤在所有材料下都无需清理。")
            return 0
        njob = sum(1 for _, s in tgts if s.get("job"))
        for _fm, _fs in tgts:                        # kls4
            if not _fanout_guard(_fm, _fs, yes, "clean"):
                return 1
        if not yes:
            ans = input("clean 全部材料的步骤 %s（%d 个目录%s）？ [y/N] "
                        % (tgts[0][1]["label"], len(tgts),
                           "，并取消 %d 个作业" % njob if njob else "")
                        ).strip().lower()
            if ans not in ("y", "yes"):
                print("已取消操作。")
                return 1
        fails = 0
        for m, s in tgts:
            host = s.get("_host") or "__default__"
            if s.get("job"):
                remote_scancel(cfg, [s["job"]["id"]], host=host)
                print("%s: scancel %s" % (m["name"], s["job"]["id"]))
            if s.get("exists"):
                rc, out = run_remote(cfg, "rm -rf -- " + shlex.quote(s["dir"]),
                                     host=host)
                if rc != 0:
                    print("%s[%s]: 删除失败。%s" % (m["name"], s["label"], out))
                    fails += 1
                    continue
                print("%s[%s]: 已删除 %s" % (m["name"], s["label"], s["dir"]))
                log_action(m, "clean %s（删除 %s）" % (s["label"], s["dir"]))
            _scancel_clear(m, s["name"])   # v1.4：目录已清，stop 标记一并失效
        return fails
    if proj and job:  # 步骤级
        t, m = find_material(data, proj)
        s = find_step(m, job)
        if not s.get("exists") and not s.get("job"):
            print("%s[%s]: 目录不存在且无作业，无需清理。" % (m["name"], s["label"]))
            return 0
        if not _fanout_guard(m, s, yes, "clean"):    # kls4
            return 1
        if not yes:
            ans = input("clean %s[%s]（删除步骤目录%s）？ [y/N] "
                        % (m["name"], s["label"],
                           "，并取消作业 " + s["job"]["id"] if s.get("job") else "")
                        ).strip().lower()
            if ans not in ("y", "yes"):
                print("已取消操作。")
                return 1
        if s.get("job"):
            remote_scancel(cfg, [s["job"]["id"]],
                           host=s.get("_host") or "__default__")
            print("%s: scancel %s" % (m["name"], s["job"]["id"]))
        if s.get("exists"):
            rc, out = run_remote(cfg, "rm -rf -- " + shlex.quote(s["dir"]),
                                 host=s.get("_host") or "__default__")
            if rc != 0:
                print("%s[%s]: 删除失败。%s" % (m["name"], s["label"], out))
                return 1
            print("%s[%s]: 已删除 %s" % (m["name"], s["label"], s["dir"]))
            log_action(m, "clean %s（删除 %s）" % (s["label"], s["dir"]))
        _scancel_clear(m, s["name"])   # v1.4
        return 0

    if proj:  # 体系目录优先（-p C20 → 其下所有材料），否则按材料解析
        mats = [(t["key"], m) for t in data["types"] for m in t["materials"]
                if "/" in m["name"] and m["name"].split("/")[0] == proj]
        if not mats:
            t, m = find_material(data, proj)
            mats = [(t["key"], m)]
    else:
        mats = [(t["key"], m) for t in data["types"] for m in t["materials"]]
    todo = [(k, m, [s["job"] for s in m["steps"] if s.get("job")])
            for k, m in mats]
    njob = sum(len(j) for _, _, j in todo)
    for _fk, _fm, _fj in todo:                       # kls4：逐个列出会被毁的扇出步骤
        for _fs in _fm.get("steps") or []:
            if not _fanout_guard(_fm, _fs, yes, "clean"):
                return 1
    if not yes:
        ans = input("clean %d 个材料（删除全部生成物，只留 POSCAR%s%s）？ [y/N] "
                    % (len(todo),
                       "，并取消 %d 个作业" % njob if njob else "",
                       "；project_setting 也一并删除，重算需 tf init"
                       if purge_config else
                       "；project_setting 保留，可直接重算")
                    ).strip().lower()
        if ans not in ("y", "yes"):
            print("已取消操作。")
            return 1
    def _clean_one(item):
        key, m, jobs = item
        host = m.get("host_eff") or "__default__"
        if jobs:
            remote_scancel(cfg, [j["id"] for j in jobs], host=host)
            print("%s: scancel %s" % (m["name"], " ".join(j["id"] for j in jobs)))
        if not m.get("path"):
            return 0
        line = ("[ -d %s ] && find %s -mindepth 1 -maxdepth 1 ! -name POSCAR "
                "-exec rm -rf -- {} + || true"
                % (shlex.quote(m["path"]), shlex.quote(m["path"])))
        rc, out = run_remote(cfg, line, host=host)
        if rc != 0:
            print("%s: 清理失败。%s" % (m["name"], out))
            return 1
        for d in (m.get("log_dir"), m.get("result_dir")):  # 本地 log/result 一并删
            if d and os.path.isdir(d):
                shutil.rmtree(d, ignore_errors=True)
        _scancel_clear(m)   # v1.4：材料回到全新状态，stop 标记一并清
        # v1.6：材料自己的 project_setting 一并删除（材料回到全新未初始化状态，
        # 重算需 tf init）；体系级共享配置（如 C20/project_setting）保留不动，
        # 否则会误删兄弟材料的配置。
        # v1.3.4：技能段感知——多技能项目的配置里还有其他技能的段时，只移除
        # 本技能段、保留 project_setting；本技能是最后一个段才整目录删。
        # 此前 tf -tt elastic clean 把整目录删掉，band 段跟着丢、band 瘫痪。
        ps = (m.get("ps") or {}).get("dir")
        lp = m.get("lpath")
        _psr = os.path.realpath(ps) if ps else None
        # v1.7：材料自己的 project_setting 既可能在材料级（dirname == 材料目录），
        # 也可能在技能子目录级（dirname(dirname) == 材料目录，如 Mg2C60/band/project_setting）。
        own_ps = bool(ps and lp and os.path.isdir(ps) and (
            os.path.dirname(_psr) == os.path.realpath(lp) or
            os.path.dirname(os.path.dirname(_psr)) == os.path.realpath(lp)))
        kept_note = ""
        if own_ps and not purge_config:
            # v1.9.7：默认保留 project_setting。它是手写的配置，删了不能自动恢复，
            # 而且删光之后连材料都发现不到（local_root 就写在这些 tf_*.yaml 里），
            # 只能靠在项目根裸跑 tf init 兜回来。要连配置一起清用 --purge-config。
            kept_note = "，project_setting 保留（要连配置一起删加 --purge-config）"
        elif own_ps:
            f0s = glob.glob(os.path.join(ps, "tf_*.yaml"))
            remaining, removed = [], False
            if f0s:
                ex = (_load_yaml_file(f0s[0]).get("task_types") or {})
                remaining = [k for k in ex if k != key]
                if key in ex:
                    removed = _yaml_type_block_remove(f0s[0], key)
            if remaining and removed:
                kept_note = "，已移除 %s 段、project_setting 保留（%s）" % (
                    key, ", ".join(remaining))
            else:
                shutil.rmtree(ps, ignore_errors=True)   # log 已删，不写日志
                kept_note = "，project_setting 已删，重算需 tf init"
        else:
            kept_note = "，体系级共享配置保留"
        print("%s: 已清理（本地+超算只留 POSCAR%s）" % (m["name"], kept_note))
        return 0
    results = _parallel_map(_clean_one, todo, desc="clean")
    return sum(r or 0 for r in results)

# ===== _fetch_stamp_clear (原 L5091-L5098) =====
def _fetch_stamp_clear(m, step_name):
    """步骤重提交/重生成后调用：清掉抓取戳记，让 auto-fetch 重拉新结果。"""
    try:
        sp = os.path.join(m.get("result_dir") or "", step_name, FETCH_STAMP)
        if os.path.isfile(sp):
            os.remove(sp)
    except OSError:
        pass

# ===== _relay_prev_across_host (原 L5101-L5142) =====
def _relay_prev_across_host(cfg, m, s):
    """per-step 跨集群数据传递：当前步骤与其前序步骤不在同一超算时，把本地
    result_dir/<prev>/ 已 fetch 的产物上传到当前 host 的 <prev>/ 目录，让 gen
    脚本的 find_prev_dir 就地找到（FORCES_FC3 等大文件走 tar 流，吃内存低）。"""
    steps = m.get("steps") or []
    idx = next((i for i, x in enumerate(steps) if x.get("name") == s.get("name")), -1)
    if idx <= 0:
        return
    cur_host = s.get("_host")
    cur_dir = s.get("dir")
    rdir = m.get("result_dir")
    if not cur_host or not cur_dir or not rdir:
        return
    for p in reversed(steps[:idx]):
        if not (p.get("exists") or p.get("done")):
            continue
        if p.get("_host") == cur_host:
            return   # 同集群：gen 就地能找到，无需回传
        local = os.path.join(rdir, p["name"])
        if not os.path.isdir(local):
            fetch_material(cfg, m, only_steps={p["name"]}, quiet=True)
        if not os.path.isdir(local):
            continue   # 前序产物拉取失败
        remote_prev = os.path.join(os.path.dirname(cur_dir), p["name"])
        remote = "mkdir -p %s && tar -xf - -C %s" % (
            shlex.quote(remote_prev), shlex.quote(remote_prev))
        p1 = subprocess.Popen(["tar", "-cf", "-", "-C", local, "."],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        p2 = subprocess.Popen(_ssh_cmd(cfg, cur_host, [remote]),
                              stdin=p1.stdout, stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE)
        p1.stdout.close()
        rc1 = p1.wait()
        _, err2 = p2.communicate()
        if rc1 != 0 or p2.returncode != 0:
            print("警告：跨集群回传 %s → %s 失败：%s"
                  % (p["name"], remote_prev, (err2 or b"").decode().strip()))
            continue
        print("%s: 已把 %s 产物回传到 %s → %s"
              % (m["name"], p["label"], cur_host, remote_prev))
        log_action(m, "relay %s → %s" % (p["label"], cur_host))
        return

# ===== fetch_material (原 L5145-L5190) =====
def fetch_material(cfg, m, only_steps=None, quiet=False, all_files=False):
    """把该材料各已存在步骤的 fetch_files 从超算拉回本地 result_dir/<step>/。
    用 tar 管道流式传输，缺失文件自动跳过。only_steps = 只拉这些步骤名。"""
    host = m.get("host_eff") or cfg.get("host")
    files = m.get("fetch_files") or []
    if not files:
        if not quiet:
            print("%s: fetch_files 为空，跳过。" % m["name"])
        return True
    nstep = 0
    for s in m["steps"]:
        if not s.get("exists"):
            continue
        if only_steps is not None and s["name"] not in only_steps:
            continue
        dest = os.path.join(m["result_dir"], s["name"])
        os.makedirs(dest, exist_ok=True)
        sc2 = next((x for x in ((m.get("_seg") or {}).get("steps_cfg") or [])
                    if x.get("name") == s["name"]), {})
        if sc2.get("fetch_all") or all_files:   # v3.21：画图步骤产物文件名不固定，整目录拉回；v1.1：fetch --all 整目录
            remote = "cd %s && tar -cf - ." % shlex.quote(s["dir"])
        else:
            remote = ("cd %s && tar --ignore-failed-read -cf - %s"
                      % (shlex.quote(s["dir"]),
                         " ".join(shlex.quote(f) for f in files)))
        s_host = s.get("_host") or host   # v1.12+：每步在各自集群，按步骤 host 拉
        cmd1 = (_ssh_cmd(cfg, s_host, [remote]) if s_host else ["bash", "-c", remote])
        p1 = subprocess.Popen(cmd1, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL)
        p2 = subprocess.run(["tar", "xf", "-", "-C", dest], stdin=p1.stdout,
                            capture_output=True, text=True)
        p1.stdout.close()
        rc1 = p1.wait()
        if rc1 != 0 or p2.returncode != 0:
            print("%s: fetch %s 失败。%s" % (m["name"], s["label"], p2.stderr))
            return False
        try:   # v1.11：抓取成功写戳记（auto-fetch 据此跳过，不再每次重拉）
            open(os.path.join(dest, FETCH_STAMP), "w").close()
        except OSError:
            pass
        nstep += 1
    if nstep and not quiet:
        print("%s: 已拉回 %d 个步骤 → %s" % (m["name"], nstep, m["result_dir"]))
    if nstep:
        log_action(m, "fetch %d 个步骤 → %s" % (nstep, m["result_dir"]))
    return True

# ===== auto_fetch (原 L5193-L5253) =====
def auto_fetch(cfg, data):
    """status 时自动把"已完成但尚未拉回"的步骤结果保存到本地（本地模式；
    项目 setting.yaml 里 auto_fetch: false 可关闭）。失败只警告不打断。"""
    pending = []
    for t in data["types"]:
        for m in t["materials"]:
            if not m.get("result_dir"):
                continue
            st = (m.get("ps") or {}).get("setting") or {}
            if st.get("auto_fetch") is False:
                continue
            need = []
            for s in m["steps"]:
                if not s.get("done"):
                    continue
                dest = os.path.join(m["result_dir"], s["name"])
                # v1.11：按戳记判"已抓取"。旧守卫（dest 非空）对跳过段失效——
                # 它们远端没目录，永远拉不到文件，会每次都重试空拉
                if os.path.isfile(os.path.join(dest, FETCH_STAMP)):
                    continue
                # v1.12：老版本已拉回过的（dest 非空）补写戳记直接跳过——否则
                # 戳记制上线第一次会把全部历史结果（含巨大的 HSE OUTCAR、整目录
                # 画图产物）重拉一遍，表现为长时间"卡死"
                if os.path.isdir(dest) and os.listdir(dest):
                    try:
                        open(os.path.join(dest, FETCH_STAMP), "w").close()
                    except OSError:
                        pass
                    continue
                need.append(s["name"])
            if not need:
                continue
            pending.append((m, need))
    if not pending:
        return
    for m, need in pending:
        print("auto-fetch %s: %s → %s" % (m["name"], ",".join(need),
                                          m["result_dir"]))

    def one(mn):
        m, need = mn
        try:
            ok = fetch_material(cfg, m, only_steps=set(need), quiet=True)
        except Exception as e:  # noqa: BLE001
            ok = False
            print("警告：auto-fetch %s 失败：%s" % (m["name"], e),
                  file=sys.stderr)
        if ok:  # 戳记补写：跳过段远端没目录，fetch 不会经手，这里统一补上
            for nm in need:
                try:
                    d = os.path.join(m["result_dir"], nm)
                    os.makedirs(d, exist_ok=True)
                    open(os.path.join(d, FETCH_STAMP), "w").close()
                except OSError:
                    pass
    if len(pending) == 1:
        one(pending[0])
    else:  # v3.17：多材料并行拉回（配合 ssh 连接复用，等待时间大幅缩短）
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(4, len(pending))) as ex:
            list(ex.map(one, pending))

# ===== cmd_fetch (原 L5256-L5269) =====
def cmd_fetch(cfg, data, mname, all_files=False):
    fails = 0
    if mname:
        t, m = find_material(data, mname)
        mats = [m]
    else:
        mats = [m for t in data["types"] for m in t["materials"]]
    for m in mats:
        if not m.get("result_dir"):
            print("%s: 非本地模式（无 result_dir），跳过 fetch。" % m["name"])
            continue
        if not fetch_material(cfg, m, all_files=all_files):
            fails += 1
    return fails

