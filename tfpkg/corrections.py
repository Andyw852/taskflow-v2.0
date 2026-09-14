# -*- coding: utf-8 -*-
"""corrections —— _corrections/ 纠错 handler 库的加载、匹配与 tf correct 命令。

设计（2026-09-14 建议 1.1，详见 skill/_common/_corrections/README.md）：
  · handler = 一个"诊断特征 → 该动什么"的可枚举单元（skill/_common/_corrections/*.py）；
  · 本模块负责把它们**按需加载**（tf 启动时不加载，只有 tf schema / tf diagnose /
    tf correct 真正需要时才 import），并做"诊断 ↔ handler"的匹配；
  · 匹配纯字符串、纯本地，不连超算；只有 handler.apply() 会碰远端输入文件，
    且那一步由 tf correct -y 显式触发。

加载方式：把每个 _corrections 目录注册成一个**命名空间包**，再用正常 import 机制
加载目录里的模块——这样 handler 文件之间可以写相对导入（from .base import ...，
照抄模板即可），也不需要把技能目录塞进 sys.path（不污染全局）。
"""

import importlib
import importlib.machinery
import importlib.util
import hashlib
import os
import sys

# 目录名：全局库（skill/_common/_corrections）+ 技能私有库（skill/<技能>/_corrections）
CORR_DIR_NAME = "_corrections"

# 加载缓存：{目录元组: (指纹, {name: handler}, [问题])}。目录 mtime 变了就重新加载。
_CACHE = {}
_BASE_REF = {"mod": None}     # 最近一次加载成功的 base 模块（供无 base.py 的私有目录复用）


# =============================================================================
# 目录发现
# =============================================================================
def correction_dirs(cfg):
    """纠错 handler 搜索目录：[(绝对路径, owner 技能名或 None)]，靠前优先。

    顺序：① 各技能搜索路径下的公共池 _common/_corrections（全局库）
          ② 每个已发现技能的私有 <技能目录>/_corrections（只对该技能生效）
    同名 handler 先命中者生效（全局库在前），避免某技能偷偷改掉全局纠错行为。"""
    from tfpkg import skill_search_dirs
    out, seen = [], set()

    def _add(d, owner):
        rd = os.path.realpath(d)
        if rd in seen or not os.path.isdir(rd):
            return
        seen.add(rd)
        out.append((rd, owner))

    for base in skill_search_dirs(cfg or {}):
        _add(os.path.join(base, "_common", CORR_DIR_NAME), None)
    for key, skel in sorted(((cfg or {}).get("_skills") or {}).items()):
        sd = (skel or {}).get("skill_dir")
        if sd:
            _add(os.path.join(sd, CORR_DIR_NAME), key)
    return out


def _dir_fingerprint(d):
    """目录指纹：目录 mtime + 每个 .py 的 mtime/size（任一改动就重新加载）。"""
    try:
        st = os.stat(d)
        fp = [int(st.st_mtime_ns)]
    except OSError:
        return None
    try:
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".py"):
                continue
            try:
                s2 = os.stat(os.path.join(d, fn))
                fp.append((fn, int(s2.st_mtime_ns), s2.st_size))
            except OSError:
                pass
    except OSError:
        return None
    return tuple(fp)


# =============================================================================
# 加载
# =============================================================================
def _pkg_name(d):
    return "_tf_corrections_" + hashlib.md5(d.encode("utf-8")).hexdigest()[:8]


def _ensure_pkg(d):
    """把目录注册成命名空间包，返回包名。已注册则复用。"""
    name = _pkg_name(d)
    if name in sys.modules:
        return name
    spec = importlib.machinery.ModuleSpec(name, None, is_package=True)
    spec.submodule_search_locations = [d]
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    return name


