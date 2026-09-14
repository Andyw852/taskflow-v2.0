"""CPU-only orchestration regressions; no MACE, scheduler or scientific packages."""
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for p in ('skill/_common/opt', 'skill/_common/mace', 'skill/_common'):
    sys.path.insert(0, str(ROOT / p))
import gen_step4_kappa as k
import gen_step3_fc as fc

class Array:
    def __init__(self, values): self.values = values
    def __len__(self): return len(self.values)
    def __getitem__(self, key):
        if isinstance(key, tuple): return self.values[key[0]][key[1]]
        return self.values[key]
    def __sub__(self, other): return Array([v-other for v in self.values])
    def __abs__(self): return Array([abs(v) for v in self.values])
    def tolist(self): return self.values

class Regression(unittest.TestCase):
    def setUp(self):
        (ROOT / 'tmp').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / 'tmp', prefix='klmace-test-')
        self.addCleanup(self.temp.cleanup)
        self.cwd = Path(self.temp.name)
        self.old = Path.cwd()
        os.chdir(self.cwd)
        self.addCleanup(os.chdir, self.old)

    def test_extract_scan_order_and_missing(self):
        values = {'kappa-m404040.hdf5': 40, 'kappa-m808080.hdf5': 80,
                  'kappa-m120120120.hdf5': 120, 'kappa-m999.hdf5': 999}
        class H5:
            def __init__(self, name, mode): self.value = values[name]
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def __getitem__(self, key):
                return [300.] if key == 'temperature' else [[self.value]*6]
        np = types.SimpleNamespace(array=lambda x: Array(x), abs=abs,
            argmin=lambda x: min(range(len(x)), key=lambda i:x[i]))
        modules = {'numpy': np, 'h5py': types.SimpleNamespace(File=H5), 'spglib': None}
        meta = {'meshes': ['40 40 40', '80 80 80', '120 120 120']}
        script = k.build_extract(1., meta).split('\n',1)[1].rsplit('\nPY',1)[0]
        with patch.dict(sys.modules, modules), patch('glob.glob', side_effect=lambda p: list(values) if p=='kappa-m*.hdf5' else []):
            exec(script, {})
            out = json.loads(Path('kappa_summary.json').read_text())
            self.assertEqual(out['kappa_300K_xx_yy_zz'], [120]*3)
            self.assertEqual(len(out['runs']), 3)
            del values['kappa-m120120120.hdf5']
            exec(script, {})
            self.assertFalse(json.loads(Path('kappa_summary.json').read_text())['KAPPA_DONE'])

    def test_shengbte_resolved_mesh_nac_and_normalization(self):
        src = self.cwd/'step3_fc'; src.mkdir()
        for f in ('fc2.hdf5','fc3.hdf5','BORN','POSCAR','phono3py_disp.yaml'):
            (src/f).write_text('fixture')
        (src/'klmace_params.txt').write_text('DIM=2d\nMESH=80 80 1\nSUPERCELL=2 2 1\n')
        conf = {name: spec[0] for name,spec in k.SPEC.items()}
        conf.update(SOLVER='shengbte', MESH_SCAN='15 15 15', KAPPA_NAC='off')
        class Conf(dict): submit = {}
        controls=[]; submitted=[]
        with patch.object(k.stepconf,'load',return_value=Conf(conf)), \
             patch.object(k.kc,'resolve_dim',return_value=('2d',2)), \
             patch.object(k,'_ensure_shengbte_fc',return_value=(src/'fc2.hdf5',src/'fc3.hdf5')), \
             patch.object(k,'_shengbte_control_from_poscar',side_effect=lambda *a:controls.append(a)), \
             patch.object(k,'two_d_norm_factor',return_value=(4.,{'kappa_2d_norm_factor':4.,'Lz_ang':20.,'thickness_d_ang':5.,'thickness_convention':'fixed'})), \
             patch.object(k.kc,'resolve_submit',return_value='unused'), \
             patch.object(k.kc,'write_submit',side_effect=lambda *a:submitted.append(a[2])), \
             patch.object(k.stepconf,'apply_submit'):
            k.main()
        self.assertEqual(controls[0][2], '15 15 1')
        self.assertFalse(controls[0][8])
        outdir=self.cwd/'step4_kappa'; os.chdir(outdir)
        Path('BTE.KappaTensorVsT_RTA').write_text('300 1 0 0 0 2 0 0 0 3\n')
        script=submitted[0]['SB_EXTRACT'].split('\n',1)[1].rsplit('\nPY',1)[0]
        exec(script,{})
        result=json.loads(Path('kappa_summary.json').read_text())
        self.assertEqual(result['kappa_2d_normalized_300K_xx_yy_zz'],[4.,8.,12.])

    def test_phonon_gate_fc2_and_nac(self):
        Path('fit_config.json').write_text(json.dumps({'nac': True, 'yaml': 'phono3py_params.yaml'}))
        ph3 = types.SimpleNamespace(unitcell='unit', supercell_matrix='small',
            phonon_supercell_matrix='large', primitive_matrix='primitive', nac_params={'born': 'charges'})
        seen = []
        class Phonopy:
            def __init__(self, unit, supercell_matrix, primitive_matrix):
                seen.append(self); self.scm = supercell_matrix
            def run_mesh(self, **kw): pass
            def get_mesh_dict(self): return {'frequencies': [0.]}
            def auto_band_structure(self, **kw): pass
        modules = {'numpy': types.SimpleNamespace(asarray=lambda x:x, min=min),
            'h5py': types.SimpleNamespace(),
            'phono3py': types.SimpleNamespace(load=lambda *a, **kw: ph3),
            'phono3py.file_IO': types.SimpleNamespace(read_fc2_from_hdf5=lambda **kw: 'fc2'),
            'phonopy': types.SimpleNamespace(Phonopy=Phonopy)}
        with patch.dict(sys.modules, modules): exec(fc._PHONON_GATE, {})
        self.assertEqual(seen[0].scm, 'large')
        self.assertEqual(seen[0].nac_params, {'born': 'charges'})

    def test_cpu_defaults(self):
        for base in (ROOT/'skill/kl-mace-cpu/templates', ROOT/'test/tf_test/Si/kl-mace-cpu/project_setting/templates'):
            text=(base/'step2_disp_force/step.conf').read_text()
            line=next(l for l in text.splitlines() if l.startswith('KAPPA_MESH'))
            self.assertEqual(line.split('=',1)[1].split('#')[0].strip(),'15 15 15')
            text=(base/'step4_kappa/step.conf').read_text()
            line=next(l for l in text.splitlines() if l.startswith('MESH_SCAN'))
            self.assertEqual(line.split('=',1)[1].split('#')[0].strip(),'')
        self.assertEqual(k.meshes({'MESH_SCAN':'','MESH_OVERRIDE':'15 15 15'}, {}, '2d',0),['1 15 15'])

if __name__ == '__main__': unittest.main()
