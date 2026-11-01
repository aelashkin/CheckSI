"""Real controller behavior with only name resolution replaced."""

import unittest
import threading
from unittest.mock import patch

from checksi.config_schema import AppConfig
from checksi.controller import CheckSIController
from checksi.errors import ErrorCode
from checksi.errors.findings import AnalysisNote
from checksi.errors.manager import emit_finding


DESCRIPTION = "1H NMR (400 MHz, CDCl3): δ 3.50 (s, 2H), 1.20 (s, 6H). 13C NMR (100 MHz, CDCl3): δ 60.0, 20.0."


class ControllerTests(unittest.TestCase):
    """Keep invalid structures local to one compound and isolate runs."""

    def setUp(self):
        self.cfg = AppConfig.model_validate({
            "paths": {"input_path": "input.pdf", "output_dir": "."},
            "modes": {"deep_name_analysis": "on", "output_mode": "essential"},
            "solvents": {"entries": [{"name": "CDCl3", "aliases": [], "proton_shifts": [7.26], "carbon_shifts": [77.0]}]},
            "errors": {"default_severity": "warning", "overrides": {}},
        })

    def test_invalid_resolver_smiles_is_a_finding_and_next_compound_continues(self):
        def resolve(cfg, tracker, paragraph, stats, **kwargs):
            kwargs["details"].update(status="transformed", accepted_name="propan-1-ol",
                                    transformations=[{"kind": "isolate_name_segment"}, {"kind": "normalize_whitespace"}])
            return ("invalid-smiles" if paragraph == "Bad (1)" else "CCCO", "propan-1-ol", "1", stats, [AnalysisNote("resolver diagnostic")])

        controller = CheckSIController(self.cfg)
        with patch("checksi.controller._smiles_and_name_from_paragraph", side_effect=resolve):
            result = controller.run_analysis(["Bad (1)", DESCRIPTION, "Good (2)", DESCRIPTION])
        self.assertFalse(result["aborted"])
        self.assertEqual(result["total_found"], 2)
        bad, good = result["compounds"]
        self.assertIsNone(bad["smiles"])
        self.assertIsNone(bad["name_resolution"]["accepted_name"])
        # The shown name still came from isolation; only the accepted-name edits are dropped.
        self.assertEqual(bad["name_resolution"]["transformations"], [{"kind": "isolate_name_segment"}])
        self.assertEqual(good["smiles"], "CCCO")
        self.assertIn("resolver diagnostic", bad["all_warnings"])
        self.assertTrue(any("name_to_smiles_failed." in line for line in bad["all_warnings"]))
        self.assertEqual(result["error_stats"]["name_to_smiles_failed"], 1)
        self.assertFalse(bad["run_dp5"])

    def test_controller_repeated_runs_do_not_share_findings_or_compounds(self):
        controller = CheckSIController(self.cfg)
        with patch("checksi.controller._smiles_and_name_from_paragraph", return_value=(None, None, None, {}, [])):
            first = controller.run_analysis(["Name", DESCRIPTION])
            second = controller.run_analysis([])
        self.assertEqual(first["total_found"], 1)
        self.assertEqual(second["total_found"], 0)
        self.assertEqual(second["compounds"], [])
        self.assertTrue(all(value == 0 for value in second["error_stats"].values()))

    def test_accepted_header_grammar_is_characterized_without_expansion(self):
        controller = CheckSIController(self.cfg)
        self.assertEqual(controller.run_analysis([DESCRIPTION])["total_found"], 1)
        self.assertEqual(controller.run_analysis([DESCRIPTION.replace("400 MHz", "400MHz")])["total_found"], 0)

    def test_resolver_cancellation_preserves_completed_compounds_and_stops_analysis(self):
        token = threading.Event()
        def resolve(cfg, tracker, paragraph, stats, **kwargs):
            if paragraph == "Cancel (2)":
                self.assertIs(kwargs["cancel_token"], token)
                kwargs["details"].update(status="cancelled")
                token.set()
            else:
                kwargs["details"].update(status="resolved", original_name=paragraph, accepted_name=paragraph)
            return None, paragraph, "1", stats, []
        controller = CheckSIController(self.cfg)
        with patch("checksi.controller._smiles_and_name_from_paragraph", side_effect=resolve), \
                patch.object(controller, "_analyze_description_diagnostics", wraps=controller._analyze_description_diagnostics) as analyze:
            result = controller.run_analysis(["Done (1)", DESCRIPTION, "Cancel (2)", DESCRIPTION], cancel_token=token)
        self.assertTrue(result["aborted"])
        self.assertEqual(result["total_found"], 1)
        self.assertEqual(len(result["compounds"]), 1)
        self.assertEqual(result["compounds"][0]["name_resolution"]["status"], "resolved")
        self.assertEqual(analyze.call_count, 1)
        self.assertEqual(result["error_stats"]["name_to_smiles_failed"], 0)

    def test_late_resolver_cancellation_discards_findings_and_name_statistics(self):
        token = threading.Event()
        def resolve(cfg, tracker, paragraph, stats, **kwargs):
            finding = emit_finding(cfg, tracker, ErrorCode.NAME_TO_SMILES_FAILED,
                                   variant="resolve_failed")
            stats["numeric_id_match_count"] += 1
            if paragraph == "Cancel (2)":
                token.set()
            return None, paragraph, "1", stats, [finding] if finding is not None else []
        controller = CheckSIController(self.cfg)
        with patch("checksi.controller._smiles_and_name_from_paragraph", side_effect=resolve):
            result = controller.run_analysis(["Done (1)", DESCRIPTION, "Cancel (2)", DESCRIPTION], cancel_token=token)
        self.assertEqual(result["error_stats"]["name_to_smiles_failed"], 1)
        self.assertEqual(result["name_finding_stats"]["numeric_id_match_count"], 1)

    def test_refused_mixed_prefix_has_one_finding_and_no_stereo_or_annotation(self):
        prefix = "Et3N (0.24 mmol) was added to a solution of acyloxyazirine"
        for mode in ("on", "off"):
            with self.subTest(mode=mode):
                cfg = self.cfg.model_copy(deep=True)
                cfg.modes.deep_name_analysis = mode
                controller = CheckSIController(cfg)
                with patch("checksi.analysis.name_resolver._ResolverWorker", side_effect=AssertionError("backend started")), \
                        patch("checksi.controller._check_streo_desc", side_effect=AssertionError("mixed name reached stereo")):
                    result = controller.run_analysis([prefix + " (3)", DESCRIPTION])
                compound = result["compounds"][0]
                self.assertEqual(compound["name"], "")
                self.assertIsNone(compound["highlight_info"]["name_text"])
                self.assertEqual(compound["name_resolution"]["original_name"], prefix)
                self.assertEqual(compound["name_resolution"]["reason"], "mixed_context")
                self.assertEqual((result["error_stats"]["name_not_found"], result["error_stats"]["name_to_smiles_failed"]), (1, 0))
                self.assertIn("could not be isolated", compound["all_warnings"][0])

    def test_disabled_deep_analysis_uses_isolated_name_without_lookup_findings(self):
        prefix = "IR: 1710 cm-1. reference compound was (R)-lactic acid. ethanol"
        cfg = self.cfg.model_copy(deep=True)
        cfg.modes.deep_name_analysis = "off"
        with patch("checksi.analysis.name_resolver._ResolverWorker", side_effect=AssertionError("backend started")):
            result = CheckSIController(cfg).run_analysis([prefix + " (2)", DESCRIPTION])
        compound = result["compounds"][0]
        self.assertEqual(compound["name"], "ethanol")
        self.assertEqual(compound["highlight_info"]["name_text"], "ethanol (2)")
        self.assertEqual(compound["name_resolution"]["original_name"], prefix)
        self.assertEqual((result["error_stats"]["name_not_found"], result["error_stats"]["name_to_smiles_failed"]), (0, 0))
