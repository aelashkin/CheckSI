# Standard library imports
from copy import deepcopy
from dataclasses import asdict
import re
import threading

# Third-party imports
from rdkit import Chem
from molmass import Formula

# Project imports
from checksi.config_schema import AppConfig, ErrorSeverity, DeepNameAnalysis
from checksi.assessment import (
    FORMULA_NOTATION_NOT_SUPPORTED, SUPPORTED_PROTON_HEADER, UnassessedDescription, build_assessment,
    paragraph_omissions, unassessed_description,
)
from checksi.utils.text_processing import replace_dash
from checksi.utils.error_handler import ErrorTracker
from checksi.errors import ErrorCode
from checksi.errors.manager import emit_finding
from checksi.errors.findings import AnalysisNote, highlight_color, messages, scoped
from checksi.errors.run_diagnostics import RunDiagnostics
# Compatibility re-exports. Internal analysis uses the typed producers below.
from checksi.analysis.name_resolver import (
    _ResolverSession,
    smiles_and_name_from_paragraph as smiles_and_name_from_paragraph,
)
from checksi.analysis.structure_analysis import analyze_molecular_structure_detailed
from checksi.analysis.counterions import counterion_only_nmr_elements
from checksi.dp5q.assignment import AssignmentError
from checksi.dp5q.observation_policy import derive_boron_diagnostic_policy, derive_observation_policy, has_boron
from checksi.analysis.cross_checks import (
    collect_formula_sources,
    compare_name_and_formula as compare_name_and_formula,
)
from checksi.analysis.formula_isotopes import _comparison_counts
from checksi.analysis.elemental import check_elemental_analysis as check_elemental_analysis
from checksi.analysis.hrms import check_hrms as check_hrms, adjust_mass_by_hrms as adjust_mass_by_hrms
from checksi.analysis.hrms_fields import (
    NOTATION_NOT_SUPPORTED, find_ea_formula_candidate, find_hrms_formula_candidate,
    protect_ea_component_separators,
)
from checksi.analysis.nmr import check_presence_nmr_spectra as check_presence_nmr_spectra, check_nmr as check_nmr
from checksi.analysis.stereochem import check_streo_desc as check_streo_desc
from checksi.events import EventDispatcher, LogEvent, ProgressEvent, create_event_dispatcher, infer_log_severity

from checksi.analysis.name_resolver import _smiles_and_name_from_paragraph
from checksi.analysis.cross_checks import _compare_name_and_formula
from checksi.analysis.elemental import _check_elemental_analysis
from checksi.analysis.hrms import _check_hrms, _adjust_mass_by_hrms
from checksi.analysis.nmr import (
    _check_presence_nmr_spectra, _check_nmr, CheckContext, DP5Preparation,
    NUCLEUS_SPECS, PROTON, FLUORINE, BORON, PHOSPHORUS, SILICON, DEUTERIUM, NITROGEN, CARBON,
)
from checksi.analysis.stereochem import _check_streo_desc

