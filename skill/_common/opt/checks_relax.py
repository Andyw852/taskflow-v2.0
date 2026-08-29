#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
checks_relax.py —— 公共技能池的私有判据：作业内分段弛豫（STAGE_MODE=in_job）

用法：技能的 skill.yaml 里写
    checks: ../_common/checks_relax.py
步骤里写
    {seq: 1, name: step1_PBE_opt, label: S1_opt, check: relax_injob, gen: gen_step1_PBE_opt.py}

tf 会把本文件的源码随 payload 下发到远端 exec，然后把 CHECKERS 里的判据注册进去
（见 tf 的"技能私有判据"一段）。所以本文件必须自包含：不要 import 技能里的其它模块。
可以直接用 payload 里已有的 tail_text/os/glob——下面做了兜底，缺了也能跑。

判据语义
--------
done  : OUTCAR 出现 "reached required accuracy"（收敛总闸）
        或者兼容老材料：同级 step1{a,b,c}_PBE_opt 里任意一段已收敛
fail  : 所有段都跑完了但没收敛 / 某段异常退出（.sN.started 有而 .sN.done 无）
running 之外的"还没跑" -> OUTCAR missing

注意：作业内分段只有一个目录，所以 tf 看不到段间状态；进度信息从
.sN.done 标记和 OUTCAR.sN 存档里读，判据的 note 会带上"已完成 N/M 段"。
"""

import os
import glob as _glob


def _tail(path, n=200):
    fn = globals().get("tail_text")
    if fn:
        return fn(path)
    try:
        with open(path, "rb") as fh:
            try:
                fh.seek(-200000, os.SEEK_END)
            except OSError:
                fh.seek(0)
            return fh.read().decode("utf-8", "ignore")
    except OSError:
        return ""


def _conv(d):
    p = os.path.join(d, "OUTCAR")
    return os.path.isfile(p) and "reached required accuracy" in _tail(p)


def _stage_progress(d):
    """返回 (已完成段数, 计划段数, 有没有段跑了一半没完成)。"""
    planned = len(_glob.glob(os.path.join(d, "INCAR.s*_*")))
    done = len(_glob.glob(os.path.join(d, ".s*.done")))
    started = len(_glob.glob(os.path.join(d, ".s*.started")))
    return done, planned, started > done


def ck_relax_injob(d, sc):
    """作业内分段弛豫的判据（check: relax_injob）。"""
    mat = os.path.dirname(d)

    # 老材料兼容：tf 分段时代留下的 step1a/b/c，任意一段收敛即认账
    for legacy in ("step1a_PBE_opt", "step1b_PBE_opt", "step1c_PBE_opt",
                   "step1a_std_opt", "step1b_std_opt", "step1c_std_opt"):
        if _conv(os.path.join(mat, legacy)):
            return True, "旧分段 %s 已收敛，跳过" % legacy

    if _conv(d):
        done, planned, _ = _stage_progress(d)
        if planned:
            return True, "converged（%d/%d 段）" % (done, planned)
        return True, "converged"

    if not os.path.isfile(os.path.join(d, "OUTCAR")):
        return False, "OUTCAR missing"

    done, planned, half = _stage_progress(d)
    if planned and done >= planned:
        return False, ("%d 段全部跑完但未收敛 —— 看 OUTCAR.s* / OSZICAR.s* 定位是哪一段"
                       "开始震荡，调完 step.conf 后 tf retry" % planned)
    if half:
        return False, ("第 %d 段中断（有 .started 无 .done）：可能撞墙钟或被看门狗杀掉；"
                       "重投会从 CONTCAR 续跑" % (done + 1))
    return False, "已完成 %d/%d 段" % (done, planned or 1)


CHECKERS = {"relax_injob": ck_relax_injob}
