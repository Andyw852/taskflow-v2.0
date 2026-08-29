
import base64, getpass, glob, json, os, re, subprocess, sys, atexit, tempfile, time

def natkey(s):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]

# ---------- 文件尾读取缓存（perf）----------
# 两级缓存：
#   1) 进程内 memo：同一轮采集里同一文件尾只读一次（消除三段弛豫/振荡判据的重复读）。
#   2) 跨进程磁盘缓存：key=(路径,mtime_ns,ctime_ns,size,nbytes)。任一写操作都会改变
#      mtime/ctime/size 三者之一，命中即内容未变，无陈旧风险——watch 每 300 秒的
#      "无变化"轮从此近乎零 IO。TF_READ_CACHE=0 关闭；路径/上限可用环境变量调。
_READ_CACHE_DISABLE = os.environ.get("TF_READ_CACHE") == "0"
_READ_CACHE_PATH = os.path.expanduser(
    os.environ.get("TF_READ_CACHE_PATH") or "~/.cache/taskflow/read_cache.json")
try:
    _READ_CACHE_MAX_ENTRIES = int(os.environ.get("TF_READ_CACHE_MAX", "2048") or 2048)
    _READ_CACHE_MAX_BYTES = int(os.environ.get("TF_READ_CACHE_BYTES", "134217728") or 134217728)
except (TypeError, ValueError):
    _READ_CACHE_MAX_ENTRIES, _READ_CACHE_MAX_BYTES = 2048, 134217728

_tail_memo = {}          # (path, mtime_ns, ctime_ns, size, nbytes) -> text
_tail_memo_bytes = 0
_TAIL_MEMO_MAX_BYTES = 268435456   # 进程内 memo 上限 256MB
_tail_disk = None        # 磁盘缓存 dict，惰性加载；None=未加载
_tail_disk_dirty = False
_TAIL_MISS = object()


def _tail_disk_load():
    global _tail_disk
    if _tail_disk is not None:
        return
    if _READ_CACHE_DISABLE:
        _tail_disk = {}
        return
    try:
        with open(_READ_CACHE_PATH, encoding="utf-8") as _f:
            _d = json.load(_f)
        _tail_disk = _d if isinstance(_d, dict) else {}
    except Exception:
        _tail_disk = {}


def _tail_disk_save():
    global _tail_disk, _tail_disk_dirty
    if _tail_disk is None or not _tail_disk_dirty:
        return
    try:
        _items = sorted(_tail_disk.items(), key=lambda kv: kv[1][0])
        _total = sum(len(v[1]) for _, v in _items)
        while _items and (len(_items) > _READ_CACHE_MAX_ENTRIES
                          or _total > _READ_CACHE_MAX_BYTES):
            _k, _v = _items.pop(0)
            _total -= len(_v[1])
            _tail_disk.pop(_k, None)
        _d = os.path.dirname(_READ_CACHE_PATH)
        os.makedirs(_d, exist_ok=True)
        _fd, _tmp = tempfile.mkstemp(dir=_d, prefix=".read_cache.", suffix=".tmp")
        try:
            with os.fdopen(_fd, "w", encoding="utf-8") as _f:
                json.dump(_tail_disk, _f, ensure_ascii=False)
            os.replace(_tmp, _READ_CACHE_PATH)
        finally:
            if os.path.exists(_tmp):
                try:
                    os.remove(_tmp)
                except OSError:
                    pass
    except Exception:
        pass
    _tail_disk_dirty = False


def _tail_memo_put(key, text):
    global _tail_memo_bytes
    _tail_memo[key] = text
    _tail_memo_bytes += len(text)
    if _tail_memo_bytes > _TAIL_MEMO_MAX_BYTES or len(_tail_memo) > 4096:
        _tail_memo.clear()
        _tail_memo_bytes = 0


