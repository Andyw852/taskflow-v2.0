# templates/mace/ —— MACE 模型库

放 `.model` 权重文件的地方。**它是本地模型库，不是 tf 的模板搜索路径。**

先把这一条说清楚，免得你把模型丢进来发现远端找不到：tf 找 `gen_need` 里的文件时，
搜索链是

```
templates/<步骤名>/ → templates/ → <技能根>/ → _common/… 
```

`templates/mace/` **不在这条链上**（它不是步骤名）。所以模型放这里 tf 不会自动推。
这是刻意的——`mace-mp` medium 权重 ~130 MB，`large` ~600 MB，按 `gen_need` 推送
意味着**每个材料的每一步都要比一次 md5、可能推一遍**，几十个材料下来纯属折磨 ssh。

## 推荐做法：模型在超算上放一份，所有材料共用

```bash
# 本地：模型下载/训练完丢进本目录
ls skill/kl-mace-cpu/templates/mace/
#   mace-mpa-0-medium.model
#   my-finetuned-C60.model

# 一次性推到超算（本目录的 push_model.sh 就干这个）
bash skill/kl-mace-cpu/templates/mace/push_model.sh jzzn /public/home/wangchao/software/mace_models
```

然后在 step.conf 里指名字（路径由 `MACE_MODEL_DIR` 提供）：

```
tf -tt kl-mace-cpu -p <材料> conf --set params.MACE_MODEL=mace-mpa-0-medium.model
tf -tt kl-mace-cpu -p <材料> conf --set params.MACE_MODEL_DIR=/public/home/wangchao/software/mace_models
```

`MACE_MODEL_DIR` 一般写进全局 `templates/step.conf` 一次就够，不用每个材料设。

## 备选：跟着 gen_need 走（小模型 / 每个材料用不同微调模型时）

把 `.model` 拷进 `templates/step2_disp_force/` 和 `templates/step1_mace_relax/`，
再把文件名加进 `skill.yaml` 里这两步的 `gen_need`。tf 就会把它推到材料目录，
`MACE_MODEL` 只写文件名即可（脚本会在步骤目录、材料目录里找）。

代价前面说了：每个材料一份副本。**只有在不同材料要用不同模型时才值得**（比如你为
某个体系单独微调过一版）。

## 再备选：基座模型联网下载

`MACE_MODEL = mace-mp:medium` 会走 `mace_mp()` 工厂函数自动下载到 `~/.cache/mace`。
计算节点通常没有外网，所以**第一次一定要在登录节点触发一次下载**（跑一遍 step1
就行，它在登录节点），把权重缓存好，step2 的作业才能离线用上。

## 记一笔：模型是结果的一部分

同一个结构、同一套流程，换个模型 κ 能差一倍以上。所以：

- 别用 `mace-mp:medium` 这种会随上游更新而变的写法发论文，**用固定的 `.model` 文件**，
  并把文件名和来源（版本号、下载日期、或你的微调脚本 commit）记在材料的实验记录里。
- `step1_mace_relax/relax_summary.json` 和 `step2_disp_force/forces_summary.json`
  里都存了 `model` 字段（含真实解析到的绝对路径），事后对不上账时先查这两处。
