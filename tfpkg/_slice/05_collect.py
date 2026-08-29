# -*- coding: utf-8 -*-
# 05_collect —— 远端状态采集（ssh + COLLECTOR）
#
# 本分片由 tfpkg/__init__.py 装配器在单一命名空间里按顺序执行；
# 函数之间的引用按名字解析（与原单文件一致），分片间无需 import。
# 内容清单（按原文件行号）：
#   L1819  _dedup_segments
#   L1880  _queue_total
#   L1889  collect_v3_batch
#   L2046  _ssh_cmd
#   L2063  _ssh_cmd_pre
#   L2069  collect
#   L2100  run_remote
#   L2118  sh_b64
#   L2122  _parallel_map

# ===== _dedup_segments (原 L1819-L1871) =====
def _dedup_segments(mats):
    """多段（项目配置）发现同名材料时：材料归属"其 project_setting 里 tf_*.yaml
    所对应的那个段"（如 C20/qHPC20 归 C20/project_setting/tf_C20.yaml 的段）；
    材料没有 project_setting 时归全局段。无法判定归属的重复保留给重名检查。"""
    import glob as _glob
    _rp = {}   # v-perf：realpath 结果缓存（网络盘/慢盘上较贵；重复材料会反复 realpath）
    _oc = {}   # v-perf：owner_cfg 的 glob+realpath 结果缓存

    def _realpath(p):
        _v = _rp.get(p)
        if _v is None:
            _v = os.path.realpath(p)
            _rp[p] = _v
        return _v

    groups = {}
    order = []
    for m in mats:
        key = _realpath(m.get("lpath") or m["name"])  # v3.11：按材料目录去重
        if key not in groups:                         # （同名/不同名的重复发现都算）
            groups[key] = []
            order.append(key)
        groups[key].append(m)

    def owner_cfg(m):
        psdir = (m.get("ps") or {}).get("dir")
        if not psdir:
            return None
        if psdir in _oc:
            return _oc[psdir]
        cfgs = sorted(_glob.glob(os.path.join(psdir, "tf_*.yaml")))
        res = _realpath(cfgs[0]) if cfgs else None
        _oc[psdir] = res
        return res

    out = []
    for n in order:
        g = groups[n]
        if len(g) == 1:
            out.append(g[0])
            continue
        owned = [m for m in g
                 if (m.get("_seg") or {}).get("_from") == owner_cfg(m)]
        if owned:
            out.append(owned[0])
        elif owner_cfg(g[0]) is None:
            out.append(g[0])  # 无 ps 的新材料被多个重叠段发现 → 只留首个，
                              # 交给"未初始化"提示，不触发重名报错
        else:  # 归属段存在但名字不同（如材料级段 qHPC20 vs 体系段 C20/qHPC20）：
            out.append(max(g, key=lambda m: len((m.get("_seg") or {}).get("_base_dir") or "")))
                       # 取 _base_dir 最深（ps 离材料最近）的发现
    out.sort(key=lambda m: _natkey(m["name"]))
    return out

# ===== _queue_total (原 L1880-L1886) =====
def _queue_total(queue_by_host):
    """把各 host 的队列统计求和（不同 host = 不同集群，作业互不重叠；同 host 已去重）。"""
    out = {}
    for q in (queue_by_host or {}).values():
        for k, v in (q or {}).items():
            out[k] = out.get(k, 0) + (v or 0)
    return out

