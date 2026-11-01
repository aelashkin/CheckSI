"""Error identity definitions for analysis and reporting."""

from enum import StrEnum, auto


class AnalysisErrorCode(StrEnum):
    # Stereochemistry
    STEREO_CHECK_REQUIRED = auto()
    STEREO_DESCRIPTOR_INVALID = auto()
    STEREO_DESCRIPTOR_INCONSISTENT = auto()

    # Formula / elemental parsing
    FORMULA_INVALID_FORMAT = auto()
    EA_NUMBER_FORMAT_INVALID = auto()  # Malformed, missing, duplicate and out-of-range EA entries.
    EA_CALCD_FOUND_ELEMENT_MISMATCH = auto()
    EA_FORMULA_CALCD_MISMATCH = auto()

    # NMR presence / parsing
    REQUIRED_NMR_MISSING = auto()
    NAME_TO_SMILES_FAILED = auto()
    NMR_FREQUENCY_INVALID = auto()
    NMR_FREQUENCY_MISMATCH = auto()
    SOLVENT_PROTON_PEAK_FOUND = auto()
    SOLVENT_CARBON_PEAK_FOUND = auto()
    SOLVENT_NOT_FOUND = auto()

    # Name parsing
    NAME_MODIFIED_FOR_SMILES = auto()

    # HRMS checks
    HRMS_ADDUCT_MISMATCH = auto()
    HRMS_ION_BALANCE_INVALID = auto()
    HALOGEN_ISOTOPE_UNSPECIFIED = auto()

    # 1H accounting
    H_COUNT_MISMATCH = auto()
    H_COUNT_EXCHANGEABLE_OK = auto()
    H_COUNT_RATIO_CHECK = auto()
    HNMR_J_MISSING = auto()
    HNMR_H_COUNT_MISSING = auto()
    H_COUNT_NOT_INTEGER = auto()
    HNMR_MULTIPLICITY_MISSING = auto()
    HNMR_SIGNAL_DESC_INVALID = auto()
    HNMR_WRONG_MULTIPLICITY = auto()

    # Isotope / formula syntax
    ISOTOPE_FORMAT_INVALID = auto()
    FORMULA_ATOM_SYMBOL_INVALID = auto()
    FORMULA_NOT_FOUND = auto()
    HRMS_FORMULA_CHARGE_MISSING = auto()
    HRMS_ION_CHARGE_MISSING = auto()
    HRMS_FORMULA_MISSING = auto()
    HRMS_CHARGE_INCONSISTENT = auto()

    # Mass mismatch checks
    CALCD_FORMULA_MASS_MISMATCH = auto()
    CALCD_FORMULA_MASS_DELTA = auto()
    HRMS_PPM_TOO_LARGE = auto()
    HRMS_FOUND_MASS_MISSING = auto()
    HRMS_CALCD_MASS_MISSING = auto()

    # NMR signal quality
    NMR_SIGNAL_ORDER_INVALID = auto()
    NMR_SIGNAL_PRECISION_INVALID = auto()
    NMR_SIGNAL_ROUNDING_NEEDED = auto()
    NMR_SIGNAL_FORMAT = auto()
    BAD_ENDING_NMR = auto()
    HNMR_DUPLICATE_SIGNALS = auto()
    C13_COUPLING_MISSING_WITH_F = auto()
    NMR_J_MISSING = auto()
    NMR_MULTIPLICITY_MISSING = auto()
    NMR_SIGNAL_DESC_INVALID = auto()
    NMR_WRONG_MULTIPLICITY = auto()
    NMR_MENTIONED_BUT_ELEMENT_MISSING = auto()
    NMR_SOLVENT_OR_FREQUENCY_MISSING = auto()
    HNMR_MISSING_OR_UNCOUNTED = auto()
    NMR_SIGNAL_COUNT_STRUCTURE_MISMATCH = auto()
    NMR_DIASTEREOTOPIC_NOTE = auto()
    NMR_SECTION_MISSING = auto()
    C13_COUNT_EXCEEDS_FORMULA = auto()
    C13_COUNT_STRUCTURE_CHECK = auto()
    C13_COUNT_STRUCTURE_CHECK_STEREO = auto()

    # Cross-checks and controller warnings
    NAME_FORMULA_MISMATCH = auto()
    NAME_FORMULA_MASS_DELTA = auto()
    CYRILLIC_LETTERS_FOUND = auto()
    NAME_NOT_FOUND = auto()

    # Config toggle (no emitted text)
    EA_INCLUDE_OXYGEN = auto()

    # Elemental-analysis tolerances / formula style
    EA_TOLERANCE_EXCEEDED = auto()
    FORMULA_DUPLICATE_ELEMENTS = auto()
    FORMULA_ORDER_INVALID = auto()
    EA_FORMULA_ELEMENT_MISSING_IN_CALCD = auto()
    C13_SECTION_MISSING = auto()
    XNUC_DUPLICATE_SIGNALS = auto()

    # Managed DP5 integration
    DP5_LOW_PROBABILITY = auto()
    DP5_ANALYSIS_FAILED = auto()

    # New report rows are appended to preserve the historical XLSX layout.
    DP5_NOT_APPLICABLE = auto()
    FORMULA_SOURCE_MISMATCH = auto()
    FORMULA_SOURCE_REVIEW_REQUIRED = auto()
    EA_ISOTOPE_CALCD_MISMATCH = auto()
    DP5_BORON_NOT_SUPPORTED = auto()
    C13_BORON_SIGNAL_NOT_OBSERVED = auto()
    NMR_CONFIGURATION_DEPENDENT = auto()
    DP5_CONFIGURATION_DEPENDENT = auto()

    # Public spelling retained as an alias, not an independent finding or row.
    NMR_SOLVENT_AND_FREQUENCY_MISSING = NMR_SOLVENT_OR_FREQUENCY_MISSING

    @classmethod
    def _missing_(cls, value: object) -> "AnalysisErrorCode | None":
        """Accept the candidate spelling while retaining the stable identity."""
        if value == "nmr_solvent_and_frequency_missing":
            return cls.NMR_SOLVENT_OR_FREQUENCY_MISSING
        return None
