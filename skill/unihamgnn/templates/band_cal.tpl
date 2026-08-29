# band_cal.yaml —— 从预测的哈密顿量计算能带
# 由 gen_step3_band.py 渲染；{{...}} 是占位符。
# 注意 strcture_name 是 band_cal.py 里的历史拼写，勿改。
nao_max: {{NAO_MAX}}
graph_data_path: '{{GRAPH_DATA_PATH}}'
hamiltonian_path: '{{HAMILTONIAN_PATH}}'
nk: {{NK}}
save_dir: '{{SAVE_DIR}}'
strcture_name: '{{SYSTEM_NAME}}'
soc_switch: {{SOC}}
spin_colinear: false
auto_mode: true
Ham_type: 'openmx'
