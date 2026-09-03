#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mace_finetune.py —— step7_finetune 引擎（在 venv 里跑，mace_run_train 的封装）。

cwd = step7_finetune/seed-<S>/。多头微调一个 seed：
    - float64 死规则（float32 的力误差会在声学支造出假虚频）
    - 多头必需（基座参考 DFT 与本项目泛函/色散/截断多半不同，能量基准不同）
    - replay 数据必需（集群无外网，自动下载必失败）：缺 REPLAY_XYZ 直接报错，
      绝不做 naive 微调降级（否则 model_card 结论跨代不可比）
    - BATCH_SIZE OOM 时自动降到 1（GPU 上才有意义，CPU 不影响）
输出：<材料>_gen<K>_seed<S>.model + finetune_summary.json（含训练日志尾部的 RMSE）。
退出码 0 成功；非 0 = [ERROR]。
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mlff_common as mc  # noqa: E402


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--name", required=True)          # <材料>_gen<K>_seed<S>
    p.add_argument("--seed", type=int, required=True)
    p.add_argument("--gen", type=int, required=True)
    p.add_argument("--foundation", required=True)
    p.add_argument("--train-file", required=True)
    p.add_argument("--test-file", required=True)
    p.add_argument("--valid-fraction", type=float, default=0.10)
    p.add_argument("--e0s", default="")
    p.add_argument("--e0s-mode", default="estimated")   # estimated | json
    p.add_argument("--patience", type=int, default=100)
    p.add_argument("--start-swa", type=int, default=1200)
    p.add_argument("--use-swa", default="false")
    p.add_argument("--loss", default="huber")
    p.add_argument("--huber-delta", type=float, default=0.05)
    p.add_argument("--force-mh-ft-lr", default="false")
    p.add_argument("--replay", default="")
    p.add_argument("--num-samples-pt", type=int, default=30000)
    p.add_argument("--multiheads-finetuning", default="false")   # true=多头replay / false=naive单头（默认 naive，单材料专用势）
    p.add_argument("--energy-weight", type=float, required=True)
    p.add_argument("--forces-weight", type=float, required=True)
    p.add_argument("--stress-weight", type=float, required=True)
    p.add_argument("--lr", type=float, default=1e-4)
    p.add_argument("--epochs", type=int, default=30)
    p.add_argument("--batch-size", type=int, default=4)
    p.add_argument("--device", default="cpu")
    p.add_argument("--dtype", default="float64")
    return p.parse_args()


def find_trained_model(name, cwd):
    """训练产物：优先 <name>.model，其次 *.model 里最新的。"""
    cand = list(Path(cwd).rglob("*.model"))
    direct = [c for c in cand if c.name == "%s.model" % name]
    if direct:
        return direct[0]
    if cand:
        return sorted(cand, key=lambda c: c.stat().st_mtime)[-1]
    return None


def parse_metrics(log_text):
    """从 mace 训练日志抓最后一组 RMSE/MAE（尽力而为，抓不到填 None）。"""
    out = {}
    pats = {
        "E_rmse_meV_atom": r"RMSE_E_per_atom\s*=\s*([-+0-9.eE]+)\s*meV",
        "F_rmse_meV_A": r"RMSE_F\s*=\s*([-+0-9.eE]+)\s*meV",
        "S_rmse_meV_A3": r"RMSE_stress\s*=\s*([-+0-9.eE]+)",
        "E_mae_meV_atom": r"MAE_E_per_atom\s*=\s*([-+0-9.eE]+)\s*meV",
        "F_mae_meV_A": r"MAE_F\s*=\s*([-+0-9.eE]+)\s*meV",
    }
    for k, pat in pats.items():
        m = re.findall(pat, log_text)
        if m:
            try:
                out[k] = float(m[-1])
            except ValueError:
                out[k] = None
    return out


