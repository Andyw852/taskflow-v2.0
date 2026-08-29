#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_step7_deform.py —— 形变势单点，扇出（step7_deform）。

流程：
  1. POSCAR ← step1_std_opt/CONTCAR
  2. amset deform create → 在本目录生成 undeformed/ + deform-01..NN/（各含形变 POSCAR）
  3. 给每个子目录补 INCAR（渲染 incar_deform_*.tpl）+ KPOINTS + POTCAR + submit.sh
tf 的扇出机制随后对每个 *deform* 子目录各自 sbatch。
产出目录：step7_deform/{undeformed, deform-01, ...}
"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ke_common as kc
import stepconf  # noqa: E402
from dim_common import require_dim, resolve_tpl  # noqa: E402

# =========================== 可改参数区 ===========================
OUTDIR_NAME  = "step7_deform"
PREV_CANDS   = ["step1_opt", "step1_std_opt"]
DIMENSION    = "auto"
VASPKIT_EXE  = "vaspkit"
KSCHEME      = "2"
KSPACING     = "0.03"
FUNC         = "inherit"      # patch_ke_dag: inherit=继承 step1
# 注意：本步必须和 step3_uniform 同泛函，否则形变势里会掺进泛函差异
MANUAL_ENCUT = None
ENCUT_FACTOR = 1.5
STEP_LABEL   = "S7_deform"
# amset 环境：优先读 step.conf 里 tf 注入的集群 CONDA_SH/AMSET_ENV（setting/<集群>.yaml
# 里配 conda_sh/amset_env，每人按自己的机器配置，脚本不硬编码路径）；
# step.conf 没有（旧项目/直跑脚本）才回退主机探测。
import os as _os
def _amset_env_src():
    try:
        import stepconf as _sc
        _txt = open(_sc.CONF_NAME, encoding="utf-8-sig").read()
        _p = {k.upper(): v for k, v, _ in _sc.parse(_txt, _sc.CONF_NAME).get("params", [])}
        _sh, _env = _p.get("CONDA_SH"), _p.get("AMSET_ENV")
        if _sh and _env:
            return "source %s && conda activate %s" % (_sh, _env)
    except Exception:
        pass
    if _os.path.isdir("/home/wangchaoyue852/miniconda3"):
        return "source /home/wangchaoyue852/miniconda3/etc/profile.d/conda.sh && conda activate amset"
    return "source /public/home/wangchao/miniconda3/etc/profile.d/conda.sh && conda activate amset_clean"
AMSET_ENV_SRC = _amset_env_src()
DEFORM_GLOB  = "*deform*"      # 与 skill.yaml 的 fanout 一致
                               # （同时匹配 deform-NNN 和 undeformed，两者都要交）
# patch_deform_fix：形变幅度，amset 默认 0.005 = ±0.5%，形变势对 ±0.5% 取平均
STRAIN_DISTANCE = 0.005
# 对称精度：None = amset 默认 0.01；"N" = 完全关对称（12 个形变全算）；
# 也可写数值。放宽它能减少不等价形变数，但要确认放宽后的空间群是对的。
SYMPREC      = None
# =================================================================
GGA_MAP = {"pbe": "PE", "pbesol": "PS", "pbe-d3": "PE"}


