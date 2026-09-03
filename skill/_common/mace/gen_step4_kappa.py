#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_step4_kappa.py —— BTE 晶格热导率（step4_kappa），提交计算节点。

从 step3_fc 取 fc2/fc3（+BORN），按 klmace_params 的 MESH 组 phono3py-load --br 命令，
渲染提交模板。作业跑完就地抽 κ 张量写 kappa_summary.json（marker: KAPPA_DONE）。

MESH_SCAN：分号分隔的多套 q 网格，例如 "16 16 16; 20 20 20; 24 24 24"。
DFT 路线不敢做的收敛测试，在这条链上几乎白送——**κ 对 q 网格的收敛性是这类结果最
常被审稿人问的一条**，顺手扫出来比事后补便宜得多。扫描时每套网格的 κ 都进 summary。
"""
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import klmace_common as kc
import stepconf

OUTDIR = "step4_kappa"
STEP = "step4_kappa"
SRC = "step3_fc"

SPEC = {
    # ---- 全局 ----
    "MACE_MODEL": ("mace-mp:medium", "str"),
    "MACE_MODEL_DIR": ("", "str"),
    "DEVICE": ("auto", "str"),
    "DTYPE": ("float64", "str"),
    "CONDA_SH": (kc.DEFAULT_CONDA_SH, "str"),
    "CONDA_ENV": (kc.DEFAULT_CONDA_ENV, "str"),
    # ---- 本步 ----
    "MESH_OVERRIDE": (None, "str"),     # 空=用 step2 写进 klmace_params 的 MESH
    "MESH_SCAN": ("", "str"),           # "16 16 16; 20 20 20"，空=只跑一套
    "T_MIN": (100, "int"),
    "T_MAX": (800, "int"),
    "T_STEP": (100, "int"),
    "ISOTOPE": (True, "bool"),
    "BTE": ("rta", "str"),              # rta（--br）| lbte（--lbte，直接解，贵得多）
    "EXTRA_ARGS": ("", "str"),          # 原样附加，如 "--boundary-mfp 1e6" / "--write-gamma"
    # phono3py 网格点并行（--gp/--write-gamma/--read-gamma）：>=2 时把 q 网格均分 N 份，
    #   N 个 --write-gamma 作业并行算散射率，再 --read-gamma 收拢出 κ。单机多核白拿的加速，
    #   BTE 网格加密后尤其值得（DFT 路线同样适用）。=0/1 关闭，走单作业。
    "GP_SPLIT": (0, "int"),
    # MFP 累积 κ 诊断：加 --mfp 让 phono3py 写 kappa-mfp.hdf5，extract 把「累积 κ vs 声子
    #   自由程」与 50%/90% 累积处的 MFP 收进 kappa_summary.json —— 审稿人常问的热输运尺度。
    "MFP_CUMULATIVE": (False, "bool"),
    # 2D κ 厚度归一化：phono3py 用含真空的原胞体积做分母，2D 面内 κ 被 Lz 稀释；
    #   需乘 Lz/d。取法：vdw=原子z跨度+两侧vdW半径 | cell=用Lz(即不归一) | 数值=固定Å
    "KAPPA_2D_THICKNESS": ("vdw",  "str"),
    # 2D NAC：auto=2D默认不用3D-NAC(LO-TO在2D应趋零)/3D随BORN；on/off 强制
    "KAPPA_NAC":          ("auto", "str"),
}

# patch_vdw_shared：单一真源在 skill/_common/vdw_radii.py，ke-dft-cpu/kl-dft-cpu 读同一份。
#   两边算层厚用同一个公式 d = zspan + vdW(top) + vdW(bot)，表必须同源。
try:
    from vdw_radii import VDW_RADII as _VDW        # noqa: F401
except ImportError:
    _VDW = {"H":1.20,"B":1.92,"C":1.70,"N":1.55,"O":1.52,"F":1.47,"Na":2.27,"Mg":1.73,
            "Al":1.84,"Si":2.10,"P":1.80,"S":1.80,"Cl":1.75,"K":2.75,"Ca":2.31,
            "Ti":2.11,"V":2.07,"Cr":2.06,"Mn":2.05,"Fe":2.04,"Co":2.00,"Ni":1.97,
            "Cu":1.96,"Zn":2.01,"Ga":1.87,"Ge":2.11,"As":1.85,"Se":1.90,"Br":1.85,
            "Mo":2.17,"Ru":2.13,"Rh":2.10,"Pd":2.10,"Ag":2.11,"Cd":2.18,"In":1.93,
            "Sn":2.17,"Sb":2.06,"Te":2.06,"I":1.98,"W":2.18,"Pt":2.13,"Au":2.14,
            "Hg":2.23,"Tl":1.96,"Pb":2.02,"Bi":2.07}
    print("[WARN] 没找到 _common/vdw_radii.py，用内置最小表")


def _poscar_species(poscar):
    """从 POSCAR 读每个原子的元素符号（VASP5）；VASP4 无元素行则返回 None。"""
    import re as _re
    L = Path(poscar).read_text(encoding="utf-8-sig").splitlines()
    line6 = L[5].split()
    if not line6 or _re.fullmatch(r"[+-]?\d+", line6[0]):
        return None
    counts = [int(x) for x in L[6].split()]
    out = []
    for sym, n in zip(line6, counts):
        out += [sym] * n
    return out


def two_d_norm_factor(poscar, vac_axis, mode):
    """2D κ 厚度归一化因子 factor=Lz/d 与元数据。mode: vdw | cell | 数值(Å)。"""
    import numpy as np
    from dim_common import read_poscar_cell_frac
    lat, frac = read_poscar_cell_frac(poscar)
    lat = np.array(lat, float); frac = np.array(frac, float)
    ax = vac_axis if vac_axis is not None else 2
    Lz = float(np.linalg.norm(lat[ax]))
    axis_unit = lat[ax] / Lz
    proj = (frac @ lat) @ axis_unit
    zspan = float(proj.max() - proj.min()) if len(proj) else 0.0
    m = str(mode).strip().lower()
    try:
        d = float(m); conv = "fixed %.3f A" % d
    except ValueError:
        if m in ("cell", "lz", "none", ""):
            d = Lz; conv = "cell Lz (no norm)"
        else:
            sp = _poscar_species(poscar)
            if sp and len(sp) == len(proj):
                order = np.argsort(proj)
                bot, top = sp[int(order[0])], sp[int(order[-1])]
                d = zspan + _VDW.get(top, 2.0) + _VDW.get(bot, 2.0)
                conv = "zspan %.2f + vdW(%s,%s)" % (zspan, top, bot)
            else:
                d = Lz; conv = "cell Lz (no species -> fallback)"
    factor = Lz / d if d else 1.0
    meta = {"kappa_2d_norm_factor": round(factor, 5), "Lz_ang": round(Lz, 3),
            "thickness_d_ang": round(d, 3) if d else None,
            "thickness_convention": conv, "atomic_zspan_ang": round(zspan, 3),
            "note": "kappa_2d_normalized = kappa_raw * Lz/d（面内分量才有物理意义）"}
    return factor, meta

# 作业里抽 κ 的小脚本：把所有 kappa-m*.hdf5 都收进 summary（配合 MESH_SCAN）。
# factor!=1（2D）时每套网格额外给 kappa_2d_normalized（原始 κ × Lz/d）。
# 张量审计：读完整 3x3 κ 张量；按 spglib 空间群定晶系，对要求面内各向同性的晶系
# （hex/cubic/trigonal/tetragonal）用 kappa_validation 的敏感判据审 300K 面内块。
# MFP 累积 κ：--mfp 时 phono3py 写 kappa-mfp.hdf5，把累积分数与 50%/90% MFP 收进 summary。
def build_extract(factor, meta):
    import json as _json
    return (
        "python - <<'PY'\n"
        "import glob, json, os\n"
        "import numpy as np, h5py\n"
        "FACTOR=%r\n" % float(factor) +
        "META=json.loads(%r)\n" % _json.dumps(meta, ensure_ascii=False) +
        "out = {\"KAPPA_DONE\": False, \"runs\": []}\n"
        "out.update(META)\n"
        "# ---- 晶系判定 + 面内投影轴（2D 用真空方向，3D 用 None=原始 xy 块）----\n"
        "CRYSTAL, SG, PLANE = None, None, None\n"
        "try:\n"
        "    import spglib\n"
        "    from phonopy.interface.calculator import read_crystal_structure\n"
        "    from kappa_validation import crystal_system_from_spacegroup, audit_kappa_voigt\n"
        "    _cell, _ = read_crystal_structure(\"POSCAR\", interface_mode=\"vasp\")\n"
        "    _sg = spglib.get_spacegroup((_cell.cell, _cell.scaled_positions, _cell.numbers), symprec=1e-3)\n"
        "    CRYSTAL = crystal_system_from_spacegroup(int(_sg.split(\"(\")[-1].rstrip(\")\")))\n"
        "    SG = _sg\n"
        "    out[\"crystal_system\"] = CRYSTAL; out[\"spacegroup\"] = SG\n"
        "    if META.get(\"dim\") == \"2d\":\n"
        "        PLANE = np.array(_cell.cell, float)[int(META.get(\"vac_axis\", 2))]\n"
        "except Exception as _e:\n"
        "    out.setdefault(\"warnings\", []).append(\"kappa symmetry audit unavailable: %s\" % _e)\n"
        "def _audit(K3):\n"
        "    if not CRYSTAL:\n"
        "        return None\n"
        "    voigt = [K3[0,0], K3[1,1], K3[2,2], K3[1,2], K3[0,2], K3[0,1]]\n"
        "    aud = audit_kappa_voigt(voigt, crystal_system=CRYSTAL, plane_normal=PLANE)\n"
        "    return {k: aud[k] for k in (\"eigenvalue_ratio\", \"diagonal_relative_mismatch\",\n"
        "                                 \"offdiag_relative_magnitude\", \"determinant_ratio\",\n"
        "                                 \"threshold\", \"gate\", \"crystal_system\")}\n"
        "for f in sorted(glob.glob(\"kappa-m*.hdf5\")):\n"
        "    if \"kappa-mfp\" in f:\n"
        "        continue\n"
        "    try:\n"
        "        with h5py.File(f, \"r\") as h:\n"
        "            T = np.array(h[\"temperature\"]); Kv = np.array(h[\"kappa\"])\n"
        "        # phono3py kappa-m*.hdf5 的 kappa 是 (n_T,6) Voigt [xx,yy,zz,yz,xz,xy]，不是 (n_T,3,3)\n"
        "        raw3 = [[[float(Kv[i,0]),float(Kv[i,5]),float(Kv[i,4])],\n"
        "                 [float(Kv[i,5]),float(Kv[i,1]),float(Kv[i,3])],\n"
        "                 [float(Kv[i,4]),float(Kv[i,3]),float(Kv[i,2])]] for i in range(len(T))]\n"
        "        raw = [[float(Kv[i,0]),float(Kv[i,1]),float(Kv[i,2])] for i in range(len(T))]\n"
        "        rec = {\"file\": f, \"mesh\": os.path.basename(f)[7:].replace(\".hdf5\",\"\"),\n"
        "               \"temperatures\": T.tolist(), \"kappa_xx_yy_zz\": raw,\n"
        "               \"kappa_tensor_3x3\": raw3}\n"
        "        j = int(np.argmin(np.abs(T-300.0)))\n"
        "        rec[\"kappa_300K_xx_yy_zz\"] = raw[j]\n"
        "        rec[\"kappa_300K_tensor_3x3\"] = raw3[j]\n"
        "        aud = _audit(np.array(raw3[j]))\n"
        "        if aud:\n"
        "            rec[\"kappa_symmetry_audit_300K\"] = aud\n"
        "        if abs(FACTOR-1.0)>1e-9:\n"
        "            nrm=[[v*FACTOR for v in r] for r in raw]\n"
        "            rec[\"kappa_2d_normalized_xx_yy_zz\"]=nrm\n"
        "            rec[\"kappa_2d_normalized_300K_xx_yy_zz\"]=nrm[j]\n"
        "        out[\"runs\"].append(rec)\n"
        "    except Exception as e:\n"
        "        out.setdefault(\"errors\", []).append(\"%s: %s\" % (f, e))\n"
        "out[\"KAPPA_DONE\"] = bool(out[\"runs\"])\n"
        "if out[\"runs\"]:\n"
        "    r = out[\"runs\"][-1]\n"
        "    out[\"kappa_300K_xx_yy_zz\"] = r[\"kappa_300K_xx_yy_zz\"]\n"
        "    out[\"mesh_last\"] = r[\"mesh\"]\n"
        "    if \"kappa_2d_normalized_300K_xx_yy_zz\" in r:\n"
        "        out[\"kappa_2d_normalized_300K_xx_yy_zz\"]=r[\"kappa_2d_normalized_300K_xx_yy_zz\"]\n"
        "    if \"kappa_symmetry_audit_300K\" in r:\n"
        "        out[\"kappa_symmetry_audit_300K\"] = r[\"kappa_symmetry_audit_300K\"]\n"
        "# ---- MFP 累积 κ 诊断（MFP_CUMULATIVE/--mfp 时才有 kappa-mfp.hdf5）----\n"
        "for f in sorted(glob.glob(\"kappa-mfp*.hdf5\")):\n"
        "    try:\n"
        "        with h5py.File(f, \"r\") as h:\n"
        "            mfp = np.array(h[\"mfp\"]); km = np.array(h[\"kappa_mfp\"])\n"
        "            T = np.array(h[\"temperature\"])\n"
        "        j = int(np.argmin(np.abs(T-300.0)))\n"
        "        cum = np.array(km[j], float)\n"
        "        tot = np.trace(cum[-1]) / 3.0\n"
        "        frac = np.array([np.trace(c)/3.0/tot for c in cum]) if tot > 0 else np.zeros(len(cum))\n"
        "        def _mfp_at(pct):\n"
        "            idx = np.argmax(frac >= pct)\n"
        "            return float(mfp[idx]) if frac[idx] >= pct else None\n"
        "        out[\"mfp_cumulative_300K\"] = {\"file\": f,\n"
        "            \"mfp_ang\": [float(x) for x in mfp],\n"
        "            \"cumulative_fraction\": [float(x) for x in frac],\n"
        "            \"mfp_at_50pct_ang\": _mfp_at(0.5), \"mfp_at_90pct_ang\": _mfp_at(0.9)}\n"
        "    except Exception as e:\n"
        "        out.setdefault(\"errors\", []).append(\"mfp: %s\" % e)\n"
        "json.dump(out, open(\"kappa_summary.json\",\"w\"), ensure_ascii=False, indent=2)\n"
        "print(\"KAPPA_DONE\" if out[\"runs\"] else \"NO_KAPPA\")\n"
        "PY")



def meshes(conf, params):
    scan = str(conf["MESH_SCAN"] or "").strip()
    if scan:
        return [m.strip() for m in scan.split(";") if m.strip()]
    return [conf["MESH_OVERRIDE"] or params.get("MESH") or "24 24 24"]


def main():
    cwd = Path.cwd()
    out = cwd / OUTDIR
    out.mkdir(exist_ok=True)
    conf = stepconf.load(SPEC, STEP)
    src = cwd / SRC
    if not src.is_dir():
        sys.exit("[ERROR] 找不到 %s" % SRC)

    ps = src / "phonon_summary.json"
    if ps.is_file():
        import json
        try:
            if not json.loads(ps.read_text()).get("stable", True):
                sys.exit("[ERROR] step3 判定声子谱有虚频，κ 无物理意义，已中止。"
                         "先按 step3 的排查顺序处理。")
        except ValueError:
            pass

    for f in ("fc2.hdf5", "fc3.hdf5"):
        if not (src / f).is_file():
            sys.exit("[ERROR] %s 缺 %s（step3 没拟合成）" % (SRC, f))
        kc.link_or_copy(src / f, out / f)          # fc3.hdf5 可以上 GB，软链不复制
    yaml = ("phono3py_params.yaml" if (src / "phono3py_params.yaml").is_file()
            else "phono3py_disp.yaml")
    for f in (yaml, "BORN", "POSCAR", kc.KL_PARAMS, kc.METHOD_FILE):
        if (src / f).is_file():
            shutil.copyfile(str(src / f), str(out / f))
    use_nac = (out / "BORN").is_file()

    params = kc.read_kl_params(out / kc.KL_PARAMS)
    # 维度 + 2D NAC 门槛（phono3py 只有 3D 方案；2D 极性材料 LO-TO 在 q->0 应趋零，
    #   3D-NAC 是随真空变化的伪劈裂）。auto：2D 不用 NAC、3D 随 BORN；on/off 强制。
    dim = (params.get("DIM") or "").lower()
    try:
        _d, vac_axis = kc.resolve_dim(out / "POSCAR", dim or "auto")
        dim = dim or _d
    except Exception:
        vac_axis = None
    nac_mode = str(conf["KAPPA_NAC"] or "auto").strip().lower()
    if nac_mode in ("on", "true", "1", "yes"):
        pass
    elif nac_mode in ("off", "false", "0", "no"):
        use_nac = False
    elif dim == "2d" and use_nac:
        use_nac = False
        print("[WARN] 2D + KAPPA_NAC=auto：默认不对 2D 施加 phono3py 的 3D-NAC")
        print("       （LO-TO 在 2D 应趋零，3D 方案是随真空变化的伪劈裂）。")
        print("       要强制用设 KAPPA_NAC=on；正确的 2D-NAC 需用 QE 的 2D-DFPT。")

    ms = meshes(conf, params)
    ts = " ".join(str(t) for t in range(conf["T_MIN"], conf["T_MAX"] + 1, conf["T_STEP"]))
    bte = str(conf["BTE"] or "rta").lower()
    if bte not in ("rta", "lbte"):
        sys.exit("[ERROR] BTE 只允许 rta / lbte")
    solver = "--br" if bte == "rta" else "--lbte"
    print("[..] 求解=%s  网格=%s  温度=%d~%dK  NAC=%s  DIM=%s"
          % (bte, " | ".join(ms), conf["T_MIN"], conf["T_MAX"], use_nac, dim or "?"))

    # 2D κ 厚度归一化因子（3D 时 factor=1、不归一）
    factor, meta = 1.0, {"dim": dim or "?"}
    if dim == "2d":
        try:
            factor, m2 = two_d_norm_factor(out / "POSCAR", vac_axis, conf["KAPPA_2D_THICKNESS"])
            meta.update(m2)
            meta["vac_axis"] = vac_axis          # extract 用它投影面内块做 κ 对称审计
            print("[..] 2D κ 归一化：Lz=%.3f d=%.3f factor=Lz/d=%.4f (%s)"
                  % (meta["Lz_ang"], meta["thickness_d_ang"], factor, meta["thickness_convention"]))
        except Exception as e:
            print("[WARN] 2D 归一化因子算失败，只出原始 κ：%s" % e)
    if bte == "lbte" and len(ms) > 1:
        print("[WARN] LBTE 要存整个碰撞矩阵，内存 ~O(N_mode²)。多网格扫描请用 rta。")

    gp = int(conf["GP_SPLIT"] or 0)
    if gp >= 2:
        print("[..] GP_SPLIT=%d：每套网格拆 %d 个 --write-gamma 并行作业，再 --read-gamma 收拢"
              % (gp, gp))
    mfp_on = bool(conf["MFP_CUMULATIVE"])
    if mfp_on:
        print("[..] MFP_CUMULATIVE：加 --mfp，extract 收累积 κ vs 自由程诊断")

    def _base(m):
        """一条 phono3py-load 的公共前缀（网格/温度/同位素/NAC/MFP）。"""
        return '%s %s --mesh %s --ts="%s"%s%s%s' % (
            yaml, solver, m, ts,
            " --isotope" if conf["ISOTOPE"] else "",
            " --nac" if use_nac else "",
            " --mfp" if mfp_on else "")

    lines = []
    for m in ms:
        extra = str(conf["EXTRA_ARGS"] or "").strip()
        if gp >= 2:
            # 网格点并行：N 份 gamma 作业并行（各自日志），wait 齐后 --read-gamma 收拢。
            # 单份失败会被 collect 的缺 gamma 文件暴露，整链 fail fast。
            for i in range(gp):
                lines.append('phono3py-load %s --gp %d %d --write-gamma%s '
                             '> kappa_gp_%d_%d.log 2>&1 &'
                             % (_base(m), i, gp, (" " + extra) if extra else "", i, gp))
            lines.append('wait')
            lines.append('echo "[gp] mesh %s: %d gamma chunks done, collecting..."' % (m, gp))
            lines.append('phono3py-load %s --gp 0 %d --read-gamma%s 2>&1 '
                         '| tee -a phono3py_kappa.log'
                         % (_base(m), gp, (" " + extra) if extra else ""))
        else:
            lines.append('phono3py-load %s %s 2>&1 | tee -a phono3py_kappa.log'
                         % (_base(m), (" " + extra) if extra else ""))
    cmd = "\n".join(lines) + "\n" + build_extract(factor, meta)

    here = Path(__file__).resolve().parent
    # κ 对称审计模块在 extract 的计算节点进程里 import，必须随 submit.sh 一起推进
    # step 目录（gen_need 只服务本地 gen 阶段、不推送计算节点辅助脚本——
    # 对照 gen_step2 显式复制 mc_rattle.py/mc_rattle_disp.py 的做法）。
    for _aux in ("kappa_validation.py",):
        if (here / _aux).is_file():
            shutil.copyfile(str(here / _aux), str(out / _aux))
    tpl = kc.resolve_submit(here, "submit_p3py")
    kc.write_submit(tpl, out / "submit.sh",
                    {"JOBNAME": kc.new_jobname(cwd, "S4kappa"),
                     "CONDA_SH": conf["CONDA_SH"] or kc.DEFAULT_CONDA_SH,
                     "CONDA_ENV": conf["CONDA_ENV"] or kc.DEFAULT_CONDA_ENV,
                     "P3PY_CMD": cmd})
    stepconf.apply_submit(out / "submit.sh", conf.submit)
    print("[DONE] %s：submit.sh 就绪（%d 套网格），跑完写 kappa_summary.json"
          % (OUTDIR, len(ms)))


if __name__ == "__main__":
    main()
