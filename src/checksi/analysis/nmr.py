# src/checksi/analysis/nmr.py

# Standard library imports
import math
import re
from collections import Counter
from dataclasses import dataclass, field, replace

# Third-party imports
import regex

# Project imports
from checksi.config_schema import AppConfig, OutputMode
from checksi.errors import ErrorCode
from checksi.errors.manager import emit_finding
from checksi.errors.findings import AnalysisNote, message, messages, Diagnostic, Finding, FindingSink
from checksi.utils.text_processing import (
    replace_dash,
    balance_brackets_in_text as balance_brackets_in_text,
)
from checksi.analysis.formula_utils import count_hydrogen, count_carbon
from checksi.analysis.structure_analysis import C13Alternative, StructureAnalysisResult
from checksi.dp5q.assignment import AssignmentError
from checksi.dp5q.observation_assignment import validate_observation_coverage
from checksi.dp5q.observation_policy import BORON_REASON, BoronDiagnosticPolicy, derive_observation_policy
from checksi.utils.error_handler import ErrorTracker

def _broad_multiplicity_pattern(token):
    """Return a regex pattern for compact/dotted/spaced broad multiplicities."""
    return rf'br\.?\s*{re.escape(token)}'


SIMPLE_MULTIPLICITIES = [
    's', 'm', 'mc', _broad_multiplicity_pattern('s'), _broad_multiplicity_pattern('m'), 'bs',
]
J_REQUIRED_MULTIPLICITIES = [
    'd', 't', 'q', 'hep', 'hept', 'p', 'pent', 'h',
    'dt', 'dd', 'td', 'dq', 'tt', 'tq', 'qt', 'qd','dhept', 'dp', 'tp',
    'ddd', 'ddt', 'ddq', 'dtd', 'dtt', 'dtq', 'dqd', 'dqt', 'dqq', 'ddp',
    'tdd', 'tdt', 'tdq', 'ttd', 'ttt', 'ttq', 'tqd', 'tqt', 'tqq', 'dddd',
    'qdd', 'qdt', 'qdq', 'qtd', 'qtt', 'qtq', 'qqd', 'qqt', 'qqq', 
    _broad_multiplicity_pattern('d'), _broad_multiplicity_pattern('t'),
    _broad_multiplicity_pattern('q'), 'AB-q',
]
MULTIPLICITIES = [*SIMPLE_MULTIPLICITIES, *J_REQUIRED_MULTIPLICITIES]
MULTIPLICITY_PATTERN = "|".join(MULTIPLICITIES)
J_PATTERN = r'J\s*=\s*\d+(?:\.\d+)?(?:\s*(?:,|and)\s*\d+(?:\.\d+)?)*(?:\s*Hz)?'
_CARBON_MULTIPLICITY_PATTERN = r'(?<![\d.])(\d+)C(?![A-Za-z0-9])'
# General NMR support is intentionally broader than the DP5q atom/group limits.
_MAX_CARBON_SIGNALS = 10_000


class _CarbonSignalLimitExceeded(ValueError):
    """The spectrum exceeds the supported carbon expansion budget."""


@dataclass(frozen=True, slots=True)
class CarbonObservation:
    """One shift; represented_atoms retains its name but counts overlapping signals."""

    shift: float | None
    represented_atoms: int | None
    raw_token: str
    start_offset: int
    end_offset: int


@dataclass(slots=True)
class DP5Preparation:
    """One call's selected evidence, eligibility and borrowed output buffers."""

    groups: list[list[str]] | None = field(default_factory=list)
    eligible: bool = False
    shifts: list[float] | None = None
    refusal_details: dict = field(default_factory=dict)
    observations: list[CarbonObservation] = field(default_factory=list)
    coincident_environments: tuple[tuple[int, ...], ...] = ()
    carbon_scope: dict = field(default_factory=dict)
    coverage_reason: str | None = None


@dataclass(frozen=True, slots=True)
class NumberCountResult:
    """Named count and display slots, independent of diagnostic emission order."""

    count: int
    order: Finding | None
    rounding: Finding | None
    duplicate: Finding | None
    precision: Finding | None
    hydrogen: list[Diagnostic]
    ratio: float


@dataclass(frozen=True, slots=True)
class SignalAnalysisResult:
    """Scientific signal checks and their separate carbon preparation evidence."""

    diagnostics: list[Diagnostic]
    ratio: float
    eq_c_ratio: float
    preparation: DP5Preparation


@dataclass(frozen=True, slots=True)
class CarbonSignalEvidence:
    """Parsed source observations and annotation errors, before scientific checking."""

    observations: list[CarbonObservation]
    annotation_errors: list[str]
    shifts: list[float]

    @property
    def minimum_atoms(self) -> int:
        """Count validated signals, treating unknown annotations as one signal."""
        return sum(item.represented_atoms if item.represented_atoms is not None else 1
                   for item in self.observations)


@dataclass(frozen=True, slots=True)
class CarbonCountResult:
    """Count comparison and selected partition before annotation eligibility."""

    diagnostics: list[Diagnostic]
    eq_c_ratio: float
    selected_count: int | None
    formula_count: int | None
    preparation: DP5Preparation


@dataclass(frozen=True, slots=True)
class NMRCheckResult:
    """Named analysis facts projected into the public eight-value result."""

    diagnostics: list[Diagnostic]
    frequency: int | float
    ratio: float
    section: str | None
    eq_c_ratio: float
    preparation: DP5Preparation


@dataclass(frozen=True, slots=True)
class NucleusSpec:
    """Canonical label/selector binding; legacy adapters may bind them independently."""

    label: str
    atom: int


# Analytical schedule; required-spectrum checks have a separate element order.
NUCLEUS_SPECS = (
    NucleusSpec('1H', 0), NucleusSpec('19F', 2), NucleusSpec('11B', 3),
    NucleusSpec('31P', 4), NucleusSpec('29Si', 5), NucleusSpec('2H', 6),
    NucleusSpec('15N', 7), NucleusSpec('13C', 1),
)
PROTON, FLUORINE, BORON, PHOSPHORUS, SILICON, DEUTERIUM, NITROGEN, CARBON = NUCLEUS_SPECS


@dataclass(frozen=True, slots=True)
class ParsedSection:
    """Selected source and normalized parse facts, without scientific decisions."""

    raw_text: str | None
    normalized_text: str | None = None
    fluorine_decoupled: int = 0
    ending_text: str | None = None
    signals: str | None = None
    outside_brackets: str | None = None
    solvent_frequency_text: str | None = None


@dataclass(frozen=True, slots=True)
class CheckContext:
    """One NMR call's configuration, source scope and scientific inputs."""

    cfg: AppConfig
    sink: ErrorTracker | FindingSink
    nucleus: NucleusSpec
    trees_count: int | None
    frequency_h: int | float | None
    atom_with_four_unique_substituents: int
    atom_with_two_identical_substituents: int
    formula_nmr: str | None
    e_formula: int
    onh_count: int
    c13_count_alternatives: set[int] | None
    carbon_groups: list[list[str]] | None
    c13_count_alternatives_w_groups: list | None
    stereogenic_signal_flag: bool
    preparation: DP5Preparation = field(default_factory=DP5Preparation)
    structure_partition_complete: bool = True
    structure_partition_reason: str | None = None
    coincident_environments: tuple[tuple[int, ...], ...] = ()
    contains_boron: bool = False
    configuration_status: str = "unassessed"
    policy_reason: str | None = None
    direct_boron_groups: tuple[tuple[str, ...], ...] = ()
    boron_diagnostic_policy: BoronDiagnosticPolicy | None = None
    carbon_signal_format_valid: bool = True
    structure_result: StructureAnalysisResult | None = None
    selected_isomer_smiles: str | None = None


def _descriptor_fields(text: str) -> list[str]:
    """Keep descriptor fields before the first assignment or atom locant."""
    fields = []
    for descriptor_field in text.split(','):
        descriptor_field = descriptor_field.strip()
        if re.match(r'^(?:[HC]-|Ar|CH|NH|OH|\d+[a-z]?(?:[-:]|$))', descriptor_field) or re.fullmatch(
                r'(?:O|N)?Me\d*|Ph|tBu|Boc|TMS|Ac|OAc|Bn|Ts|H[a-z]', descriptor_field):
            break
        fields.append(descriptor_field)
    return fields


def _has_multiplicity_token(text, tokens):
    """Classify the first whole descriptor field before any assignment tail."""
    for descriptor_field in _descriptor_fields(text):
        if re.fullmatch(r'\s*(?:' + MULTIPLICITY_PATTERN + r')\s*', descriptor_field):
            return any(re.fullmatch(r'\s*(?:' + token + r')\s*', descriptor_field) for token in tokens)
    return False


def _has_unknown_multiplicity(text: str) -> bool:
    """Find an unknown letter field before a valid descriptor or assignment."""
    for descriptor_field in _descriptor_fields(text):
        if _has_multiplicity_token(descriptor_field, MULTIPLICITIES):
            return False
        if descriptor_field not in {'cis', 'trans'} and re.fullmatch(r'(?:br\.?\s*)?[a-zA-Z]{1,4}', descriptor_field):
            return True
    return False


def _multiplicity_at_start(text):
    """Return a multiplicity match only when it starts the bracket description."""
    return re.search(r'^\s*(?:' + MULTIPLICITY_PATTERN + r')\s*,', text)


def _check_presence_nmr_spectra(
    cfg: AppConfig,
    tracker: ErrorTracker | FindingSink,
    elements_in_formula,
    text,
    output,
):
    """Typed implementation behind `check_presence_nmr_spectra`."""
    
    # Dictionary of elements and their NMR patterns
    nmr_elements = {
        "F": {
            "patterns": [r"19F NMR", r"19F\{1H\} NMR", r"19F \{1H\} NMR"]
        },
        "P": {
            "patterns": [r"31P NMR", r"31P\{1H\} NMR", r"31P \{1H\} NMR"]
        },
        "B": {
            "patterns": [r"11B NMR", r"11B\{1H\} NMR", r"11B \{1H\} NMR"]
        },
        "Si": {
            "patterns": [r"29Si NMR", r"29Si\{1H\} NMR", r"29Si \{1H\} NMR"]
        },
        "15N": {
            "patterns": [r"15N NMR", r"15N\{1H\} NMR", r"15N \{1H\} NMR"]
        },
        "2H": {
            "patterns": [r"2H NMR"]
        },
        
    }
    
    try:
        # Check each NMR-relevant element
        for element, data in nmr_elements.items():
            patterns = data["patterns"]
            
            if element in elements_in_formula:
                # Check if there is at least one NMR pattern in the text
                if not any(re.search(pattern, text) for pattern in patterns):
                    warning = emit_finding(
                        cfg,
                        tracker,
                        ErrorCode.REQUIRED_NMR_MISSING,
                        context={"element": element},
                        variant="missing_nmr",
                    )
                    if warning:
                        output.append(warning)
        
                    
    except ValueError as e:
        warning = emit_finding(
            cfg,
            tracker,
            ErrorCode.REQUIRED_NMR_MISSING,
            context={"details": str(e)},
            variant="parse_error",
        )
        if warning:
            output.append(warning)
    except Exception as e:
        warning = emit_finding(
            cfg,
            tracker,
            ErrorCode.REQUIRED_NMR_MISSING,
            context={"details": str(e)},
            variant="unexpected_error",
        )
        if warning:
            output.append(warning)

    return output