# ===== collect_v3_batch (原 L1889-L2043) =====
def collect_v3_batch(cfg, segs):
    """v3 本地模式批量采集：所有段（项目配置）先本地解析，再按 (host, work_dir)
    全局分组——同组所有段合进一次 ssh 采集；多组之间并行。
    v3.20 之前每段各发一条 ssh：10 个材料配置就串行 10 次握手（每次 ~2s）。
    返回按段组织的类型条目列表（保持段顺序，_dedup_segments 归属判定依赖它）。"""
    resolved = []
    for t in segs:
        root, mats = discover_local(t["local_root"])
        for m in mats:
            resolve_material_local(t, root, m)
        steps = []
        for s in (t.get("steps") or []):
            sd = {"name": s["name"], "label": s.get("label") or s["name"],
                  "check": s.get("check", "outcar"), "marker": s.get("marker", ""),
                  "submit": s.get("submit", "submit.sh")}
            # v1.8：判据覆盖键非 None 才下发采集器（relax_skip 的 stage、
            # relax_diag/phrase/pressure_tol 覆盖此前根本传不到远端；
            # None 会破坏采集器里 .get(k, 默认值) 的语义）
            # v1.2：改成黑名单——这些键 tf 本地自己消费，其余（含技能私有
            # 判据参数，如 kappa_rtol）一律透传给远端采集器，加技能不用改这里
            for k, v in s.items():
                if k in _LOCAL_ONLY_STEP_KEYS or k in sd or v is None:
                    continue
                sd[k] = v
            steps.append(sd)
        resolved.append((t, root, mats, steps))

    by_hw = {}
    for t, root, mats, steps in resolved:
        for m in mats:
            if not m["work_dir_eff"]:
                sys.exit("错误：%s 的材料 %s 没有 work_dir"
                         "（在 project_setting/setting.yaml 或类型配置里写）。"
                         % (t["key"], m["name"]))
            wd0 = os.path.normpath(os.path.expanduser(str(m["work_dir_eff"])))
            # v1.12：每步可指定超算（步骤配置里 hpc 字段，如 skill.yaml 步骤
            # 或项目 tf_*.yaml 里写 hpc: 3090）。把步骤按各自集群拆开：材料
            # 会出现在多个 (host, work_dir) 组里，每组只带归属该集群的步骤，
            # 采集/提交时各自走对应集群。不写的步骤留在材料默认集群。
            by_step = {}
            for s in steps:
                shpc = step_cfg(t, s["name"], m).get("hpc")
                if shpc and str(shpc) != str(m["hpc_name"]):
                    chpc = _load_yaml_file(pkg_setting_path(str(shpc) + ".yaml")) or {}
                    host = chpc.get("ssh_host") or m["host_eff"]
                    wd = os.path.normpath(os.path.expanduser(
                        str(chpc.get("work_dir") or m["work_dir_eff"])))
                else:
                    host, wd = m["host_eff"], wd0
                by_step.setdefault((host or "", wd), []).append(s)
            for (host, wd), ssteps in by_step.items():
                by_hw.setdefault((host, wd), []).append((t, ssteps, m))

    def probe_group(item):
        (host, wdir), entries = item
        seg_ms = {}
        for t, steps, m in entries:   # 同组内按段聚合（保持首次出现顺序）
            seg_ms.setdefault(id(t), {"t": t, "steps": steps, "ms": []})["ms"].append(m)
        subs, back = [], []
        for g in seg_ms.values():
            # v1.1 skill_subdir：采集键 = 材料名/技能子目录（远端步骤目录多一层），
            # 回流后 name 复原为材料名（显示与匹配不变）
            subs.append({"key": g["t"]["key"],
                         "desc": g["t"].get("desc", g["t"]["key"]),
                         "root": wdir, "steps": g["steps"],
                         "materials": ["%s/%s" % (m["name"], m["_subdir"])
                                       if m.get("_subdir") else m["name"]
                                       for m in g["ms"]]})
            back.append(g)
        d = collect(cfg, subs, host=host or "__default__")
        return ([(g, {x["name"]: x for x in td["materials"]})
                 for g, td in zip(back, d["types"])], host or "", d.get("queue") or {})

    def _safe_probe(item):            # fixte⑦：单组失败/超时不拖垮整张表
        (host, wdir), _ = item
        try:
            return probe_group(item)
        except BaseException as _e:    # 含 collect() 的 SystemExit（超时/失败）
            sys.stderr.write("警告：跳过采集失败的组 host=%s wd=%s：%s\n"
                             % (host or "本地", wdir, _e))
            return ([], host or "", {})
    _nw = int(os.environ.get("TF_WORKERS", "6") or "6")   # fixte⑦：并发可配（=1 串行定位）
    # 大体系分块采集：同组材料太多时，单条 ssh 的 --config64 会超 argv 上限
    # （Argument list too long）。按 TF_COLLECT_CHUNK 切成多块，每块单独 ssh
    # （ControlMaster 复用连接，代价很小）。默认 500：大体系（数千材料）自动
    # 分块，不再需要手动设环境变量；小体系（<500 材料）仍是单块、无额外开销。
    _chunk = max(1, int(os.environ.get("TF_COLLECT_CHUNK", "500") or 500))
    gitems = []
    for _key, _entries in sorted(by_hw.items()):
        for _i in range(0, len(_entries), _chunk):
            gitems.append((_key, _entries[_i:_i + _chunk]))
    if len(gitems) > 1 and _nw > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(_nw, len(gitems))) as ex:
            grouped = list(ex.map(_safe_probe, gitems))
    else:
        grouped = [_safe_probe(x) for x in gitems]

    seg_results = {id(t): [] for t, _, _, _ in resolved}
    _queue_by_host = {}   # 同一 host 的多个分组/分块会各自 squeue，队列数相同，按 host 去重
    for gots, _host, _q in grouped:
        _queue_by_host[_host] = _q
        for g, got in gots:
            t = g["t"]
            for m in g["ms"]:
                rkey = ("%s/%s" % (m["name"], m["_subdir"])
                        if m.get("_subdir") else m["name"])
                if rkey not in got:
                    continue
                r = got[rkey]
                r["name"] = m["name"]   # 采集键带子目录层，显示名复原
                for k in ("lpath", "ps", "hpc_name", "host_eff", "result_dir",
                          "log_dir", "fetch_files", "template_map",
                          "_subdir", "_skill_dir_local", "rpath"):
                    r[k] = m[k]
                r["_seg"] = {"steps_cfg": t.get("steps"),
                             "gen_need": t.get("gen_need"),
                             "aux_files": t.get("aux_files"),
                             "skill_dir": t.get("skill_dir"),
                             "template_dir": t.get("template_dir"),
                             "template_layout": t.get("template_layout"),
                             "gen_dir": t.get("gen_dir"),
                             "_base_dir": t.get("_base_dir"),
                             "_from": t.get("_from"),
                             "optional_off": t.get("_optional_off"),
                             "optional_off_flat": t.get("_optional_off_flat")}
                # v1.12：每步记自己所在集群的 host（跨集群拆分时各组 host 不同）。
                for s in r["steps"]:
                    s["_host"] = _host or m["host_eff"]
                # v1.12：材料可能出现在多个 (host, wd) 组（每步可指定超算），
                # 步骤要合并而不是覆盖（保持原顺序，追加新组的步骤）。
                _lst = seg_results[id(t)]
                _existing = next((e for e in _lst
                                  if e.get("name") == r["name"]), None)
                if _existing is None:
                    seg_results[id(t)].append(r)
                else:
                    _names = {s["name"] for s in _existing["steps"]}
                    _existing["steps"].extend(
                        s for s in r["steps"] if s["name"] not in _names)
                    # v1.12 fix：per-step hpc 拆分后 by_hw 组顺序 ≠ seq 顺序，
                    # 按 skill.yaml 的 seq 重排，保证 _deps / relay 的 prev 判断正确。
                    _scfg = ((_existing.get("_seg") or {}).get("steps_cfg")
                             or t.get("steps") or [])
                    _order = {x.get("name"): i for i, x in enumerate(_scfg)}
                    _existing["steps"].sort(
                        key=lambda s: _order.get(s.get("name"), 999))

    out = []
    for t, root, mats, steps in resolved:
        ms = seg_results[id(t)]
        ms.sort(key=lambda m: _natkey(m["name"]))
        out.append({"key": t["key"], "desc": t.get("desc", t["key"]), "root": root,
                    "local": True, "materials": ms})
    return out, _queue_by_host

