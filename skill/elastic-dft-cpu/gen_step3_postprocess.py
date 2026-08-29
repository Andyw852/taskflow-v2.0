#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_step3_postprocess.py — 弹性/力学性能流程 step3：本地后处理 + 出图（不提交 SLURM）

类比 bd 的 plot 步：在材料父目录直接运行，读 step2_elastic 的 OUTCAR + 结构，
用 pymatgen 解析弹性张量、推导力学性能并画各向异性图，产物落在 step3_postprocess/，
最后向 stdout 打印一行 JSON（供 agent 解析），过程日志走 stderr。

维度自适应：优先读 workflow_method.txt 的 DIM=，缺失时按真空层自动判定。
  3D → convert_to_ieee + VRH(B/G/E/ν) + Born + Pugh/Cauchy/硬度/各向异性/Debye/声速/
       最小热导；出图=E 在 (001)/(010)/(100) 三截面的方向依赖极坐标图。
  2D → 抽面内 3×3 子块 × 真空轴胞高换算 2D 刚度(N/m) + Y_2D/ν_2D + 2D Born；
       出图=面内 E_2D(θ) 与 ν_2D(θ) 极坐标图。

退出码：0 成功 / 40 后处理失败（缺 OUTCAR/张量、力学不稳定）。

用法：
    python gen_step3_postprocess.py
    python gen_step3_postprocess.py --step2-dir step2_elastic