def extract_and_compare(text, nmr, frequency_h):
    '''extract the frequency and check it'''
    war = None
    # look for the number before MHz, including decimal numbers
    war2 = None
    match = re.search(r'(\d+\.?\d*)\s*MHz', text, re.IGNORECASE)
    flag = 0
    frequency = 0

    if not match:
        war = "could not be read: no number before MHz was found; check it manually."
    else:
        frequency = float(match.group(1))  #
        #0H 1C 2F 3B 4P 5Si 6D 7N
        # The series for comparison is determined
        if nmr == 0:
            valid_frequencies = {100, 200, 300, 400, 500, 600, 700, 800, 900, 1000}
        elif nmr == 1:
            valid_frequencies = {25.1, 50.3, 75.4, 100.6, 125.7, 150.9, 176.0, 201.2, 226.3, 251.5}
        elif nmr == 2:
            valid_frequencies = {94.1, 188.2, 282.3, 376.4, 470.5, 564.6, 658.7, 752.8, 846.8, 940.9}
        elif nmr == 3:
            valid_frequencies = {32.2, 64.3, 96.5, 128.6, 160.8, 192.9, 225.1, 257.3, 289.4, 321.6}
        elif nmr == 4:
            valid_frequencies = {40.5, 81.0, 121.4, 161.9, 202.4, 242.9, 283.3, 323.8, 364.3, 404.8}
        elif nmr == 5:
            valid_frequencies = {19.9, 39.7, 59.6, 79.4, 99.3, 119.2, 139.0, 158.9, 178.7, 198.6}
        elif nmr == 6:
            valid_frequencies = {15.35, 30.7, 46.05, 61.4, 76.75, 92.1, 107.45, 122.8, 138.15, 153.5} 
        elif nmr == 7:
            valid_frequencies = {10.1, 20.3, 30.4, 40.6, 50.7, 60.8, 70.9, 81.1, 91.3, 101.4}
        
        if frequency:
            # Whether the frequency falls within ±2 of any value is verified
            for valid_freq in valid_frequencies:
                if abs(frequency - valid_freq) <= 1:
                    flag = 1
            if flag == 0:
                war = (f"({match.group(1)} MHz) is not among the common frequencies; "
                       "check it manually.")
            elif flag ==1:
                war = None
            if frequency_h and not war:
                if nmr == 0:
                    war2 = None
                if nmr == 1:
                    if abs(frequency - (frequency_h*0.25145)) > 1:
                        war2 = f"The frequency of 13C NMR does not match the frequency of 1H NMR. Should be {(frequency_h*0.25145):.1f}. Check manually."
                elif nmr == 2:
                    if abs(frequency - (frequency_h*0.94094)) > 1:
                        war2 = f"The frequency of 19F NMR does not match the frequency of 1H NMR. Should be {(frequency_h*0.94094):.1f}. Check manually."
                elif nmr == 3:
                    if abs(frequency - (frequency_h*0.3216)) > 1:
                        war2 = f"The frequency of 11B NMR does not match the frequency of 1H NMR. Should be {(frequency_h*0.3216):.1f}. Check manually."
                elif nmr == 4:
                    if abs(frequency - (frequency_h*0.4048)) > 1:
                        war2 = f"The frequency of 31P NMR does not match the frequency of 1H NMR. Should be {(frequency_h*0.4048):.1f}. Check manually."
                elif nmr == 5:
                    if abs(frequency - (frequency_h*0.1986)) > 1:
                        war2 = f"The frequency of 29Si NMR does not match the frequency of 1H NMR. Should be {(frequency_h*0.1986):.1f}. Check manually."
                elif nmr == 6:
                    if abs(frequency - (frequency_h*0.1535)) > 1:
                        war2 = f"The frequency of 2H NMR does not match the frequency of 1H NMR. Should be {(frequency_h*0.1535):.1f}. Check manually."
                elif nmr == 7:
                    if abs(frequency - (frequency_h*0.1014)) > 1:
                        war2 = f"The frequency of 15N NMR does not match the frequency of 1H NMR. Should be {(frequency_h*0.1014):.1f}. Check manually."



                
        else:
            war = (f"({match.group(1)} MHz) is not among the common frequencies; "
                   "check it manually.")
    return war, frequency, war2

def _find_peaks_in_texts(cfg: AppConfig, tracker: ErrorTracker | FindingSink, text1, textnmr, nuclei, nmr):
    """Typed implementation behind `find_peaks_in_texts`."""

    textnmr = regex.sub(r'\((?:[^()]|(?R))*\)', '', textnmr)
    errors_textnmr = []  # A list for storing errors.
    solvents_not_found = ""  # Message about solvents not found
    found_any_solvent = False  # A flag to check whether at least one solvent was found

    # The textnmr is parsed into individual numbers.
    found_peaks = set(re.findall(r"(?<!\d)-?\d+(?:\.\d+)?", textnmr))

    for solvent in cfg.solvents.entries:
        names = [solvent.name, *solvent.aliases]
        if not any(name.lower() in text1.lower() for name in names):
            continue

        found_any_solvent = True  # The solvent is found
        name = solvent.name

        if nmr == 0:  # The 1H signals are checked
            found_h_peak = False
            for h_peak in solvent.proton_shifts:
                if str(h_peak) in found_peaks:
                    warning = emit_finding(
                        cfg,
                        tracker,
                        ErrorCode.SOLVENT_PROTON_PEAK_FOUND,
                        context={"name": name, "peak": h_peak},
                    )
                    if warning:
                        errors_textnmr.append(warning)
                    found_h_peak = True

            if not found_h_peak and cfg.modes.output_mode == OutputMode.DETAILED:
                errors_textnmr.append(AnalysisNote(f"¹H peaks for {name} not found in textnmr."))

        elif nmr == 1:  # The 13C signals are checked
            found_c_peak = False
            for c_peak in solvent.carbon_shifts:
                if str(c_peak) in found_peaks:
                    warning = emit_finding(
                        cfg,
                        tracker,
                        ErrorCode.SOLVENT_CARBON_PEAK_FOUND,
                        context={"name": name, "peak": c_peak},
                    )
                    if warning:
                        errors_textnmr.append(warning)
                    found_c_peak = True

            if not found_c_peak and cfg.modes.output_mode == OutputMode.DETAILED:
                errors_textnmr.append(AnalysisNote(f"¹³C peaks for {name} not found in textnmr.")
)
    # If no solvent was found, a message is added
    if not found_any_solvent:
        
        solvents_not_found = (
            f"None of the solvents were found in description of {nuclei} NMR. "
            "If the solvent is specified but not found, check that all characters are typed in the English keyboard layout. "
            "Check the solvent list in settings.toml if less common solvents are used."
        )

    return errors_textnmr, solvents_not_found




def _check_H_difference(cfg: AppConfig, tracker: ErrorTracker | FindingSink, hnmr_sum, hrms_h_sum, onh_count):
    """Typed implementation behind `check_H_difference`."""

    ratio = 0
    if not onh_count:
        onh_count = 0
   
    if hnmr_sum != hrms_h_sum:
        # A match may occur if it is considered that some signals might not be visible
        if onh_count >0:
            # Signals may be partially invisible, so the range is checked
            if not((hnmr_sum + onh_count) >= hrms_h_sum >= hnmr_sum):
                war1 = emit_finding(
                    cfg,
                    tracker,
                    ErrorCode.H_COUNT_MISMATCH,
                    context={
                        "hnmr_sum": hnmr_sum,
                        "exchange_count": onh_count,
                        "hrms_sum": hrms_h_sum,
                    },
                    variant="with_exchange",
                )
                if (hnmr_sum + onh_count) > hrms_h_sum:
                    # If the number of protons in HNMR is greater than in HRMS or EA, it means it's a mixture, and the ratio can be calculated
                    ratio = ((hnmr_sum) / (hrms_h_sum)) - 1
                    if ratio < 1:
                        ratio = 1 / ratio
                    war2 = emit_finding(
                        cfg,
                        tracker,
                        ErrorCode.H_COUNT_RATIO_CHECK,
                        context={"ratio": ratio, "hnmr_sum": hnmr_sum, "hrms_sum": hrms_h_sum},
                        variant="with_context",
                    )
                else:
                    war2 = None
            else:
                x = hrms_h_sum - hnmr_sum
                war1 = emit_finding(
                    cfg,
                    tracker,
                    ErrorCode.H_COUNT_EXCHANGEABLE_OK,
                    context={"exchange_count": x, "hnmr_sum": hnmr_sum, "hrms_sum": hrms_h_sum},
                )
                return war1, None, None, ratio
        else:
            war1 = emit_finding(
                cfg,
                tracker,
                ErrorCode.H_COUNT_MISMATCH,
                context={"hnmr_sum": hnmr_sum, "hrms_sum": hrms_h_sum},
                variant="without_exchange",
            )
            if hnmr_sum > hrms_h_sum:
                # If the number of protons in HNMR is greater than in HRMS or EA, it means it's a mixture, and the ratio can be calculated
                ratio = (hnmr_sum / hrms_h_sum) - 1
                if ratio < 1:
                    ratio = 1 / ratio
                war2 = emit_finding(
                    cfg,
                    tracker,
                    ErrorCode.H_COUNT_RATIO_CHECK,
                    context={"ratio": ratio},
                    variant="simple",
                )
            else:
                war2 = None
    else :
        return None, None, AnalysisNote(f"Num H in 1H NMR ok (name, HRMS or EA). In 1H NMR {hnmr_sum}H, in formula from name/HRMS/EA {hrms_h_sum}H"), ratio
    return war1, war2, None, ratio



