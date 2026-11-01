# src/checksi/analysis/cross_checks.py

from dataclasses import asdict, dataclass
from itertools import combinations
from fractions import Fraction
import re

# Third-party imports
from molmass import Formula, FormulaError
from rdkit import Chem
from rdkit.Chem import rdMolDescriptors

# Project imports
from checksi.config_schema import AppConfig, OutputMode
from checksi.errors import ErrorCode
from checksi.errors.manager import emit_finding
from checksi.errors.findings import AnalysisNote, messages, FindingSink
from checksi.analysis.formula_isotopes import _NATURAL_ISOTOPE_ELEMENTS, _comparison_counts, _comparison_formula
from checksi.analysis.formula_source_policy import (
    _NEUTRAL_COMPONENTS, counterion_disagreement, explain_formula_difference,
    listed_component_feasibility, match_counterion, parse_ea_formula,
)
from checksi.analysis.formula_utils import get_isotopic_masses
from checksi.analysis.structure_analysis import count_exchangeable_protons
from checksi.analysis.mass_spec_utils import find_elemental_combinations_for_mass, format_combination
from checksi.utils.error_handler import ErrorTracker


@dataclass(frozen=True)
class SourceComposition:
    """Formula evidence on an explicit state basis, separate from diagnostics."""

    source: str
    raw_formula: str | None
    formula: str | None
    counts: tuple[tuple[str, int | Fraction], ...] | None
    usable: bool
    state: str
    basis: str
    reason: str
    derivation: str | None
    span_basis: str | None
    span: tuple[int, int] | None
    review_required: bool = False
    charge: int | None = 0

    def to_dict(self) -> dict:
        """Return JSON-safe isotope counts and input-relative spans."""
        result = asdict(self)
        result['counts'] = ({key: int(value) if Fraction(value).denominator == 1 else float(value)
                             for key, value in self.counts} if self.counts is not None else None)
        result['span'] = list(self.span) if self.span is not None else None
        return result


@dataclass(frozen=True)
class CompositionComparison:
    """One independently inspectable relationship between neutral-source peers."""

    left: str
    right: str
    status: str
    reason: str
    review_required: bool = False

    def to_dict(self) -> dict:
        """Return a JSON-safe relationship without diagnostic policy."""
        return asdict(self)


def source_composition(
    source: str, details: dict, *, state: str, basis: str, reason: str,
    derivation: str | None = None, review_required: bool = False,
    charge: int | None = 0,
) -> SourceComposition:
    """Retain complete usable atom counts without changing the source formula."""
    candidate = details.get('candidate') or {}
    formula = details.get('formula')
    counts = None
    if details.get('usable') and formula:
        try:
            policy = parse_ea_formula(formula) if source == 'ea' else None
            if policy is not None and policy.status == 'accepted':
                counts = tuple(sorted(policy.full_counts.items()))
            else:
                counts = tuple(sorted((key, entry.count) for key, entry in Formula(formula).composition().items()
                                      if key != 'e-'))
        except (FormulaError, ValueError, OverflowError):
            reason = 'The complete source composition is unavailable.'
    elif not review_required:
        state = 'unknown'
        reason = candidate.get('reason') or f"Complete {source} composition is unavailable ({details.get('status', 'unavailable')})."
    return SourceComposition(
        source, candidate.get('raw', formula), formula, counts, counts is not None,
        state, basis, reason, derivation, details.get('span_basis'),
        (candidate['start'], candidate['end']) if candidate else None, review_required, charge,
    )


def _counts_for_source(source: SourceComposition) -> dict:
    return dict(source.counts) if source.source == 'name' else _comparison_counts(source.counts)


def _isotope_review(left: SourceComposition, right: SourceComposition) -> str | None:
    for source in (left, right):
        if source.source == 'name' and any(key in _NATURAL_ISOTOPE_ELEMENTS for key, _ in source.counts):
            return 'An isotope label in the accepted name denotes isotopic modification, not a natural calculated-peak label.'
    usual_labels = {'2H', '13C', '15N', '17O', '18O'}
    if left.counts != right.counts and any(key[0].isdigit() and key not in _NATURAL_ISOTOPE_ELEMENTS
            and key not in usual_labels for source in (left, right) for key, _ in source.counts):
        return 'The isotope-label relationship is outside the adjudicated natural/enrichment policy.'
    return None


