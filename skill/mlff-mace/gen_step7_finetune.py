#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_step7_finetune.py —— mlff-mace step7：MACE 多头微调（fanout seed-*，GPU/CPU）。

运行位置：超算登录节点，cwd = 材料目录；为 N_COMMITTEE 个 seed 各造一个 seed-<S>/
子目录（submit.sh + 引擎脚本），tf 按 fanout 各 sbatch 一次，作业里跑 mace_finetune.py。

死规则（§10）：
    - --default_dtype=float64 不许改（float32 力误差会造出假虚频）
    - 应力权重按维度自动：3D → STRESS_WEIGHT=STRESS_WEIGHT_3D(默认1.0,autoplex)/ENERGY_WEIGHT=1；2D → 0/10
    - 多头不是可选项；replay 数据（REPLAY_XYZ）必需——集群无外网，缺就报错
    - N_COMMITTEE=4，seed 固定 1,2,3,4
读：step6_dataset/gen-<K>/{train.xyz,test.xyz,e0s.json} + 基座模型 + REPLAY_XYZ
写：step7_finetune/seed-<S>/<材料>_gen<K>_seed<S>.model + finetune_summary.json
幂等：本代模型已存在的 seed 目录不再重训（引擎里有 fast-path），重跑不清已算产物。
退出码 0 成功；非 0 = [ERROR]。
"""
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mlff_common as mc
import stepconf

OUTDIR = "step7_finetune"
STEP = "step7_finetune"


def main():
    cwd = Path.cwd()
    out = cwd / OUTDIR
    out.mkdir(exist_ok=True)
    spec = dict(mc.SHARED_PARAM_SPEC)
    spec.update({"STEP": (STEP, "str")})
    conf = stepconf.load(spec, STEP)
    cv = dict(conf.params)

    ds_sum = mc.read_json(cwd / "step6_dataset" / "dataset_summary.json")
    if not ds_sum:
        sys.exit("[ERROR] step6_dataset/dataset_summary.json 不存在 —— 先让 S6 跑完。")
    gen = int(ds_sum["generation"])
    train = cwd / "step6_dataset" / ("gen-%d" % gen) / "train.xyz"
    test = cwd / "step6_dataset" / ("gen-%d" % gen) / "test.xyz"
    e0s = cwd / "step6_dataset" / ("gen-%d" % gen) / "e0s.json"
    for p, tag in ((train, "train.xyz"), (test, "test.xyz")):
        if not p.is_file():
            sys.exit("[ERROR] 缺 %s（%s）" % (tag, p))

    method = mc.read_kv(cwd / "step1_relax" / mc.METHOD_FILE)
    dim = (method.get("DIM") or "3d").lower()
    if dim == "2d":
        e_w, s_w = 10.0, 0.0          # §5.3：2D 面外应力是垃圾不能训，能量权重补位
    else:
        e_w, s_w = 1.0, cv["STRESS_WEIGHT_3D"] or 1.0   # autoplex：stress_weight=1.0
    if str(cv["ENERGY_WEIGHT"]).strip().lower() != "auto":
        e_w = cv["ENERGY_WEIGHT"]
    if str(cv["STRESS_WEIGHT"]).strip().lower() != "auto":
        s_w = cv["STRESS_WEIGHT"]

    # 基座模型/REPLAY 解析成绝对路径（作业 cwd=seed-*/，相对路径会找不到）
    from mace_model import resolve_model as _rm
    _kind, _val = _rm(cv["MACE_MODEL"] or "mace-mp:medium",
                      [str(cwd), cv["MACE_MODEL_DIR"] or ""])
    if _kind != "file":
        sys.exit("[ERROR] MACE_MODEL=%r 解析成了基座名（%s）。微调必须指向 .model 文件：\n"
                 "         把 .model 放进 MACE_MODEL_DIR 或写绝对路径。"
                 % (cv["MACE_MODEL"], _val))
    model = _val
    replay = cv["REPLAY_XYZ"] or ""
    if replay:
        rp = Path(replay)
        if not rp.is_absolute():
            rp = (Path.cwd() / rp)
        if not rp.is_file():
            sys.exit("[ERROR] REPLAY_XYZ=%s 不存在（相对路径按材料目录解析）。" % rp)
        replay = str(rp)
    here = Path(__file__).resolve().parent
    mat = sys.argv[1] if len(sys.argv) > 1 else cwd.parent.name
    n_seeds = int(cv["N_COMMITTEE"])
    # GPU 分卡：3090 的 fakeslurm 把每个 --gres=gpu 作业都钉在 CUDA_VISIBLE_DEVICES=0
    # （GPU 0），4 个 seed 挤一张 24G 卡 → replay 预处理 ~11.5G/seed 直接 OOM。
    # 这里按 seed 均摊到 N_GPU 张卡：seed s → GPU (s-1) % N_GPU。N_GPU=0(=auto) 时
    # 用 N_COMMITTEE 张卡。DEVICE=cpu 不设（设了也无意义）。
    dev = str(cv["DEVICE"] or "auto").strip().lower()
    gpu_mode = dev != "cpu"
    n_gpu = int(cv["N_GPU"] or 0)
    if n_gpu <= 0:
        n_gpu = n_seeds
    for s in range(1, n_seeds + 1):
        sdir = out / ("seed-%d" % s)
        sdir.mkdir(exist_ok=True)
        target = sdir / ("%s_gen%d_seed%d.model" % (mat, gen, s))
        if target.is_file():
            # 模型存在也不跳：submit.sh 必须按最新配方重写，重训与否由引擎的
            # fast-path（recipe 指纹）决定
            print("[..] seed-%d 本代模型已存在（引擎会按 recipe 指纹决定是否重训）" % s)
        gpu_export = ""
        if gpu_mode and n_gpu >= 1:
            # 无条件覆盖：fakeslurm 已把 CUDA_VISIBLE_DEVICES 钉成 0，必须显式改
            gpu_export = "export CUDA_VISIBLE_DEVICES=%d; " % ((s - 1) % n_gpu)
        cmd = (gpu_export + "python ../../mace_finetune.py "
               "--name %s_gen%d_seed%d --seed %d --gen %d "
               "--foundation '%s' --train-file '%s' --test-file '%s' "
               "--valid-fraction %g --e0s '%s' --e0s-mode %s --replay '%s' --num-samples-pt %d "
               "--energy-weight %g --forces-weight %g --stress-weight %g "
               "--lr %g --epochs %d --batch-size %d --patience %d --start-swa %d --loss %s "
               # [FIX-H4] HUBER_DELTA / USE_SWA 必须透传，否则 step.conf 里这两个键是死的
               "--huber-delta %g --use-swa %s "
               "--force-mh-ft-lr %s --multiheads-finetuning %s --device %s --dtype %s"
               % (mat, gen, s, s, gen, model, train, test,
                  cv["VALID_FRACTION"], e0s if e0s.is_file() else "", cv["E0S_MODE"] or "estimated",
                  replay, int(cv["NUM_SAMPLES_PT"] or 30000), e_w, cv["FORCES_WEIGHT"], s_w,
                  cv["LR"], cv["EPOCHS"], cv["BATCH_SIZE"],
                  cv["PATIENCE"], cv["START_SWA"], cv["LOSS"] or "huber",
                  cv["HUBER_DELTA"], str(cv["USE_SWA"]).lower(),
                  str(cv["FORCE_MH_FT_LR"]).lower(), str(cv["MULTIHEAD"]).lower(),
                  cv["DEVICE"] or "auto", cv["DTYPE"]))
        tpl = mc.resolve_submit(here, "submit_mace")
        mc.write_submit(tpl, sdir / "submit.sh",
                        {"JOBNAME": mc.new_jobname(cwd, "ft%d" % s, tag="mlff")[:80],
                         "CONDA_SH": cv["CONDA_SH"],
                         "CONDA_ENV": cv["CONDA_ENV"],
                         "MACE_CMD": cmd})
        stepconf.apply_submit(sdir / "submit.sh", conf.submit)
        # 引擎脚本直接落在材料目录（gen_need 推送），作业 cd 到 seed 目录后调用
        print("[OK] seed-%d：submit.sh 就绪（%s）" % (s, target.name))
    print("[DONE] %s：%d 个 seed（gen %d，%s，e_w=%.0f s_w=%.0f）"
          % (OUTDIR, n_seeds, gen, dim, e_w, s_w))


if __name__ == "__main__":
    main()
