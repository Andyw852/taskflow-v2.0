# -*- coding: utf-8 -*-
# 09_submit —— 远端生成 / sbatch 提交 / scancel 取消
#
# 本分片由 tfpkg/__init__.py 装配器在单一命名空间里按顺序执行；
# 函数之间的引用按名字解析（与原单文件一致），分片间无需 import。
# 内容清单（按原文件行号）：
#   L3253  _scancel_desc
#   L3269  remote_scancel
#   L3282  remote_gen
#   L3372  remote_sbatch_fanout
#   L3436  remote_sbatch
#   L3462  kill_if_queued
#   L3479  do_run_gen_step
#   L3513  do_submit
#   L3549  _fanout_guard
#   L3581  do_rerun_step
#   L3607  tag_of

# ===== _scancel_desc (原 L3253-L3266) =====
def _scancel_desc(jobids):
    """fixte⑫：把代表 jobid 展开成真实取消数量，用于回显。

    扇出步骤一个"代表 jobid"背后是几百个子作业（remote_scancel 会用
    FAN_JOBIDS 展开后一起 scancel）。回显若只打代表号，看着像只取消了一个。
    """
    ids = []
    for x in (jobids or []):
        for y in FAN_JOBIDS.get(str(x), [str(x)]):
            if y not in ids:
                ids.append(y)
    if len(ids) > len(list(jobids or [])):
        return "%d 个作业（代表 %s）" % (len(ids), " ".join(str(x) for x in jobids))
    return " ".join(str(x) for x in ids)

# ===== remote_scancel (原 L3269-L3279) =====
def remote_scancel(cfg, jobids, host="__default__"):
    ids = []                                  # v1.4：代表 jobid → 全部 jobid
    for x in (jobids or []):
        for y in FAN_JOBIDS.get(str(x), [str(x)]):
            if y not in ids:
                ids.append(y)
    jobids = ids
    if not jobids:
        return True, ""
    rc, out = run_remote(cfg, "scancel " + " ".join(jobids), host=host)
    return rc == 0, out

