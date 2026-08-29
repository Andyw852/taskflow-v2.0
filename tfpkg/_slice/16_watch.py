# -*- coding: utf-8 -*-
# 16_watch —— 后台监控 watch（daemon/cron）
#
# 本分片由 tfpkg/__init__.py 装配器在单一命名空间里按顺序执行；
# 函数之间的引用按名字解析（与原单文件一致），分片间无需 import。
# 内容清单（按原文件行号）：
#   L6846  _watch_files
#   L6854  _watch_running_pid
#   L6870  _watch_pid_alive
#   L6878  _watch_daemon
#   L6911  _watch_stop
#   L6927  _watch_ensure
#   L6953  _watch_cron
#   L6982  _watch_cfg_sig
#   L7013  cmd_watch

# ===== _watch_files (原 L6846-L6851) =====
def _watch_files(cfg=None):
    """watch 的 pid/log 路径：v1.10 起锚定配置文件所在目录（setting/），
    在任何目录执行 tf watch --stop 都找得到；旧版 cwd 下的 pid 文件由
    _watch_stop 兜底识别。"""
    base = (cfg or {}).get("_config_dir") or os.getcwd()
    return (os.path.join(base, WATCH_PID), os.path.join(base, WATCH_LOG))

# ===== _watch_running_pid (原 L6854-L6867) =====
def _watch_running_pid(cfg=None):
    """返回在跑的后台监控 PID（没有在跑返回 None）。先看锚定位置，再看 cwd。"""
    cands = [_watch_files(cfg)[0], os.path.abspath(WATCH_PID)]
    for pidfile in dict.fromkeys(cands):
        if not os.path.isfile(pidfile):
            continue
        try:
            pid = int(open(pidfile).read().strip())
        except ValueError:
            pid = None
        if pid and _watch_pid_alive(pid):
            return pid, pidfile
        os.remove(pidfile)
    return None, None

# ===== _watch_pid_alive (原 L6870-L6875) =====
def _watch_pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False

# ===== _watch_daemon (原 L6878-L6908) =====
def _watch_daemon(a, mat_toks, root, cfg=None):
    """tf watch -d：把监控作为 detached 子进程放后台，日志写配置目录下。"""
    pidfile, logfile = _watch_files(cfg)
    pid, _ = _watch_running_pid(cfg)
    if pid:
        print("tf watch 已在后台运行 (PID %d)" % pid)
        print("日志：%s（tail -f 查看）；停止：tf watch --stop" % logfile)
        return
    argv = [sys.executable, os.path.realpath(sys.argv[0])]
    if a.config:
        argv += ["-c", a.config]
    if a.tt:
        argv += ["-tt", a.tt]
    if a.proj:
        argv += ["-p", a.proj]
    if a.exclude:
        argv += ["-x", a.exclude]
    if a.user:
        argv += ["-u", a.user]
    argv += ["watch", "-i", str(a.interval)]
    if root:
        argv.append(root)
    argv += mat_toks
    log = open(logfile, "ab", buffering=0)
    p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log,
                         stderr=subprocess.STDOUT, start_new_session=True)
    with open(pidfile, "w") as f:
        f.write(str(p.pid))
    print("tf watch 已转入后台 (PID %d)" % p.pid)
    print("日志：%s（tail -f %s 查看）" % (logfile, logfile))
    print("停止：tf watch --stop")

# ===== _watch_stop (原 L6911-L6924) =====
def _watch_stop(cfg=None):
    """tf watch --stop：按 pid 文件停止后台监控（任意目录可执行）。"""
    import signal as _sig
    pid, pidfile = _watch_running_pid(cfg)
    if pid:
        os.kill(pid, _sig.SIGTERM)
        try:
            os.remove(pidfile)
        except OSError:
            pass
        print("已停止 tf watch (PID %d)。" % pid)
        return 0
    print("没有运行中的 tf watch。")
    return 1

# ===== _watch_ensure (原 L6927-L6950) =====
def _watch_ensure(cfg):
    """v1.10 auto_watch：任何 tf 命令顺带确保后台监控在跑（没在跑就拉起）。
    配合 tf watch --install 的 crontab 保活 = 零输入全自动：
    重启/WSL 关闭后 cron 拉起；平时敲任何 tf 命令也会顺带拉起。
    保活失败绝不影响主命令。"""
    if not cfg.get("auto_watch"):
        return
    try:
        pid, _ = _watch_running_pid(cfg)
        if pid:
            return
        pidfile, logfile = _watch_files(cfg)
        argv = [sys.executable, os.path.realpath(sys.argv[0])]
        if cfg.get("_config_path"):
            argv += ["-c", cfg["_config_path"]]
        argv += ["watch", "-i", str(cfg.get("watch_interval") or 300)]
        log = open(logfile, "ab", buffering=0)
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log,
                             stderr=subprocess.STDOUT, start_new_session=True)
        with open(pidfile, "w") as f:
            f.write(str(p.pid))
        print("已自动启动后台监控 tf watch (PID %d)。日志：%s" % (p.pid, logfile))
    except Exception:
        pass

