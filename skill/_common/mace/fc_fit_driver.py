#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fc_fit_driver.py —— 力常数拟合 + 虚频闸 + phonon_summary.json。

klmace S3_fc 的 submit 模式驱动脚本，在计算节点作业里跑（conda/venv 里）。
读 fit_config.json（gen_step3 写好），依次：拟合（phono3py symfc/alm 或 pheasy）
→ phonopy 虚频闸 → 写 phonon_summary.json。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np



def export_shengbte(yaml, prim_src=None):
    """拟合完成后把力常数导成 ShengBTE 格式，放 step3_fc/shengbte/。

    两条拟合路径统一出口：
      - pheasy（--full_ifc）已直接写 FORCE_CONSTANTS/FORCE_CONSTANTS_3RD（ShengBTE 文本，
        3RD 就是块格式、2ND 即 phonopy full 格式）→ 搬进 shengbte/ 并改名 2ND。
      - phono3py（symfc/alm）只写 fc2/fc3.hdf5 → 用 hiphive 读 hdf5 转 ShengBTE 格式。
    ShengBTE 格式定义见 hiphive input_output/shengBTE.py；数值正确性已用 Si 实测
    （ShengBTE RTA κ 与 phono3py 一致）。"""
    import shutil
    from pathlib import Path
    sb = Path("shengbte"); sb.mkdir(exist_ok=True)
    # 情形 A：pheasy 已产出文本（同目录 FORCE_CONSTANTS + FORCE_CONSTANTS_3RD）
    if Path("FORCE_CONSTANTS").is_file() and Path("FORCE_CONSTANTS_3RD").is_file():
        shutil.copyfile("FORCE_CONSTANTS", str(sb / "FORCE_CONSTANTS_2ND"))
        shutil.copyfile("FORCE_CONSTANTS_3RD", str(sb / "FORCE_CONSTANTS_3RD"))
        print("[OK] shengbte/ <- pheasy 文本导出（FORCE_CONSTANTS/3RD）")
        return
    # 情形 B：phono3py 路径，从 hdf5 用 hiphive 转换
    import ase, h5py
    import phono3py
    from hiphive import ForceConstants
    def _load_fc(name, keys):
        # 数据集名兼容：pheasy 写 'fc2'/'fc3'；phono3py symfc 的 fc2 实际写成
        # phonopy full 格式 'force_constants'（无 'fc2'，fc3 仍是 'fc3'）。
        with h5py.File(name, "r") as _h:
            for _k in keys:
                if _k in _h:
                    return np.asarray(_h[_k][()])
        raise KeyError("%s: no %s dataset" % (name, "/".join(keys)))
    ph3 = phono3py.load(yaml, produce_fc=False, log_level=0)
    prim, sc = ph3.phonon_primitive, ph3.supercell
    fc2 = _load_fc("fc2.hdf5", ("fc2", "force_constants"))
    fc3 = _load_fc("fc3.hdf5", ("fc3", "force_constants"))
    prim_ase = ase.Atoms(symbols=prim.symbols, cell=prim.cell,
                         scaled_positions=prim.scaled_positions, pbc=True)
    sc_ase = ase.Atoms(symbols=sc.symbols, cell=sc.cell,
                       scaled_positions=sc.scaled_positions, pbc=True)
    sc2 = ph3.phonon_supercell
    if sc2 is None:
        sc2 = sc
    sc2_ase = ase.Atoms(symbols=sc2.symbols, cell=sc2.cell,
                        scaled_positions=sc2.scaled_positions, pbc=True)
    fcs2 = ForceConstants.from_arrays(sc2_ase, fc2_array=fc2)
    fcs = ForceConstants.from_arrays(sc_ase, fc3_array=fc3)
    fcs2.write_to_phonopy(str(sb / "FORCE_CONSTANTS_2ND"), format="text")
    fcs.write_to_shengBTE(str(sb / "FORCE_CONSTANTS_3RD"), prim_ase)
    print("[OK] shengbte/ <- fc2/fc3.hdf5（hiphive 转换导出）")