def tail_text(path, nbytes=1000000):   # v3.18：默认 1MB，收敛/结束标志都在末尾
    global _tail_disk_dirty
    try:
        st = os.stat(path)
    except OSError:
        return ""
    key = (path, st.st_mtime_ns, st.st_ctime_ns, st.st_size, nbytes)
    _v = _tail_memo.get(key, _TAIL_MISS)
    if _v is not _TAIL_MISS:
        return _v
    dk = None
    if not _READ_CACHE_DISABLE:
        _tail_disk_load()
        if _tail_disk is not None:
            dk = json.dumps([path, st.st_mtime_ns, st.st_ctime_ns, st.st_size, nbytes])
            _ent = _tail_disk.get(dk)
            if _ent is not None:
                _ent[0] = time.time()
                _tail_disk_dirty = True
                _tail_memo_put(key, _ent[1])
                return _ent[1]
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - nbytes))
            data = f.read()
        if size > nbytes:      # 首个字节可能落在半行，丢弃残缺首行（对齐行边界）
            _nl = data.find(b"\n")
            if _nl != -1:
                data = data[_nl + 1:]
        text = data.decode("utf-8", "ignore")
    except OSError:
        return ""
    _tail_memo_put(key, text)
    if dk is not None and _tail_disk is not None:
        _tail_disk[dk] = [time.time(), text]
        _tail_disk_dirty = True
    return text

atexit.register(_tail_disk_save)

# ---------- 步骤完成判据 ----------
# ---------- 弛豫诊断：判断是不是在空转 ----------
# 判据全部只读 OSZICAR（每步几十字节，很便宜），必要时才读 INCAR 拿 NSW/NELM。
# 目的：把"还在往下走"和"已经在原地打转"区分开，后者应当停掉换 IBRION=1 或
# 回退到固定胞阶段，而不是继续烧机时。
RELAX_DIAG_DEFAULTS = {
    "window":      8,        # 看最后多少个离子步
    "osc_tol":     5e-3,     # eV，|dE| 小于它且符号乱翻 = 小幅振荡
    "stall_tol":   1e-4,     # eV，|dE| 小于它但力没收敛 = 停滞
    "jump_tol":    0.5,      # eV，单步能量上涨超过它 = 线搜索把结构甩飞了
    "min_steps":   6,        # 少于这么多步不下结论
}

def _incar_vals(d):
    v = {}
    try:
        with open(os.path.join(d, "INCAR"), encoding="utf-8", errors="ignore") as f:
            for ln in f:
                ln = ln.split("#")[0].split("!")[0]
                for part in ln.split(";"):
                    if "=" in part:
                        k, x = part.split("=", 1)
                        v[k.strip().upper()] = x.strip()
    except OSError:
        pass
    return v

def read_oszicar_ionic(d):
    """返回 [(离子步号, F, dE, 最后一步电子迭代序号), ...]。
    第四个字段是"该离子步收尾时最后一个 SCF 迭代序号"（如 DAV: 12 → 12），
    用它判断是否撞 NELM（序号 >= NELM 则该步的力不可信）。"""
    p = os.path.join(d, "OSZICAR")
    if not os.path.isfile(p):
        return []
    out, last_scf = [], 0
    for ln in tail_text(p, 4000000).splitlines():
        m = re.match(r"\s*(DAV|RMM|CG|EDDAV|DIIS|BLK)\s*:\s*(\d+)", ln)
        if m:
            last_scf = int(m.group(2))
            continue
        m = re.match(r"\s*(\d+)\s+F=\s*(\S+)\s+E0=\s*(\S+)\s+d\s*E\s*=\s*(\S+)", ln)
        if m:
            try:
                out.append((int(m.group(1)), float(m.group(2)),
                            float(m.group(4)), last_scf))
            except ValueError:
                pass
            last_scf = 0
    return out

