#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_step4_wave.py —— amset wave → wavefunction.h5（step4_wave）。

amset wave 需要 uniform 步的 vasprun.xml 和 WAVECAR。WAVECAR 动辄几十 GB，
不复制，改用软链。提交脚本在计算节点 amset_clean 环境里跑 amset wave。
产出目录：step4_wave/，产物 wavefunction.h5
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stepconf  # noqa: E402
# 本步不跑 VASP，只软链 + 填提交脚本，不需要 ke_common 的 VASPKIT 部分
# 但复用它的 jobname/submit 工具
try:
    import ke_common as kc
    _HAS_KC = True
except Exception:
    _HAS_KC = False

# =========================== 可改参数区 ===========================
OUTDIR_NAME = "step4_wave"
UNIFORM_DIR = "step3_uniform"
STEP_LABEL  = "S4_wave"
# amset wave 命令（--planewave-cutoff 可按体系加大；见 amset 文档）
AMSET_CMD   = "amset wave >> amset.log 2>&1 && ls -l wavefunction.h5"
LINK_FILES  = ["vasprun.xml", "WAVECAR"]   # 从 uniform 步软链过来
# =================================================================


def main():
    cwd = Path.cwd()
    out = cwd / OUTDIR_NAME
    out.mkdir(exist_ok=True)
    uni = cwd / UNIFORM_DIR

    for f in LINK_FILES:
        src = uni / f
        if not src.is_file():
            sys.exit("[ERROR] %s 缺失（uniform 步没跑完？）：%s" % (f, src))
        dst = out / f
        if dst.is_symlink() or dst.exists():
            dst.unlink()
        # 相对软链，超算换挂载点也不断
        rel = os.path.relpath(src, out)
        dst.symlink_to(rel)
        print("[OK] 软链 %s -> %s" % (f, rel))

    # tf 把 submit_amset.tpl 与本脚本一起推到 gen 运行目录，但按原名推、不会改成
    # submit.sh；本步自己把它渲染成 out/submit.sh（维度步靠各自 gen 的 render，这里同理）。
    here = Path(__file__).resolve().parent
    tpl = next((p for p in (here / "submit_amset.tpl", cwd / "submit_amset.tpl")
                if p.is_file()), None)
    if tpl is None:
        sys.exit("[ERROR] 找不到 submit_amset.tpl（gen_need 里要有它，且应随 gen 脚本一起推送）")
    submit = out / "submit.sh"
    text = tpl.read_text(encoding="utf-8")
    jobname = ("%s-ke-dft-cpu-%s" % (cwd.name, STEP_LABEL)) if not _HAS_KC \
        else kc.new_jobname(cwd, STEP_LABEL)
    text = text.replace("{{JOBNAME}}", jobname).replace("{{AMSET_CMD}}", AMSET_CMD)
    submit.write_text(text, encoding="utf-8", newline="\n")
    stepconf.apply_submit(submit, stepconf.read_submit(stepconf.CONF_NAME))
    print("[OK] submit.sh 填好 amset 命令")
    print("[DONE] %s：软链就绪，提交后 amset wave 产出 wavefunction.h5" % OUTDIR_NAME)


if __name__ == "__main__":
    main()