def _check_constant_hnmr(cfg: AppConfig, tracker: ErrorTracker | FindingSink, section):
    """Typed implementation behind `check_constant_hnmr`."""

    # Extract everything after δ
    section_after_delta = re.search(r'δ\s*\D*?(\d.*)', section)
    if section_after_delta:
        section = section_after_delta.group(1).strip()
    # Pattern for searching within parentheses
    pattern = r'\(([^()]*(?:\([^()]*\)[^()]*)*)\)'
    # Pattern for checking the presence of a number before H (for example, 1H, 2 H, 3.5H, etc.)
    h_pattern = r'\b(\d+(?:\.\d+)?)\s?[HН](?=[,;]|\b)'
    missing_j = []  # List for parentheses without 'J = number Hz'
    missing_h = []  # List for parentheses without a number before H
    wrong_order = []
    missing_multiplicity = []
    wrong_multiplicity = []

    # Search for all brackets
    matches = re.findall(pattern, section)
    for match in matches:
        # Check if there is a number before H
        m = re.search(h_pattern, match)
        if not m:
            missing_h.append(f"({match})")  # If not, add it to the list of errors
        else:
            value = float(m.group(1))
            if abs(value) < 1e-12:
                missing_h.append(f"({match})") 

        has_j = re.search(J_PATTERN, match)
        has_simple = _has_multiplicity_token(match, SIMPLE_MULTIPLICITIES)
        has_j_required = _has_multiplicity_token(match, J_REQUIRED_MULTIPLICITIES)
        has_any_multiplicity = has_simple or has_j_required
        invalid_field = _has_unknown_multiplicity(match)

        if has_j_required and not has_j:
            missing_j.append(f"({match})")
        elif (invalid_field and not has_any_multiplicity) or (has_j and has_simple):
            wrong_multiplicity.append(f"({match})")
        elif not has_any_multiplicity:
            missing_multiplicity.append(f"({match})")

        mult_start = _multiplicity_at_start(match)
        h_any = re.search(h_pattern, match)
        order_is_wrong = False

        if not mult_start:
            # With no multiplicity, J followed by the integral is already in
            # standard relative order. Missing multiplicity owns that finding.
            order_is_wrong = not (
                not has_any_multiplicity and not invalid_field and has_j and h_any
                and has_j.start() < h_any.start()
            )
        else:
            mult_pos = mult_start.start()
            j_pos = has_j.start() if has_j else None
            h_pos = h_any.start() if h_any else None

            if h_pos is not None and h_pos < mult_pos:
                order_is_wrong = True
            if j_pos is not None and j_pos < mult_pos:
                order_is_wrong = True
            if j_pos is not None and h_pos is not None and h_pos < j_pos:
                order_is_wrong = True
            if has_j_required:
                if j_pos is None or h_pos is None:
                    order_is_wrong = True
                elif not (mult_pos <= j_pos <= h_pos):
                    order_is_wrong = True
            elif has_simple and (h_pos is None or h_pos < mult_pos):
                order_is_wrong = True

        if order_is_wrong:
            wrong_order.append(f"({match})")

    # Generate warnings
    warnings = []
    if missing_j:
        warning = emit_finding(
            cfg,
            tracker,
            ErrorCode.HNMR_J_MISSING,
            context={"brackets": ", ".join(missing_j)},
        )
        if warning:
            warnings.append(warning)
    if missing_h:
        warning = emit_finding(
            cfg,
            tracker,
            ErrorCode.HNMR_H_COUNT_MISSING,
            context={"brackets": ", ".join(missing_h)},
        )
        if warning:
            warnings.append(warning)
    if wrong_order:
        warning = emit_finding(
            cfg,
            tracker,
            ErrorCode.HNMR_SIGNAL_DESC_INVALID,
            context={"brackets": ", ".join(wrong_order)},
        )
        if warning:
            warnings.append(warning)
    if missing_multiplicity:
        warning = emit_finding(
            cfg,
            tracker,
            ErrorCode.HNMR_MULTIPLICITY_MISSING,
            context={"brackets": ", ".join(missing_multiplicity)},
        )
        if warning:
            warnings.append(warning)
    if wrong_multiplicity:
        warning = emit_finding(
            cfg,
            tracker,
            ErrorCode.HNMR_WRONG_MULTIPLICITY,
            context={"brackets": ", ".join(wrong_multiplicity)},
        )
        if warning:
            warnings.append(warning)

    return warnings


def _check_constant_nmr(cfg: AppConfig, tracker: ErrorTracker | FindingSink, section, nuclei):
    """Typed implementation behind `check_constant_nmr`."""
    warnings = []
    # Extract everything after δ
    section_after_delta = re.search(r'δ\s*\D*?(\d.*)', section)  
    if section_after_delta:
        section = section_after_delta.group(1).strip()  
    missing_j = []
    wrong_order = []
    missing_multiplicity = []
    wrong_multiplicity = []
    # Pattern for searching within parentheses
    pattern = r'\((.*?)\)'

    # This flag is needed for check carbon splitting with fluorine
    nmr_const = 0

    # Search for all brackets
    matches = re.findall(pattern, section)
    for match in matches:
        has_j = re.search(J_PATTERN, match)
        mult_start = _multiplicity_at_start(match)
        has_any_multiplicity = _has_multiplicity_token(match, MULTIPLICITIES)
        has_simple = _has_multiplicity_token(match, SIMPLE_MULTIPLICITIES)
        has_j_required = _has_multiplicity_token(match, J_REQUIRED_MULTIPLICITIES)

        if has_j_required:
            if not has_j:
                missing_j.append(f"({match})")
            else:
                nmr_const = 1

        if has_j:
            nmr_const = 1
            if not has_any_multiplicity:
                missing_multiplicity.append(f"({match})")
            elif has_simple:
                wrong_multiplicity.append(f"({match})")

        if has_j and has_any_multiplicity:
            if not mult_start:
                wrong_order.append(f"({match})")
            else:
                after_mult = match[mult_start.end():].lstrip()
                if not re.match(J_PATTERN, after_mult):
                    wrong_order.append(f"({match})")

    if missing_j:
        warning = emit_finding(
            cfg,
            tracker,
            ErrorCode.NMR_J_MISSING,
            context={"brackets": ", ".join(missing_j), "nuclei": nuclei},
        )
        if warning:
            warnings.append(warning)
    if wrong_order:
        warning = emit_finding(
            cfg,
            tracker,
            ErrorCode.NMR_SIGNAL_DESC_INVALID,
            context={"brackets": ", ".join(wrong_order), "nuclei": nuclei},
        )
        if warning:
            warnings.append(warning)
    if missing_multiplicity:
        warning = emit_finding(
            cfg,
            tracker,
            ErrorCode.NMR_MULTIPLICITY_MISSING,
            context={"brackets": ", ".join(missing_multiplicity), "nuclei": nuclei},
        )
        if warning:
            warnings.append(warning)
    if wrong_multiplicity:
        warning = emit_finding(
            cfg,
            tracker,
            ErrorCode.NMR_WRONG_MULTIPLICITY,
            context={"brackets": ", ".join(wrong_multiplicity), "nuclei": nuclei},
        )
        if warning:
            warnings.append(warning)

    return warnings, nmr_const



def _bounded_carbon_count(digits: str) -> int:
    """Convert a carbon count only after checking its significant digit length."""
    digits = digits.lstrip('0') or '0'
    if len(digits) > len(str(_MAX_CARBON_SIGNALS)) or int(digits) > _MAX_CARBON_SIGNALS:
        raise _CarbonSignalLimitExceeded("13C signal expansion exceeds the supported limit")
    return int(digits)


def _parenthesis_spans(text: str) -> dict[int, int]:
    """Find outer description spans in one pass, including an unclosed tail."""
    spans = {}
    depth = 0
    start = 0
    for index, character in enumerate(text):
        if character == '(':
            if depth == 0:
                start = index
            depth += 1
        elif character == ')' and depth:
            depth -= 1
            if depth == 0:
                spans[start] = index + 1
    if depth:
        spans[start] = len(text)
    return spans


def _outside_parentheses(text: str, spans: dict[int, int] | None = None) -> str:
    """Mask descriptions without recursive matching or changing offsets."""
    parts = []
    end = 0
    for start, stop in (spans if spans is not None else _parenthesis_spans(text)).items():
        parts.extend((text[end:start], ' ' * (stop - start)))
        end = stop
    parts.append(text[end:])
    return ''.join(parts)


def _count_carbon_signals(signal_text: str) -> int:
    """Bound legacy expansion, excluding coupling nuclei from annotation counts."""
    signal_text = _mask_coupling_nuclei(signal_text)
    signal_text = re.sub(r'(\d+)\s*-\s*(\d+)', r'\1-\2', signal_text)
    signal_text = _normalize_carbon_multiplicity(signal_text)
    range_pattern = r'-?\d+\.\d+\s*[\-\–\—]\s*-?\d+\.\d+'
    count = len(re.findall(range_pattern, _outside_parentheses(signal_text)))
    # Preserve the legacy expansion preflight for malformed range/count tokens.
    signal_text = re.sub(range_pattern, '', signal_text)
    spans = _parenthesis_spans(signal_text)
    for start, end in spans.items():
        for match in re.compile(_CARBON_MULTIPLICITY_PATTERN).finditer(signal_text, start, end):
            count += _bounded_carbon_count(match.group(1)) - 1
            if count > _MAX_CARBON_SIGNALS:
                raise _CarbonSignalLimitExceeded("13C signal expansion exceeds the supported limit")
    count += len(re.findall(r'-?\d+\.\d+', _outside_parentheses(signal_text, spans)))
    if count > _MAX_CARBON_SIGNALS:
        raise _CarbonSignalLimitExceeded("13C signal expansion exceeds the supported limit")
    return count


def _counted_signal_text(nmr_text: str) -> str:
    """Select the text the ordinary signal counter reads, from the next δ onward."""
    nmr_text = re.sub(r'(\d+)\s*-\s*(\d+)', r'\1-\2', nmr_text)  # remove extra spaces
    nmr_after_delta = re.search(r'δ\s*\D*?(\d.*)', nmr_text)
    return nmr_after_delta.group(1).strip() if nmr_after_delta else nmr_text


def _bounded_carbon_tokens(signal_text: str) -> tuple[list[re.Match[str]], dict[int, int]]:
    """Check the shared represented-atom budget before preparing shift values."""
    signal_text = _normalize_carbon_multiplicity(_mask_coupling_nuclei(signal_text))
    _count_carbon_signals(signal_text)
    # Replace intervals (for example, '130.9-131.1' → '130.9')
    signal_text = re.sub(r'(-?\d+\.\d+)\s*[\-\–\—]\s*-?\d+\.\d+', r'\1', signal_text)

    # Check every accepted count before int conversion or ordinary NMR counting,
    # including annotations whose preceding shift is not accepted by this parser.
    for match in re.finditer(rf'\([^)]*?{_CARBON_MULTIPLICITY_PATTERN}[^)]*\)', signal_text):
        _bounded_carbon_count(match.group(1))

    multiplicities = {}
    for match in re.finditer(
        rf'(?<![\d.])(-?\d+\.\d+)\s*\([^)]*?{_CARBON_MULTIPLICITY_PATTERN}[^)]*\)', signal_text,
    ):
        if len(multiplicities) >= _MAX_CARBON_SIGNALS:
            raise _CarbonSignalLimitExceeded("13C signal expansion exceeds the supported limit")
        multiplicities[match.start(1)] = _bounded_carbon_count(match.group(2))
    matches = []
    represented_count = 0
    for match in re.finditer(r'(?<![\d.])-?\d+\.\d+(?![^()]*\))', signal_text):
        represented_count += multiplicities.get(match.start(), 1)
        if represented_count > _MAX_CARBON_SIGNALS or len(matches) >= _MAX_CARBON_SIGNALS:
            raise _CarbonSignalLimitExceeded("13C signal expansion exceeds the supported limit")
        matches.append(match)
    return matches, multiplicities