def relax_diagnose(d, cfg=None):
    """返回 (verdict, text)。verdict ∈ progressing/oscillating/stalled/
       thrown/electronic/nsw/unknown。text 是给人看的一句话。"""
    c = dict(RELAX_DIAG_DEFAULTS)
    c.update((cfg or {}).get("relax_diag", {}) if isinstance(cfg, dict) else {})
    steps = read_oszicar_ionic(d)
    n = len(steps)
    if n == 0:
        return "unknown", "还没有完成的离子步"

    iv = _incar_vals(d)
    def _i(k, dflt):
        try:
            return int(float(iv.get(k, dflt)))
        except (TypeError, ValueError):
            return dflt
    nsw, nelm = _i("NSW", 0), _i("NELM", 60)
    ibrion, isif = iv.get("IBRION", "?"), iv.get("ISIF", "?")

    bits = ["%d 步" % n]

    # --- 1. 电子步撞 NELM：力是错的，别的判断都不用看了 ---
    bad = [s for s in steps if s[3] >= nelm]
    if bad:
        return "electronic", ("%s；%d 步电子循环撞了 NELM=%d，这些步的力不可信 "
                              "-> 先调大 NELM 或放宽 EDIFF，别继续弛豫"
                              % ("/".join(bits), len(bad), nelm))

    W = min(n, int(c["window"]))
    win = steps[-W:]
    dEs = [x[2] for x in win]

    # --- 2. 单步能量暴涨：CG 线搜索把结构甩出去了 ---
    up = max(dEs)
    if up > c["jump_tol"]:
        return "thrown", ("%s；最近有一步能量上涨 %.3f eV —— CG 的线搜索把结构甩飞了。"
                          "对策：先跑阶段 a（ISIF=2 固定胞）把原子弛豫干净，"
                          "或把 POTIM 调到 0.1" % ("/".join(bits), up))

    if n < int(c["min_steps"]):
        return "progressing", "%s；步数还少，继续观察" % "/".join(bits)

    flips = sum(1 for i in range(1, len(dEs)) if dEs[i] * dEs[i - 1] < 0)
    amax = max(abs(x) for x in dEs)
    net = sum(dEs)
    bits.append("末 %d 步 dE 翻转 %d 次，|dE|max=%.2g eV，净变化 %+.2g eV"
                % (W, flips, amax, net))

    hit_nsw = (nsw and n >= nsw)
    if hit_nsw:
        bits.append("已撞 NSW=%d" % nsw)

    # --- 3. 振荡 ---
    if flips >= max(2, (W - 1) // 2):
        if amax < c["osc_tol"]:
            adv = ("小幅振荡，已经在极小值附近了 -> 换 IBRION=1（准牛顿）收尾，"
                   "通常 10 步内落底" if str(ibrion).strip() != "1"
                   else "IBRION=1 仍在小幅振荡 -> 检查 EDIFFG 是不是设得太严，"
                        "或 EDIFF 不够紧导致力有噪声")
        else:
            adv = ("大幅振荡（|dE| 到 %.2g eV）-> 两组自由度在打架。"
                   "先跑阶段 a 固定胞弛豫原子，再放开晶胞" % amax)
        return "oscillating", "%s；%s" % ("；".join(bits), adv)

    # --- 4. 停滞 ---
    if amax < c["stall_tol"]:
        return "stalled", ("%s；能量基本不动但力还没到判据 -> 换 IBRION=1 收尾；"
                           "若已是 IBRION=1，多半是 EDIFFG 过严或 EDIFF/NELM 不足"
                           % "；".join(bits))

    if hit_nsw:
        return "nsw", ("%s；还在往下走但步数用完了 -> cp CONTCAR POSCAR 续跑，"
                       "或直接进入 IBRION=1 阶段" % "；".join(bits))

    return "progressing", "%s；仍在正常下降" % "；".join(bits)


def ck_outcar_relax(d, cfg):
    p = os.path.join(d, "OUTCAR")
    if not os.path.isfile(p):
        return False, "OUTCAR missing"
    text = tail_text(p)
    phrase = cfg.get("phrase", "reached required accuracy")
    if phrase not in text:
        _v, _t = relax_diagnose(d, cfg)
        return False, "未收敛 [%s] %s" % (_v, _t)
    tol = float(cfg.get("pressure_tol", 5.0))
    m = re.findall(r"external pressure\s*=\s*(-?[\d.]+)\s*kB", text)
    if not m:
        # 极端长跑（NSW 很大）时压力行可能被推到 1MB 窗口之外，扩大窗口再找一次
        m = re.findall(r"external pressure\s*=\s*(-?[\d.]+)\s*kB",
                       tail_text(p, 8000000))
    if not m:
        return False, "pressure not found"
    pr = float(m[-1])
    if abs(pr) > tol:
        # 力过了但应力没过：ISIF=3 的常态（EDIFFG 负值只判力、不判应力）。
        # 2D 固定 c 轴时真空方向永远有残余应力，这里的判据应当放宽或改看面内分量。
        return False, ("力已收敛但压强 %.1f kB > %g kB -> cp CONTCAR POSCAR 再跑一轮"
                       "（Pulay 应力，通常 2~3 轮收敛）；2D 冻结 c 轴时残余压强属正常，"
                       "可调大本步骤的 pressure_tol" % (pr, tol))
    return True, "converged (p=%.1fkB)" % pr

def ck_outcar(d, cfg):
    p = os.path.join(d, "OUTCAR")
    if not os.path.isfile(p):
        return False, "OUTCAR missing"
    # 该标志是 OUTCAR 的最后一行，400KB 尾部足够（比 1MB 默认值更省 IO）
    if "General timing and accounting informations" not in tail_text(p, 400000):
        return False, "OUTCAR incomplete"
    return True, "finished"

def ck_wavecar(d, cfg):
    p = os.path.join(d, "WAVECAR")
    if not os.path.isfile(p):
        return False, "WAVECAR missing"
    sz = os.path.getsize(p)
    mn = int(cfg.get("wavecar_min", 1024 * 1024))
    if sz < mn:
        return False, "WAVECAR too small (%dB)" % sz
    return True, "WAVECAR OK (%.1fMB)" % (sz / 1e6)

def ck_eigenval(d, cfg):
    if not os.path.isfile(os.path.join(d, "EIGENVAL")):
        return False, "EIGENVAL missing"
    if os.path.isfile(os.path.join(d, "KPOINTS_OPT")) and \
       not os.path.isfile(os.path.join(d, "vasprun.xml")):
        return False, "vasprun.xml missing"
    return True, "band output OK"

def ck_marker(d, cfg):
    spec = cfg.get("marker", "OUTCAR:General timing and accounting informations")
    fn, text = spec.split(":", 1)
    p = os.path.join(d, fn)
    if not os.path.isfile(p):
        return False, fn + " missing"
    if text not in tail_text(p):
        return False, fn + " incomplete"
    return True, "finished"


def ck_phonon(d, cfg):
    """S5_fc/S3_fc 虚频闸判据：读 phonon_summary.json，区分
    稳定 / 虚频(imaginary) / 真失败(工具错误) 三态。
    返回 (done, diag, imaginary)。虚频不是 error —— 算完了但动力学不稳定。"""
    p = os.path.join(d, "phonon_summary.json")
    if not os.path.isfile(p):
        return False, "phonon_summary.json missing", False
    try:
        with open(p, encoding="utf-8") as _f:
            js = json.load(_f)
    except Exception as e:
        return False, "phonon_summary.json invalid (%s)" % e, False
    if not js.get("tool_ok", True):          # mesh 没算成 = 真失败(error)
        return False, "phonon gate tool error", False
    mf = js.get("min_frequency_THz")
    mfs = ("min_freq=%.3f THz" % mf) if isinstance(mf, (int, float)) else "n/a"
    if js.get("stable"):                     # '"stable": true' → 完成
        return True, "stable (%s)" % mfs, False
    return False, "imaginary frequency (%s)" % mfs, True   # 完成但有虚频

def ck_plot(d, sc):
    """画图步骤：done_marker（默认 band_summary.json）或任一 .png 存在即完成。"""
    marker = sc.get("done_marker") or "band_summary.json"
    if os.path.isfile(os.path.join(d, marker)):
        return True, marker
    pngs = glob.glob(os.path.join(d, "*.png"))
    if pngs:
        return True, os.path.basename(pngs[0])
    return False, "no plot output"


def _relax_conv(d):
    """该弛豫目录已收敛：OUTCAR 存在且含 reached required accuracy。
    不查压强——2D 冻结 c 轴时真空方向残余压属正常（压强判断留给人工）。"""
    p = os.path.join(d, "OUTCAR")
    return os.path.isfile(p) and "reached required accuracy" in tail_text(p)


def ck_relax_skip(d, sc):
    """三段式弛豫的收敛感知判据（步骤里写 stage: a/b/c + check: relax_skip）。
    变段数：a 收敛 → b/c 跳过；b 收敛 → c 跳过；c 是收敛总闸，
    跑了没收敛 → FAIL 并附空转诊断（relax_diagnose）。
    a/b "跑过"（有 OUTCAR）即 done——前两段不收敛是常态，自然流转下一段。
    旧单段 step1_PBE_opt 已收敛 → 三段全部 done（老材料免迁移直接跳过）。
    步骤目录约定：<材料目录>/step1{a,b,c}_PBE_opt。"""
    mat = os.path.dirname(d)
    def P(s):
        return os.path.join(mat, "step1%s_PBE_opt" % s)
    def ran(s):
        return os.path.isfile(os.path.join(P(s), "OUTCAR"))
    if _relax_conv(os.path.join(mat, "step1_PBE_opt")):
        return True, "旧单段已收敛，跳过"
    st = str(sc.get("stage") or "")

    def gate(s):
        """v1.9：段 s 跑完未收敛时的放行闸门（防空转）。
        病态轨迹——甩飞(thrown)/电子步撞 NELM(electronic)/大幅振荡——
        拦截在本段（FAIL 等人工）：带病结构进下一段只会接着震荡烧机时。
        小幅振荡/停滞/NSW 用尽但仍在下降 → 放行，下一段换算法自然收尾。
        返回 None=放行，否则=拦截理由。"""
        v, t = relax_diagnose(P(s), sc)
        if v in ("thrown", "electronic"):
            return t
        if v == "oscillating":
            cc = dict(RELAX_DIAG_DEFAULTS)
            if isinstance(sc, dict) and sc.get("relax_diag"):
                cc.update(sc["relax_diag"])
            # 二次读 OSZICAR：tail_text 有缓存，此处命中内存缓存、仅重解析（无重复 IO）
            steps = read_oszicar_ionic(P(s))
            W = min(len(steps), int(cc["window"]))
            if W >= 2 and max(abs(x[2]) for x in steps[-W:]) >= float(cc["osc_tol"]):
                return t   # 大幅振荡（|dE| 超 osc_tol）
        return None

    if st == "a":
        if _relax_conv(P("a")):
            return True, "converged"
        if ran("a"):
            bad = gate("a")
            if bad:
                return False, ("a 轨迹异常，不放行 b：%s → 按对策处理后 "
                               "tf retry 本段" % bad)
            return True, "ran（未收敛，流转 b）"
        if ran("b") or ran("c"):
            return True, "后续段已跑"
        return False, "OUTCAR missing"
    if st == "b":
        if _relax_conv(P("a")):
            return True, "a 已收敛，跳过"
        if ran("b"):
            if _relax_conv(P("b")):
                return True, "converged"
            bad = gate("b")
            if bad:
                return False, ("b 轨迹异常，不放行 c：%s → 按对策处理后 "
                               "retry 本段（或回 a 段 rerun）" % bad)
            return True, "ran（未收敛，流转 c）"
        if ran("c"):
            return True, "c 已跑"
        return False, "OUTCAR missing"
    # c：收敛总闸——前面任何一段收敛则跳过；自己收敛才 done
    if _relax_conv(P("a")) or _relax_conv(P("b")):
        return True, "前段已收敛，跳过"
    if _relax_conv(P("c")):
        return True, "converged"
    if ran("c"):
        _v, _t = relax_diagnose(P("c"), sc)
        return False, "c 未收敛 [%s] %s" % (_v, _t)
    return False, "OUTCAR missing"


CHECKERS = {"outcar_relax": ck_outcar_relax, "outcar": ck_outcar,
            "wavecar": ck_wavecar, "eigenval": ck_eigenval, "marker": ck_marker,
            "plot": ck_plot, "relax_skip": ck_relax_skip}

def collect_type(t, jobs_by_dir):
    root = os.path.realpath(os.path.expanduser(t["root"]))
    cfg_steps = t.get("steps") or []
    step_names = [s["name"] for s in cfg_steps]

    def has_incar_sub(d):
        try:
            return any(os.path.isfile(os.path.join(d, x, "INCAR"))
                       for x in os.listdir(d) if os.path.isdir(os.path.join(d, x)))
        except OSError:
            return False

    materials = []
    if t.get("materials"):
        for name in t["materials"]:
            materials.append({"name": name, "path": os.path.join(root, name)})
    else:
        cands = [root]
        for pat in ("/*", "/*/*"):
            cands += [p for p in glob.glob(root + pat) if os.path.isdir(p)]
        picked, picked_set = [], set()
        for c in cands:
            if has_incar_sub(c) and c not in picked_set:
                picked.append(c); picked_set.add(c)
        for c in cands:
            if c in picked_set or not os.path.isfile(os.path.join(c, "POSCAR")):
                continue
            anc, inside = os.path.dirname(c), False
            while anc.startswith(root) and anc != root:
                if anc in picked_set:
                    inside = True
                    break
                anc = os.path.dirname(anc)
            if not inside:
                picked.append(c); picked_set.add(c)
        for c in picked:
            name = os.path.relpath(c, root)
            materials.append({"name": name if name != "." else os.path.basename(c),
                              "path": c})
        materials.sort(key=lambda m: natkey(m["name"]))

    scfg = {s["name"]: s for s in cfg_steps}

    def probe(m):
        """单材料全部步骤的文件状态与完成判据（IO 密集，材料间并行）。"""
        names = list(step_names)
        if not names:
            try:
                names = sorted([x for x in os.listdir(m["path"])
                                if os.path.isfile(os.path.join(m["path"], x, "INCAR"))],
                               key=natkey)
            except OSError:
                names = []
        steps = []
        for sname in names:
            d = os.path.normpath(os.path.join(m["path"], sname))
            sc = scfg.get(sname, {})
            f = {"name": sname, "label": sc.get("label", sname), "dir": d,
                 "exists": os.path.isdir(d),
                 "has_incar": os.path.isfile(os.path.join(d, "INCAR")),
                 "has_outcar": os.path.isfile(os.path.join(d, "OUTCAR")),
                 "has_slurm_out": bool(glob.glob(os.path.join(d, "slurm-*.out")) or glob.glob(os.path.join(d, "queue.out"))),
                 "submit": sc.get("submit", "submit.sh")}
            j = jobs_by_dir.get(d)
            f["job"] = j
            if sc.get("fanout"):
                # v1.4 扇出步骤：步骤目录下每个子目录 = 一个独立作业。
                # done 要求全部子目录判据都过；has_* 取子目录的并集，
                # 这样 step_state 的 PREP/TODO/FAIL 三态判断照常成立。
                subs = sorted((p for p in glob.glob(
                    os.path.join(d, str(sc["fanout"]))) if os.path.isdir(p)),
                    key=natkey)
                ck = CHECKERS.get(sc.get("check", "outcar"), ck_outcar)
                fj, ndone, todo = [], 0, []
                for p in subs:
                    jp = jobs_by_dir.get(p)
                    okp = ck(p, sc)[0]
                    if okp:
                        ndone += 1
                    if jp:
                        fj.append(jp)
                    elif not okp:
                        todo.append(os.path.basename(p))
                n = len(subs)
                f["fanout"] = str(sc["fanout"])   # 回填 glob，本地提交要用
                f["subs"] = [os.path.basename(p) for p in subs]
                f["fan_jobids"] = [x["id"] for x in fj]
                f["fan_todo"] = todo          # 没作业也没完成 → 待提交/失败
                f["fan_done"] = ndone
                f["has_incar"] = any(os.path.isfile(os.path.join(p, "INCAR"))
                                     for p in subs)
                f["has_outcar"] = any(os.path.isfile(os.path.join(p, "OUTCAR"))
                                      for p in subs)
                f["has_slurm_out"] = any((glob.glob(os.path.join(p, "slurm-*.out")) or glob.glob(os.path.join(p, "queue.out")))
                                         for p in subs)
                if fj:
                    # fixte⑫：CG/CF = 正在取消/收尾，不能混进 R，否则 scancel 刚
                    # 发出、作业还在 CG 时会被显示成"正在运行"，start 被拦下，
                    # 看起来像取消失败。
                    nr = sum(1 for x in fj if x.get("state") == "R")
                    npd = sum(1 for x in fj if x.get("state") == "PD")
                    ncg = sum(1 for x in fj if x.get("state") in ("CG", "CF"))
                    f["job"] = dict(fj[0])
                    f["job"]["info"] = "%d/%d %dR %dPD%s" % (
                        ndone, n, nr, npd, (" %dCG" % ncg) if ncg else "")
                    f["job"]["state"] = "R" if nr else ("PD" if npd else "CG")
                    f["done"] = False
                    f["diag"] = ""     # 进度已在 label 里（R@3/5 2R 0PD），不重复
                elif not n:
                    f["done"], f["diag"] = False, "dir missing"
                else:
                    f["done"] = (ndone == n)
                    f["diag"] = ("%d/%d" % (ndone, n) if f["done"] else
                                 "%d/%d 完成；未完成 %s" %
                                 (ndone, n, ",".join(todo[:4]) +
                                  ("…" if len(todo) > 4 else "")))
            elif sc.get("check") == "plot":
                f["plot"] = True          # v3.21：画图步骤不提交作业，状态三态
                if not f["exists"]:
                    f["done"], f["diag"] = False, "not started"
                else:
                    f["done"], f["diag"] = ck_plot(d, sc)
                    if not f["done"]:
                        f["plot_error"] = True   # 目录在但没有产出 = 上次运行失败
            elif j and j.get("state") in ("R", "PD", "CG", "CF"):
                f["done"], f["diag"] = False, "job " + j["state"]  # v3.19：作业在跑/排队
            elif sc.get("check") == "phonon":
                if not f["exists"]:
                    f["done"], f["diag"] = False, "not started"
                else:
                    f["done"], f["diag"], f["imaginary"] = ck_phonon(d, sc)
            elif f["exists"] or sc.get("check") == "relax_skip":   # → 不读输出文件
                # relax_skip 目录不在也要判：它看的是兄弟段目录（a 收敛则
                # b/c 跳过；旧单段收敛则全跳过），自己的目录多半还没生成
                ck = CHECKERS.get(sc.get("check", "outcar" if step_names
                                  else "marker"), ck_outcar)
                f["done"], f["diag"] = ck(d, sc)
            else:
                f["done"], f["diag"] = False, "dir missing"
            steps.append(f)
        m["steps"] = steps
        # v3.3：维度标记（任一步骤目录 workflow_method.txt 里的 DIM=2D/3D）
        m["dim"] = ""
        for sname in names:
            mf = os.path.join(m["path"], sname, "workflow_method.txt")
            if os.path.isfile(mf):
                try:
                    for ln in open(mf, errors="ignore"):
                        if ln.upper().startswith("DIM="):
                            v = ln.split("=", 1)[1].strip().upper()
                            if v in ("2D", "3D"):
                                m["dim"] = v
                            break
                except OSError:
                    pass
            if m["dim"]:
                break
        return m

    if len(materials) > 1:   # v3.18：材料间并行探测（IO 密集，GIL 无影响）
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(8, len(materials))) as ex:
            materials = list(ex.map(probe, materials))
    else:
        materials = [probe(m) for m in materials]
    return {"key": t["key"], "desc": t.get("desc", t["key"]), "root": root,
            "materials": materials}

