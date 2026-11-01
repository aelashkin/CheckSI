"""Source ownership and bounded work at compound-name boundaries."""

import unittest
from unittest.mock import patch

from checksi.analysis import name_resolver
from checksi.analysis.nmr import _check_constant_hnmr
from checksi.controller import CheckSIController
from checksi.utils import name_boundaries
from checksi.utils.error_handler import ErrorTracker
from tests.test_name_resolver import StubWorker, make_config
from tests import test_wave3_regressions as wave3


PROTON = wave3.PROTON


NMR = ("1H NMR (400 MHz, CDCl3): δ 3.60 (t, J = 6.4 Hz, 2H), 2.00 (s, 1H), "
       "1.60 (m, 2H), 1.00 (t, J = 7.2 Hz, 3H). "
       "13C NMR (100 MHz, CDCl3): δ 63.0, 25.0, 10.0. ")


class NameSourceIntegrityTests(unittest.TestCase):
    def parse(self, parts, extension):
        return wave3.ReviewNameBoundaryTests().parse_sources(parts, extension)

    def analyze(self, parts, extension="docx", mode="on"):
        paragraphs = self.parse(parts, extension)
        worker = StubWorker(lambda backend, name: {"propan-1-ol": "CCCO", "ethanol": "CCO"}.get(name))
        with patch.object(name_resolver, "_ResolverWorker", return_value=worker):
            result = CheckSIController(make_config(mode=mode), event_dispatcher=lambda _: None).run_analysis(paragraphs)
        return result, worker.calls, paragraphs

    def test_wrapped_analytical_tails_retain_previous_formula_evidence_and_findings(self):
        layouts = (
            (NMR + "HRMS (ESI):", "[M+H]+ calcd for C3H9O+ 61.0648; found 61.0648"),
            (NMR + "HRMS (ESI): calcd for C3H9O+", "[M+H]+ 61.0648, found 61.0648"),
            (NMR + "HRMS (ESI): [M+H]+ calcd for C3H9O+ 61.0648;", "found 61.0648 (Δ 0.0 ppm)"),
            (NMR + "HRMS (ESI): [M+H]+ calcd for C3H9O+ 61.0648;",
             "found 61.0648; FT-IR (KBr): ν˜= 3499 cm-1"),
            (NMR + "Anal. Calcd for C3H8O: C, 59.96; H, 13.42; O, 26.62.",
             "Found: C, 59.96; H, 13.42; O, 26.62 (±0.4)"),
        )
        for header, tail in layouts:
            for extension in ("docx", "pdf"):
                for mode in ("off", "on"):
                    with self.subTest(tail=tail, extension=extension, mode=mode):
                        original = ["propan-1-ol (1)", header, tail]
                        baseline, _, _ = self.analyze(original, extension, mode)
                        result, calls, paragraphs = self.analyze(original + ["ethanol (2)", PROTON], extension, mode)
                        first, second = result["compounds"]
                        previous = baseline["compounds"][0]
                        self.assertEqual(first["original_text"].strip(), previous["original_text"].strip())
                        self.assertEqual(first["formula_sources"], previous["formula_sources"])
                        self.assertEqual(first["all_warnings"], previous["all_warnings"])
                        self.assertEqual((second["name"], second["identifier"]), ("ethanol", "2"))
                        self.assertEqual(sum(part.count("ethanol (2)") for part in paragraphs), 1)
                        self.assertTrue(all(name in {"propan-1-ol", "ethanol"} for _, name in calls))

    def test_bracket_led_nmr_tails_retain_the_previous_signal_analysis(self):
        layouts = (
            ("1H NMR (400 MHz, CDCl3): δ 3.60 (t, J = 6.4 Hz, 2H), 2.00 (s, 1H), 1.60 (m, 2H), 1.00",
             "(t, J = 7.2 Hz, 3H); mp 52-54 °C"),
            (NMR.replace("63.0, 25.0, 10.0. ", "63.0 "), "(1C), 25.0, 10.0; IR (KBr): 3499 cm-1"),
            (PROTON, "NMR (100 MHz, CDCl3): δ 63.0, 25.0, 10.0"),
        )
        for header, tail in layouts:
            for extension in ("docx", "pdf"):
                with self.subTest(tail=tail, extension=extension):
                    original = ["propan-1-ol (1)", header, tail]
                    baseline, _, _ = self.analyze(original, extension)
                    result, calls, _ = self.analyze(original + ["ethanol (2)", PROTON], extension)
                    first, second = result["compounds"]
                    self.assertEqual(first["original_text"].strip(), baseline["compounds"][0]["original_text"].strip())
                    self.assertEqual(first["all_warnings"], baseline["compounds"][0]["all_warnings"])
                    self.assertEqual(second["name"], "ethanol")
                    self.assertTrue(all(name in {"propan-1-ol", "ethanol"} for _, name in calls))

    def test_natural_pdf_wrap_preserves_pavel_172_hrms_and_173_name(self):
        description = (
            "1H NMR (400 MHz, CDCl3) δ 7.39–7.31 (m, 4H), 7.30–7.27 (m, 1H), 7.00 (d, J = 8.2 Hz, 2H), "
            "6.57 (d, J = 8.2 Hz, 2H), 4.31 (s, 2H), 3.89 (br. s, 1H), 2.25 (s, 3H); "
            "13C NMR (101 MHz, CDCl3) δ 145.9, 139.6, 129.8, 128.6, 127.5, 127.2, 126.9, 113.1, 48.7, 20.4. "
            "HRMS (ESI) m/z: [M+H]+ calcd for C14H16N+ 198.1277; found 198.1280; FT-IR (KBr): ν˜= 3499 cm-1"
        )
        first_name = "N-Benzyl-4-methylaniline"
        for mode in ("off", "on"):
            for ending in (" (1.73)", " (1.73).", "(1.73)", " (1.73):"):
                with self.subTest(mode=mode, ending=ending):
                    parts = [first_name + " (1.72)", description, wave3.NAME + ending, PROTON]
                    paragraphs = self.parse(parts, "pdf")
                    baseline_parts = self.parse(parts[:2], "pdf")
                    runs = []
                    for source in (baseline_parts, paragraphs):
                        worker = StubWorker(lambda backend, name: {
                            first_name: "Cc1ccc(NCc2ccccc2)cc1", wave3.NAME: wave3.SMILES,
                        }.get(name))
                        with patch.object(name_resolver, "_ResolverWorker", return_value=worker):
                            result = CheckSIController(make_config(mode=mode), event_dispatcher=lambda _: None).run_analysis(source)
                        runs.append(result)
                        self.assertTrue(all(name in {first_name, wave3.NAME} for _, name in worker.calls))
                    previous = runs[0]["compounds"][0]
                    first, second = runs[1]["compounds"]
                    self.assertEqual(first["original_text"], previous["original_text"])
                    self.assertEqual(first["formula_sources"], previous["formula_sources"])
                    self.assertEqual(first["all_warnings"], previous["all_warnings"])
                    self.assertEqual(first["formula_sources"]["hrms"]["formula"], "C14H16N")
                    self.assertEqual((second["name"], second["identifier"]), (wave3.NAME, "1.73"))
                    self.assertEqual(" ".join(paragraphs).count("3499 cm-1"), 1)

    def test_data_bearing_identifier_candidates_stay_in_the_previous_description(self):
        for tail in ("18.4 (CH3)", "18.4 (C-2)", "18.4 (C)", "found 61.0648. ethanol (2):"):
            for extension in ("docx", "pdf"):
                with self.subTest(tail=tail, extension=extension):
                    header = NMR.replace(". 13C", "; 13C").removesuffix("63.0, 25.0, 10.0. ") + "63.0,"
                    if tail.startswith("found"):
                        header = NMR + "HRMS (ESI): [M+H]+ calcd for C3H9O+ 61.0648;"
                    result, calls, _ = self.analyze(["propan-1-ol (1)", header, tail, PROTON], extension)
                    first, _ = result["compounds"]
                    self.assertIn(tail.split(". ethanol")[0], first["original_text"])
                    self.assertFalse(any(name.startswith(("18.4", "found")) for _, name in calls))

    def test_boundary_guards_refuse_finished_labels_and_unbalanced_names(self):
        for previous in ("discussion-with-hyphen.", "earlier-name (3a)", "earlier-name (A)"):
            with self.subTest(previous=previous):
                self.assertIsNone(name_boundaries.compound_name_start([PROTON, previous, "ethanol (2)", PROTON], 2))
        self.assertIsNone(name_boundaries.compound_name_start([PROTON, "N-(phenyl", "ethanol (2)", PROTON], 2))
        self.assertIsNone(name_boundaries.compound_name_start(
            [PROTON, "Unproven-name-fragment", "found 61.0648 (sample A)", "ethanol (2)", PROTON], 3))

    def test_genuine_multiword_wrapped_chemical_name_is_retained(self):
        self.assertEqual(name_boundaries.compound_name_start(
            [PROTON, "tert-Butyl 2-bromo-3-phenyl", "acetate (2a)", PROTON], 2), 1)

    def test_source_tokens_before_first_name_are_refused_without_suffix_guessing(self):
        for prefix in ("Amides", "(KBr)", "[M+H]+", "17"):
            for extension in ("docx", "pdf"):
                with self.subTest(prefix=prefix, extension=extension):
                    result, calls, _ = self.analyze([prefix, "ethanol (2)", PROTON], extension)
                    compound, = result["compounds"]
                    self.assertEqual(compound["name"], "")
                    self.assertEqual(compound["name_resolution"]["reason"], "mixed_context")
                    self.assertEqual(calls, [])

    def test_wrapped_first_chemical_name_keeps_all_chemical_words(self):
        name = "ethyl 3a-methyl-1H-indene-2-carboxylate"
        for extension in ("docx", "pdf"):
            with self.subTest(extension=extension):
                paragraphs = self.parse(["ethyl", name.removeprefix("ethyl ") + " (3b)", PROTON], extension)
                worker = StubWorker(lambda backend, candidate: "CCO" if candidate == name else None)
                with patch.object(name_resolver, "_ResolverWorker", return_value=worker):
                    result = CheckSIController(make_config(), event_dispatcher=lambda _: None).run_analysis(paragraphs)
                compound, = result["compounds"]
                self.assertEqual((compound["name"], compound["identifier"]), (name, "3b"))
                self.assertEqual(worker.calls, [("local", name)])


