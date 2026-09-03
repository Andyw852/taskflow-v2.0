#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_step3_fc.py —— 拟合 fc2/fc3 → 声子谱 → 虚频闸（step3_fc）。

run: gen —— 登录节点跑。步骤：
  1. 从 step2 取位移+力（优先 phono3py_params.yaml；没有就 phono3py_disp.yaml + FORCES_FC3）
  2. 可选 NAC：MACE **给不出 Born 有效电荷和 ε∞**（势里没有电荷响应），所以这里只能
     用外部 BORN 文件。有 kl-dft-cpu 技能算过 step3_nac 的同一材料，把那份 BORN 的路径填进
     NAC_BORN 即可；极性材料不加 NAC，Γ 点 LO-TO 劈裂缺失，光学支和高温 κ 会偏。
  3. 拟合力常数：findiff → --sym-fc；random → --fc-calc symfc（退回 alm）
  4. 声子谱 → band-dft-cpu.yaml → 取最小频率
  5. 写 phonon_summary.json："stable": true 才放行 step4（判据 marker）

虚频这一关对 MLIP 尤其要认真看：基座模型（mace-mp 等）在训练分布外的体系上，
软模判断经常不可信。出虚频先别急着说材料不稳定，按 README 的排查顺序走一遍。
"""
import json
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import klmace_common as kc
import stepconf

OUTDIR = "step3_fc"
STEP = "step3_fc"
SRC = "step2_disp_force"

SPEC = {
    # ---- 全局 ----
    "MACE_MODEL": ("mace-mp:medium", "str"),
    "MACE_MODEL_DIR": ("", "str"),
    "DEVICE": ("auto", "str"),
    "DTYPE": ("float64", "str"),
    "CONDA_SH": (kc.DEFAULT_CONDA_SH, "str"),
    "CONDA_ENV": (kc.DEFAULT_CONDA_ENV, "str"),
    # ---- 本步 ----
    "BAND_POINTS": (101, "int"),
    "IMAG_THR": (0.10, "float"),      # 虚频阈值(THz)
    "FIT": ("auto", "str"),           # phono3py 拟合器：auto | sym-fc | symfc | alm
    "NAC_BORN": ("", "str"),          # 外部 BORN 文件路径（DFPT 算的），空=不加 NAC
    # ---- 拟合软件（FIT_SOFTWARE）----
    "FIT_SOFTWARE": ("phono3py", "str"),  # phono3py（symfc/alm）| pheasy
    "PHEASY_METHOD": ("OLS", "str"),      # pheasy 拟合方法：OLS | LASSO | RFE | RFE_TSQR
    "PHEASY_C3_CUTOFF": ("6.0", "str"),   # pheasy 三阶截断(Å)；None/空=不截断
    "PHEASY_BIN": ("pheasy", "str"),        # pheasy 可执行名：pheasy | pheasy-gpu（GPU 版）
}


def parse_min_freq(band_yaml):
    p = Path(band_yaml)
    if not p.is_file():
        return None
    fr = [float(m.group(1))
          for ln in p.read_text(errors="ignore").splitlines()
          for m in [re.match(r"\s*frequency:\s*(-?[\d.Ee+]+)", ln)] if m]
    return min(fr) if fr else None


# 虚频闸用 phonopy API（对照 kl-dft-cpu 的 _stability_gate）：phono3py-load 3.x 的
# `--band-dft-cpu auto` 不再产出 band-dft-cpu.yaml（只写 phono3py.yaml 摘要），所以这里直接
# 读 symfc 拟合好的 fc2.hdf5 → Phonopy run_mesh 取 q-mesh 最小频率，再 best-effort
# 出 band-dft-cpu.yaml 存档。stdout 打 `MIN_FREQ_THZ <值>` 供外层解析。
_PHONON_GATE = r'''import numpy as np, os
import phono3py
from phono3py.file_IO import read_fc2_from_hdf5
from phonopy import Phonopy

yaml = "phono3py_disp.yaml" if os.path.isfile("phono3py_disp.yaml") else "phono3py_params.yaml"
ph3 = phono3py.load(yaml, produce_fc=False, is_nac=False, log_level=0)
uc, scm, pm = ph3.unitcell, ph3.supercell_matrix, ph3.primitive_matrix
fc2 = np.asarray(read_fc2_from_hdf5(filename="fc2.hdf5"))
ph = Phonopy(uc, supercell_matrix=scm, primitive_matrix=pm)
ph.force_constants = fc2
ph.run_mesh(mesh=60.0, with_eigenvectors=False, is_mesh_symmetry=True)
mf = float(np.min(ph.get_mesh_dict()["frequencies"]))
print("MIN_FREQ_THZ %.6f" % mf)
try:
    ph.auto_band_structure(plot=False, write_yaml=True, filename="band-dft-cpu.yaml")
except Exception:
    pass
'''


# pheasy 拟合（FIT_SOFTWARE=pheasy）：从 phono3py_params.yaml 抽出随机位移+力 →
# 写 POSCAR/SPOSCAR/dataset_disps.npy/dataset_forces.npy → 四步 pheasy CLI
# (-s cluster space / -c 对称约束 / -d 位移矩阵 / -f 拟合) → fc2.hdf5/fc3.hdf5。
# 对照 wangchao 的通用 pheasy 脚本：float64、RASR=none、LASSO 走 celer + --std。
# 参数：拟合方法(OLS|LASSO|RFE|RFE_TSQR) 三阶截断(Å)
_PHEASY_FIT = r'''import os, sys, subprocess
import numpy as np
import phono3py
from phonopy.interface.vasp import write_vasp

method = sys.argv[1] if len(sys.argv) > 1 else "OLS"
c3 = sys.argv[2] if len(sys.argv) > 2 else "6.0"

yaml = "phono3py_params.yaml" if os.path.isfile("phono3py_params.yaml") else "phono3py_disp.yaml"
ph3 = phono3py.load(yaml, produce_fc=False, log_level=0)
write_vasp("POSCAR", ph3.unitcell, direct=True)
write_vasp("SPOSCAR", ph3.supercell, direct=True)
ds = ph3.dataset
disps = np.asarray(ds["displacements"], float)
forces = np.asarray(ds["forces"], float)
nsc = disps.shape[1]
ndata = len(disps)
# pheasy 的 -d/--disp_file 读 disp_matrix.pkl、-f 读 force_matrix.pkl
# （对照通用脚本步骤1的 pickle 输出：cartesian 位移 + 已扣平衡帧的力）。
import pickle
pickle.dump(disps, open("disp_matrix.pkl", "wb"))
pickle.dump(forces, open("force_matrix.pkl", "wb"))
dim = " ".join(str(int(x)) for x in np.diag(ph3.supercell_matrix))

env = dict(os.environ)
env.update({
    "PHEASY_SM_DTYPE": "float64",
    "PHEASY_SM_THR": "1e-12",
    "PHEASY_ASR_SPARSE": "1",
    "PHEASY_ASR_SPARSE_THR": "1e-10",
    "PHEASY_ASR_COL_BLOCK": "5000",
    "PHEASY_ASR_COMBINED": "1",
    "PHEASY_NS_RANK_TOL": "1e-6",
    "PHEASY_USE_CELER": "1" if method in ("LASSO", "RFE", "RFE_TSQR") else "0",
    "PHEASY_LASSO_DEBIAS": "1" if method == "LASSO" else "0",
})

cflag = "" if c3 in ("None", "none", "") else "--c3 %s" % c3
bin = os.environ.get("PHEASY_BIN", "pheasy")
fit = ("%s --dim %s -w 3 -f %s --ndata %d --eps 0.001 --full_ifc -l %s --hdf5"
       % (bin, dim, cflag, ndata, method))
if method == "LASSO":
    fit += " --std --mu_min -8 --mu_max -2 --max_iter 2000 --cv 5 --nmu 10 --tol 0.0001"
elif method in ("RFE", "RFE_TSQR"):
    fit += " --mu_min -8 --mu_max -5 --max_iter 1000 --cv 5 --nmu 5 --tol 0.001"
steps = [
    "%s --dim %s -w 3 -s %s --eps 0.001" % (bin, dim, cflag),
    "%s --dim %s -w 3 -c %s --eps 0.001" % (bin, dim, cflag),
    "%s --dim %s -w 3 -d %s --ndata %d --disp_file --eps 0.001" % (bin, dim, cflag, ndata),
]
for s in steps:
    print("[pheasy]", s, flush=True)
    r = subprocess.run(s, shell=True, env=env)
    if r.returncode != 0:
        sys.exit("[ERROR] pheasy 步骤失败(rc=%d): %s" % (r.returncode, s))

# 拟合步：捕获输出，做 LASSO alpha 边界门禁（对照通用脚本 LASSO_GATE_ON_BOUNDARY）
print("[pheasy]", fit, flush=True)
r = subprocess.run(fit, shell=True, env=env, capture_output=True, text=True)
sys.stdout.write(r.stdout or "")
if r.stderr:
    sys.stderr.write(r.stderr or "")
if r.returncode != 0:
    sys.exit("[ERROR] pheasy 拟合失败(rc=%d): %s" % (r.returncode, fit))
if method == "LASSO":
    import re as _re
    m = _re.search(r"best alpha=\s*([\d.eE+-]+)", r.stdout)
    if m:
        a = float(m.group(1))
        lg = float(np.log10(a))
        lo, hi = -8.0, -2.0
        if lg <= lo + 0.05 or lg >= hi - 0.05:
            print("[GATE] alpha_opt=%.3e 撞网格边界 [%g,%g] —— LASSO 选型无效，"
                  "建议加大 N_RANDOM/OVERSAMPLE" % (a, lo, hi))
            open(".fit_gate_fail", "a").write("alpha 撞边界\n")

for f in ("fc2.hdf5", "fc3.hdf5"):
    if not os.path.isfile(f):
        sys.exit("[ERROR] pheasy 未产出 %s" % f)
print("PHEASY_DONE dim=%s ndata=%d method=%s" % (dim, ndata, method))
'''


def stage_inputs(cwd, out):
    """从 step2 取位移+力。大文件软链，小文件拷。-> 用哪个 yaml。"""
    src = cwd / SRC
    if not src.is_dir():
        sys.exit("[ERROR] 找不到 %s（step2 没跑）" % SRC)
    for f in ("POSCAR", kc.KL_PARAMS, kc.METHOD_FILE, "phono3py_disp.yaml"):
        if (src / f).is_file():
            shutil.copyfile(str(src / f), str(out / f))
    for f in ("FORCES_FC3", "FORCES_FC2", "phono3py_params.yaml"):
        if (src / f).is_file():
            kc.link_or_copy(src / f, out / f)

    if (out / "phono3py_params.yaml").is_file():
        return "phono3py_params.yaml"
    if (out / "phono3py_disp.yaml").is_file() and (out / "FORCES_FC3").is_file():
        return "phono3py_disp.yaml"
    sys.exit("[ERROR] %s 里既没有 phono3py_params.yaml，也没有 "
             "phono3py_disp.yaml + FORCES_FC3 —— step2 的取力作业没跑完。" % SRC)


def stage_born(out, conf):
    p = str(conf["NAC_BORN"] or "").strip()
    if not p:
        print("[..] NAC_BORN 未设 → 不加 NAC（非极性体系无所谓；极性体系见 README）")
        return False
    src = Path(p).expanduser()
    if not src.is_file():
        sys.exit("[ERROR] NAC_BORN 指向的文件不存在：%s" % src)
    shutil.copyfile(str(src), str(out / "BORN"))
    print("[OK] BORN ← %s（外部 DFPT 结果，MACE 自己给不出 Born 电荷）" % src)
    return True


def main():
    cwd = Path.cwd()
    out = cwd / OUTDIR
    out.mkdir(exist_ok=True)
    conf = stepconf.load(SPEC, STEP)

    yaml = stage_inputs(cwd, out)
    params = kc.read_kl_params(out / kc.KL_PARAMS)
    method = (params.get("METHOD") or "findiff").lower()
    use_nac = stage_born(out, conf)

    software = str(conf["FIT_SOFTWARE"] or "phono3py").lower()
    # p_bin 先落默认值：fit_cfg 无论哪种 software 都引用它（此前只在校验分支内赋值，
    # phono3py 路会 NameError）。PHEASY_BIN 校验对两条路都生效。
    p_bin = str(conf["PHEASY_BIN"] or "pheasy").lower()
    if p_bin not in ("pheasy", "pheasy-gpu"):
        sys.exit("[ERROR] PHEASY_BIN 只允许 pheasy / pheasy-gpu")
    if software == "pheasy":
        p_method = str(conf["PHEASY_METHOD"] or "OLS").upper()
        if p_method not in ("OLS", "LASSO", "RFE", "RFE_TSQR"):
            sys.exit("[ERROR] PHEASY_METHOD 只允许 OLS / LASSO / RFE / RFE_TSQR")
        fit = ("pheasy-gpu" if p_bin == "pheasy-gpu" else "pheasy") + " (" + p_method + ")"
        (out / "_pheasy_fit.py").write_text(_PHEASY_FIT, encoding="utf-8")
    else:
        fit = str(conf["FIT"] or "auto").lower()
        if fit == "auto":
            fit = "sym-fc" if method == "findiff" else "symfc"
        if fit not in ("sym-fc", "symfc", "alm"):
            sys.exit("[ERROR] FIT 只允许 auto / sym-fc / symfc / alm")
    print("[..] 拟合软件=%s  方法=%s  输入=%s" % (software, fit, yaml))

    # 虚频闸脚本 + 驱动脚本 + 拟合配置（submit 作业里跑）
    (out / "_phonon_gate.py").write_text(_PHONON_GATE, encoding="utf-8")
    here = Path(__file__).resolve().parent
    if not (here / "fc_fit_driver.py").is_file():
        sys.exit("[ERROR] 缺 fc_fit_driver.py —— 本步 gen_need 里漏了它？")
    shutil.copyfile(str(here / "fc_fit_driver.py"), str(out / "fc_fit_driver.py"))

    fit_cfg = {
        "yaml": yaml,
        "software": software,
        "fit": fit,
        "pheasy_method": str(conf["PHEASY_METHOD"] or "OLS").upper(),
        "pheasy_bin": p_bin,
        "c3_cutoff": str(conf["PHEASY_C3_CUTOFF"]),
        "method": method,
        "imag_thr": float(conf["IMAG_THR"]),
        "supercell": params.get("SUPERCELL"),
        "fc2_supercell": params.get("FC2_SUPERCELL"),
        "nac": use_nac,
    }
    (out / "fit_config.json").write_text(
        json.dumps(fit_cfg, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")

    # GPU 拟合（FIT_SOFTWARE=pheasy + PHEASY_BIN=pheasy-gpu）走 submit_fc_gpu.tpl（--gres）；
    # 集群没配该模板 → gen 期报错（pheasy-gpu 需要 GPU 节点，别等排进队才失败）。
    _kind = ("submit_fc_gpu" if (software == "pheasy" and p_bin == "pheasy-gpu")
             else "submit_fc")
    try:
        tpl = kc.resolve_submit(here, _kind)
    except SystemExit:
        if _kind == "submit_fc_gpu":
            sys.exit("[ERROR] PHEASY_BIN=pheasy-gpu 需要 GPU 拟合模板 submit_fc_gpu.tpl"
                     "（a800/3090 已配）。\n"
                     "        当前集群没有 → 改用 PHEASY_BIN=pheasy（CPU），"
                     "或把材料 hpc 切到 a800/3090。")
        raise
    kc.write_submit(tpl, out / "submit.sh",
                    {"JOBNAME": kc.new_jobname(cwd, "S3fit"),
                     "CONDA_SH": conf["CONDA_SH"] or kc.DEFAULT_CONDA_SH,
                     "CONDA_ENV": conf["CONDA_ENV"] or kc.DEFAULT_CONDA_ENV})
    stepconf.apply_submit(out / "submit.sh", conf.submit)
    print("[DONE] %s：submit.sh 就绪（作业跑完写 fc2/fc3.hdf5 + phonon_summary.json）"
          % OUTDIR)


if __name__ == "__main__":
    main()