def main():
    cfg = json.loads(Path("fit_config.json").read_text(encoding="utf-8"))
    yaml = cfg["yaml"]
    software = cfg.get("software", "phono3py")
    method = cfg.get("method", "random")
    imag_thr = float(cfg.get("imag_thr", 0.10))
    supercell = cfg.get("supercell")
    fc2_supercell = cfg.get("fc2_supercell")
    use_nac = bool(cfg.get("nac", False))

    # ---- 1. 拟合 ----
    if software == "pheasy":
        p_method = str(cfg.get("pheasy_method", "OLS")).upper()
        c3 = str(cfg.get("c3_cutoff", "6.0"))
        p_bin = str(cfg.get("pheasy_bin", "pheasy"))
        _env = dict(os.environ)
        _env["PHEASY_BIN"] = p_bin
        rc = subprocess.run("python _pheasy_fit.py %s '%s'" % (p_method, c3),
                            shell=True, env=_env).returncode
        if rc != 0:
            sys.exit("[ERROR] pheasy 拟合失败(rc=%d)，看 fc_build.log" % rc)
        fit = ("pheasy-gpu" if p_bin == "pheasy-gpu" else "pheasy") + " (" + p_method + ")"
    else:
        fit = cfg.get("fit", "symfc")
        flag = {"sym-fc": "", "symfc": "--fc-calc symfc",
                "alm": "--fc-calc alm"}.get(fit)
        if flag is None:
            sys.exit("[ERROR] FIT 只允许 auto/sym-fc/symfc/alm")
        rc = subprocess.run("phono3py-load %s %s" % (yaml, flag), shell=True).returncode
        if rc != 0 and fit == "symfc":
            print("[WARN] symfc 拟合失败（看 fc_build.log；大超胞/numpy 2.x 下可能 segfault），"
                  "退回 alm 再试一次。alm 对超大超胞很慢（半小时起）——想避免就缩超胞"
                  "（MIN_SC_LEN 或显式 SUPERCELL），或项目级把 S3 的 FIT_SOFTWARE 设为 pheasy。")
            rc = subprocess.run("phono3py-load %s --fc-calc alm" % yaml,
                                shell=True).returncode
        if rc != 0:
            sys.exit("[ERROR] 力常数拟合失败(rc=%d)，看 fc_build.log" % rc)
    for f in ("fc2.hdf5", "fc3.hdf5"):
        if not os.path.isfile(f):
            sys.exit("[ERROR] 没生成 %s" % f)
    print("[OK] fc2.hdf5 / fc3.hdf5")
    try:
        export_shengbte(yaml)
    except Exception as exc:
        print("[WARN] 可选 ShengBTE 导出失败（S4 选用时会重新检查）：%s" % exc)

    # ---- 2. 虚频闸（phonopy API）----
    rc = subprocess.run("python _phonon_gate.py", shell=True,
                        capture_output=True, text=True)
    mf = None
    for ln in (rc.stdout or "").splitlines():
        if ln.startswith("MIN_FREQ_THZ"):
            try:
                mf = float(ln.split()[1])
            except (ValueError, IndexError):
                mf = None
    if rc.returncode != 0:
        mf = None
        print(rc.stdout or "")
        print(rc.stderr or "", file=sys.stderr)
    # 不回退到可能属于旧拟合的 band 文件；当前闸门失败必须阻止后续计算。
    if mf is None:
        stable, note = False, "phonopy 虚频闸失败（看 fc_build.log）"
        print("[FAIL] " + note)
    else:
        stable = mf >= -imag_thr
        note = "最小声子频率 %.4f THz（阈值 -%.2f）：%s" % (
            mf, imag_thr,
            "无明显虚频" if stable else "存在虚频，本势下动力学不稳定，κ 无物理意义")
        print("[%s] %s" % ("OK" if stable else "FAIL", note))

    # pheasy 质量门禁（alpha 撞边界）
    if os.path.isfile(".fit_gate_fail"):
        stable = False
        note = ("pheasy 拟合质量门禁未通过（alpha 撞网格边界），κ 已阻止。"
                "加大 N_RANDOM/OVERSAMPLE 重跑。")
        print("[FAIL] " + note)

    Path("phonon_summary.json").write_text(json.dumps(
        {"stable": bool(stable), "min_frequency_THz": mf, "method": method,
         "fit": fit, "nac": use_nac, "input_yaml": yaml,
         "supercell": supercell, "fc2_supercell": fc2_supercell, "note": note},
        ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    print("[DONE] %s：stable=%s" % ("step3_fc", str(stable).lower()))


if __name__ == "__main__":
    main()