def _component_text(components) -> str:
    return ' + '.join(
        f'{str(part.amount.numerator) if part.amount.denominator == 1 else format(float(part.amount), "g")} {part.name}'
        for part in components
    )


def _component_relationship(left: SourceComposition, right: SourceComposition):
    """Qualify full EA/name inventories against parents without deleting source atoms."""
    if left.source == 'ea':
        full, parent = left, right
    elif right.source == 'ea':
        full, parent = right, left
    elif left.state in ('ion', 'ionic') and right.state == 'disconnected':
        full, parent = right, left
    elif right.state in ('ion', 'ionic') and left.state == 'disconnected':
        full, parent = left, right
    else:
        return None
    if full.state == 'unknown' or parent.state == 'unknown':
        return None
    full_counts, parent_counts = _counts_for_source(full), _counts_for_source(parent)
    if parent.state in ('ion', 'ionic'):
        component = match_counterion(full_counts, parent_counts, parent.charge)
        if component:
            return 'agree', f'{"EA formula" if full.source == "ea" else "Accepted structure"} includes {_component_text((component,))} counter-ion; the cation charge is balanced.', False
        if parent.source == 'hrms_neutral' and counterion_disagreement(full_counts, parent_counts, parent.charge):
            return 'disagree', 'The complete formula cannot supply the stated parent-ion inventory with a balanced listed counter-ion.', False
        return 'not_comparable', 'A charged parent and complete formula do not establish a supported charge-balanced counter-ion relation.', True
    if full.charge or parent.charge:
        return None
    policy = parse_ea_formula(full.raw_formula or full.formula)
    if (policy.status == 'accepted' and policy.explicit_components and policy.comparable_parent is not None
            and all(component.name in _NEUTRAL_COMPONENTS for component in policy.explicit_components)
            and parent.state == 'neutral'):
        if _comparison_counts(policy.comparable_parent.items()) == parent_counts:
            return 'agree', f'EA formula includes {_component_text(policy.explicit_components)}; its explicitly declared parent agrees and full-formula percentages remain checked.', False
        return 'disagree', 'The explicitly declared EA parent differs from this complete parent inventory.', False
    if (policy.status == 'accepted' and policy.explicit_components and policy.comparable_parent is not None
            and parent.state == 'neutral'
            and _comparison_counts(policy.comparable_parent.items()) == parent_counts):
        return 'not_comparable', 'The declared EA parent has matching atoms, but its counter-ion relationship to the neutral reference is unresolved.', True
    if full_counts == parent_counts:
        return 'agree', 'The complete atom inventories agree.', False
    # A legacy normalization does not establish a chemical component relation.
    if 'Legacy' in full.reason and 'BArF' not in full.reason:
        return None
    declared = policy.explicit_components if policy.status == 'accepted' else ()
    if policy.status == 'accepted' and not declared:
        matching = next((option for option in policy.parent_options
                         if _comparison_counts(option.parent_counts.items()) == parent_counts), None)
        declared = matching.components if matching else ()
    relation = explain_formula_difference(full_counts, parent_counts, declared_components=declared)
    if relation.recognized:
        explanations = ' or '.join(_component_text(option) for option in relation.options)
        return 'agree', f'EA formula includes components consistent with {explanations}; the full declaration is used for EA percentages.', False
    if policy.status == 'accepted' and any(value.denominator != 1 for value in policy.full_counts.values()):
        return 'not_comparable', 'The fractional EA parent is not established by this reference; full-formula percentages remain checked.', True
    if full.state not in ('neutral', 'composite') or parent.state != 'neutral':
        return None
    feasibility = listed_component_feasibility(full_counts, parent_counts)
    if feasibility.status == 'feasible':
        return 'not_comparable', f'An exact whole-unit explanation of the complete formula difference is {_component_text(feasibility.components)}; it is outside the accepted folded-inference bound and review is pending.', True
    if feasibility.status == 'unknown':
        reason = ('The whole-component search reached its bounded work limit without establishing a complete explanation.'
                  if feasibility.reason == 'work_limit' else 'The whole-component search could not validate an exact candidate.')
        return 'not_comparable', reason + ' No component relationship was certified; review is pending.', True
    if full.state == 'composite' and parent.state == 'neutral':
        return 'disagree', 'The complete composite EA inventory does not match the stated parent or a supported complete component relationship.', False
    return 'disagree', 'The atom inventories differ and exact whole-unit constraints exclude a complete listed-component explanation; this does not identify which source is wrong.', False


