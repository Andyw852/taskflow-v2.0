# defect-dft-cpu —— 缺陷形成能 + P/N 型判定技能（修正版方法论）

对 A2B2Te5（A=Pb/Sn，B=Bi/Sb，POSCAR_B 稳定相）这类 V2VI3 家族层状碲化物，
做本征缺陷形成能并判断 P/N 型。band+ELF 只能给带隙与带边，P/N 与"最容易的缺陷"
必须靠缺陷超胞总能，且必须用下面这版修正后的方法论。

## 流程（5 步，全自动，tf 一条龙）

| 步 | 内容 | 类型 |
|---|---|---|
| S0_refs | 凸包参考相（5 元素相 + 4 二元相）PBE-D3+SOC 弛豫 | run:gen，4 材料共享一份 |
| S1_bulk | 3x3x1 完美超胞 PBE-D3+SOC 弛豫 + LOCPOT | 单作业，与 S0 并行 |
| S2_def | 空位/反位/互占位对/间隙 中性弛豫 | fanout(每个缺陷一作业) |
| S3_chg | 各缺陷带电态单点(NELECT±q, LVHAR) | fanout |
| S4_anlys | 凸包出化学势 → 形成能/转变能级/自洽 E_F/P-N | run:gen(登录节点) |

- S0 参考相在 step.conf 的 REFERENCES_DIR（默认 /public/home/wangchao/convex_hull_refs）
  里算一份，gen_step0_references.py 幂等：首个材料提交作业，后续材料发现已收敛直接复用。
- S4 需要 references_energy.json（S0 产出）+ step1_bulk（目标相总能）才能出化学势；
  若 energies.json 缺 E_gap/epsilon/mstar，S4 会提示补上后重跑。

## 关键修正（相对"空位主导"的直觉）

1. 缺陷化学主角是反位，不是空位。Bi2Te3/Sb2Te3 家族里三个子晶格的空位形成能都远高于
   反位：Bi 富时主导 Bi_Te（受主反位）、Te 富时主导 Te_Bi。Sb2Te3 的 Sb_Te 形成能更低 →
   全组分范围强 p 型。判据是 (chi, r) 模型：阳离子-阴离子电负性差/尺寸差越小，反位形成能
   越低（Sb_Te≈0.35 eV < Bi_Te≈0.50 eV < Bi_Se≈0.64 eV）。
2. "ns2 孤对"不能直接推 P/N。正确链条是：孤对活性 → Sb–Te 键更共价(ELF 更连续) →
   反位形成能更低 → p 型更强。Sn/Pb 侧走的是阳离子尺寸机制：SnTe 本征 Sn 空位导致 p 型、
   空穴浓度 ~10^20–10^21 cm^-3；PbTe 阳离子空位空穴仅 ~10^18 cm^-3。
   预测：四个材料大概率全偏 p 型，强度 Sn2Sb2Te5 > Pb2Sb2Te5 ≈ Sn2Bi2Te5 > Pb2Bi2Te5。
3. 超胞用 3x3x1（面内），不是 2x2x2。vdW 层状材料层间耦合弱，c 方向 1 层足够；2x2x2 把
   原子数翻倍而面内镜像距离不变(8.7 A)，纯浪费。3x3x1 面内 13.06 A、81 原子，匹配文献。
   再用 4x4x1 做一次收敛校验。
4. 0.1 eV 窄带隙 → "中带隙最低 Ef"捷径失效。形成能误差(0.1–0.3 eV)与带隙同量级，自洽 EF
   几乎必落进能带(简并)，必须用真实 DOS 做有限温 Fermi–Dirac 积分，不能用 Boltzmann 近似；
   Bi_Te/Te_Bi 是带边共振态，"转变能级"可能不在带隙内。
5. E_VBM 必须与缺陷超胞同参数取、做势对齐（core-level/静电势），不能用 band_summary 里
   原胞的原始本征值——否则整条 Ef 曲线平移，P/N 直接错。
6. 带电缺陷必做 eFNV(各向异性, 用 DFPT 含离子贡献的介电张量) + band-filling 修正。
7. HSE 只对 PBE 批的前 3–5 名做校验；主力 PBE(+D3)+SOC 批量。HSE 对这类拓扑材料有过度
   打开带隙/破坏能带反转的风险，需与 Bi2Te3/PbTe/SnTe 实验带隙做泛函基准。

## 结构事实（已用 pymatgen 对 POSCAR_B 验证）