def main():
    import time as _time
    _t0 = _time.time()
    args = sys.argv[1:]
    opts = dict(zip(args[::2], args[1::2]))
    cfg = json.loads(base64.b64decode(opts["--config64"]).decode("utf-8"))
    _pp = cfg.get("path_prefix") or ""
    if _pp:
        os.environ["PATH"] = os.path.expandvars(_pp) + os.pathsep + os.environ.get("PATH", "")
    user = cfg.get("user") or getpass.getuser()

    # ---------- squeue（%Z = 作业工作目录，所有类型共用一份队列数据） ----------
    jobs, squeue_err = [], None
    def run_squeue(fmt):
        return subprocess.run(["squeue", "-u", user, "-h", "-o", fmt],
                              capture_output=True, text=True, timeout=60)
    try:
        r = run_squeue("%i|%j|%T|%M|%D|%R|%Z")
        if r.returncode != 0:
            r = run_squeue("%i|%j|%T|%M|%D|%R")
        if r.returncode != 0:
            squeue_err = (r.stderr or "").strip()
        else:
            STATE_MAP = {"RUNNING": "R", "PENDING": "PD", "COMPLETING": "CG",
                         "CONFIGURING": "CF", "SUSPENDED": "S", "CANCELLED": "CA"}
            for ln in r.stdout.splitlines():
                p = ln.rstrip("\n").split("|")
                if len(p) < 6:
                    continue
                st = STATE_MAP.get(p[2], p[2])   # 兼容长状态名（RUNNING/PENDING）和缩写（R/PD）
                jobs.append({"id": p[0], "name": p[1], "state": st, "time": p[3],
                             "nodes": p[4], "info": p[5],
                             "workdir": os.path.normpath(p[6]) if len(p) > 6 and p[6] else ""})
    except Exception as e:
        squeue_err = str(e)
    jobs_by_dir = {}
    for j in jobs:
        if j["workdir"] and j["workdir"] not in jobs_by_dir:
            jobs_by_dir[j["workdir"]] = j

    # 技能私有判据：源码随 payload 下发，在这里注册进 CHECKERS
    # v1.10：多个技能可以共用公共池里的同一份 checks.py（skill.yaml 写
    # checks: ../_common/checks_relax.py）——源码相同就只注册一次，
    # 不再当成重名冲突；只有【不同源码抢同一个判据名】才报错。
    _seen_src = {}
    for _sk, _src in (cfg.get("extra_checks") or {}).items():
        if _src in _seen_src:
            continue
        _seen_src[_src] = _sk
        _ns = dict(globals())
        try:
            exec(compile(_src, "<skill:%s>" % _sk, "exec"), _ns)
        except Exception as _e:
            sys.exit("技能 %s 的 checks.py 执行失败：%s" % (_sk, _e))
        for _n, _f in (_ns.get("CHECKERS") or {}).items():
            if _n in CHECKERS:
                sys.exit("技能 %s 的判据名 %s 与已有判据冲突（同名但不是同一份 "
                         "checks.py），请改名或改成共用公共池那一份。" % (_sk, _n))
            CHECKERS[_n] = _f

    _t1 = _time.time()
    types = [collect_type(t, jobs_by_dir) for t in cfg["types"]]
    _t2 = _time.time()
    _queue = {"R": 0, "PD": 0, "other": 0, "total": len(jobs)}
    for _j in jobs:
        if _j.get("state") == "R":
            _queue["R"] += 1
        elif _j.get("state") == "PD":
            _queue["PD"] += 1
        else:
            _queue["other"] += 1
    print(json.dumps({"types": types, "squeue_err": squeue_err, "queue": _queue,
                      "timings": {"squeue": _t1 - _t0, "probe": _t2 - _t1}},
                     ensure_ascii=False))

main()