def _qualified_ion_relationship(left: SourceComposition, right: SourceComposition):
    """Compare established ion inventories without guessing protonation states."""
    ionic = ('ion', 'ionic')
    if left.state in ionic and right.state in ionic and left.charge and left.charge == right.charge:
        agree = _counts_for_source(left) == _counts_for_source(right)
        return ('agree' if agree else 'disagree',
                'The ion inventories and charges agree.' if agree else 'The complete atom inventories differ for the same stated ion charge.', False)
    parent, name = (left, right) if left.source == 'hrms_neutral' else (right, left)
    if (parent.source == 'hrms_neutral' and parent.state == 'ion' and name.source == 'name'
            and parent.charge is not None and name.charge is not None
            and (name.state == 'neutral' or (name.state == 'ionic' and name.charge))):
        parent_counts, name_counts = _counts_for_source(parent), _counts_for_source(name)
        delta = {key: parent_counts.get(key, 0) - name_counts.get(key, 0)
                 for key in parent_counts.keys() | name_counts.keys()}
        difference = {key: value for key, value in delta.items() if value}
        if difference == {'H': parent.charge - name.charge} and parent.charge != name.charge:
            return 'not_comparable', 'The parent ion and accepted name differ only by hydrogen atoms corresponding to their charge difference; the protonation relationship is pending.', True
        if difference:
            return 'disagree', 'The accepted name cannot supply the complete stated parent-ion atom inventory.', False
    return None


def _source_label(source: SourceComposition) -> str:
    return 'HRMS parent ion' if source.source == 'hrms_neutral' and source.state == 'ion' else source.source


def reconcile_compositions(sources: list[SourceComposition]) -> list[CompositionComparison]:
    """Compare complete counts only on the common neutral basis, without edits."""
    result = []
    for left, right in combinations(sources, 2):
        review = left.review_required or right.review_required
        if ((not left.usable and not left.review_required)
                or (not right.usable and not right.review_required)):
            status = 'unavailable'
            reason = 'At least one complete source composition is unavailable.'
            review = False
        elif left.usable and right.usable and (isotope_reason := _isotope_review(left, right)):
            status, reason, review = 'not_comparable', isotope_reason, True
        elif left.usable and right.usable and (relation := _component_relationship(left, right)):
            status, reason, review = relation
        elif left.usable and right.usable and (relation := _qualified_ion_relationship(left, right)):
            status, reason, review = relation
        elif not left.usable or not right.usable or left.state != 'neutral' or right.state != 'neutral':
            status = 'not_comparable'
            pending = [source for source in (left, right) if source.review_required or not source.usable
                       or source.state == 'unknown']
            reason = (' '.join(f'{_source_label(source)}: {source.reason}' for source in pending)
                      or 'The sources do not share a qualified chemical-state basis.')
        elif _counts_for_source(left) == _counts_for_source(right):
            status = 'agree'
            reason = 'Neutral atom counts agree after comparison normalization of natural-isotope peak labels.'
        else:
            status = 'disagree'
            reason = 'The complete atom counts differ on the neutral basis; this does not identify which source is wrong.'
        result.append(CompositionComparison(left.source, right.source, status, reason, review))
    return result


def structure_formula_state(molecule) -> tuple[str, str]:
    """Describe explicit structure state without deleting components or charges."""
    if molecule is None:
        return 'unknown', 'Accepted name-derived structure is unavailable.'
    if len(Chem.GetMolFrags(molecule)) != 1:
        return 'disconnected', 'The accepted structure is disconnected; components were not stripped.'
    charged = [atom for atom in molecule.GetAtoms() if atom.GetFormalCharge()]
    local_charge_pairs = all(any(atom.GetFormalCharge() * neighbor.GetFormalCharge() < 0
                                 for neighbor in atom.GetNeighbors()) for atom in charged)
    if Chem.GetFormalCharge(molecule) != 0 or not local_charge_pairs:
        return 'ionic', 'The accepted structure is charged or has separated zwitterionic centers.'
    return 'neutral', 'Accepted connected net-neutral structure, including local charge-separated valence groups.'