def _load_dir(d, verbose=False):
    """加载一个 _corrections 目录，返回 (base 模块或 None, {name: handler}, [问题])。"""
    issues, handlers = [], {}
    pkg = _ensure_pkg(d)
    base_mod = None
    base_py = os.path.join(d, "base.py")
    if os.path.isfile(base_py):
        try:
            base_mod = importlib.import_module(pkg + ".base")
            _BASE_REF["mod"] = base_mod
        except Exception as e:
            issues.append("base.py 加载失败：%s" % e)
    elif _BASE_REF["mod"] is not None:
        # 技能私有目录没带 base.py：把已加载的 base 挂成它的子模块，
        # 这样 handler 里的相对导入（from .base import ...）照样能解析。
        sys.modules[pkg + ".base"] = _BASE_REF["mod"]
        base_mod = _BASE_REF["mod"]
    if base_mod is None:
        return None, {}, ["找不到 handler 基类 base.py（公共池 skill/_common/_corrections/base.py）"]
    try:
        names = sorted(os.listdir(d))
    except OSError as e:
        return base_mod, {}, ["目录不可读：%s" % e]
    for fn in names:
        if not fn.endswith(".py") or fn.startswith("_"):
            continue
        stem = fn[:-3]
        try:
            mod = importlib.import_module(pkg + "." + stem)
        except Exception as e:
            issues.append("%s 加载失败：%s" % (fn, e))
            continue
        found = []
        one = getattr(mod, "HANDLER", None)
        if one is not None and getattr(one, "name", ""):
            found.append(one)
        else:
            for _nm, obj in vars(mod).items():
                if (isinstance(obj, type) and issubclass(obj, base_mod.CorrectionHandler)
                        and obj is not base_mod.CorrectionHandler
                        and getattr(obj, "__module__", None) == mod.__name__):
                    found.append(obj())
        if not found:
            if verbose:
                issues.append("%s 里没有 HANDLER（跳过）" % fn)
            continue
        for h in found:
            nm = str(getattr(h, "name", "") or "").strip()
            if not nm:
                issues.append("%s 的 handler 缺 name（跳过）" % fn)
                continue
            if nm in handlers:
                issues.append("同名 handler '%s'（%s 覆盖前一个）" % (nm, fn))
            handlers[nm] = h
    return base_mod, handlers, issues


def load_correction_handlers(cfg, verbose=False):
    """加载全部纠错 handler，返回 {name: handler}（带缓存；目录改动自动失效）。

    handler 上的 owner 属性会被填成"技能私有库"的技能名（全局库为 None）。"""
    dirs = correction_dirs(cfg or {})
    key = tuple(d for d, _o in dirs)
    fp = tuple((d, _dir_fingerprint(d)) for d, _o in dirs)
    hit = _CACHE.get(key)
    if hit and hit[0] == fp:
        return hit[1]
    out, all_issues = {}, []
    for d, owner in dirs:
        base_mod, handlers, issues = _load_dir(d, verbose=verbose)
        all_issues += ["%s: %s" % (d, i) for i in issues]
        for nm, h in handlers.items():
            if nm in out:
                continue
            h.owner = owner
            out[nm] = h
    _CACHE[key] = (fp, out, all_issues)
    if verbose:
        for i in all_issues:
            sys.stderr.write("警告（纠错库）：%s\n" % i)
    return out


def correction_handler_names(cfg):
    """已注册 handler 的名字列表（tf schema 校验 corrections 引用时用）。"""
    return sorted(load_correction_handlers(cfg or {}).keys())


def correction_issues(cfg):
    """加载期产生的问题（供 tf schema 打印）。"""
    dirs = correction_dirs(cfg or {})
    key = tuple(d for d, _o in dirs)
    hit = _CACHE.get(key)
    return list(hit[2]) if hit else []


