#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_step6_kappa.py —— BTE 晶格热导率，提交计算节点（step6_kappa）。

从 step5_fc 拷 fc2/fc3/phono3py_disp.yaml/BORN，按 kl_params 的 MESH 组 BTE 命令，
渲染提交模板 → submit.sh，tf 提交到计算节点。成功后把 κ 张量写进 kappa_summary.json
并落 KAPPA_DONE（marker 判据）。
求解器（step.conf 的 SOLVER）：
  phono3py : phono3py-load --br（完整支持 findiff/alm + NAC，默认）
  shengbte : 写 ShengBTE CONTROL（复用参考引擎例程）。注意 fc3→ShengBTE 导出仅
             random/hiphive 路线可靠，findiff 的 compact fc3 无稳定导出口——solver=shengbte
             建议配 METHOD=alm，且需在集群装好 ShengBTE、把 exe 填进 step.conf。
产出目录：step6_kappa/
"""
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kl_common as kc
import stepconf

OUTDIR = "step6_kappa"
STEP   = "step6_kappa"
FC_DIR = "step5_fc"

SPEC = {
    "FUNC":        ("pbesol", "str"),  # 全局 step.conf 带入，本步不用
    "SOLVER":       ("phono3py", "str"),  # phono3py | shengbte（= 热导率计算软件）
    "BTE_METHOD":   ("rta",     "str"),   # phono3py 路：rta(--br) | lbte(--lbte)
    "MESH_OVERRIDE": (None,     "str"),   # 空=用 step4 写入 kl_params 的 MESH
    "T_MIN":        (100,       "int"),
    "T_MAX":        (800,       "int"),
    "T_STEP":       (100,       "int"),
    "ISOTOPE":      (True,      "bool"),
    "SCALEBROAD":   (0.1,       "float"), # shengbte 展宽
    "SHENGBTE_EXE": ("ShengBTE", "str"),
    # 2D κ 厚度归一化：phono3py 用含真空的原胞体积做分母，2D 面内 κ 被 Lz 稀释，
    #   需乘 Lz/d（d=有效厚度）。取法：vdw=原子z跨度+两侧vdW半径 | cell=用Lz(即不归一) | 数值=固定Å
    "KAPPA_2D_THICKNESS": ("vdw",  "str"),
    # 2D NAC 覆盖：auto=2D默认不用3D-NAC(LO-TO在2D应趋零)/3D随BORN；on=强制用；off=强制不用
    "KAPPA_NAC":          ("auto", "str"),
}

# patch_vdw_shared：单一真源在 skill/_common/vdw_radii.py，ke-dft-cpu 读同一份。
#   两边算层厚用同一个公式 d = zspan + vdW(top) + vdW(bot)，表必须同源。
try:
    from vdw_radii import VDW_RADII as _VDW
except ImportError:
    _VDW = {
        "H": 1.20, "Li": 1.82, "Be": 1.53, "B": 1.92, "C": 1.70, "N": 1.55,
        "O": 1.52, "F": 1.47, "Na": 2.27, "Mg": 1.73, "Al": 1.84, "Si": 2.10,
        "P": 1.80, "S": 1.80, "Cl": 1.75, "K": 2.75, "Ca": 2.31, "Ti": 2.11,
        "V": 2.07, "Cr": 2.06, "Mn": 2.05, "Fe": 2.04, "Co": 2.00, "Ni": 1.97,
        "Cu": 1.96, "Zn": 2.01, "Ga": 1.87, "Ge": 2.11, "As": 1.85, "Se": 1.90,
        "Br": 1.85, "Mo": 2.17, "Ru": 2.13, "Rh": 2.10, "Pd": 2.10, "Ag": 2.11,
        "Cd": 2.18, "In": 1.93, "Sn": 2.17, "Sb": 2.06, "Te": 2.06, "I": 1.98,
        "W": 2.18, "Pt": 2.13, "Au": 2.14, "Hg": 2.23, "Tl": 1.96, "Pb": 2.02,
        "Bi": 2.07,
    }
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
    proj = (frac @ lat) @ axis_unit           # 原子沿真空轴的笛卡尔投影
    zspan = float(proj.max() - proj.min()) if len(proj) else 0.0
    m = str(mode).strip().lower()
    try:
        d = float(m); conv = "fixed %.3f A" % d
    except ValueError:
        if m in ("cell", "lz", "none", ""):
            d = Lz; conv = "cell Lz (no norm)"
        else:  # vdw
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


def build_extract(factor, meta):
    """phono3py 跑完后就地抽 κ 到 kappa_summary.json（计算节点 conda 里执行）。
    factor!=1 时额外写 kappa_2d_normalized（原始 κ × Lz/d）。"""
    import json as _json
    return (
        "python - <<'PY'\n"
        "import glob,json,h5py,numpy as np\n"
        "FACTOR=%r\n" % float(factor) +
        "META=json.loads(%r)\n" % _json.dumps(meta, ensure_ascii=False) +
        "fs=sorted(glob.glob('kappa-m*.hdf5'))\n"
        "d={'KAPPA_DONE':bool(fs)}\n"
        "d.update(META)\n"
        "if fs:\n"
        "    with h5py.File(fs[-1],'r') as f:\n"
        "        T=np.array(f['temperature']); K=np.array(f['kappa'])\n"
        "        d['file']=fs[-1]; d['temperatures']=T.tolist()\n"
        "        raw=[[float(K[i,0]),float(K[i,1]),float(K[i,2])] for i in range(len(T))]\n"
        "        d['kappa_xx_yy_zz']=raw\n"
        "        if abs(FACTOR-1.0)>1e-9:\n"
        "            d['kappa_2d_normalized_xx_yy_zz']=[[v*FACTOR for v in r] for r in raw]\n"
        "json.dump(d,open('kappa_summary.json','w'),ensure_ascii=False,indent=2)\n"
        "print('KAPPA_DONE' if fs else 'NO_KAPPA')\n"
        "PY")


def build_phono3py_cmd(mesh, ts, isotope, use_nac, extract, bte="rta"):
    method = "--lbte" if str(bte).lower() == "lbte" else "--br"
    # NAC：有 nac_params/BORN 就默认启用；无 --nac 开关（会被当 --nac-method），要关才 --nonac。
    #   phono3py 3.24/4.x 行为一致。use_nac=True 就默认带上（不加开关），否则显式 --nonac。
    nac_flag = "" if use_nac else " --nonac"
    # phono3py 4.x：phono3py-load 是 phono3py 的 deprecated 别名，直接用 phono3py（读 disp.yaml 跑 BTE）。
    p3 = ('phono3py phono3py_disp.yaml %s --mesh %s --ts="%s"%s%s'
          % (method, mesh, ts, " --isotope" if isotope else "", nac_flag))
    return "%s 2>&1 | tee phono3py_kappa.log\n%s" % (p3, extract)


def prepare_shengbte(cwd, out, sb_src, conf, mesh, use_nac):
    """S5 已把 FORCE_CONSTANTS_2ND/3RD 导到 step5_fc/shengbte/；这里拷进 step6，
    再按本步运行参数（T/mesh/scalebroad/NAC）写 CONTROL。CONTROL 依赖运行参数，
    故归 S6 生成，不在 S5 固化。"""
    try:
        import lattice_kappa as lk
        from ase.io import read as ase_read
    except Exception as e:
        sys.exit("[ERROR] shengbte 需要 lattice_kappa/ase：%s" % e)

    need = ("FORCE_CONSTANTS_2ND", "FORCE_CONSTANTS_3RD")
    missing = [f for f in need if not (sb_src / f).is_file()]
    if missing:
        sys.exit("[ERROR] SOLVER=shengbte 但 %s 缺 %s。\n"
                 "        请确认 S5_fc 的 step.conf 里 EXPORT_SHENGBTE=true（且 hiphive 可用），"
                 "重跑 S5_fc 后再来。" % (sb_src, ", ".join(missing)))
    for f in need:
        shutil.copyfile(sb_src / f, out / f)
    # POSCAR：优先用 shengbte 导出时的原胞（与力常数同源）
    src_pos = sb_src / "POSCAR" if (sb_src / "POSCAR").is_file() else (out / "POSCAR")
    atoms = ase_read(str(src_pos), format="vasp")
    shutil.copyfile(src_pos, out / "POSCAR")

    params = kc.read_kl_params(out / kc.KL_PARAMS)
    sc = [int(x) for x in (params.get("SUPERCELL") or "2 2 2").split()]
    C = {"kappa_mesh": [int(x) for x in mesh.split()],
         "kappa_t_min": conf["T_MIN"], "kappa_t_max": conf["T_MAX"],
         "kappa_t_step": conf["T_STEP"], "kappa_scalebroad": conf["SCALEBROAD"],
         "kappa_isotope": conf["ISOTOPE"], "kappa_convergence": True}
    lk._write_shengbte_control(C, atoms, sc, out / "CONTROL", use_nac)
    print("[OK] ShengBTE 输入就绪：FORCE_CONSTANTS_2ND/3RD（拷自 S5）+ CONTROL")
    if use_nac:
        print("[WARN] CONTROL 已置 nonanalytic=T，但未自动写 born/epsilon；"
              "极性材料请手动在 CONTROL 补 Born 有效电荷与介电张量。")


def main():
    cwd = Path.cwd()
    out = cwd / OUTDIR
    out.mkdir(exist_ok=True)
    conf = stepconf.load(SPEC, STEP)
    fcd = cwd / FC_DIR                 # step5_fc
    p3d = fcd / "phono3py"            # S5 产出的 phono3py 格式子目录
    sbd = fcd / "shengbte"           # S5 产出的 shengbte 力常数子目录
    if not fcd.is_dir():
        sys.exit("[ERROR] 找不到 step5_fc")
    if not p3d.is_dir():
        sys.exit("[ERROR] 找不到 step5_fc/phono3py（S5 拟合未完成或为旧版布局）")

    for f in ("fc2.hdf5", "fc3.hdf5", "phono3py_disp.yaml"):
        if not (p3d / f).is_file():
            sys.exit("[ERROR] %s 缺 %s（step5 力常数没建成）" % (p3d, f))
        shutil.copyfile(p3d / f, out / f)
    if (p3d / "BORN").is_file():
        shutil.copyfile(p3d / "BORN", out / "BORN")
    for f in ("POSCAR", kc.KL_PARAMS):
        src = p3d / f if (p3d / f).is_file() else fcd / f
        if src.is_file():
            shutil.copyfile(src, out / f)
    use_nac = (out / "BORN").is_file()

    # 维度 + 2D NAC 门槛：phono3py 只有 3D-Wang/Gonze 方案，对真 2D 是近似
    #   （2D 极性材料 LO-TO 在 q->0 应趋零，3D 方案给的是随真空变化的伪劈裂）。
    #   auto（默认）：2D 不用 NAC，3D 随 BORN；on/off 强制。正确的 2D-NAC 在 QE。
    params0 = kc.read_kl_params(out / kc.KL_PARAMS)
    dim = (params0.get("DIM") or "").lower()
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
        print("       要强制用请设 KAPPA_NAC=on；正确的 2D-NAC 需用 QE 的 2D-DFPT。")

    # 稳定性闸：step5 判过虚频才该到这（phonon_summary.json 在 step5_fc 根）
    ps = fcd / "phonon_summary.json"
    if ps.is_file():
        import json
        try:
            if not json.loads(ps.read_text()).get("stable", True):
                sys.exit("[ERROR] step5 判定声子谱有虚频（不稳定），热导率无物理意义，已中止。")
        except Exception:
            pass

    params = kc.read_kl_params(out / kc.KL_PARAMS)
    mesh = conf["MESH_OVERRIDE"] or params.get("MESH") or "20 20 20"
    ts = " ".join(str(t) for t in range(conf["T_MIN"], conf["T_MAX"] + 1, conf["T_STEP"]))
    solver = str(conf["SOLVER"]).lower()
    print("[..] 求解器=%s mesh=%s 温度=%s K NAC=%s DIM=%s" % (solver, mesh, ts, use_nac, dim or "?"))

    # 2D κ 厚度归一化因子（3D 时 factor=1、不归一）
    factor, meta = 1.0, {"dim": dim or "?"}
    if dim == "2d":
        try:
            factor, m2 = two_d_norm_factor(out / "POSCAR", vac_axis, conf["KAPPA_2D_THICKNESS"])
            meta.update(m2)
            print("[..] 2D κ 归一化：Lz=%.3f d=%.3f factor=Lz/d=%.4f (%s)"
                  % (meta["Lz_ang"], meta["thickness_d_ang"], factor, meta["thickness_convention"]))
        except Exception as e:
            print("[WARN] 2D 归一化因子算失败，只出原始 κ：%s" % e)

    here = Path(__file__).resolve().parent
    if solver == "phono3py":
        cmd = build_phono3py_cmd(mesh, ts, conf["ISOTOPE"], use_nac,
                                 build_extract(factor, meta), conf["BTE_METHOD"])
        print("[..] BTE 方法=%s" % str(conf["BTE_METHOD"]).lower())
        tpl = kc.resolve_submit(here, "3d", "submit_p3py")   # 单节点，无 2D/3D 之分
        kc.write_submit(tpl, out / "submit.sh",
                        {"JOBNAME": kc.new_jobname(cwd, "S6kappa"), "P3PY_CMD": cmd})
    elif solver == "shengbte":
        prepare_shengbte(cwd, out, sbd, conf, mesh, use_nac)
        tpl = kc.resolve_submit(here, "3d", "submit_shengbte")
        kc.write_submit(tpl, out / "submit.sh",
                        {"JOBNAME": kc.new_jobname(cwd, "S6kappa"),
                         "SHENGBTE_EXE": conf["SHENGBTE_EXE"]})
    else:
        sys.exit("[ERROR] SOLVER 只允许 phono3py / shengbte")
    stepconf.apply_submit(out / "submit.sh", conf.submit)
    print("[DONE] %s：submit.sh 就绪，提交后计算节点出 κ，写 kappa_summary.json(KAPPA_DONE)"
          % OUTDIR)


if __name__ == "__main__":
    main()