# fc-fit — force-constant fitting (fc2 / fc3)

Fit second- and third-order interatomic force constants from an existing
displacement + force dataset. Three interchangeable engines:

| `FIT_ENGINE` | Method | Dataset it accepts |
|---|---|---|
| `phono3py` | phono3py + symfc or alm (least squares) | finite-displacement **and** random-displacement |
| `pheasy` | pheasy compressive sensing (OLS / LASSO / ALASSO / RFE / RFE-OLS-TSQR / RIDGE) | random-displacement |
| `hiphive` | hiphive cluster space + linear regression (ols / ridge / lasso / ard / bayes), optional Huang + Born-Huang projection | random-displacement |

The skill **never runs VASP** and **never generates displacements**. It is the
force-constant-fitting branch lifted out of `kl-dft-cpu` S5_fc
(`gen_step5_fc.py` + `kl_fc_backends.py` + `submit_fit_*.tpl` +
`templates/step5_fc/step.conf`), decoupled from the thermal-conductivity
pipeline so that any dataset can be fitted, and extended with a third engine
(hiphive).

## Pipeline

~~~text
S1_fit   (submitted job, compute node)
         prep  normalise the dataset  -> POSCAR / SPOSCAR / dataset_*.npy
                                         disp_matrix.pkl (pheasy input)
                                         phono3py_*.yaml passthrough, BORN
         fit   fc2 (+ fc3) with the selected engine   -> fc2.hdf5 (+ fc3.hdf5)
         post  ShengBTE export (optional) + imaginary-frequency gate
                                         -> phonon_summary.json, fc_fit_summary.json
S2_plot  (login node, optional)  phonon band figures from fc2.hdf5
~~~

## Quick start

~~~bash
cd <material root>
tf -tt fc-fit -p <material> -j step1_fit conf          # effective parameters
tf -tt fc-fit -p <material> -j step1_fit \
   conf --set params.FIT_ENGINE=hiphive               # switch engine
tf -tt fc-fit -p <material> start                     # generate inputs + submit
tf -tt fc-fit -p <material> status                    # collect and inspect
~~~

The gen step locates the dataset automatically. It searches `step4_disp`,
`step2_disp_force`, `step2_disp`, `step3_disp` in the skill directory, then the
sibling skills' step directories (`../kl-dft-cpu/step4_disp`,
`../kl-mace-cpu/step2_disp_force`, `../kl-mace-gpu/step2_disp_force`,
`../phonon-dft-cpu/step2_disp`, `../phonon-mace-cpu/step2_disp_force`,
`../phonon-mace-gpu/step2_disp_force`) and then every sibling skill directory
whose step starts with `step*disp*` or `step*force*`, so a producer that does
not exist yet is found without editing anything. Point it somewhere else explicitly:

~~~bash
tf -tt fc-fit -p <material> -j step1_fit \
   conf --set params.FIT_INPUT_DIR=../<other-skill>/<step>
~~~

## Dataset contract

Six input layouts are recognised, in this priority order:

| # | Files in the dataset directory | Needs |
|---|---|---|
| 1 | `phono3py_params.yaml` (forces embedded) | phono3py |
| 2 | `phono3py_disp.yaml` + `FORCES_FC3` | phono3py |
| 3 | `phono3py_disp.yaml` (forces embedded) | phono3py |
| 4 | `dataset_disps.npy` + `dataset_forces.npy` | phonopy only |
| 5 | `disp_matrix.pkl` + `force_matrix.pkl` | phonopy only |
| 6 | `disp-*/vasprun.xml` (+ `phono3py_disp.yaml`) | phonopy + phono3py |

Layouts 1-3 and 6 are read with phono3py's own YAML/dataset machinery; layouts
4-5 are plain NumPy/pickle arrays. In every case prep normalises the data to

~~~text
POSCAR                                  unit cell
SPOSCAR                                 supercell
dataset_disps.npy, dataset_forces.npy   (n+1, natom_super, 3); Cartesian
                                        displacements by default (trailing frame
                                        = the zero equilibrium reference), or
                                        fractional coordinates when COORDS =
                                        fractional (trailing frame = reference)