class NameIdentifierAndDescriptorTests(unittest.TestCase):
    def test_spaced_hyphen_after_uppercase_identifier_is_a_property_separator(self):
        for suffix in (" - colorless liquid", " – colorless liquid", " — colorless liquid"):
            result = name_resolver.smiles_and_name_from_paragraph(
                make_config(mode="off"), ErrorTracker(), "2-Methylpropan-2-ol (A)" + suffix, {})
            self.assertEqual(result[1:3], ("2-Methylpropan-2-ol", "A"))

    def test_common_group_assignment_labels_are_not_unknown_multiplicities(self):
        for assignment in ("Me", "OMe", "Ph", "tBu", "Boc", "TMS", "Ac", "OAc", "NMe", "Bn", "Ha", "Hb"):
            with self.subTest(assignment=assignment):
                tracker = ErrorTracker()
                _check_constant_hnmr(make_config(), tracker, f"δ 3.50 (3H, {assignment})")
                self.assertEqual(tracker.get_count("hnmr_multiplicity_missing"), 1)
                self.assertEqual(tracker.get_count("hnmr_wrong_multiplicity"), 0)

    def test_assignment_stop_still_excludes_a_later_multiplicity_letter(self):
        for descriptor, missing in (("3H, NMe, s", 1), ("3H, s, NMe", 0)):
            with self.subTest(descriptor=descriptor):
                tracker = ErrorTracker()
                _check_constant_hnmr(make_config(), tracker, f"δ 3.50 ({descriptor})")
                self.assertEqual(tracker.get_count("hnmr_multiplicity_missing"), missing)
                self.assertEqual(tracker.get_count("hnmr_wrong_multiplicity"), 0)
                self.assertEqual(tracker.get_count("hnmr_signal_desc_invalid"), 1)