def preprocess_signals(signal_text: str) -> list[float]:
    """Return the capped legacy atom-expanded shifts, retaining range endpoints."""
    matches, multiplicities = _bounded_carbon_tokens(signal_text)

    # Repeat parsed values, never document-controlled text. The total is checked
    # first so neither a long shift token nor several counts amplify allocation.
    signals = []
    for match in matches:
        signals.extend([float(match.group())] * multiplicities.get(match.start(), 1))
    return signals


def _mask_coupling_nuclei(description: str) -> str:
    """Hide J-label nuclei while retaining offsets and adjacent annotations."""
    carbon_label = r'(?:13\s*+)?[CС]'
    other_label = r'(?:\d++\s*+)?(?:[ABD-Z][a-z]?|C[a-z])'
    token = rf'(?:{carbon_label}|{other_label})(?![A-Za-z0-9])'
    nucleus = rf'(?:\(\s*+)*+{token}\s*+\)*+'
    # Possessive whitespace/bracket runs avoid overlapping backtracking paths.
    label = rf'(?<!\w)(?:[0-9⁰¹²³⁴⁵⁶⁷⁸⁹]++)?J\s*+{nucleus}(?:\s*+[,/\-‐‑‒–—]\s*+{nucleus})?'
    return re.sub(label, lambda match: ' ' * len(match.group()), description)


def _parse_carbon_observations(signal_text: str) -> tuple[list[CarbonObservation], list[str]]:
    """Parse each top-level shift once and retain bounded overlap annotations."""
    _count_carbon_signals(signal_text)
    spans = _parenthesis_spans(signal_text)
    outside = _outside_parentheses(signal_text, spans)
    range_ends = {}

    def mask_range(match):
        range_ends[match.start()] = match.end()
        return match.group(1) + " " * (len(match.group()) - len(match.group(1)))

    masked = re.sub(r'(-?\d+\.\d+)\s*[\-\–\—]\s*-?\d+\.\d+', mask_range, outside)
    observations, errors = [], []
    represented_count = 0
    for match in re.finditer(r'(?<![\d.])-?\d+\.\d+', masked):
        end = range_ends.get(match.start(), match.end())
        bracket_start = end
        while bracket_start < len(signal_text) and signal_text[bracket_start].isspace():
            bracket_start += 1
        bracket_end = spans.get(bracket_start)
        represented_atoms = None
        if bracket_end is not None:
            end = bracket_end
            description = _mask_coupling_nuclei(signal_text[bracket_start:end])
            counts = list(re.finditer(r'(?<!\d)((?:[+\-–—]\s*+)?\d++)\s*+[CС](?![A-Za-z0-9])', description))
            if counts:
                token = ''.join(counts[0].group(1).split())
                prefix = description[counts[0].start() - 1] if counts[0].start() else ""
                if prefix and (prefix.isalnum() or prefix in '._'):
                    errors.append(f"Carbon atom annotation {signal_text[match.start():end]!r} must specify one positive count per observation")
                else:
                    digits = token.lstrip('+-–—').lstrip('0') or '0'
                    represented_atoms = _bounded_carbon_count(digits)
                    if token.startswith(('-', '–', '—')):
                        represented_atoms = -represented_atoms
                    if represented_atoms <= 0 or len(counts) != 1 or token.startswith('+'):
                        errors.append(f"Carbon atom annotation {signal_text[match.start():end]!r} must specify one positive count per observation")
                        represented_atoms = None
        represented_count += represented_atoms if represented_atoms is not None else 1
        if represented_count > _MAX_CARBON_SIGNALS:
            raise _CarbonSignalLimitExceeded("13C signal expansion exceeds the supported limit")
        shift = float(match.group())
        if not math.isfinite(shift):
            shift = None
            errors.append(f"Carbon chemical shift {signal_text[match.start():end]!r} is outside the finite numeric range")
        observations.append(CarbonObservation(shift, represented_atoms, signal_text[match.start():end], match.start(), end))
    return observations, errors


def _normalize_carbon_multiplicity(signal_text: str) -> str:
    """Use one spelling for the carbon-count forms already accepted by validation."""
    return re.sub(
        r'(?<![\d.])(\d+)\s*[CС](?![A-Za-z0-9])',
        lambda match: f"{match.group(1).lstrip('0') or '0'}C",
        signal_text,
    )


def _has_complete_dp5_shift_tokens(signal_text: str) -> bool:
    """Refuse DP5 when its decimal-only shift parser would omit numeric input."""
    outside_brackets = _outside_parentheses(signal_text)
    # Both endpoints belong to an accepted range, although only the first is scored.
    remainder = re.sub(r'-?\d+\.\d+\s*[\-\–\—]\s*-?\d+\.\d+', '', outside_brackets)
    remainder = re.sub(r'(?<![\d.])-?\d+\.\d+', '', remainder)
    return re.search(r'\d', remainder) is None


def count_signals_by_regions(signals):

    # Define the boundaries of the regions
    signal1 = signal2 = signal3 = signal4 = 0

    # Iterate through the numbers and distribute them into regions
    for signal in signals:
        if signal < 80:
            signal1 += 1
        elif 80 <= signal < 120:
            signal2 += 1
        elif 120 <= signal < 170:
            signal3 += 1
        elif signal >= 170:
            signal4 += 1

    return signal1, signal2, signal3, signal4 



def _calculate_nmr_number_count(nmr_text, n, hnmr_sum, context: CheckContext,
                                carbon_evidence: CarbonSignalEvidence | None = None) -> NumberCountResult:
    """Typed implementation behind `calculate_nmr_number_count`."""
    cfg, tracker = context.cfg, context.sink
    nuclei, atom = context.nucleus.label, context.nucleus.atom
    formula, onh_count = context.formula_nmr, context.onh_count
    warn = None
    warn_5 = []
    #0H 1C 2F 3B 4P 5 Si 7 N
    count = 0
    all_numbers = []  # To check the order of numbers.
    all_numbers_st = []  # To check the significant figures after the decimal point
    ratio = 0
    if atom == 1:
        if carbon_evidence is None:
            observations, errors = _parse_carbon_observations(nmr_text)
            carbon_evidence = CarbonSignalEvidence(observations, errors, [])
        nmr_text_no_brackets = _outside_parentheses(nmr_text)
    else:
        nmr_text = _counted_signal_text(nmr_text)
        nmr_text_no_brackets = regex.sub(r'\((?:[^()]|(?R))*\)', '', nmr_text)
    if atom == 0:
        # Misplaced integrals remain malformed source evidence, but are never
        # chemical shifts for precision, order or duplicate comparisons.
        nmr_text_no_brackets = re.sub(
            r'(?<![\w.])\d+(?:\.\d+)?\s*[HН](?!\w)', '', nmr_text_no_brackets
        )
    # create a list of signals
    raw_numbers = re.findall(r'-?\d+(?:\.\d+)?(?:\s*[\-\–\—]\s*-?\d+(?:\.\d+)?)?', nmr_text_no_brackets)

    for item in raw_numbers:
        match = re.match(r'(-?\d+(?:\.\d+)?)\s*[\-\–\—]\s*(-?\d+(?:\.\d+)?)', item)

        if match:
            start, end = match.groups()
            all_numbers.extend([float(start.strip()), float(end.strip())])
            all_numbers_st.extend([start.strip(), end.strip()])
        else:
            try:
                all_numbers.append(float(item.strip()))
                all_numbers_st.append(item.strip())
            except ValueError:
                pass  # pass incorrect value
    
    # Check the descending order
    out_of_order = []
    sorted_numbers = sorted(all_numbers, reverse=True)
    if all_numbers != sorted_numbers:
        out_of_order = [num for i, num in enumerate(all_numbers) if num != sorted_numbers[i]]
        if out_of_order:
            warn = emit_finding(
                cfg,
                tracker,
                ErrorCode.NMR_SIGNAL_ORDER_INVALID,
                context={"nuclei": nuclei, "signals": out_of_order},
            )
    else:
        warn = None

    # Look for numbers with an incorrect number of significant digits
    invalid_numbers = [
        num for num in all_numbers_st
        if "." not in num or len(num.split(".")[-1]) > (n + 1) or len(num.split(".")[-1]) < n
    ]
    if invalid_numbers:
        warn_4 = emit_finding(
            cfg,
            tracker,
            ErrorCode.NMR_SIGNAL_PRECISION_INVALID,
            context={
                "max_digits": n + 1,
                "min_digits": n,
                "nuclei": nuclei,
                "invalid_numbers": invalid_numbers,
            },
        )
    else:
        warn_4 = None  

    # Filter numbers that have exactly n+1 digits after the decimal point. If the frequency is ≥ 300, then n = 2; if it's less, then n = 1
    filtered_numbers = [num for num in all_numbers_st if len(num.split(".")[-1]) == (n+1)]

    # Round the numbers to n decimal places and check for duplicates
    rounded_numbers = [str(round(float(num), n)) for num in filtered_numbers]
    counted_numbers = Counter(rounded_numbers)

    # Remove pairs of identical values
    unique_filtered_numbers = [
        num for num in filtered_numbers if counted_numbers[str(round(float(num), n))] % 2 != 0
    ]

    # Check if the list is empty
    if unique_filtered_numbers:
        warn_2 = emit_finding(
            cfg,
            tracker,
            ErrorCode.NMR_SIGNAL_ROUNDING_NEEDED,
            context={"nuclei": nuclei, "invalid_numbers": unique_filtered_numbers},
        )
    else:
        warn_2 = None  

    # Check for duplicates
    all_numbers_counted = Counter(all_numbers)
    duplicates_in_all_numbers = [num for num, count in all_numbers_counted.items() if count > 1]
    if duplicates_in_all_numbers:
        if atom == 0:
            warn_3 = emit_finding(
                cfg,
                tracker,
                ErrorCode.HNMR_DUPLICATE_SIGNALS,
                context={"nuclei": nuclei, "duplicates": duplicates_in_all_numbers},
            )
        else:
            warn_3 = emit_finding(
                cfg,
                tracker,
                ErrorCode.XNUC_DUPLICATE_SIGNALS,
                context={"nuclei": nuclei, "duplicates": duplicates_in_all_numbers},
            )
    else:
        warn_3 = None  # 

    count = 0
    if atom == 1:
        count = carbon_evidence.minimum_atoms
    elif atom > 0:
        # Count the number of signals - Search for ranges like 153.0-154.0
        ranges = re.findall(r'(-?\d+\.\d+)\s*[\-\–\—]\s*(-?\d+\.\d+)', nmr_text)
        for start, end in ranges:
            count += 1
            
        # Remove the ranges
        nmr_text = re.sub(r'-?\d+\.\d+\s*[\-\–\—]\s*-?\d+\.\d+', '', nmr_text)

        #Count the number of signals - Search for (X C) inside parentheses
        # 1C 2F 3B 4P 5Si
        matches = None
        if atom == 2:
            matches = re.findall(r'\([^)]*?(\d+)\s*F(?![A-Za-z0-9])[^)]*?\)', nmr_text)
        elif atom == 3:
            matches = re.findall(r'\([^)]*?(\d+)\s*B(?![A-Za-z0-9])[^)]*?\)', nmr_text)
        elif atom == 4:
            matches = re.findall(r'\([^)]*?(\d+)\s*P(?![A-Za-z0-9])[^)]*?\)', nmr_text)
        elif atom == 5:
            matches = re.findall(r'\([^)]*?(\d+)\s*Si(?![A-Za-z0-9])[^)]*?\)', nmr_text)

        if matches:
            for match in matches:
                count += int(match) - 1  

        # Remove everything inside the parentheses
        nmr_text = re.sub(r'\(.*?\)', '', nmr_text)

        # Search for individual signals
        numbers = re.findall(r'-?\d+\.\d+', nmr_text)
        count += len(numbers) 
    else:
        formula_h_count = 0
        # Compare the number of hydrogens in the formula and in the HNMR
        if formula:
            
            # Extract the number of H from the formula.
            formula_h_count = count_hydrogen(formula)
            if formula_h_count:
                
                # Compare the number of protons in the HNMR with the HRMS or EA, and determine the ratio if it's a mixture of substances
                war1, war2, error_ok, ratio = _check_H_difference(
                    cfg,
                    tracker,
                    hnmr_sum,
                    formula_h_count,
                    onh_count,
                )
                if war1: 
                    warn_5.append(war1)
                if war2:
                    warn_5.append(war2)
                else:
                    if error_ok is not None and cfg.modes.output_mode == OutputMode.DETAILED:
                        warn_5.append(AnalysisNote(f"{error_ok.text}, {hnmr_sum:.1f} = {formula_h_count}"))
    return NumberCountResult(count, warn, warn_2, warn_3, warn_4, warn_5, ratio)

