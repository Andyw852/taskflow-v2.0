#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_step5_label.py —— mlff-mace step5：DFT 单点标注（fanout cfg-*，唯一昂贵一步）。

运行位置：超算登录节点，cwd = 材料目录；为每个待标注构型造一个 cfg-* 子目录
（POSCAR + INCAR + KPOINTS + POTCAR + submit.sh + MAGMOM），tf 自动按 fanout 提交。

操作定则（必须遵守）：
    ★ 只用 retry 或 start -f，绝不用 rerun / clean —— 后两者会 rm -rf 步骤目录，
      毁掉已经算完的 DFT 帧。retry 会先 scancel 在跑的作业再重跑 gen（gen 幂等，
      不删文件、不碰已算完的 cfg-*）；单独补某几帧就进对应 cfg-* 目录手工 sbatch。

DFT 设置（§8.1，从 step1 输出自动推导）：
    PREC=Accurate；ENCUT=ceil(1.5×ENMAX)（ENCUT_OVERRIDE 可覆盖）
    ISMEAR/SIGMA：step1 EIGENVAL 带隙 > 0.1 eV → 0/0.05，否则 1/0.2
    ISPIN/MAGMOM：step1 末次磁矩 max|m| > 0.1 μB → 2 + 逐原子继承（超胞按
                  image-major 展开，磁性体系绝不许用 VASP 默认初值）
    EDIFF=1E-7、LREAL=.FALSE.、ALGO=Normal、NELM=200、ISYM=0、IBRION=-1、NSW=0、
    LWAVE/LCHARG=.FALSE.、LASPH=.TRUE.、LMAXMIX 按元素（d→4 f→6 否则 2）、
    不许出现 MAXMIX；KPAR 从 IBZKPT 推（Γ-only 强制 1）、NCORE 默认 4；
    U 沿用 step1 的 LDAU 设置（与 relax 完全一致，U_OVERRIDE 计入指纹）
    KPOINTS：默认 Γ-only（超胞取力标准做法），KPOINTS_GRID 可显式覆盖
    12 核：step.conf [submit] nodes=1 ntasks_per_node=12