def collect_formula_sources(
    ea: dict, hrms: dict, neutral: dict, molecule, name_formula: str | None,
) -> tuple[dict[str, dict], list[CompositionComparison]]:
    """Retain independent stage evidence and qualify only narrow neutral peers."""
    if molecule is not None and name_formula is None:
        name_formula = _structure_formula(molecule)
    ea_candidate = ea.get('candidate') or {}
    ea_transform_reason = ea.get('comparison_unresolved_reason')
    ea_policy = parse_ea_formula(ea.get('formula') or '')
    ea_composite = ea_policy.status == 'accepted' and bool(ea_policy.explicit_components or any(
        count.denominator != 1 for count in ea_policy.full_counts.values()))
    ea_state_unresolved = bool(ea.get('usable') and (ea_transform_reason or ea_candidate.get('charge')
        or (not ea_composite and re.search(r'[.·()]', ea_candidate.get('raw', '')))))
    ea_reason = ea_transform_reason or (
        'Explicit charge or joined/grouped formula state requires review.' if ea_state_unresolved
        else 'Complete simple EA declaration; measured percentages are evaluated separately.'
    )
    ea_record = source_composition(
        'ea', ea, state='unknown' if ea_state_unresolved else 'composite' if ea_composite else 'neutral',
        basis='simple_ea_declaration', review_required=ea_state_unresolved,
        reason=ea_reason,
    )
    ion_record = source_composition(
        'hrms', hrms, state='ion', basis='declared_hrms_ion',
        reason='Original ion evidence is retained separately from neutral-source comparisons.',
        charge=hrms.get('charge'),
    )
    for key, value in {'source': 'hrms_neutral', 'status': 'unavailable', 'formula': None, 'mass': None, 'usable': False}.items():
        neutral.setdefault(key, value)
    neutral['candidate'] = hrms.get('candidate')
    neutral['span_basis'] = hrms.get('span_basis')
    neutral['normalization'] = hrms.get('normalization', {})
    hrms_transform_reason = hrms.get('comparison_unresolved_reason')
    if hrms_transform_reason:
        neutral.update(comparable=False, state='unknown', reason=hrms_transform_reason)
    neutral_record = source_composition(
        'hrms_neutral', neutral, state='unknown' if hrms_transform_reason else neutral.get('state', 'unknown'), basis='inverse_hrms_adduct',
        reason=hrms_transform_reason or neutral.get('reason', 'No usable HRMS ion composition was available for neutral recovery.'),
        derivation=neutral.get('derivation'),
        charge=neutral.get('charge', 0),
        review_required=bool(hrms.get('usable') and neutral.get('status') != 'invalid'
                             and (neutral.get('usable') or ea.get('usable') or name_formula)
                             and neutral.get('review_required', True)
                             and (hrms_transform_reason or not neutral.get('comparable'))),
    )
    name_state, name_reason = structure_formula_state(molecule)
    name_review = name_state in ('disconnected', 'ionic')
    name = {'source': 'name', 'status': 'valid' if name_formula else 'unavailable',
            'formula': name_formula, 'usable': bool(name_formula), 'span_basis': 'accepted_structure'}
    name_record = source_composition(
        'name', name, state=name_state, basis='accepted_structure',
        reason=name_reason if name_formula else 'Accepted name-derived structure is unavailable.',
        review_required=name_review, charge=Chem.GetFormalCharge(molecule) if molecule is not None else 0,
    )
    sources = {'ea': ea, 'hrms': hrms, 'hrms_neutral': neutral, 'name': name}
    for source, record in zip(sources.values(), (ea_record, ion_record, neutral_record, name_record)):
        source['composition'] = record.to_dict()
    return sources, reconcile_compositions([ea_record, neutral_record, name_record])


def _structure_formula(molecule) -> str:
    """Use the existing isotope-preserving structure formula spelling."""
    return rdMolDescriptors.CalcMolFormula(molecule, separateIsotopes=True, abbreviateHIsotopes=True)


