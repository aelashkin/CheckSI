# Standard library imports
import re

# Third-party imports
from rdkit import Chem

# Project imports
from checksi.analysis.structure_analysis import StereoAssessment, assess_molecular_stereochemistry
from checksi.config_schema import AppConfig, OutputMode
from checksi.errors import ErrorCode
from checksi.errors.manager import emit_finding
from checksi.errors.findings import AnalysisNote, Finding, message, messages, FindingSink
from checksi.utils.error_handler import ErrorTracker


def _descriptor_shape_valid(tokens):
    kinds = {get_type(token) for token in tokens}
    return ((len(tokens) == 1 and kinds.issubset({4, 5, 6}))
            or (len(tokens) > 1 and len(kinds) == 1 and kinds.issubset({1, 2, 3})))


def _check_streo_desc(
    cfg: AppConfig,
    tracker: ErrorTracker | FindingSink,
    words,
    eq_c_ratio,
    ratio,
    atom_with_four_unique_substituents,
    smiles=None,
    *,
    stereo_assessment: StereoAssessment | None = None,
):
    """Check descriptor roles using structural stereo; legacy fractions are ignored."""
    if stereo_assessment is None and smiles:
        molecule = Chem.MolFromSmiles(smiles)
        if molecule is not None:
            stereo_assessment = assess_molecular_stereochemistry(molecule)
    status = stereo_assessment.status if stereo_assessment is not None else 'unassessed'
    complete = (stereo_assessment is not None and stereo_assessment.enumeration_complete
                and status != 'unassessed')
    centers = stereo_assessment.rs_center_count if complete else None
    context = {'meso_status': status, 'stereocenter_count': centers,
               'structure_reason': (stereo_assessment.reason if stereo_assessment is not None
                                    else 'structure_unavailable')}
    diagnostics = []

    def emit(code, variant):
        finding = emit_finding(cfg, tracker, code, context=context, variant=variant)
        if finding is not None:
            diagnostics.append(finding)

    descriptors = re.findall(r'\((?:\d*[a-zA-Z]*R\*?|\d*[a-zA-Z]*S\*?)(?:,\d*[a-zA-Z]*[RS]\*?)*\)|\((?:R|S|RS|SR|R\*|S\*)\)', words)
    if not descriptors:
        if centers is not None and centers > 1 and ratio == 0:
            emit(ErrorCode.STEREO_DESCRIPTOR_INVALID, 'missing_descriptors')
        if stereo_assessment is not None and status in ('meso_possible', 'no_meso_form', 'unassessed'):
            diagnostics.append(AnalysisNote(_structural_status_text(status, context['structure_reason'])))
        return diagnostics

    homogeneous, kind, type_diagnostics = _check_all_same_type(cfg, tracker, descriptors)
    if isinstance(type_diagnostics, Finding):
        diagnostics.append(type_diagnostics)
    elif isinstance(type_diagnostics, list):
        diagnostics.extend(type_diagnostics)
    # An invalid group cannot trigger its own omission finding, but must not
    # suppress a separate valid group's independent descriptor checks.
    valid_groups = [item for item in descriptors
                    if _descriptor_shape_valid(item.strip('()').replace(' ', '').split(','))]
    if not homogeneous or not valid_groups:
        return diagnostics
    context['descriptor_groups'] = valid_groups
    if cfg.modes.output_mode == OutputMode.DETAILED:
        diagnostics.append(AnalysisNote('Stereodescriptors are of the same type.'))

    if kind in (1, 4):
        variant = {'confirmed_meso': 'structural_meso', 'confirmed_non_meso': 'structural_non_meso',
                   'meso_possible': 'meso_possible', 'no_meso_form': 'no_meso_form',
                   'achiral_no_centres': 'no_stereocenters'}.get(status, 'structural_unassessed')
        emit(ErrorCode.STEREO_CHECK_REQUIRED, variant)
    elif kind in (3, 6):
        emit(ErrorCode.STEREO_CHECK_REQUIRED, 'relative_descriptor')
    elif kind in (2, 5):
        if kind == 2 and centers is not None and centers < 2:
            emit(ErrorCode.STEREO_DESCRIPTOR_INVALID, 'too_few_stereocenters')
        elif kind == 5 and centers == 1:
            emit(ErrorCode.STEREO_DESCRIPTOR_INVALID, 'can_be_omitted')
        elif kind == 2 and status == 'confirmed_meso':
            emit(ErrorCode.STEREO_DESCRIPTOR_INVALID, 'structural_meso_descriptor')
        if status in ('meso_possible', 'no_meso_form', 'unassessed'):
            diagnostics.append(AnalysisNote(_structural_status_text(status, context['structure_reason'])))
    if centers is not None and centers > 1 and ratio == 0 and kind not in (1, 2, 3):
        emit(ErrorCode.STEREO_DESCRIPTOR_INVALID, 'descriptor_type_incorrect')
    return diagnostics