# ===== remote_gen (原 L3282-L3366) =====
def remote_gen(cfg, t, m, sname, host=None):
    """执行 gen：先建目录、补 POSCAR（v3 本地模式）和 gen_need 依赖文件、gen 脚本，
    再运行。文件来源：find_asset 查找链（project_setting > skill_dir，支持
    template_map 映射，本地 base64 经 ssh 推送，超算无需存放）> gen_dir
    （远端目录，超算上 cp）。材料目录已有的文件不覆盖。"""
    sc = step_cfg(t, sname, m)
    gen = sc.get("gen")
    if not gen:
        return False, "任务类型 %s 的步骤 %s 没有配置 gen" % (t["key"], sname)
    gen = gen.format(mat=m["name"], matdir=m["path"], root=t["root"],
                     step=sname, tt=t["key"])
    seg = (m.get("_seg") or {})
    gd = seg.get("gen_dir") or t.get("gen_dir")
    host = host or m.get("host_eff") or "__default__"
    # gen 允许带参数，例如 "gen_step1_PBE_opt.py --stage a"：
    # 前面是脚本名（用于查找/推送），后面原样作为命令行参数传下去。
    _m_py = re.match(r"^(\S+\.py)(\s.*)?$", gen)
    is_py = bool(_m_py)
    gen_script = _m_py.group(1) if _m_py else gen
    gen_args = (_m_py.group(2) or "").strip() if _m_py else ""
    if "gen_need" in sc:   # v1.0：步骤声明 gen_need 则完全替代类型级依赖
        need = list(sc.get("gen_need") or [])       # （画图步不需要 dim_common 等）
    else:
        need = (list(seg.get("gen_need") or t.get("gen_need") or [])
                + list(seg.get("aux_files") or t.get("aux_files") or []))
        # v1.4：template_map 的逻辑名（submit_std_*.tpl 等）始终纳入推送清单。
        # gen 脚本在远端按逻辑名找模板；gen_need 漏写时老材料靠远端残留文件
        # 掩盖，新材料（空目录）就报"找不到模板"——这里兜底，不依赖清单完整。
        for lg in (m.get("template_map") or {}):
            if lg not in need:
                need.append(lg)
    line = "mkdir -p %s && cd %s && " % (shlex.quote(m["path"]),
                                         shlex.quote(m["path"]))
    if is_py:  # gen 脚本以 skill 为唯一样板：总是覆盖推送（本地改了立即生效）
        gsrc = find_asset(cfg, t, m, gen_script, sname)
        if gsrc:
            with open(gsrc, "rb") as fh:
                gb64 = base64.b64encode(fh.read()).decode()
            line += "echo %s | base64 -d > %s ; " % (gb64, shlex.quote(gen_script))
        else:
            need = need + [gen_script]  # 本地找不到 → gen_dir 远端兜底
    lp = m.get("lpath")
    if lp:  # v3：POSCAR 以本地项目目录为准，远端缺则推送
        pos = os.path.join(lp, "POSCAR")
        if os.path.isfile(pos):
            with open(pos, "rb") as fh:
                b64 = base64.b64encode(fh.read()).decode()
            line += "[ -f POSCAR ] || echo %s | base64 -d > POSCAR ; " % b64
    for f in need:
        if f == STEP_CONF:      # v1.9：step.conf 不按单文件推，先本地合并分层
            text, _lg = build_step_conf(cfg, t, m, sname)
            if text is None:
                return False, ("找不到任何 %s（skill/templates 与 "
                               "project_setting/templates 都没有）" % STEP_CONF)
            b64 = base64.b64encode(text.encode("utf-8")).decode()
            line += "echo %s | base64 -d > %s ; " % (b64, shlex.quote(f))
            continue
        local_src = find_asset(cfg, t, m, f, sname)
        if local_src:
            with open(local_src, "rb") as fh:
                data = fh.read()
            b64 = base64.b64encode(data).decode()
            # v1.3.1：依赖文件 md5 比对、不同才覆盖——此前"存在即不推"，
            # 本地 skill 库更新（如 dim_common 加函数）后远端旧版残留，
            # 与新 gen 脚本混用直接 ImportError
            lmd5 = hashlib.md5(data).hexdigest()
            line += ("[ -f %s ] && [ \"$(md5sum %s 2>/dev/null | "
                     "cut -d' ' -f1)\" = %s ] || echo %s | base64 -d > %s ; "
                     % (shlex.quote(f), shlex.quote(f), lmd5,
                        b64, shlex.quote(f)))
        elif gd:
            line += ("[ -f %s ] || { [ -f %s ] && cp %s . || "
                     "{ echo 'ERROR: gen_dir 里缺少 %s' >&2; exit 1; }; }; "
                     % (shlex.quote(f), shlex.quote(os.path.join(t["root"], gd, f)),
                        shlex.quote(os.path.join(t["root"], gd, f)), f))
        else:
            return False, ("project_setting/skill_dir 里缺少 %s，"
                           "且未配置 gen_dir 兜底" % f)
    if is_py:
        line += "python %s%s" % (shlex.quote(gen_script),
                                 (" " + gen_args) if gen_args else "")
        rc, out = run_remote(cfg, line, host=host, use_stdin=True)
    else:
        rc, out = run_remote(cfg, line + sh_b64(gen), host=host, use_stdin=True)
    return rc == 0, out