- 空间群 P-3m1 (#164)，9 原子/胞，1 个 NL；堆垛 Te–A–Te–B–Te–Te–B–Te–A。
- 5 个不等价位：Sb(2c,z≈0.334)、Te(2d,z≈0.428)、Te(2d,z≈0.225)、Te(1a,z=0)、Pb/Sn(2d,z≈0.113)
  → 5 空位 + 10 反位 + 1 互占位对 + 间隙。
- vdW 间隙(Te–Te)≈2.52 A（比 Bi2Te3 的 ~3.6 A 更紧，层间耦合更强）。
- 第一壳 3+3 劈裂：Sb Δd≈0.16 A（明显 3+3 畸变，支持 Sb 孤对活性）；Sn/Pb Δd≈0.01–0.02 A（几何上无孤对活性）。

## 电荷态清单（按价电子数判据：Te=6 > Sb/Bi=5 > Pb/Sn=4，X 占 Y 位价电子多→施主/少→受主）

- v_Te: 0/+1/+2（施主，缺阴离子）；v_Sb/v_Pb/v_Sn: 0/−1/−2（受主，缺阳离子）
- Sb_Te/Bi_Te（阳离子占 Te，少 1 电子）: 0/−1（受主）；Pb_Te/Sn_Te（少 2）: 0/−1/−2（受主）
- Te_Sb/Te_Bi（Te 占阳离子，多 1 电子）: 0/+1（施主）；Te_Pb/Te_Sn（多 2）: 0/+1/+2（施主）
- Sb_Pb/Bi_Sn（5 占 4）: 0/+1（施主）；Pb_Sb/Sn_Bi（4 占 5）: 0/−1（受主）；互占位对: 0
- 间隙：Te 0/−1/−2（受主）；阳离子(Sn/Sb/Pb/Bi) 0/+1/+2（施主）——间隙保持自身价态，只取单边

## 计算资源（重要）

- jzzn（默认）：VASP 6.4.3 + POTCAR 库(potpaw_PBE_54/64) + 192 核节点，HSE+SOC 唯一现实选择。
- 3090：仅 CPU VASP（vasp_normal，24 核，无 GPU-VASP），且未找到 POTCAR 库——只适合补
  POTCAR 后跑 PBE 批；HSE 在 24 核上不现实。切换： tf -p 材料名 hpc 3090

## 使用（全自动，5 步一条龙）

1. 材料目录放已弛豫 POSCAR_B（原胞）。
2. 一次性准备凸包参考相（只需做一次，4 材料共享）：把 9 个相 POSCAR 放进
   REFERENCES_DIR/convex_hull_references/<相>/POSCAR（Pb_fcc、Sn_beta、Sb_rhombo、
   Bi_rhombo、Te_trig、PbTe_rs、SnTe_rs、Sb2Te3、Bi2Te3）。
3. tf -tt defect-dft-cpu -p 材料名 init → 检查 → start 提交 → watch 无人值守。
   S0 参考相与 S1 bulk 并行；S4 自动跑凸包出化学势、再算形成能/转变能级/P-N。
4. 补 energies.json 的物理量（凸包只填 mu，还需手动/脚本补）：
   E_gap（band 步）、epsilon（静态介电常数）、mstar_e/mstar_h（有效质量）。
   S4 会提示缺哪项；补全后 retry S4 即得最终结论 formation_energy_results.json。

各脚本（AI/人可单独调用）：
- gen_step0_references.py 生成+提交+收集参考相能量（幂等）
- convex_hull.py          参考相能量 → 化学势窗口 → energies.json
- formation_energy.py     形成能/转变能级/自洽 E_F(费米-狄拉克积分)/P-N
- gen_references.py       参考相输入的一键生成器（等价于 gen_step0 的生成部分）

## 自动化（v1.11）

- 挂死自动恢复：tf watch 用**进度指纹**判定挂死——(OUTCAR 字节数, OSZICAR 行数)
  连续 hang_min_stale_rounds（默认 2）轮不变且输出年龄超 hang_stale_secs 才算
  （指纹在涨 = 活着；SCF 迭代 rms 还在降 = 慢但活着，都不判）。判定后按原因处理：
  SCF 空转 → 自动升级 INCAR（补 AMIX/BMIX → ALGO=All → NELM≥200，原子写+备份
  INCAR.bak.*）→ scancel（等退出）+ 校验 CONTCAR 续跑重交（旧输出存 *.hung），并给
  hang_grace_rounds 轮宽限期；NODE_FAIL → 直接重跑；磁盘满 → 只告警不重跑。
  每个作业最多 hang_max_retries 次，计数在 <配置目录>/.tf_hung.json；超限只告警。
  ★当前 hang_dry_run: true（观察期只打印判定不动手），确认无误后改 false。
  参数优先级：项目 setting.yaml > 技能 task_types.defect-dft-cpu.* > 全局 tf.yaml >
  默认。关掉写 hang_check: false，不想自动改 INCAR 写 hang_fix_scf: false。
- SOC 缺陷弛豫 SCF 易空转（空位悬挂键电荷涨落），INCAR 模板默认 ALGO=All + AMIX=0.1 +
  BMIX=0.0001（精细混合），配合上面的挂死自动恢复，整条流水线可无人值守。