# ===== _ssh_cmd (原 L2046-L2060) =====
def _ssh_cmd(cfg, host, remote_args):
    """构造 ssh 命令。v3.17：ControlMaster 连接复用——首条 ssh 建连后，
    ControlPersist 窗口内（默认 120 秒）的后续 ssh 共用通道，免去重复握手，
    status/auto-fetch 明显加速。设环境变量 TF_NO_SSH_MUX=1 可关闭。"""
    opts = ["-o", "BatchMode=yes"]
    if not os.environ.get("TF_NO_SSH_MUX"):
        sockdir = os.path.expanduser("~/.ssh")
        try:
            os.makedirs(sockdir, exist_ok=True)
        except OSError:
            pass
        opts += ["-o", "ControlMaster=auto",
                 "-o", "ControlPath=" + os.path.join(sockdir, "tf-%r@%h:%p.sock"),
                 "-o", "ControlPersist=600"]
    return ["ssh"] + opts + [host] + remote_args

# ===== _ssh_cmd_pre (原 L2063-L2066) =====
def _ssh_cmd_pre(cfg, host, pre_opts, remote_args):
    """同 _ssh_cmd，但允许在 host 前追加选项（如 ConnectTimeout）。"""
    base = _ssh_cmd(cfg, host, [])          # ["ssh", 复用选项..., host]
    return base[:-1] + pre_opts + [host] + remote_args

