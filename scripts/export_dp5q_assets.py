"""Export fixed numeric assets only after actual semantic/activation proof."""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path

import numpy as np

from checksi.dp5q.assets import MODEL_ID, PREPROCESSING_ID, SCORE_ID, GEOMETRY_POLICY, RAW_WEIGHT_BYTES, tensor_shapes, load_assets


REFERENCE_DIR = Path(__file__).resolve().parent / 'dp5q-reference'
TENSOR_MAP_SHA256 = '76d81ae00ba3cd91f1243fd8df616a0967e1dc41eda8d84c9462ea09c91177a4'
NOTICE_PATH = Path(__file__).resolve().parents[1] / 'src/checksi/dp5q/data/NOTICE.txt'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference', type=Path, required=True, help='Completed oracle output directory')
    parser.add_argument('--mapping', type=Path, default=REFERENCE_DIR / 'tensor-map.json', help='Hash-pinned 49-row tensor identity map (defaults to the tracked map)')
    parser.add_argument('--source', type=Path, required=True, help='Inspected immutable DP5 snapshot')
    parser.add_argument('--output', type=Path, required=True, help='New output directory; never overwritten')
    args = parser.parse_args()
    mapping_bytes = args.mapping.read_bytes()
    if sha256(mapping_bytes).hexdigest() != TENSOR_MAP_SHA256:
        raise ValueError('The frozen tensor map hash does not match the pinned identity map.')
    source_lock = json.loads((REFERENCE_DIR / 'source-lock.json').read_bytes())
    source_license = (args.source / 'license.md').read_bytes()
    license_pin = source_lock['files']['license.md']
    if len(source_license) != license_pin['bytes'] or sha256(source_license).hexdigest() != license_pin['sha256']:
        raise ValueError('The source license differs from the frozen source lock.')
    receipt = json.loads((args.reference/'receipt.json').read_bytes())
    if receipt.get('source_revision') != '8f8e22364654bbce71dcb6abb414eee793b00d92' or receipt.get('source_unchanged') is not True:
        raise ValueError('A completed frozen-source reference receipt is required.')
    if receipt.get('shared_count') != 41 or receipt.get('unique_count') != 57 or receipt.get('raw_unique_bytes') != RAW_WEIGHT_BYTES:
        raise ValueError('The semantic tensor-sharing gate has not passed.')
    if len(receipt.get('fixtures', [])) < 2 or not all(fixture.get('shared_activations') and all(stage['exact'] for stage in fixture['shared_activations'].values()) for fixture in receipt['fixtures']):
        raise ValueError('Shared-encoder activation equality must be demonstrated on two geometries.')
    if receipt.get('batch_count') != 16 or receipt.get('batch_parity') != 'PASS atol=1e-3ppm rtol=0':
        raise ValueError('Reference batch comparison has not passed.')
    mapping = json.loads(mapping_bytes)
    mapped = {}
    for row in mapping['rows']:
        layer = row['semantic_layer']
        suffix = row['mean_key'].rsplit('/',1)[-1]
        kind = {'embeddings:0':'embedding','kernel:0':'kernel','bias:0':'bias'}[suffix]
        mapped[(layer,kind)] = row
    if len(mapped) != 49 or len(receipt['semantic_tensors']) != 49:
        raise ValueError('Unexpected static or semantic tensor map size.')
    expected_hashes = {}
    for row in receipt['semantic_tensors']:
        expected = mapped[(row['layer'],row['kind'])]
        for key in ('mean_sha256','quantile_sha256','mean_shape','quantile_shape'):
            if row[key] != expected[key]:
                raise ValueError('Actual semantic tensors disagree with the frozen identity map.')
        stem = row['layer'] if row['kind'] == 'embedding' else row['layer']+'.'+row['kind']
        if row['shared']:
            expected_hashes['shared.'+stem] = row['mean_sha256']
        else:
            for head in ('mean','quantile'):
                expected_hashes[head+'.'+stem] = row[head+'_sha256']
    shapes = tensor_shapes()
    with np.load(args.reference/'semantic-weights.npz',allow_pickle=False) as archive:
        if set(archive.files) != set(shapes):
            raise ValueError('Unexpected export tensor names.')
        weights = {name:archive[name] for name in shapes}
    for name, array in weights.items():
        if array.shape != shapes[name] or array.dtype != np.float32 or not np.isfinite(array).all() or sha256(array.tobytes()).hexdigest() != expected_hashes[name]:
            raise ValueError('Invalid exported numeric tensor.')
    notices = REFERENCE_DIR / 'notices'
    notice_sources = json.loads((notices/'sources.json').read_text())
    notice_bytes = {name: (notices / name).read_bytes() for name in notice_sources}
    for name, info in notice_sources.items():
        if len(notice_bytes[name]) != info['bytes'] or sha256(notice_bytes[name]).hexdigest() != info['sha256']:
            raise ValueError('A third-party notice differs from its fetched primary source.')
    notice_bytes['LICENSE.dp5.txt'] = source_license
    notice_sources['LICENSE.dp5.txt'] = {
        'url': source_lock['source_url'] + '/blob/' + source_lock['source_revision'] + '/license.md',
        'bytes': len(source_license), 'sha256': sha256(source_license).hexdigest(),
        'source_bytes': license_pin['bytes'], 'source_sha256': license_pin['sha256'],
        'transformation': 'Exact frozen upstream license bytes; no transformation.',
    }
    notice_bytes['NOTICE.txt'] = NOTICE_PATH.read_bytes()
    notice_sources['NOTICE.txt'] = {
        'path': 'src/checksi/dp5q/data/NOTICE.txt',
        'bytes': len(notice_bytes['NOTICE.txt']), 'sha256': sha256(notice_bytes['NOTICE.txt']).hexdigest(),
        'transformation': 'Authoritative CheckSI modification and third-party attribution notice; copied byte-for-byte.',
    }
    args.output.mkdir(parents=True,exist_ok=False)
    np.savez_compressed(args.output/'weights.npz',**weights)
    data = (args.output/'weights.npz').read_bytes()
    manifest = {
        'format_version':1,'model_id':MODEL_ID,'preprocessing_id':PREPROCESSING_ID,
        'score_id':SCORE_ID,'geometry_policy':GEOMETRY_POLICY,
        'architecture':'three-256-message-blocks-shared-encoder-two-heads',
        'raw_weight_bytes':RAW_WEIGHT_BYTES,
        'files':{'weights.npz':{'bytes':len(data),'sha256':sha256(data).hexdigest()}},
        'tensors':{name:{'shape':list(array.shape),'dtype':'float32','sha256':sha256(array.tobytes()).hexdigest()} for name,array in weights.items()},
        'tokens':receipt['tokens'],'quantile_levels':receipt['quantile_levels'],
        'preprocessing':{'cutoff_angstrom':5.,'max_neighbors':100,'rbf_width':256,'rbf_delta':.1,'rbf_distance_dtype':'float32','rbf_expansion_dtype':'float64','input_dtype':'float32'},
        'provenance':{'source_revision':receipt['source_revision'],'source_url':'https://github.com/aelashkin/DP5',
                      'original_asset_hashes':{path:digest for path,digest in receipt['source_hashes'].items() if path.startswith('dp5/neural_net/') and path.rsplit('/',1)[-1] in ('NMRdb-CASCADEset_Exp_mean_model_atom_features256.hdf5','NMRdb_CASCADE_99quantiles.zip','CASCADE_quantile_extended.keras','mean_model_preprocessor.p')},
                      'reference_receipt_sha256':sha256((args.reference/'receipt.json').read_bytes()).hexdigest(),
                      'tensor_map_sha256':TENSOR_MAP_SHA256,
                      'shared_tensor_count':41,'unique_tensor_count':57,'semantic_and_activation_gate':'PASS',
                      'source_license':'MIT, Copyright (c) 2022 Goodman Lab','modified_asset_notice':'NOTICE.txt',
                      'notices':notice_sources,
                      'public_derived_dataset':'https://github.com/aelashkin/DP5/tree/8f8e22364654bbce71dcb6abb414eee793b00d92/nmrdb-dataset',
                      'method_publication':'https://doi.org/10.1039/D5SC06988B',
                      'cascade_publication':'https://doi.org/10.1039/D1SC03343C',
                      'training_data_distributed':False,'training_data_provenance':'Public developmental corpus overlaps the pinned dataset: 94/370 candidate constitutions across provided train/valid/test splits (78 train, 0 valid, 50 test); all four BMRB depictions overlap that union. Stereo-aware exact identities and per-split/cohort metrics are recorded in tests/fixtures/dp5q/development-audit.json. These are not independent validation data or proof of membership in a particular training run.'},
    }
    (args.output/'manifest.json').write_bytes((json.dumps(manifest,indent=2,allow_nan=False)+'\n').encode('utf-8'))
    for name, data_bytes in notice_bytes.items():
        (args.output / name).write_bytes(data_bytes)
    load_assets(args.output)
    print(json.dumps({'status':'PASS safe asset export','arrays':57,'raw_bytes':RAW_WEIGHT_BYTES,'archive_bytes':len(data),'sha256':sha256(data).hexdigest()}))


if __name__ == '__main__':
    main()