def _match_nmr_section(text, nuclei):
    """
    Extracts the NMR data section from the text based on the nucleus.
    Handles special cases for 13C NMR decoupling.
    """
    nmr_match = None
    f_dec = 0
    
    if nuclei == '13C':
        patterns = [
            (r'13C\s*\{1H\}\s*NMR(.*?)(?:[.;](?:\s|$))(?!(?:[^()]*\([^()]*\))*[^()]*\))', 0),
            (r'13C\s*NMR(.*?)(?:[.;](?:\s|$))(?!(?:[^()]*\([^()]*\))*[^()]*\))', 0),
            (r'13C\s*\{1H,\s*19F\}\s*NMR(.*?)(?:[.;](?:\s|$))(?!(?:[^()]*\([^()]*\))*[^()]*\))', 1),
            (r'13C\s*\{19F,\s*1H\}\s*NMR(.*?)(?:[.;](?:\s|$))(?!(?:[^()]*\([^()]*\))*[^()]*\))', 1),
            (r'13C\s*\{19F\}\s*NMR(.*?)(?:[.;](?:\s|$))(?!(?:[^()]*\([^()]*\))*[^()]*\))', 1),
        ]
        for pattern, dec_flag in patterns:
            nmr_match = re.search(pattern, text, re.DOTALL)
            if nmr_match:
                f_dec = dec_flag
                break
    else:
        pattern = (
            rf"{nuclei}\s*(?:{{1H}})?[\s-]?NMR"
            r"(.*?)(?:; |\. |\.\n|\.(?=\s*$)|\;(?=\s*$))"
            r"(?!(?:[^()]*\([^()]*\))*[^()]*\))"
        )
        nmr_match = re.search(pattern, text, re.DOTALL)

    return nmr_match, f_dec


def _extract_nmr_section(text, nuclei):
    """Return the selected source section and its fluorine-decoupling flag."""
    nmr_match, f_dec = _match_nmr_section(text, nuclei)
    if nmr_match:
        return nmr_match.group(1), f_dec
    return None, 0

def _parse_nmr_section(text, nuclei) -> ParsedSection:
    """Parse source and normalized facts, distinguishing absent and empty sections."""
    raw_text, f_dec = _extract_nmr_section(text, nuclei)
    if not raw_text:
        return ParsedSection(raw_text, fluorine_decoupled=f_dec)
    normalized = replace_dash(raw_text)
    ending = re.search(r'δ\s*\D*?(\d.*)', normalized)
    signals = re.search(r'δ\s*(.*)', normalized)
    outside = regex.sub(r'\((?:[^()]|(?R))*\)', '', normalized)
    header = regex.search(r"\((?:[^()]+|(?R))*\)", normalized)
    return ParsedSection(
        raw_text, normalized, f_dec,
        ending.group(1).strip() if ending else None,
        signals.group(1).strip() if signals else None,
        outside, header.group(0) if header else None,
    )


def _check_nmr_ending(parsed: ParsedSection, context: CheckContext) -> list[Diagnostic]:
    """Check forbidden ending terms independently of signal parsing."""
    warnings = []
    if parsed.ending_text is not None:
        forbidden_terms = ["NMR", "APCI", "ESI", "HRMS"]
        found = [term for term in forbidden_terms
                 if re.search(rf'\b{term}\b', parsed.ending_text, re.IGNORECASE)]
        if found:
            warning = emit_finding(context.cfg, context.sink, ErrorCode.BAD_ENDING_NMR,
                                   context={"nuclei": context.nucleus.label})
            if warning:
                warnings.append(warning)
    return warnings


def _check_nmr_formula(context: CheckContext) -> list[Diagnostic]:
    """Check the element-presence flag independently of diagnostic visibility."""
    warnings = []
    nuclei, atom = context.nucleus.label, context.nucleus.atom
    if context.e_formula != 1 and context.formula_nmr and atom > 1:
        element_name = "N" if atom == 7 else nuclei
        warning = emit_finding(context.cfg, context.sink, ErrorCode.NMR_MENTIONED_BUT_ELEMENT_MISSING,
                               context={"nuclei": nuclei, "element_name": element_name})
        if warning:
            warnings.append(warning)
    return warnings


def _check_coupling_constants(parsed: ParsedSection, context: CheckContext) -> list[Diagnostic]:
    """Checks for presence and format of coupling constants in an NMR section."""
    cfg, tracker = context.cfg, context.sink
    nuclei, atom = context.nucleus.label, context.nucleus.atom
    nmr_section, f_dec = parsed.normalized_text, parsed.fluorine_decoupled
    e_formula = context.e_formula
    warnings = []
    if atom > 0:  # For nuclei other than 1H
        check_constant_war3, nmr_const = _check_constant_nmr(cfg, tracker, nmr_section, nuclei)
        if check_constant_war3:
            warnings.extend(check_constant_war3)
        elif cfg.modes.output_mode == OutputMode.DETAILED:
            warnings.append(AnalysisNote(f"The presence of {nuclei} NMR constants has been verified"))

        if atom == 1:  # for 13C only
            # If molecule has Fluorine, and there is no F-decoupling, and no constants found, it's an error.
            if e_formula == 1 and nmr_const == 0 and f_dec == 0:
                warning = emit_finding(cfg, tracker, ErrorCode.C13_COUPLING_MISSING_WITH_F)
                if warning:
                    warnings.append(warning)
    else:  # For 1H NMR
        check_constant_war = _check_constant_hnmr(cfg, tracker, nmr_section)
        if check_constant_war:
            warnings.extend(check_constant_war)
        elif cfg.modes.output_mode == OutputMode.DETAILED:
            warnings.append(AnalysisNote("The presence of H NMR constants has been verified"))
    
    return warnings

def _check_solvent_and_frequency(parsed: ParsedSection, context: CheckContext) -> tuple[list[Diagnostic], int | float]:
    """Checks solvent, frequency, and residual solvent peaks."""
    cfg, tracker = context.cfg, context.sink
    nuclei, atom = context.nucleus.label, context.nucleus.atom
    frequency_h = context.frequency_h
    nmr_text_no_brackets = parsed.outside_brackets
    warnings = []
    frequency = 0
    solventfr_text = parsed.solvent_frequency_text
    
    if not solventfr_text:
        warning = emit_finding(
            cfg,
            tracker,
            ErrorCode.NMR_SOLVENT_OR_FREQUENCY_MISSING,
            context={"nuclei": nuclei},
        )
        if warning:
            warnings.append(warning)
        return warnings, frequency

    if cfg.modes.output_mode == OutputMode.DETAILED:
        warnings.append(AnalysisNote(f"Extracted content solventfr {nuclei}: {solventfr_text}"))

    # Check frequency
    warn_freq, frequency, warn_freq_match = extract_and_compare(solventfr_text, atom, frequency_h)
    if warn_freq:
        warning = emit_finding(
            cfg,
            tracker,
            ErrorCode.NMR_FREQUENCY_INVALID,
            context={"details": warn_freq, "nuclei": nuclei},
        )
        if warning:
            warnings.append(warning)
    
    if warn_freq_match and atom > 0:
        warning = emit_finding(
            cfg,
            tracker,
            ErrorCode.NMR_FREQUENCY_MISMATCH,
            context={"details": warn_freq_match},
        )
        if warning:
            warnings.append(warning)

    # Check solvent peaks
    error_textnmr, solvent_not_found = _find_peaks_in_texts(
        cfg,
        tracker,
        solventfr_text,
        nmr_text_no_brackets,
        nuclei,
        atom,
    )
    if error_textnmr:
        warnings.extend(error_textnmr)
    
    if solvent_not_found:
        warning = emit_finding(
            cfg,
            tracker,
            ErrorCode.SOLVENT_NOT_FOUND,
            context={"details": solvent_not_found},
        )
        if warning:
            warnings.append(warning)
    
    return warnings, frequency

def _parse_carbon_signal_evidence(signal_section, context: CheckContext) -> CarbonSignalEvidence:
    """Populate the borrowed observation buffer before any number-count checks."""
    observations, errors = _parse_carbon_observations(signal_section)
    context.preparation.observations.extend(observations)
    return CarbonSignalEvidence(observations, errors,
                                [item.shift for item in observations if item.shift is not None])


