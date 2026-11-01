"""Catalogue regressions at the NMR, document and stereo boundaries."""

from pathlib import Path
from importlib.resources import files
import tempfile
import unittest
from unittest.mock import patch

from docx import Document
import fitz
from rdkit import Chem

from checksi.analysis import name_resolver
from checksi.analysis.nmr import _check_constant_hnmr, _check_constant_nmr
from checksi.analysis.stereochem import _check_streo_desc
from checksi.controller import CheckSIController
from checksi.config_schema import ErrorSeverity
from checksi.document_parser import DocumentParser
from checksi.errors import ErrorCode
from checksi.errors.findings import AnalysisNote, Finding
from checksi.file_io import paragraphs_from_input
from checksi.utils.error_handler import ErrorTracker
from checksi.utils.name_boundaries import compound_name_start
from tests.test_name_resolver import make_config, StubWorker


PROTON = "1H NMR (400 MHz, CDCl3): δ 3.50 (s, 2H), 1.20 (s, 3H)."
NAME = "N,N'-(((1R,3S)-cyclopentane-1,3-diyl)bis(methylene))bis(4-methylaniline)"
SMILES = "Cc1ccc(NC[C@H]2CC[C@@H](CNc3ccc(C)cc3)C2)cc1"


def active_counts(tracker):
    return {code: count for code, count in tracker.to_dict().items() if count}