disp_matrix.pkl, force_matrix.pkl       (n, natom_super, 3), equilibrium
                                        subtracted - this is what pheasy reads
fc_dataset.json                         frames, atoms, RMS displacement,
                                        provenance, matrices
~~~

Displacements are **Cartesian, in Angstrom** by default.  Set
`COORDS = fractional` when `dataset_disps.npy` holds fractional coordinates;
the driver then subtracts the trailing reference frame, minimum-image wraps and
converts to Cartesian with the supercell lattice (mirrors
`prepare_dataset.py --frac`).  The RMS gate (1e-8 .. 1 A) does **not** catch a
fractional array fed as Cartesian, so the driver warns when it sees no zero
reference frame and every value in [0,1).

Layouts 4-6 do not always carry a supercell matrix (a bare `.npy`/`.pkl`
dataset has no YAML to record one), so the gen falls back to the `POSCAR` /
`SPOSCAR` edge ratio for `SUPERCELL`, and `prep` records the result in
`fc_dataset.json` together with the atom order (see the pitfalls).

### Equilibrium residual forces

The reference (perfect) supercell is rarely force-free. Subtract its residual
forces from every frame or the fit is biased:

* `SUBTRACT_EQUILIBRIUM = true` (default) does it automatically when the
  dataset carries a reference: `disp-00000/vasprun.xml` for the VASP layout, or
  a trailing all-zero displacement frame (the convention the kl skills write).
* `EQUILIBRIUM_FORCES_NPY = <file.npy>` supplies the reference by hand
  (`(natom_super, 3)`), for datasets without one.
* With no reference available the driver prints a note and fits the raw forces.

## Engines

### phono3py (FIT_ENGINE = phono3py, default)

`phono3py.produce_fc2` / `produce_fc3` with `FC_CALC = symfc | alm`, written as
`fc2.hdf5` / `fc3.hdf5`. Works for both finite-displacement and
random-displacement datasets, because phono3py rebuilds the finite-difference
set itself from the YAML + `FORCES_FC3`.

* `FC3_CUTOFF` - third-order cutoff in Angstrom; empty means "all interactions",
  which is correct but slow and produces a very large `fc3.hdf5`.
* symfc is the fast path; if it is not installed, or it fails on a very large
  supercell, install it (`pip install symfc`) or set `FC_CALC = alm`.

### pheasy (FIT_ENGINE = pheasy)

Runs the pheasy CLI in four steps - cluster space (`-s`), symmetry constraints
(`-c`), sensing matrix (`-d --disp_file`), fit (`-f --full_ifc`) - with
`--hdf5` so the output is the same `fc2.hdf5` / `fc3.hdf5` pair. The environment
tuning that used to live in the submit template (`PHEASY_ASR_*`, `PHEASY_SM_*`,
celer, two-level solvers) now lives in the driver (`_pheasy_env`), so the same
job runs on any cluster.

* `PHEASY_FIT_METHOD` - `OLS` (most memory hungry), `LASSO`, `ALASSO`,
  `RFE` (default), `RFE-OLS-TSQR`, `RIDGE`.
* `PHEASY_C2_CUTOFF` / `PHEASY_C3_CUTOFF` - cutoffs in Angstrom (empty = all
  interactions). Keep both comfortably below half the smallest supercell edge,
  otherwise periodic images double-count interactions.
* `PHEASY_RASR = BHH` imposes Born-Huang rotational invariance and the Huang
  equilibrium conditions, which is what makes a truncated fit physical.
* `PHEASY_BIN = pheasy | pheasy-gpu`. The GPU build needs the GPU submit
  template; if the cluster has no GPU nodes use the CPU build.