class NameSourceWorkTests(unittest.TestCase):
    def test_bulk_source_scan_checks_headers_before_identifiers(self):
        parts = ["(1a,(1,(a1,(1-A," * 250] * 200
        with patch.object(name_boundaries, "terminal_identifier", wraps=name_boundaries.terminal_identifier) as scan:
            self.assertEqual(name_boundaries.compound_name_starts(parts), set())
        self.assertEqual(scan.call_count, 0)

    def test_header_adjacent_malformed_sources_are_scanned_once_each(self):
        attack = ("(1a,(1,(a1,(1-A," * 300)[:4000]
        parts = [part for _ in range(200) for part in (attack, PROTON)]
        with patch.object(name_boundaries, "terminal_identifier", wraps=name_boundaries.terminal_identifier) as scan:
            self.assertEqual(name_boundaries.compound_name_starts(parts), set())
        self.assertEqual(scan.call_count, 200)
        self.assertEqual(sum(len(call.args[0]) for call in scan.call_args_list), 800000)

    def test_terminal_identifier_handles_adversarial_openings_without_global_regex_search(self):
        with patch.object(name_boundaries.regex, "finditer", side_effect=AssertionError("unbounded global search")):
            for size in (1000, 4000, 16000):
                with self.subTest(size=size):
                    self.assertIsNone(name_boundaries.terminal_identifier(("(1a,(1,(a1,(1-A," * size)[:size]))
                    text = ("(1,(1a," * size)[:size] + ")"
                    span = name_boundaries.terminal_identifier(text)
                    self.assertIsNotNone(span)
                    self.assertEqual(text[span[0]:span[1]][-1], ")")

    def test_repeated_single_letter_identifiers_share_one_bracket_scan(self):
        for count in (1000, 3000, 10000):
            paragraph = "x " + "(A) " * count + "propan-1-ol (B)"
            with self.subTest(count=count), patch.object(
                name_boundaries, "balanced_name", side_effect=AssertionError("rescanned prefix"),
            ), patch.object(name_resolver, "balanced_identifier_starts", wraps=name_resolver.balanced_identifier_starts) as scan:
                names = list(name_resolver._extracted_names(paragraph, name_resolver._Budget(2.0, None)))
                self.assertEqual(names[-1][1], "B")
                self.assertEqual(scan.call_count, 1)
                self.assertEqual(len(scan.call_args.args[0]), len(paragraph))

    def test_repeated_single_letter_identifiers_do_not_refuse_a_bounded_public_name(self):
        details = {}
        result = name_resolver.smiles_and_name_from_paragraph(
            make_config(mode="off"), ErrorTracker(), "x " + "(A) " * 1000 + "propan-1-ol (B)", {}, details=details)
        self.assertTrue(result[1])
        self.assertEqual(result[2], "B")
        self.assertEqual((details["status"], details["reason"]), ("disabled", None))

    def test_terminal_identifier_preserves_the_established_terminal_grammar(self):
        for identifier in ("1a", "1.73", "6aa-1", "12ab-34", "4.1", "A", "3j, S", "1-A", "GP1", "1, 2"):
            with self.subTest(identifier=identifier):
                text = f"ethanol ({identifier})."
                start, end = name_boundaries.terminal_identifier(text)
                self.assertEqual(text[start:end], f"({identifier})")
        for text in ("ethanol (1,2)", "ethanol (1a, 5 mg)", "(R)-ethanol", "ethanol (1a) notes"):
            with self.subTest(text=text):
                self.assertIsNone(name_boundaries.terminal_identifier(text))