def run_amset_deform_create(out: Path):
    """在 out 目录跑 amset deform create，再把产出的 POSCAR-NNN 摊成子目录。

    patch_deform_fix：amset 的 create 子命令
      - 不吃位置参数，结构文件走 -f/--filename；
      - 产出的是【文件】POSCAR-001 / POSCAR-002 …（位数随总数变），
        写在当前目录，【不建任何子目录，也不建 undeformed/】。
    所以目录结构得我们自己摊：
        undeformed/POSCAR   <- 原始（未形变）结构
        deform-001/POSCAR   <- POSCAR-001
        deform-002/POSCAR   <- POSCAR-002
        ...
    skill.yaml 的 fanout "*deform*" 同时匹配 deform-NNN 和 undeformed，
    两者都会被提交——undeformed 是形变势的参考态，不能漏。
    """
    import glob
    import shutil as _sh

    done = [p for p in glob.glob(str(out / "deform-*")) if os.path.isdir(p)]
    if done and (out / "undeformed").is_dir():
        print("[..] 已有形变子目录 %d 个 + undeformed，跳过 create（幂等）"
              % len(done))
        return

    opts = "-f POSCAR -d %s" % STRAIN_DISTANCE
    if SYMPREC is not None:
        opts += " -s %s" % SYMPREC
    cmd = ("%s && cd %s && amset deform create %s >> deform_create.log 2>&1"
           % (AMSET_ENV_SRC, str(out), opts))
    print("[..] amset deform create %s ..." % opts)
    rc = subprocess.run(["bash", "-lc", cmd]).returncode
    if rc != 0:
        sys.exit("[ERROR] amset deform create 失败，看 %s/deform_create.log" % out)

    poscars = sorted(Path(p) for p in glob.glob(str(out / "POSCAR-*")))
    if not poscars:
        sys.exit("[ERROR] amset deform create 没有产出 POSCAR-NNN，"
                 "看 %s/deform_create.log" % out)

    src_poscar = out / "POSCAR"
    if not src_poscar.is_file():
        sys.exit("[ERROR] %s 缺原始 POSCAR，无法建 undeformed/" % out)
    und = out / "undeformed"
    und.mkdir(exist_ok=True)
    _sh.copy2(str(src_poscar), str(und / "POSCAR"))
    print("[OK] undeformed/POSCAR（参考态）")

    for p in poscars:
        tag = p.name.split("-", 1)[1]          # POSCAR-003 -> 003
        d = out / ("deform-%s" % tag)
        d.mkdir(exist_ok=True)
        _sh.copy2(str(p), str(d / "POSCAR"))
    print("[OK] 摊出 %d 个 deform-NNN 子目录（+ undeformed，共 %d 个单点）"
          % (len(poscars), len(poscars) + 1))


# === patch_kpts_align：形变势要求所有构型共用同一套 k 点 ===
def _reference_kpoints(out, dim, vac_axis, n_sub):
    """只在 undeformed/ 生成一次 KPOINTS 作为基准，返回其路径（失败返回 None）。

    形变势是逐 k 点、逐能带做差。vaspkit 按各自晶格常数换算 KSPACING，
    ±0.5% 应变足以让某个方向的细分数跳一档，于是 deform-NN 与 undeformed
    的 k 点数对不上，差分失效。统一拷贝基准 KPOINTS 可根除这个问题
    （应变只有 0.5%，共用网格在收敛性上无损）。"""
    und = out / "undeformed"
    if not (und / "POSCAR").is_file():
        print("[WARN] 没有 undeformed/POSCAR —— 退回逐目录生成 KPOINTS，"
              "各形变的 k 网格可能不一致，形变势会失真")
        return None
    kc.vaspkit_kpoints(und, KSCHEME, KSPACING, VASPKIT_EXE, dim, vac_axis)
    kpts = und / "KPOINTS"
    if not kpts.is_file():
        print("[WARN] undeformed/KPOINTS 生成失败 —— 退回逐目录生成")
        return None
    print("[OK] 基准 k 网格取自 undeformed/，拷贝给全部 %d 个单点目录" % n_sub)
    return kpts