# ===== collect (原 L2069-L2097) =====
def collect(cfg, types, host="__default__"):
    # v1.2：把本次涉及技能的 checks.py 源码一起打包，远端注册成判据
    extra = skill_checks_for(cfg, [td.get("key") for td in types])
    payload = base64.b64encode(
        json.dumps({"user": cfg.get("user"), "types": types,
                    "extra_checks": extra,
                    "path_prefix": (cfg.get("remote_path_prefix") or "")}).encode()).decode()
    args = ["python3", "-", "--config64", payload]
    if host == "__default__":
        host = cfg.get("host")
    cmd = (_ssh_cmd_pre(cfg, host, ["-o", "ConnectTimeout=60"], ["timeout", "170"] + args)
           if host else args)
    try:
        r = subprocess.run(cmd, input=COLLECTOR, capture_output=True, text=True, timeout=180)
    except FileNotFoundError:
        sys.exit("错误：找不到 ssh 命令。")
    except subprocess.TimeoutExpired:
        sys.exit("错误：连接超时（host=%s）。" % host)
    if r.returncode != 0:
        sys.exit("错误：远端采集失败。\n" + (r.stderr or "").strip())
    data = json.loads(r.stdout)
    if os.environ.get("TF_DEBUG_TIME") and data.get("timings"):
        tm = data["timings"]
        print("[计时] 远端分解：squeue %.1fs + 目录探测 %.1fs（其余为 ssh/启动开销）"
              % (tm.get("squeue", 0), tm.get("probe", 0)), file=sys.stderr)
    if data.get("squeue_err"):
        print("警告：squeue 查询失败：%s（队列状态将只按文件判断）" % data["squeue_err"],
              file=sys.stderr)
    return data

# ===== run_remote (原 L2100-L2115) =====
def run_remote(cfg, shell_line, host="__default__", use_stdin=False):
    """use_stdin=True 时整个脚本经 stdin 投递（bash -s），不受 argv 长度限制
    （gen 推送大量 base64 文件时必须用，否则 Argument list too long）。"""
    if host == "__default__":
        host = cfg.get("host")
    _pp = (cfg.get("remote_path_prefix") or "").strip()
    if _pp and host:
        shell_line = "export PATH=\"%s:$PATH\"; %s" % (_pp, shell_line)
    if use_stdin:
        cmd = (_ssh_cmd(cfg, host, ["timeout 600 bash -s"]) if host else ["bash", "-s"])
        r = subprocess.run(cmd, input=shell_line, capture_output=True, text=True)
    else:
        cmd = (_ssh_cmd(cfg, host, [shell_line])
               if host else ["bash", "-c", shell_line])
        r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()

# ===== sh_b64 (原 L2118-L2119) =====
def sh_b64(cmd_text):
    return "echo %s | base64 -d | bash" % base64.b64encode(cmd_text.encode()).decode()

# ===== _parallel_map (原 L2122-L2151) =====
def _parallel_map(worker, items, nw=None, desc="批量操作"):
    """并发跑 worker(item)。TF_OP_WORKERS 控制并发数（默认 8；=1 串行定位用）。
    批量 clean/start/auto 等逐材料操作都是"每材料若干次 ssh 往返"，串行时
    上百个材料就是上百次握手——并发后共享 ControlMaster 通道，提速明显。
    输出不锁序（各线程消息可能交错，但每行完整）；异常项吞掉并告警，不中断整批。"""
    import concurrent.futures as _cf
    items = list(items)
    _nw = int(nw or os.environ.get("TF_OP_WORKERS", "8") or 8)
    if _nw <= 1 or len(items) <= 1:
        return [worker(x) for x in items]

    def _safe(x):
        try:
            return worker(x)
        except BaseException as _e:
            sys.stderr.write("警告：%s 异常：%s\n" % (desc, _e))
            return None
    _done = [0]

    def _progress(_):
        _done[0] += 1
        if _done[0] % 100 == 0:
            sys.stderr.write("  %s %d/%d\n" % (desc, _done[0], len(items)))
            sys.stderr.flush()
    with _cf.ThreadPoolExecutor(max_workers=min(_nw, len(items))) as _ex:
        _futs = [_ex.submit(_safe, x) for x in items]
        for _f in _futs:
            _f.add_done_callback(_progress)
        out = [_f.result() for _f in _futs]
    return out

