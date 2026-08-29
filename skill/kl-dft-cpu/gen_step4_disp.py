#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_step4_disp.py —— 超胞 + 位移生成，扇出（step4_disp）。

从 step1 弛豫结构接力，扩超胞并生成位移超胞，每个位移一个 disp-NNNNN 子目录单点取力。
两种方法（step.conf 的 METHOD）：
  alm     : 随机位移（默认）。位移数 = Σ_order ceil(nfree_order / (3·N_sc)) × OVERSAMPLE，
            nfree 由 ALM suggest 给出；位移用 hiPhive MC-rattle 生成（高斯幅度 +
            最近邻 d_min 保护 + rattle_std 标定），不是固定模长的 phono3py --rd。
  findiff : phono3py 对称有限位移，位移数由空间群对称约化决定。三阶全对称集在
            大超胞/低对称下轻易上万帧 —— 本步先在内存里数帧数，超 MAX_DISP 直接报错，
            绝不落盘。

关键闸门：MAX_DISP（默认 500）。生成前先算帧数，超限 → sys.exit，不写任何 POSCAR-*/disp-*。
把 DIM/SUPERCELL/MESH/METHOD 写进 kl_params.txt，供 step5/step6 严格继承。
产出目录：step4_disp/{phono3py_disp.yaml, SPOSCAR, disp-00001, ...}
"""
import glob
import json
import logging
import os
import shutil
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kl_common as kc
from dim_common import require_dim  # noqa: E402
import stepconf

OUTDIR = "step4_disp"
STEP   = "step4_disp"
PREV   = ["step1_std_opt"]

SPEC = {
    "FUNC":         ("pbesol", "str"),
    "KSPACING":     ("0.04",   "str"),   # 超胞取力，比原胞略稀
    "KSCHEME":      ("2",      "str"),
    "ENCUT":        (None,     "int"),
    "ENCUT_FACTOR": (1.5,      "float"),
    "VASPKIT_EXE":  ("vaspkit", "str"),
    "METHOD":       ("alm",    "str"),   # alm | findiff
    "SUPERCELL":    (None,     "words"), # 显式 "3 3 3"；空=按 MIN_SC_LEN 自动
    "MIN_SC_LEN":   (12.0,     "float"),
    "MAX_MULTIPLE": (6,        "int"),
    "KAPPA_MESH":   ("20 20 20", "str"), # 写进 kl_params 供 step6
    "MAX_DISP":     (500,      "int"),   # ★ 位移帧数硬闸：超过就报错停步
    "FC3_CUTOFF_PAIR": (None,  "float"), # findiff 三阶对距离上限(Å)；空=全对称集
    "FD_DISTANCE":  (0.03,     "float"), # findiff 位移幅度(Å)
    "OVERSAMPLE":   (3,        "int"),   # alm 过采样系数（超定 3 倍）
    "ALM_CUT2":     (None,     "float"), # alm 二阶截断(Å)；空=不截断
    "ALM_CUT3":     (6.0,      "float"), # alm 三阶截断(Å)
    "DISP_RMS":     (0.03,     "float"), # MC-rattle 目标位移模长 RMS(Å)
    "MC_DMIN_SCALE": (0.75,    "float"), # MC d_min = 最近邻 × 此系数
    "MC_NITER":     (10,       "int"),   # MC 迭代数
    "MC_SEED":      (2025,     "int"),
}


# ==========================================================================
# phono3py 对象 / 帧数统计
# ==========================================================================
def make_ph3(out, reps):
    """用 phono3py Python API 建对象（等价 CLI 的 --dim=... --pa auto -c POSCAR）。"""
    try:
        from phono3py import Phono3py
        from phonopy.interface.calculator import read_crystal_structure
    except Exception as e:
        sys.exit("[ERROR] 无法 import phono3py/phonopy（%s）—— step4 需要在装了 "
                 "phono3py 的 conda 环境里跑 gen" % e)
    cell, _ = read_crystal_structure(str(out / "POSCAR"), interface_mode="vasp")
    return Phono3py(cell,
                    supercell_matrix=np.diag(np.array(reps, dtype=int)),
                    primitive_matrix="auto")


def count_findiff_frames(ph3, distance, cutoff_pair):
    """只数帧数、不建超胞：一阶位移 + 被 included 的二阶对位移。"""
    ph3.generate_displacements(distance=distance, cutoff_pair_distance=cutoff_pair)
    ds = ph3.dataset
    n = 0
    for fa in ds.get("first_atoms", []):
        n += 1
        for sa in fa.get("second_atoms", []):
            if sa.get("included", True):
                n += 1
    return n


def gate(n, conf, method, extra=""):
    """位移帧数硬闸。超过 MAX_DISP 直接停步，绝不落盘。"""
    cap = int(conf["MAX_DISP"])
    print("[..] 计划位移帧数 = %d（上限 MAX_DISP=%d）%s" % (n, cap, extra))
    if n <= cap:
        return
    if method == "findiff":
        hint = ("findiff 三阶全对称集在这个超胞下爆了。请二选一：\n"
                "  ① 换随机位移：tf -tt kl-dft-cpu -p <材料> -j step4_disp conf --set params.METHOD=alm\n"
                "  ② 收三阶对距离：... conf --set params.FC3_CUTOFF_PAIR=4.0（或 5.0）\n"
                "  ③ 缩超胞：... conf --set params.MIN_SC_LEN=12.0 / params.SUPERCELL=\"2 2 2\"")
    else:
        hint = ("ALM 自由参数太多。请二选一：\n"
                "  ① 收三阶截断：... conf --set params.ALM_CUT3=4.0\n"
                "  ② 缩超胞：... conf --set params.MIN_SC_LEN=12.0 / params.SUPERCELL=\"2 2 2\"\n"
                "  ③ 降过采样：... conf --set params.OVERSAMPLE=3（不建议低于 3）")
    sys.exit("[ERROR] 位移帧数 %d > MAX_DISP=%d，step4 已停止（未写任何 disp-*）。\n%s"
             % (n, cap, hint))


# ==========================================================================
# alm 分支：ALM 定帧数 + hiPhive MC-rattle 生成位移
# ==========================================================================
def plan_alm(out, ph3, conf):
    """ALM suggest → nfree → N = Σ ceil(nfree/(3·N_sc))×OVERSAMPLE。返回 (N, nfree, atoms)。"""
    import lattice_kappa as lk
    from ase import Atoms
    from phonopy.interface.vasp import write_vasp

    sc = ph3.supercell
    write_vasp(str(out / "SPOSCAR"), sc, direct=True)
    atoms = Atoms(numbers=sc.numbers, positions=sc.positions,
                  cell=sc.cell, pbc=True)
    n_sc = len(atoms)

    orders, cuts = [2, 3], [conf["ALM_CUT2"], conf["ALM_CUT3"]]
    # kl10: 优先用 ALM Python API 取 nfree —— ALM 的命令行可执行文件要单独
    # cmake 编译，pip 装的 Python 包并不产出它（PATH 里的 alm 只是坏入口脚本）。
    # API 拿不到（没装 alm 包等）才回落到"写 alm.in + 跑 alm + 解析日志"的老路。
    lk.write_alm_suggest_input(atoms, orders, cuts, out / "alm.in")  # 留档，便于复查
    try:
        nfree = lk.alm_nfree_via_api(atoms, orders, cuts)
    except Exception as _e:
        print("[..] ALM Python API 不可用（%s），回落到命令行 alm" % _e)
        lk.run_alm(out)
        nfree = lk.parse_alm_nfree(out / "alm.log", orders)
    n = int(lk.estimate_n_struct(nfree, n_sc, int(conf["OVERSAMPLE"])))
    detail = "  [自由参数 %s / DOF=3×%d=%d × OVERSAMPLE=%d]" % (
        nfree, n_sc, 3 * n_sc, int(conf["OVERSAMPLE"]))
    return n, nfree, atoms, detail


def make_mc_rattle_dataset(ph3, atoms, n, conf):
    """hiPhive MC-rattle（标定 rattle_std + d_min 保护）→ 塞进 phono3py 的 type-2 dataset。"""
    import lattice_kappa as lk
    structs, rattle_std, rms = lk.generate_calibrated_mc_rattle(
        atoms, n, float(conf["DISP_RMS"]), float(conf["MC_DMIN_SCALE"]),
        int(conf["MC_NITER"]), int(conf["MC_SEED"]))
    ref = atoms.get_positions()
    disps = np.array([s.get_positions() - ref for s in structs], dtype=float)
    if disps.shape != (n, len(atoms), 3):
        sys.exit("[ERROR] MC-rattle 位移形状异常 %s" % (disps.shape,))
    if float(np.linalg.norm(disps, axis=2).std()) < 1e-4:
        sys.exit("[ERROR] 位移模长 std≈0 —— 不是高斯分布，MC-rattle 没生效")
    try:
        ph3.dataset = {"displacements": disps}
    except Exception:
        ph3.displacements = disps
    return rattle_std, rms


# ==========================================================================
# 落盘
# ==========================================================================
def write_supercells(out, ph3):
    """写 POSCAR-XXXXX + phono3py_disp.yaml。返回写出的编号列表。"""
    from phonopy.interface.vasp import write_vasp
    nums = []
    for i, s in enumerate(ph3.supercells_with_displacements, 1):
        if s is None:
            continue
        write_vasp(str(out / ("POSCAR-%05d" % i)), s, direct=True)
        nums.append("%05d" % i)
    ph3.save(filename=str(out / "phono3py_disp.yaml"))
    if not (out / "phono3py_disp.yaml").is_file():
        sys.exit("[ERROR] 未产出 phono3py_disp.yaml")
    return nums


def build_displacements(out, reps, method, conf):
    """幂等：已有位移就跳过；否则先算帧数过闸，再落盘。"""
    if (out / "phono3py_disp.yaml").is_file() and glob.glob(str(out / "POSCAR-*")):
        print("[..] 已有位移超胞，跳过生成（幂等）")
        return
    ph3 = make_ph3(out, reps)
    info = {"method": method, "n_atoms_sc": len(ph3.supercell)}

    if method == "findiff":
        n = count_findiff_frames(ph3, float(conf["FD_DISTANCE"]),
                                 conf["FC3_CUTOFF_PAIR"])
        gate(n, conf, method, "  [对称有限位移，cutoff_pair=%s]" % conf["FC3_CUTOFF_PAIR"])
    else:
        n, nfree, atoms, detail = plan_alm(out, ph3, conf)
        gate(n, conf, method, detail)
        std, rms = make_mc_rattle_dataset(ph3, atoms, n, conf)
        info.update(nfree=nfree, oversample=int(conf["OVERSAMPLE"]),
                    rattle_std=round(std, 6), disp_rms=round(rms, 5))

    nums = write_supercells(out, ph3)
    if not nums:
        sys.exit("[ERROR] 没有产出 POSCAR-* 位移超胞")
    info["n_disp"] = len(nums)
    (out / "disp_plan.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    print("[OK] 位移生成完毕：%d 帧（%s）" % (len(nums), method))


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cwd = Path.cwd()
    out = cwd / OUTDIR
    out.mkdir(exist_ok=True)
    conf = stepconf.load(SPEC, STEP)

    prev = kc.find_prev_dir(cwd, PREV)
    if prev is None:
        sys.exit("[ERROR] 找不到 step1_std_opt 的结构")
    kc.relay_poscar(prev / "CONTCAR", out / "POSCAR", "step1_std_opt")

    meth = kc.read_method(prev / kc.METHOD_FILE)
    dim = (meth.get("DIM", "").lower() or kc.resolve_dim(out / "POSCAR")[0])
    _, vac_axis = kc.resolve_dim(out / "POSCAR", dim)
    require_dim(dim, ('2d', '3d'), "step4_disp",
                why="晶格热导需要声子群速度和布里渊区积分，孤立分子只有分立振动模式")
    func = conf["FUNC"] if conf["FUNC"] not in (None, "", "auto") \
        else meth.get("FUNC", "pbesol").lower()
    method = str(conf["METHOD"]).lower()
    if method not in ("findiff", "alm"):
        sys.exit("[ERROR] METHOD 只允许 findiff / alm")

    if conf["SUPERCELL"]:
        reps = [int(x) for x in conf["SUPERCELL"]]
        if dim == "2d":
            reps[vac_axis if vac_axis is not None else 2] = 1
    else:
        reps = kc.supercell_matrix(out / "POSCAR", dim, conf["MIN_SC_LEN"],
                                   conf["MAX_MULTIPLE"], vac_axis if vac_axis is not None else 2)
    mesh = kc.mesh_str(conf["KAPPA_MESH"].split(), dim, vac_axis if vac_axis is not None else 2)
    print("[..] 维度=%s 方法=%s 超胞=%s mesh=%s" % (dim.upper(), method, kc.dim_str(reps), mesh))
    kc.write_kl_params(out / kc.KL_PARAMS, DIM=dim.upper(), SUPERCELL=kc.dim_str(reps),
                       MESH=mesh, METHOD=method, FUNC=func)

    build_displacements(out, reps, method, conf)

    poscars = sorted(glob.glob(str(out / "POSCAR-*")))
    if not poscars:
        sys.exit("[ERROR] 没有产出 POSCAR-* 位移超胞")
    # 二道闸：目录里本来就躺着一堆旧帧（上一版无闸门跑出来的）也要拦住，别扇出。
    if len(poscars) > int(conf["MAX_DISP"]):
        sys.exit("[ERROR] %s 下已有 %d 个 POSCAR-*，超过 MAX_DISP=%d —— 拒绝扇出。\n"
                 "        这多半是旧版本（无闸门）留下的，先清干净再重跑：\n"
                 "        rm -rf %s && tf -tt kl-dft-cpu -p <材料> -j step4_disp rerun"
                 % (OUTDIR, len(poscars), int(conf["MAX_DISP"]), OUTDIR))
    print("[..] 位移超胞 %d 个，逐个补输入" % len(poscars))

    # kleq：平衡帧 disp-00000 —— 未位移的完美超胞单点，INCAR/K 点/泛函与位移帧完全一致。
    #   ① 拟合时逐帧扣它的残余力（F_disp - F_eq），去掉未完全弛豫 / egg-box 的净力；
    #   ② 它是"缺帧容错"的锚：随机位移可以少几帧，平衡帧不能缺。
    #   phono3py 的位移帧从 POSCAR-00001 起编号，00000 正好空出来给它。
    sposcar = out / "SPOSCAR"
    if not sposcar.is_file():
        from phonopy.interface.vasp import write_vasp as _write_vasp
        _write_vasp(str(sposcar), make_ph3(out, reps).supercell, direct=True)
    frames = [("00000", str(sposcar))]
    frames += [(os.path.basename(p).split("-", 1)[1], p) for p in poscars]

    here = Path(__file__).resolve().parent
    incar_tpl = kc.resolve_submit(here, dim, "incar_force")
    submit_tpl = kc.resolve_submit(here, dim, "submit_std")
    encut = None
    for num, pos in frames:
        d = out / ("disp-%s" % num)
        d.mkdir(exist_ok=True)
        if (d / "INCAR").is_file() and (d / "POSCAR").is_file():
            continue
        shutil.copyfile(pos, d / "POSCAR")
        kc.vaspkit_kpoints(d, conf["KSCHEME"], conf["KSPACING"], conf["VASPKIT_EXE"], dim, vac_axis)
        kc.vaspkit_potcar(d, conf["VASPKIT_EXE"])
        if encut is None:
            encut = conf["ENCUT"] or kc.encut_from_potcar(d / "POTCAR", conf["ENCUT_FACTOR"])
        # patch_kl_vdw：step2/step3 都渲染了 VDW_LINE，唯独取力这步漏了 ——
        # FUNC=pbe-d3 时会变成"带 D3 弛豫、无 D3 取力"，平衡位置残留净力，
        # fc2 里混进伪软模，fc3 更不可用。
        kc.render_tpl(incar_tpl, {"SYSTEM": "%s force %s" % (cwd.name, num),
                                  "ENCUT": encut,
                                  "GGA": kc.GGA_MAP.get(func, "PS"),
                                  "VDW_LINE": ("IVDW = %s" % kc.VDW_MAP[func])
                                  if kc.VDW_MAP.get(func) else "# no vdW"},
                      d / "INCAR")
        kc.write_submit(submit_tpl, d / "submit.sh",
                        {"JOBNAME": "%s-kl-dft-cpu-S4-%s" % (cwd.name, num)})
        stepconf.apply_submit(d / "submit.sh", conf.submit)
    print("[DONE] %s：%d 个位移子目录 + 平衡帧 disp-00000 就绪，"
          "tf 各自提交（fanout disp-*）" % (OUTDIR, len(poscars)))


if __name__ == "__main__":
    main()