# ---- [PATCH-IONRELAX] 对 xx±/yy± 4 个形变目录生成 ionrelax/ 子目录 ----
def _build_ionrelax(d: Path, encut, subs, submit_body):
    """在 deform-NN 下建 ionrelax/：两段式（弛豫段 IBRION=2 + 静态段 LVHAR）。

    E1 必须用「离子弛豫后的内坐标」取带边能量（刚性形变 S 原子不动，E1 系统性
    偏小 24%），amset h5 仍用本级刚性单点（clamped-ion 口径不变）。弛豫段
    EDIFF=1E-6 省时间，静态段读 CONTCAR+WAVECAR 取 EDIFF=1E-8 精确带边 + LOCPOT。
    两段都保持 ISYM=0。"""
    ir = d / "ionrelax"
    ir.mkdir(exist_ok=True)
    # 清理旧 SCF 产物（重跑/幂等时残留的 vasprun/LOCPOT/OUTCAR 等），避免
    # step7b 的 _resolved 读到过期结果
    for _f in list(ir.glob("*")):
        if _f.name not in ("POSCAR", "KPOINTS", "POTCAR"):
            if _f.is_file() or _f.is_symlink():
                _f.unlink()
    shutil.copy2(str(d / "POSCAR"), str(ir / "POSCAR"))
    shutil.copy2(str(d / "KPOINTS"), str(ir / "KPOINTS"))
    shutil.copy2(str(d / "POTCAR"), str(ir / "POTCAR"))

    base = (d / "INCAR").read_text(encoding="utf-8")
    # 弛豫段
    relax = re.sub(r"IBRION\s*=.*", "IBRION = 2            # 离子弛豫（固定晶格）", base)
    relax = re.sub(r"NSW\s*=.*", "NSW    = 60", relax)
    relax = re.sub(r"EDIFF\s*=.*", "EDIFF  = 1E-6", relax)
    relax = re.sub(r"LVHAR\s*=.*", "LVHAR  = .FALSE.", relax)
    relax = re.sub(r"LWAVE\s*=.*", "LWAVE  = .TRUE.", relax)
    relax = re.sub(r"LCHARG\s*=.*", "LCHARG = .FALSE.", relax)
    if "EDIFFG" not in relax:
        relax = relax.rstrip() + "\nEDIFFG = -1E-3\n"
    (ir / "INCAR.relax").write_text(relax, encoding="utf-8", newline="\n")
    # 静态段
    stat = re.sub(r"IBRION\s*=.*", "IBRION = -1           # 静态段（弛豫后精确带边）", base)
    stat = re.sub(r"NSW\s*=.*", "NSW    = 0", stat)
    stat = re.sub(r"ISTART\s*=.*", "ISTART = 1            # 接弛豫段 WAVECAR", stat)
    stat = re.sub(r"ICHARG\s*=.*", "ICHARG = 0", stat)
    if "ISTART" not in stat:
        stat = stat.rstrip() + "\nISTART = 1\n"
    (ir / "INCAR.static").write_text(stat, encoding="utf-8", newline="\n")

    # deform-NN 的 submit.sh 追加 ionrelax 两段（跑完刚性单点后 cd ionrelax 续跑）
    sub = (d / "submit.sh").read_text(encoding="utf-8")
    _mpi = None
    for _ln in sub.splitlines():
        if "mpirun" in _ln:
            _mpi = _ln.strip()
            break
    if not _mpi:
        _mpi = "mpirun -np $SLURM_NTASKS vasp_std"
    extra = (
        "\n# ---- ionrelax 两段（E1 用离子弛豫内坐标）----\n"
        "cd ionrelax || exit 1\n"
        "cp INCAR.relax INCAR\n"
        + _mpi + " 2>&1 | tail -20\n"
        "cp CONTCAR POSCAR\n"
        "cp INCAR.static INCAR\n"
        + _mpi + " 2>&1 | tail -20\n"
        "cp CONTCAR POSCAR\n"
    )
    sub = sub.rstrip() + extra
    (d / "submit.sh").write_text(sub, encoding="utf-8", newline="\n")


