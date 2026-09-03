# _common/mace —— MACE 晶格热导率引擎（kl-mace-gpu / kl-mace-cpu 共用）

两个技能 `kl-mace-gpu` 和 `kl-mace-cpu` 共用这一份代码。技能目录里只有 `skill.yaml`
和 `templates/`，差异全在模板里（队列、`DEVICE`、超胞默认值）。

`tf` 的资源查找链是

```
<技能>/templates/<步骤名>/ → <技能>/templates/ → <技能>/ → _common/*/ → _common/*/templates/
```

所以 `gen_need` 里写文件名就够，tf 会自己找到这里。**技能目录里有同名文件时优先用
技能自己的**——想让某一版彻底自包含（比如你要给某个版本改引擎而不影响另一个）：

```bash
cp skill/_common/mace/*.py skill/kl-mace-gpu/     # 拷进去 = 覆盖公共池
rm skill/kl-mace-gpu/*.py                          # 删掉 = 回到共用
```

## 文件

| 文件 | 在哪跑 | 干什么 |
|---|---|---|
| `gen_step1_mace_relax.py` | 登录节点 | 建目录、接 POSCAR、调 `mace_relax.py` |
| `gen_step2_disp_force.py` | 登录节点 | 定超胞、`phono3py -d` 生成位移、写 `submit.sh` |
| `gen_step3_fc.py` | 登录节点 | 取力常数、出声子谱、虚频闸 |
| `gen_step4_kappa.py` | 登录节点 | 组 BTE 命令、写 `submit.sh` |
| `mace_relax.py` | conda 环境 | ASE + FrechetCellFilter + FixSymmetry 弛豫 |
| `mace_forces.py` | conda 环境（计算节点） | 遍历位移超胞取力，断点续算 |
| `mace_model.py` | conda 环境 | 模型定位 + calculator 构造 + 设备判定 |
| `klmace_common.py` | 两边 | 维度/超胞/模板渲染/conda 子进程 |

## 四步

| 步骤 | 干什么 | 在哪跑 | 判据 |
|---|---|---|---|
| S1_relax | MACE 弛豫原胞 | 登录节点 `run: gen` | `relax_summary.json` 的 `"converged": true` |
| S2_force | 位移生成 + **一个作业算完全部力** | 计算节点 ×1 | `forces_summary.json` 的 `FORCES_DONE` |
| S3_fc | 拟合 fc2/fc3 → 声子谱 → 虚频闸 | 登录节点 `run: gen` | `phonon_summary.json` 的 `"stable": true` |
| S4_kappa | phono3py BTE → κ | 计算节点（**两版都是 CPU**） | `kappa_summary.json` 的 `KAPPA_DONE` |

## 三件必须知道的事

**1. 结构一定要用同一个势重弛豫。** 力常数是在**势自身的能量极小点**上做泰勒展开。
在 DFT 极小点上取 MACE 的力，残余力不为零，二阶力常数里混进一次项，Γ 附近声学支
直接掉成虚频——看起来像"这材料不稳定"，其实是流程错了。所以 S1 不是装饰步，
`RESIDUAL_TOL`（默认 2e-3 eV/Å）就是这道闸；S2 还会再测一次未位移超胞的残余力并扣掉
（不扣会破坏声学求和规则）。

**2. MACE 给不出 Born 有效电荷和 ε∞。** 势里没有电荷响应，这是原理性的，不是没实现。
极性材料（多数热电材料都是）不加 NAC，Γ 点 LO-TO 劈裂缺失，光学支偏、高温 κ 偏。
补救：拿同一材料 DFPT 算的 BORN 接进来，比如你已经用 `kl-dft-cpu` 跑过 `step3_nac`：

```
tf -tt kl-mace-cpu -p <材料> -j step3_fc conf --set \
   params.NAC_BORN=/public/home/.../<材料>/kl-dft-cpu/step3_nac/BORN
```

