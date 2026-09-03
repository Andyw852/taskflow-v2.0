#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""skill/mlff-mace/checks.py —— 本技能私有判据。

约束（SKILL_DEV §8）：只用标准库、无顶层副作用、判据名不与内置重名、秒级返回。
采集器已提供 os/re/json/glob/subprocess/tail_text。

判据一览：
    ck_step1       step1 紧弛豫：收敛 + 末次 external pressure ≤ pressure_tol（2D 只看面内）
    ck_manifest    step4：顶层 struct_manifest.json 存在
    ck_label       step5（fanout，逐 cfg 子目录）：本代清单所有帧都必须算完，
                   否则全部子目录报 False（驱动 retry/start 补生成新帧）
    ck_dataset     step6：数据集代数 == 清单代数
    ck_finetune    step7（fanout，逐 seed）：本代模型存在
    ck_benchmark   step8：validation_summary 存在且代数一致；halt_* → FAIL
    ck_publish     step9：publish_status.json 存在；pass 时必须还有 model_card.json

代数一致性是「手动推进下一代」的触发器：bump GENERATION → rerun -j 4（重新推送
step.conf + 生成新代清单）→ 本判据自动把 5/6/7/8 判成未完成 → tf retry/start 重跑。
"""
import os
import re

# ---- 基础小工具（只用标准库；复用采集器全局 tail_text 兜底）----


def _tail(path, n=200000):
    fn = globals().get("tail_text")
    if fn:
        try:
            return fn(path, n)
        except TypeError:
            return fn(path)
    try:
        with open(path, "rb") as fh:
            try:
                fh.seek(-n, os.SEEK_END)
            except OSError:
                fh.seek(0)
            return fh.read().decode("utf-8", "ignore")
    except OSError:
        return ""


def _json(d, rel):
    p = os.path.join(d, rel)
    if not os.path.isfile(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def _matdir(d):
    """步骤目录 → 材料目录（fanout 子目录多一层）。"""
    if os.path.basename(d).startswith(("cfg-", "seed-")):
        return os.path.dirname(os.path.dirname(d))
    return os.path.dirname(d)


def _manifest_gen(d):
    man = _json(os.path.join(_matdir(d), "step4_genstruct"), "struct_manifest.json")
    return (man or {}).get("generation") if man else None


def _outcar_ok(path):
    if not os.path.isfile(path):
        return False
    return "General timing and accounting informations" in _tail(path)


# ---- 步骤判据 -----------------------------------------------------------


def ck_step1(d, sc):
    """step1_relax：收敛（复用 relax_injob 语义）+ 末次 external pressure ≤ tol。
    pressure_tol 写在 skill.yaml 该步（默认 2.0 kB）；2D 只看面内 XX/YY 分量。"""
    ok, diag = ck_relax_injob(d, sc)
    if not ok:
        return ok, diag
    outcar = os.path.join(d, "OUTCAR")
    if not os.path.isfile(outcar):
        return False, "OUTCAR missing"
    txt = _tail(outcar)
    m = re.findall(r"external pressure\s*=\s*([-+0-9.eE]+)\s*kB", txt)
    if not m:
        return True, "converged（压力行读不出，跳过压力闸）"
    tol = float(sc.get("pressure_tol", 2.0))
    pres = float(m[-1])
    dim2d = False
    mf = os.path.join(d, "workflow_method.txt")
    if os.path.isfile(mf):
        for ln in open(mf, encoding="utf-8", errors="ignore"):
            if ln.upper().startswith("DIM="):
                dim2d = ln.split("=", 1)[1].strip().upper() == "2D"
    if dim2d:
        blocks = re.findall(r"in\s+kB\s*\n((?:\s*[-+0-9.eE]+){6}[^\n]*\n)+", txt)
        if blocks:
            rows = [ln.split() for ln in blocks[-1].strip().splitlines()]
            if len(rows) >= 2:
                try:
                    pres = max(abs(float(rows[0][0])), abs(float(rows[1][1])))
                except (ValueError, IndexError):
                    pass
        if abs(pres) > tol:
            return False, ("面内压强 %.2f kB > %.1f kB（2D 只看面内 XX/YY；"
                           "真空方向分量不判）。cp CONTCAR POSCAR 再弛豫" % (pres, tol))
        return True, "converged（2D 面内压强 %.2f kB ≤ %.1f）" % (pres, tol)
    if abs(pres) > tol:
        return False, ("external pressure %.2f kB > %.1f kB —— 晶胞没弛豫到位，"
                       "cp CONTCAR POSCAR 再跑一轮" % (pres, tol))
    return True, "converged（pressure %.2f kB ≤ %.1f）" % (pres, tol)


def ck_manifest(d, sc):
    """step4_genstruct：当前代结构清单存在。"""
    man = _json(d, "struct_manifest.json")
    if man is None:
        return False, "struct_manifest.json 未生成（retry 重跑 gen）"
    return True, "第 %d 代 %d 帧已生成" % (man.get("generation"), man.get("n_frames"))


def ck_label(d, sc):
    """step5_label（fanout，逐 cfg 子目录）：本帧 OUTCAR 完整 + 结构指纹一致。

    ★ 只查本帧：其它帧（含其它代）缺失/未算完不能把已算完的本帧误判成未完成，
      否则推进下一代时会把上几代已算完的帧全部重新提交一遍（浪费 DFT 单点）。"""
    mat = _matdir(d)
    cid = os.path.basename(d)
    man = _json(os.path.join(mat, "step4_genstruct"), "struct_manifest.json")
    if man is None:
        return False, "step4 清单缺失，先让 S4 生成结构"
    if cid.startswith("cfg-"):
        # 本帧（fanout 子目录）：只查本帧是否算完
        if not _outcar_ok(os.path.join(d, "OUTCAR")):
            return False, "本帧未算完"
        # 结构指纹（本帧）：同一 cfg id 换过结构则旧 OUTCAR 是脏的
        import hashlib as _hl
        ent = next((e for e in man.get("frames", []) if e["id"] == cid), None)
        if ent is not None:
            src = os.path.join(mat, "step4_genstruct", "gen-%d" % ent["gen"], ent["file"])
            old = os.path.join(d, "POSCAR")
            if os.path.isfile(src) and os.path.isfile(old):
                try:
                    md5_new = _hl.md5(open(src, "rb").read()).hexdigest()
                    md5_old = _hl.md5(open(old, "rb").read()).hexdigest()
                    if md5_new != md5_old:
                        return False, "本帧结构已变（REF_DISP/应变改了？），retry 重算"
                except OSError:
                    pass
        return True, "本帧已算完"
    # 步骤级：本代清单所有帧都必须算完（驱动 retry 补生成缺失的 cfg 目录）
    missing = [e["id"] for e in man.get("frames", [])
               if not _outcar_ok(os.path.join(mat, "step5_label", e["id"], "OUTCAR"))]
    if missing:
        return False, ("第 %d 代还有 %d 帧未算完（%s...）：retry 本步生成缺失的 "
                       "cfg 目录后 start 提交" % (man.get("generation"), len(missing),
                                                   ", ".join(missing[:3])))
    return True, "本代清单所有帧均已算完"

def ck_dataset(d, sc):
    """step6_dataset：数据集存在且代数 == 清单代数。"""
    mat = _matdir(d)
    top = _json(d, "dataset_summary.json")
    man = _json(os.path.join(mat, "step4_genstruct"), "struct_manifest.json")
    if top is None:
        return False, "dataset_summary.json 未生成（retry 重跑 gen）"
    if man is None:
        return False, "step4 清单缺失"
    if int(top.get("generation", -1)) != int(man.get("generation", -2)):
        return False, ("数据集代数 %s ≠ 清单代数 %s：推进下一代后需 retry 重建"
                       % (top.get("generation"), man.get("generation")))
    import hashlib as _hl
    _mf = os.path.join(mat, "step4_genstruct", "struct_manifest.json")
    _dh = _hl.md5(open(_mf, "rb").read()).hexdigest() if os.path.isfile(_mf) else ""
    if top.get("data_hash") != _dh:
        return False, "清单内容已变（REF_DISP/应变改了？）：retry 重建数据集"
    return True, ("gen %d：%d 帧（train %d / test %d / filtered %d）"
                  % (top["generation"], top.get("n_frames_total", 0),
                     top.get("n_train", 0), top.get("n_test", 0),
                     top.get("n_filtered", 0)))


def ck_finetune(d, sc):
    """step7_finetune（fanout，d=seed 子目录）：本代模型存在。"""
    mat = _matdir(d)
    gen = _manifest_gen(d)
    if gen is None:
        return False, "step4 清单缺失"
    seed = os.path.basename(d).split("-")[-1]
    matname = os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(d))))
    model = os.path.join(d, "%s_gen%d_seed%s.model" % (matname, gen, seed))
    import hashlib as _hl
    _ds = os.path.join(mat, "step6_dataset", "dataset_summary.json")
    _dh = _hl.md5(open(_ds, "rb").read()).hexdigest() if os.path.isfile(_ds) else ""
    _fs = _json(d, "finetune_summary.json") or {}
    if os.path.isfile(model) and _fs.get("data_hash") == _dh:
        return True, "seed %s 已训出 gen %d 模型" % (seed, gen)
    if os.path.isfile(model) and _fs.get("data_hash") != _dh:
        return False, "数据集内容已变：seed %s 的模型是旧数据训的，retry 重训" % seed
    return False, ("seed %s 缺 gen %d 模型：retry 本步（幂等，已算的 seed 会跳过）"
                   % (seed, gen))


def ck_benchmark(d, sc):
    """step8_benchmark：validation_summary 存在且代数一致；halt_* 判 FAIL。"""
    mat = _matdir(d)
    val = _json(d, "validation_summary.json")
    man = _json(os.path.join(mat, "step4_genstruct"), "struct_manifest.json")
    if val is None:
        return False, "validation_summary.json 未生成（作业还没跑完？）"
    if man is None:
        return False, "step4 清单缺失"
    if int(val.get("generation", -1)) != int(man.get("generation", -2)):
        return False, ("验收代数 %s ≠ 清单代数 %s：推进下一代后需 retry 重跑"
                       % (val.get("generation"), man.get("generation")))
    status = val.get("status", "expand")
    if str(status).startswith("halt_"):
        return False, ("停机：%s —— %s" % (status,
                       (val.get("decision") or "").splitlines()[0]))
    rmse = val.get("phonon_rmse_THz")
    return True, ("status=%s（RMSE %.3f THz，闸 %d/%d 过）"
                  % (status, rmse, sum(1 for g in val.get("gates", []) if g.get("pass")),
                     sum(1 for g in val.get("gates", []) if g.get("required"))))


def ck_publish(d, sc):
    """step9_publish：publish_status.json 必须存在且代数一致；pass 时还要 model_card.json。"""
    st = _json(d, "publish_status.json")
    if st is None:
        return False, "publish_status.json 未生成（retry 重跑 gen）"
    mat = _matdir(d)
    man = _json(os.path.join(mat, "step4_genstruct"), "struct_manifest.json")
    if man is not None and int(st.get("generation", -1)) != int(man.get("generation", -2)):
        return False, ("发布代数 %s ≠ 清单代数 %s：推进下一代后需 retry 重跑发布"
                       % (st.get("generation"), man.get("generation")))
    if st.get("status") == "pass":
        if _json(d, "model_card.json") is None:
            return False, "status=pass 但 model_card.json 缺失"
        return True, "已发布：%s" % st.get("published_path", "")
    if str(st.get("status", "")).startswith("halt_"):
        if _json(d, "model_card.json") is None:
            return False, "停机但 model_card.json 缺失（应写 converged:false）"
        return True, "停机归档（converged=false，模型未发布）"
    return True, "跳过发布：%s" % (st.get("note") or st.get("status"))


# ---- 内置语义复用：relax_injob（step1 收敛判据）----
def _conv(d):
    p = os.path.join(d, "OUTCAR")
    return os.path.isfile(p) and "reached required accuracy" in _tail(p)


def ck_relax_injob(d, sc):
    """作业内分段弛豫：收敛 = OUTCAR 出现 reached required accuracy。"""
    mat = os.path.dirname(d)
    for legacy in ("step1a_PBE_opt", "step1b_PBE_opt", "step1c_PBE_opt",
                   "step1a_std_opt", "step1b_std_opt", "step1c_std_opt"):
        if _conv(os.path.join(mat, legacy)):
            return True, "旧分段 %s 已收敛，跳过" % legacy
    if _conv(d):
        return True, "converged"
    if not os.path.isfile(os.path.join(d, "OUTCAR")):
        return False, "OUTCAR missing"
    return False, "弛豫未收敛 —— 看 OUTCAR 尾部定位震荡来源，调参后 tf retry"


CHECKERS = {
    "step1": ck_step1,
    "manifest": ck_manifest,
    "label": ck_label,
    "dataset": ck_dataset,
    "finetune": ck_finetune,
    "benchmark": ck_benchmark,
    "publish": ck_publish,
    # relax_injob 已由公共池 skill/_common/opt/checks_relax.py 注册，此处不再重复
    # （本技能 step1 用 check: step1，内部直接调用上方 ck_relax_injob 函数，无需注册该名）
}
