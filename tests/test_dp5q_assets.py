"""Fail closed on invalid model manifests and numeric archives."""
from hashlib import sha256
import json
import io
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys
import zipfile
from unittest.mock import patch

import numpy as np

from checksi.dp5q.assets import AssetError, GEOMETRY_POLICY, MODEL_ID, load_assets, tensor_shapes, validate_asset_metadata


class AssetTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.arrays = {name: np.zeros(shape, dtype='<f4') for name, shape in tensor_shapes().items()}
        self.metadata = {
            'format_version': 1, 'model_id': MODEL_ID,
            'preprocessing_id':'dp5q-spatial-v1', 'score_id':'dp5q-logtail-v1',
            'geometry_policy':GEOMETRY_POLICY,
            'quantile_levels': np.linspace(.01,.99,99).tolist(),
            'tokens': {'16':6, '1':4, '17':7, '6':2, '7':5, '8':3,
                       '9':8, '15':9, '35':10, '14':11, '5':12},
            'tensors': {name: {'shape': list(array.shape), 'dtype':'float32', 'sha256': sha256(array.tobytes()).hexdigest()} for name, array in self.arrays.items()},
        }
        self.write()

    def write(self):
        np.savez(self.root/'weights.npz', **self.arrays)
        data = (self.root/'weights.npz').read_bytes()
        self.metadata['files'] = {'weights.npz': {'bytes':len(data), 'sha256':sha256(data).hexdigest()}}
        (self.root/'manifest.json').write_text(json.dumps(self.metadata))

    def test_exact_architecture_and_immutable_loaded_bytes(self):
        self.assertEqual(len(tensor_shapes()),57)
        self.assertEqual(sum(array.nbytes for array in self.arrays.values()),11_625_924)
        arrays, metadata = load_assets(self.root)
        self.assertEqual(metadata['model_id'],MODEL_ID)
        self.assertEqual(set(arrays),set(self.arrays))
        with self.assertRaises(ValueError):
            arrays['shared.atom_embedding'].setflags(write=True)
        with self.assertRaises(TypeError):
            arrays['other'] = np.array([])

    def test_missing_tampered_and_nonfinite_weights_refused(self):
        data = bytearray((self.root/'weights.npz').read_bytes())
        data[-20] ^= 1
        (self.root/'weights.npz').write_bytes(data)
        with self.assertRaises(AssetError):
            load_assets(self.root)
        self.arrays['shared.atom_embedding'][0,0] = np.nan
        self.write()
        with self.assertRaises(AssetError):
            load_assets(self.root)
        (self.root/'weights.npz').unlink()
        with self.assertRaises(AssetError):
            validate_asset_metadata(self.root)

    def test_manifest_and_archive_shape_disagreement_refused(self):
        self.arrays['shared.atom_embedding'] = np.zeros((1,256), np.float32)
        self.write()
        with self.assertRaises(AssetError):
            load_assets(self.root)

    def test_forged_huge_header_is_rejected_before_allocation(self):
        path = self.root/'weights.npz'
        with zipfile.ZipFile(path) as archive:
            members = {name:archive.read(name) for name in archive.namelist()}
        header = io.BytesIO()
        np.lib.format.write_array_header_1_0(header,{'descr':'<f4','fortran_order':False,'shape':(2**40,)})
        members['shared.atom_embedding.npy'] = header.getvalue()+bytes(13*256*4)
        with zipfile.ZipFile(path,'w') as archive:
            for name,data in members.items():
                archive.writestr(name,data)
        data = path.read_bytes()
        self.metadata['files']['weights.npz'] = {'bytes':len(data),'sha256':sha256(data).hexdigest()}
        (self.root/'manifest.json').write_text(json.dumps(self.metadata))
        original_empty = np.empty
        def bounded_empty(shape,*args,**kwargs):
            if int(np.prod(shape)) > 1_000_000:
                raise AssertionError('Unsafe header reached the array allocator')
            return original_empty(shape,*args,**kwargs)
        with patch('numpy.empty',side_effect=bounded_empty), self.assertRaises(AssetError):
            load_assets(self.root)

    def test_malformed_tokens_refused_as_asset_error(self):
        self.metadata['tokens']['1'] = []
        (self.root/'manifest.json').write_text(json.dumps(self.metadata))
        with self.assertRaises(AssetError):
            validate_asset_metadata(self.root)

    def test_swapped_trained_tokens_cannot_reuse_the_fixed_preprocessing_identity(self):
        self.metadata['tokens']['6'], self.metadata['tokens']['8'] = 3, 2
        (self.root/'manifest.json').write_text(json.dumps(self.metadata))
        with self.assertRaises(AssetError):
            validate_asset_metadata(self.root)

    def test_extra_zip_member_and_duplicate_metadata_refused(self):
        self.arrays['unexpected'] = np.array([1], np.float32)
        self.write()
        with self.assertRaises(AssetError):
            load_assets(self.root)
        (self.root/'manifest.json').write_text('{"format_version":1,"format_version":1}')
        with self.assertRaises(AssetError):
            validate_asset_metadata(self.root)

    def test_metadata_import_does_not_initialize_numeric_runtime(self):
        completed = subprocess.run(
            [sys.executable, '-c', 'import sys; import checksi.dp5q.assets; assert "numpy" not in sys.modules; assert "rdkit" not in sys.modules'],
            capture_output=True, text=True, timeout=10, check=False,
        )
        self.assertEqual(completed.returncode,0,completed.stderr)

    def test_nonfinite_json_and_boolean_format_refused(self):
        (self.root/'manifest.json').write_text('{"unexpected":NaN}')
        with self.assertRaises(AssetError):
            validate_asset_metadata(self.root)
        self.metadata['format_version'] = True
        self.write()
        with self.assertRaises(AssetError):
            validate_asset_metadata(self.root)

    def test_missing_or_unknown_method_ids_refused_before_preflight(self):
        for key in ('preprocessing_id','score_id','geometry_policy'):
            valid = self.metadata[key]
            for value in (None,'','unknown',[]):
                with self.subTest(key=key,value=value):
                    self.metadata[key] = value
                    self.write()
                    with self.assertRaises(AssetError):
                        validate_asset_metadata(self.root)
            del self.metadata[key]
            self.write()
            with self.assertRaises(AssetError):
                validate_asset_metadata(self.root)
            self.metadata[key] = valid


class FrozenPercentileTests(unittest.TestCase):
    def test_actual_numpy_linspace_levels_are_accepted_exactly(self):
        case = AssetTests()
        case.setUp()
        try:
            case.metadata['quantile_levels'] = np.linspace(.01,.99,99).tolist()
            case.write()
            validate_asset_metadata(case.root)
        finally:
            case.doCleanups()


if __name__ == '__main__':
    unittest.main()