# =============================================================================
# 上下文构造 + 匹配
# =============================================================================
class _FallbackContext(object):
    """纠错库缺失时的兜底上下文（字段与 base.CorrectionContext 一致）。"""

    def __init__(self, **kw):
        self.material = kw.get("material")
        self.skill = kw.get("skill")
        self.step = kw.get("step")
        self.label = kw.get("label")
        self.workdir = kw.get("workdir")
        self.diag = kw.get("diag") or ""
        self.diag_code = kw.get("diag_code") or ""
        self.text = kw.get("text") or ""
        self.host = kw.get("host")
        self.job_id = kw.get("job_id")
        self.extra = kw.get("extra") or {}

    def haystack(self):
        return " ".join([self.diag, self.diag_code, self.text]).lower()


def make_ctx(cfg, **kw):
    """构造纠错上下文（优先用 handler 库里的类，库不在就兜底）。"""
    base_mod = _BASE_REF["mod"]
    if base_mod is None:
        try:
            load_correction_handlers(cfg or {})
            base_mod = _BASE_REF["mod"]
        except Exception:
            base_mod = None
    if base_mod is not None and hasattr(base_mod, "make_context"):
        return base_mod.make_context(**kw)
    return _FallbackContext(**kw)


def _score(h, ctx):
    """命中强度：matches 命中的条数（越具体越高）。0 = 不命中。"""
    if not getattr(h, "enabled", True):
        return 0
    try:
        if not h.applies_to(ctx):
            return 0
    except Exception:
        return 0
    hay = ctx.haystack()
    hits = sum(1 for m in (getattr(h, "matches", ()) or ())
               if str(m).lower() in hay)
    if hits == 0:
        return 0
    try:                        # 子类覆盖了 match()：尊重它的判定
        if not h.match(ctx):
            return 0
    except Exception:
        return 0
    return hits


def match_corrections(cfg, ctx, skill=None):
    """诊断上下文 → 命中的 handler 列表 [(handler, score)]，分数高的在前。

    skill 参数给出时：全局 handler + 该技能私有的 handler 都算；其他技能的私有
    handler 不参与（避免 A 技能的纠错规则误伤 B 技能）。"""
    handlers = load_correction_handlers(cfg or {})
    out = []
    for _nm, h in handlers.items():
        owner = getattr(h, "owner", None)
        if owner and owner != skill:
            continue
        sc = _score(h, ctx)
        if sc:
            out.append((h, sc))
    out.sort(key=lambda x: (-x[1], str(getattr(x[0], "name", ""))))
    return out


def suggest_for_diag(cfg, **ctx_kw):
    """给一段诊断直接拿到建议（tf diagnose 用）：返回 [Suggestion.as_dict(), ...]。"""
    ctx = make_ctx(cfg, **ctx_kw)
    res = []
    for h, _sc in match_corrections(cfg, ctx, skill=ctx_kw.get("skill")):
        try:
            sug = h.suggest(ctx)
        except Exception as e:
            res.append({"handler": getattr(h, "name", "?"),
                        "title": getattr(h, "title", ""),
                        "risk": getattr(h, "risk", "review"),
                        "reason": "handler 报错：%s" % e, "actions": [],
                        "commands": [], "auto": False, "notes": ""})
            continue
        d = sug.as_dict() if hasattr(sug, "as_dict") else dict(sug or {})
        try:
            d["apply_available"] = bool(h.can_apply())
        except Exception:
            d["apply_available"] = False
        res.append(d)
    return res


