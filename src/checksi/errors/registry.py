"""Centralized message templates keyed by error code and variant."""

from checksi.errors.defs import AnalysisErrorCode


ERROR_TEMPLATES = {
    AnalysisErrorCode.STEREO_CHECK_REQUIRED: {
        "structural_meso": (
            "The fully specified supported structure is meso. Verify that the name's absolute descriptors "
            "describe this structure."
        ),
        "structural_non_meso": (
            "The fully specified supported structure is non-meso. Verify whether the sample is enantiopure "
            "or a single diastereomer and whether supporting chiral data is provided."
        ),
        "meso_possible": "A meso form exists among supported stereoisomers. The unspecified sample is not confirmed meso; verify its configuration.",
        "no_meso_form": "No meso form exists among supported stereoisomers. The sample configuration is unspecified; verify the stated absolute descriptors.",
        "no_stereocenters": "The supported structure has no relevant stereocenters and is not meso. Verify the stated stereodescriptor.",
        "structural_unassessed": "Verify whether the compound is enantiopure, meso, or a single diastereomer. Structural meso assessment is unassessed ({structure_reason}); verify manually.",
        "generic_check": (
            "Verify whether the compound is enantiopure, meso, or a single diastereomer. If the "
            "compound is chiral, ensure that chiral HPLC or optical rotation data is provided. "
            "If it is a single diastereomer, change the description type to RS/SR."
        ),
        "relative_descriptor": "Relative stereodescriptors are indicated, such as R*. Verify manually that they are used correctly.",
    },
    AnalysisErrorCode.STEREO_DESCRIPTOR_INVALID: {
        "unrecognized": "There is an unrecognized stereodescriptor.",
        "invalid_type": (
            "Probably an invalid stereodescription type. For one stereocenter: (R). For two or more: "
            "(1RS,2SR), (1R,2S), or (1R*,2S*)."
        ),
        "too_few_stereocenters": (
            "The supported structure has {stereocenter_count} atom R/S stereocenters. "
            "Check the use of a stereodescription for two or more centers."
        ),
        "can_be_omitted": "Stereodescription can be omitted.",
        "structural_meso_descriptor": (
            "The specified supported structure is meso. Use absolute descriptors such as (1R,2S), "
            "rather than describing an enantiomeric pair."
        ),
        "descriptor_type_incorrect": (
            "The type of stereodescriptor may be incorrect. Expected formats: (1RS,2SR) or "
            "(1R,2S) or (1R*,2S*)."
        ),
        "missing_descriptors": (
            "The supported structure has {stereocenter_count} atom R/S stereocenters, but no stereodescriptors "
            "were found. Check whether the intended configuration should be stated."
        ),
    },
    AnalysisErrorCode.STEREO_DESCRIPTOR_INCONSISTENT: {
        "mixed_types": "Probably an invalid stereodescription type. Different types are used in one description.",
    },
    AnalysisErrorCode.FORMULA_INVALID_FORMAT: {
        "default": "Invalid formula format: '{molecular_formula}'.",
        # Definite rule violations in a complete source token; other refusals are not findings.
        "unknown_element": "Invalid {source} formula '{molecular_formula}': unknown element symbol '{detail}'.",
        "zero_count": "Invalid {source} formula '{molecular_formula}': zero atom count '{detail}'.",
        "non_integer_count": "Invalid {source} formula '{molecular_formula}': atom count '{detail}' is not a whole number.",
        "unbalanced_brackets": "Invalid {source} formula '{molecular_formula}': unbalanced bracket '{detail}'.",
    },
    AnalysisErrorCode.FORMULA_ATOM_SYMBOL_INVALID: {
        "default": "Invalid atom symbol or formula syntax in '{formula_string}'.",
    },
    AnalysisErrorCode.HRMS_FORMULA_MISSING: {
        "default": "No formula was found in the HRMS data.",
    },
    AnalysisErrorCode.EA_NUMBER_FORMAT_INVALID: {
        "malformed_entry": (
            "Malformed entry in {context} of elemental analysis: '{entry}'. "
            "Expected an element symbol followed by a percentage."
        ),
        "invalid_number_format": (
            "Invalid number format in {context} of elemental analysis: '{percentage_str}' "
            "(should be XX.XX)."
        ),
        "non_numeric_value": "Skipping non-numeric value for {atom_symbol}: {percentage_str}.",
        "duplicate_element": (
            "Element {atom_symbol} occurs more than once in {context} of elemental analysis; "
            "no unique percentage can be used."
        ),
        "missing_value": "Missing percentage for {atom_symbol} in {context} of elemental analysis.",
        "out_of_range_value": (
            "Percentage for {atom_symbol} in {context} of elemental analysis is outside 0–100%: "
            "{percentage_str}."
        ),
    },
    AnalysisErrorCode.EA_CALCD_FOUND_ELEMENT_MISMATCH: {
        "missing_in_found": (
            "Element '{element}' is present in the calculated composition but is missing in the found "
            "composition or has an incorrect format."
        ),
        "missing_in_calculated": (
            "Element '{element}' is present in the found composition but is missing in the calculated "
            "composition or has an incorrect format."
        ),
    },
    AnalysisErrorCode.EA_FORMULA_CALCD_MISMATCH: {
        "formula_vs_calculated": (
            "Mismatch between the formula and the calculated composition. Discrepancy for {element} - "
            "from the formula: {formula_percent_value:.2f}%, calculated: {calculated_percent_value:.2f}% "
            "(delta={delta:.2f}%). Check the formula or the calculated composition."
        ),
        "element_not_in_formula": "Element {element} was not found in the composition from formula.",
    },
    AnalysisErrorCode.REQUIRED_NMR_MISSING: {
        "missing_nmr": "{element} found in HRMS or EA data, but no {element} NMR data was found in the text.",
        "parse_error": "Error parsing formula: {details}",
        "unexpected_error": "Unexpected error: {details}",
    },
    AnalysisErrorCode.NAME_TO_SMILES_FAILED: {
        "parsed_name": "Parsed name: '{resolved_name}' ({identifier})",
        "resolve_failed": "Could not resolve name to SMILES. Please check the name and experimental data manually.",
    },
    AnalysisErrorCode.NMR_FREQUENCY_INVALID: {
        "default": "The {nuclei} NMR frequency {details}",
    },
    AnalysisErrorCode.NMR_FREQUENCY_MISMATCH: {
        "default": "{details}",
    },
    AnalysisErrorCode.SOLVENT_PROTON_PEAK_FOUND: {
        "default": "Found a 1H peak for {name}: {peak}",
    },
    AnalysisErrorCode.SOLVENT_CARBON_PEAK_FOUND: {
        "default": "Found a 13C peak for {name}: {peak}",
    },
    AnalysisErrorCode.SOLVENT_NOT_FOUND: {
        "default": "{details}",
    },
    AnalysisErrorCode.NAME_MODIFIED_FOR_SMILES: {
        "default": "Name was modified to '{resolved_name}' to obtain SMILES. Please verify the original name.",
    },
    AnalysisErrorCode.HRMS_ADDUCT_MISMATCH: {
        "m_plus_na_but_no_na": "Warning: Found M + Na, but Na is not in the molecular formula.",
        "formula_has_na_but_not_ion": "Warning: The molecular formula contains Na, but M + Na was not found.",
        "m_plus_k_but_no_k": "Warning: Found M + K, but K is not in the molecular formula.",
        "formula_has_k_but_not_ion": "Warning: The molecular formula contains K, but M + K was not found.",
    },
    AnalysisErrorCode.HRMS_ION_BALANCE_INVALID: {
        "element_not_found": "Element '{elem}' not found in HRMS formula.",
        "negative_quantity": "Quantity of element '{elem}' in HRMS formula became negative. Check ion description.",
    },
    AnalysisErrorCode.HALOGEN_ISOTOPE_UNSPECIFIED: {
        "chlorine": "Formula contains Cl, but isotope is not specified (e.g., [35Cl] or [37Cl]).",
        "bromine": "Formula contains Br, but isotope is not specified (e.g., [79Br] or [81Br]).",
    },
    AnalysisErrorCode.H_COUNT_MISMATCH: {
        "with_exchange": (
            "The H number in 1H NMR and in formula from name/HRMS/EA does not match. In 1H NMR "
            "({hnmr_sum}H, potentially exchangeable {exchange_count}H) and in formula from name/HRMS/EA ({hrms_sum}H)."
        ),
        "without_exchange": (
            "The H number in 1H NMR and in formula from name/HRMS/EA does not match. In 1H NMR "
            "({hnmr_sum}H) and in formula from name/HRMS/EA ({hrms_sum}H)."
        ),
    },
    AnalysisErrorCode.H_COUNT_EXCHANGEABLE_OK: {
        "default": (
            "Number of H in 1H NMR matches if {exchange_count}H are exchangeable protons. Make sure "
            "that the missing signal in the spectrum is an exchangeable proton (NH/OH) and not "
            "something else. In 1H NMR {hnmr_sum}H, in formula from name/HRMS/EA {hrms_sum}H."
        ),
    },
    AnalysisErrorCode.H_COUNT_RATIO_CHECK: {
        "with_context": (
            "Ratio should be 1 : {ratio:.2f} (using 1H NMR {hnmr_sum}H and formula from "
            "name/HRMS/EA {hrms_sum}H). If some exchangeable protons are missing in 1H NMR, "
            "the ratio may differ."
        ),
        "simple": "Ratio should be 1 : {ratio:.2f}",
    },
    AnalysisErrorCode.HNMR_J_MISSING: {
        "default": (
            "The following brackets are missing the pattern 'J = number Hz' or are in wrong "
            "format in 1H NMR: {brackets}."
        ),
    },
    AnalysisErrorCode.HNMR_H_COUNT_MISSING: {
        "default": (
            "The following brackets are missing a number before 'H' or are in wrong format in "
            "1H NMR: {brackets}."
        ),
    },
    AnalysisErrorCode.H_COUNT_NOT_INTEGER: {
        "default": (
            "Non-integer proton counts detected {non_integer_values}; check whether the sample "
            "is a pure compound and not a mixture."
        ),
    },
    AnalysisErrorCode.HNMR_MULTIPLICITY_MISSING: {
        "default": "The following brackets are missing multiplicity in 1H NMR: {brackets}.",
    },
    AnalysisErrorCode.HNMR_SIGNAL_DESC_INVALID: {
        "default": (
            "The following brackets have an incorrect order of elements "
            "(multiplicity, J, proton count) in 1H NMR: {brackets}."
        ),
    },
    AnalysisErrorCode.HNMR_WRONG_MULTIPLICITY: {
        "default": "The following brackets have invalid multiplicity in 1H NMR: {brackets}.",
    },
    AnalysisErrorCode.ISOTOPE_FORMAT_INVALID: {
        "default": "Incorrect isotope format: '{incorrect}'. Expected '{correct}'.",
    },
    AnalysisErrorCode.FORMULA_NOT_FOUND: {
        "default": "Molecular formula was not found in HRMS/elemental analysis, or no HRMS/elemental analysis data was provided.",
    },
    AnalysisErrorCode.HRMS_FORMULA_CHARGE_MISSING: {
        "default": "No charge sign was written on the HRMS formula.",
    },
    AnalysisErrorCode.HRMS_ION_CHARGE_MISSING: {
        "default": "No charge sign was found in the ion formula (e.g. [M+H]+).",
    },
    
    AnalysisErrorCode.HRMS_CHARGE_INCONSISTENT: {
        "default": "Inconsistent charge signs in the formula: {charge_ion}, {charge_formula}",
    },
    AnalysisErrorCode.CALCD_FORMULA_MASS_MISMATCH: {
        "default": "Warning: The calculated mass does not match the mass implied by the formula. Check the calculated mass and the formula.",
        "possible_typo": (
            "Warning: The calculated mass does not match the mass implied by the formula. Check "
            "the calculated mass and the formula. Possible typo."
        ),
    },
    AnalysisErrorCode.CALCD_FORMULA_MASS_DELTA: {
        "with_combinations": "The difference between the calculated mass and the formula mass is {difference:.4f}. It could be: {combinations}",
        "without_combinations": "The difference between the calculated mass and the formula mass is {difference:.4f}.",
    },
    AnalysisErrorCode.HRMS_PPM_TOO_LARGE: {
        "default": "The difference exceeds 10 ppm (diff = {ppm_diff:.1f} ppm). Check the found or calculated masses.",
        "values": "Calculated: {calcd_mass}; found: {found_mass}",
    },
    AnalysisErrorCode.HRMS_FOUND_MASS_MISSING: {
        "default": "Found mass was not found.",
    },
    AnalysisErrorCode.HRMS_CALCD_MASS_MISSING: {
        "default": "Calculated mass was not found.",
    },
    AnalysisErrorCode.NMR_SIGNAL_ORDER_INVALID: {
        "default": "The order of {nuclei} NMR signals is incorrect. Wrong signal order: {signals}",
    },
    AnalysisErrorCode.NMR_SIGNAL_PRECISION_INVALID: {
        "default": (
            "Signals with more than {max_digits} or fewer than {min_digits} digits after the decimal "
            "point were found in {nuclei} NMR. It is likely that {invalid_numbers} need rounding."
        ),
    },
    AnalysisErrorCode.NMR_SIGNAL_ROUNDING_NEEDED: {
        "default": (
            "Signals with one more digit after the decimal point than allowed were found in {nuclei} NMR. "
            "It is likely that {invalid_numbers} need rounding."
        ),
    },
    AnalysisErrorCode.NMR_SIGNAL_FORMAT: {
        "default": "{error}.",
        "signal_limit": "13C NMR exceeds the supported limit of {limit} represented carbon shifts. Carbon analysis and DP5q were refused.",
    },
    AnalysisErrorCode.BAD_ENDING_NMR: {
        "default": "Section {nuclei} NMR likely has an unsupported ending format. Supported endings are '.' or ';'.",
    },
    AnalysisErrorCode.HNMR_DUPLICATE_SIGNALS: {
        "default": "Duplicate numbers found in {nuclei} NMR: {duplicates}",
    },
    AnalysisErrorCode.C13_COUPLING_MISSING_WITH_F: {
        "default": "F was found in the molecule. No coupling constant was found in the 13C NMR; coupling data may be missing.",
    },
    AnalysisErrorCode.NMR_J_MISSING: {
        "default": (
            "The following brackets are missing the pattern 'J = number Hz' or are in wrong "
            "format in {nuclei} NMR: {brackets}."
        ),
    },
    AnalysisErrorCode.NMR_MULTIPLICITY_MISSING: {
        "default": "The following brackets are missing multiplicity in {nuclei} NMR: {brackets}.",
    },
    AnalysisErrorCode.NMR_SIGNAL_DESC_INVALID: {
        "default": (
            "The following brackets have an incorrect order of elements "
            "(multiplicity, J, atom count) in {nuclei} NMR: {brackets}."
        ),
    },
    AnalysisErrorCode.NMR_WRONG_MULTIPLICITY: {
        "default": "The following brackets have invalid multiplicity in {nuclei} NMR: {brackets}.",
    },
    AnalysisErrorCode.NMR_MENTIONED_BUT_ELEMENT_MISSING: {
        "default": "{nuclei} NMR is mentioned in the text, but {element_name} is not present in the molecular formula from HRMS/EA/name.",
    },
    AnalysisErrorCode.NMR_SOLVENT_OR_FREQUENCY_MISSING: {
        "default": "No solvent and frequency information was found in {nuclei} NMR.",
    },
    AnalysisErrorCode.HNMR_MISSING_OR_UNCOUNTED: {
        "default": "No 1H NMR was found, or H was not counted.",
    },
    AnalysisErrorCode.NMR_SIGNAL_COUNT_STRUCTURE_MISMATCH: {
        "default": "The number of signals in {nuclei} NMR ({nmr_count}) is not equal to the number of non-equivalent atoms in the structure inferred from the name ({trees_count}).",
    },
    AnalysisErrorCode.NMR_DIASTEREOTOPIC_NOTE: {
        "default": (
            "The {nuclei} isotope-substitution test distinguishes atoms with the same achiral graph "
            "environment. These diastereotopic environments can affect the number of signals."
        ),
    },
    AnalysisErrorCode.NMR_SECTION_MISSING: {
        "default": "No {nuclei} NMR section was found.",
    },
    AnalysisErrorCode.C13_COUNT_EXCEEDS_FORMULA: {
        "default": "The carbon count in 13C NMR exceeds the formula (from name/HRMS/EA): {nmr_count} > {formula_c_count}.",
    },
    AnalysisErrorCode.C13_COUNT_STRUCTURE_CHECK: {
        "annotation_inventory": "Carbon observations represent at least {minimum_atoms} atoms, exceeding the available {atom_budget}. Carbon annotations require review.{dp5_note}",
        "annotation_partition": "Explicit carbon overlap annotations {annotations} cannot fit the selected groups’ formal-environment counts {group_sizes}. DP5q was refused.",
        "parsed_name": "Check the parsed name. If it is wrong, check the data manually.",
        "count_high": (
            "Check the number of C signals in 13C NMR: {nmr_count}; probably, it should be {trees_count}. "
            "Also check the parsed name. If the molecule has groups that are diastereotopic, shows hindered rotation, "
            "exists as E/Z (cis/trans) stereoisomers, or other factors, the number of signals can be higher than expected."
        ),
        "count_low": (
            "Check the number of C signals in 13C NMR: {nmr_count}; probably, it should be {trees_count}. "
            "Also check the parsed name. If the molecule contains a diazo group, isonitrile, carbene, carbocations, organometallic "
            "compounds, polyfluoroorganic groups, an alpha-C atom next to boron, or other cases where carbon signals can be broadened or not observed, "
            "the number of 13C NMR signals may be lower than expected."
        ),
        "count_high_c_set": (
            "Check the number of C signals in 13C NMR: {nmr_count}; probably, it should be "
            "{trees_count} or {c_set}. Also check the parsed name. If the molecule has groups "
            "that are diastereotopic, shows hindered rotation, exists as E/Z (cis/trans) "
            "stereoisomers, or other factors, the number of signals can be higher than expected."
        ),
        "count_low_c_set": (
            "Check the number of C signals in 13C NMR: {nmr_count}; probably, it should be "
            "{trees_count} or {c_set}. Also check the parsed name. If the molecule contains "
            "a diazo group, isonitrile, carbene, carbocations, organometallic compounds, "
            "polyfluoroorganic groups, an alpha-C atom next to boron, or other cases where carbon "
            "signals can be broadened or not observed, "
            "the number of 13C NMR signals may be lower than expected."
        ),
        "diastereotopic": (
            "There is probably a stereogenic center in the molecule. Therefore, two identical "
            "substituents on the same atom may be diastereotopic, which can affect the number "
            "of signals."
        ),
    },
    AnalysisErrorCode.C13_COUNT_STRUCTURE_CHECK_STEREO: {
        "manual_coincidence": (
            "The full structure has {trees_count} distinct carbon environments. "
            "The {nmr_count} listed observations may include coincident shifts; manual verification is required. "
            "Distinct environments remain separate."
        ),
        "default": (
            "The number of signals in the 13C NMR spectrum requires manual verification. "
            "A stereogenic center is present in the structure. The number "
            "of signals matches when accounting for partially unresolved split signals = {nmr_count}, "
            "while the total number of inequivalent atoms is {trees_count}."
        ),
    },
    AnalysisErrorCode.NAME_FORMULA_MISMATCH: {
        "default": (
            "Mismatch between name and formula. Name implies formula {name_formula} (mass {name_mass:.4f}), "
            "but instrument data gives {instrument_formula} (mass {instrument_mass:.4f})."
        ),
    },
    AnalysisErrorCode.NAME_FORMULA_MASS_DELTA: {
        "with_combinations": "The mass difference ({mass_difference:.4f}) could be explained by: {combinations}.",
        "without_combinations": "The mass difference is {mass_difference:.4f}.",
    },
    AnalysisErrorCode.CYRILLIC_LETTERS_FOUND: {
        "default": "Cyrillic letters were found in the text: {letters}",
    },
    AnalysisErrorCode.NAME_NOT_FOUND: {
        "default": "Name was not found or unsupported compound number format.",
        "not_isolated": "A compound name could not be isolated from the text before the compound number. Check the name manually.",
    },
    AnalysisErrorCode.EA_TOLERANCE_EXCEEDED: {
        "default": "The allowable deviation for {element} has been exceeded: {diff:.2f}% (max. {tolerance:.1f}%).",
    },
    AnalysisErrorCode.FORMULA_DUPLICATE_ELEMENTS: {
        "default": "The elements {duplicates} are repeated in the formula {molecular_formula}. Please combine them and try again.",
    },
    AnalysisErrorCode.FORMULA_ORDER_INVALID: {
        "default": (
            "Incorrect order of atoms in the formula {molecular_formula}. Found: {found}, expected "
            "(IUPAC): {expected}. Note: According to IUPAC rules, elements should appear in the following "
            "order: Carbon (C), Hydrogen (H), and then the remaining elements in alphabetical order."
        ),
    },
    AnalysisErrorCode.EA_FORMULA_ELEMENT_MISSING_IN_CALCD: {
        "default": (
            "Element {element} is present in the composition from the formula but is missing in the calculated "
            "composition. See the calculated formula mass mismatch warning for O in the composition."
        ),
    },
    AnalysisErrorCode.C13_SECTION_MISSING: {
        "default": "No {nuclei} NMR section was found. For known compounds, please verify that a literature reference is included.",
    },
    AnalysisErrorCode.XNUC_DUPLICATE_SIGNALS: {
        "default": "Duplicate numbers found in {nuclei} NMR: {duplicates}",
    },
    AnalysisErrorCode.DP5_LOW_PROBABILITY: {
        "default": (
            "DP5q compatibility score is {score_text}% "
            "(threshold {threshold_text}%). Verify the assigned structure and 13C NMR data."
        ),
    },
    AnalysisErrorCode.DP5_SIGNAL_OUTLIER: {
        "default": (
            "13C NMR signal {signal} ppm does not fit the structure: without it the other {others} signals "
            "match the DP5q predictions (rms {rest} ppm), and the carbon left without a signal is predicted "
            "near {predicted} ppm. Check this shift for a typo."
        ),
    },
    AnalysisErrorCode.DP5_SERIES_DEVIATION: {
        "default": (
            "The same carbon deviates from the DP5q prediction in {count} compounds of this series "
            "({compounds}). Either the model mispredicts this fragment or the structure of the whole series "
            "needs checking. Is there an X-ray structure for at least one compound of the series?"
        ),
    },
    AnalysisErrorCode.NAME_DATA_MISMATCH: {
        "default": (
            "{checks} all disagree with the structure taken from the name. Independent checks rarely fail "
            "together by chance: check the name first (locants, substituents, how the name is parsed), then "
            "the data."
        ),
    },
    AnalysisErrorCode.DP5_REFERENCING_OFFSET: {
        "default": (
            "All 13C NMR signals are displaced by about {offset} ppm from the predictions, unlike the other "
            "compounds of this document ({carbons} signals; they fit within {residual} ppm once the "
            "displacement is removed). Check the referencing of this spectrum."
        ),
    },
    AnalysisErrorCode.DP5_ANALYSIS_FAILED: {
        "default": "DP5q analysis failed for this compound: {details}",
    },
    AnalysisErrorCode.DP5_NOT_APPLICABLE: {
        "default": "DP5q not applicable: {details}",
    },
    AnalysisErrorCode.FORMULA_SOURCE_MISMATCH: {
        "default": "HRMS gives {hrms_ion} (neutral {hrms_neutral}), EA gives {ea_formula}. Check which formula source is correct.",
        "parent_ion": "HRMS gives {hrms_ion} (parent ion {hrms_neutral}), EA gives {ea_formula}. Check which formula source is correct.",
    },
    AnalysisErrorCode.FORMULA_SOURCE_REVIEW_REQUIRED: {
        "default": "Formula-source comparability requires scientific review. {reasons}",
    },
    AnalysisErrorCode.EA_ISOTOPE_CALCD_MISMATCH: {
        "default": (
            "Calculated isotope-EA percentages ({calculated_percentages}) match neither "
            "the actual-isotope mass values ({mass_percentages}) nor the ordinary-element "
            "atom-count values ({atom_count_percentages}), both using the actual isotope molar mass. "
            "The isotope EA percentage convention remains pending review."
        ),
    },
    AnalysisErrorCode.DP5_BORON_NOT_SUPPORTED: {
        "default": "DP5q not calculated: boron-containing compounds are not supported (no validated geometry/prediction for boron)",
    },
    AnalysisErrorCode.C13_BORON_SIGNAL_NOT_OBSERVED: {
        "default": "C–B carbon not observed, as is common. {details}",
    },
    AnalysisErrorCode.NMR_CONFIGURATION_DEPENDENT: {
        "default": (
            "The expected {nuclei} signal count depends on unspecified configuration: "
            "expected {counts} depending on configuration. Verify the sample configuration."
        ),
        "grouping": (
            "The expected {nuclei} atom grouping depends on unspecified configuration. "
            "Supported forms have {count} environments but different original atom groups; "
            "verify the sample configuration."
        ),
    },
    AnalysisErrorCode.DP5_CONFIGURATION_DEPENDENT: {
        "default": (
            "DP5q calculated for one stereoisomer only; another isomer may give a very different result. "
            "Selected isomer: {selected_isomer_smiles}."
        ),
        "unverified_geometry": (
            "DP5q calculated for one stereoisomer only; another isomer may give a very different result. "
            "Coordinate verification of stereochemistry or carbon equivalence is incomplete. "
            "Selected isomer: {selected_isomer_smiles}."
        ),
        "organic_component": (
            "DP5q calculated for the organic component only; the counter-ion/solvate was ignored, "
            "and protonation or salt formation may shift some 13C signals."
        ),
    },
    AnalysisErrorCode.H1_SHIFT_STRUCTURE_MISMATCH: {
        "default": (
            "1H NMR shifts deviate from CASCADE predictions for this structure by {mean_error} ppm on average "
            "(threshold {threshold} ppm). Verify the structure and the 1H NMR data."
        ),
    },
    AnalysisErrorCode.H1_SHIFT_SIGNAL_OUTLIER: {
        "default": (
            "1H NMR signal {signal} is far from the CASCADE-predicted {predicted} ppm "
            "(threshold {threshold} ppm). The prediction can be wrong for an unusual group; if the shift is "
            "typical for this group, no change is needed. Otherwise check the name and the data."
        ),
    },
    AnalysisErrorCode.H1_SHIFT_NOT_APPLICABLE: {
        "default": "1H shift comparison not applicable: {details}",
    },
    AnalysisErrorCode.H1_MULTIPLICITY_STRUCTURE_MISMATCH: {
        "default": (
            "1H NMR signal {signal} needs more coupling partners than any proton of the structure predicted "
            "near this shift has (at most {available}). Verify the multiplicity, J values and structure."
        ),
        "partly_possible": (
            "1H NMR signal {signal}: a first-order signal of {protons} protons needs one group of {protons} "
            "equivalent protons with this multiplicity, and this structure has none near this shift "
            "(largest such group: {available}). The signal probably combines several signals under one "
            "label; report them separately or as a multiplet."
        ),
        "must_split": (
            "1H NMR signal {signal} is reported as a singlet, but the protons predicted near this shift have "
            "neighbouring protons and cannot give a singlet of this size. Check the multiplicity and the integral."
        ),
    },
    AnalysisErrorCode.HNMR_J_PARTNER_MISSING: {
        "default": (
            "1H NMR signal {signal}: no other signal shows the coupling J = {value} Hz (tolerance {tolerance} Hz). "
            "Every H-H coupling must appear in both partners. Check the J values."
        ),
    },
    AnalysisErrorCode.HNMR_J_PARTNER_MISMATCH: {
        "default": (
            "1H NMR signals {signal} and {other} look like coupling partners, but their constants differ: "
            "J = {value} and {other_value} Hz (difference {difference} Hz, tolerance {tolerance} Hz). "
            "Coupled protons share one constant; re-measure both signals."
        ),
    },
    AnalysisErrorCode.HNMR_J_VALUE_IMPLAUSIBLE: {
        "absolute": (
            "1H NMR signal {signal}: J = {value} Hz is larger than any H-H coupling (up to {limit} Hz) and the "
            "structure has no F or P. The multiplet is probably two overlapping signals or mislabelled."
        ),
        "no_structure": (
            "1H NMR signal {signal}: J = {value} Hz is larger than any H-H coupling (up to {limit} Hz). Unless "
            "it is a coupling to F or P, the multiplet is probably two overlapping signals or mislabelled."
        ),
        "structure": (
            "1H NMR signal {signal}: J = {value} Hz is larger than any H-H coupling this structure can show "
            "(up to about {limit} Hz). Check the J value, the multiplicity and the structure."
        ),
    },
    AnalysisErrorCode.HNMR_HETERONUCLEAR_COUPLING_MISSING: {
        "geminal_hf": (
            "The structure has a proton on a carbon bearing fluorine, but the 1H NMR description has no coupling "
            "of {limit} Hz or more (2J H-F is 44-85 Hz). The H-F coupling is missing or hidden in a multiplet."
        ),
        "direct_ph": (
            "The structure has a P-H bond, but the 1H NMR description has no coupling of {limit} Hz or more "
            "(1J P-H). The P-H coupling is missing."
        ),
    },
    AnalysisErrorCode.C13_CF_J_VALUE_IMPLAUSIBLE: {
        "no_partner": (
            "13C NMR signal {signal} is reported with a coupling constant, but the structure has no F or P."
        ),
        "no_cf_bond": (
            "13C NMR signal {signal}: J = {value} Hz is a one-bond C-F coupling, but the structure has no fluorine."
        ),
        "range": (
            "13C NMR signal {signal}: J = {value} Hz is outside the ranges of C-F couplings "
            "(1J 150-400, 2J 12-60, longer range below 12 Hz). Check the value."
        ),
    },
    AnalysisErrorCode.C13_CF_MULTIPLICITY_MISMATCH: {
        "default": (
            "13C NMR signal {signal}: for a carbon {bonds} bond(s) from fluorine this structure requires the "
            "multiplicity {expected} (C-F d, CF2 t, CF3 q). Check the multiplicity."
        ),
    },
    AnalysisErrorCode.FNMR_DECOUPLING_NOT_STATED: {
        "default": (
            "The structure has protons two or three bonds from fluorine, but the 19F NMR signal {signal} is "
            "reported as a singlet. If the 19F spectra were recorded with 1H decoupling, say so; otherwise "
            "check that the signal is a singlet."
        ),
    },
    AnalysisErrorCode.C13_CF_LONG_RANGE_COUPLING_MISSING: {
        "default": (
            "13C NMR: the one- and two-bond C-F couplings are reported, but {expected_three} aromatic carbon "
            "environment(s) three bonds from fluorine should also be split (3J of 3-9 Hz) and the description "
            "has {found_three} signal(s) with such a coupling. Check that these signals are really singlets."
        ),
    },
    AnalysisErrorCode.C13_CF_COUPLING_INCOMPLETE: {
        "default": (
            "13C NMR: not all C-F couplings are reported. The structure has {expected_one} carbon environment(s) "
            "bonded to F, {expected_two} two bonds away and {expected_three} aromatic three bonds away; the "
            "description has {found_one} signal(s) with 1J, {found_two} with 2J and {found_three} with smaller "
            "couplings."
        ),
        "separate_lines": (
            "13C NMR: no one-bond C-F coupling is reported for the carbon bonded to fluorine, and the plain "
            "signals at {low}-{high} ppm are spaced like the lines of a C-F doublet. The doublet seems to be "
            "listed as separate signals: report one signal with its multiplicity and coupling constant."
        ),
    },
    AnalysisErrorCode.H1_PROTONS_UNACCOUNTED: {
        "default": (
            "{count} C-H proton(s) of the structure (predicted near {shifts} ppm) have no 1H NMR signal within "
            "{threshold} ppm, and the description lists {reported} H against {total} in the structure "
            "({carbon_bound} C-H + {exchangeable} exchangeable). The missing protons are not only exchangeable "
            "ones: a signal may be absent or under-integrated, or the name may describe another structure."
        ),
    },
}
