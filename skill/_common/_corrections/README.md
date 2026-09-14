# _corrections/ —— 纠错 handler 库

> taskflow-v1.0 建议 1.1。目标：**把"这个报错该怎么办"从人的脑子里搬进仓库**，
> 让新成员/新技能不必读完 AGENTS.md 决策表 + 问作者。

## 1. 它解决什么

老流程：FAIL → 人/AI 看 diag → 翻 AGENTS.md 第五节决策表 → 猜 → retry/rerun。
问题是：决策表是散文，各技能的"专属坑"散在 16 份 README 里，**加新技能时没人知道
要写什么**，也没有机器可判读的"报错→动作"映射。

handler 库把这件事变成**可枚举的代码**：

| 诊断特征 | handler | 风险 | 动作 |
|---|---|---|---|
| ZBRENT / EDDAV / SCF 空转 | `zbrent` | review | retry；连续失败则打 INCAR 补丁（AMIX/BMIX→ALGO=All→NELM≥200） |
| force not converged / relax_nsw | `nsw_continue` | safe | retry 续算（自动 cp CONTCAR） |
| NODE_FAIL / 被抢占 | `node_fail` | safe | retry 重交 |
| dir missing | `dir_missing` | destructive | 只建议；rerun 须人工执行 |

## 2. 怎么用

```bash
tf diagnose -p C24/qHPC24              # 诊断里直接带 corrections 建议（只读）
tf correct  -p C24/qHPC24 -j S1_opt    # 只打印命中的 handler + 建议命令（只读）
tf correct  -p C24/qHPC24 -j S1_opt -y # 执行 handler.apply（改远端 INCAR，自动备份）
tf schema   band-dft-cpu               # 看某技能声明了哪些 corrections
```

★ `tf correct` **永不提交作业、永不删目录**：apply 只允许改输入文件（备份 + 原子写），
提交仍走 `tf start`、重生成仍走 `tf retry/rerun`。

## 3. 怎么写一个新 handler（照 zbrent.py 复制改 20 分钟）

```python
# skill/_common/_corrections/my_case.py
from .base import CorrectionHandler, Suggestion

class MyCaseCorrection(CorrectionHandler):
    name = "my_case"                  # ★ 唯一名，skill.yaml 的 corrections: [my_case] 引用它
    title = "一句话说明"
    matches = ("特征子串1", "特征子串2")   # 小写子串，命中 diag/diag_code/日志即算命中
    steps = ("S3", "step3")            # 适用步骤前缀；("*",) = 所有步骤
    risk = "review"                    # safe | review | destructive

    def suggest(self, ctx):
        return Suggestion(
            handler=self.name, title=self.title, risk=self.risk,
            reason="为什么这么判",
            actions=["人看的动作清单"],
            commands=[self.retry_cmd(ctx), self.start_cmd(ctx)])

    # 可选：真要改输入文件才实现（写前必须备份 + 原子替换）
    # def apply(self, ctx, cfg=None): return rc, ["改了啥"]

HANDLER = MyCaseCorrection()
```

写完 `tf schema <技能>` 就能看到它（`tf diagnose` 会自动命中，不注册也行）。

## 4. 放哪 & 优先级

| 位置 | 作用范围 |
|---|---|
| `skill/_common/_corrections/` | **全局**：所有技能都能命中（推荐放这里） |
| `skill/<技能>/_corrections/` | **技能私有**：只对该技能生效，同名可覆盖全局 |

技能 `skill.yaml` 里写 `corrections: [名字, ...]` 表示"本技能声明会用到这些纠错"——
这只是**自描述**（`tf schema` 展示给人看），不写也照样能被全局兜底命中。

## 5. 纪律（写 handler 前必读）

1. `apply()` 只改输入文件，**必须**先备份再原子写（参考 `tfpkg._hung_incar_fix`）；
2. **禁止**在 handler 里 sbatch / scancel / rm / mv 目录——提交与取消永远只走 tf；
3. 风险等级别乱标：`safe` 才允许无人值守；`destructive` 只许给建议（`dir_missing` 就是范例）；
4. 判据要窄：宁可漏判（人再判断），不可误判（把正常推进的作业当成故障处理）。