def main():
    a = parse_args()
    cwd = Path.cwd()
    cwd.mkdir(parents=True, exist_ok=True)

    # mace 0.3.16 的 --device 没有 auto 档：这里按环境解析（venv 里有 torch）
    if str(a.device).strip().lower() == "auto":
        try:
            import torch
            a.device = "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:
            a.device = "cpu"

    if a.dtype != "float64":
        sys.exit("[ERROR] DTYPE=%s —— float64 不许改（float32 力误差 ~1e-3 eV/Å，"
                 "足以在声学支造出假虚频）。" % a.dtype)

    # 幂等 fast-path：本代模型已训好且配方指纹一致才跳过（retry 安全；
    # 配方/E0S_MODE 变了必须重训——能量零点/训练配置不同，旧模型不能混用）
    target = cwd / ("%s.model" % a.name)
    old_sum = mc.read_json(cwd / "finetune_summary.json", {})
    recipe = "%s|%g|%d|%s|ew%g|fw%g|sw%g|bs%d|hd%g|mh%s|pt%d|multi%s|npt%d" % (a.e0s_mode, a.lr, a.epochs, a.loss, a.energy_weight, a.forces_weight, a.stress_weight, a.batch_size, a.huber_delta, str(a.force_mh_ft_lr).lower(), a.patience, str(a.multiheads_finetuning).lower(), a.num_samples_pt)
    if target.is_file() and old_sum.get("e0s_mode") == a.e0s_mode \
            and old_sum.get("recipe") == recipe:
        print("[SKIP] %s 已存在（幂等跳过），不再重训" % target.name)
        print("[DONE] %s" % target.name)
        return

    # 走到这里 = 全新训练（配方/数据变了）：清掉 checkpoints 目录。--restart_latest
    # 会挑 epoch 号最大的断点续训，旧数据/旧配方的断点混在里面会被错误续上（实测）。
    import shutil as _sh
    _ck = cwd / "checkpoints"
    if _ck.is_dir():
        _sh.rmtree(_ck)
        print("[..] 清掉旧 checkpoints（配方/数据变更，禁止跨代续训）")

    # 相对路径兜底：作业 cwd=seed-*/，gen 传绝对路径；这里防手改后踩坑
    def _resolve(p):
        pp = Path(p)
        if pp.is_absolute():
            return pp
        if (cwd / pp).is_file():
            return cwd / pp
        if (cwd.parent / pp).is_file():
            return cwd.parent / pp
        return pp

    a.foundation = str(_resolve(a.foundation))
    a.train_file = str(_resolve(a.train_file))
    a.test_file = str(_resolve(a.test_file))
    if a.e0s:
        a.e0s = str(_resolve(a.e0s))
    if a.replay:
        a.replay = str(_resolve(a.replay))
    if not Path(a.foundation).is_file():
        sys.exit("[ERROR] 基座模型不存在：%s" % a.foundation)
    if not Path(a.train_file).is_file():
        sys.exit("[ERROR] 训练集不存在：%s（step6 没跑成？）" % a.train_file)
    if not Path(a.test_file).is_file():
        sys.exit("[ERROR] 测试集不存在：%s（step6 没跑成？）" % a.test_file)
    if str(a.multiheads_finetuning).lower() in ("1", "true", "yes") \
            and (not a.replay or not Path(a.replay).is_file()):
        sys.exit("[ERROR] REPLAY_XYZ 缺失：%s。\n"
                 "         多头微调（MULTIHEAD=true）的 replay 数据是必需的（集群无外网，"
                 "mace 的自动下载必失败）。naive 单头（MULTIHEAD=false）不需要 replay。\n"
                 "         获取：在有网机器跑\n"
                 "           python -m mace.cli.fine_tuning_select --configs_pt <replay数据集>"
                 " --configs_ft train.xyz --subselect fps --num_samples 30000"
                 " --model <基座> --output replay_sel.xyz\n"
                 "         再拷到超算，把路径写进 step.conf 的 REPLAY_XYZ。" % a.replay)

    cmd = [
        sys.executable, "-m", "mace.cli.run_train",
        "--name=%s" % a.name,
        "--foundation_model=%s" % a.foundation,
        "--multiheads_finetuning=%s" % str(a.multiheads_finetuning).lower(),
        "--pt_train_file=%s" % a.replay,
        "--num_samples_pt=%d" % a.num_samples_pt,
        "--train_file=%s" % a.train_file,
        "--valid_fraction=%g" % a.valid_fraction,
        "--test_file=%s" % a.test_file,
        "--energy_key=REF_energy",
        "--forces_key=REF_forces",
        "--stress_key=REF_stress",
        "--energy_weight=%g" % a.energy_weight,
        "--forces_weight=%g" % a.forces_weight,
        "--stress_weight=%g" % a.stress_weight,
        "--lr=%g" % a.lr,
        "--max_num_epochs=%d" % a.epochs,
        "--batch_size=%d" % a.batch_size,
        "--ema",
        "--ema_decay=0.99",
        "--amsgrad",
        "--patience=%d" % a.patience,
        "--loss=%s" % a.loss,
        # [FIX-H2] huber 默认 delta=0.01 eV/Å 远小于本数据的力尺度，
        # 全程落在线性段 = 对力做 L1，会推高 RMSE。显式给大一些。
        "--huber_delta=%g" % a.huber_delta,
        "--force_mh_ft_lr=%s" % str(a.force_mh_ft_lr).lower(),
        "--scaling=rms_forces_scaling",
        "--default_dtype=%s" % a.dtype,
        "--device=%s" % a.device,
        "--seed=%d" % a.seed,
        "--error_table=PerAtomRMSE",
        "--eval_interval=2",
        "--restart_latest",
    ]
    # [FIX-H3] SWA 只在 USE_SWA=true 且 START_SWA 确实能跑到时才有意义。
    # PATIENCE 早停会把训练截断在 START_SWA 之前的话，--swa 是死配置。
    if str(a.use_swa).lower() in ("1", "true", "yes"):
        if a.start_swa >= a.epochs:
            sys.exit("[ERROR] USE_SWA=true 但 START_SWA=%d >= EPOCHS=%d，"
                     "SWA 段跑不到。调小 START_SWA 或关掉 USE_SWA。"
                     % (a.start_swa, a.epochs))
        cmd += ["--swa", "--start_swa=%d" % a.start_swa]
        print("[..] SWA 已开启，start_swa=%d（注意 PATIENCE=%d 的早停"
              "可能仍会在此之前截断）" % (a.start_swa, a.patience))

    # E0s：默认 estimated（mace 用基座对训练数据的预测回归出与目标 DFT 一致的零点）。
    # 直接给 DFT 孤立原子能量（json）看似更“正确”，但基座 E0(Si)≈-7.7 eV 与目标
    # 泛函零点差 ~8 eV/atom：新头会从 8 eV/atom 的初始能量误差起步，ENERGY_WEIGHT=1
    # 的梯度根本学不动（实测 30 epoch 纹丝不动）。estimated 是 mace 官方为微调
    # 提供的零点对齐机制（源码为准）。e0s.json 仍算并写进 model_card 备查。
    if a.e0s and Path(a.e0s).is_file() and a.e0s_mode == "json":
        cmd.append("--E0s=%s" % a.e0s)
    else:
        cmd.append("--E0s=estimated")

    # [FIX-PATIENCE] mace 单卡早停不生效（上游 bug）：非分布式时 train.py 的
    # exit_now=None，patience 触发只打印日志不跳出循环，实际训到 max_num_epochs。
    # 模型取的是最佳 checkpoint（验证损失改善才保存），所以结果仍有效，只是白烧 GPU。
    try:
        import mace as _mace_mod
        _mver = getattr(_mace_mod, "__version__", "?")
    except Exception:
        _mver = "?"
    print("[WARN] mace %s 单卡早停不生效（上游 bug）：PATIENCE 只打日志不跳出，实际训到 EPOCHS=%d。模型取最佳 checkpoint，结果有效。" % (_mver, a.epochs))

    logf = Path("train.log")
    print("[..] " + " ".join(cmd[1:]))
    # [FIX-MH] 固定 replay 采样等子进程随机源：PYTHONHASHSEED + CUBLAS workspace。
    # 注意 MACE 库的 set_seeds 只设了 np+torch CPU seed、漏 cuda seed；CUBLAS workspace
    # 约束能消掉一部分 GPU 非确定性（更彻底要改库加 torch.cuda.manual_seed_all）。
    import os as _os
    _env = dict(_os.environ)
    _env["PYTHONHASHSEED"] = str(a.seed)
    _env["CUBLAS_WORKSPACE_CONFIG"] = ":4096:8"
    try:
        with open(str(logf), "w") as fh:
            r = subprocess.run(cmd, cwd=str(cwd), stdout=fh, stderr=subprocess.STDOUT, env=_env)
        rc = r.returncode
    except Exception as e:
        rc = -1
        logf.write_text("subprocess 失败：%s" % e, encoding="utf-8")
    log_text = logf.read_text(errors="ignore") if logf.is_file() else ""

    # OOM 降级（只对 GPU 有意义）：batch 降到 1 重试一次
    if rc != 0 and a.batch_size > 1 and "out of memory" in log_text.lower():
        print("[WARN] 疑似 OOM，BATCH_SIZE %d → 1 重试一次" % a.batch_size)
        cmd = [c.replace("--batch_size=%d" % a.batch_size, "--batch_size=1")
               for c in cmd]
        with open(str(logf), "a") as fh:
            r = subprocess.run(cmd, cwd=str(cwd), stdout=fh, stderr=subprocess.STDOUT)
        rc = r.returncode
        log_text = logf.read_text(errors="ignore")

    model = find_trained_model(a.name, cwd)
    if model is None:
        print(log_text[-4000:])
        sys.exit("[ERROR] 训练没产出 .model（returncode=%d）。看 train.log 尾部。" % rc)

    # 标准化命名：<材料>_gen<K>_seed<S>.model（gen 脚本/判据按这个名字找）
    dst = cwd / ("%s.model" % a.name)
    if model.resolve() != dst.resolve():
        model.replace(dst)
    metrics = parse_metrics(log_text)
    import hashlib as _hl
    _ds = cwd.parent.parent / "step6_dataset" / "dataset_summary.json"
    _dh = _hl.md5(_ds.read_bytes()).hexdigest() if _ds.is_file() else "?"
    summary = {
        "name": a.name,
        "data_hash": _dh,
        "seed": a.seed,
        "generation": a.gen,
        "model_file": dst.name,
        "foundation": Path(a.foundation).name,
        "device": a.device,
        "dtype": a.dtype,
        "batch_size": a.batch_size,
        "energy_weight": a.energy_weight,
        "forces_weight": a.forces_weight,
        "stress_weight": a.stress_weight,
        "returncode": rc,
        "e0s_mode": a.e0s_mode,
        "recipe": "%s|%g|%d|%s|ew%g|fw%g|sw%g|bs%d|hd%g" % (a.e0s_mode, a.lr, a.epochs, a.loss, a.energy_weight, a.forces_weight, a.stress_weight, a.batch_size, a.huber_delta),
        "metrics": metrics,
        "replay": Path(a.replay).name if a.replay else None,
    }
    (cwd / "finetune_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n")
    print("[DONE] %s（%s，%s）" % (dst.name, a.device, json.dumps(metrics)))
    sys.exit(0 if rc == 0 else rc)


if __name__ == "__main__":
    main()