def _structural_status_text(status, reason):
    if status == 'meso_possible':
        return 'A meso form exists among the supported stereoisomers; the sample configuration is unspecified.'
    if status == 'no_meso_form':
        return 'No meso form exists among the supported stereoisomers; the sample configuration is unspecified.'
    return f'Structural meso assessment is unassessed ({reason or "incomplete assessment"}); verify manually.'


def get_type(token):
    '''Determine the type of part of the stereodescription – one of 6. The order of checking is important. '''

    # Type 2: (1RS,2SR)
    if re.fullmatch(r'\d+[a-zA-Z]?(RS|SR|DL|LD)', token):
        return 2
    # Type 1:  (1R,2S)
    elif re.fullmatch(r'\d+[a-zA-Z]?[RSDL]', token):
        return 1
    # Type 3: (1R*,2S*)
    elif re.fullmatch(r'\d+[a-zA-Z]?[RSDL]\*', token):
        return 3
    # Type 5: RS, SR, DL, LD = (RS)
    elif re.fullmatch(r'(RS|SR|DL|LD)', token):
        return 5
    # Type 4:  R, S, D, L = (R)
    elif re.fullmatch(r'[RSDL]', token):
        return 4
    # Type 6: R*, S*, D*, L* = (S*) 
    elif re.fullmatch(r'[RSDL]\*', token):
        return 6
    return None

def _check_all_same_type(cfg: AppConfig, tracker: ErrorTracker | FindingSink, input_list):
    """Typed implementation behind `check_all_same_type`."""
    stereodescriptor_type_warnings = []
    overall_types = set()
    for item in input_list:
        if item is None:
            continue
        
        # Remove the parentheses from the stereodescription and remove the spaces
        content = item.strip('()').replace(' ', '')

        # Split into parts by comma
        tokens = content.split(',')

        # Determine the type for each part
        token_types = set(get_type(t) for t in tokens)
        
        # If the type could not be determined, raise an error
        if None in token_types:
            warning = emit_finding(cfg, tracker, ErrorCode.STEREO_DESCRIPTOR_INVALID, variant="unrecognized")
            if stereodescriptor_type_warnings:
                if warning:
                    stereodescriptor_type_warnings.append(warning)
                return False, None, stereodescriptor_type_warnings
            return False, None, warning or ""

        # Create a list of unique types for checking against a single type of stereodescriptor
        overall_types.update(token_types)
        # Check: one element = only type 4-6
        # Multiple elements = only type 1-3
        # Otherwise, raise an error
        if not _descriptor_shape_valid(tokens):
            warning = emit_finding(cfg, tracker, ErrorCode.STEREO_DESCRIPTOR_INVALID,
                                   context={"descriptor_group": item}, variant="invalid_type")
            if warning:
                stereodescriptor_type_warnings.append(warning)

    # If all parts are of the same type
    if len(overall_types) == 1:
        return True, overall_types.pop(), stereodescriptor_type_warnings or None
    else:
        # Elements of different types
        warning = emit_finding(cfg, tracker, ErrorCode.STEREO_DESCRIPTOR_INCONSISTENT, variant="mixed_types")
        if warning:
            stereodescriptor_type_warnings.append(warning)
        
    return False, None, stereodescriptor_type_warnings


def check_streo_desc(
    cfg: AppConfig,
    tracker: ErrorTracker,
    words,
    eq_c_ratio,
    ratio,
    atom_with_four_unique_substituents,
    smiles=None,
    *,
    stereo_assessment: StereoAssessment | None = None,
):
    """Check stereochemical names using structural status; retain the legacy call shape."""
    return messages(_check_streo_desc(cfg, tracker, words, eq_c_ratio, ratio,
        atom_with_four_unique_substituents, smiles, stereo_assessment=stereo_assessment))


def check_all_same_type(cfg: AppConfig, tracker: ErrorTracker, input_list):
    """Return token-type homogeneity while retaining every emitted diagnostic."""
    homogeneous, kind, diagnostics = _check_all_same_type(cfg, tracker, input_list)
    if isinstance(diagnostics, Finding):
        diagnostics = message(diagnostics)
    elif isinstance(diagnostics, list):
        diagnostics = messages(diagnostics)
    return homogeneous, kind, diagnostics
