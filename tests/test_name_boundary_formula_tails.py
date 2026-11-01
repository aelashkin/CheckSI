"""Keep physical formula fragments with their declared analytical source."""

import time
import unittest
from unittest.mock import patch

from checksi.analysis import name_resolver
from checksi.controller import CheckSIController
from checksi.utils import name_boundaries
from tests import test_name_boundary_bracket_tails as native
from tests.test_name_boundary_integrity import NMR
from tests.test_name_resolver import StubWorker, make_config
from tests.test_wave3_regressions import PROTON


BODY = NMR.replace(". 13C", "; 13C").removesuffix(". ") + "; "


class FormulaTailNameOwnershipTests(unittest.TestCase):
    def test_long_numeric_formula_fragments_have_bounded_work(self):
        started = time.perf_counter()
        for suffix, expected in (('', True), ('_opaque', False)):
            self.assertEqual(name_boundaries._formula_fragment('C' + '9' * 12000 + suffix), expected)
        self.assertLess(time.perf_counter() - started, 0.75)

    def test_formula_tails_preserve_previous_sources_and_findings(self):
        helper = native.BracketTailSourceTests()
        cases = (
            ("hrms", "HRMS [M+H]+ calcd for", ["C3H9O+"], "C3H9O"),
            ("hrms", "HRMS [M+H]+ calcd for", ["C3H9O(H2O)+"], None),
            ("hrms", "HRMS [M+H]+ calcd for", ["C3H9[18O]+"], "C3H9[18O]"),
            ("ea", "Anal Calcd for", ["C3H8O"], "C3H8O"),
            ("ea", "Anal Calcd for", ["C3H8O(H2O)"], None),
            ("ea", "Anal. Calcd for", ["C3H8O", "C, 59.96; H, 13.42; O, 26.62. Found: C, 59.96; H, 13.42; O, 26.62"], "C3H8O"),
            ("ea", "Anal Calcd for", ["C3H8O", "hydrochloride"], None),
        )
        for source, cue, fragments, formula in cases:
            for extension in ("docx", "pdf"):
                parts = ["propan-1-ol (1.75)", BODY + cue, *fragments]
                baseline_parts = helper.parse(parts, extension)
                paragraphs = helper.parse(parts + ["ethanol (1.76)", PROTON], extension)
                for mode in ("off", "on"):
                    for severity in ("warning", "disabled"):
                        with self.subTest(source=source, fragments=fragments, extension=extension, mode=mode, severity=severity):
                            baseline, _ = helper.analyze(baseline_parts, mode, severity)
                            result, calls = helper.analyze(paragraphs, mode, severity)
                            previous, = baseline["compounds"]
                            first, second = result["compounds"]
                            self.assertIsNotNone(previous["formula_sources"][source]["candidate"])
                            self.assertEqual(previous["formula_sources"][source]["formula"], formula)
                            self.assertIn(" ".join(fragments), previous["original_text"])
                            self.assertEqual(first["original_text"].strip(), previous["original_text"].strip())
                            self.assertEqual(first["formula_sources"], previous["formula_sources"])
                            self.assertEqual(first["all_warnings"], previous["all_warnings"])
                            self.assertEqual((second["name"], second["identifier"]), ("ethanol", "1.76"))
                            self.assertTrue(all(name in {"propan-1-ol", "ethanol"} for _, name in calls))

    def test_formula_candidate_is_not_mistaken_for_a_name_identifier(self):
        for formula in ("C3H8O(H2O)", "C3H8O(2)"):
            parts = [BODY + "Anal Calcd for", formula, PROTON]
            with self.subTest(formula=formula):
                self.assertIsNone(name_boundaries.compound_name_start(parts, 1))

    def test_formula_ownership_requires_mass_declaration_and_preserves_chemical_wraps(self):
        for prefix, name in (("C60", "fullerene"), ("(R)-1-phenyl", "ethanol"),
                             ("1H-indole-3-", "carboxylic acid"), ("N-methyl", "propan-1-amine"),
                             ("sodium", "acetate")):
            parts = [PROTON, prefix, name + " (2a)", PROTON]
            with self.subTest(prefix=prefix):
                self.assertEqual(name_boundaries.compound_name_start(parts, 2), 1)
                if prefix != "C60":
                    parts[0] = BODY + "HRMS [M+H]+ calcd for"
                    self.assertEqual(name_boundaries.compound_name_start(parts, 2), 1)
        for cue in (PROTON, PROTON + " calcd for", BODY + "HRMS spectra: Figure S12",
                    BODY + "HRMS calcd for C3H9O+ 61.0648; found 61.0648"):
            parts = [cue, "C60", "fullerene (2a)", PROTON]
            with self.subTest(cue=cue):
                self.assertEqual(name_boundaries.compound_name_start(parts, 2), 1)
        self.assertEqual(name_boundaries.compound_name_start(
            ["HRMS calcd for", PROTON, "C60", "fullerene (2a)", PROTON], 3), 2)

    def test_ethyl_name_wrap_after_formula_is_preserved(self):
        helper = native.BracketTailSourceTests()
        name = "ethyl acetate"
        for extension in ("docx", "pdf"):
            with self.subTest(extension=extension):
                paragraphs = helper.parse(["propan-1-ol (1.75)", BODY + "HRMS [M+H]+ calcd for", "C3H9O+",
                                           "ethyl", "acetate (1.76)", PROTON], extension)
                worker = StubWorker(lambda backend, candidate: "CCO" if candidate in {"propan-1-ol", name} else None)
                with patch.object(name_resolver, "_ResolverWorker", return_value=worker):
                    result = CheckSIController(make_config(), event_dispatcher=lambda _: None).run_analysis(paragraphs)
                first, second = result["compounds"]
                self.assertEqual(first["formula_sources"]["hrms"]["formula"], "C3H9O")
                self.assertEqual((second["name"], second["identifier"]), (name, "1.76"))
                self.assertTrue(all(candidate in {"propan-1-ol", name} for _, candidate in worker.calls))

    def test_formula_context_work_is_bounded_and_memoized(self):
        parts = [PROTON, *(["hydrate"] * 1000), "ethanol (2)", PROTON]
        with patch.object(name_boundaries, "_formula_fragment", wraps=name_boundaries._formula_fragment) as scanned:
            self.assertEqual(name_boundaries.compound_name_start(parts, 1001), 1)
        self.assertLessEqual(scanned.call_count, 3 * len(parts))
        for parts in (
            [BODY + "HRMS calcd for", "C" + "H2" * 9000, "ethanol (2)", PROTON],
            [BODY + "HRMS calcd for " + "x" * 17000, "C3H9O+", "ethanol (2)", PROTON],
        ):
            with self.subTest(lengths=[len(part) for part in parts]):
                self.assertIsNone(name_boundaries.compound_name_start(parts, 2))