def main():
    import glob
    cwd = Path.cwd(); out = cwd / OUTDIR_NAME; out.mkdir(exist_ok=True)
    prev = kc.find_prev_dir(cwd, PREV_CANDS)
    if prev is None:
        sys.exit("[ERROR] 找不到含 CONTCAR 的上一步：%s" % PREV_CANDS)
    kc.relay_poscar(prev / "CONTCAR", out / "POSCAR", "step1_opt")
    _func, _subs = kc.resolve_func(prev, FUNC, OUTDIR_NAME)
    dim = kc.read_method_dim(prev / kc.METHOD_FILE) \
        or kc.resolve_dim_for(out / "POSCAR", DIMENSION)[0]
    _, vac_axis = kc.resolve_dim_for(out / "POSCAR", dim)
    require_dim(dim, ('2d', '3d'), "step7_deform",
                why="载流子输运/形变势建立在能带色散上，孤立分子没有色散")
    print("[..] 维度：%s" % dim.upper())
    kc.write_method(out / kc.METHOD_FILE, dim, "形变势单点（扇出）",
                    func=_func)

    run_amset_deform_create(out)

    subs = sorted([Path(p) for p in glob.glob(str(out / DEFORM_GLOB)) if os.path.isdir(p)])
    if not subs:
        sys.exit("[ERROR] amset deform create 没有产出形变子目录")
    print("[..] 形变子目录 %d 个，逐个补输入" % len(subs))

    tpl = Path(__file__).resolve().parent / ("incar_deform_%s.tpl" % dim)
    submit_tpl = resolve_tpl(Path(__file__).resolve().parent, "submit_std", dim)
    submit_body = submit_tpl.read_text(encoding="utf-8")

    ref_kpts = _reference_kpoints(out, dim, vac_axis, len(subs))   # patch_kpts_align

    encut = None
    for d in subs:
        pos = d / "POSCAR"
        if not pos.is_file():
            sys.exit("[ERROR] %s 缺 POSCAR" % d)
        if ref_kpts is not None:
            # patch_kpts_samefile：subs 里包含 undeformed 自己，而基准 KPOINTS
            #   就生成在那儿——拷到自己身上会抛 SameFileError，跳过即可。
            _dst = d / "KPOINTS"
            _same = False
            if _dst.is_file():
                try:
                    _same = os.path.samefile(str(ref_kpts), str(_dst))
                except OSError:
                    _same = (_dst.resolve() == Path(ref_kpts).resolve())
            if not _same:
                shutil.copy2(str(ref_kpts), str(_dst))
        else:
            kc.vaspkit_kpoints(d, KSCHEME, KSPACING, VASPKIT_EXE, dim, vac_axis)
        kc.vaspkit_potcar(d, VASPKIT_EXE)
        if encut is None:
            encut = MANUAL_ENCUT or kc.encut_from_potcar(d / "POTCAR", ENCUT_FACTOR)
        _sub = {"SYSTEM": "%s deform %s" % (cwd.name, d.name),
                "ENCUT": encut}
        _sub.update(_subs)
        kc.render_tpl(tpl, _sub, d / "INCAR")
        kc.inherit_scf_tags(d / "INCAR", cwd, with_u=True, label=d.name)
        # 并行参数按宿主机自适应（GPU 版强制 NCORE=1/KPAR=1，CPU 保持模板默认）
        kc.apply_parallel_tags(d / "INCAR")
        sub = d / "submit.sh"
        sub.write_text(submit_body.replace(
            "{{JOBNAME}}", "%s-ke-dft-cpu-%s-%s" % (cwd.name, STEP_LABEL, d.name)),
            encoding="utf-8", newline="\n")
        stepconf.apply_submit(sub, stepconf.read_submit(stepconf.CONF_NAME))

    # [PATCH-IONRELAX] 对 xx±/yy± 4 个形变目录生成 ionrelax/ 子目录（共用
    # resolve_strain_pairs 反解，与 step7b 找 ionrelax 的是同一套，不错位）。
    if dim == "2d":
        _pairs, _sm = kc.resolve_strain_pairs(out)
        if _pairs:
            _ir_dirs = {_pairs["xx"][0], _pairs["xx"][1],
                        _pairs["yy"][0], _pairs["yy"][1]}
            for _name in sorted(_ir_dirs):
                _build_ionrelax(out / _name, encut, _subs, submit_body)
            print("[IONRELAX] 已生成 %d 个 ionrelax/（xx±/yy± 弛豫），E1 将用离子弛豫构型"
                  % len(_ir_dirs))
        else:
            print("[WARN] 应变配对反解失败，跳过 ionrelax/（E1 退回刚性单点口径）")

    print("[DONE] %s：%d 个形变子目录输入就绪，tf 会各自提交" % (OUTDIR_NAME, len(subs)))


if __name__ == "__main__":
    main()
