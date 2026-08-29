#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""converge_ctrl.py —— 验收决策 + 停机规则 + 定向加采计划（纯标准库，venv/登录节点都能跑）。

cwd = 材料目录。读 step8 刚写的 gen-<K>/validation_summary.json + 学习曲线 +
convergence_history.json，按 §7.3 决策表出 status：

    主闸（声子 RMSE < RMS_MAX）过        → pass
    未过 + 曲线未平                        → expand（数据量不足，定向加采）
    未过 + 曲线已平                        → halt_not_data_limited（加数据没用，停机）
    连续两代 Δrmse < IMPROVE_MIN（stagnant）→ halt_stagnant（停机）

输出：
    gen-<K>/plan.json                expand 时的定向加采计划（不是黑箱，写理由）
    convergence_history.json         追加本代记录（step8 顶层）
    gen-<K>/validation_summary.json 回填 status/decision
退出码 0 成功；非 0 = [ERROR]。
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mlff_common as mc  # noqa: E402

CHECKLIST = [
    "位移幅度窗口是否太窄（RATTLE_STD 三档是否覆盖目标温度的热运动幅度）",
    "训练超胞是否太小（r_max 与胞长的关系、2D 的面内尺寸）",
    "DFT 设置是否与验收基准一致（指纹、k 点密度）",
    "基座模型是否适配该体系（元素覆盖、CALIB_FC2 是否有虚频）",
    "离群过滤是否把有效数据滤掉了（FORCE_LIMIT 是否过严）",
    "微调超参（FORCES_WEIGHT、EPOCHS、BATCH_SIZE）",
]


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--gen", type=int, required=True)
    p.add_argument("--validation", required=True)    # step8_benchmark/gen-<K>/validation_summary.json
    p.add_argument("--curve", default=None)          # 学习曲线 json
    p.add_argument("--matdir", default=".")          # 材料目录（history/plan 落这里）
    p.add_argument("--rmse-max", type=float, default=0.2)
    p.add_argument("--improve-min", type=float, default=0.02)
    p.add_argument("--gen-increment", type=int, default=20)
    p.add_argument("--max-gen", type=int, default=4)
    p.add_argument("--mat", default="材料")
    return p.parse_args()