# ===== _watch_cron (原 L6953-L6979) =====
def _watch_cron(install):
    """tf watch --install/--uninstall：crontab 保活——每 10 分钟检查，
    监控死了（重启/崩溃）自动拉起。tf watch -d 有 pid 检查，不会重复启动。"""
    import shutil as _sh
    marker = "# tf-watch-keepalive"
    if not _sh.which("crontab"):
        print("系统没有 crontab 命令。手动保活方案：")
        print("  每 10 分钟执行一次：%s watch -d" % os.path.realpath(sys.argv[0]))
        return 1
    cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    old = [] if cur.returncode else [l for l in cur.stdout.splitlines()
                                     if marker not in l]
    if install:
        exe = os.path.realpath(sys.argv[0])
        old.append("*/10 * * * * %s watch -d >/dev/null 2>&1  %s"
                   % (exe, marker))
    r = subprocess.run(["crontab", "-"], input="\n".join(old) + "\n",
                       text=True, capture_output=True)
    if r.returncode:
        print("写入 crontab 失败：%s" % (r.stderr or "").strip())
        return 1
    if install:
        print("已写入 crontab 保活：每 10 分钟确保 tf watch 在跑（重启后自动恢复）")
        print("查看：crontab -l；移除：tf watch --uninstall")
    else:
        print("已移除 crontab 保活。")
    return 0

# ===== _watch_cfg_sig (原 L6982-L7010) =====
def _watch_cfg_sig(cfg):
    """v1.8：配置签名——主配置 + project_roots 下全部 *.yaml/*.yml 的
    (路径, mtime_ns, size)（result/log/隐藏目录不扫，里面没有配置）。
    watch 每轮对比，变了自动重载配置。"""
    sig = []
    cp = cfg.get("_config_path")
    if cp and os.path.isfile(cp):
        try:
            st = os.stat(cp)
            sig.append((os.path.realpath(cp), st.st_mtime_ns, st.st_size))
        except OSError:
            pass
    for r in (cfg.get("project_roots") or []):
        r = os.path.realpath(os.path.expanduser(r))
        if not os.path.isdir(r):
            continue
        for dp, dn, fn in os.walk(r):
            dn[:] = [d for d in dn
                     if d not in ("result", "log") and not d.startswith(".")]
            for f in fn:
                if f.endswith((".yaml", ".yml")):
                    fp = os.path.join(dp, f)
                    try:
                        st = os.stat(fp)
                    except OSError:
                        continue
                    sig.append((fp, st.st_mtime_ns, st.st_size))
    sig.sort()
    return tuple(sig)

# ===== cmd_watch (原 L7013-L7075) =====
def cmd_watch(cfg, types, projs, exclude, interval, tt=None, root=None,
              overrides=None):
    """v3.15 监控模式：每 interval 秒重新采集 → auto-fetch → auto-advance。
    v1.8：每轮检测配置文件改动（tf.yaml / project_setting/*.yaml / 各级
    hpc.yaml），变了自动重载——改配置或换 tf 版本后不用手动重启监控。
    状态有变化才打印总表，否则打印一行心跳。Ctrl+C 退出。"""
    import time as _time
    try:
        sys.stdout.reconfigure(line_buffering=True)  # 重定向/管道时也逐行输出
    except Exception:
        pass

    def _banner(c):
        return ("auto-advance 开" if c.get("auto_advance") else
                "auto-advance 关（tf.yaml 里 auto_advance: true 可开）")
    print("监控模式：每 %d 秒刷新（auto-fetch + %s；配置改动自动重载），"
          "Ctrl+C 退出。" % (interval, _banner(cfg)))
    last = None
    sig = _watch_cfg_sig(cfg)
    try:
        while True:
            s2 = _watch_cfg_sig(cfg)
            if s2 != sig:   # v1.8：配置变了 → 重载（沿用 adopt 的重载路径）
                try:
                    c2, _ = load_config(cfg.get("_config_path"))
                    c2["_config_dir"] = cfg["_config_dir"]
                    c2["_config_path"] = cfg["_config_path"]
                    for k, v in (overrides or {}).items():
                        c2[k] = v          # 命令行 --host/-u 覆盖照旧生效
                    c2 = apply_skills(c2)   # 重载需重装技能骨架（否则 get_types 缺 steps）
                    c2 = merge_project_configs(c2)
                    t2 = get_types(c2, tt=tt, root_override=root)
                    cfg, types = c2, t2
                    sig = _watch_cfg_sig(cfg)   # 以重载后的新配置为准重算
                    last = None                 # 强制重印一次总表
                    print("[%s] 检测到配置变更，已自动重载（%s）。"
                          % (_time.strftime("%H:%M:%S"), _banner(cfg)))
                except (SystemExit, Exception) as e:
                    # 新配置有误：保留旧配置继续跑，文件再改动会再试
                    print("[%s] 配置变更但重载失败（%s），沿用旧配置继续监控。"
                          % (_time.strftime("%H:%M:%S"), e))
                    sig = s2
            data = collect_data(cfg, types)
            fill_local_dim(cfg, data, types)
            # patch_state_cache：把本轮采集结果写进本地缓存，前台 tf list/summary
            # 在 TTL 内直接读它，不用再 ssh 采集一遍。
            _state_cache_save(cfg, data, types, tt, root)
            apply_exclude(data, exclude)
            filter_projs(data, projs)
            auto_fetch(cfg, data)
            auto_recover_hung(cfg, data)   # v1.11: 挂死作业自动恢复（scancel+CONTCAR 续跑）
            auto_advance(cfg, data)
            if cfg.get("hide_done"):
                apply_hide_done(data)   # v1.1：监控里同样支持隐藏完成项
            snap = _snapshot(data)
            if snap != last:
                cmd_status(cfg, data, projs[0] if len(projs) == 1 else None, None)
                last = snap
            else:
                print("[%s] 无变化" % _time.strftime("%H:%M:%S"))
            _time.sleep(interval)
    except KeyboardInterrupt:
        print("\n已退出监控。")