def _compare_name_and_formula(
    config: AppConfig,
    tracker: ErrorTracker | FindingSink,
    output,
    molecule,
    instrument_mass,
    instrument_formula,
):
    """Typed implementation behind `compare_name_and_formula`."""
    name_formula = None
    exchangeable_proton_count = 0
    
    if not molecule:
        return output, name_formula, exchangeable_proton_count

    # Calculate properties from the name-derived molecule
    exchangeable_proton_count = count_exchangeable_protons(molecule)
    name_formula = _structure_formula(molecule)
    name_mass = Formula(name_formula).monoisotopic_mass

    if config.modes.output_mode == OutputMode.DETAILED:
        output.append(AnalysisNote(f"Formula from SMILES: {name_formula} (Mass: {name_mass:.4f})"))
    
    if instrument_mass and name_mass:
        mass_difference = abs(instrument_mass - name_mass)
        composition_disagrees = False
        name_counts = tuple((key, item.count) for key, item in Formula(name_formula).composition().items() if key != 'e-')
        if any(key in _NATURAL_ISOTOPE_ELEMENTS for key, _ in name_counts):
            notice = emit_finding(config, tracker, ErrorCode.FORMULA_SOURCE_REVIEW_REQUIRED,
                context={'reasons': 'The accepted name specifies isotopic modification; natural calculated-peak labels do not establish that modification.'})
            if notice:
                output.append(notice)
            return output, name_formula, exchangeable_proton_count
        natural_peak_match = False
        if instrument_formula:
            try:
                instrument_counts = tuple((key, item.count) for key, item in Formula(instrument_formula).composition().items() if key != 'e-')
                composition_disagrees = _comparison_counts(name_counts) != _comparison_counts(instrument_counts)
                if not composition_disagrees and any(key in _NATURAL_ISOTOPE_ELEMENTS for key, _ in instrument_counts):
                    natural_peak_match = abs(instrument_mass - Formula(instrument_formula).isotope.mass) <= 0.0001
            except (FormulaError, ValueError, OverflowError):
                pass  # Legacy direct callers can supply mass without a parseable formula.

        if composition_disagrees or mass_difference > 0.0001:
            # The simple masses don't match; check isotopic variants
            comparison_formula = _comparison_formula(name_formula)
            isotopic_mass_variants = get_isotopic_masses(comparison_formula)
            match_found = natural_peak_match
            if isotopic_mass_variants and not composition_disagrees:
                for iso_formula, iso_mass in isotopic_mass_variants:
                    if abs(instrument_mass - iso_mass) <= 0.0001:
                        if config.modes.output_mode == OutputMode.DETAILED:
                            output.append(AnalysisNote(f"Mass match found with isotopic variant. Name: {iso_formula}, Instrument: {instrument_formula}."))
                        match_found = True
                        break
            
            if not match_found:
                info = emit_finding(
                    config,
                    tracker,
                    ErrorCode.NAME_FORMULA_MISMATCH,
                    context={
                        "name_formula": name_formula,
                        "name_mass": name_mass,
                        "instrument_formula": instrument_formula,
                        "instrument_mass": instrument_mass,
                    },
                )
                if info:
                    output.append(info)

                # Try to explain the mass difference
                if mass_difference > 0.0002:
                    combinations = find_elemental_combinations_for_mass(instrument_mass - name_mass)
                    if combinations:
                        combo_str = "; ".join([format_combination(c) for c in combinations])
                        mass_diff_warning = emit_finding(
                            config,
                            tracker,
                            ErrorCode.NAME_FORMULA_MASS_DELTA,
                            context={"mass_difference": mass_difference, "combinations": combo_str},
                            variant="with_combinations",
                        )
                        if mass_diff_warning:
                            output.append(mass_diff_warning)
                    else:
                        mass_diff_warning = emit_finding(
                            config,
                            tracker,
                            ErrorCode.NAME_FORMULA_MASS_DELTA,
                            context={"mass_difference": mass_difference},
                            variant="without_combinations",
                        )
                        if mass_diff_warning:
                            output.append(mass_diff_warning)
        else:
            if config.modes.output_mode == OutputMode.DETAILED:
                output.append(AnalysisNote(f"Mass match between name and instrument verified. Name: {name_formula}, Instrument: {instrument_formula}."))

    return output, name_formula, exchangeable_proton_count


def compare_name_and_formula(
    config: AppConfig,
    tracker: ErrorTracker,
    output,
    molecule,
    instrument_mass,
    instrument_formula,
):
    """
    Compares a molecule (from a name) with a formula (from HRMS/EA) via their masses.
    Also calculates the number of exchangeable protons.

    Args:
        config: The application configuration object.
        output: The list of report lines to append warnings to.
        molecule: An RDKit molecule object.
        instrument_mass: The monoisotopic mass from HRMS or elemental analysis.
        instrument_formula: The formula string from HRMS or elemental analysis.

    Returns:
        A tuple containing:
        - output (list): The updated list of report lines.
        - name_formula (str or None): The formula derived from the name.
        - exchangeable_proton_count (int): The number of O-H/N-H protons.
    """
    diagnostics = []
    _, first, second = _compare_name_and_formula(config, tracker, diagnostics, molecule, instrument_mass, instrument_formula)
    output.extend(messages(diagnostics))
    return output, first, second