def main():
    a = parse_args()
    matdir = Path(a.matdir)
    cwd = Path.cwd()
    val = json.loads(Path(a.validation).read_text(encoding="utf-8"))
    rmse = float(val.get("phonon_rmse_THz"))
    curve = json.loads(Path(a.curve).read_text(encoding="utf-8")) if a.curve and Path(a.curve).is_file() \
        else {}
    plateau = bool(curve.get("curve_plateau", False))
    gates_failed = [g["name"] for g in val.get("gates", []) if g.get("required") and not g.get("pass")]

    hist = mc.conv_history(str(matdir / mc.CONV_HISTORY))
    # 上一代 = 最后一条 gen < 当前代 的记录。同一代重跑时 hist[-1] 是本代旧记录，
    # 拿它当"上一代"会把 delta 算成 0、误标 stagnant（进而误导"连续两代停滞"停机）。
    _prev = [r for r in hist if int(r.get("gen", -1)) != a.gen]
    prev = _prev[-1] if _prev else None
    prev_rmse = prev.get("rmse_thz") if prev else None
    delta = (prev_rmse - rmse) if prev_rmse is not None else None
    stagnant = bool(delta is not None and delta < a.improve_min)
    prev_status = prev.get("status") if prev else None
    n_stagnant = 1 if stagnant else 0
    if stagnant and prev_status == "expand" and prev and prev.get("stagnant"):
        n_stagnant = 2

    # [FIX-F3] --max-gen 原本解析了但从未使用。硬上限主要由 gen_step4 的
    # mc.halt_guard 把守，这里补一道兜底：已经是最后一代还没过主闸，
    # 就不要再写 expand 计划误导下一步。
    _at_last_gen = (a.gen >= a.max_gen)

    if rmse < a.rmse_max:
        status = "pass"
        reason = "声子 RMSE %.3f THz < RMS_MAX=%.2f" % (rmse, a.rmse_max)
    elif plateau:
        status = "halt_not_data_limited"
        reason = ("学习曲线已平（CURVE_TOL=%.2f），加数据没用。排查清单：\n  - %s"
                  % (curve.get("curve_tol", 0.05), "\n  - ".join(CHECKLIST)))
    elif n_stagnant >= 2:
        status = "halt_stagnant"
        reason = ("连续两代（gen %d, %d）声子 RMSE 改善均 < IMPROVE_MIN=%.2f THz，"
                  "主闸仍未通过。继续加数据不划算，已停止。排查清单：\n  - %s"
                  % (a.gen - 1, a.gen, a.improve_min, "\n  - ".join(CHECKLIST)))
    elif _at_last_gen:
        status = "halt_max_gen"
        reason = ("已到 MAX_GENERATION=%d 且主闸未过（RMSE %.3f THz）。排查清单：\n  - %s"
                  % (a.max_gen, rmse, "\n  - ".join(CHECKLIST)))
    else:
        status = "expand"
        reason = "曲线未平且主闸未过，数据量不足，定向加采"

    # ---- expand 时的定向加采计划（§7.3，不许是黑箱）----
    plan = None
    if status == "expand":
        q_map = val.get("qpoint_rmse_map") or {}
        sig_ext = float(val.get("committee_extrapolation_rate") or 0.0)
        if q_map:
            worst = sorted(q_map.items(), key=lambda kv: -kv[1])[:3]
            plan = {"strategy": "q点误差集中",
                    "reason": "q 点逐点 RMSE 最大的前 3 个：%s —— 对应频段/支的幅度档"
                              "加采" % [("q=%s RMSE=%.2f" % (k, v)) for k, v in worst],
                    "n_new": a.gen_increment}
        elif sig_ext > 0.05:
            plan = {"strategy": "committee外推",
                    "reason": "committee σ_F 外推率 %.1f%% > 5%%：在 σ_F 最大的构型所在"
                              "（应变, 幅度）格点加采" % (sig_ext * 100),
                    "n_new": a.gen_increment}
        else:
            plan = {"strategy": "加密应变网格",
                    "reason": "q 点误差与 committee 都不明显：新增 ±0.06/±0.09 应变档加采",
                    "n_new": a.gen_increment}
        plan_path = matdir / ("step8_benchmark/gen-%d" % a.gen) / "plan.json"
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(
            json.dumps(plan, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8", newline="\n")

    record = {
        "gen": a.gen,
        "n_frames": int(val.get("n_frames_total") or val.get("n_frames") or 0),
        "rmse_thz": round(rmse, 4),
        "force_rmse_meV_A": round(float(val.get("test_force_rmse_meV_A") or 0.0), 2),
        "delta_rmse": (round(delta, 4) if delta is not None else None),
        "curve_plateau": bool(plateau),
        "stagnant": bool(stagnant),
        "gates_failed": gates_failed,
        "status": status,
        "reason": reason,
    }
    # 同一代重跑（如改了 REF_DISP 后重训）→ 替换旧记录，不追加（否则 delta_rmse 会拿
    # 过期记录当“上一代”算，误导停机判定）
    if hist and hist[-1].get("gen") == a.gen:
        hist[-1] = record
    else:
        hist.append(record)
    mc.write_json(matdir / mc.CONV_HISTORY, hist)

    # 回填 validation_summary.json
    val["status"] = status
    val["decision"] = reason
    val["prev_rmse_thz"] = prev_rmse
    val["delta_rmse_thz"] = delta
    Path(a.validation).write_text(json.dumps(val, ensure_ascii=False, indent=2) + "\n",
                                  encoding="utf-8", newline="\n")
    print("[COST] gen=%d 累计 DFT 单点 %d 帧（本代见 manifest）；status=%s" %
          (a.gen, val.get("n_frames_total"), status))
    print("[DONE] status=%s：%s" % (status, reason.splitlines()[0]))


if __name__ == "__main__":
    main()
