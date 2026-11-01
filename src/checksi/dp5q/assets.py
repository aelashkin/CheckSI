"""Bounded, immutable numeric assets; metadata preflight stays lightweight."""
from hashlib import sha256
from importlib import resources
import io
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any
import zipfile

FORMAT_VERSION = 1
MODEL_ID = 'dp5q-cascade-owned-v1'
PREPROCESSING_ID = 'dp5q-spatial-v1'
SCORE_ID = 'dp5q-logtail-v1'
GEOMETRY_POLICY = 'converged-single-v2'
TRAINED_TOKENS = MappingProxyType({'16':6, '1':4, '17':7, '6':2, '7':5, '8':3,
                                 '9':8, '15':9, '35':10, '14':11, '5':12})
RAW_WEIGHT_BYTES = 11_625_924
MAX_METADATA_BYTES = 128 * 1024
MAX_ARCHIVE_BYTES = 13 * 1024 * 1024


class AssetError(ValueError):
    """An absent, incompatible, corrupted or unsafe packaged model asset."""


def tensor_shapes() -> dict[str, tuple[int, ...]]:
    """The fixed architecture is code-owned, not interpreted from an archive."""
    shapes = {'shared.atom_embedding': (13, 256), 'shared.atomwise_shift': (13, 1)}
    for block in range(3):
        dimensions = [(256, 256), (768, 512), (512, 256)] + [(256, 256)] * 4
        for offset, shape in enumerate(dimensions):
            index = block * 7 + offset
            name = 'dense' if index == 0 else f'dense_{index}'
            shapes[f'shared.{name}.kernel'] = shape
            if offset:
                shapes[f'shared.{name}.bias'] = (shape[1],)
    for head, width in [('mean', 1), ('quantile', 99)]:
        for layer, shape in zip(('loc_1', 'loc_2', 'loc_3', 'loc_reduce'), ((256, 256), (256, 256), (256, 128), (128, width))):
            shapes[f'{head}.{layer}.kernel'] = shape
            shapes[f'{head}.{layer}.bias'] = (shape[1],)
    return shapes


def _root(asset_dir: str | Path | None):
    return Path(asset_dir) if asset_dir is not None else resources.files('checksi.dp5q').joinpath('data')


def _read_bounded(path, maximum: int) -> bytes:
    try:
        with path.open('rb') as stream:
            data = stream.read(maximum + 1)
    except (OSError, FileNotFoundError) as exc:
        raise AssetError(f'Model asset is unavailable: {path.name}.') from exc
    if len(data) > maximum:
        raise AssetError(f'Model asset exceeds its size limit: {path.name}.')
    return data


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AssetError('Duplicate key in model metadata.')
        result[key] = value
    return result


def asset_metadata(asset_dir: str | Path | None = None) -> dict[str, Any]:
    """Read bounded JSON without importing NumPy or reading model arrays."""
    try:
        def reject_constant(value):
            raise AssetError('Nonfinite constant in model metadata.')
        result = json.loads(_read_bounded(_root(asset_dir).joinpath('manifest.json'), MAX_METADATA_BYTES), object_pairs_hook=_unique_object, parse_constant=reject_constant)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AssetError('Invalid model metadata JSON.') from exc
    if not isinstance(result, dict):
        raise AssetError('Model metadata must be an object.')
    return result


