# graph_data_gen.yaml —— overlap.scfout -> graph_data.npz
# 由 gen_step1_graph_data.py 渲染；{{...}} 是占位符。
nao_max: {{NAO_MAX}}
graph_data_save_path: '{{SAVE_PATH}}'
read_openmx_path: '{{READ_OPENMX}}'
max_SCF_skip: 200
scfout_paths: '{{SCFOUT_DIR}}'
dat_file_name: 'openmx.dat'
std_file_name: null
scfout_file_name: 'overlap.scfout'
soc_switch: {{SOC}}
