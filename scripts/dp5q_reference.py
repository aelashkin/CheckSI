"""Opt-in frozen TensorFlow oracle; never imported by runtime/default tests.

Only run against the byte-pinned immutable DP5 snapshot. Integrity checks run
before source imports or legacy deserialization; they do not make pickle safe.
All outputs are JSON, exact SDF bytes and numeric NPZ, never a new pickle.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import importlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import pickle
import platform
import stat
import sys
from types import ModuleType, SimpleNamespace

for _key in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS',
             'TF_NUM_INTRAOP_THREADS', 'TF_NUM_INTEROP_THREADS'):
    os.environ[_key] = '1'
os.environ['CUDA_VISIBLE_DEVICES'] = '-1'
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2'
sys.dont_write_bytecode = True

SOURCE_REVISION = '8f8e22364654bbce71dcb6abb414eee793b00d92'
ASSET_NAMES = ('NMRdb-CASCADEset_Exp_mean_model_atom_features256.hdf5',
               'NMRdb_CASCADE_99quantiles.zip', 'CASCADE_quantile_extended.keras',
               'mean_model_preprocessor.p')
MAX_SOURCE_FILE_BYTES = 64 * 1024 * 1024


def _regular_file(path: Path, maximum: int, collect: bool = False):
    """Read a bounded stable regular file without following a leaf symlink."""
    if path.is_symlink():
        raise ValueError('Symlink is not a pinned regular reference file.')
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        with os.fdopen(descriptor, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
                raise ValueError('Reference file exceeds its pinned regular-file bound.')
            digest, blocks, count = sha256(), [], 0
            for block in iter(lambda: stream.read(min(1024 * 1024, maximum - count + 1)), b''):
                count += len(block)
                if count > maximum:
                    raise ValueError('Reference file exceeds its pinned byte bound.')
                digest.update(block)
                if collect:
                    blocks.append(block)
            after = os.fstat(stream.fileno())
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                raise ValueError('Pinned reference file changed during its read.')
    except OSError as exc:
        raise ValueError('Missing or unreadable pinned reference file: ' + str(path)) from exc
    return digest.hexdigest(), count, b''.join(blocks)


def _source_pin() -> dict:
    """Load code-owned source pins and cross-check original model provenance."""
    root = Path(__file__).resolve().parents[1]
    _, _, data = _regular_file(root / 'scripts/dp5q-reference/source-lock.json', 128 * 1024, True)
    pin = json.loads(data)
    manifest = json.loads((root / 'src/checksi/dp5q/data/manifest.json').read_text())
    if (pin.get('schema_version') != 1 or pin.get('source_revision') != SOURCE_REVISION or
            manifest['provenance']['source_revision'] != SOURCE_REVISION or
            not 1 <= len(pin.get('files', {})) <= 256):
        raise ValueError('Invalid pinned reference source identity.')
    for name, row in pin['files'].items():
        if (name != 'license.md' and not name.startswith('dp5/')) or '..' in Path(name).parts:
            raise ValueError('Invalid pinned reference source path.')
        if (type(row['bytes']) is not int or not 0 <= row['bytes'] <= MAX_SOURCE_FILE_BYTES or
                len(row['sha256']) != 64 or any(value not in '0123456789abcdef' for value in row['sha256'])):
            raise ValueError('Invalid pinned reference file digest or size.')
    if sum(row['bytes'] for row in pin['files'].values()) > 256 * 1024 * 1024:
        raise ValueError('Pinned reference source exceeds its aggregate bound.')
    for name, digest in manifest['provenance']['original_asset_hashes'].items():
        if pin['files'].get(name, {}).get('sha256') != digest:
            raise ValueError('Pinned original assets disagree with shipped provenance.')
    return pin


def digest_file(path: Path) -> str:
    """Hash without reading a large frozen model into another full buffer."""
    return _regular_file(path, MAX_SOURCE_FILE_BYTES)[0]


def source_hashes(source: Path) -> dict[str, str]:
    """Fingerprint the complete pinned subtree, refusing extra executable/cache files."""
    source = Path(source)
    pin = _source_pin()
    if (source / 'dp5').is_symlink():
        raise ValueError('Symlink is not a pinned source directory.')
    actual = {'license.md'}
    for path in (source / 'dp5').rglob('*'):
        if path.is_dir() and not path.is_symlink():
            continue
        actual.add(path.relative_to(source).as_posix())
        if len(actual) > 256:
            raise ValueError('Reference inventory exceeds the pinned source bound.')
    if actual != set(pin['files']):
        raise ValueError('Reference inventory differs from the pinned source.')
    hashes = {}
    for name, row in sorted(pin['files'].items()):
        digest, size, _ = _regular_file(source / name, row['bytes'])
        if size != row['bytes']:
            raise ValueError('Reference size differs from the pinned source: ' + name)
        hashes[name] = digest
    return hashes


def verify_reference_source(source: Path) -> dict[str, str]:
    """Authenticate every pinned source/asset byte before any upstream code executes."""
    hashes = source_hashes(source)
    expected = {name: row['sha256'] for name, row in _source_pin()['files'].items()}
    if hashes != expected:
        raise ValueError('Reference bytes differ from the pinned source or assets.')
    return hashes


def load_verified_preprocessor(source: Path):
    """Verify the exact bytes supplied to the unavoidable development-only pickle load."""
    verify_reference_source(source)
    _bind_reference_package(source)
    name = 'dp5/neural_net/mean_model_preprocessor.p'
    row = _source_pin()['files'][name]
    digest, size, data = _regular_file(Path(source) / name, row['bytes'], True)
    if size != row['bytes'] or digest != row['sha256']:
        raise ValueError('Preprocessor differs from the pinned asset.')
    return pickle.loads(data)


def _bind_reference_package(source: Path) -> None:
    """Expose only the verified dp5 package, never the checkout root as an import path."""
    location = str(Path(source).resolve() / 'dp5')
    previous = sys.modules.get('dp5')
    if previous is not None and getattr(previous, '_dp5q_reference_root', None) != location:
        raise ValueError('An unverified dp5 package is already imported.')
    if previous is None:
        spec = importlib.util.spec_from_file_location('dp5', Path(location) / '__init__.py',
                                                     submodule_search_locations=[location])
        package = importlib.util.module_from_spec(spec)
        package._dp5q_reference_root = location
        row = _source_pin()['files']['dp5/__init__.py']
        digest, size, data = _regular_file(Path(location) / '__init__.py', row['bytes'], True)
        if size != row['bytes'] or digest != row['sha256']:
            raise ValueError('Package initializer differs from the pinned source.')
        sys.modules['dp5'] = package
        # Execute verified source bytes without reading or writing a bytecode cache.
        exec(compile(data, str(Path(location) / '__init__.py'), 'exec'), package.__dict__)


def load_reference(source: Path):
    """Import frozen source in place, bypassing only invalid packaging metadata."""
    verify_reference_source(source)
    _bind_reference_package(source)
    import numpy as np
    import tensorflow as tf
    tf.config.set_visible_devices([], 'GPU')
    tf.config.threading.set_intra_op_parallelism_threads(1)
    tf.config.threading.set_inter_op_parallelism_threads(1)
    from dp5.neural_net import CNN_model as cnn
    mean = cnn.load_NMR_prediction_model()
    quantile = cnn.CASCADE_Quantile.load(str(source/'dp5/neural_net/NMRdb_CASCADE_99quantiles.zip'))
    preprocessor = load_verified_preprocessor(source)
    preprocessor.atom_tokenizer.train = False
    preprocessor.bond_tokenizer.train = False
    if preprocessor.cutoff != 5 or preprocessor.n_neighbors != 100:
        raise ValueError('Unexpected frozen graph preprocessing settings.')
    if not np.array_equal(quantile.quantiles, np.linspace(.01,.99,99)):
        raise ValueError('Unexpected frozen quantile levels.')
    return cnn, mean, quantile, preprocessor


def semantic_weights(mean, quantile):
    """Read trained tensors by actual loaded Keras semantic layer names."""
    import numpy as np
    rows, shared, unique = [], 0, {}
    layers = ['atom_embedding', 'atomwise_shift'] + ['dense' if i == 0 else f'dense_{i}' for i in range(21)] + ['loc_1','loc_2','loc_3','loc_reduce']
    for layer in layers:
        left = mean.get_layer(layer)
        right = quantile.model.get_layer(layer)
        a, b = left.get_weights(), right.get_weights()
        if len(a) != len(b):
            raise ValueError(f'Weight count differs: {layer}')
        for index, (x,y) in enumerate(zip(a,b)):
            kind = 'embedding' if layer in ('atom_embedding','atomwise_shift') else ('kernel' if index == 0 else 'bias')
            stem = layer if kind == 'embedding' else layer+'.'+kind
            same = x.shape == y.shape and x.dtype == y.dtype and np.array_equal(x,y)
            expected_shared = not layer.startswith('loc_')
            if same != expected_shared:
                raise ValueError(f'Unexpected semantic tensor sharing: {stem}')
            for array in (x,y):
                if array.dtype != np.float32 or not np.isfinite(array).all():
                    raise ValueError('Invalid frozen numeric tensor.')
            row = {'layer':layer, 'kind':kind, 'mean_variable':left.weights[index].name,
                   'quantile_variable':right.weights[index].name,
                   'mean_shape':list(x.shape), 'quantile_shape':list(y.shape),
                   'mean_sha256':sha256(x.tobytes()).hexdigest(),
                   'quantile_sha256':sha256(y.tobytes()).hexdigest(), 'shared':same}
            rows.append(row)
            if same:
                unique['shared.'+stem] = x
                shared += 1
            else:
                unique['mean.'+stem] = x
                unique['quantile.'+stem] = y
    if len(rows) != 49 or shared != 41 or len(unique) != 57 or sum(a.nbytes for a in unique.values()) != 11_625_924:
        raise ValueError('Unexpected architecture or unique trained parameter bytes.')
    return unique, rows


def capture_layers(cnn, model, inputs):
    """Evaluate actual frozen layer activations, including every residual."""
    names = {'embedding':'atom_embedding', 'representations':'reduce_atom_to_pro', 'atomic_shift':'reduce_atom_to_pro_1'}
    for block in range(3):
        index = 7*block
        names[f'block_{block+1}.projected_atom'] = 'dense' if index == 0 else f'dense_{index}'
        names[f'block_{block+1}.bond_state'] = 'add' if block == 0 else f'add_{2*block}'
        names[f'block_{block+1}.atom_state'] = f'add_{2*block+1}'
        names[f'block_{block+1}.messages'] = 'reduce_bond_to_atom' if block == 0 else f'reduce_bond_to_atom_{block}'
    probe = cnn.GraphModel(inputs=model.inputs, outputs=[model.get_layer(name).output for name in names.values()]+[model.output])
    values = probe(inputs, training=False)
    return {key: value.numpy() for key,value in zip([*names, 'output'], values)}


def load_manual_modules(source: Path):
    """Scope the oracle to frozen parsing/assignment, excluding raw-FID imports."""
    verify_reference_source(source)
    _bind_reference_package(source)
    package = ModuleType('dp5.nmr_processing')
    package.__path__ = [str(source/'dp5/nmr_processing')]
    sys.modules['dp5.nmr_processing'] = package
    description = importlib.import_module('dp5.nmr_processing.description_files')
    assignment = importlib.import_module('dp5.nmr_processing.manual_assignment')
    analysis = importlib.import_module('dp5.analysis.dp5')
    return description, assignment, analysis


def capture_fixture(source, sdf, nmr, output, reference):
    """Archive the exact final SDF plus frozen graph/network/statistical values."""
    import numpy as np
    import pandas as pd
    from rdkit import Chem
    from scipy.stats import norm
    cnn, mean, quantile, preprocessor = reference
    output.mkdir(parents=True, exist_ok=False)
    (output/'initial.sdf').write_bytes(sdf.read_bytes())
    # The frozen read_sdf path AddHs, then prepare_inputs SDWriter, then
    # Molecule.__init__ reparses removeHs=False. No geometry generation here.
    mol = Chem.AddHs(Chem.MolFromMolFile(str(sdf), removeHs=False, sanitize=True))
    with Chem.SDWriter(str(output/'final.sdf')) as writer:
        writer.write(mol)
    mol = Chem.MolFromMolFile(str(output/'final.sdf'), removeHs=False, sanitize=True)
    carbon_indices = np.array([a.GetIdx() for a in mol.GetAtoms() if a.GetAtomicNum() == 6], dtype=np.int64)
    features = preprocessor.construct_feature_matrices((mol, carbon_indices))
    inputs = cnn.RBFSequence([features], batch_size=1, shuffle=False)[0]
    left = capture_layers(cnn, mean, inputs)
    right = capture_layers(cnn, quantile.model, inputs)
    comparisons = {}
    for key in left:
        if key == 'output':
            continue
        exact = np.array_equal(left[key],right[key])
        comparisons[key] = {'exact':exact, 'max_absolute_error':float(np.max(np.abs(left[key]-right[key])))}
        if not exact:
            raise ValueError(f'Shared reference activation differs: {key}')
    arrays = {'atomic_numbers':np.array([a.GetAtomicNum() for a in mol.GetAtoms()], dtype=np.int64),
              'positions':mol.GetConformer().GetPositions(), 'atom_indices':carbon_indices,
              **{'graph.'+key:np.asarray(value) for key,value in features.items()},
              'rbf.float64':cnn.rbf_expansion(features['distance']),
              'rbf.float32':inputs['distance_rbf'].astype(np.float32),
              **{'mean.'+key:value for key,value in left.items()},
              **{'quantile.'+key:value for key,value in right.items()}}
    metadata = {'sdf_source_name':sdf.name, 'initial_sdf_sha256':digest_file(output/'initial.sdf'),
                'final_sdf_sha256':digest_file(output/'final.sdf'), 'shared_activations':comparisons,
                'atom_count':mol.GetNumAtoms(), 'edge_count':len(features['distance'])}
    if nmr is not None:
        description_module, assignment_module, analysis = load_manual_modules(source)
        # Archive source bytes and a separate 4-decimal adapter transcript.
        (output/'source.nmr').write_bytes(nmr.read_bytes())
        original = description_module.parse_manual_description(nmr)
        def rendered(value):
            text = f'{value:.4f}'.rstrip('0').rstrip('.')
            return text if '.' in text else text+'.0'
        contents = ','.join(rendered(s.value)+'('+(','.join(s.labels) or 'any')+')' for s in original.carbon_shifts)+'\n\n0.0(any)\n\n'
        contents += ''.join(','.join(group)+'\n' for group in original.equivalence_groups)
        if original.omitted_labels:
            contents += 'OMIT '+','.join(original.omitted_labels)+'\n'
        (output/'emitted.nmr').write_text(contents)
        description = description_module.parse_manual_description(output/'emitted.nmr')
        stub = SimpleNamespace(atoms=[a.GetSymbol() for a in mol.GetAtoms()],
                               C_labels=np.array([f'C{i+1}' for i in carbon_indices]),
                               conformer_C_pred=left['output'].reshape(1,-1), populations=np.array([1.]))
        arrays.update({'experimental.original':np.array([s.value for s in original.carbon_shifts]),
                       'experimental.parsed':np.array([s.value for s in description.carbon_shifts])})
        metadata['experimental_dtype'] = str(arrays['experimental.parsed'].dtype)
        try:
            assignment_module.assign_manual_carbon_strict(stub,description)
        except assignment_module.NMRAssignmentError as exc:
            metadata['assessment'] = {'status':'not_applicable','reason':str(exc)}
        else:
            group_quantiles = np.array([right['output'][positions].mean(axis=0) for positions in stub.C_group_carbon_positions])
            calculator = analysis.QuantileDP5ProbabilityCalculator.__new__(analysis.QuantileDP5ProbabilityCalculator)
            calculator.model = quantile
            parameters = calculator.generate_distributions(pd.Series([group_quantiles]))
            mu, sigma = parameters.iloc[0]['mu'], parameters.iloc[0]['sigma']
            atom_probabilities = 1-np.abs(1-2*norm.cdf(stub.C_group_exp,mu,sigma))
            _, molecular = calculator.rescale_probabilities([atom_probabilities],None)
            arrays.update({'group.mean':stub.conformer_C_group_pred[0], 'group.quantiles':group_quantiles,
                           'assignment.experimental':stub.C_group_exp, 'fit.mu':mu, 'fit.sigma':sigma,
                           'legacy.atom_probability':atom_probabilities, 'legacy.score':molecular})
            metadata.update({'assignment':stub.C_assignment_coverage, 'legacy_score':float(molecular[0]),
                             'assessment':{'status':'assessed'}})
    np.savez_compressed(output/'capture.npz', **arrays)
    (output/'capture.json').write_text(json.dumps(metadata,indent=2,allow_nan=False)+'\n')
    return metadata, features, left['output'], right['output']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--verify-only',action='store_true')
    parser.add_argument('--sdf',type=Path,action='append')
    parser.add_argument('--nmr',type=Path,action='append')
    args = parser.parse_args()
    source = args.source.resolve()
    before = verify_reference_source(source)
    if args.verify_only:
        print(json.dumps({'status':'PASS pinned reference source and assets',
                          'source_revision':SOURCE_REVISION,'verified_files':len(before)}))
        return
    if args.output is None:
        parser.error('--output is required unless --verify-only is selected')
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (300, 300))
    args.output.mkdir(parents=True,exist_ok=False)
    os.environ['KERAS_HOME'] = str(args.output/'keras-cache')
    reference = load_reference(source)
    weights, rows = semantic_weights(reference[1],reference[2])
    np = importlib.import_module('numpy')
    np.savez_compressed(args.output/'semantic-weights.npz',**weights)
    tokens = reference[3].atom_tokenizer._data
    token_metadata = {str(key):value for key,value in tokens.items() if isinstance(key,int)}
    pairs = list(zip(args.sdf,args.nmr or [None]*len(args.sdf))) if args.sdf else [(source/f'reassignments/{case}/{case}a_.sdf',source/f'reassignments/{case}/{case}_NMR') for case in ('S1','S11')]
    if args.nmr and len(args.nmr)!=len(args.sdf):
        raise ValueError('Supply one NMR file for each explicit SDF.')
    fixtures, features, means, quantiles = [],[],[],[]
    for index,(sdf,nmr) in enumerate(pairs):
        result,graph,mean,quantile = capture_fixture(source,sdf,nmr,args.output/f'fixture-{index}',reference)
        fixtures.append(result)
        features.append(graph)
        means.append(mean)
        quantiles.append(quantile)
    cnn,mean,quantile,_ = reference
    selection = [i % len(features) for i in range(16)]
    batch = cnn.RBFSequence([features[i] for i in selection],batch_size=16,shuffle=False)[0]
    for model,expected in ((mean,means),(quantile.model,quantiles)):
        np.testing.assert_allclose(model(batch).numpy(),np.concatenate([expected[i] for i in selection]),atol=1e-3,rtol=0)
    after = source_hashes(source)
    if before != after:
        raise ValueError('Frozen source or assets changed during reference execution.')
    result = {'source_revision':SOURCE_REVISION,'source_hashes':before,'source_unchanged':True,
              'versions':{name:importlib.metadata.version(name) for name in ('tensorflow','keras','numpy','rdkit','scipy','h5py')},
              'platform':platform.platform(),'semantic_tensors':rows,'shared_count':41,'unique_count':57,
              'raw_unique_bytes':sum(array.nbytes for array in weights.values()), 'tokens':token_metadata,
              'quantile_levels':reference[2].quantiles.tolist(),'fixtures':fixtures,'batch_count':16,'batch_parity':'PASS atol=1e-3ppm rtol=0',
              'resources':{'max_rss_native_units':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                           'user_cpu_seconds':resource.getrusage(resource.RUSAGE_SELF).ru_utime,
                           'system_cpu_seconds':resource.getrusage(resource.RUSAGE_SELF).ru_stime}}
    (args.output/'receipt.json').write_text(json.dumps(result,indent=2,allow_nan=False)+'\n')
    print(json.dumps({'status':'PASS frozen reference','fixtures':len(fixtures),'shared_count':41,'raw_unique_bytes':result['raw_unique_bytes']}))


if __name__ == '__main__':
    main()
