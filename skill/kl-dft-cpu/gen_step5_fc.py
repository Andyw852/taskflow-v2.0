#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_step5_fc.py —— 力常数拟合，提交计算节点（step5_fc）。

【结构变更】S5_fc 从"登录节点 gen 里裸跑 phono3py"改成"提交计算节点作业"：pheasy/symfc
拟合是重活（几十核 + 大内存 + 数小时），压不进登录节点。本 gen 只做准备：
  1. 校验 step4 产物（phono3py_disp.yaml + disp-*/vasprun.xml；alm 才有 SPOSCAR）
  2. 把 step.conf + kl_params 解析成 fit_config.json（作业里 kl_fc_backends.py 读它）
  3. 按 FIT_ENGINE 选提交模板渲染 submit.sh
tf 提交后，计算节点按 submit.sh 依次跑 kl_fc_backends 的 prep → 拟合 → post：
  拟合器（FIT_ENGINE）：phono3py(symfc/alm，默认) | pheasy(随机位移压缩感知，需 METHOD=alm)
  产出：step5_fc/phono3py/（fc2/fc3.hdf5 + phono3py_disp.yaml）
        step5_fc/shengbte/（FORCE_CONSTANTS_2ND/3RD，EXPORT_SHENGBTE=true 时）
        step5_fc/phonon_summary.json（虚频闸 marker：'"stable": true'）