class ReviewNameBoundaryTests(unittest.TestCase):
    def parse_sources(self, parts, extension="docx"):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / f"input.{extension}"
            if extension == "docx":
                document = Document()
                for text in parts:
                    document.add_paragraph(text)
                document.save(source)
            else:
                with fitz.open() as document:
                    page = document.new_page()
                    remaining = page.insert_textbox(
                        fitz.Rect(40, 40, 555, 800), "\n".join(parts), fontsize=9,
                        fontname="Noto", fontfile=str(files("checksi").joinpath(
                            "resources/fonts/NotoSans-Regular.ttf")),
                    )
                    self.assertGreaterEqual(remaining, 0)
                    document.save(source)
            parsed = DocumentParser().parse(str(source), header_margin=0, footer_margin=0)
            direct = paragraphs_from_input(str(source), header_margin=0, footer_margin=0)
        self.assertEqual(parsed.paragraphs, direct)
        return parsed.paragraphs

    def check_second_name(self, parts, name, identifier, extension="docx"):
        paragraphs = self.parse_sources(parts, extension)
        worker = StubWorker(lambda backend, candidate: "CCO")
        with patch.object(name_resolver, "_ResolverWorker", return_value=worker):
            result = CheckSIController(make_config(), event_dispatcher=lambda _: None).run_analysis(paragraphs)
        first, second = result["compounds"]
        self.assertEqual((second["name"], second["identifier"]), (name, identifier))
        self.assertNotIn(name, first["original_text"])
        self.assertIn(("local", name), worker.calls)
        self.assertTrue(all(not any(word in candidate for word in ("NMR", "calcd", "found"))
                            for _, candidate in worker.calls))

    def test_property_lines_do_not_replace_the_name(self):
        name = "4-Methylbenzaldehyde"
        for property_line in ("Colorless oil (0.50 mmol)", "Colorless oil (45.2 mg)",
                              "[α]D = +12 (c 1.0, CHCl3)", "Colorless oil (1.0 equiv)",
                              "Colorless oil (Table 2)", "Colorless oil (ref. 12)",
                              "Yield: 85% (0.85 mmol)", "Colorless oil, route B (GP1)"):
            with self.subTest(property_line=property_line):
                self.check_second_name(["ethanol (1)", PROTON, f"{name} (2a)", property_line, PROTON], name, "2a")

    def test_proton_signal_continuations_stay_in_the_first_spectrum(self):
        layouts = (
            ("1H NMR (400 MHz, CDCl3): δ 3.50 (s, 2H),", ["1.20 (s, 3H)"]),
            ("1H NMR (400 MHz, CDCl3): δ", ["3.50 (s, 2H), 1.20 (s, 3H)"]),
            ("1H NMR (400 MHz, CDCl3): δ", ["3.50 (s, 2H),", "1.20 (s, 3H)"]),
        )
        for header, continuations in layouts:
            for extension in ("docx", "pdf"):
                for mode in ("off", "on"):
                    with self.subTest(header=header, continuations=continuations, extension=extension, mode=mode):
                        paragraphs = self.parse_sources(["ethanol (1)", header, *continuations,
                                                         "methanol (2)", PROTON], extension)
                        worker = StubWorker(lambda backend, name: {"ethanol": "CCO", "methanol": "CO"}.get(name))
                        with patch.object(name_resolver, "_ResolverWorker", return_value=worker):
                            result = CheckSIController(make_config(mode=mode), event_dispatcher=lambda _: None).run_analysis(paragraphs)
                        first, second = result["compounds"]
                        self.assertEqual((second["name"], second["identifier"]), ("methanol", "2"))
                        self.assertIn("3.50 (s, 2H), 1.20 (s, 3H)", first["original_text"])
                        self.assertNotIn("methanol", first["original_text"])
                        self.assertTrue(all(name in {"ethanol", "methanol"} for _, name in worker.calls))

    def test_numeric_fragments_without_proven_proton_context_do_not_establish_names(self):
        for context, fragment in (("HRMS [M+H]+ calcd for C3H9O+", "1.20 (s, 3H)"),
                                  (PROTON, "1.20 (s)"), (PROTON, "1.20 (s, CH3)"),
                                  (PROTON, "1.20 (s, 3H) unknown residue"),
                                  (PROTON, "52.14 (C)"), (PROTON, "0.85 mmol")):
            with self.subTest(context=context, fragment=fragment):
                parts = [context, fragment, "methanol (2)", PROTON]
                self.assertIsNone(compound_name_start(parts, 2))
        self.assertIsNone(compound_name_start([PROTON, "Unproven notes", "1.20 (s, 3H)",
                                               "methanol (2)", PROTON], 3))

    def test_proton_continuation_context_preserves_wrapped_chemical_locants(self):
        for first_half, last_half in (("2-(4-Methoxyphenyl)-1-phenyl", "ethan-1-one"),
                                     ("1H-indole-3-", "carboxylic acid")):
            with self.subTest(first_half=first_half):
                self.check_second_name(["ethanol (1)", "1H NMR (400 MHz, CDCl3): δ 3.50 (s, 2H),",
                                        "1.20 (s, 3H)", first_half, last_half + " (2a)", PROTON],
                                       first_half + " " + last_half, "2a")

    def test_delta_and_signed_signal_fragments_preserve_source_ownership(self):
        fragments = ("δ 1.20 (s)", "δ = 1.20 (s)", "δ 1.20 (s, CH3)",
                     "+1.20 (s, 3H)", "-1.20 (s, 3H)", "−1.20 (s, 3H)")
        for fragment in fragments:
            # The packaged PDF fixture font does not encode U+2212 reliably;
            # DOCX proves that sign, while PDF also covers its ASCII equivalent.
            extensions = ("docx",) if "−" in fragment else ("docx", "pdf")
            for extension in extensions:
                for mode in ("off", "on"):
                    for severity in ("warning", "disabled"):
                        with self.subTest(fragment=fragment, extension=extension, mode=mode, severity=severity):
                            paragraphs = self.parse_sources(["ethanol (1)",
                                                             "1H NMR (400 MHz, CDCl3): δ 3.50 (s, 2H),",
                                                             fragment, "methanol (2)", PROTON], extension)
                            worker = StubWorker(lambda backend, name: {"ethanol": "CCO", "methanol": "CO"}.get(name))
                            config = make_config(mode=mode)
                            config.errors.default_severity = ErrorSeverity(severity)
                            with patch.object(name_resolver, "_ResolverWorker", return_value=worker):
                                result = CheckSIController(config, event_dispatcher=lambda _: None).run_analysis(paragraphs)
                            first, second = result["compounds"]
                            self.assertIn(fragment, first["original_text"])
                            self.assertIn(second["name"], ("", "methanol"))
                            self.assertTrue(all(name in {"ethanol", "methanol"} for _, name in worker.calls))

    def test_data_free_mass_captions_do_not_enter_the_next_source_name(self):
        ea_propanol = "Anal. Calcd for C3H8O: C, 59.96; H, 13.42; O, 26.62. Found: C, 59.96; H, 13.42; O, 26.62."
        ea_ethanol = "Anal. Calcd for C2H6O: C, 52.14; H, 13.13; O, 34.73. Found: C, 52.14; H, 13.13; O, 34.73."
        for tail in ("HRMS spectra: Figure S12", "ESI-2", "TOF MS measurements were not possible"):
            for extension in ("docx", "pdf"):
                for mode in ("off", "on"):
                    with self.subTest(tail=tail, extension=extension, mode=mode):
                        # DOCX retains EA in the native paragraph. The PDF case
                        # uses a complete source line to prove analytical context.
                        first = PROTON + " " + ea_propanol if extension == "docx" else PROTON
                        second = PROTON + " " + ea_ethanol if extension == "docx" else PROTON
                        paragraphs = self.parse_sources(["propan-1-ol (1)", first, tail,
                                                         "ethanol (2)", second], extension)
                        worker = StubWorker(lambda backend, name: {"propan-1-ol": "CCCO", "ethanol": "CCO"}.get(name))
                        with patch.object(name_resolver, "_ResolverWorker", return_value=worker):
                            result = CheckSIController(make_config(mode=mode), event_dispatcher=lambda _: None).run_analysis(paragraphs)
                        self.assertEqual([(item["name"], item["identifier"]) for item in result["compounds"]],
                                         [("propan-1-ol", "1"), ("ethanol", "2")])
                        self.assertEqual(sum(part.count(tail) for part in paragraphs), 1)
                        self.assertTrue(all(name in {"propan-1-ol", "ethanol"} for _, name in worker.calls))

    def test_mass_caption_fence_requires_preceding_analytical_context(self):
        paragraphs = self.parse_sources(["Unfinished discussion", "ESI-2", "ethanol (2)", PROTON])
        prefix, = [part for part in paragraphs if "ethanol (2)" in part]
        self.assertIn("Unfinished discussion", prefix)
        self.assertIn("ESI-2", prefix)

    def test_mass_caption_fence_does_not_scan_back_over_unproven_source_parts(self):
        parts = [PROTON, "Found: C, 59.96; H, 13.42; O, 26.62.",
                 "HRMS spectra: Figure S12", "ethanol (2)", PROTON]
        self.assertIsNone(compound_name_start(parts, 3))

    def test_a_truncated_mass_section_still_proves_analytical_context(self):
        parts = ["Unfinished discussion", "HRMS [M+H]+ calcd for C3H9O+", "ethanol (2)", PROTON]
        self.assertEqual(compound_name_start(parts, 2), 2)

    def test_an_instrument_token_inside_wrapped_text_does_not_fence_it(self):
        parts = [PROTON, "N-(ESI-2)-ethyl", "amine (2a)", PROTON]
        self.assertEqual(compound_name_start(parts, 2), 1)

    def test_property_procedure_identifiers_keep_one_name_in_both_modes(self):
        name = "4-Methylbenzaldehyde"
        for mode in ("off", "on"):
            for property_line in ("Colorless oil, route B", "colourless solid", "Colorless crystals",
                                  "Colourless solid: sample A", "Yield 85%", "Yield: 85%",
                                  "route B", "route: B", "routeB"):
                with self.subTest(mode=mode, property_line=property_line):
                    paragraphs = self.parse_sources(["ethanol (1)", PROTON, f"{name}(2a)",
                                                     property_line + " (GP1)", PROTON])
                    # Force clean-name resolution to fail so deep analysis must
                    # consider every extraction, rather than succeed before GP1.
                    worker = StubWorker(lambda backend, candidate: "CCO" if candidate == "ethanol" else None)
                    with patch.object(name_resolver, "_ResolverWorker", return_value=worker):
                        result = CheckSIController(make_config(mode=mode), event_dispatcher=lambda _: None).run_analysis(paragraphs)
                    second = result["compounds"][1]
                    self.assertEqual((second["name"], second["identifier"]), (name, "2a"))
                    self.assertTrue(all(candidate in {"ethanol", name, "4Methylbenzaldehyde"}
                                        for _, candidate in worker.calls))
                    if mode == "on":
                        self.assertIn(("local", name), worker.calls)
                    else:
                        self.assertEqual(worker.calls, [])

    def test_property_cues_do_not_refuse_color_words_or_name_substrings(self):
        for name in ("Brilliant Blue FCF", "Crystal Violet", "Oil Blue N", "Methylene Blue",
                     "colorless", "colourless", "yieldene", "router", "route"):
            for mode in ("off", "on"):
                with self.subTest(name=name, mode=mode):
                    worker = StubWorker(lambda backend, candidate: "CCO")
                    with patch.object(name_resolver, "_ResolverWorker", return_value=worker):
                        result = name_resolver.smiles_and_name_from_paragraph(
                            make_config(mode=mode), ErrorTracker(), name + " (A)", {})
                    self.assertEqual(result[1:3], (name, "A"))
                    self.assertEqual(worker.calls, [("local", name)] if mode == "on" else [])

    def test_a_property_sentence_does_not_hide_a_later_clean_name(self):
        for prefix in ("Colorless oil.", "Yield: 85%.", "route B: sample."):
            for mode in ("off", "on"):
                with self.subTest(prefix=prefix, mode=mode):
                    worker = StubWorker(lambda backend, candidate: "CCO")
                    with patch.object(name_resolver, "_ResolverWorker", return_value=worker):
                        result = name_resolver.smiles_and_name_from_paragraph(
                            make_config(mode=mode), ErrorTracker(), prefix + " ethanol (2a)", {})
                    self.assertEqual(result[1:3], ("ethanol", "2a"))
                    self.assertEqual(worker.calls, [("local", "ethanol")] if mode == "on" else [])

    def test_wrapped_catalogue_and_ethyl_names_stay_complete(self):
        names = (
            ("4-Chloro-N-(((1R,3S)-3-((1-(4-chlorophenyl)hydrazinyl)methyl)cyclopentyl)methyl)",
             "aniline", "4.1"),
            ("2-(4-Methoxyphenyl)-1-phenyl", "ethan-1-one", "2a"),
            ("ethyl", "3a-methyl-1H-indene-2-carboxylate", "3b"),
        )
        for first_half, second_half, identifier in names:
            for extension in ("docx", "pdf"):
                with self.subTest(name=second_half, extension=extension):
                    self.check_second_name(["ethanol (1)", PROTON.removesuffix("."), first_half,
                                            f"{second_half} ({identifier})", PROTON],
                                           first_half + " " + second_half, identifier, extension)

    def test_report_finding_paragraphs_do_not_become_wrapped_name_parts(self):
        first_half = "4-Chloro-N-(((1R,3S)-3-((1-(4-chlorophenyl)hydrazinyl)methyl)cyclopentyl)methyl)"
        self.check_second_name(["ethanol (1)", PROTON.removesuffix("."),
                                "❗ 40. Verify whether the compound is enantiopure or meso",
                                first_half, "aniline (4.1)", PROTON], first_half + " aniline", "4.1")

    def test_wrapped_report_prose_is_refused_without_dispatching_a_suffix(self):
        paragraphs = self.parse_sources(["ethanol (1)", PROTON.removesuffix("."),
                                        "49. No solvent or freq information 19F NMR. Check solvent list if you",
                                        "⚠", "use anny less common",
                                        "4-Methylbenzaldehyde (2a)", PROTON])
        worker = StubWorker(lambda backend, candidate: "CCO")
        with patch.object(name_resolver, "_ResolverWorker", return_value=worker):
            result = CheckSIController(make_config(), event_dispatcher=lambda _: None).run_analysis(paragraphs)
        self.assertEqual(result["compounds"][1]["name_resolution"]["reason"], "mixed_context")
        self.assertFalse(any("use anny" in candidate for _, candidate in worker.calls))

    def test_catalogue_4_1_and_4c_retain_their_complete_source_names(self):
        # Preserve the original catalogue's names and paragraph boundaries in a
        # minimal document, independently of the replaceable public examples.
        paragraphs = self.parse_sources([
            "ethanol (1)", PROTON.removesuffix("."),
            "❗ 40. Verify whether the compound is enantiopure or meso or single diastereomer. "
            "It is unlikely to be meso. If the compound is chiral, ensure that chiral HPLC or "
            "optical rotation data is provided. If it is single diastereomer change description type to RS/SR",
            "4-Chloro-N-(((1R,3S)-3-((1-(4-chlorophenyl)hydrazinyl)methyl)cyclopentyl)methyl)",
            "aniline (4.1)", "", PROTON,
            "❗ 40. Relative stereodescriptors are indicated, such as R*",
            "(1S*,4R*,5S*)-Dimethyl 3-(4-methoxybenzyl)-2-thia-3- "
            "azabicyclo[2.1.1]hexane-4,5-dicarboxylate 2,2-dioxide (4c). ",
            "", PROTON,
        ])
        worker = StubWorker(lambda backend, candidate: "CCO")
        with patch.object(name_resolver, "_ResolverWorker", return_value=worker):
            result = CheckSIController(make_config(), event_dispatcher=lambda _: None).run_analysis(paragraphs)
        compound = next(item for item in result["compounds"] if item["identifier"] == "4.1")
        self.assertEqual(compound["name"], "4-Chloro-N-(((1R,3S)-3-((1-(4-chlorophenyl)hydrazinyl)methyl)cyclopentyl)methyl) aniline")
        compound = next(item for item in result["compounds"] if item["identifier"] == "4c")
        self.assertTrue(compound["name"].startswith("(1S*,4R*,5S*)-Dimethyl"))

    def test_pdf_and_identifier_punctuation_variants_recover_bounded_name(self):
        name = "4-Methylbenzaldehyde"
        for extension in ("docx", "pdf"):
            for ending, identifier in ((" (1.73)", "1.73"), ("(1.76)", "1.76"),
                                       (" (12ab-34).", "12ab-34"), (" (1.73):", "1.73"),
                                       (" (4c). \u200b", "4c")):
                with self.subTest(extension=extension, ending=ending):
                    self.check_second_name(["ethanol (1)", PROTON.removesuffix("."),
                                            name + ending, PROTON], name, identifier, extension)

    def test_analytical_property_and_nonterminal_paragraphs_do_not_start_names(self):
        continuations = ("13C NMR (100 MHz, CDCl3): δ 120.0 (2C)",
                         "13C NMR (100 MHz, CDCl3): δ 120.0 (2a)",
                         "19F NMR (376 MHz, CDCl3): δ -60.0 (3F)",
                         "[α]D = +12 (c 1.0, CHCl3)", "Rf = 0.4 (hexane)", "mp = 100 (lit.)",
                         "4-Methylbenzaldehyde (2a) additional context")
        for continuation in continuations:
            with self.subTest(continuation=continuation):
                paragraphs = self.parse_sources(["ethanol (1)", PROTON.removesuffix("."), continuation, PROTON])
                first_description = next(part for part in paragraphs if part.startswith("1H NMR"))
                self.assertIn(continuation, first_description)

    def test_name_boundary_requires_preceding_analysis_and_following_proton_header(self):
        name = "4-Methylbenzaldehyde (2a)"
        for parts in (("Unfinished discussion", name, PROTON),
                      ("ethanol (1)", PROTON.removesuffix("."), name,
                       "13C NMR (100 MHz, CDCl3): δ 120.0")):
            with self.subTest(parts=parts):
                paragraphs = self.parse_sources(parts)
                paragraph_with_name, = [part for part in paragraphs if name in part]
                self.assertIn(parts[-3], paragraph_with_name)


