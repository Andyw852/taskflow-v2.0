#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
step4_check_and_resubmit.py
===========================
step4_HSE_band（HSE06+SOC 能带，vasp_ncl）收敛检查 + 自动重投。

判定：电子自洽达 EDIFF + VASP 正常收尾 + EIGENVAL 产出。
重投：**热重启** —— HSE 很贵、常见失败原因是墙钟超时，因此：
      - 绝不删除 WAVECAR（ISTART=1 会从当前 WAVECAR 续算，等价断点续跑）；
      - 只打包留档日志类输出后重投；
      - 若 WAVECAR 意外丢失，回退为拷贝 step3 的 WAVECAR 再重投。

约定（与 step1 脚本一致）：
    stdout 只有一行 JSON；stderr 是过程日志。
    退出码 0=converged  10=resubmitted/check-only  20=running
           30=max_restarts_exceeded  40=error

用法：
    python step4_check_and_resubmit.py                # 默认检查同级 step4_HSE_band
    python step4_check_and_resubmit.py /path/to/dir --check-only
"""

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_common import log, make_argparser, run_static_check  # noqa: E402

DEFAULT_DIR = "step4_HSE_band"
STEP3_DIR = "step3_PBE_WAVECAR"

# 本步交付物 —— 取决于用的是哪种能带方案：
#   有 KPOINTS_OPT : 自洽只在 KPOINTS 上做；路径点的本征值是自洽收敛【之后】才做的
#                    one-shot。★ VASP 不把它写进 EIGENVAL，也不写 EIGENVAL_OPT ★——
#                    唯一的纯文本出口是 vasprun.xml 的 <eigenvalues_kpoints_opt> 块。
#                    所以：EIGENVAL 存在【不代表能带算出来了】。作业若在自洽收敛前被
#                    墙钟砍掉，EIGENVAL 照样在，但 vasprun.xml 里没有那个块。
#                    -> 必须做内容级校验（见 kpoints_opt_done），否则会把跑了一半的
#                       作业误判成 converged，agent 直接往下走。
#   无 KPOINTS_OPT : 旧的零权重方案，路径点就混在 EIGENVAL 里。
def deliverables(job_dir: Path):
    if (job_dir / "KPOINTS_OPT").exists():
        return [("EIGENVAL", 100), ("vasprun.xml", 1000)]
    return [("EIGENVAL", 100)]


def kpoints_opt_done(job_dir: Path):
    """确认 vasprun.xml 里真的写出了 KPOINTS_OPT 的 one-shot 本征值。"""
    if not (job_dir / "KPOINTS_OPT").exists():
        return True, ""                       # 零权重方案，不适用
    vr = job_dir / "vasprun.xml"
    if not vr.exists():
        return False, "缺 vasprun.xml —— KPOINTS_OPT 的能带本征值只写在里面"
    # 流式扫描，别把几百 MB 的 xml 读进内存
    try:
        with open(vr, "r", errors="ignore") as f:
            for line in f:
                if "eigenvalues_kpoints_opt" in line:
                    return True, ""
    except OSError as exc:
        return False, f"读 vasprun.xml 失败: {exc}"
    return False, ("vasprun.xml 里没有 <eigenvalues_kpoints_opt> —— "
                   "自洽没收敛完，KPOINTS_OPT 的 one-shot 根本没跑")


ARCHIVE_KEEP = ["OUTCAR", "OSZICAR", "queue.out", "queue.err", "EIGENVAL"]

# 热重启的关键：清理时绝不碰 WAVECAR
PRESERVE = ["WAVECAR"]


def ensure_wavecar(job_dir: Path):
    """
    重投前确保 WAVECAR 可用于 ISTART=1 热重启：
      - step4 自己的 WAVECAR 存在且 >1MB -> 直接续算；
      - 否则尝试从同级 step3 拷贝预收敛 WAVECAR；
      - 都没有 -> 报错交人工。
    """
    wave = job_dir / "WAVECAR"
    if wave.exists() and wave.stat().st_size > 1024 * 1024:
        return True, f"将从现有 WAVECAR（{wave.stat().st_size / 1e6:.0f} MB）热重启"

    step3_wave = job_dir.parent / STEP3_DIR / "WAVECAR"
    if step3_wave.exists() and step3_wave.stat().st_size > 1024 * 1024:
        shutil.copyfile(step3_wave, wave)
        log(f"[prep] step4 WAVECAR 缺失/无效，已从 {STEP3_DIR} 重新拷贝")
        return True, "已从 step3 重新拷贝预收敛 WAVECAR"

    return False, ("WAVECAR 缺失且 step3 也没有可用的 WAVECAR，"
                   "HSE 冷启动代价极高，请先复查 step3")


def main():
    args = make_argparser(
        "step4 HSE06+SOC 能带检查 + 自动重投（WAVECAR 热重启，供多智能体调用）",
        DEFAULT_DIR,
    ).parse_args()
    run_static_check(
        args,
        step_name="step4_HSE_band",
        default_dir=DEFAULT_DIR,
        deliverables=deliverables,
        archive_keep_outputs=ARCHIVE_KEEP,
        preserve=PRESERVE,
        extra_check=kpoints_opt_done,
        pre_resubmit=ensure_wavecar,
        restart_hint=("已达最大重启次数，建议人工检查："
                      "KPOINTS 里是否还混着零权重路径点（应全部搬到 KPOINTS_OPT）/ "
                      "HFRCUT 是否为 -1 / TIME 调小(0.4->0.3) / ALGO=Damped->All / "
                      "PRECFOCK=Fast 引起振荡时改 Normal / 增大墙钟或核数"),
    )


if __name__ == "__main__":
    main()