产出目录：step5_fc/
"""
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kl_common as kc
import stepconf

OUTDIR   = "step5_fc"
STEP     = "step5_fc"
DISP_DIR = "step4_disp"

SPEC = {
    "FUNC":        ("pbesol", "str"),   # 全局带入，本步不用
    # —— 拟合器选择 ——
    "FIT_ENGINE":  ("phono3py", "str"), # phono3py | pheasy
    # phono3py 路（symfc/alm）
    "FC_CALC":     ("symfc",  "str"),   # symfc | alm
    "FC3_CUTOFF":  (None,     "str"),   # fc3 截断 Å（"5.0"）；空/None=不截断
    # pheasy 路
    "PHEASY_FIT_METHOD": ("RFE", "str"), # LASSO | RFE | OLS（OLS 最吃内存）
    "PHEASY_C3_CUTOFF":  ("5.2", "str"), # pheasy fc3 截断 Å；None=不截断
    "PHEASY_ENABLE_FC":  (3,     "int"), # 2|3|4（热导率需 ≥3）
    "PHEASY_BIN":        ("pheasy", "str"), # pheasy 可执行名：pheasy | pheasy-gpu（GPU 版）
    "NULL_SPACE_EPS":    (0.001, "float"),
    # —— 导出 & 虚频闸 ——
    # —— 缺帧容错（仅随机位移 METHOD=alm 生效；findiff 必须帧帧齐全）——
    "MIN_SUCCESS_RATIO":  (0.9, "float"),  # 成功帧占比下限，低于它报错
    "MIN_SUCCESS_FRAMES": (0,   "int"),    # 成功帧绝对下限，0=不限
    # —— 导出 & 虚频闸 ——
    "EXPORT_SHENGBTE": (True, "bool"),   # 任一拟合器都产出 shengbte 力常数
    "BAND_POINTS":     (51,   "int"),
    "IMAG_THR":        (0.10, "float"),  # 虚频阈值(THz)
    # 作业资源（核数/qos/时长）走 step.conf 的 [submit] 段覆盖 #SBATCH，不在 [params] 里。
}


def main():
    cwd = Path.cwd()
    out = cwd / OUTDIR
    out.mkdir(exist_ok=True)
    conf = stepconf.load(SPEC, STEP)
    disp = cwd / DISP_DIR

    # ---- 校验 step4 产物 ----
    if not (disp / "phono3py_disp.yaml").is_file():
        sys.exit("[ERROR] %s 缺 phono3py_disp.yaml（step4 未生成位移）" % disp)
    if not list(disp.glob("disp-*/vasprun.xml")):
        sys.exit("[ERROR] %s 下无 disp-*/vasprun.xml，位移单点还没算完" % disp)

    params = kc.read_kl_params(disp / kc.KL_PARAMS)
    method = (params.get("METHOD") or "alm").lower()
    supercell = params.get("SUPERCELL") or ""
    dim = (params.get("DIM") or "").lower()

    engine = str(conf["FIT_ENGINE"]).lower()
    if engine not in ("phono3py", "pheasy"):
        sys.exit("[ERROR] FIT_ENGINE 只允许 phono3py / pheasy")
    if engine == "pheasy" and method != "alm":
        sys.exit("[ERROR] FIT_ENGINE=pheasy 需要随机位移（step4 METHOD=alm）。\n"
                 "        findiff 请用 FIT_ENGINE=phono3py，或把 step4 改成 alm 重跑。")
    p_bin = str(conf["PHEASY_BIN"] or "pheasy").lower()
    if p_bin not in ("pheasy", "pheasy-gpu"):
        sys.exit("[ERROR] PHEASY_BIN 只允许 pheasy / pheasy-gpu")
    if str(conf["FC_CALC"]).lower() not in ("symfc", "alm"):
        sys.exit("[ERROR] FC_CALC 只允许 symfc / alm")

    # kl_params 一并拷进 step5_fc（溯源/后续继承）
    for f in (kc.KL_PARAMS, kc.METHOD_FILE):
        if (disp / f).is_file():
            shutil.copyfile(disp / f, out / f)

    # ---- 写 fit_config.json（作业里读）----
    cfg = {
        "FIT_ENGINE": engine,
        "METHOD": method,
        "DIM": dim.upper(),
        "SUPERCELL": supercell,                       # 对角三整数，pheasy --dim / shengbte scell
        "FC_CALC": str(conf["FC_CALC"]).lower(),
        "FC3_CUTOFF": (None if conf["FC3_CUTOFF"] in (None, "", "None", "none")
                       else str(conf["FC3_CUTOFF"])),
        "PHEASY_FIT_METHOD": str(conf["PHEASY_FIT_METHOD"]).upper(),
        "PHEASY_C3_CUTOFF": str(conf["PHEASY_C3_CUTOFF"]),
        "PHEASY_ENABLE_FC": int(conf["PHEASY_ENABLE_FC"]),
        "PHEASY_BIN": p_bin,
        "NULL_SPACE_EPS": float(conf["NULL_SPACE_EPS"]),
        "MIN_SUCCESS_RATIO": float(conf["MIN_SUCCESS_RATIO"]),
        "MIN_SUCCESS_FRAMES": int(conf["MIN_SUCCESS_FRAMES"]),
        "EXPORT_SHENGBTE": bool(conf["EXPORT_SHENGBTE"]),
        "BAND_POINTS": int(conf["BAND_POINTS"]),
        "IMAG_THR": float(conf["IMAG_THR"]),
    }
    (out / "fit_config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")

    # ---- 选模板渲染 submit.sh ----
    here = Path(__file__).resolve().parent
    # 关键：把作业运行时要执行的驱动脚本拷进产出目录。gen_need 只保证 gen 本地能用到它，
    #   不会自动进到发往计算节点的 step 目录；作业里要 `python kl_fc_backends.py`，
    #   必须显式拷过去（否则计算节点报 No such file）。
    shutil.copyfile(here / "kl_fc_backends.py", out / "kl_fc_backends.py")
    if engine == "pheasy":
        # GPU 拟合（PHEASY_BIN=pheasy-gpu）走独立模板 submit_fit_pheasy_gpu（--gres + 降核）。
        # 纯 CPU 集群（jzzn/hanhai25）没有该模板 → gen 期即报错，不排进队才失败（G2）。
        _kind = ("submit_fit_pheasy_gpu" if p_bin == "pheasy-gpu"
                 else "submit_fit_pheasy")
        try:
            tpl = kc.resolve_submit(here, dim or "3d", _kind)
        except SystemExit:
            if _kind == "submit_fit_pheasy_gpu":
                sys.exit("[ERROR] PHEASY_BIN=pheasy-gpu 需要 GPU 拟合模板 "
                         "submit_fit_pheasy_gpu.tpl（a800/3090 已配）。\n"
                         "        当前集群只有 CPU 模板——pheasy-gpu 在无 GPU 节点跑不了。\n"
                         "        改用 PHEASY_BIN=pheasy（CPU 拟合），或把材料 hpc 切到 "
                         "a800/3090。")
            raise
        subs = {"JOBNAME": kc.new_jobname(cwd, "S5fit"),
                "DIM": supercell or "1 1 1",
                "FIT_METHOD": cfg["PHEASY_FIT_METHOD"],
                "ENABLE_FC": str(cfg["PHEASY_ENABLE_FC"]),
                "PHEASY_BIN": p_bin,
                "C3_CUTOFF": cfg["PHEASY_C3_CUTOFF"],
                "NULL_SPACE_EPS": str(cfg["NULL_SPACE_EPS"])}
    else:
        tpl = kc.resolve_submit(here, dim or "3d", "submit_fit_p3py")
        subs = {"JOBNAME": kc.new_jobname(cwd, "S5fit")}
    kc.write_submit(tpl, out / "submit.sh", subs)
    stepconf.apply_submit(out / "submit.sh", conf.submit)

    print("[..] 拟合器=%s 方法=%s 超胞=%s DIM=%s" % (engine, method, supercell, dim or "?"))
    print("[DONE] %s：submit.sh + fit_config.json 就绪。tf 提交后计算节点出 fc2/fc3，"
          "写 phonon_summary.json（'\"stable\": true' 为 marker）。" % OUTDIR)


if __name__ == "__main__":
    main()