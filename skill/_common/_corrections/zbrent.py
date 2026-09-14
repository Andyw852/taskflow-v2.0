# -*- coding: utf-8 -*-
"""zbrent —— 电子步不收敛（ZBRENT / EDDAV / SCF 空转）纠错 handler。

典型现场（见 AGENTS.md 第五节决策表）：
  · slurm-*.out 里出现 ZBRENT: fatal error in subroutine
  · OUTCAR 尾部电子步反复 EDDAV/DAV，能量/电荷振荡不降
  · tf 结构化诊断 relax_electronic / relax_oscillating

处理路线（由轻到重，绝不直接删目录）：
  1. retry（opt 步自动 cp CONTCAR 续算）——多数一轮就过；
  2. 同一材料连续失败 ≥2 次：apply() 给远端 INCAR 打补丁
     （AMIX=0.1 / BMIX=0.0001 → ALGO=All → NELM≥200，写前备份 INCAR.bak.<ts>）；
  3. 仍不过：人工看 MAGMOM / ISMEAR / 结构是否合理。

apply() 复用 tf 挂死恢复里的同一份 INCAR 升级实现（tfpkg._hung_incar_fix），
保证"手动纠错"和"自动恢复"改的是同一套参数，不会两套逻辑打架。
"""
from .base import CorrectionHandler, Suggestion


class ZbrentCorrection(CorrectionHandler):
    name = "zbrent"
    title = "电子步不收敛（ZBRENT / EDDAV / SCF 空转）"
    # 小写子串匹配：诊断文本 / diag_code / 日志素材任一命中即算命中。
    matches = ("zbrent", "eddav", "电子步", "scf 收敛困难",
               "relax_electronic", "relax_oscillating")
    steps = ("*",)          # 任何含电子步的步骤都可能撞上
    risk = "review"         # apply 会改远端 INCAR，必须人确认
    incar_level = 2         # 1 = 只补 AMIX/BMIX；2 = 再 ALGO=All + NELM>=200

    def suggest(self, ctx):
        return Suggestion(
            handler=self.name, title=self.title, risk=self.risk,
            reason="诊断命中电子步不收敛特征（ZBRENT/EDDAV/SCF 空转）："
                   "电荷混合失败或迭代空转，不是结构本身算错。",
            actions=[
                "第 1 步：retry 续算（opt 步会自动 cp CONTCAR POSCAR，不丢已有结果）",
                "第 2 步：同一材料连续 2 次仍 FAIL，再用 tf correct -y 打 INCAR 补丁"
                "（AMIX=0.1 / BMIX=0.0001 → ALGO=All → NELM≥200，自动备份 INCAR）",
                "第 3 步：仍不收敛 → 人工核 MAGMOM（磁性体系初值）/ ISMEAR / 结构合理性",
            ],
            commands=[self.retry_cmd(ctx),
                      (self.retry_cmd(ctx).rsplit(" ", 1)[0] + " correct -y")
                      if ctx.material else "tf ... correct -y",
                      self.start_cmd(ctx)],
            notes="若该技能已开 hang_check，SCF 空转类挂死 tf 会自动做同样的 INCAR 升级，"
                  "先看 monitor 日志 / .tf_hung.json 是否已处理过，避免重复改。")

    def apply(self, ctx, cfg=None):
        if not ctx.workdir:
            return 1, []
        try:
            from tfpkg import _hung_incar_fix
        except Exception as e:            # 独立运行（不经 tf）时的兜底
            raise RuntimeError("apply 需要 tf 运行环境（tfpkg._hung_incar_fix 不可用：%s）" % e)
        return _hung_incar_fix(cfg, ctx.workdir, int(self.incar_level))


HANDLER = ZbrentCorrection()