# =============================================================================
# tf correct —— 列出/执行纠错 handler（永不提交作业、永不删目录）
# =============================================================================
def cmd_correct(cfg, data, proj, job=None, yes=False, dry=False):
    """tf correct -p MAT [-j STEP]：把步骤的诊断喂给 handler 库。

    默认只打印命中情况 + 建议命令（只读）；加 -y 才执行 handler.apply()
    （只允许改输入文件；作业在跑时拒绝执行）。返回 0/1。"""
    from tfpkg import find_material, find_step, _add_diag_codes
    _add_diag_codes(data)
    t, m = find_material(data, proj)
    if m is None:
        print("错误：找不到材料 %s。" % proj)
        return 1
    if job:
        steps = [find_step(m, job)]
    else:
        steps = [s for s in m.get("steps", []) if str(s.get("kind")) == "FAIL"]
        if not steps:
            print("%s：没有 FAIL 步骤（tf -p %s status 看全貌）。" % (m.get("name"), proj))
            return 0
    handlers = load_correction_handlers(cfg)
    skill = t.get("key")
    print("材料 %s（技能 %s，集群 %s）—— 纠错库 %d 个 handler"
          % (m.get("name"), skill, m.get("host_eff") or "-", len(handlers)))
    n_hit, n_applied = 0, 0
    for s in steps:
        label = s.get("label") or s.get("name")
        jobinfo = s.get("job") or {}
        running = str(s.get("kind")) in ("R", "PD") or bool(jobinfo)
        ctx = make_ctx(cfg, material=m.get("name"), skill=skill,
                       step=s.get("name"), label=s.get("label"),
                       workdir=s.get("dir"), diag=s.get("diag") or "",
                       diag_code=s.get("diag_code") or "",
                       host=m.get("host_eff"), job_id=jobinfo.get("id"),
                       extra={"kind": s.get("kind"), "template": s.get("template")})
        print("")
        print("步骤 %-14s 状态 %-6s 诊断 %s"
              % (label, s.get("kind"), (s.get("diag") or "-")[:60]))
        hits = match_corrections(cfg, ctx, skill=skill)
        if not hits:
            print("  · 无匹配 handler —— 按 diag 人工判断（或照 _corrections/README.md 写一个）")
            continue
        n_hit += len(hits)
        for h, _score_v in hits:
            try:
                sug = h.suggest(ctx)
            except Exception as e:
                print("  · %-16s handler 报错：%s" % (getattr(h, "name", "?"), e))
                continue
            print("  · %-16s [%s] %s" % (sug.handler, sug.risk, sug.title))
            if sug.reason:
                print("      判据: %s" % sug.reason)
            for a in sug.actions:
                print("      - %s" % a)
            for c in sug.commands:
                print("      $ %s" % c)
            if sug.notes:
                print("      注意: %s" % sug.notes)
            if not yes or dry:
                continue
            # ---- 用户显式 -y：执行 handler.apply（只改输入文件） ----
            if not h.can_apply():
                print("      （该 handler 只给建议，没有可执行的 apply）")
                continue
            if running:
                print("      ✗ 拒绝执行 apply：该步骤有作业在跑（job=%s）。"
                      "先 tf stop 再改输入。" % (jobinfo.get("id") or "?"))
                continue
            try:
                rc, changed = h.apply(ctx, cfg)
            except Exception as e:
                print("      ✗ apply 失败：%s" % e)
                continue
            if rc == 0 and changed:
                n_applied += 1
                print("      ✓ 已改远端输入（自动备份 INCAR）：%s" % ", ".join(changed))
                print("      下一步：%s   然后  %s" % (h.retry_cmd(ctx), h.start_cmd(ctx)))
            elif rc == 0:
                print("      · 远端无需改动（参数已是该状态）")
            else:
                print("      ✗ apply 返回码 %s（远端可能没改成功，看上面输出）" % rc)
    print("")
    if not yes:
        print("（只读模式：命中 %d 条建议。要执行 handler.apply 加 -y；"
              "apply 只改输入文件、不提交作业。）" % n_hit)
    elif not n_applied:
        print("（-y 已给，但没有可执行的改动——见上面每条的说明。）")
    else:
        print("（已执行 %d 处输入修正。提交仍要你自己敲 tf ... start。）" % n_applied)
    return 0


def cmd_correct_usage():
    return ("用法: tf correct -p <材料> [-j <步骤>] [-y]\n"
            "  只读列出命中的纠错 handler 与建议命令；-y 才执行 handler.apply。\n"
            "  ★ 永不提交作业、永不删目录：提交走 tf start，重生成走 tf retry/rerun。")