"""
import sys
import json
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dim_common import require_dim, read_dim, detect_dimension, AXIS_NAMES  # noqa: E402

# =====================================================================
#                           用户配置区
#   （step3 为本地后处理，不提交 SLURM，故无 SUBMIT_OVERRIDE；
#     以下为后处理/出图的可调注入参数，默认即当前值，可自由修改。）
# =====================================================================

STEP2_DIR_DEFAULT = "step2_elastic"   # 上一步目录
FORCE_DIM = "auto"        # "auto"(继承/判定) | "2d" | "3d" 强制指定维度
SYMPREC = 0.01            # 晶系识别容差（Born 判据分支用）

# ---- 出图 ----
MAKE_PLOTS = True         # 是否画各向异性图（需 matplotlib）
PLOT_DPI = 150
PLOT_NPTS = 361           # 角度采样点数
PLOT_3D_PLANES = {        # 3D：画哪些晶面截面的方向杨氏模量（法向定义平面）
    "(001) xy": (0, 1),   # 平面由两个笛卡尔轴张成：0=x,1=y,2=z
    "(010) xz": (0, 2),
    "(100) yz": (1, 2),
}

# ---- 硬度经验模型（3D）----
HARDNESS_MODELS = ("chen2011", "tian2012", "teter1998")

# =====================================================================
#                         用户配置区结束
# =====================================================================

STEP = "step3_postprocess"
METHOD_FILE = "workflow_method.txt"

# VASP 顺序 [xx,yy,zz,xy,yz,zx] -> 标准 Voigt [xx,yy,zz,yz,zx,xy]
_VASP2VOIGT = [0, 1, 2, 4, 5, 3]


def log(*a):
    print("[mech]", *a, file=sys.stderr, flush=True)


def emit(rec, code):
    print(json.dumps(rec, ensure_ascii=False), flush=True)
    sys.exit(code)


def parse_total_elastic(outcar):
    import re
    import numpy as np
    lines = Path(outcar).read_text(errors="ignore").splitlines()
    idx = [i for i, l in enumerate(lines) if "TOTAL ELASTIC MODULI" in l]
    if not idx:
        return None
    row_re = re.compile(
        r"\s*(XX|YY|ZZ|XY|YZ|ZX)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)"
        r"\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)")
    rows = []
    for l in lines[idx[-1] + 1:]:
        m = row_re.match(l)
        if m:
            rows.append([float(x) for x in m.groups()[1:]])
        if len(rows) == 6:
            break
    if len(rows) != 6:
        return None
    V = np.array(rows) / 10.0
    return V[np.ix_(_VASP2VOIGT, _VASP2VOIGT)]


def hardness_models(B, G):
    k = G / B
    allv = {
        "chen2011": round(2 * (k ** 2 * G) ** 0.585 - 3, 2),
        "tian2012": round(0.92 * (k ** 1.137) * (G ** 0.708), 2),
        "teter1998": round(0.151 * G, 2),
    }
    return {m: allv[m] for m in HARDNESS_MODELS if m in allv}


def born_stable_3d(C, crystal_system):
    import numpy as np
    cs = (crystal_system or "").lower()

    def g(i, j):
        return float(C[i - 1, j - 1])
    checks = {}
    eig = np.linalg.eigvalsh(C)
    checks["eigenvalues_positive"] = bool(np.all(eig > 0))
    if cs == "cubic":
        checks["C11>|C12|"] = g(1, 1) > abs(g(1, 2))
        checks["C11+2C12>0"] = g(1, 1) + 2 * g(1, 2) > 0
        checks["C44>0"] = g(4, 4) > 0
    elif cs == "tetragonal":
        checks["C11>|C12|"] = g(1, 1) > abs(g(1, 2))
        checks["2C13^2<C33(C11+C12)"] = 2 * g(1, 3) ** 2 < g(3, 3) * (g(1, 1) + g(1, 2))
        checks["C44>0"] = g(4, 4) > 0
        checks["C66>0"] = g(6, 6) > 0
    elif cs in ("hexagonal", "trigonal", "rhombohedral"):
        checks["C11>|C12|"] = g(1, 1) > abs(g(1, 2))
        checks["2C13^2<C33(C11+C12)"] = 2 * g(1, 3) ** 2 < g(3, 3) * (g(1, 1) + g(1, 2))
        checks["C44>0"] = g(4, 4) > 0
    elif cs == "orthorhombic":
        for k in ("C11", "C22", "C33", "C44", "C55", "C66"):
            i = int(k[1]); checks[f"{k}>0"] = g(i, i) > 0
        checks["C11C22>C12^2"] = g(1, 1) * g(2, 2) > g(1, 2) ** 2
    checks["min_eigenvalue_GPa"] = round(float(eig.min()), 3)
    return all(v for v in checks.values() if isinstance(v, bool)), checks


def plot_3d_young(C_ieee, outdir):
    """3D：方向杨氏模量 E(n)=1/(s_ijkl n n n n)，画配置的若干晶面截面极坐标图。"""
    try:
        import numpy as np
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from pymatgen.analysis.elasticity.elastic import ComplianceTensor
    except Exception as e:
        log(f"跳过出图（matplotlib/依赖缺失）：{e}")
        return None
    s4 = np.array(ComplianceTensor.from_voigt(np.linalg.inv(C_ieee)))

    def E_of(n):
        n = np.asarray(n, float); n = n / np.linalg.norm(n)
        return 1.0 / np.einsum('ijkl,i,j,k,l->', s4, n, n, n, n)

    th = np.linspace(0, 2 * np.pi, PLOT_NPTS)
    planes = list(PLOT_3D_PLANES.items())
    fig, axes = plt.subplots(1, len(planes), subplot_kw={'projection': 'polar'},
                             figsize=(4 * len(planes), 4))
    if len(planes) == 1:
        axes = [axes]
    for ax, (name, (u, v)) in zip(axes, planes):
        E = []
        for t in th:
            n = [0.0, 0.0, 0.0]
            n[u] = np.cos(t); n[v] = np.sin(t)
            E.append(E_of(n))
        ax.plot(th, E, lw=2)
        ax.set_title(f"E in {name} (GPa)", fontsize=10)
    fig.suptitle("3D directional Young's modulus")
    fig.tight_layout()
    path = outdir / "mechanical_anisotropy.png"
    fig.savefig(path, dpi=PLOT_DPI); plt.close(fig)
    log(f"出图：{path}")
    return str(path)


def plot_2d(C2_nm, outdir):
    """2D：面内 E_2D(θ) 与 ν_2D(θ) 极坐标图。"""
    try:
        import numpy as np
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as e:
        log(f"跳过出图（matplotlib 缺失）：{e}")
        return None
    S = np.linalg.inv(C2_nm)
    th = np.linspace(0, 2 * np.pi, PLOT_NPTS)
    c, s = np.cos(th), np.sin(th)
    invE = S[0, 0] * c**4 + S[1, 1] * s**4 + (2 * S[0, 1] + S[2, 2]) * c**2 * s**2
    E = 1.0 / invE
    num = S[0, 1] * (c**4 + s**4) - (S[0, 0] + S[1, 1] - S[2, 2]) * c**2 * s**2
    nu = -num / invE
    fig, ax = plt.subplots(1, 2, subplot_kw={'projection': 'polar'}, figsize=(9, 4))
    ax[0].plot(th, E, lw=2, color="C3"); ax[0].set_title("2D Young's modulus E(θ) [N/m]", fontsize=10)
    ax[1].plot(th, nu, lw=2, color="C0"); ax[1].set_title("2D Poisson ratio ν(θ)", fontsize=10)
    fig.tight_layout()
    path = outdir / "mechanical_anisotropy_2d.png"
    fig.savefig(path, dpi=PLOT_DPI); plt.close(fig)
    log(f"出图：{path}")
    return str(path)


def postprocess_3d(C, struct, outdir):
    import numpy as np
    from pymatgen.analysis.elasticity.elastic import ElasticTensor
    from pymatgen.symmetry.analyzer import SpacegroupAnalyzer

    crystal_system = SpacegroupAnalyzer(struct, symprec=SYMPREC).get_crystal_system()
    et = ElasticTensor.from_voigt(C)
    try:
        et = et.convert_to_ieee(struct)
    except Exception as e:
        log(f"convert_to_ieee 失败（用原始取向继续）：{e}")
    C_ieee = np.array(et.voigt)

    stable, born = born_stable_3d(C_ieee, crystal_system)
    B, G = et.k_vrh, et.g_vrh
    E = et.y_mod / 1e9
    nu = et.homogeneous_poisson
    pugh = B / G
    cauchy = float(C_ieee[0, 1] - C_ieee[3, 3])
    hardness = hardness_models(B, G)
    try:
        debyeT = et.debye_temperature(struct)
        v_l, v_t = et.long_v(struct), et.trans_v(struct)
    except Exception as e:
        log(f"声速/Debye 失败：{e}"); debyeT = v_l = v_t = None
    spd = et.get_structure_property_dict(struct)

    S_ieee = np.linalg.inv(C_ieee)
    (outdir / "elastic_tensor.json").write_text(json.dumps({
        "unit": "GPa", "voigt_order": "xx,yy,zz,yz,xz,xy", "frame": "IEEE",
        "crystal_system": crystal_system,
        "C_ij": [[round(x, 3) for x in r] for r in C_ieee.tolist()],
        "S_ij_1e-3_per_GPa": [[round(x * 1e3, 4) for x in r] for r in S_ieee.tolist()],
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    fig_path = plot_3d_young(C_ieee, outdir) if MAKE_PLOTS else None

    props = {
        "dimension": "3D", "crystal_system": crystal_system,
        "mechanically_stable": bool(stable), "born_checks": born,
        "moduli_GPa": {"bulk_vrh": round(B, 2), "shear_vrh": round(G, 2), "young": round(E, 2)},
        "poisson_ratio": round(nu, 4),
        "pugh_ratio_B_over_G": round(pugh, 3), "ductile_by_pugh": bool(pugh > 1.75),
        "cauchy_pressure_GPa": round(cauchy, 2), "hardness_GPa": hardness,
        "universal_anisotropy": round(et.universal_anisotropy, 4),
        "debye_temperature_K": round(debyeT, 1) if debyeT else None,
        "sound_velocity_ms": {"longitudinal": round(v_l, 1) if v_l else None,
                              "transverse": round(v_t, 1) if v_t else None},
        "min_thermal_conductivity_W_mK": {
            "clarke": round(spd["clarke_thermalcond"], 3) if spd.get("clarke_thermalcond") else None,
            "cahill": round(spd["cahill_thermalcond"], 3) if spd.get("cahill_thermalcond") else None},
        "figure": fig_path,
    }
    (outdir / "mechanical_properties.json").write_text(
        json.dumps(props, ensure_ascii=False, indent=2), encoding="utf-8")
    (outdir / "summary.txt").write_text(
        f"维度: 3D\n晶系: {crystal_system}\n力学稳定(Born): {'是' if stable else '否'}\n"
        f"B_vrh={B:.1f} GPa  G_vrh={G:.1f} GPa\nE={E:.1f} GPa  ν={nu:.3f}\n"
        f"Pugh B/G={pugh:.2f} -> {'韧' if pugh > 1.75 else '脆'}  Cauchy={cauchy:.1f} GPa\n"
        f"Hv(Chen)={hardness.get('chen2011','-')} Hv(Tian)={hardness.get('tian2012','-')} GPa\n"
        + (f"Debye={debyeT:.0f} K  v_l/v_t={v_l:.0f}/{v_t:.0f} m/s\n" if debyeT else ""),
        encoding="utf-8")
    return props, stable


def postprocess_2d(C, struct, vac_axis, outdir):
    import numpy as np
    inplane = [a for a in (0, 1, 2) if a != vac_axis]
    i, j = inplane
    shear_map = {2: 5, 1: 4, 0: 3}
    vs = shear_map[vac_axis]
    C2 = np.array([[C[i, i], C[i, j], C[i, vs]],
                   [C[j, i], C[j, j], C[j, vs]],
                   [C[vs, i], C[vs, j], C[vs, vs]]])
    L = float(np.linalg.norm(struct.lattice.matrix[vac_axis]))
    C2_nm = C2 * L * 0.1   # GPa·Å -> N/m

    C11, C22, C12, C66 = C2_nm[0, 0], C2_nm[1, 1], C2_nm[0, 1], C2_nm[2, 2]
    det = C11 * C22 - C12 ** 2
    stable = (C11 > 0) and (C66 > 0) and (det > 0)
    Y2d_x = det / C22 if C22 else None
    Y2d_y = det / C11 if C11 else None
    nu_x = C12 / C22 if C22 else None
    nu_y = C12 / C11 if C11 else None

    (outdir / "elastic_tensor.json").write_text(json.dumps({
        "unit": "N/m", "frame": "in-plane 2D", "vacuum_axis": AXIS_NAMES[vac_axis],
        "vacuum_axis_height_Ang": round(L, 4),
        "C2D_Nm": [[round(x, 3) for x in r] for r in C2_nm.tolist()],
        "note": "C2D = C3D[GPa] × L_vac[Å] × 0.1；顺序 [C11,C12,C16;·,C22,C26;·,·,C66]",
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    fig_path = plot_2d(C2_nm, outdir) if (MAKE_PLOTS and stable) else None

    props = {
        "dimension": "2D", "vacuum_axis": AXIS_NAMES[vac_axis],
        "mechanically_stable": bool(stable),
        "born_checks_2d": {"C11>0": bool(C11 > 0), "C66>0": bool(C66 > 0),
                           "C11*C22>C12^2": bool(det > 0)},
        "stiffness_2D_Nm": {"C11": round(C11, 2), "C22": round(C22, 2),
                            "C12": round(C12, 2), "C66": round(C66, 2)},
        "young_modulus_2D_Nm": {"x": round(Y2d_x, 2) if Y2d_x else None,
                                "y": round(Y2d_y, 2) if Y2d_y else None},
        "poisson_ratio_2D": {"x": round(nu_x, 4) if nu_x is not None else None,
                             "y": round(nu_y, 4) if nu_y is not None else None},
        "figure": fig_path,
    }
    (outdir / "mechanical_properties.json").write_text(
        json.dumps(props, ensure_ascii=False, indent=2), encoding="utf-8")
    (outdir / "summary.txt").write_text(
        f"维度: 2D（真空沿 {AXIS_NAMES[vac_axis]} 轴，胞高 {L:.2f} Å）\n"
        f"力学稳定(2D Born): {'是' if stable else '否'}\n"
        f"C11={C11:.1f}  C22={C22:.1f}  C12={C12:.1f}  C66={C66:.1f} (N/m)\n"
        + (f"Y_2D(x/y)={Y2d_x:.1f}/{Y2d_y:.1f} N/m  ν(x/y)={nu_x:.3f}/{nu_y:.3f}\n"
           if stable else ""),
        encoding="utf-8")
    return props, stable


def resolve_dimension(method_file, struct_file):
    """自适应维度：FORCE_DIM 优先，其次继承 workflow_method.txt，最后按真空层判定。"""
    if FORCE_DIM in ("2d", "3d"):
        vac = None
        if FORCE_DIM == "2d":
            try:
                _, vac, _ = detect_dimension(str(struct_file))
            except SystemExit:
                vac = 2
        return FORCE_DIM, vac, f"FORCE_DIM={FORCE_DIM}"
    dim = read_dim(method_file)
    if dim:
        vac = None
        if dim == "2d":
            try:
                _, vac, _ = detect_dimension(str(struct_file))
            except SystemExit:
                vac = 2
        return dim, vac, f"继承 workflow_method.txt (DIM={dim.upper()})"
    # 没有 method 文件：按结构判定
    try:
        dim, vac, vacs = detect_dimension(str(struct_file))
        return dim, vac, f"按结构真空层判定 (DIM={dim.upper()})"
    except SystemExit as e:
        log(str(e))
        return "3d", None, "维度判定失败，回退 3D"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--step2-dir", default=STEP2_DIR_DEFAULT)
    args = ap.parse_args()

    base = {"step": STEP}
    step2 = Path(args.step2_dir)
    outcar = step2 / "OUTCAR"
    if not outcar.exists():
        emit({**base, "status": "error", "reason": f"{outcar} 缺失"}, 40)

    C = parse_total_elastic(outcar)
    if C is None:
        emit({**base, "status": "error",
              "reason": "OUTCAR 无 TOTAL ELASTIC MODULI（IBRION=6 未跑完？）"}, 40)

    struct_file = next((step2 / n for n in ("POSCAR", "CONTCAR")
                        if (step2 / n).exists()), None)
    if struct_file is None:
        emit({**base, "status": "error", "reason": f"{args.step2_dir} 无结构文件"}, 40)

    from pymatgen.core import Structure
    struct = Structure.from_file(str(struct_file))

    outdir = Path("step3_postprocess")
    outdir.mkdir(exist_ok=True)

    dim, vac_axis, dim_note = resolve_dimension(step2 / METHOD_FILE, struct_file)
    log(f"维度：{dim.upper()} — {dim_note}")

    if dim == "2d":
        props, stable = postprocess_2d(C, struct, vac_axis if vac_axis is not None else 2, outdir)
    else:
        props, stable = postprocess_3d(C, struct, outdir)

    status = "ok" if stable else "warning"
    code = 0 if stable else 40
    log(f"完成：{outdir}/mechanical_properties.json  力学稳定={stable}")
    emit({**base, "status": status, "outdir": str(outdir), "dimension": props["dimension"],
          "mechanically_stable": bool(stable), "figure": props.get("figure")}, code)


if __name__ == "__main__":
    main()