# ===== remote_sbatch_fanout (原 L3372-L3433) =====
def remote_sbatch_fanout(cfg, s, jobname=None):
    """扇出步骤：步骤目录下每个匹配子目录各自 sbatch 一次。

    s["fan_todo"] 非空时只提交这些子目录（retry 只补没完成的）；
    为空或缺失时提交全部（首次 gen 之后就是这条路）。
    返回 (是否成功, 输出, 逗号分隔的全部 jobid)。
    """
    pat = str(s.get("fanout"))
    only = s.get("fan_todo") or None
    # patch_fanout_cap：扇出是"每个子目录各 sbatch 一次"的无上限循环，而
    # max_inflight 只数步骤、不数子目录 —— kl 的 findiff 三阶位移动辄上千个，
    # auto_advance 会一口气全交出去，占满作业配额把别的技能一起堵死。
    # 这里先远端数一遍，超阈值直接拒绝（retry 补帧的 fan_todo 不受限）。
    _cap = int(os.environ.get("TF_FANOUT_MAX",
                              str(cfg.get("fanout_max", 200))) or 200)
    if _cap > 0 and not only:
        _rc0, _o0 = run_remote(cfg, sh_b64(
            "cd %s 2>/dev/null && ls -d %s 2>/dev/null | wc -l || echo 0"
            % (shlex.quote(s["dir"]), pat)),
            host=s.get("_host") or "__default__")
        try:
            _n = int((_o0 or "0").strip().splitlines()[-1])
        except (ValueError, IndexError):
            _n = 0
        if _n > _cap:
            return (False,
                    "扇出 %d 个子目录，超过上限 %d，已拒绝提交。\n"
                    "  确认要交：TF_FANOUT_MAX=%d tf -tt <技能> -p <材料> start\n"
                    "  或先减少位移数：tf -tt kl -p <材料> -j 4 "
                    "conf --set params.METHOD=alm\n"
                    "  永久调阈值：全局 tf.yaml 写 fanout_max: <N>"
                    % (_n, _cap, _n + 1), None)
    cands = [s["submit"]] + [c for c in ("submit.sh", "sub.sh", "job.sh",
                                         "run.sh", "sub.slurm")
                             if c != s["submit"]]
    jn = re.sub(r"[^A-Za-z0-9_.-]", "_", str(jobname or ""))
    ln = ["cd %s || exit 1" % shlex.quote(s["dir"]), "rc=0",
          "ONLY=%s" % (shlex.quote(" ".join(only)) if only else "''"),
          "for d in %s; do" % pat,
          '  [ -d "$d" ] || continue',
          '  if [ -n "$ONLY" ]; then',
          '    case " $ONLY " in *" $d "*) ;; *) continue ;; esac',
          '  fi',
          '  ( cd "$d" || exit 1',
          '    f=""',
          '    for c in %s; do [ -f "$c" ] && f="$c" && break; done' % " ".join(cands),
          '    [ -z "$f" ] && f=$(ls *.sub *.slurm 2>/dev/null | head -1)',
          '    if [ -z "$f" ]; then',
          '      echo "ERROR: $d 里找不到提交脚本" >&2; exit 1',
          '    fi']
    if jn:
        ln.append('    sed -i -e "s/^#SBATCH[[:space:]]\\+--job-name=.*/'
                  '#SBATCH --job-name=%s-$d/" -e "s/^#SBATCH[[:space:]]\\+-J'
                  '[[:space:]].*/#SBATCH --job-name=%s-$d/" "$f" '
                  '2>/dev/null || true' % (jn, jn))
    ln += ['    sbatch "$f" ) || rc=1',
           "done",
           "exit $rc"]
    rc, out = run_remote(cfg, sh_b64("\n".join(ln)),
                         host=s.get("_host") or "__default__")
    jids = re.findall(r"Submitted batch job\s+(\d+)", out or "")
    return (rc == 0 and bool(jids)), out, (",".join(jids) if jids else None)

# ===== remote_sbatch (原 L3436-L3459) =====
def remote_sbatch(cfg, s, jobname=None):
    if s.get("fanout"):                       # v1.4
        return remote_sbatch_fanout(cfg, s, jobname=jobname)
    cands = [s["submit"]] + [c for c in
                             ("submit.sh", "sub.sh", "job.sh", "run.sh", "sub.slurm")
                             if c != s["submit"]]
    loop = ("f=''; for c in %s; do [ -f \"$c\" ] && f=\"$c\" && break; done; "
            % " ".join(cands))
    loop += ("[ -z \"$f\" ] && f=$(ls *.sub *.slurm 2>/dev/null | head -1); "
             "[ -z \"$f\" ] && { echo 'ERROR: 步骤目录里找不到提交脚本' >&2; exit 1; }; ")
    if jobname:
        jn = re.sub(r"[^A-Za-z0-9_.-]", "_", str(jobname))
        loop += ("sed -i -e 's/^#SBATCH[[:space:]]\\+--job-name=.*/#SBATCH --job-name=%s/' "
                 "-e 's/^#SBATCH[[:space:]]\\+-J[[:space:]].*/#SBATCH --job-name=%s/' \"$f\"; "
                 "grep -q '^#SBATCH --job-name=%s' \"$f\" || "
                 "sed -i '0,/^#SBATCH/s//#SBATCH --job-name=%s\\n&/' \"$f\"; " % (jn, jn, jn, jn))
    loop += "sbatch \"$f\""
    rc, out = run_remote(cfg, "cd %s && %s" % (shlex.quote(s["dir"]), loop),
                         host=s.get("_host") or "__default__")
    jid = None
    m = re.search(r"Submitted batch job\s+(\d+)", out or "")
    if m:
        jid = m.group(1)
    return rc == 0 and jid is not None, out, jid

