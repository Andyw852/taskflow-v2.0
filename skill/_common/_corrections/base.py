# -*- coding: utf-8 -*-
"""_corrections/base.py —— 纠错 handler 基类 + 上下文 + 建议对象（技能可照抄的模板）。

一个 correction handler 只回答一个问题：
    「诊断文本长成这样时，该动什么？」
它**不提交作业、不监控、不删目录**——只产出两样东西：
    suggest(ctx)      纯建议（人/AI 看的：为什么、跑哪条命令）——必须实现
    apply(ctx, cfg)   可选的输入修正（如远端 INCAR 打补丁）——默认没有

写一个新 handler（照 zbrent.py 复制改 20 分钟）：
  1. 在本目录新建 <名字>.py（名字= handler 的 name）；
  2. class XxxCorrection(CorrectionHandler)：填 name/title/matches/steps/risk；
  3. 实现 suggest(ctx) -> Suggestion（必须），可选 apply(ctx, cfg) -> (rc, changed 列表)；
  4. 文件末尾写 HANDLER = XxxCorrection()；
  5. 想让某个技能"自己声明会用这个纠错"，在该技能 skill.yaml 里写
     corrections: [<名字>]（不写也能被全局兜底命中——声明只是为了 tf schema 里显示出来）。

★ 纪律：apply() 只允许**改输入文件**（写前必须备份 + 原子替换），
   绝不允许自己 sbatch/scancel/rm——提交与取消永远只走 tf（见 AGENTS.md 铁律 1）。
"""

# 风险等级：safe = 只是重交/续算（不改输入，非破坏）
#           review = 要改输入（改 INCAR/模板），需要人确认
#           destructive = 要删/重生成目录 —— 这类 handler 只允许给建议，不许自动 apply
RISK_ORDER = ("safe", "review", "destructive")


class CorrectionContext(object):
    """一次诊断的上下文（由 tf 填好交给 handler，handler 只读不改）。"""

    __slots__ = ("material", "skill", "step", "label", "workdir", "diag",
                 "diag_code", "text", "host", "job_id", "extra")

    def __init__(self, material=None, skill=None, step=None, label=None,
                 workdir=None, diag="", diag_code="", text="", host=None,
                 job_id=None, extra=None):
        self.material = material          # 材料名（如 C24/qHPC24）
        self.skill = skill                # 技能 key（如 opt-dft-cpu）；没有就 None
        self.step = step                  # 步骤名（如 step1_PBE_opt）
        self.label = label                # 步骤 label（如 S1_opt）
        self.workdir = workdir            # 远端步骤目录（apply 改 INCAR 用）
        self.diag = diag or ""            # tf 采集到的诊断文本
        self.diag_code = diag_code or ""  # 结构化错误码（如 relax_electronic）
        self.text = text or ""            # 额外素材（日志尾部等），可空
        self.host = host                  # 该材料用的集群（ssh 别名）
        self.job_id = job_id              # 当前/上次作业号，可空
        self.extra = extra or {}          # 逃生口：调用方想多塞什么都行

    def haystack(self):
        """匹配用的全文（小写）：诊断 + 错误码 + 日志素材。"""
        return " ".join([self.diag or "", self.diag_code or "",
                         self.text or ""]).lower()

    def __repr__(self):
        return "<CorrectionContext %s %s diag=%r>" % (
            self.material, self.step, (self.diag or "")[:40])


class Suggestion(object):
    """一条纠错建议（handler.suggest 的返回值）。"""

    __slots__ = ("handler", "title", "risk", "reason", "actions", "commands",
                 "auto", "notes")

    def __init__(self, handler=None, title="", risk="review", reason="",
                 actions=None, commands=None, auto=False, notes=""):
        self.handler = handler or ""
        self.title = title or ""
        self.risk = risk if risk in RISK_ORDER else "review"
        self.reason = reason or ""            # 为什么这么判
        self.actions = list(actions or [])    # 人类可读的动作清单
        self.commands = list(commands or [])  # 可直接照抄的 tf 命令
        self.auto = bool(auto)                # 是否允许无人值守执行（保守：默认 False）
        self.notes = notes or ""

    def as_dict(self):
        return {"handler": self.handler, "title": self.title, "risk": self.risk,
                "reason": self.reason, "actions": self.actions,
                "commands": self.commands, "auto": self.auto,
                "notes": self.notes}


class CorrectionHandler(object):
    """纠错 handler 基类。子类只需改类属性 + 实现 suggest()。"""

    name = ""              # ★ 唯一名（skill.yaml 的 corrections: [...] 引用它）
    title = ""             # 一句话说明（tf correct / tf diagnose 会打印）
    matches = ()           # 小写子串元组：命中 diag/diag_code/日志素材任一即算命中
    steps = ("*",)         # 适用步骤：label/name 前缀；"*" = 全部步骤
    risk = "review"        # safe | review | destructive
    owner = None           # 由加载器填：技能私有目录里的 handler 只对该技能生效
    enabled = True

    # ---- 匹配 ---------------------------------------------------------------
    def applies_to(self, ctx):
        """步骤是否适用：steps 里任一项是 "*"、或是 ctx.label/ctx.step 的前缀。"""
        for s in (self.steps or ("*",)):
            s = str(s)
            if s == "*":
                return True
            for v in (getattr(ctx, "label", None), getattr(ctx, "step", None)):
                if v and (str(v).startswith(s) or s.startswith(str(v))):
                    return True
        return False

    def match(self, ctx):
        """默认匹配：matches 里任一子串出现在 haystack 里，且步骤适用。
        子类可覆盖成更精细的判定（如要求日志里同时出现两个特征）。"""
        if not self.applies_to(ctx):
            return False
        if not self.matches:
            return False
        hay = ctx.haystack()
        return any(str(m).lower() in hay for m in self.matches)

    # ---- 建议（必须实现）-----------------------------------------------------
    def suggest(self, ctx):
        raise NotImplementedError("%s 没实现 suggest()" % (self.name or type(self).__name__))

    # ---- 可选：改输入 ---------------------------------------------------------
    def can_apply(self):
        """是否提供了可执行的 apply（默认没有 → tf correct 只打印建议）。"""
        return type(self).apply is not CorrectionHandler.apply

    def apply(self, ctx, cfg=None):
        """改远端输入（如 INCAR 打补丁）。返回 (rc, changed 列表)。
        默认没实现——tf correct 会提示"该 handler 只能给建议"。"""
        raise NotImplementedError

    # ---- 便利工具（子类里直接用）----------------------------------------------
    def retry_cmd(self, ctx):
        """该步骤的标准 retry 命令（opt 步 retry 会自动 cp CONTCAR 续算）。"""
        bits = ["tf"]
        if ctx.skill:
            bits += ["-tt", str(ctx.skill)]
        if ctx.material:
            bits += ["-p", str(ctx.material)]
        if ctx.step or ctx.label:
            bits += ["-j", str(ctx.label or ctx.step)]
        bits.append("retry")
        return " ".join(bits)

    def start_cmd(self, ctx):
        bits = ["tf"]
        if ctx.skill:
            bits += ["-tt", str(ctx.skill)]
        if ctx.material:
            bits += ["-p", str(ctx.material)]
        if ctx.step or ctx.label:
            bits += ["-j", str(ctx.label or ctx.step)]
        bits.append("start")
        return " ".join(bits)


def make_context(**kw):
    """工厂：tf 侧统一用它构造上下文（避免 tf 直接依赖本模块的类）。"""
    return CorrectionContext(**kw)