两边的原胞要是同一个设定（原子顺序、晶格取向）。MACE 弛豫后的晶格常数和 DFT 不完全
一样，Born 电荷对此不敏感，可以接受；但**不要拿另一个材料的 BORN 来凑**。

**3. `DTYPE` 必须 float64。** float32 的力误差在 1e-3 eV/Å 量级，足以在声学支上造出
几十 cm⁻¹ 的假虚频。两版的全局 `step.conf` 里都写死了，别改。

## 装

```bash
# 解包（会把引擎放进 _common/mace/，两个技能各一个目录）
tar xzf klmace2.tar.gz -C ~/software/taskflow/
tf skills            # 应看到 kl-mace-gpu / kl-mace-cpu 各 4 步、启用
```

超算上准备 conda 环境。环境名/路径写进 `setting/<hpc>.yaml` 的 `conda_env`
（MACE 技能构建 step.conf 时自动注入 `CONDA_ENV`，换超算自动跟着走；
项目 `project_setting/templates/step.conf` 可覆盖）：

```bash
# CPU 版（kl 技能 S3 默认用 pheasy 拟合，见文末「已知问题」的 numpy 2 兼容说明）
conda create -n mace python=3.11 -y && conda activate mace
pip install mace-torch phono3py symfc pheasy ase spglib h5py

# GPU 版（torch 按你们的 CUDA 版本装，别让 pip 装成 CPU 版）
conda create -n mace-gpu python=3.11 -y && conda activate mace-gpu
pip install torch --index-url https://download.pytorch.org/whl/cu121
pip install mace-torch phono3py symfc pheasy ase spglib h5py
python -c "import torch;print(torch.__version__, torch.cuda.is_available())"
```

GPU 集群上想把 S3 拟合换成 **pheasy-gpu**（`pheasy_gpu` 包 + `pheasy-gpu` 命令，CUDA/PyTorch 后端，与 `pheasy` 同参数、可并存安装）：

```bash
pip install -e '.[gpu]'    # 在 ~/software/pheasy-gpu 里执行，装出 pheasy-gpu 命令 + torch(CUDA)
# 然后把 S3 的拟合软件指过去：
tf -tt kl-mace-gpu -p <材料> -j 3 conf --set params.FIT_SOFTWARE=pheasy params.PHEASY_BIN=pheasy-gpu
```

> S3 拟合作业模板：`PHEASY_BIN=pheasy-gpu` 时 gen 自动改选 **submit_fc_gpu.tpl**
> （自动加 `--gres=gpu:<类型>:1` 并降核），只有 a800/3090 配了该模板；纯 CPU 集群
> （jzzn/hanhai25）gen 期直接报错提示换 `PHEASY_BIN=pheasy`，不会排进队才失败。
> 旧项目需重跑一次 `tf -p X init`（刷新项目 templates/）才能拿到新 GPU 模板。

`tf.yaml` 里加两条 work_dir（**两个 task_type，同一材料可以各跑一遍互相对照**，
目录不会打架）：

```yaml
task_types:
  kl-mace-gpu:
    work_dir: /public/home/wangchao/Fullerene_Network/work
  kl-mace-cpu:
    work_dir: /public/home/wangchao/Fullerene_Network/work
```

## 出虚频了怎么办

按这个顺序排查，**不要一上来就说材料不稳定**：

1. `step1_mace_relax/relax_summary.json` 的 `max_force_eV_per_A` 真的压到
   `RESIDUAL_TOL` 以下了吗？`spacegroup_in` 和 `spacegroup_out` 一样吗
   （变了说明弛豫跑出了原相）？
2. `step2_disp_force/forces_summary.json` 的 `residual_force_max_eV_per_A` 是否接近 0？
   不接近说明 S1 和 S2 用的不是同一个模型/dtype——**GPU/CPU 两版混用时特别容易踩**：
   S1 在 CPU 上弛豫、S2 在 GPU 上取力，只要模型和 dtype 一致就没问题，
   不一致（比如两版 `MACE_MODEL` 写得不一样）就会在这里露馅。