# ===== kill_if_queued (原 L3462-L3476) =====
def kill_if_queued(cfg, s, force, tag):
    j = s.get("job")
    if not j:
        return True
    if not force:
        if str(j.get("state")) in ("CG", "CF"):   # fixte⑫：正在取消中，不是还在算
            print("%s: 上一批作业正在取消中(CG，SLURM 异步收尾)，"
                  "等几秒再 start；急的话加 -f 强制。" % tag)
            return False
        print("%s: 已有作业 %s(%s)，先 stop 或加 -f。" % (tag, j["id"], j["state"]))
        return False
    ok, out = remote_scancel(cfg, [j["id"]], host=s.get("_host") or "__default__")
    print("%s: scancel %s %s" % (tag, _scancel_desc([j["id"]]),
                                 "成功" if ok else ("失败: " + out)))
    return ok

# ===== do_run_gen_step (原 L3479-L3510) =====
def do_run_gen_step(cfg, t, m, s, tag):
    """run: gen 的步骤（v3.21 能带画图等）：只在材料目录远端执行 gen 脚本，
    不提交 SLURM；完成后按 done_marker 复判。失败（目录残留无产出）下次
    状态显示 error，retry/rerun 可重来。"""
    if s.get("job") and not kill_if_queued(cfg, s, True, tag):
        return False
    ok, out = remote_gen(cfg, t, m, s["name"], host=s.get("_host"))
    if not ok:
        print("%s: 运行失败。%s" % (tag, out))
        return False
    marker = step_cfg(t, s["name"], m).get("done_marker") or "band_summary.json"
    rc, o = run_remote(cfg, "test -f %s && echo MARKER_OK"
                       % shlex.quote(os.path.join(s["dir"], marker)),
                       host=s.get("_host") or "__default__")
    if rc == 0 and "MARKER_OK" in (o or ""):
        print("%s: 已生成 %s（%s）" % (tag, s["dir"], marker))
        log_action(m, "plot %s（生成 %s）" % (s["label"], s["dir"]))
        _fetch_stamp_clear(m, s["name"])   # v1.11：产物已更新，让 auto-fetch 重拉
        _scancel_clear(m, s["name"])       # v1.4：重跑成功，清 stop 标记
        if m.get("result_dir"):   # patch_auto2：即时步产物立刻拉回，
            s["done"], s["exists"] = True, True   # 不等下一轮 auto_fetch
            try:
                if fetch_material(cfg, m, only_steps={s["name"]}, quiet=True):
                    print("%s: 已拉回 → %s"
                          % (tag, os.path.join(m["result_dir"], s["name"])))
            except Exception as _e:   # noqa: BLE001
                print("警告：拉回 %s 失败：%s" % (s["name"], _e), file=sys.stderr)
        return True
    tail = (out or "").strip().splitlines()
    print("%s: 脚本运行了但没产出 %s%s（状态将显示 error，检查日志后 retry）"
          % (tag, marker, ("：" + tail[-1]) if tail else ""))
    return False