class EmbeddedStereoIdentifierTests(unittest.TestCase):
    def test_embedded_stereodescriptors_never_become_additional_identifiers(self):
        for name, identifier in (("(R)-1-phenylethanol", "3a"),
                                 ("N,N'-((R)-1-phenylethyl)ethane-1,2-diamine", "5.1"),
                                 ("N-((S))ethylamine", "3j")):
            with self.subTest(name=name):
                worker = StubWorker()
                with patch.object(name_resolver, "_ResolverWorker", return_value=worker):
                    result = name_resolver.smiles_and_name_from_paragraph(
                        make_config(mode="off"), ErrorTracker(), f"{name} ({identifier})", {})
                self.assertEqual(result[1:3], (name, identifier))
                self.assertEqual(worker.calls, [])

    def test_single_uppercase_compound_identifiers_remain_supported(self):
        for identifier in ("A", "R", "S"):
            with self.subTest(identifier=identifier):
                result = name_resolver.smiles_and_name_from_paragraph(
                    make_config(mode="off"), ErrorTracker(), f"ethanol ({identifier})", {})
                self.assertEqual(result[1:3], ("ethanol", identifier))


class MultiplicityPlacementTests(unittest.TestCase):
    def test_assignment_tails_do_not_replace_the_first_descriptor(self):
        for descriptor in ("m, 3H, H-o,p", "m, 3H, H-m,p", "s, 1H, H-a,d",
                           "d, J = 8.0 Hz, 2H, H-o,m", "t, J = 7.6 Hz, 2H, m"):
            with self.subTest(descriptor=descriptor):
                tracker = ErrorTracker()
                self.assertEqual(_check_constant_hnmr(make_config(), tracker, f"δ 7.2 ({descriptor})"), [])
                self.assertEqual(active_counts(tracker), {})
        tracker = ErrorTracker()
        findings, coupled = _check_constant_nmr(make_config(), tracker, "δ 120.0 (CH, C-m,p)", "13C")
        self.assertEqual((findings, coupled, active_counts(tracker)), ([], 0, {}))

    def test_unknown_letter_fields_are_wrong_multiplicity_in_any_position(self):
        for descriptor in ("z, 1H", "a, 1H", "b, 2H", "x, 1H", "br z, 1H", "1H, z",
                           "1H, abc", "abcd, 2H", "1H, J = 8.0 Hz, xyz"):
            with self.subTest(descriptor=descriptor):
                tracker = ErrorTracker()
                _check_constant_hnmr(make_config(), tracker, f"δ 3.50 ({descriptor})")
                self.assertEqual(tracker.get_count("hnmr_multiplicity_missing"), 0)
                self.assertEqual(tracker.get_count("hnmr_wrong_multiplicity"), 1)

    def test_assignments_stop_descriptor_search_before_unknown_letters(self):
        for assignment in ("H-o,p", "C-m,p", "Ar-H, d", "Ar, z", "CH3, z", "NH, z", "OH, z", "2-H, z"):
            with self.subTest(assignment=assignment):
                tracker = ErrorTracker()
                _check_constant_hnmr(make_config(), tracker, f"δ 3.50 (2H, {assignment})")
                self.assertEqual(tracker.get_count("hnmr_multiplicity_missing"), 1)
                self.assertEqual(tracker.get_count("hnmr_wrong_multiplicity"), 0)

    def test_proton_multiplicity_in_any_whole_field_only_has_order_finding(self):
        for descriptor in (
            "5H, m, Ar-H", "1H, m, CHBr", "3H, d, J= 7.0 Hz, CH3",
            "4H, d, J = 8.0 Hz", "J = 8.4 Hz, 4H, d", "cis, 4H, d, J = 8.0 Hz",
        ):
            with self.subTest(descriptor=descriptor):
                tracker = ErrorTracker()
                findings = _check_constant_hnmr(make_config(), tracker, f"δ 3.50 ({descriptor})")
                self.assertEqual(active_counts(tracker), {"hnmr_signal_desc_invalid": 1})
                self.assertEqual(len(findings), 1)

    def test_unknown_leading_token_with_valid_multiplicity_is_order_only(self):
        tracker = ErrorTracker()
        findings = _check_constant_hnmr(
            make_config(), tracker,
            "δ 7.82 (4H, d, J = 8.0 Hz), 6.59 (J = 8.4 Hz, 4H, d), "
            "3.44 (br. s, 2H), 3.12–3.04 (z, 4H, d, J = 8.0 Hz)",
        )
        self.assertEqual(active_counts(tracker), {"hnmr_signal_desc_invalid": 1})
        self.assertEqual(len(findings), 1)

    def test_assignment_substrings_never_supply_a_multiplicity(self):
        for label in ("Ar-H", "CH3", "CHBr", "p-OMe", "H-p", "t-Bu"):
            with self.subTest(label=label):
                tracker = ErrorTracker()
                _check_constant_hnmr(make_config(), tracker, f"δ 3.50 (2H, {label})")
                self.assertEqual(tracker.get_count("hnmr_multiplicity_missing"), 1)

    def test_carbon_multiplicity_after_coupling_only_has_order_finding(self):
        tracker = ErrorTracker()
        findings, coupled = _check_constant_nmr(
            make_config(), tracker, "δ 60.0 (J = 8.4 Hz, d)", "13C",
        )
        self.assertEqual(active_counts(tracker), {"nmr_signal_desc_invalid": 1})
        self.assertEqual(len(findings), 1)
        self.assertEqual(coupled, 1)