def _check_proton_integrals(signal_section, context: CheckContext) -> tuple[list[Diagnostic], float]:
    """Check integrals and summarize their total independently of signal counting."""
    cfg, tracker = context.cfg, context.sink
    formula_nmr = context.formula_nmr
    warnings = []
    hnmr_values = re.findall(r'\([^)]*?\b(\d+(?:\.\d+)?)\s?[HН](?:[,;]|\))', signal_section)
    hnmr_values_f = list(map(float, hnmr_values))
    non_integer_values = [value for value in hnmr_values_f if not value.is_integer()]
    if non_integer_values:
        warning = emit_finding(
            cfg,
            tracker,
            ErrorCode.H_COUNT_NOT_INTEGER,
            context={"non_integer_values": non_integer_values},
        )
        if warning:
            warnings.append(warning)
    hnmr_sum = sum(hnmr_values_f)
    if hnmr_sum > 0:
        msg = f"H NMR Result: {hnmr_sum}H"
        if not formula_nmr:
            msg = f"❗{msg}"
        warnings.append(AnalysisNote(msg, 'yellow' if not formula_nmr else None))
    else:
        warning = emit_finding(cfg, tracker, ErrorCode.HNMR_MISSING_OR_UNCOUNTED)
        if warning:
            warnings.append(warning)

    return warnings, hnmr_sum


def _accepted_boron_absence(nmr_count: int, context: CheckContext) -> int:
    """Accept at most one absence per proved direct environment, without assignment."""
    policy = context.boron_diagnostic_policy
    if (not context.contains_boron or policy is None or not policy.partition_complete
            or not context.carbon_signal_format_valid or nmr_count <= 0 or context.trees_count is None):
        return 0
    missing = len(policy.groups) - nmr_count
    return missing if 0 < missing <= len(policy.direct_environments) else 0


def _carbon_scope(context: CheckContext, observation_count: int, represented_count: int) -> dict:
    """Project diagnostic/scoring scope into plain, source-indexed JSON values."""
    policy = context.boron_diagnostic_policy
    complete = (policy.partition_complete if policy is not None else
                context.structure_partition_complete and context.trees_count is not None)
    missing = _accepted_boron_absence(represented_count, context)
    direct = ([list(policy.groups[index].labels) for index in policy.direct_environments]
              if policy is not None else [list(group) for group in context.direct_boron_groups])
    structure = context.structure_result
    return dict(
        full_environment_count=(len(policy.groups) if policy is not None and complete else
                                context.trees_count if complete else None),
        native_candidate_environment_count=context.trees_count,
        observation_count=observation_count, represented_signal_count=represented_count,
        accepted_missing_direct_cb_count=missing,
        accepted_missing_direct_cb_environments=direct if missing and missing == len(direct) else [],
        eligible_direct_cb_environments=direct,
        direct_cb_atom_pairs=[list(pair) for pair in policy.direct_atom_pairs] if policy is not None else [],
        source_fragments=[list(fragment) for fragment in policy.source_fragments] if policy is not None else [],
        carbon_component_original_indices=list(policy.carbon_component_original_indices) if policy is not None else [],
        applicability_status=BORON_REASON if context.contains_boron else 'unassessed',
        configuration_status=policy.configuration_status if policy is not None else context.configuration_status,
        partition_complete=complete,
        partition_reason=(policy.partition_reason if policy is not None else
                          context.structure_partition_reason or context.policy_reason
                          or ('structure_unavailable' if context.trees_count is None else None)),
        scoring_partition_complete=context.structure_partition_complete and context.trees_count is not None,
        scoring_partition_reason=context.structure_partition_reason,
        expected_counts=list(structure.expected_counts('C')) if structure is not None else [],
        configuration_dependent=bool(structure is not None and structure.configuration_dependent('C')),
        isomer_scope_dependent=bool(structure is not None and any(
            structure.configuration_dependent(element) for element in ('C', 'F', 'Si', 'B', 'P'))),
        selected_isomer_smiles=context.selected_isomer_smiles,
    )


def _select_carbon_configuration(context: CheckContext, evidence: CarbonSignalEvidence) -> CheckContext:
    """Choose by structural coverage only, before predictions or score inspection."""
    source = context.structure_result
    if source is None or not source.partition_complete or not any(
            source.configuration_dependent(element) for element in ('C', 'F', 'Si', 'B', 'P')):
        return context
    candidates = []
    for variant in source.isomer_partitions:
        reason = None if context.policy_reason == 'group_limit' else context.policy_reason
        policy = None
        if reason is None:
            try:
                policy = derive_observation_policy(variant.isomer, variant.carbon_groups, structure=source)
            except AssignmentError as exc:
                reason = exc.reason
        candidate = replace(context, trees_count=len(variant.for_element('C').groups),
                            carbon_groups=variant.carbon_groups, policy_reason=reason,
                            coincident_environments=policy.coincident_environments if policy is not None else ())
        candidates.append((candidate, variant, policy))
        if reason is not None:
            continue
        try:
            validate_observation_coverage(len(policy.groups), len(evidence.observations),
                observation_atom_counts=[item.represented_atoms for item in evidence.observations],
                coincident_environments=policy.coincident_environments)
            break
        except AssignmentError:
            continue
    else:
        # Preserve the legitimate configuration count for diagnostics even when
        # annotations prevent scoring; no observation-to-atom assignment is guessed.
        candidate, variant, policy = next((item for item in candidates
                                          if item[0].trees_count == evidence.minimum_atoms), candidates[0])
    alternatives = [C13Alternative('formal', candidate.trees_count, variant.carbon_groups)]
    if variant.merged_carbon_count != candidate.trees_count:
        alternatives.append(C13Alternative('diastereotopic_merged', variant.merged_carbon_count, [], True))
    return replace(candidate, c13_count_alternatives_w_groups=alternatives,
                   c13_count_alternatives={option.count for option in alternatives} | set(source.expected_counts('C')),
                   stereogenic_signal_flag=variant.merged_carbon_count != candidate.trees_count,
                   selected_isomer_smiles=policy.selected_isomer_smiles if policy is not None else variant.smiles)


def _configuration_diagnostic(context: CheckContext) -> Finding | None:
    source = context.structure_result
    element = {'13C': 'C', '19F': 'F', '29Si': 'Si', '11B': 'B', '31P': 'P'}.get(context.nucleus.label)
    if source is None or element is None or not source.configuration_dependent(element):
        return None
    counts = source.expected_counts(element)
    return emit_finding(context.cfg, context.sink, ErrorCode.NMR_CONFIGURATION_DEPENDENT,
                        context={'nuclei': context.nucleus.label, 'counts': ' or '.join(map(str, counts)),
                                 'count': counts[0], 'expected_counts': list(counts)},
                        variant='default' if len(counts) > 1 else 'grouping')


def _check_carbon_count(nmr_count, evidence: CarbonSignalEvidence, context: CheckContext) -> CarbonCountResult:
    """Compare environment counts and select the first matching partition."""
    cfg, tracker = context.cfg, context.sink
    trees_count, formula_nmr = context.trees_count, context.formula_nmr
    if context.boron_diagnostic_policy is not None and context.boron_diagnostic_policy.partition_complete:
        trees_count = len(context.boron_diagnostic_policy.groups)
    carbon_groups = context.carbon_groups
    c13_count_alternatives = context.c13_count_alternatives
    c13_count_alternatives_w_groups = context.c13_count_alternatives_w_groups
    stereogenic_signal_flag = context.stereogenic_signal_flag
    atom_with_four_unique_substituents = context.atom_with_four_unique_substituents
    atom_with_two_identical_substituents = context.atom_with_two_identical_substituents
    c_signals_list = evidence.shifts
    minimum_atoms = evidence.minimum_atoms
    warnings = []
    eq_c_ratio = 100
    selected_count = trees_count
    dp5_carbon_groups = carbon_groups if carbon_groups is not None else []
    run_dp5 = False
    # Scoring receives each observed peak once, independently of the diagnostic
    # signal count: an (nC) annotation represents n overlapping signals.
    observed_count = len(evidence.observations)
    # An incomplete stereo proof is advisory; the engine may still build a
    # usable geometry for the retained source-frame count estimate.
    scoring_partition_available = trees_count is not None
    context.preparation.coverage_reason = None
    if scoring_partition_available and not context.policy_reason:
        try:
            validate_observation_coverage(trees_count, observed_count,
                observation_atom_counts=[item.represented_atoms for item in evidence.observations],
                coincident_environments=context.coincident_environments)
            run_dp5 = True
        except AssignmentError as exc:
            context.preparation.coverage_reason = exc.reason
    context.preparation.coincident_environments = context.coincident_environments
    region_signals = (item.shift for item in evidence.observations if item.shift is not None
                      for _ in range(item.represented_atoms if item.represented_atoms is not None else 1))
    signal1, signal2, signal3, signal4 = count_signals_by_regions(region_signals)

    msg = f"C NMR Result: {nmr_count}. Signals: [>170] {signal4}, [120-170] {signal3}, [80-120] {signal2}, [<80] {signal1}"
    if not trees_count:
        msg = f"❗ {msg}"
    warnings.append(AnalysisNote(msg, 'yellow' if not trees_count else None))

    c_num_hrms = count_carbon(formula_nmr) if formula_nmr else 0

    accepted_absence = _accepted_boron_absence(nmr_count, context) if not evidence.annotation_errors else 0
    diagnostic_complete = (context.boron_diagnostic_policy is not None
                           and context.boron_diagnostic_policy.partition_complete)
    if accepted_absence:
        warning = emit_finding(cfg, tracker, ErrorCode.C13_BORON_SIGNAL_NOT_OBSERVED, context={
            'details': f'Full expected environments: {trees_count}; represented signals: {nmr_count}; '
                       f'accepted absent directly bonded C–B environments: {accepted_absence}.'})
        if warning:
            warnings.append(warning)

    if trees_count is not None and (not context.contains_boron or diagnostic_complete):
        if cfg.modes.output_mode == OutputMode.DETAILED:
            warnings.append(AnalysisNote(f"Num unique С in mol file: {trees_count}"))
        if nmr_count != trees_count and not accepted_absence:
            c13_count_alternatives_w_groups = c13_count_alternatives_w_groups or []
            matching_c13_alternatives = [
                alternative
                for alternative in c13_count_alternatives_w_groups
                if alternative.count == nmr_count
            ]

            warning = None

            if matching_c13_alternatives and (stereogenic_signal_flag or any(
                    getattr(option, "manual_check_only", False) for option in matching_c13_alternatives)):
                # A manual lower count never selects a merged prediction partition.
                matched_alternative = matching_c13_alternatives[0]
                manual = getattr(matched_alternative, "manual_check_only", False)
                if manual:
                    if not run_dp5:
                        context.preparation.refusal_details["cause"] = "unresolved_coincident_environments"
                    warnings.append(AnalysisNote(
                        f"Full expected carbon environments: {trees_count}. "
                        "Distinct environments may have coincident shifts; manual verification is required. "
                        + ("The comparison preserves separate predictions against shared observations."
                           if run_dp5 else "The comparison is unassessed while observation sharing is unresolved.")
                    ))
                warning = emit_finding(
                    cfg,
                    tracker,
                    ErrorCode.C13_COUNT_STRUCTURE_CHECK_STEREO,
                    context={"nmr_count": nmr_count, "trees_count": trees_count},
                    variant="manual_coincidence" if manual else "default",
                )
            else:
                if trees_count == 0:
                    warning = emit_finding(cfg, tracker, ErrorCode.C13_COUNT_STRUCTURE_CHECK, variant="parsed_name")
                else:
                    finding_context = {"nmr_count": nmr_count, "trees_count": trees_count}
                    variant = "count_high" if nmr_count > trees_count else "count_low"
                    # Alternatives equal to trees_count are already printed before "or"
                    other_counts = sorted(set(c13_count_alternatives or ()) - {trees_count})
                    if stereogenic_signal_flag and other_counts:
                        finding_context["c_set"] = " or ".join(str(count) for count in other_counts)
                        variant = f"{variant}_c_set"
                    warning = emit_finding(
                        cfg,
                        tracker,
                        ErrorCode.C13_COUNT_STRUCTURE_CHECK,
                        context=finding_context,
                        variant=variant,
                    )

                if atom_with_four_unique_substituents > 0 and atom_with_two_identical_substituents:
                    stereo_note = emit_finding(cfg, tracker, ErrorCode.C13_COUNT_STRUCTURE_CHECK, variant="diastereotopic")
                    if stereo_note:
                        warnings.append(stereo_note)
            if warning:
                warnings.append(warning)
        else:
            if c_num_hrms > 0:
                c_diff_nmr_hrms = c_num_hrms - nmr_count
                if not accepted_absence:
                    warnings.append(AnalysisNote(f"equivalent C in 13C NMR: {c_num_hrms} - {nmr_count} = {c_diff_nmr_hrms} "))
    elif trees_count is None:  # Formula inventory is only a no-structure fallback.
        if c_num_hrms:
            if minimum_atoms > c_num_hrms:
                warning = emit_finding(
                    cfg,
                    tracker,
                    ErrorCode.C13_COUNT_EXCEEDS_FORMULA,
                    context={"nmr_count": minimum_atoms, "formula_c_count": c_num_hrms},
                )
                if warning:
                    warnings.append(warning)
            else:
                c_diff_nmr_hrms = c_num_hrms - nmr_count
                warnings.append(AnalysisNote(f"❗ Check equivalent  C: {c_num_hrms} - {nmr_count} = {c_diff_nmr_hrms} ", 'yellow'))
    if c_num_hrms and nmr_count:
        eq_c_ratio = nmr_count / c_num_hrms

    preparation = context.preparation
    if not scoring_partition_available:
        run_dp5 = False
        dp5_carbon_groups = carbon_groups if carbon_groups is not None else []
        preparation.refusal_details.update(
            cause="structure_partition_incomplete",
            structure_reason=context.structure_partition_reason or "unassessed",
        )
    preparation.groups = dp5_carbon_groups
    preparation.eligible = run_dp5
    preparation.shifts = c_signals_list
    return CarbonCountResult(warnings, eq_c_ratio, selected_count, c_num_hrms, preparation)


