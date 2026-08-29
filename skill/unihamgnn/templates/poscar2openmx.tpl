# poscar2openmx.yaml —— POSCAR -> OpenMX .dat（non-SOC）
# 由 gen_step1_graph_data.py 渲染；{{...}} 是占位符。
system_name: 'openmx'
poscar_path: "{{POSCAR_PATH}}"
filepath: '{{FILEPATH}}'
basic_command: |+
  System.CurrrentDirectory         ./
  System.Name                     {{SYSTEM_NAME}}
  {{DATA_PATH_LINE}}
  level.of.stdout                   1
  level.of.fileout                  1
  HS.fileout                   on

  scf.XcType                  {{XC}}
  scf.SpinPolarization        off
  scf.ElectronicTemperature  {{ELECTRONIC_TEMP}}
  scf.energycutoff           {{ENERGY_CUTOFF}}
  scf.maxIter                 {{MAX_SCF_ITER}}
  scf.EigenvalueSolver        Band
  scf.Kgrid                  {{KGRID}}
  scf.Mixing.Type           rmm-diis
  scf.Init.Mixing.Weight     0.10
  scf.Min.Mixing.Weight      0.001
  scf.Max.Mixing.Weight      0.400
  scf.Mixing.History          7
  scf.Mixing.StartPulay       5
  scf.criterion             {{SCF_CRITERION}}

  MD.Type                      Nomd
  MD.maxIter                 100
  MD.TimeStep                1.0
  MD.Opt.criterion          1.0e-4

  MO.fileout                  off
  num.HOMOs                    2
  num.LUMOs                    2

  Dos.fileout                  off
  Dos.Erange              -10.0  10.0
  Dos.Kgrid                 1  1  1