class CheckSIController:
    """
    Encapsulates the core logic for analyzing chemical data.
    This class is UI-agnostic and returns structured data.
    """
    def __init__(
        self,
        config: AppConfig,
        *,
        event_dispatcher: EventDispatcher | None = None,
        log_callback=None,
        progress_callback=None,
    ):
        """
        Initializes the controller with a runtime-injected configuration.

        The controller is agnostic to *how* the configuration was created.
        The caller may load settings from a file, construct them in-memory
        from GUI widgets, or combine both approaches.

        Args:
            config (AppConfig): A fully validated application configuration model.
            event_dispatcher (callable, optional): Unified event sink with
                signature ``(event) -> None``.
            log_callback (callable, optional): Legacy text log callback.
            progress_callback (callable, optional): Legacy progress callback.
        """
        self.config = config
        self._dispatch = create_event_dispatcher(
            event_dispatcher,
            log_callback=log_callback,
            progress_callback=progress_callback,
        )

    def _dispatch_log(
        self,
        message: str,
        severity: ErrorSeverity = ErrorSeverity.INFO,
    ) -> None:
        self._dispatch(LogEvent(severity=severity, message=message))

    def _dispatch_progress(self, current: int, total: int, message: str = "") -> None:
        self._dispatch(ProgressEvent(current=current, total=total, message=message))

    def _legacy_log_callback(self, message: str) -> None:
        self._dispatch_log(message, severity=infer_log_severity(message))

    def _analyze_description_diagnostics(self, text, smiles, tracker: ErrorTracker):
        """
        Analyzes a single compound description text.
        """
        output = [] # List for all warnings
        real_mass = 0
        pre_real_mass = 0
        formula = None
        anal_formula = None
        mass_from_formula_anal = None
        real_formula = None
        u_c_trees_count = None
        u_f_trees_count = None
        u_si_trees_count= None 
        u_b_trees_count= None 
        u_p_trees_count= None
        atom_with_four_unique_substituents = 0
        atom_with_two_identical_substituents = 0
        onh_count = 0
        mol = None
        c13_count_alternatives = set()
        stereogenic_signal_flag = False
        carbon_groups = []
        c13_count_alternatives_w_groups = []
        dp5_refusal_details = {}
        structure_partition_complete = True
        structure_partition_reason = None
        stereo_assessment = None
        structure_result = None
        observation_policy = None
        observation_policy_reason = None
        observation_policy_message = None
        boron_diagnostic_policy = None
        contains_boron = False
        ea_details = {}
        hrms_details = {}
        carbon_observations = []
        neutral_details = {}
        # Preserve an EA-owned component separator before legacy typography
        # normalization removes bullets elsewhere in the description.
        protected = protect_ea_component_separators(text)
        text2 = replace_dash(protected)
        dash_normalized = text2
        barf_replacements = []
        offset_delta = 0
        for match in re.finditer(r"\bBArF\b", text2):
            start = match.start() + offset_delta
            barf_replacements.append({
                'before': match.group(), 'after': 'C32H12BF24',
                'input_span': [match.start(), match.end()],
                'output_span': [start, start + len('C32H12BF24')],
            })
            offset_delta += len('C32H12BF24') - len(match.group())
        text2 = re.sub(r"\bBArF\b", "C32H12BF24", text2)
        original_candidates = ((find_ea_formula_candidate(text), find_hrms_formula_candidate(text))
                               if text != text2 else (None, None))
        formula_normalization = {'normalized_description': text2}

        if smiles:
            mol = Chem.MolFromSmiles(smiles) 
            if mol:
                mol2 = Chem.AddHs(mol)
                structure_result = analyze_molecular_structure_detailed(mol2)
                u_c_trees_count = structure_result.carbon_count
                carbon_groups = structure_result.carbon_groups
                atom_with_four_unique_substituents = structure_result.stereocenter_count
                atom_with_two_identical_substituents = structure_result.one_pair_center_count
                u_f_trees_count = structure_result.fluorine_count
                u_si_trees_count = structure_result.silicon_count
                u_b_trees_count = structure_result.boron_count
                u_p_trees_count = structure_result.phosphorus_count
                c13_count_alternatives = structure_result.c13_count_alternatives
                c13_count_alternatives_w_groups = structure_result.c13_alternatives
                stereogenic_signal_flag = structure_result.stereogenic_signal_flag
                structure_partition_complete = structure_result.partition_complete
                structure_partition_reason = structure_result.partition_reason
                stereo_assessment = structure_result.stereo_assessment
                contains_boron = has_boron(mol)
                try:
                    observation_policy = derive_observation_policy(mol2, carbon_groups, structure=structure_result)
                except AssignmentError as exc:
                    observation_policy_reason = exc.reason
                    observation_policy_message = str(exc)
                if contains_boron:
                    try:
                        boron_diagnostic_policy = derive_boron_diagnostic_policy(mol, carbon_groups, structure=structure_result)
                    except AssignmentError:
                        # The ordinary source refusal remains explicit; no
                        # diagnostic count/absence is invented from a failed proof.
                        pass
        
        output, anal_formula, mass_from_formula_anal = _check_elemental_analysis(
            self.config,
            scoped(tracker, 'ea', None),
            text2,
            output,
            details=ea_details,
        )
        output, formula, pre_real_mass = _check_hrms(self.config, scoped(tracker, 'hrms', None), text2, output, details=hrms_details)
        for details, original_candidate in zip((ea_details, hrms_details), original_candidates):
            details['span_basis'] = 'normalized_description'
            candidate = details.get('candidate')
            if candidate:
                affected = any(replacement['output_span'][0] < candidate['end']
                               and replacement['output_span'][1] > candidate['start']
                               for replacement in barf_replacements)
                details['normalization'] = {}
                if (original_candidate is not None
                        and (original_candidate.raw != candidate['raw']
                             or original_candidate.charge_raw != candidate.get('charge_raw'))):
                    details['normalization']['pre_normalization_candidate'] = {
                        **original_candidate.to_dict(), 'span_basis': 'original_description',
                    }
                if affected:
                    start = candidate['start'] - sum(
                        len(item['after']) - len(item['before']) for item in barf_replacements
                        if item['output_span'][1] <= candidate['start']
                    )
                    end = candidate['end'] - sum(
                        len(item['after']) - len(item['before']) for item in barf_replacements
                        if item['output_span'][1] <= candidate['end']
                    )
                    details['normalization']['pre_expansion_candidate'] = {
                        'raw': dash_normalized[start:end],
                    }
                    details['comparison_unresolved_reason'] = (
                        'Legacy BArF abbreviation expansion does not establish a comparable chemical state.'
                    )
                if original_candidate is not None:
                    original_normalized = replace_dash(original_candidate.raw)
                    original_expanded = re.sub(r"\bBArF\b", "C32H12BF24", original_normalized)
                    if (original_candidate.raw != original_normalized
                            and original_candidate.formula is None
                            and original_expanded == candidate['raw']
                            and re.search(r'[•ꞏ](?=.+)', original_candidate.raw.rstrip('•'))):
                        details['comparison_unresolved_reason'] = (
                            'Legacy formula-token normalization removed unsupported source syntax; '
                            'the original token does not establish a comparable neutral state.'
                        )
        # Source tokens outside the supported notation stay visible as coverage gaps,
        # located in the original description when normalization shifted offsets.
        unchecked_formulas = []
        for details, original_candidate in zip((ea_details, hrms_details), original_candidates):
            candidate = details.get('candidate')
            if candidate and candidate.get('refusal') == NOTATION_NOT_SUPPORTED:
                if text == text2:
                    unchecked_formulas.append((candidate['raw'], candidate['start'], candidate['end']))
                elif original_candidate is not None:
                    unchecked_formulas.append((original_candidate.raw, original_candidate.start, original_candidate.end))
                else:
                    unchecked_formulas.append((candidate['raw'], None, None))

        if formula:
            output, real_formula, real_mass = _adjust_mass_by_hrms(
                self.config,
                scoped(tracker, 'hrms_neutral', None),
                text2,
                output,
                formula,
                pre_real_mass,
                details=neutral_details,
                original_text=text,
            )
            
        if not real_mass:
            real_mass = mass_from_formula_anal
        if not real_formula:
            real_formula = anal_formula
        
        formula_sources, formula_comparisons = collect_formula_sources(
            ea_details, hrms_details, neutral_details, mol, None,
        )
        mol_formula = None
        comparison_mass = None
        comparison_formula = None
        if smiles and mol:
            for source in ('hrms_neutral', 'ea'):
                pair = next(pair for pair in formula_comparisons if pair.left == source and pair.right == 'name')
                if pair.status in ('agree', 'disagree'):
                    comparison_mass = formula_sources[source].get('mass')
                    comparison_formula = formula_sources[source].get('formula')
                    selected = formula_sources[source]
                    if source == 'ea' and selected.get('comparison_formula') and pair.status == 'disagree':
                        comparison_formula = selected['comparison_formula']
                        comparison_mass = selected['comparison_mass']
                    source_counts = (selected.get('composition') or {}).get('counts') or {}
                    name_counts = formula_sources['name']['composition'].get('counts') or {}
                    if pair.status == 'agree' and _comparison_counts(source_counts.items()) != name_counts:
                        # Accepted solvent/counter-ion relations compare parent
                        # inventories, not masses of the unlike full states.
                        comparison_formula = None
                        comparison_mass = None
                    elif selected['composition'].get('state') == 'ion':
                        comparison_mass -= selected['composition'].get('charge', 0) * 0.000548579909065
                    break
            output, mol_formula, onh_count = _compare_name_and_formula(
                self.config,
                scoped(tracker, 'cross_check', None),
                output,
                mol,
                comparison_mass,
                comparison_formula,
            )
        if mol_formula:
          real_formula = mol_formula

        for reason in dict.fromkeys(pair.reason for pair in formula_comparisons
                                    if pair.status == 'agree' and ('EA formula includes' in pair.reason or 'counter-ion' in pair.reason)):
            if not any(isinstance(note, AnalysisNote) and note.text == reason for note in output):
                output.append(AnalysisNote(reason))

        # Name comparisons retain their existing diagnostic identity. The new
        # code owns only the independent EA/neutral-HRMS pair.
        conflicting_pairs = [pair for pair in formula_comparisons if pair.status == 'disagree'
                             and pair.left == 'ea' and pair.right == 'hrms_neutral']
        if conflicting_pairs:
            context = {
                'hrms_ion': (hrms_details.get('formula') or '') + ((hrms_details.get('candidate') or {}).get('charge_raw') or ''),
                'hrms_neutral': neutral_details.get('formula'),
                'ea_formula': ea_details.get('formula'),
            }
            warning = emit_finding(self.config, scoped(tracker, 'formula_reconciliation'), ErrorCode.FORMULA_SOURCE_MISMATCH,
                context=context, variant='parent_ion' if neutral_details.get('state') == 'ion' else 'default')
            if warning:
                output.append(warning)
        review_reasons = list(dict.fromkeys(pair.reason for pair in formula_comparisons if pair.review_required))
        if review_reasons:
            warning = emit_finding(self.config, scoped(tracker, 'formula_reconciliation'), ErrorCode.FORMULA_SOURCE_REVIEW_REQUIRED,
                                 context={'reasons': ' '.join(review_reasons)})
            if warning:
                output.append(warning)

        f_formula = p_formula = b_formula = si_formula = n_formula = d_formula = 0
        counterion_only_elements = counterion_only_nmr_elements(mol)
        if real_formula:
            parsed_formula_nmr = Formula(real_formula)
            composition_nmr = parsed_formula_nmr.composition()
            elements_in_formula_nmr = list(composition_nmr.keys())

            if "F" in elements_in_formula_nmr:
                f_formula = 1
            if "P" in elements_in_formula_nmr:
                p_formula = 1
            if "B" in elements_in_formula_nmr:
                b_formula = 1
            if "Si" in elements_in_formula_nmr:
                si_formula = 1
            if ("15N" in elements_in_formula_nmr or "N" in elements_in_formula_nmr):
                n_formula = 1
            if "2H" in elements_in_formula_nmr:
                d_formula = 1
            
            output = _check_presence_nmr_spectra(
                self.config,
                scoped(tracker, 'nmr', None),
                set(elements_in_formula_nmr) - counterion_only_elements,
                text2,
                output,
            )
        
        carbon_f_coupling_expected = int(f_formula and 'F' not in counterion_only_elements)
        # H first supplies even an invalid parsed frequency; carbon owns final preparation.
        nucleus_inputs = {
            PROTON: (0, 0),
            FLUORINE: (u_f_trees_count, f_formula),
            BORON: (u_b_trees_count, b_formula),
            PHOSPHORUS: (u_p_trees_count, p_formula),
            SILICON: (u_si_trees_count, si_formula),
            DEUTERIUM: (None, d_formula),
            NITROGEN: (None, n_formula),
            CARBON: (u_c_trees_count, carbon_f_coupling_expected),
        }
        nmr_results = {}
        for nucleus in NUCLEUS_SPECS:
            trees_count, element_flag = nucleus_inputs[nucleus]
            is_carbon = nucleus is CARBON
            context = CheckContext(
                self.config, scoped(tracker, 'nmr', nucleus.label), nucleus,
                trees_count=trees_count,
                frequency_h=0 if nucleus is PROTON else nmr_results[PROTON].frequency,
                atom_with_four_unique_substituents=atom_with_four_unique_substituents,
                atom_with_two_identical_substituents=atom_with_two_identical_substituents,
                formula_nmr=real_formula, e_formula=element_flag, onh_count=onh_count,
                c13_count_alternatives=c13_count_alternatives if is_carbon else None,
                carbon_groups=carbon_groups if is_carbon else None,
                c13_count_alternatives_w_groups=c13_count_alternatives_w_groups if is_carbon else None,
                stereogenic_signal_flag=stereogenic_signal_flag,
                structure_result=structure_result,
                structure_partition_complete=structure_partition_complete,
                structure_partition_reason=structure_partition_reason,
                coincident_environments=(observation_policy.coincident_environments if observation_policy is not None else ()),
                contains_boron=contains_boron,
                configuration_status=(observation_policy.configuration_status if observation_policy is not None else "unassessed"),
                policy_reason=observation_policy_reason,
                direct_boron_groups=(tuple(observation_policy.groups[index].labels
                    for index in observation_policy.direct_boron_environments) if observation_policy is not None else ()),
                boron_diagnostic_policy=boron_diagnostic_policy,
                preparation=(DP5Preparation(refusal_details=dp5_refusal_details, observations=carbon_observations)
                             if is_carbon else DP5Preparation()),
            )
            checked = _check_nmr(text, context)
            nmr_results[nucleus] = checked
            output.extend(checked.diagnostics)
        h_nmr, c_nmr = nmr_results[PROTON], nmr_results[CARBON]
        if observation_policy_reason and dp5_refusal_details.get('cause') == observation_policy_reason:
            dp5_refusal_details.setdefault('reason_details', {})[observation_policy_reason] = observation_policy_message

        return {
            'warnings': output,
            'hnmr_section_text': h_nmr.section,
            'cnmr_section_text': c_nmr.section,
            'atom_with_four_unique_substituents': atom_with_four_unique_substituents,
            'stereo_assessment': stereo_assessment,
            'ratio': h_nmr.ratio,
            'eq_c_ratio': c_nmr.eq_c_ratio,
            'dp5_carbon_groups': c_nmr.preparation.groups,
            'dp5_c_signals_list': c_nmr.preparation.shifts,
            'run_dp5': c_nmr.preparation.eligible,
            'dp5_refusal_details': dp5_refusal_details,
            'carbon_observations': [asdict(item) for item in carbon_observations],
            'dp5_coincident_environments': [list(group) for group in c_nmr.preparation.coincident_environments],
            'carbon_scope': deepcopy(c_nmr.preparation.carbon_scope),
            'formula_sources': formula_sources,
            'formula_comparisons': [pair.to_dict() for pair in formula_comparisons],
            'formula_normalization': formula_normalization,
            'unchecked_formulas': unchecked_formulas,
        }

    def _analyze_single_description(self, text, smiles, tracker: ErrorTracker):
        """Keep the existing description dictionary and warning-string adapter."""
        result = self._analyze_description_diagnostics(text, smiles, tracker)
        return {**result, 'warnings': messages(result['warnings']),
                'stereo_assessment': result['stereo_assessment'].to_dict() if result['stereo_assessment'] else None}

    def run_analysis(
        self,
        paragraphs,
        cancel_token: threading.Event | None = None,
        *,
        source_omissions: list[UnassessedDescription] | tuple[UnassessedDescription, ...] = (),
        _diagnostics: RunDiagnostics | None = None,
    ):
        """
        Runs the full analysis process on pre-parsed input paragraphs.

        Args:
            paragraphs (list[str]): Paragraphs extracted from an input file.
            cancel_token (threading.Event | None): Optional cancellation flag
                that can be set by the caller to request an early, graceful stop.
            source_omissions: Known unsupported extraction regions from the parser.

        Returns:
            dict: A dictionary containing the structured results of the analysis.
                Includes `aborted=True` if cancellation was requested mid-run.
        """
        diagnostics = _diagnostics if _diagnostics is not None else RunDiagnostics(self.config)
        error_tracker = diagnostics.tracker
        # Keep one occurrence owner and one lazy resolver session per run.
        resolver_session = _ResolverSession(log=self._legacy_log_callback)
        try:
            name_finding_statistics = {
                "alphanumeric_id_match_count": 0,
                "numeric_id_match_count": 0,
                "general_id_match_count": 0,
                "numeric_prefix_code_count": 0,
                "single_uppercase_id_match_count": 0,
            }
            
            analysis_results = []
            count_compounds = 0
            run_aborted = False
            processed_paragraphs = 0
            unassessed = list(source_omissions)

            # NOTE: This method intentionally performs no disk I/O.
            # Persisting or exporting results is handled by a separate service.

            # Count how many paragraphs contain compound descriptions so we can
            # report accurate progress (total is unknown until we scan).
            total_paragraphs = len(paragraphs)

            for i, text in enumerate(paragraphs):
                if cancel_token and cancel_token.is_set():
                    run_aborted = True
                    self._dispatch_log(
                        "Cancellation requested. Stopping analysis loop.",
                        severity=ErrorSeverity.WARNING,
                    )
                    self._dispatch_progress(
                        processed_paragraphs,
                        total_paragraphs,
                        "Analysis cancelled by user.",
                    )
                    break

                processed_paragraphs = i + 1
                self._dispatch_progress(
                    i + 1,
                    total_paragraphs,
                    f"Scanning paragraph {i + 1}/{total_paragraphs}",
                )

                # Check if the paragraph contains a compound description
                unassessed.extend(paragraph_omissions(text, i + 1))
                if SUPPORTED_PROTON_HEADER.search(text):
                    count_compounds += 1
                    self._dispatch_log(f"Compound #{count_compounds} found in paragraph {i + 1}")
                    smiles, words, target_text, prev_paragraph = None, None, None, ""
                    
                    compound_diagnostics = []
                    pending_diagnostics = diagnostics.begin_compound(count_compounds)
                    compound_sink = pending_diagnostics.sink(i + 1, 'description')
                    compound_result = {
                        'id': count_compounds,
                        'source_paragraph_1based': i + 1,
                        'smiles': None,
                        'name': None,
                        'identifier': None,
                        'original_text': text,
                        'all_warnings': [],
                        'dp5_carbon_groups': [],
                        'dp5_c_signals_list': None, 
                    }

                    if i > 0:
                        prev_paragraph = paragraphs[i - 1]
                        resolution_details = {}
                        pending_name_statistics = dict(name_finding_statistics)
                        smiles, words, target_text, pending_name_statistics, name_warnings = _smiles_and_name_from_paragraph(
                            self.config,
                            pending_diagnostics.sink(i, 'name'),
                            prev_paragraph,
                            pending_name_statistics,
                            log=self._legacy_log_callback,
                            cancel_token=cancel_token,
                            details=resolution_details,
                            _session=resolver_session,
                        )
                        if (resolution_details.get('status') == 'cancelled'
                                or (cancel_token is not None and cancel_token.is_set())):
                            run_aborted = True
                            count_compounds -= 1
                            self._dispatch_log("Cancellation requested during name resolution.", severity=ErrorSeverity.WARNING)
                            self._dispatch_progress(processed_paragraphs, total_paragraphs, "Analysis cancelled by user.")
                            break
                        name_finding_statistics = pending_name_statistics
                        compound_result['name_resolution'] = resolution_details
                        if smiles:
                            mol = Chem.MolFromSmiles(smiles)
                            if mol is not None:
                                smiles = Chem.MolToSmiles(mol,canonical=True,isomericSmiles=True)
                            else:
                                smiles = None
                                if resolution_details:
                                    # Keep how the shown name was isolated; drop the accepted-name edits.
                                    isolation = [change for change in resolution_details.get('transformations', [])
                                                 if change.get('kind') == 'isolate_name_segment']
                                    resolution_details.update(status='failed', reason='invalid_smiles', accepted_name=None, transformations=isolation)
                                warning = emit_finding(
                                    self.config,
                                    pending_diagnostics.sink(i, 'name'),
                                    ErrorCode.NAME_TO_SMILES_FAILED,
                                    variant="resolve_failed",
                                )
                                if warning:
                                    name_warnings.append(warning)
                        compound_diagnostics.extend(name_warnings)
                        compound_result['smiles'] = smiles
                        compound_result['name'] = words
                        compound_result['identifier'] = target_text
                    
                    # Check for Cyrillic characters
                    russian_letters = re.findall(r'[А-Яа-я]', text)
                    if russian_letters:
                        cyrillic_warning = emit_finding(
                            self.config,
                            pending_diagnostics.sink(i + 1, 'text'),
                            ErrorCode.CYRILLIC_LETTERS_FOUND,
                            context={"letters": ", ".join(sorted(set(russian_letters)))},
                        )
                        if cyrillic_warning:
                            compound_diagnostics.append(cyrillic_warning)

                    # Run the core analysis for the description
                    description_analysis = self._analyze_description_diagnostics(
                        text,
                        smiles,
                        compound_sink,
                    )
                    compound_diagnostics.extend(description_analysis['warnings'])
                    for excerpt, start, end in description_analysis['unchecked_formulas']:
                        location = {"kind": "paragraph", "paragraph_1based": i + 1}
                        if start is not None:
                            location.update(start_offset=start, end_offset=end)
                        unassessed.append(unassessed_description(excerpt, location, FORMULA_NOTATION_NOT_SUPPORTED))

                    #Carbon groups and 13C NMR signal lists for DP5 probability calculation
                    if description_analysis['dp5_carbon_groups']:
                        compound_result['dp5_carbon_groups'] = description_analysis['dp5_carbon_groups']
                    if description_analysis['dp5_c_signals_list']:
                        compound_result['dp5_c_signals_list'] = description_analysis['dp5_c_signals_list']
                    compound_result['run_dp5'] = description_analysis['run_dp5']
                    compound_result['dp5_refusal_details'] = description_analysis['dp5_refusal_details']
                    compound_result['formula_sources'] = description_analysis['formula_sources']
                    compound_result['carbon_observations'] = description_analysis['carbon_observations']
                    compound_result['dp5_coincident_environments'] = deepcopy(description_analysis['dp5_coincident_environments'])
                    compound_result['carbon_scope'] = deepcopy(description_analysis['carbon_scope'])
                    compound_result['formula_comparisons'] = description_analysis['formula_comparisons']
                    compound_result['formula_normalization'] = description_analysis['formula_normalization']
                    compound_result['stereo_assessment'] = (
                        description_analysis['stereo_assessment'].to_dict()
                        if description_analysis['stereo_assessment'] else None)
                    # Add stereochemistry warnings
                    if words and target_text:
                        compound_diagnostics.insert(0, AnalysisNote(f"parsed name: {words} ({target_text})"))
                        stereochem_warnings = _check_streo_desc(
                            self.config,
                            pending_diagnostics.sink(i, 'stereo'),
                            words,
                            description_analysis['eq_c_ratio'],
                            description_analysis['ratio'],
                            description_analysis['atom_with_four_unique_substituents'],
                            smiles,
                            stereo_assessment=description_analysis['stereo_assessment'],
                        )
                        compound_diagnostics.extend(stereochem_warnings)
                    else:
                        refused = compound_result.get('name_resolution', {}).get('status') == 'refused'
                        name_not_found_warning = emit_finding(
                            self.config,
                            pending_diagnostics.sink(i + 1, 'name'),
                            ErrorCode.NAME_NOT_FOUND,
                            variant="not_isolated" if refused else "default",
                        )
                        if name_not_found_warning:
                            compound_diagnostics.insert(0, name_not_found_warning)

                    fallback = ('yellow' if self.config.modes.deep_name_analysis == DeepNameAnalysis.OFF
                                and not smiles else 'blue')
                    color = highlight_color(compound_diagnostics, fallback=fallback)
                    compound_result['all_warnings'] = messages(compound_diagnostics)

                    compound_result['highlight_info'] = {
                        'color': color,
                        'name_text': f"{words} ({target_text})" if words and target_text else None,
                        'hnmr_text': description_analysis['hnmr_section_text'],
                        'cnmr_text': description_analysis['cnmr_section_text'],
                    }
                    
                    pending_diagnostics.commit(compound_diagnostics)
                    analysis_results.append(compound_result)
            
            diagnostics.complete_analysis()
            return {
                'compounds': analysis_results,
                'name_finding_stats': name_finding_statistics,
                'total_found': count_compounds,
                'error_stats': error_tracker.to_dict(),
                'assessment': build_assessment(count_compounds, unassessed, scan_complete=not run_aborted),
                'aborted': run_aborted,
                'processed_paragraphs': processed_paragraphs,
                'total_paragraphs': total_paragraphs,
            }
        finally:
            resolver_session.close()