def _check_carbon_annotations(evidence: CarbonSignalEvidence,
                              checked: CarbonCountResult, context: CheckContext) -> list[Diagnostic]:
    """Apply inventory and partition constraints without changing counted environments."""
    cfg, tracker = context.cfg, context.sink
    nuclei = context.nucleus.label
    trees_count, carbon_groups = context.trees_count, context.carbon_groups
    c_num_hrms = checked.formula_count
    c_observations, annotation_errors = evidence.observations, evidence.annotation_errors
    run_dp5 = checked.preparation.eligible
    dp5_refusal_details = checked.preparation.refusal_details
    warnings = []
    # Counts constrain formal environments within each selected group. Symmetry
    # atoms do not multiply that count; an unknown annotation remains a wildcard.
    minimum_atoms = evidence.minimum_atoms
    structure_atoms = (trees_count + sum(len(group) - 1 for group in (carbon_groups or []))
                       if trees_count is not None else None)
    atom_budget = structure_atoms if structure_atoms is not None else c_num_hrms or None
    refusal = None
    if annotation_errors:
        refusal = "invalid_carbon_annotation"
        warning = emit_finding(cfg, tracker, ErrorCode.NMR_SIGNAL_FORMAT,
                             context={"nuclei": nuclei, "error": "; ".join(dict.fromkeys(annotation_errors)).rstrip(".")})
        if warning:
            warnings.append(warning)
    elif (any(item.represented_atoms is not None for item in c_observations)
          and atom_budget is not None and minimum_atoms > atom_budget):
        # The structure count comparison already owns the analytical finding.
        # Keep impossible inventory as a hard scoring gate, including when the
        # count diagnostic is hidden or DP5q has not been requested.
        refusal = "annotation_inventory_mismatch"
    elif (trees_count is not None and context.structure_partition_complete and not context.policy_reason
          and not _accepted_boron_absence(minimum_atoms, context)):
        reason = checked.preparation.coverage_reason
        if reason is None:
            if len(c_observations) < trees_count and evidence.minimum_atoms == trees_count:
                warnings.append(AnalysisNote(
                    f"Reported overlap retains {trees_count} distinct environments and {len(c_observations)} original observations."
                ))
        else:
            refusal = (dp5_refusal_details.get('cause', 'count_mismatch')
                       if reason == 'strict_nmr_coverage_mismatch' else reason)
    if refusal:
        run_dp5 = False
        dp5_refusal_details["cause"] = refusal

    if context.policy_reason:
        run_dp5 = False
        dp5_refusal_details.setdefault("cause", context.policy_reason)
    if context.contains_boron:
        run_dp5 = False
        dp5_refusal_details.setdefault("cause", BORON_REASON)
    checked.preparation.eligible = run_dp5
    return warnings


def _analyze_nmr_signals(signal_section, frequency, context: CheckContext) -> SignalAnalysisResult:
    """Analyzes NMR signals for various nuclei, including counts, precision, and comparisons."""
    cfg, tracker = context.cfg, context.sink
    nuclei, atom = context.nucleus.label, context.nucleus.atom
    trees_count = context.trees_count
    dp5_refusal_details = context.preparation.refusal_details
    warnings = []
    c_signals_list = None
    ratio = 0
    eq_c_ratio = 100
    dp5_carbon_groups = []
    run_dp5 = False
    selected_count = trees_count
    if atom == 1:
        try:
            evidence = _parse_carbon_signal_evidence(signal_section, context)
            if evidence.annotation_errors:
                context = replace(context, carbon_signal_format_valid=False)
            c_signals_list = evidence.shifts
            context = _select_carbon_configuration(context, evidence)
            trees_count = selected_count = context.trees_count
        except _CarbonSignalLimitExceeded:
            warning = emit_finding(
                cfg, tracker, ErrorCode.NMR_SIGNAL_FORMAT,
                context={"limit": _MAX_CARBON_SIGNALS}, variant="signal_limit",
            )
            if warning:
                warnings.append(warning)
            dp5_refusal_details.update(cause="signal_limit_exceeded", limit=_MAX_CARBON_SIGNALS)
            context.preparation.groups = []
            context.preparation.eligible = False
            context.preparation.shifts = None
            return SignalAnalysisResult(warnings, ratio, eq_c_ratio, context.preparation)
    # Determine precision 'n' based on frequency
    n = 2 if (atom == 0 or frequency > 299) else 1
    
    hnmr_sum = 0
    if atom == 0: # 1H-specific: calculate proton sum
        integral_warnings, hnmr_sum = _check_proton_integrals(signal_section, context)
        warnings.extend(integral_warnings)

    # Common signal analysis for all nuclei
    count_result = _calculate_nmr_number_count(
        signal_section, n, hnmr_sum, context, evidence if atom == 1 else None
    )
    
    nmr_count = count_result.count
    ratio = count_result.ratio
    conditional = _configuration_diagnostic(context)
    if conditional:
        warnings.append(conditional)

    if count_result.order:
        warnings.append(count_result.order)
    elif cfg.modes.output_mode == OutputMode.DETAILED:
        warnings.append(AnalysisNote(f"Order {nuclei} NMR signals ok"))
    if count_result.rounding:
        warnings.append(count_result.rounding)
    if count_result.duplicate:
        warnings.append(count_result.duplicate)
    if count_result.precision:
        warnings.append(count_result.precision)
    if count_result.hydrogen:
        warnings.extend(count_result.hydrogen)

    # Post-analysis checks for XNUC
    if 0 < atom < 7 and atom != 1:
        source = context.structure_result
        element = {'19F': 'F', '29Si': 'Si', '11B': 'B', '31P': 'P'}.get(nuclei)
        expected = source.expected_counts(element) if source is not None and element else ()
        if trees_count is not None and nmr_count not in (expected or (trees_count,)):
            warning = emit_finding(
                cfg,
                tracker,
                ErrorCode.NMR_SIGNAL_COUNT_STRUCTURE_MISMATCH,
                context={"nuclei": nuclei, "nmr_count": nmr_count, "trees_count": trees_count},
            )
            if warning:
                warnings.append(warning)
            if (source is not None and element and not source.configuration_dependent(element)
                    and any(variant.for_element(element).diastereotopic_split
                            for variant in source.isomer_partitions)):
                warning = emit_finding(cfg, tracker, ErrorCode.NMR_DIASTEREOTOPIC_NOTE, context={'nuclei': nuclei})
                if warning:
                    warnings.append(warning)
        


    if nmr_count == 0 and atom > 0 and (atom != 1 or not evidence.observations):
        c_signals_list = None
        warning = emit_finding(cfg, tracker, ErrorCode.NMR_SECTION_MISSING, context={"nuclei": nuclei})
        if warning:
            warnings.append(warning)
    elif atom == 1: # Post-analysis for 13C
        checked = _check_carbon_count(nmr_count, evidence, context)
        warnings.extend(checked.diagnostics)
        warnings.extend(_check_carbon_annotations(evidence, checked, context))
        eq_c_ratio = checked.eq_c_ratio
        selected_count = checked.selected_count
        dp5_carbon_groups = checked.preparation.groups
        run_dp5 = checked.preparation.eligible

    if atom == 1:
        dp5_refusal_details.update(
            observed_count=len(evidence.observations),
            prepared_shift_count=len(c_signals_list or []),
            required_count=selected_count,
        )

    preparation = context.preparation
    if atom == 1:
        preparation.carbon_scope.clear()
        preparation.carbon_scope.update(_carbon_scope(context, len(evidence.observations), nmr_count))
        preparation.carbon_scope.update(
            applicability_status=BORON_REASON if context.contains_boron else 'eligible' if run_dp5 else 'unassessed',
            preparation_eligible=run_dp5, preparation_reason=dp5_refusal_details.get('cause'))
    preparation.groups = dp5_carbon_groups
    preparation.eligible = run_dp5
    preparation.shifts = c_signals_list
    return SignalAnalysisResult(warnings, ratio, eq_c_ratio, preparation)