# ===== do_submit (原 L3513-L3546) =====
def do_submit(cfg, t, m, s, force, gen_first, contcar_cp, tag, submit=True):
    """返回 True=成功 / False=失败或被拒绝（供退出码统计）。
    submit=False：只生成输入（gen），不 sbatch、不触发本地生成步，交由 tf start。"""
    if step_cfg(t, s["name"], m).get("run") == "gen":  # v3.21：画图等轻量步骤
        if not submit:
            print("%s: 本地生成步（画图/读取），已就绪，待 tf … start 触发。" % tag)
            return True
        return do_run_gen_step(cfg, t, m, s, tag)
    if not kill_if_queued(cfg, s, force, tag):
        return False
    if gen_first or not s["has_incar"]:
        _relay_prev_across_host(cfg, m, s)   # v1.12：跨集群回传前序产物
        ok, out = remote_gen(cfg, t, m, s["name"], host=s.get("_host"))
        if not ok:
            print("%s: gen 失败。%s" % (tag, out))
            return False
        print("%s: gen 完成。%s" % (tag, out.strip().splitlines()[-1] if out.strip() else ""))
    if contcar_cp:
        run_remote(cfg, "cd %s && [ -f CONTCAR ] && cp CONTCAR POSCAR || true"
                   % shlex.quote(s["dir"]))
    if not submit:   # v3.22：只生成不提交，交由 start
        print("%s: 已生成输入，未提交。检查后运行  tf -p %s -j %s start  提交。"
              % (tag, m["name"].split("/")[-1], s["label"]))
        log_action(m, "gen %s（只生成输入，待 start 提交）" % s["label"])
        return True
    jobname = "%s-%s-%s" % (m["name"].split("/")[-1], m["tt"], s["label"])
    ok, out, jid = remote_sbatch(cfg, s, jobname=jobname)
    print("%s: %s" % (tag, ("已提交 %s (jobid=%s)" % (jobname, jid)) if ok
                            else ("提交失败。" + out)))
    if ok:
        log_action(m, "%s jobid=%s" % (tag.split(" ", 1)[0] + " " + s["label"], jid))
        _fetch_stamp_clear(m, s["name"])   # v1.11：重交后结果会更新，清戳记重拉
        _scancel_clear(m, s["name"])       # v1.4：重交成功，清 stop 标记
    return ok

# ===== _fanout_guard (原 L3549-L3578) =====
def _fanout_guard(m, s, yes, action):
    """kls4：rerun / clean 会 rm -rf 整个步骤目录。扇出步骤（kl 的 S4_disp 等）
    目录里往往躺着几百个算完的位移帧，删掉就是几百个机时；而"补缺帧"用 retry
    就够了（gen 幂等，已有 INCAR+POSCAR 的子目录直接跳过，不动任何文件）。
    所以这里额外拦一道：有已完成子目录就必须完整输入步骤名才放行。
    返回 True=继续，False=中止。"""
    ndone = int(s.get("fan_done") or 0)
    if not s.get("fanout") or ndone <= 0:
        return True
    n = len(s.get("subs") or []) or ndone
    p = str(m["name"]).split("/")[-1]
    print("")
    print("  !! %s[%s] 是扇出步骤，目录里有 %d/%d 个【已算完】的子目录。"
          % (m["name"], s["label"], ndone, n))
    print("     %s 会 rm -rf %s —— 这 %d 个结果全部丢失，要重算。"
          % (action, s["dir"], ndone))
    print("     只是想补没算完的帧的话，用 retry（不删任何文件）：")
    print("       tf -tt %s -p %s -j %s retry" % (m.get("tt", "<技能>"), p, s["label"]))
    print("       tf -tt %s -p %s -j %s start -f" % (m.get("tt", "<技能>"), p, s["label"]))
    if yes:
        print("     （已给 -y，跳过确认，继续 %s）" % action)
        return True
    try:
        ans = input("     确认要删？完整输入步骤名 %s 继续（其他任意键取消）：" % s["name"])
    except EOFError:
        ans = ""
    if ans.strip() != s["name"]:
        print("     已取消，%s 未执行。" % action)
        return False
    return True

# ===== do_rerun_step (原 L3581-L3604) =====
def do_rerun_step(cfg, t, m, s, yes, tag):
    if s.get("job"):
        if not kill_if_queued(cfg, s, True, tag):
            return False
    if s["exists"]:
        if not _fanout_guard(m, s, yes, "rerun"):   # kls4
            return False
        if not yes:
            ans = input("删除 %s 并重新生成？ [y/N] " % s["dir"]).strip().lower()
            if ans not in ("y", "yes"):
                print("%s: 已跳过。" % tag)
                return False
        rc, out = run_remote(cfg, "rm -rf -- " + shlex.quote(s["dir"]),
                             host=s.get("_host") or "__default__")
        if rc != 0:
            print("%s: 删除失败。%s" % (tag, out))
            return False
        print("%s: 已删除 %s" % (tag, s["dir"]))
        log_action(m, "rerun %s（删除 %s）" % (s["label"], s["dir"]))
    s2 = dict(s)
    s2["has_incar"] = False
    s2["job"] = None
    return do_submit(cfg, t, m, s2, force=False, gen_first=True, contcar_cp=False,
                     tag=tag, submit=False)

# ===== tag_of (原 L3607-L3608) =====
def tag_of(m, s):
    return "%s[%s|%s]" % (m["name"], m["tt"], s["label"])