幂等：已算完（OUTCAR 完整）的 cfg-* 目录原样跳过；没算完的重建输入文件、不清 OUTCAR。
退出码 0 成功；非 0 = [ERROR]。
"""
import json
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dft_settings as ds
import mlff_common as mc
import stepconf

OUTDIR = "step5_label"
STEP = "step5_label"
STEP4 = "step4_genstruct"


def outcar_done(path):
    p = Path(path)
    if not p.is_file():
        return False
    try:
        with open(p, "rb") as fh:
            try:
                fh.seek(-200000, 2)
            except OSError:
                fh.seek(0)
            tail = fh.read().decode("utf-8", "ignore")
    except OSError:
        return False
    return "General timing and accounting informations" in tail


def write_incar(out, vals, comment_lines=()):
    """把字典写成 INCAR（保证标签顺序稳定、无 MAXMIX）。"""
    order = ["SYSTEM", "PREC", "ENCUT", "GGA", "IVDW", "ISMEAR", "SIGMA",
             "ISPIN", "MAGMOM", "LREAL", "LASPH", "LMAXMIX", "EDIFF", "NELM",
             "ALGO", "ISYM", "IBRION", "NSW", "LWAVE", "LCHARG", "KPAR",
             "NCORE", "LDAU", "LDAUTYPE", "LDAUL", "LDAUU", "LDAUJ"]
    lines = ["# INCAR 由 gen_step5_label.py 按 step1 输出自动生成（勿手改，改 step.conf）"]
    lines += ["# " + c for c in comment_lines]
    for k in order:
        if k in vals and vals[k] not in (None, ""):
            lines.append("%-8s = %s" % (k, vals[k]))
    Path(out).write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def write_kpoints(out, grid):
    if grid in (None, ""):
        lines = ["Gamma-only（mlff-mace 超胞取力标准做法；KPOINTS_GRID 可覆盖）",
                 "0", "Gamma", "1 1 1"]
    else:
        g = [int(x) for x in str(grid).split()]
        if len(g) != 3:
            sys.exit("[ERROR] KPOINTS_GRID 要三个整数，收到 %r" % grid)
        lines = ["Automatic mesh（KPOINTS_GRID=%s）" % grid,
                 "0", "Gamma", " ".join(map(str, g))]
    Path(out).write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def main():
    cwd = Path.cwd()
    out = cwd / OUTDIR
    out.mkdir(exist_ok=True)
    spec = dict(mc.SHARED_PARAM_SPEC)
    spec.update({"STEP": (STEP, "str")})
    conf = stepconf.load(spec, STEP)
    cv = dict(conf.params)

    # ---- 上游接力（§13：显式检查，绝不拿旧文件默默往下算）----
    man_path = cwd / STEP4 / "struct_manifest.json"
    if not man_path.is_file():
        sys.exit("[ERROR] %s 不存在 —— 先让 S4 生成结构。" % man_path)
    for need in ("step1_relax/OUTCAR", "step1_relax/INCAR", "step1_relax/POTCAR",
                 "step1_relax/EIGENVAL", "step1_relax/KPOINTS"):
        if not (cwd / need).is_file():
            sys.exit("[ERROR] 缺 %s —— step1_relax 没算完？" % need)
    man = json.loads(man_path.read_text())
    gen = man["generation"]

    # ---- DFT 设置（从 step1 输出推导）----
    incar1 = ds.read_incar(cwd / "step1_relax" / "INCAR")
    encut, encut_note = ds.encut_from_potcar(cwd / "step1_relax" / "POTCAR", 1.5,
                                             cv["ENCUT_OVERRIDE"])
    gap, gap_note = ds.read_bandgap(cwd / "step1_relax" / "EIGENVAL")
    ismear, sigma, sm_note = ds.decide_ismear(gap, gap_note)
    moments, m_note = ds.read_magmom(cwd / "step1_relax" / "OUTCAR")
    ispin, ispin_note = ds.magnetic_setting(moments)
    lmaxmix, lmm_note = ds.decide_lmaxmix(man["elements"])
    method = mc.read_kv(cwd / "step1_relax" / mc.METHOD_FILE)
    func = method.get("FUNC", "?")
    print("[..] ENCUT=%d（%s）  ISMEAR=%s SIGMA=%s（%s）  ISPIN=%s（%s）  LMAXMIX=%d"
          % (encut, encut_note, ismear, sigma, sm_note, ispin, ispin_note, lmaxmix))

    # [FIX-F2] 金属 + Γ-only 守卫：ISMEAR=1 说明 step1 判定为金属/半金属。
    # 金属的费米面对 k 点采样极敏感，~100 原子超胞只取 Γ 点会让力带上系统误差，
    # 而这个误差会原样进 fc2/fc3。半导体 Γ-only 没问题，金属必须显式给网格。
    if str(ismear).strip() == "1" and not (cv["KPOINTS_GRID"] or "").strip():
        sys.exit(
            "[ERROR] step1 判定本体系为金属（%s），但 KPOINTS_GRID 为空 = Γ-only。\n"
            "        金属超胞单点只取 Γ 点，力会有系统误差并原样进 fc2/fc3。\n"
            "        请显式设网格再重跑，例如：\n"
            "          tf -tt mlff-mace -p <材料> -j 5 conf --set params.KPOINTS_GRID='2 2 2'\n"
            "        （2D 体系真空方向恒为 1，如 '2 2 1'）\n"
            "        确认要用 Γ-only 就把 KPOINTS_GRID 显式写成 '1 1 1'。" % sm_note)

    # LDAU 从 step1 INCAR 原样继承（与弛豫完全一致）
    ldau = {k: incar1[k] for k in
            ("LDAU", "LDAUTYPE", "LDAUL", "LDAUU", "LDAUJ") if k in incar1}
    u_note = ("LDAU=%s LDAUU=%s" % (ldau.get("LDAU", ".FALSE."),
                                    ldau.get("LDAUU", ""))) if ldau else "off"

    base = {"PREC": "Accurate", "ENCUT": str(encut),
            "ISMEAR": ismear, "SIGMA": sigma,
            "ISPIN": ispin, "LREAL": ".FALSE.", "LASPH": ".TRUE.",
            "LMAXMIX": str(lmaxmix), "EDIFF": "%g" % cv["EDIFF"],
            "NELM": "200", "ALGO": str(cv["ALGO"] or "Normal"),
            "ISYM": "0", "IBRION": "-1", "NSW": "0",
            "LWAVE": ".FALSE.", "LCHARG": ".FALSE.",
            "NCORE": str(cv["NCORE"])}
    base.update(ldau)
    base["GGA"] = incar1.get("GGA", "")
    base["IVDW"] = incar1.get("IVDW", "")

    # ---- POTCAR 元素顺序 = step1；超胞 POSCAR 由 S4 按同一顺序写出 ----
    potcar = (cwd / "step1_relax" / "POTCAR").read_text(errors="ignore")

    here = Path(__file__).resolve().parent
    n_new, n_done = 0, 0
    for ent in man["frames"]:
        cfgdir = out / ent["id"]
        poscar_src = cwd / STEP4 / ("gen-%d" % ent["gen"]) / ent["file"]
        if not poscar_src.is_file():
            sys.exit("[ERROR] 清单里的结构文件不存在：%s" % poscar_src)
        # 结构变更守卫：同一 cfg id 换过结构（如改 REF_DISP）时，旧 OUTCAR 是脏的，
        # 必须清掉重算（标签错了比多算一帧贵得多）
        import hashlib as _hl
        md5 = _hl.md5(poscar_src.read_bytes()).hexdigest()
        stamp = cfgdir / ".poscar_md5"
        old_poscar = cfgdir / "POSCAR"
        stale = (old_poscar.is_file() and
                 _hl.md5(old_poscar.read_bytes()).hexdigest() != md5)
        if outcar_done(cfgdir / "OUTCAR") and not stale:
            n_done += 1
            continue
        if stale:
            for _f in cfgdir.glob("*"):
                if _f.is_file() and _f.name not in ("submit.sh",):
                    _f.unlink()
            print("[..] %s 结构已变（REF_DISP/应变改了？），旧产物已清，重算" % ent["id"])
        cfgdir.mkdir(exist_ok=True)
        shutil.copyfile(str(poscar_src), str(cfgdir / "POSCAR"))
        stamp.write_text(md5, encoding="utf-8")
        # MAGMOM（磁性体系逐原子继承；iso 帧有自己的）
        mag_file = cwd / STEP4 / ("gen-%d" % ent["gen"]) / \
            ent["file"].replace(".poscar", ".magmom")
        vals = conf.apply_incar({}, base)   # [incar]→自动推导→[incar.final]→删除
        if mag_file.is_file():
            vals["MAGMOM"] = mag_file.read_text().split("=", 1)[1].strip()
        comment = ["%s  %s  gen=%d  strain_factor=%s  volume_factor=%s  rattle_std=%s"
                   % (ent["id"], ent["config_type"], ent["gen"],
                      ent.get("strain_factor"), ent.get("volume_factor"),
                      ent.get("rattle_std")),
                   "DFT 设置来源：step1 输出推导（FUNC=%s，%s）" % (func, u_note),
                   "ENCUT=%d（%s）" % (encut, encut_note)]
        write_incar(cfgdir / "INCAR", vals, comment)
        if ent["config_type"] == "iso":
            write_kpoints(cfgdir / "KPOINTS", None)      # 孤立原子 Γ-only
            vals2 = dict(vals)
            vals2["KPAR"] = "1"
        else:
            write_kpoints(cfgdir / "KPOINTS", cv["KPOINTS_GRID"])
            # KPAR：Γ-only 强制 1；显式网格按 IBZKPT 逻辑简化为 1（12 核小作业）
            vals2 = dict(vals)
            vals2["KPAR"] = "1"
        # KPAR/NCORE 写进 INCAR（补在末尾，稳定顺序）
        incar_lines = (cfgdir / "INCAR").read_text().splitlines()
        incar_lines += ["KPAR    = %s   # 12核：从 IBZKPT 推（Γ-only 强制 1）" % vals2["KPAR"]]
        (cfgdir / "INCAR").write_text("\n".join(incar_lines) + "\n",
                                      encoding="utf-8", newline="\n")
        (cfgdir / "POTCAR").write_text(potcar, encoding="utf-8")

        # ---- submit.sh（12 核来自 step.conf [submit]）----
        dim = man["dim"]
        tpl = mc.resolve_submit(here, "submit_std", dim)
        mc.write_submit(tpl, cfgdir / "submit.sh",
                        {"JOBNAME": mc.new_jobname(cwd, ent["id"].replace("cfg-", "l"),
                                                   tag="mlff")[:80]})
        stepconf.apply_submit(cfgdir / "submit.sh", conf.submit)
        n_new += 1
    print("[DONE] %s：本代 %d 帧，已算完 %d，新生成 %d 个 cfg 目录（12 核提交）"
          % (OUTDIR, man["n_frames"], n_done, n_new))


if __name__ == "__main__":
    main()