3. 加大超胞（`MIN_SC_LEN` 往上调一档），看虚频是不是随超胞变小/消失。是 → 有限尺寸
   效应，不是真软模。
4. 换 `DISP_DISTANCE`（0.01 / 0.05）。敏感 → 非谐性强，或模型在大位移处外推失效。
5. 换模型。基座模型在训练分布外的体系（比如富勒烯共价网络）上，软模判断经常不可信。
   有条件就用同体系 DFT 数据微调一版。
6. 以上都排除，才轮到"这个结构在 0 K 确实有软模"。

## 换超算（MACE 技能）

MACE 技能出厂 step.conf **不写死任何机器的 conda/模型路径**——`CONDA_SH`/`CONDA_ENV`/
`MACE_MODEL_DIR` 由 `setting/<hpc>.yaml` 的 `conda_sh`/`conda_env`/`mace_model_dir`
自动注入（最低优先级），所以换超算只需改项目的 hpc `name`（`tf -p <材料> hpc <集群>`），
环境与模型自动跟着走；项目级 `project_setting/templates/step.conf` 可覆盖。详见
`setting/README.md` 的「集群级默认」。

## 已知问题

- **pheasy 0.0.2 在 numpy 2.x 下会崩**（`np.math` 已被 numpy 2 删除，报
  `module 'numpy' has no attribute 'math'`）。kl 技能 S3 默认 `FIT_SOFTWARE=pheasy`，
  numpy≥2 的环境要先打补丁：把 site-packages 里
  `pheasy/core/cluster_orbit.py` 的 `np.math.factorial` 改成 `math.factorial` 并
  在文件头加 `import math`。或者项目级把 S3 的 `FIT_SOFTWARE` 设为 `symfc`。
- **symfc 拟合超大超胞（约 500 原子以上）的 fc3 可能 segfault**（在 numpy 2.4 +
  symfc 1.6.0 上实测 686 原子崩过）。`fc_fit_driver.py` 会自动退回 `--fc-calc alm`，
  但 alm 对超大超胞也很慢（半小时起）。遇到先把超胞缩小（`MIN_SC_LEN` 或显式
  `SUPERCELL`），或用 pheasy（见上一条）。
- **findiff 位移数由对称性决定，小原胞大超胞会爆**：`MAX_DISP`（默认 500）是硬闸，
  超了就停在 gen 阶段。提示会建议换 `METHOD=random` 或缩超胞。对 Si 这类
  |a|≈3.8 Å 的原胞，`MIN_SC_LEN=20` 一档就扩到 [7,7,7]=686 原子，两版都建议显式
  `SUPERCELL="4 4 4"`（约 128 原子）起步。

## 已知限制

- **只有 phono3py 求解器**。ShengBTE 那条路不做——fc3 的 ShengBTE 格式导出没有稳定
  出口，做半截比不做更坑。
- **NAC 必须外接**，见上文。非极性体系不受影响。
- **S1/S3 在登录节点跑**（`run: gen`）。原胞弛豫和力常数拟合通常几秒到几分钟；
  如果你们集群禁止登录节点跑 torch，或者超胞大到拟合吃几十 GB，就要把这两步改成
  提交模式（加 submit 模板、去掉 `run: gen`）。
- **没做批量推理**。`mace_forces.py` 是逐个结构调 ASE calculator。GPU 上做 batch
  推理还能再快 2~5 倍（尤其小超胞、帧数多时），但要绕开 ASE 直接用 torch 模型，
  版本兼容性差，暂时不值得。
- **fc3.hdf5 / kappa-m\*.hdf5 不拉回本地**（可上 GB），`fetch_files` 只拉摘要和日志。
- **精度需要你自己标定**。至少拿一个同族的、有 DFT 或实验 κ 的材料跑一遍对照，
  再去信这条链在新体系上的数。这不是流程能替你做的事。
