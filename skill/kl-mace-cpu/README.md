# kl-mace-cpu —— 晶格热导率（MACE，CPU 取力）

引擎、物理说明、安装、虚频排查在 `skill/_common/mace/README.md`（两版共用）。
本文只写 CPU 版特有的东西。

```
tf -tt kl-mace-cpu -p <材料> init
tf -tt kl-mace-cpu -p <材料> start
```

## 为什么还要 CPU 版

不是"没卡时的降级方案"，有三个正当理由：

1. **CPU 队列排得到。** 多数集群 GPU 卡少、排队久；MACE 在 CPU 上也就是慢几倍，
   排队省下的时间常常把这几倍抵掉。
2. **消费级卡的 FP64 反而慢。** RTX / GeForce 的 FP64 吞吐只有 FP32 的 1/32~1/64，
   而本流程强制 float64（float32 的力噪声会造出假虚频）。那种卡上 CPU 版可能更快。
3. **不吃显存。** 大超胞（几千原子）在小显存卡上会 OOM，CPU 只受内存限制。

**别猜，测。** 两版各跑一个小体系，比 `forces_summary.json` 里的 `sec_per_frame`。

## CPU 上真正管用的省钱开关

`MIN_SC_LEN` 一调大，超胞原子数按三次方涨，位移帧数也跟着涨——两头一起吃。
按性价比从高到低：

| 开关 | 怎么调 | 为什么 |
|---|---|---|
| `METHOD=random` + `N_RANDOM` | `N_RANDOM=auto` 按 ALM 数自由力常数自动反推帧数；写整数则固定 | findiff 的帧数由对称性决定、**你控制不了**；random 用 ALM 的 nfree 反推，帧数随体系自适应 |
| `FC2_SUPERCELL` 单独放大 | 如 `"5 5 5"`，fc3 超胞保持中等 | 二阶的长程尾巴决定声速和低频支，而 fc2 的帧数远少于 fc3 |
| `KAPPA_MESH` 先小后大 | 先 `16 16 16` 看数量级 | S4 也在 CPU 上，网格加密一档很贵 |
| `MACE_MODEL` 换 small | 最后才考虑 | 快 2~3 倍，但势的质量下降、虚频判断更不可信 |

```bash
tf -tt kl-mace-cpu -p <材料> -j step2_disp_force conf --set params.METHOD=random
tf -tt kl-mace-cpu -p <材料> -j step2_disp_force conf --set params.N_RANDOM=120   # 固定帧数；不设=auto
tf -tt kl-mace-cpu -p <材料> -j step2_disp_force conf --set params.FC2_SUPERCELL="5 5 5"
```

默认值：`MIN_SC_LEN=15`、`KAPPA_MESH=20 20 20`、`CKPT=10`、`N_RANDOM=auto`（按 ALM 数出的
自由力常数个数反推：`N=ceil(Σnfree/DOF)×OVERSAMPLE`，`OVERSAMPLE=3`、`ALM_CUT3=6.0 Å`）。

## 核数与断点

`submit_mace.tpl` 是**单进程多线程**（`ntasks-per-node=1` + `cpus-per-task=48`）。
MACE 靠 torch 线程并行，`mpirun -np N` 只会起 N 份互相抢核的独立进程，把同一件事
算 N 遍。

MACE 在 CPU 上的线程扩展性一般 16~32 核就饱和，整节点 48 核未必快多少，还要多等队列。
第一次跑完看 `mace_forces.log` 里的「帧/s」，再决定下次申请多少。

`CKPT=10` 意味着每 10 帧落一次盘。作业被墙钟砍掉后 `tf -tt kl-mace-cpu -p <材料> retry`
从断点接着跑，不用从头再来——CPU 版跑得久，这个比 GPU 版更常用到。

## 起始结构：复用其它链的 CONTCAR

kl 链的 S1（MACE 弛豫）从**材料根目录的 `POSCAR`** 开始（tf 的 gen 一律从材料根取初始
结构，技能子目录里的 POSCAR 不被使用），**总是会重新弛豫**（MACE 势自己的极小点），但
起始点越接近极小，离子步越少、越稳。

**建议**：同材料在其它链（ke-dft-cpu / opt-dft-cpu / band-dft-cpu / elastic-dft-cpu）
已有优化好的 `CONTCAR` 时，把它复制成材料根 `POSCAR` 作各技能 S1 起始：

```bash
# 例：复用 ke-dft-cpu 的优化结构（ke 链先跑完的前提下；原始 POSCAR 先备份）
cp <材料>/POSCAR <材料>/POSCAR_raw            # 只备一次
cp <材料>/ke-dft-cpu/result/step1_opt/CONTCAR <材料>/POSCAR
```

- **kl-dft-cpu 的 `reuse_structure.py` 自动做这件事**：`python3 reuse_structure.py <材料名>`，
  按 ke → opt → band → elastic 顺序找候选 CONTCAR（候选目录见脚本 `_STEP1_CANDS`），
  命中就复制成材料根 POSCAR，覆盖前备份 `POSCAR_raw`。
- **影响面**：材料根 POSCAR 是所有技能（ke/opt/band/elastic/kl）共用的初始结构，
  覆盖后未跑 S1 的技能都会从复用结构开始（已跑完 S1 的不受影响）。
- **注意**：复用只是给 S1 更好的起始点，**不跳过 S1**；S1 仍会完整弛豫（MACE 势）。

## 模型

看 `templates/mace/README.md`。一句话：模型在超算上放一份（`push_model.sh`），
`MACE_MODEL_DIR` 指过去，所有材料共用。