* **The GPU only accelerates the two-level sparse matvec.** pheasy builds a
  dense/CSR sensing matrix for LASSO unless `PHEASY_LASSO_TWOLEVEL=1` (OLS
  defaults to the two-level path via `PHEASY_OLS_TWOLEVEL`), and
  `PHEASY_GPU_LASSO_RESIDENT=1` / `PHEASY_GPU_TWOLEVEL_LASSO=1` *require* a
  `TwoLevelSM` — with the dense matrix pheasy raises
  `NotImplementedError: Resident GPU LASSO/ALASSO requires TwoLevelSM input`
  after the matrix has already been written. `PHEASY_LASSO_TWOLEVEL` (empty =
  auto: on when the resident GPU is requested) is the knob that makes them
  consistent; the driver also drops a resident request it cannot honour, with a
  `[WARN]`. `PHEASY_NGPU = N` is the one number the job's allocation and the
  solver share: the gen renders `--gres=gpu:N` from it (unless `[submit]` sets
  `gres`) and the driver exports `PHEASY_GPU_SM_NGPU=N` plus
  `PHEASY_GPU_SM_DEVICES=0..N-1`, so "run on three cards" is
  `PHEASY_NGPU = 3` and nothing else. `PHEASY_GPU_LASSO_RESIDENT = true`
  requests the resident backend from step.conf rather than the submit template.

  **Accuracy.** None of this changes the answer: the GPU matvec/adjoint match
  the CPU ones to ~1e-16 (pheasy-gpu's acceptance suite), the two-level matvec
  computes the same `SM_prime @ (NS @ x)` product as the materialised `SM`
  (only the rounding order differs, and it skips the float32 dense product
  altogether), and the resident LASSO keeps the same CV grouping, alpha grid and
  debias flow, in float64. Treat a systematic change in
  `pheasy_relative_error` / `pheasy_worst_force_correlation` / `fc2.hdf5` as
  a bug report, not as cost of doing business.

  Whatever happens, the fit records what actually ran:
  `pheasy_gpu_used` plus `pheasy_gpu_evidence` (`gpu_sm_matvec`,
  `gpu_resident_lasso`, `gpu_cv_folds`, `twolevel_sm`) land in
  `fit_metrics.json` / `fc_fit_summary.json`, and a GPU request with no
  marker prints a `[WARN]` instead of passing as a GPU run.
* **Quality gate.** For `LASSO`/`ALASSO` the driver parses the selected
  `alpha`; if it sits on the edge of the cross-validation grid the selection is
  meaningless (more frames are needed), the run is flagged and the S1 gate fails
  on purpose. Widen `PHEASY_MU_MIN`/`PHEASY_MU_MAX` or add frames.
* **Force correlation.** pheasy prints a per-configuration correlation of the
  fitted forces and warns when it drops. The driver reads it and records it as
  `pheasy_worst_force_correlation`. Below ~0.5 the dataset and pheasy's
  supercell almost certainly disagree on the **atom order** (see
  "Physics notes and pitfalls"), and the run is failed on purpose; between 0.5
  and 0.98 it is ordinary model error and only a note is printed.

#### Environment tuning: `PHEASY_TUNING`

`kl-dft-cpu` hard-coded the author's production solver settings in
`submit_fit_pheasy.tpl`. Those settings are not neutral on every dataset:

| `PHEASY_TUNING` | what it sets | BaS 250-atom / 10-frame / fc2+fc3 / OLS |
|---|---|---|
| `safe` (default) | memory layout, threads, CV grouping only | 2.2 % relative error, correlation 0.9997, gate **stable** |
| `kl` | the above **plus** the production solver block (`ILP64`, `ATOL`, `BTOL`) | same as `safe` here — those knobs are inert on this dataset |

The production solver block used to carry one more variable,
**`PHEASY_OLS_RIDGE=1e-4`**, and that one is not inert. It is not a normalised
sklearn ridge: pheasy's OLS turns it into Tikhonov damping
`damp = sqrt(ridge * n_samples)` appended to the LSMR system
(`core/optimizer.py::_ols_lsmr`), so its strength grows with the number of
equations — 0.87 at ndata = 7500, which swamps the design matrix. One
dataset, one sensing matrix, one seed:

| `PHEASY_OLS_RIDGE` | LSMR iterations | relative error | correlation |
|---|---|---|---|
| 0 | 509 | **2.2 %** | 0.9997 |
| 1e-10 | 508 | 2.2 % | 0.9997 |
| 1e-8 | 342 | 3.1 % | 0.9995 |
| 1e-6 | 72 | 6.8 % | 0.9977 |
| 1e-4 | 18 | **58 %** | 0.8991 |

`ridge > 0` also silently disables pheasy's resident **GPU** OLS, which falls
back to the CPU path. The failure mode is what makes this worth knowing:
pheasy still exits 0 and still writes `fc2.hdf5`; the only hints are the
correlation line and the LSMR iteration count in the log. The four production
templates that carried it (`jzzn`, `a800`, `3090`, `hanhai25`,
`submit_fit_pheasy*.tpl`) were corrected to `PHEASY_OLS_RIDGE=0` on
2026-09-11, so neither tuning profile sets it any more; an explicit
`PHEASY_OLS_RIDGE` in `step.conf` still wins, and the driver never inherits a
stray value from the environment.

### hiphive (FIT_ENGINE = hiphive)

Builds a `ClusterSpace` on the primitive cell from `HIPHIVE_CUTOFF2`
(+ `HIPHIVE_CUTOFF3` when `ENABLE_FC = 3`), adds every frame as a structure
carrying the documented `displacements` and `forces` arrays, stacks the design
matrix and solves it.

* `HIPHIVE_FIT_METHOD = ols | ridge | lasso | ard | bayes`, `HIPHIVE_ALPHA` for
  ridge / lasso. Both hiphive generations are supported: the legacy
  `hiphive.fitting.Optimizer` when present, otherwise a scikit-learn regressor
  (hiPhive 1.5 dropped its fitting module).
* `HIPHIVE_ENFORCE_ASR = true` projects the Huang and Born-Huang rotational sum
  rules onto the fitted parameters (`hiphive.enforce_rotational_sum_rules`).
* `HIPHIVE_N_CONFIGS` subsamples the frames when the design matrix becomes too
  large to hold in memory.
* hiphive derives its own cutoff-bounded atom list, but it aligns an arbitrary
  supercell internally, so the dataset supercell can be larger than the
  cluster-space cell. It must however be **at least twice the cutoff** in every
  direction, otherwise the periodic images alias.
* `fc2` is written densely (small). `fc3` is written with hiphive's streaming
  writers (`write_to_phono3py`, `write_to_shengBTE`) because the dense
  `(N, N, N, 3, 3, 3)` array is N^3 * 27 * 8 bytes - already ~3.4 GB for a
  250-atom supercell.
* `EXPORT_SHENGBTE` writes `shengbte/FORCE_CONSTANTS_2ND _3RD POSCAR`.  The
  **pheasy engine reuses the files pheasy itself wrote** (directly from its
  compact cluster IFCs, so the dense fc3 is never materialised); the phono3py
  engine re-exports through hiphive, and `FC3_LOAD_GB_LIMIT` (default 8 GB)
  skips the fc3 text when the dense `fc3.hdf5` would need more than that.

## Artifacts

~~~text
step1_fit/
  fit_config.json          everything the compute-node driver reads
  submit.sh                rendered from the engine template
  POSCAR SPOSCAR           cells used for the fit and the gate
  dataset_*.npy            normalised dataset
  disp_matrix.pkl force_matrix.pkl   pheasy input
  phono3py_disp.yaml phono3py_params.yaml FORCES_FC3 BORN   passthrough
  fc2.hdf5 [fc3.hdf5]      fitted force constants (phono3py layout)
  FORCE_CONSTANTS          fc2 in phonopy text format (hiphive path)
  shengbte/FORCE_CONSTANTS_2ND _3RD POSCAR    for ShengBTE / fourphonon
  band-dft-cpu.yaml        phonopy band structure of the fit
  fc_dataset.json          dataset audit (frames, atoms, RMS displacement,
                           supercell, frozen atom order, ...)
  fit_metrics.json         engine-reported fit quality (RMSE, correlation, ...)
  phonon_summary.json      the gate verdict (this is the step's marker)
  fc_fit_summary.json      identical content, skill-facing name
  queue.out queue.err      job log
~~~

`phonon_summary.json` carries `tool_ok`, `stable` and `min_frequency_THz`, so tf's
built-in `phonon` judge gives three outcomes:

* **stable** - the step is done, downstream steps may run;
* **imaginary frequency** - the fit finished but the mesh minimum is below
  `-IMAG_THR`; the step is *not* done and downstream steps stay held back (this
  is a physics result, not an error);
* **tool error** - the gate itself could not be evaluated; the job exits
  non-zero so the step shows as error and the log is worth reading.

`fc_fit_summary.json` adds the engine, frame count, atom count, the NAC/no-NAC
minima, the ShengBTE export status and - when `FIT_RMSE_FRAMES > 0` - the
training residual (`fit_rmse_eV_per_A`, `fit_rmse_relative`) evaluated by
predicting the forces of that many frames from the *fitted* force constants.
That number is engine independent and is the quickest way to tell a good fit
from a bad one; for a decent dataset it is well under 1 per cent relative.

The engines also report their own quality numbers, which the driver scrapes out
of the log into `fit_metrics.json` and copies into both summaries:
`hiphive_backend`, `hiphive_parameters`, `hiphive_design_matrix`,
`pheasy_relative_error`, `pheasy_worst_force_correlation`,
`pheasy_free_ifcs`. pheasy's correlation is the fastest warning sign
available and the driver acts on it (see the pheasy section).

## Physics notes and pitfalls

* **Supercell vs cutoff.** A cutoff larger than half the smallest supercell
  edge makes periodic images contribute twice. Either enlarge the dataset
  supercell or lower the cutoff.
* **Atom order (pheasy only).** pheasy does not read `SPOSCAR`: it rebuilds the
  supercell from `POSCAR` + `SUPERCELL` as *per-primitive-atom blocks* (all
  images of primitive atom 0, then atom 1, ...). phonopy and phono3py agree with
  that layout, so `kl-*` datasets are fine. A dataset assembled with ASE's
  `Atoms.repeat()` is *not*: it interleaves the images, so every atom except
  the first is scrambled. The fit still converges, still exits 0 and still
  writes `fc2.hdf5` -- it just lands near 50 % residual with a force correlation
  around 0.7. `prep` therefore compares the dataset supercell against pheasy's
  convention and freezes the answer in `fc_dataset.json`; when they differ the
  pheasy engine permutes `disp_matrix.pkl` / `force_matrix.pkl` into pheasy's
  order (the npy files and every other engine keep the dataset's order).
  Measured on the 16-atom test dataset: relative error 0.70 -> 0.0046,
  correlation 0.67 -> 1.0000.
* **pheasy rewrites `SPOSCAR`.** Its CLI writes the supercell in its own atom
  order into the step directory, so `SPOSCAR` describes pheasy's order after a
  pheasy run and the dataset's order after `prep`. `prep` always refreshes it
  from the dataset, and everything order-sensitive reads the frozen record
  instead of the file.
* **Training residual is not a validation score.** A random-displacement fit
  with enough parameters can reproduce its own training forces while
  extrapolating badly. Raise `FIT_RMSE_FRAMES` and, ideally, hold frames out.
* **2D materials.** For `DIM = 2d` the gate verdict uses the no-NAC minimum: the
  3D Coulomb kernel produces spurious imaginary frequencies near Gamma in a
  strictly 2D material. `DIM = auto` inherits the dimension from the dataset's
  `kl_params.txt` / `workflow_method.txt` when a sibling kl/phonon skill wrote
  one, and otherwise detects the vacuum axis.
* **NAC.** A `BORN` file in the dataset (from `kl-dft-cpu` step3_nac) is picked
  up automatically; both the NAC and no-NAC minima are reported, and only the
  3D verdict uses the NAC one.
* **Empty `FC3_CUTOFF` on a large supercell** is the classic way to exhaust the
  memory of a compute node. Watch `queue.err` if `fc3.hdf5` never appears.
  For the phono3py ShengBTE export, `FC3_LOAD_GB_LIMIT` skips the fc3 text when
  the dense `fc3.hdf5` exceeds it (the pheasy engine is unaffected — it writes
  ShengBTE from compact IFCs).

## Relation to the kl skills

| | `kl-dft-cpu` S5_fc | `fc-fit` S1_fit |
|---|---|---|
| Input | hard-wired to its own `step4_disp` | any dataset, `FIT_INPUT_DIR` or auto-search |
| Engines | phono3py, pheasy | phono3py, pheasy, **hiphive** |
| pheasy logic | inside the submit template | inside `fc_fit_driver.py` (testable, cluster independent) |
| Output | `step5_fc/phono3py/fc2.hdf5` (+ shengbte/) | `step1_fit/fc2.hdf5` (+ shengbte/) |
| Marker | `phonon_summary.json` | `phonon_summary.json` (+ `fc_fit_summary.json`) |
| Next step | kappa from the BTE solver | none - hand the artifacts to whoever needs them |

Nothing here replaces the kl skills: `kl-dft-cpu` / `kl-mace-*` still own the
end-to-end thermal-conductivity workflow. `fc-fit` is for fitting force
constants on their own - from a dataset another skill produced, from a
hand-assembled dataset, or as a sandbox for comparing engines on the same data.

## taskflow integration notes

Two things are specific to how this skill plugs into `tf`:

* **`submit_required`.** `tfpkg/workflow.py::_remote_submit_preflight` checks,
  before every `sbatch`, that the step's inputs exist locally, and its
  historical default is `INCAR, POSCAR, KPOINTS, submit.sh` for anything whose
  skill name does not contain *mace*. A fitting skill runs no VASP and its gen
  writes neither INCAR nor KPOINTS (the cells are copied by `prep` on the
  compute node), so the default would block every submission. The skill
  therefore declares what it really produces:

  ~~~yaml
  submit_required: [fit_config.json, submit.sh]
  ~~~

  `tfpkg/bootstrap.py` carries the key through `_MANIFEST_TYPE_KEYS`, and the
  preflight falls back to the old default when a skill does not declare it,
  so every existing skill behaves exactly as before.
* **The dataset is not part of the skill.** `tf` pushes only the gen, its
  `gen_need` files and the templates; the displacement+force dataset stays
  where it is. Point `FIT_INPUT_DIR` at it (an absolute path on the cluster
  works) or let the auto-search find a sibling `kl-*`/`phonon-*` step.

## Verification status

The rows below were run with the `atomate2_p_a` environment, pinned to what
the jzzn login node has (phonopy 2.47.1, phono3py 3.24.0, symfc 1.6.0,
hiphive 1.5, numpy 2.3.5), by invoking the compute-node driver directly — the
same `prep` / `fit` / `post` sequence the submit script runs -, except the
last row, which is a real `tf start` submission. Datasets: the 250-atom BaS
supercell produced by `kl-dft-cpu` (10 frames, `phono3py_params.yaml`), a
16-atom / 100-frame model dataset in the three array layouts, and a 128-atom
Si 4x4x4 supercell (90 frames) on the cluster.

| Case | Engine | Result |
|---|---|---|
| BaS 250 atoms, fc2 | hiphive (cutoff 6 A, 34 parameters) | stable, mesh min -2.5e-07 THz, training RMSE 6.4 % |
| BaS 250 atoms, fc2+fc3 | phono3py + symfc, c3 = 4 A | stable, min -8.8e-07 THz, RMSE 1.3 %, `fc2.hdf5` + `fc3.hdf5` + ShengBTE |
| BaS 250 atoms, fc2+fc3 | pheasy OLS, c3 = 4 A (189 free IFCs) | stable, mesh min -0.000 THz, relative error 2.2 %, correlation 0.9997 |
| 16 atoms, fc2+fc3 | pheasy LASSO (40-alpha grid) | relative error 0.46 %, correlation 1.0000 |
| 16 atoms, fc2+fc3 | hiphive (streaming fc3 writers, `shengbte/FORCE_CONSTANTS_3RD` written) | stable, RMSE 0.14 % |
| 16 atoms, fc2 | hiphive, pkl-only dataset (auto-detected, supercell deduced) | stable, mesh min -9.9e-08 THz |
| BaS 250 atoms | plot step (optional S2_plot, login node) | both PNGs + `phonon_band_summary.json`, bands 0-21 THz |
| Si 128 atoms, 90 frames, fc2 | pheasy OLS, c2 = 5 A, submitted through `tf start` to jzzn | ran on cu18, gate **stable**, but 80 % fit error -- the cutoff is far too tight, see below |
| Si 128 atoms, 90 frames, fc2+fc3 | pheasy OLS, c2 = inf, c3 = 5 A, same dataset | 147 free IFCs and 69.5 % relative error -- **identical to the user's own bench run**; gate correctly reports **imaginary** |

The three-way judge was exercised in all three states: **stable** (cases
above), **imaginary** (the deliberately under-fitted pheasy run, mesh min
-6.6 THz, job exits 0, marker unsatisfied) and **tool error** (the phonopy
route was checked against an ASE-Atoms supercell, which made the gate exit
non-zero instead of reporting a false verdict).

### Cluster run (jzzn, through `tf`)

A real submission, `tf -tt fc-fit -p Si_fcfit -j step1_fit start`, on a
2-atom Si cell / 128-atom 4x4x4 supercell / 90-frame pkl dataset on the
shared filesystem: job `3828909` ran on `cu18` and produced
`fc2.hdf5`, `shengbte/FORCE_CONSTANTS_2ND`, `phonon_summary.json`
(`stable=true`, min -4.2e-08 THz) and `fc_fit_summary.json`, with
`pheasy` and `celer` both present in the environment. The whole path is
exercised: login-node gen -> push -> `sbatch` -> compute node
(prep/fit/post) -> judge -> fetch.

Two submissions on the same dataset bracket the gate's behaviour:

* `PHEASY_C2_CUTOFF = 5.0` gives Si a **3-cluster** cluster space (27 IFCs, 6
  free) — a severe truncation — and the fit reports 80 % relative error with
  correlation 0.60. The verdict is still `stable`, and the gate prints the
  right diagnosis (*ordinary model error -- widen the cutoff or fit fc3 as
  well*) instead of passing silently.
* With the production cutoffs (`c2` = inf, `c3` = 5 A, fc2+fc3) the skill
  builds exactly the cluster space the user's own bench builds (HARM 297 IFCs
  / 138 free, ANHARM3 81 / 9) and lands on **69.5 % relative error, 147 free
  IFCs, correlation 0.698** — the bench's own run reports 69.45 % and the
  same 147. The independent RMSE check over 4 frames agrees (70.8 %).
  With that much residual the resulting spectrum is genuinely unstable
  (min -1.51 THz) and the judge says so: `stable=false`, status `imaginary`.

So the pipeline, the order check (`is_pheasy_order: true`, identity
permutation on this dataset), the quality gate and the phonon judge all
behaved correctly; the ~70 % residual is a property of that dataset. Both my
run and the user's own log report `Space group: R-3m (166), 12 symmetry
operations` for a 2-atom Si cell (diamond Si is Fd-3m, 192 operations), which
is worth a look before that dataset is used for anything beyond timing.

Still not exercised:

* **`PHEASY_BIN = pheasy-gpu`** is exercised as of 2026-09-12 (Mg4C60,
  512-atom supercell / 128 frames, 3090, `--gres=gpu:4`): cluster space, null
  space and the sensing matrix all ran, and the fit engine was the GPU build —
  but the *cards were idle*, because LASSO builds a dense/CSR matrix and only
  the two-level path has a GPU implementation. A GPU request on that path
  (`PHEASY_GPU_LASSO_RESIDENT=1` without a two-level matrix) fails hard with
  `NotImplementedError` ~2 minutes into the fit, after a 10.8 GB
  `sm_dense.npy`; the driver now reconciles the two flags and records
  `pheasy_gpu_used`. A non-zero `PHEASY_OLS_RIDGE` also disables the resident
  GPU OLS path.
* **Least-squares `phono3py` with `FC_CALC = alm`** — only `symfc` was run
  (the local phono3py 3.24.0 / phonopy 2.47.1 pair matches the cluster).
* **A dask/multi-node `phono3py` fit** on a supercell much larger than 250
  atoms.
