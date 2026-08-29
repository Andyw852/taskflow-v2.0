# kl-mace-gpu —— 晶格热导率（MACE，GPU 取力）

引擎、物理说明、安装、虚频排查在 `skill/_common/mace/README.md`（两版共用）。
本文只写 GPU 版特有的东西。

```
tf -tt kl-mace-gpu -p <材料> init
tf -tt kl-mace-gpu -p <材料> start
```

## GPU 只加速一步

| 步骤 | 跑在哪 |
|---|---|
| S1_relax | 登录节点，`DEVICE=auto` → 通常是 CPU（登录节点没卡） |
| **S2_force** | **GPU 队列，`DEVICE=cuda`** ← 唯一吃卡的一步，也是整条链最贵的一段 |
| S3_fc | 登录节点，CPU（力常数拟合，没有 GPU 路径） |
| S4_kappa | CPU 队列。**phono3py 的三声子求解没有 CUDA 实现**，和 CPU 版完全一样 |

所以 "GPU 版" 的准确含义是：位移超胞的力评估上卡。别指望 S4 变快——那一步该调的是
`MESH`、`BTE=rta`、核数。

## 卡不可用会当场失败，不会悄悄退回 CPU

`mace_model.pick_device()` 在 `DEVICE=cuda` 且 `torch.cuda.is_available()` 为假时直接
退出，`submit_mace.tpl` 里还有一道独立的前置检查。这是故意的：在 GPU 队列上退回 CPU，
作业照跑、结果照出，但占着卡跑了几十倍的时间，等你发现时机时已经烧完了。

报错时按这个顺序查：作业申请 `--gres=gpu:N` 了吗 → 分区名对吗 →
`pip show torch` 的版本名带不带 `+cu`（装成 CPU 版是最常见的原因）。

## 一个可能让你白高兴的坑：消费级卡的 FP64

`DTYPE=float64` 是硬要求（float32 的力噪声会造出假虚频）。但**消费级卡
（RTX / GeForce / TITAN）的 FP64 吞吐只有 FP32 的 1/32~1/64**，这种卡上 GPU 版
未必比整节点 CPU 快。数据中心卡（A100 / H100 / V100）的 FP64 是 1/2，才是稳赚。

脚本会在日志里识别并提醒你。**第一次上机务必两版各跑一个小体系比一下**
`forces_summary.json` 里的 `sec_per_frame`，再决定长期用哪版。别因为"有卡就该用卡"
把队列排到天亮。

## 建议的参数取向

GPU 版的钱该花在**二阶超胞**上，不是无脑放大 fc3 超胞：二阶的长程尾巴决定声速和低频
支（也就决定 κ），三阶短程收敛快。

```bash
tf -tt kl-mace-gpu -p <材料> -j step2_disp_force conf --set params.FC2_SUPERCELL="6 6 6"
tf -tt kl-mace-gpu -p <材料> -j step2_disp_force conf --set params.MIN_SC_LEN=22
tf -tt kl-mace-gpu -p <材料> -j step4_kappa conf --set params.MESH_SCAN="16 16 16; 20 20 20; 24 24 24"
```

默认值：`MIN_SC_LEN=20`、`KAPPA_MESH=24 24 24`、`CKPT=50`、`N_RANDOM=200`，
`[submit]` 里 `partition=gpu`、`gres=gpu:1`。队列名按你们集群改。

多卡不会更快——`mace_forces.py` 没做数据并行，申请多卡只是浪费。

## 模型

看 `templates/mace/README.md`。GPU 版显存够的话可以考虑 `mace-mp:large`，
推理时间增加不到一倍，精度是白拿的。