def _find_missing_commas_between_signals(text: str, nuclei, atom):
    """Find adjacent NMR signal-like fragments not separated by a comma."""
    if atom == 0:
        signal_regex = r"""
            (?P<signal>
                -?\d+(?:\.\d+)?
                (?:\s*-\s*-?\d+(?:\.\d+)?)?
                (?:\s+and\s+-?\d+(?:\.\d+)?)?
                \s*
                \([^()]*\)
            )
        """
    else:
        signal_regex = r"""
            (?P<signal>
                -?\d+(?:\.\d+)?
                (?:\s*-\s*-?\d+(?:\.\d+)?)?
                (?:\s+and\s+-?\d+(?:\.\d+)?)?
                (?:\s*\([^()]*\))?
            )
        """

    signal_pattern = re.compile(signal_regex, re.VERBOSE)
    matches = list(signal_pattern.finditer(text))
    errors = []

    for index in range(len(matches) - 1):
        current_match = matches[index]
        next_match = matches[index + 1]
        between = text[current_match.end():next_match.start()]

        if not re.fullmatch(r"\s*,\s*", between):
            context = (
                f"{current_match.group('signal')}"
                f"{between}"
                f"{next_match.group('signal')}"
            )
            if re.fullmatch(r"\s+", between):
                errors.append(f"Missing comma between {nuclei} NMR signals near: {context}")
            else:
                errors.append(f"Invalid {nuclei} NMR signal formatting near: {context}")

    return errors


def _finalize_dp5_preparation(signal_section, comma_errors, preparation: DP5Preparation,
                              context: CheckContext) -> None:
    """Set refusal priority independently of displayed diagnostic visibility."""
    atom, trees_count = context.nucleus.atom, context.trees_count
    dp5_refusal_details = preparation.refusal_details
    complete_numeric_tokens = _has_complete_dp5_shift_tokens(signal_section)
    if atom == 1:
        if dp5_refusal_details.get("cause") == "signal_limit_exceeded":
            pass
        elif comma_errors:
            dp5_refusal_details["cause"] = "malformed_separators"
        elif not complete_numeric_tokens:
            dp5_refusal_details["cause"] = "incomplete_numeric_tokens"
        elif dp5_refusal_details.get("cause"):
            pass
        elif not preparation.eligible and trees_count is not None:
            dp5_refusal_details["cause"] = "count_mismatch"
        else:
            dp5_refusal_details.clear()
    preparation.eligible = preparation.eligible and not comma_errors and complete_numeric_tokens
    if atom == 1:
        preparation.carbon_scope.update(
            applicability_status=(BORON_REASON if context.contains_boron else
                                  'eligible' if preparation.eligible else 'unassessed'),
            preparation_eligible=preparation.eligible,
            preparation_reason=dp5_refusal_details.get('cause'),
        )


def _check_nmr(text, context: CheckContext) -> NMRCheckResult:
    """Compose ordered checks over parsed facts and per-call preparation state."""
    cfg, tracker = context.cfg, context.sink
    nuclei, atom = context.nucleus.label, context.nucleus.atom
    preparation = context.preparation
    dp5_refusal_details = preparation.refusal_details
    observations_out = preparation.observations
    warnings = []
    frequency = 0
    ratio = 0
    eq_c_ratio = 100
    observations_out.clear()
    if atom == 1:
        dp5_refusal_details.clear()
        preparation.coverage_reason = None
        preparation.carbon_scope.clear()
        preparation.carbon_scope.update(_carbon_scope(context, 0, 0))
        preparation.carbon_scope.update(preparation_eligible=False, preparation_reason='missing_section')

    parsed = _parse_nmr_section(text, nuclei)

    if not parsed.raw_text:
        if atom == 1:
            warning = emit_finding(cfg, tracker, ErrorCode.C13_SECTION_MISSING, context={"nuclei": nuclei})
            if warning:
                warnings.append(warning)
        preparation.groups = None
        return NMRCheckResult(warnings, frequency, ratio, None, eq_c_ratio, preparation)

    warnings.extend(_check_nmr_ending(parsed, context))
    warnings.extend(_check_coupling_constants(parsed, context))
    warnings.extend(_check_nmr_formula(context))
    solvent_freq_warnings, frequency = _check_solvent_and_frequency(parsed, context)
    warnings.extend(solvent_freq_warnings)
    
    # Analyze the signal data after 'δ'.
    if parsed.signals is not None:
        signal_section = parsed.signals

        comma_errors = _find_missing_commas_between_signals(signal_section, nuclei, atom)
        for error in comma_errors:
            warning = emit_finding(
                cfg,
                tracker,
                ErrorCode.NMR_SIGNAL_FORMAT,
                context={"nuclei": nuclei, "error": error},
            )
            if warning:
                warnings.append(warning)
        
        if atom == 1:
            context = replace(context, carbon_signal_format_valid=(
                not comma_errors and _has_complete_dp5_shift_tokens(signal_section)))
        signal_result = _analyze_nmr_signals(signal_section, frequency, context)
        warnings.extend(signal_result.diagnostics)
        ratio = signal_result.ratio
        eq_c_ratio = signal_result.eq_c_ratio
        _finalize_dp5_preparation(signal_section, comma_errors, preparation, context)

    return NMRCheckResult(warnings, frequency, ratio, parsed.raw_text, eq_c_ratio, preparation)


def check_presence_nmr_spectra(
    cfg: AppConfig,
    tracker: ErrorTracker,
    elements_in_formula,
    text,
    output,
):
    '''Check for the presence of spectra F, P, B, Si, 15N, D, if they are present in the actual formula'''
    diagnostics = []
    _check_presence_nmr_spectra(cfg, tracker, elements_in_formula, text, diagnostics)
    output.extend(messages(diagnostics))
    return output


def find_peaks_in_texts(cfg: AppConfig, tracker: ErrorTracker, text1, textnmr, nuclei, nmr):
    '''The presence of a specified solvent is verified, and if present, its 1H and 13C peaks are searched for in the text.'''
    diagnostics, missing = _find_peaks_in_texts(cfg, tracker, text1, textnmr, nuclei, nmr)
    return messages(diagnostics), missing


def check_H_difference(cfg: AppConfig, tracker: ErrorTracker, hnmr_sum, hrms_h_sum, onh_count):
    """
    The difference between the sums of HNMR and formula from name, HRMS or EA is checked. If the difference does not match expectations, an error is added
    """
    first, second, note, ratio = _check_H_difference(cfg, tracker, hnmr_sum, hrms_h_sum, onh_count)
    return message(first), message(second), message(note), ratio


def check_constant_hnmr(cfg: AppConfig, tracker: ErrorTracker, section):
    '''Checking the presence of constants, the number of protons.'''
    return messages(_check_constant_hnmr(cfg, tracker, section))


def check_constant_nmr(cfg: AppConfig, tracker: ErrorTracker, section, nuclei):
    '''Check for the presence of constants inside parentheses, if there is splitting.'''
    diagnostics, constants = _check_constant_nmr(cfg, tracker, section, nuclei)
    return messages(diagnostics), constants


def calculate_nmr_number_count(
    cfg: AppConfig,
    tracker: ErrorTracker,
    nmr_text,
    nuclei,
    atom,
    n,
    formula,
    hnmr_sum,
    onh_count,
):
    ''' For all atoms: check the order, count duplicates as the number of signals'''
    context = CheckContext(cfg, tracker, NucleusSpec(nuclei, atom),
                           trees_count=None, frequency_h=0,
                           atom_with_four_unique_substituents=0,
                           atom_with_two_identical_substituents=0,
                           formula_nmr=formula, e_formula=0, onh_count=onh_count,
                           c13_count_alternatives=None, carbon_groups=None,
                           c13_count_alternatives_w_groups=None, stereogenic_signal_flag=False)
    result = _calculate_nmr_number_count(nmr_text, n, hnmr_sum, context)
    return result.count, message(result.order), message(result.rounding), message(result.duplicate), message(result.precision), messages(result.hydrogen), result.ratio


def check_nmr(
    cfg: AppConfig,
    tracker: ErrorTracker,
    text,
    nuclei,
    atom,
    trees_count,
    frequency_h,
    atom_with_four_unique_substituents,
    atom_with_two_identical_substituents,
    formula_nmr,
    e_formula,
    onh_count,
    c13_count_alternatives=None,
    *,
    carbon_groups = None,
    c13_count_alternatives_w_groups = None,
    stereogenic_signal_flag=False,
    dp5_refusal_details: dict | None = None,
    observations_out: list[CarbonObservation] | None = None,
    structure_partition_complete: bool = True,
    structure_partition_reason: str | None = None,
    coincident_environments: tuple[tuple[int, ...], ...] = (),
    contains_boron: bool = False,
    configuration_status: str = "unassessed",
    policy_reason: str | None = None,
    direct_boron_groups: tuple[tuple[str, ...], ...] = (),
    carbon_scope_out: dict | None = None,
    structure_result: StructureAnalysisResult | None = None,
):
    """Validate a selected NMR section and return its eight public result slots.

    Returns warning strings, frequency, proton ratio, source section, carbon
    fraction, selected carbon groups, DP5 eligibility and prepared carbon shifts.
    Optional refusal and observation buffers receive evidence during analysis.
    """
    context = CheckContext(
        cfg, tracker, NucleusSpec(nuclei, atom), trees_count=trees_count, frequency_h=frequency_h,
        atom_with_four_unique_substituents=atom_with_four_unique_substituents,
        atom_with_two_identical_substituents=atom_with_two_identical_substituents,
        formula_nmr=formula_nmr, e_formula=e_formula, onh_count=onh_count,
        c13_count_alternatives=c13_count_alternatives, carbon_groups=carbon_groups,
        c13_count_alternatives_w_groups=c13_count_alternatives_w_groups,
        stereogenic_signal_flag=stereogenic_signal_flag,
        structure_partition_complete=structure_partition_complete,
        structure_partition_reason=structure_partition_reason,
        coincident_environments=coincident_environments, contains_boron=contains_boron,
        configuration_status=configuration_status, policy_reason=policy_reason,
        direct_boron_groups=direct_boron_groups,
        structure_result=structure_result,
        preparation=DP5Preparation(
            refusal_details={} if dp5_refusal_details is None else dp5_refusal_details,
            observations=[] if observations_out is None else observations_out,
            carbon_scope={} if carbon_scope_out is None else carbon_scope_out,
        ),
    )
    result = _check_nmr(text, context)
    preparation = result.preparation
    return messages(result.diagnostics), result.frequency, result.ratio, result.section, result.eq_c_ratio, preparation.groups, preparation.eligible, preparation.shifts
