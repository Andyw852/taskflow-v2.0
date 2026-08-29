#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_step2_disp_force.py —— 超胞 + 位移生成，一个作业算完全部力（step2_disp_force）。

登录节点这一半：接力 step1 的 CONTCAR → 定超胞 → phono3py -d 生成位移（秒级，纯对称
分析）→ 报位移数 → 写 submit.sh。
计算节点那一半：submit.sh 跑 mace_forces.py，串行遍历全部位移超胞取力。

METHOD：
  random   随机位移（--rd N）。N 缺省 auto：按 ALM 数出的自由力常数个数反推
           （N=ceil(Σnfree/DOF)×OVERSAMPLE，对照 kl-dft-cpu 的 plan_alm），拟合走 symfc。
  findiff  对称有限位移。位移数由空间群约化决定，结果最干净，但 CPU 上
           高对称体系也会几百帧，慢；GPU 版才推荐。

超胞尺寸：MACE 的成本对超胞是线性的，DFT 是三次方——**这里该比 kl-dft-cpu 大胆得多**。
MIN_SC_LEN 默认 18 Å（kl-dft-cpu 是 15），fc2 还可以用 FC2_SUPERCELL 单独放到更大（--dim-fc2），
因为二阶的长程尾巴是声速和低频支准不准的关键，而 fc3 短程收敛快、不必陪着一起大。
"""
import glob
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import klmace_common as kc
from dim_common import require_dim  # noqa: E402
import stepconf

OUTDIR = "step2_disp_force"
STEP = "step2_disp_force"
PREV = ["step1_mace_relax"]

SPEC = {
    # ---- 全局 ----
    "MACE_MODEL": ("mace-mp:medium", "str"),
    "MACE_MODEL_DIR": ("", "str"),
    "DEVICE": ("auto", "str"),
    "DTYPE": ("float64", "str"),
    "CONDA_SH": (kc.DEFAULT_CONDA_SH, "str"),
    "CONDA_ENV": (kc.DEFAULT_CONDA_ENV, "str"),
    # ---- 本步 ----
    "METHOD": ("random", "str"),           # random | findiff（findiff 帧数由对称性决定，CPU 上偏多）
    "SUPERCELL": (None, "str"),            # 显式 "4 4 4"；空=按 MIN_SC_LEN 自动
    "FC2_SUPERCELL": (None, "str"),        # 二阶专用大超胞（--dim-fc2）；空=与 fc3 同
    "MIN_SC_LEN": (18.0, "float"),
    "MAX_MULTIPLE": (8, "int"),
    "DISP_DISTANCE": (0.03, "float"),      # 位移幅度(Å)：MC-rattle 的目标 RMS / findiff 模长
    "MC_DMIN_SCALE": (0.85, "float"),      # MC-rattle d_min = 最近邻 × 此系数
    "MC_NITER": (10, "int"),               # MC-rattle 迭代数
    "RANDOM_SEED": (2025, "int"),          # MC-rattle 随机种子
    "N_RANDOM": ("auto", "str"),           # random 帧数：auto=按 ALM nfree 反推；整数=固定
    "N_RANDOM_FC2": ("auto", "str"),       # fc2 专用超胞的随机帧数（auto=按 ALM 反推）
    "OVERSAMPLE": (3, "int"),              # 随机位移过采样系数：N=ceil(Σnfree/DOF)*OVERSAMPLE
    "ALM_CUT2": (None, "float"),           # 二阶截断(Å)；None=不截断
    "ALM_CUT3": (6.0, "float"),            # 三阶截断(Å)
    "MAX_DISP": (500, "int"),              # ★ 位移帧数硬闸：超过直接停步（对照 kl-dft-cpu 的 MAX_DISP）
    "KAPPA_MESH": ("24 24 24", "str"),     # 写进 klmace_params 供 step4
    "SUBTRACT_RESIDUAL": (True, "bool"),
    "CKPT": (20, "int"),                   # 每多少帧落一次断点
}


# 用 ALM Python API 数各阶自由(不可约)力常数个数 → 反推随机位移帧数（对照 kl-dft-cpu 的
# plan_alm）：每一帧随机位移同时给 fc2/fc3 提供方程，所以按 Σnfree 整体计，
# N = max(10, ceil(Σnfree/(3*N_sc)) * OVERSAMPLE)。在 venv 里跑（需要 alm/ase/phono3py）。
# 参数：POSCAR 超胞倍数 "2,3" 阶 过采样 二阶截断 三阶截断
_ALM_NRANDOM = r'''import sys, math
import numpy as np
from phono3py import Phono3py
from phonopy.interface.calculator import read_crystal_structure
import spglib
from symfc import Symfc
from symfc.utils.utils import SymfcAtoms

poscar, reps_s, orders_s, ov_s, cut2_s, cut3_s = sys.argv[1:7]
reps = [int(x) for x in reps_s.split()]
orders = [int(x) for x in orders_s.split(",")]
oversample = int(ov_s)

cell, _ = read_crystal_structure(poscar, interface_mode="vasp")
ph3 = Phono3py(cell, supercell_matrix=np.diag(np.array(reps, dtype=int)),
               primitive_matrix=np.eye(3))
sc = ph3.supercell
atoms = SymfcAtoms(cell=sc.cell, scaled_positions=sc.scaled_positions, numbers=sc.numbers)
n_sc = len(sc.numbers)

cell_tuple = (sc.cell, sc.scaled_positions, sc.numbers)
sym = spglib.get_symmetry(cell_tuple, symprec=1e-5)

cut = {}
for o in orders:
    c = cut3_s if o == 3 else cut2_s
    cut[o] = None if c in ("None", "none", "") else float(c)

sf = Symfc(atoms, None, None, sym, cutoff=cut)
nfree = {int(o): int(v) for o, v in sf.estimate_basis_size(orders=orders).items()}

nfree_total = sum(nfree.values())
dof = 3 * n_sc
n_struct = max(10, math.ceil(nfree_total / dof) * oversample)
print("N_RANDOM %d" % n_struct)
print("NFREE %s TOTAL=%d DOF=%d N_SC=%d OVERSAMPLE=%d"
      % (nfree, nfree_total, dof, n_sc, oversample))
'''


def resolve_nrandom(conf, out, poscar_name, reps, orders):
    """解析随机位移帧数。整数=固定；auto/空=按 ALM nfree 反推。"""
    key = "N_RANDOM" if orders == [2, 3] else "N_RANDOM_FC2"
    val = str(conf[key] or "").strip()
    if val.lower() not in ("", "auto", "none"):
        try:
            return int(val)
        except ValueError:
            sys.exit("[ERROR] %s=%r 不是整数也不是 auto" % (key, val))
    script = out / "_alm_nrandom.py"
    script.write_text(_ALM_NRANDOM, encoding="utf-8")
    cmd = ("python _alm_nrandom.py %s '%s' '%s' %d '%s' '%s'"
           % (poscar_name, kc.dim_str(reps), ",".join(str(o) for o in orders),
              int(conf["OVERSAMPLE"]), conf["ALM_CUT2"], conf["ALM_CUT3"]))
    rc, so = kc.run_capture(cmd, out, conf)
    for ln in (so or "").splitlines():
        if ln.startswith("N_RANDOM "):
            try:
                return int(ln.split()[1])
            except (ValueError, IndexError):
                pass
    tail = (so or "").strip()[-600:]
    sys.exit("[ERROR] ALM nfree 反推帧数失败（rc=%d）。%s"
             % (rc, ("stdout 尾部：%s" % tail) if tail else "无输出，查 venv 里 alm/ase/phono3py"))


def probe_ndisp(out, conf):
    """问 phono3py 到底生成了几个位移超胞（--rd 时 POSCAR-* 不一定可数）。"""
    code = ("python -c \"import phono3py;p=phono3py.load('phono3py_disp.yaml',"
            "produce_fc=False);a=p.supercells_with_displacements;"
            "b=p.phonon_supercells_with_displacements;"
            "print('NDISP',len(a),len(b) if b else 0,len(p.supercell))\"")
    rc, so = kc.run_capture(code, out, conf)
    for ln in so.splitlines():
        if ln.startswith("NDISP"):
            t = ln.split()
            return int(t[1]), int(t[2]), int(t[3])
    n = len(glob.glob(str(out / "POSCAR-*")))
    return n, 0, 0


def main():
    cwd = Path.cwd()
    out = cwd / OUTDIR
    out.mkdir(exist_ok=True)
    conf = stepconf.load(SPEC, STEP)

    prev = kc.find_prev_dir(cwd, PREV)
    if prev is None:
        sys.exit("[ERROR] 找不到 step1_mace_relax 的结构")
    kc.relay_poscar(prev / "CONTCAR", out / "POSCAR", "step1_mace_relax")
    for f in (kc.METHOD_FILE,):
        if (prev / f).is_file():
            shutil.copyfile(str(prev / f), str(out / f))

    meth = kc.read_method(out / kc.METHOD_FILE)
    dim = (meth.get("DIM", "").lower() or kc.resolve_dim(out / "POSCAR")[0])
    _, vac_axis = kc.resolve_dim(out / "POSCAR", dim)
    ax = vac_axis if vac_axis is not None else 2
    require_dim(dim, ("2d", "3d"), "step2_disp_force",
                why="声子/热导需要周期性布里渊区")

    method = str(conf["METHOD"] or "findiff").lower()
    if method not in ("findiff", "random"):
        sys.exit("[ERROR] METHOD 只允许 findiff / random")

    reps = (kc.parse_reps(conf["SUPERCELL"], dim, ax) if conf["SUPERCELL"]
            else kc.supercell_matrix(out / "POSCAR", dim, conf["MIN_SC_LEN"],
                                     conf["MAX_MULTIPLE"], ax))
    reps2 = kc.parse_reps(conf["FC2_SUPERCELL"], dim, ax) if conf["FC2_SUPERCELL"] else None
    mesh = kc.mesh_str(conf["KAPPA_MESH"].split(), dim, ax)
    print("[..] 维度=%s 方法=%s fc3超胞=%s fc2超胞=%s mesh=%s 位移=%.3f Å"
          % (dim.upper(), method, kc.dim_str(reps),
             kc.dim_str(reps2) if reps2 else "同 fc3", mesh, conf["DISP_DISTANCE"]))

    kc.write_kl_params(out / kc.KL_PARAMS, DIM=dim.upper(),
                       SUPERCELL=kc.dim_str(reps),
                       FC2_SUPERCELL=(kc.dim_str(reps2) if reps2 else None),
                       MESH=mesh, METHOD=method, VAC_AXIS=ax,
                       MODEL=conf["MACE_MODEL"])

    kc.check_env(conf, out)

    if (out / "phono3py_disp.yaml").is_file():
        print("[..] 已有 phono3py_disp.yaml，跳过位移生成（幂等；要重来请 tf rerun）")
    elif method == "random":
        # hiphive MC-rattle（对照 kl-dft-cpu 的 make_mc_rattle_dataset）：目标 RMS + d_min 保护，
        # 替代 phono3py --rd 的固定模长随机位移。fc3 + 可选 fc2（FC2_SUPERCELL）都走它。
        n_random = resolve_nrandom(conf, out, "POSCAR", reps, [2, 3])
        n_random_fc2 = resolve_nrandom(conf, out, "POSCAR", reps2, [2]) if reps2 else 0
        here = Path(__file__).resolve().parent
        for f in ("mc_rattle.py", "mc_rattle_disp.py"):
            if not (here / f).is_file():
                sys.exit("[ERROR] 缺 %s —— 本步 gen_need 里漏了它？" % f)
            shutil.copyfile(str(here / f), str(out / f))
        reps2_str = kc.dim_str(reps2) if reps2 else ""
        rc = kc.run_in_env(
            "python mc_rattle_disp.py POSCAR '%s' %d %g %g %d %d '%s' %d"
            % (kc.dim_str(reps), n_random, conf["DISP_DISTANCE"],
               conf["MC_DMIN_SCALE"], conf["MC_NITER"], conf["RANDOM_SEED"],
               reps2_str, n_random_fc2),
            out, "disp_create.log", conf)
        if rc != 0 or not (out / "phono3py_disp.yaml").is_file():
            sys.exit("[ERROR] MC-rattle 位移生成失败，看 %s/disp_create.log" % OUTDIR)
    else:
        args = ['-d --dim="%s" --pa auto -c POSCAR --amplitude %s'
                % (kc.dim_str(reps), conf["DISP_DISTANCE"])]
        if reps2:
            args.append('--dim-fc2="%s"' % kc.dim_str(reps2))
        if kc.run_phono3py(" ".join(args), out, "disp_create.log", conf) != 0 \
                or not (out / "phono3py_disp.yaml").is_file():
            sys.exit("[ERROR] phono3py -d 失败，看 %s/disp_create.log" % OUTDIR)

    n3, n2, nat = probe_ndisp(out, conf)
    if n3 <= 0:
        sys.exit("[ERROR] 没有位移超胞，phono3py -d 没干活")
    print("[OK] 位移超胞：fc3 %d 帧%s，超胞 %d 原子"
          % (n3, ("，fc2 %d 帧" % n2) if n2 else "", nat))
    if method == "findiff" and n3 > 2000:
        print("[WARN] %d 帧偏多。MACE 单帧秒级也要跑上小时——考虑 METHOD=random "
              "+ N_RANDOM=100~200，或把 SUPERCELL 调小一档。" % n3)
    # 帧数硬闸（对照 kl-dft-cpu 的 MAX_DISP）：findiff 全对称集在大超胞/低对称下会爆，
    # 落盘前就拦住，别让 CPU 白白跑几百上千帧。
    if n3 + n2 > int(conf["MAX_DISP"]):
        sys.exit("[ERROR] 位移帧数 %d > MAX_DISP=%d，step2 已停止。\n"
                 "        换随机位移：tf -tt <本技能名> -p <材料> -j step2_disp_force "
                 "conf --set params.METHOD=random\n"
                 "        或缩超胞：... conf --set params.SUPERCELL=\"3 3 3\" / "
                 "params.MIN_SC_LEN=12.0" % (n3 + n2, int(conf["MAX_DISP"])))

    here = Path(__file__).resolve().parent
    for f in ("mace_forces.py", "mace_model.py"):
        if not (here / f).is_file():
            sys.exit("[ERROR] 缺 %s —— 本步 gen_need 里漏了它？" % f)
        shutil.copyfile(str(here / f), str(out / f))

    cmd = ("python mace_forces.py --disp-yaml phono3py_disp.yaml "
           "--model %s --model-dir '%s' --device %s --dtype %s --ckpt %d "
           "--subtract-residual %s"
           % (conf["MACE_MODEL"], conf["MACE_MODEL_DIR"] or "",
              conf["DEVICE"] or "auto", conf["DTYPE"] or "float64",
              conf["CKPT"], str(bool(conf["SUBTRACT_RESIDUAL"])).lower()))
    tpl = kc.resolve_submit(here, "submit_mace")
    kc.write_submit(tpl, out / "submit.sh",
                    {"JOBNAME": kc.new_jobname(cwd, "S2force"),
                     "CONDA_SH": conf["CONDA_SH"] or kc.DEFAULT_CONDA_SH,
                     "CONDA_ENV": conf["CONDA_ENV"] or kc.DEFAULT_CONDA_ENV,
                     "MACE_CMD": cmd})
    stepconf.apply_submit(out / "submit.sh", conf.submit)
    print("[DONE] %s：%d 帧待取力，submit.sh 就绪（单作业串完，不扇出）" % (OUTDIR, n3 + n2))


if __name__ == "__main__":
    main()