class DocxNameBoundaryTests(unittest.TestCase):
    def test_normalized_proton_headers_keep_the_explicit_name_boundary(self):
        headers = (
            PROTON.replace("NMR (", "NMR  ("),
            PROTON.replace(": δ", ":δ"),
            PROTON.replace("NMR (", "NMR\u00a0("),
        )
        for header in headers:
            with self.subTest(header=header):
                with tempfile.TemporaryDirectory() as directory:
                    source = Path(directory) / "input.docx"
                    document = Document()
                    for text in ("ethanol (1)", "1H NMR (400 MHz, CDCl3): δ 7.04",
                                 f"{NAME} (2)", header):
                        document.add_paragraph(text)
                    document.save(source)
                    parsed = DocumentParser().parse(str(source), header_margin=0, footer_margin=0)
                    self.assertEqual(paragraphs_from_input(str(source), header_margin=0, footer_margin=0),
                                     parsed.paragraphs)
                    worker = StubWorker(lambda backend, name: SMILES if name == NAME else "CCO")
                    with patch.object(name_resolver, "_ResolverWorker", return_value=worker):
                        result = CheckSIController(make_config(), event_dispatcher=lambda _: None).run_analysis(parsed.paragraphs)
                first, second = result["compounds"]
                self.assertNotIn(NAME, first["original_text"])
                self.assertEqual((second["name"], second["identifier"], second["smiles"]), (NAME, "2", SMILES))
                self.assertEqual(second["name_resolution"]["status"], "resolved")
                self.assertFalse(any("name_not_found." in warning for warning in second["all_warnings"]))

    def test_public_docx_parser_preserves_the_same_name_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.docx"
            document = Document()
            for text in ("ethanol (1)", "1H NMR (400 MHz, CDCl3): δ 7.04", f"{NAME} (2)", PROTON):
                document.add_paragraph(text)
            document.save(source)
            parsed = DocumentParser().parse(str(source), header_margin=0, footer_margin=0)
            direct = paragraphs_from_input(str(source), header_margin=0, footer_margin=0)
            self.assertEqual(direct, parsed.paragraphs)
            worker = StubWorker(lambda backend, name: SMILES if name == NAME else "CCO")
            with patch.object(name_resolver, "_ResolverWorker", return_value=worker):
                result = CheckSIController(make_config(), event_dispatcher=lambda _: None).run_analysis(direct)
        first, second = result["compounds"]
        self.assertNotIn(NAME, first["original_text"])
        self.assertEqual((second["name"], second["identifier"], second["smiles"]), (NAME, "2", SMILES))
        self.assertEqual(second["name_resolution"]["status"], "resolved")

    def test_separate_name_survives_unpunctuated_previous_description(self):
        endings = (
            PROTON + " FT-IR (KBr): ν˜= 3499 cm-1",
            PROTON + " APCI [M+H]+ calcd for C21H29N2+",
            "1H NMR (400 MHz, (CD3)2SO) δ 7.04",
        )
        for ending, identifier in zip(endings, ("1.73", "1.76", "12ab-34")):
            with self.subTest(ending=ending):
                with tempfile.TemporaryDirectory() as directory:
                    source = Path(directory) / "input.docx"
                    document = Document()
                    for text in ("ethanol (1.72)", ending, f"{NAME} ({identifier})", "", PROTON):
                        document.add_paragraph(text)
                    document.save(source)
                    parsed = DocumentParser().parse(str(source), header_margin=0, footer_margin=0)
                    worker = StubWorker(lambda backend, name: SMILES if name == NAME else "CCO")
                    with patch.object(name_resolver, "_ResolverWorker", return_value=worker):
                        result = CheckSIController(make_config()).run_analysis(parsed.paragraphs)
                first, second = result["compounds"]
                self.assertNotIn(NAME, first["original_text"])
                self.assertEqual((second["name"], second["identifier"], second["smiles"]),
                                 (NAME, identifier, SMILES))
                self.assertEqual(second["name_resolution"]["status"], "resolved")
                self.assertIn(("local", NAME), worker.calls)
                self.assertFalse(any("name_not_found." in warning for warning in second["all_warnings"]))
                self.assertTrue(any("stereo_check_required." in warning for warning in second["all_warnings"]))
                self.assertTrue(any("h_count_mismatch." in warning for warning in second["all_warnings"]))
                self.assertEqual(second["formula_sources"]["name"]["formula"], "C21H28N2")

    def test_description_continuation_paragraphs_stay_with_their_compound(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.docx"
            document = Document()
            for text in ("ethanol (1)", PROTON, "13C NMR (100 MHz, CDCl3): δ 60.0, 20.0.",
                         "HRMS (ESI): calcd for C2H7O+ [M+H]+ 47.0491, found 47.0491."):
                document.add_paragraph(text)
            document.save(source)
            parsed = DocumentParser().parse(str(source), header_margin=0, footer_margin=0)
            direct = paragraphs_from_input(str(source), header_margin=0, footer_margin=0)
        self.assertEqual(parsed.paragraphs, direct)
        description, = [paragraph for paragraph in parsed.paragraphs if "1H NMR" in paragraph]
        self.assertIn("13C NMR", description)
        self.assertIn("HRMS", description)

    def test_line_break_within_a_source_paragraph_keeps_mixed_context_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.docx"
            document = Document()
            document.add_paragraph("ethanol (1)")
            document.add_paragraph(f"1H NMR (400 MHz, CDCl3): δ 7.04\n{NAME} (2)")
            document.add_paragraph(PROTON)
            document.save(source)
            parsed = DocumentParser().parse(str(source), header_margin=0, footer_margin=0)
            worker = StubWorker(lambda backend, name: "CCO")
            with patch.object(name_resolver, "_ResolverWorker", return_value=worker):
                result = CheckSIController(make_config()).run_analysis(parsed.paragraphs)
        first, second = result["compounds"]
        self.assertIn(NAME, first["original_text"])
        self.assertEqual(second["name_resolution"]["reason"], "mixed_context")
        self.assertNotIn(("local", NAME), worker.calls)


class StereoDuplicateTests(unittest.TestCase):
    def test_a_separate_valid_single_center_group_keeps_its_omission_finding(self):
        tracker = ErrorTracker()
        findings = _check_streo_desc(make_config(), tracker, "(RS,SR)-x-(RS)-y", 100, 0, 1, "CC(O)C(=O)O")
        self.assertEqual([item.variant for item in findings
                          if isinstance(item, Finding) and item.code == ErrorCode.STEREO_DESCRIPTOR_INVALID],
                         ["invalid_type", "can_be_omitted"])
        self.assertTrue(any(isinstance(item, AnalysisNote)
                            and "sample configuration is unspecified" in item.text
                            for item in findings))

    def test_a_separate_singleton_group_keeps_its_multicenter_type_finding(self):
        for descriptor in ('(RS,SR)-x-(RS)-y', '(R,S)-x-(R)-y', '(R*,S*)-x-(R*)-y'):
            for severity in ('disabled', 'info', 'warning', 'critical'):
                with self.subTest(descriptor=descriptor, severity=severity):
                    tracker = ErrorTracker()
                    findings = _check_streo_desc(make_config({'stereo_descriptor_invalid': severity}),
                                                 tracker, descriptor, 100, 0, 2, SMILES)
                    variants = [item.variant for item in findings if item.code == ErrorCode.STEREO_DESCRIPTOR_INVALID]
                    self.assertEqual(variants, [] if severity == 'disabled' else
                                     ['invalid_type', 'descriptor_type_incorrect'])
                    self.assertEqual(tracker.get_count('stereo_descriptor_invalid'),
                                     0 if severity == 'disabled' else 2)
                    self.assertEqual([item.variant for item in tracker.occurrences
                                      if item.code == ErrorCode.STEREO_DESCRIPTOR_INVALID],
                                     ['invalid_type', 'descriptor_type_incorrect'])

    def test_invalid_unnumbered_group_has_one_precise_finding_at_one_center(self):
        for descriptor in ("(RS,SR)", "(RS,SR,RS)"):
            for severity in ("disabled", "info", "warning", "critical"):
                with self.subTest(descriptor=descriptor, severity=severity):
                    tracker = ErrorTracker()
                    findings = _check_streo_desc(make_config({"stereo_descriptor_invalid": severity}),
                                                 tracker, descriptor + "-example", 100, 0, 1, "CC(O)C(=O)O")
                    invalid = [item for item in findings if item.code == ErrorCode.STEREO_DESCRIPTOR_INVALID]
                    self.assertEqual([item.variant for item in invalid], [] if severity == "disabled" else ["invalid_type"])
                    self.assertEqual(len(tracker.occurrences), 1)

    def test_descriptor_type_incorrect_is_retained_for_standalone_descriptors(self):
        for descriptor in ("(R)", "(RS)", "(S*)", "(R)-x-(S)-y"):
            with self.subTest(descriptor=descriptor):
                tracker = ErrorTracker()
                findings = _check_streo_desc(make_config(), tracker, descriptor, 100, 0, 2, SMILES)
                self.assertEqual([item.variant for item in findings if item.code == ErrorCode.STEREO_DESCRIPTOR_INVALID],
                                 ["descriptor_type_incorrect"])

    def test_named_meso_fixture_has_opposite_cip_labels_and_mirror_isomorphism(self):
        molecule = Chem.MolFromSmiles(SMILES)
        self.assertEqual(sorted(label for _, label in Chem.FindMolChiralCenters(molecule)), ["R", "S"])
        mirror = Chem.Mol(molecule)
        for atom in mirror.GetAtoms():
            atom.InvertChirality()
        self.assertEqual(Chem.MolToSmiles(molecule, isomericSmiles=True),
                         Chem.MolToSmiles(mirror, isomericSmiles=True))

    def test_one_invalid_descriptor_has_one_finding_and_counter(self):
        name = NAME.replace("(1R,3S)", "(RS,SR)")
        for severity in ("disabled", "info", "warning", "critical"):
            with self.subTest(severity=severity):
                tracker = ErrorTracker()
                findings = _check_streo_desc(
                    make_config({"stereo_descriptor_invalid": severity}), tracker, name, 100, 0, 2, SMILES,
                )
                invalid = [finding for finding in findings if finding.code == ErrorCode.STEREO_DESCRIPTOR_INVALID]
                expected = int(severity != "disabled")
                self.assertEqual(len(invalid), expected)
                self.assertEqual(tracker.get_count("stereo_descriptor_invalid"), expected)
                self.assertEqual(len(tracker.occurrences), 1)
                if invalid:
                    self.assertEqual(invalid[0].variant, "invalid_type")
