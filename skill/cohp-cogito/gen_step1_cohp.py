#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_step1_cohp.py —— S1_COHP：准备输入 + 渲染 submit.sh（由 tf 提交 Slurm 作业）

为什么走 Slurm：
  COGITO 对 9 原子胞约需 23 分钟（Wannier 轨道生成为主），而 tf 的 run:gen
  本地步远端执行上限是 `timeout 600`（10 分钟）——若在登录节点直跑必被 kill。

作业内依次执行：
  COGITO        自适应原子轨道基 + 紧束缚模型
  COGITOanalyze 投影质量检查（charge spilling / orbital mixing）
  COGITOpost    ICOHP / ICOBI / COHP 曲线

产物标记：bond_info.txt（COGITOpost 的键表）
"""
from __future__ import annotations

import json
import os
import sys
import shutil
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import cogito_common as cc  # noqa: E402

try:
    import stepconf  # noqa: E402
except Exception:  # pragma: no cover
    stepconf = None

TPL_NAME = "submit_cohp.tpl"


def build_cmd(num_outer: int, densify: str | None) -> str:
    """作业内执行的三段命令。任一段失败即整体失败（&& 串联）。

    ★ 必须用 './' 而不是 '.'：COGITO 的 COGITO_TB_Model.read_input 用字符串
      拼接构造路径（filename = self.directory + file），传 '.' 会拼出
      '.tb_input.txt' 而非 './tb_input.txt'；其 switching to untagged 回退
      分支同样拼接，因此仍然 FileNotFoundError。COGITOanalyze 用同一模式，
      会误报 orb_converg_info / error_output / DFT_band_error analysis failed。
      实测：作业 3832699 中 COGITO 主计算 185 s 成功，COGITOpost 因该 bug 失败。
    """
    o = f" --num_outer {num_outer}" if num_outer != 4 else ""
    d = f" --densify {densify}" if densify else ""
    return (
        f"echo '=== [1/3] COGITO ==='\n"
        f"COGITO --dir ./{o} > cogito_run.log 2>&1 || {{ tail -30 cogito_run.log; exit 1; }}\n"
        f"echo '=== [2/3] COGITOanalyze ==='\n"
        f"COGITOanalyze --dir ./ > analyze.log 2>&1 || {{ tail -30 analyze.log; exit 1; }}\n"
        f"echo '=== [3/3] COGITOpost ==='\n"
        f"COGITOpost --dir ./{d} > post.log 2>&1 || {{ tail -30 post.log; exit 1; }}\n"
        f"echo '=== all stages done ==='"
    )


OUT_DIR = "step1_cohp"   # run:gen 步骤在技能目录下运行，产物写入本子目录


def _tf_bin() -> str:
    """找"正在跑我的那个 tf"：TF_BIN 环境变量 > 本仓库 bin/tf > PATH 里的 tf。

    不要直接写 "tf"：① 副本树常以 python3 bin/tf 调用，PATH 里可能没有 tf；
    ② PATH 里的 tf 可能指向**另一棵树**（如 ~/.local/bin/tf → 生产树），
    一调用就会拿错配置去提交——这里必须优先用同一个仓库里的入口。
    """
    env = os.environ.get("TF_BIN")
    if env and os.path.isfile(env):
        return env
    here = Path(__file__).resolve().parent          # skill/cohp-cogito/
    for up in (here.parent.parent, here.parent, here):   # 仓库根 → 技能目录
        cand = up / "bin" / "tf"
        if cand.is_file():
            return str(cand)
    return shutil.which("tf") or "tf"


def main() -> int:
    root = Path(os.getcwd())
    dest = root / OUT_DIR
    dest.mkdir(parents=True, exist_ok=True)
    params = cc.load_stepconf_params(stepconf)

    num_outer = int(params.get("NUM_OUTER", 4) or 4)
    densify = (params.get("DENSIFY") or "").strip() or None

    # 1) 确保 band-dft-cpu 上游已启动或完成
    try:
        step3 = cc.find_upstream_step3(dest)
    except SystemExit:
        material = root.parent.name if root.name == "cohp-cogito" else root.name
        print(f"[..] 未找到合格 step3_PBE_WAVECAR，自动启动 band-dft-cpu：{material}")
        tfbin = _tf_bin()
        try:
            proc = subprocess.run([tfbin, "-tt", "band-dft-cpu", "-p", material, "start"], text=True, capture_output=True)
        except OSError as exc:
            # 集群登录节点上通常没有 tf（tf 跑在你本机），"自动拉起上游"这条分支
            # 只在 local 模式或集群装了 tf 时才有意义 —— 这里给出可直接照抄的命令，
            # 而不是抛一个看不懂的 [Errno 2]。
            raise SystemExit(
                f"[等待] 上游 band-dft-cpu 还没算出 step3_PBE_WAVECAR，本步无法继续。\n"
                f"       本机没有可调用的 tf（{tfbin}: {exc}），请在**本地**跑：\n"
                f"           tf -tt band-dft-cpu -p {material} start\n"
                f"       等它算出 step3_PBE_WAVECAR，再重跑本步：\n"
                f"           tf -tt cohp-cogito -p {material} -j S1_COHP retry")
        if proc.stdout: print(proc.stdout.rstrip())
        if proc.returncode != 0:
            if proc.stderr: print(proc.stderr.rstrip(), file=sys.stderr)
            raise SystemExit("[等待] band-dft-cpu 未能启动；请先检查其输出")
        raise SystemExit("[等待] band-dft-cpu 已启动/推进，完成 step3_PBE_WAVECAR 后再次运行 cohp-cogito")
    print(f"[..] 上游 step3：{step3}")

    # 2) COGITO 前置条件校验
    info = cc.preflight_check(step3)
    print("[..] 前置检查通过：NSW={NSW} ISYM={ISYM} NBANDS={NBANDS} "
          "NIONS={NIONS} LWAVE={LWAVE}".format(**info))

    # 3) 硬拷贝 5 个 VASP 文件
    copied = cc.stage_inputs(step3, dest)
    print(f"[..] 已拷贝 {len(copied)} 个文件：{', '.join(copied)}")

    # 4) 渲染 submit.sh
    tpl = next((p for p in (HERE / TPL_NAME, root / TPL_NAME, dest / TPL_NAME)
                if p.is_file()), None)
    if tpl is None:
        print(f"[错误] 找不到 {TPL_NAME}（gen_need 里要有它）", file=sys.stderr)
        return 1
    material_name = root.parent.name if root.name == "cohp-cogito" else root.name
    jobname = f"{material_name}-cohp-cogito-S1_COHP"
    text = (tpl.read_text(encoding="utf-8")
            .replace("{{JOBNAME}}", jobname)
            .replace("{{COHP_CMD}}", build_cmd(num_outer, densify)))
    submit = dest / "submit.sh"
    submit.write_text(text, encoding="utf-8", newline="\n")
    submit.chmod(0o755)
    print(f"[..] 已渲染 {submit}（jobname={jobname}）")

    (dest / "cogito_input.json").write_text(
        json.dumps({"upstream_step3": str(step3), "vasp_files": copied,
                    "preflight": info, "num_outer": num_outer,
                    "densify": densify, "jobname": jobname},
                   indent=2, ensure_ascii=False), encoding="utf-8")

    print("[DONE] 输入就绪，待 tf 提交（产物 bond_info.txt）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