def validate_asset_metadata(asset_dir: str | Path | None = None) -> dict[str, Any]:
    """Check the fixed schema and archive availability without loading weights."""
    metadata = asset_metadata(asset_dir)
    expected = tensor_shapes()
    if type(metadata.get('format_version')) is not int or metadata.get('format_version') != FORMAT_VERSION or metadata.get('model_id') != MODEL_ID:
        raise AssetError('Unsupported model asset format or identity.')
    for key, expected_id in (('preprocessing_id', PREPROCESSING_ID), ('score_id', SCORE_ID), ('geometry_policy', GEOMETRY_POLICY)):
        if metadata.get(key) != expected_id:
            raise AssetError(f'Unsupported or missing model metadata identity: {key}.')
    tensors = metadata.get('tensors')
    if not isinstance(tensors, dict) or set(tensors) != set(expected):
        raise AssetError('Model tensor names do not match the fixed architecture.')
    for name, shape in expected.items():
        entry = tensors[name]
        if not isinstance(entry, dict) or entry.get('shape') != list(shape) or entry.get('dtype') != 'float32':
            raise AssetError(f'Model tensor shape or dtype is invalid: {name}.')
        digest = entry.get('sha256')
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise AssetError(f'Model tensor hash is invalid: {name}.')
    levels = metadata.get('quantile_levels')
    # Preserve np.linspace(.01, .99, 99), including its last-bit values.
    expected_levels = [.01 + .01 * i for i in range(99)]
    expected_levels[-1] = .99
    if levels != expected_levels:
        raise AssetError('Quantile levels are incompatible.')
    tokens = metadata.get('tokens')
    if (not isinstance(tokens, dict) or tokens != TRAINED_TOKENS
            or any(type(value) is not int for value in tokens.values())):
        raise AssetError('Model token vocabulary differs from the fixed trained preprocessing identity.')
    files = metadata.get('files')
    if not isinstance(files, dict) or set(files) != {'weights.npz'}:
        raise AssetError('Model archive manifest is invalid.')
    info = files['weights.npz']
    size = info.get('bytes') if isinstance(info, dict) else None
    digest = info.get('sha256') if isinstance(info, dict) else None
    if type(size) is not int or not 0 < size <= MAX_ARCHIVE_BYTES or not isinstance(digest, str) or len(digest) != 64:
        raise AssetError('Model archive size/hash metadata is invalid.')
    try:
        with _root(asset_dir).joinpath('weights.npz').open('rb') as stream:
            stream.seek(0, 2)
            if stream.tell() != size:
                raise AssetError('Model archive size differs from the manifest.')
    except OSError as exc:
        raise AssetError('Model weights are unavailable.') from exc
    return metadata


def load_assets(asset_dir: str | Path | None = None):
    """Validate bytes, zip members and numeric tensors once; forbid pickle."""
    import numpy as np

    metadata = validate_asset_metadata(asset_dir)
    data = _read_bounded(_root(asset_dir).joinpath('weights.npz'), MAX_ARCHIVE_BYTES)
    if sha256(data).hexdigest() != metadata['files']['weights.npz']['sha256']:
        raise AssetError('Model archive hash differs from the manifest.')
    shapes = tensor_shapes()
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            if len(members) != len(shapes) or {m.filename for m in members} != {name + '.npy' for name in shapes}:
                raise AssetError('Unexpected or duplicate model archive members.')
            for member in members:
                shape = shapes[member.filename[:-4]]
                numeric_bytes = 4
                for dimension in shape:
                    numeric_bytes *= dimension
                if member.flag_bits & 1 or not numeric_bytes <= member.file_size <= numeric_bytes + 1024:
                    raise AssetError('Unsafe model archive member size or encryption.')
                # Validate the NPY header before np.load can allocate from it.
                with archive.open(member) as stream:
                    version = np.lib.format.read_magic(stream)
                    if version == (1, 0):
                        header_shape, fortran, dtype = np.lib.format.read_array_header_1_0(stream, max_header_size=1024)
                    elif version == (2, 0):
                        header_shape, fortran, dtype = np.lib.format.read_array_header_2_0(stream, max_header_size=1024)
                    else:
                        raise AssetError('Unsupported numeric model header version.')
                    if header_shape != shape or fortran or dtype != np.dtype('<f4') or stream.tell() + numeric_bytes != member.file_size:
                        raise AssetError('Invalid numeric model header shape, dtype or size.')
        arrays = {}
        with np.load(io.BytesIO(data), allow_pickle=False, max_header_size=1024) as archive:
            for name, shape in shapes.items():
                array = archive[name]
                if array.dtype != np.dtype('<f4') or array.shape != shape or not np.isfinite(array).all():
                    raise AssetError(f'Invalid numeric model tensor: {name}.')
                if sha256(array.tobytes(order='C')).hexdigest() != metadata['tensors'][name]['sha256']:
                    raise AssetError(f'Model tensor hash mismatch: {name}.')
                # bytes-backed views cannot be made writable by a consumer.
                arrays[name] = np.frombuffer(array.tobytes(order='C'), dtype='<f4').reshape(shape)
    except (OSError, ValueError, EOFError, OverflowError, zipfile.BadZipFile, KeyError) as exc:
        if isinstance(exc, AssetError):
            raise
        raise AssetError('Model archive is not a safe numeric asset.') from exc
    return MappingProxyType(arrays), metadata
