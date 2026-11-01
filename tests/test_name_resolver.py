"""Deterministic resolver policy and real private-worker regressions."""

import inspect
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from docx import Document

from checksi.analysis import name_resolver as resolver
from checksi.config_schema import AppConfig
from checksi.file_io import paragraphs_from_input
from checksi.utils.error_handler import ErrorTracker


def make_config(overrides=None, mode="on"):
    return AppConfig.model_validate({
        "paths": {"input_path": "input.pdf", "output_dir": "."},
        "modes": {"deep_name_analysis": mode, "output_mode": "essential"},
        "solvents": {"entries": [{"name": "CDCl3", "aliases": [], "proton_shifts": [7.26], "carbon_shifts": [77.0]}]},
        "errors": {"default_severity": "warning", "overrides": overrides or {}},
    })


class StubWorker:
    def __init__(self, resolve=None, cleanup=True):
        self.resolve = resolve or (lambda backend, name: None)
        self.calls = []
        self.cleanup = cleanup
        self.closed = 0

    def request(self, backend, name, budget):
        budget.check()
        self.calls.append((backend, name))
        result = self.resolve(backend, name)
        budget.check()
        return result if isinstance(result, dict) else {"status": "resolved" if result else "unresolved", "smiles": result}

    def close(self):
        self.closed += 1
        return self.cleanup


class ResolverRegressionTests(unittest.TestCase):
    def resolve(self, paragraph, worker=None, config=None, **kwargs):
        details = {}
        with patch.object(resolver, "_ResolverWorker", return_value=worker or StubWorker()):
            result = resolver.smiles_and_name_from_paragraph(
                config or make_config(), ErrorTracker(), paragraph, {}, details=details, **kwargs,
            )
        return result, details

    def test_failed_full_name_cannot_resolve_as_arbitrary_chemical_suffix(self):
        worker = StubWorker(lambda backend, name: "CC(=O)[O-]" if name == "acetate" else None)
        result, _ = self.resolve("ethly acetate (1)", worker)
        self.assertIsNone(result[0])
        self.assertEqual(result[1], "ethly acetate")
        self.assertNotIn(("local", "acetate"), worker.calls)

    def test_clean_multiword_names_preserve_all_chemical_content(self):
        # Complete names from the bundled article, including its multiword ester.
        for name in ("tert-Butyl 2-bromo-3-phenyl-2H-azirine-2-carboxylate",
                     "3-Phenyl-2-(pyrrolidine-1-carbonyl)-2H-azirin-2-yl 2-(4-nitrophenyl)acetate",
                     "alpha beta gamma delta epsilon acetate",
                     "sodium (2R)-2-hydroxypropanoate monohydrate",
                     "methyl 2-bromo-3-phenyl-2H-azirine-2-carboxylate",
                     "Methyl 2-bromo-3-(4-fluorophenyl)-2H-azirine-2-carboxylate",
                     "methyl 4-bromobenzoate", "δ-valerolactone", "Δ2-oxazoline",
                     "Methyl 4a,5,6,7-tetrahydro-2H-chromene-3-carboxylate", "ethyl 3a-methyl-1H-indene-2-carboxylate"):
            worker = StubWorker(lambda backend, candidate: "CC")
            result, details = self.resolve(name + " (1)", worker)
            self.assertEqual(result[:3], ("CC", name, "1"))
            self.assertEqual(worker.calls, [("local", name)])
            self.assertEqual(details["status"], "resolved")
            self.assertEqual(details["transformations"], [])

    def test_recognized_prose_and_mechanical_edits_keep_compact_ordered_provenance(self):
        worker = StubWorker(lambda backend, name: "CCO" if name == "ethylacetate" else None)
        original = "Preparation of  ethyl– acetate."
        result, details = self.resolve(original + " (A)", worker,
                                       make_config({"name_modified_for_smiles": "disabled"}))
        self.assertEqual(len(result), 5)
        self.assertEqual(result[:3], ("CCO", original, "A"))
        self.assertEqual(result[4], [])
        self.assertEqual(details["status"], "transformed")
        self.assertEqual(details["original_name"], original)
        self.assertEqual(details["accepted_name"], "ethylacetate")
        self.assertEqual(details["transformations"], [{"kind": kind} for kind in (
            "normalize_whitespace", "normalize_dashes", "join_hyphen_whitespace",
            "remove_terminal_period", "remove_recognized_prose", "remove_single_hyphen",
        )])
        self.assertEqual(set(details), {"status", "reason", "original_name", "accepted_name", "identifier", "transformations"})

    def test_changed_name_emits_modification_finding(self):
        result, _ = self.resolve("Synthesis of ethanol (1)", StubWorker(lambda backend, name: "CCO" if name == "ethanol" else None))
        self.assertTrue(any("name_modified_for_smiles." in warning for warning in result[4]))

    def test_real_fixture_names_are_isolated_from_labels_and_sentences(self):
        dimethylaniline = "N,N'-(((1R,3S)-cyclopentane-1,3-diyl)bis(methylene))bis(4-methylaniline)"
        cases = (
            # data/test_real_article.pdf: SI page labels, list numbering and sentence/footnote ends.
            ("S11 Methyl 2-amino-3-(2,3-dihydrobenzo[b][1,4]dioxin-6-yl)-4-(4-nitrophenyl)-5-oxo-2,5- dihydrofuran-2-carboxylate",
             "Methyl 2-amino-3-(2,3-dihydrobenzo[b][1,4]dioxin-6-yl)-4-(4-nitrophenyl)-5-oxo-2,5- dihydrofuran-2-carboxylate"),
            ("S25 4-Bromo-5-(tert-butoxy)-3-phenylisoxazole", "4-Bromo-5-(tert-butoxy)-3-phenylisoxazole"),
            ("S5 4. Synthesis of 2-bromo-2H-azirines 1d,n,p 2-Bromo-2H-azirines 1d,n,p were synthesized from "
             "4-bromoisoxazoles 12d,n,p and FeCl2·4H2O according to the reported procedure1. "
             "Methyl 2-bromo-3-(4-fluorophenyl)-2H-azirine-2-carboxylate",
             "Methyl 2-bromo-3-(4-fluorophenyl)-2H-azirine-2-carboxylate"),
            ("S24 8. Synthesis of 4-bromoisoxazole 12d,h,n,p 4-Bromoisoxazoles 12d,h,n,p were synthesized using "
             "N-bromosuccinimide according to the reported procedure.7 4-Bromo-3-(4-fluorophenyl)-5-methoxyisoxazole",
             "4-Bromo-3-(4-fluorophenyl)-5-methoxyisoxazole"),
            # A heading ends at its standalone compound label ("3c").
            ("S6 5. Synthesis of azirine 3c 3-Phenyl-2-(pyrrolidine-1-carbonyl)-2H-azirin-2-yl 2-(4-nitrophenyl)acetate",
             "3-Phenyl-2-(pyrrolidine-1-carbonyl)-2H-azirin-2-yl 2-(4-nitrophenyl)acetate"),
            # data/test_pdf_offline.pdf: text of an already annotated CheckSI report.
            ("If the compound is chiral, ensure that chiral HPLC or optical rotation data is provided. comment id 1 "
             + dimethylaniline, dimethylaniline),
            ("❗ Check equivalent C: 21 - 10 = 11 comment id 17 " + dimethylaniline, dimethylaniline),
            ("⚠ 52. Cyrillic letters found in the text С, Н parsed name: " + dimethylaniline, dimethylaniline),
        )
        for prefix, name in cases:
            for mode in ("on", "off"):
                with self.subTest(prefix=prefix, mode=mode):
                    worker = StubWorker(lambda backend, candidate: "CC" if candidate == name else None)
                    result, details = self.resolve(prefix + " (4i)", worker, make_config(mode=mode))
                    self.assertEqual(result[1:3], (name, "4i"))
                    self.assertEqual(result[4], [])
                    self.assertEqual(details["original_name"], prefix)
                    self.assertEqual(details["transformations"], [{"kind": "isolate_name_segment"}])
                    if mode == "on":
                        self.assertEqual(result[0], "CC")
                        self.assertEqual(worker.calls, [("local", name)])
                        self.assertEqual((details["status"], details["accepted_name"]), ("resolved", name))
                    else:
                        self.assertEqual(worker.calls, [])
                        self.assertEqual(details["status"], "disabled")

    def test_annotated_report_text_is_used_only_after_tool_labels(self):
        # Shortened data/test_pdf_offline.pdf paragraphs: CheckSI report prose before names.
        dimethylaniline = "N,N'-(((1R,3S)-cyclopentane-1,3-diyl)bis(methylene))bis(4-methylaniline)"
        meso = "N,N'-(((R)-cyclopentane-1,3-diyl)bis(methylene))bis(4-methylaniline)"
        cases = (
            (f"PDF error checker {dimethylaniline} (5.1)", dimethylaniline, "5.1"),
            (f"comment id 31 {dimethylaniline} (5.32) ⚠ 57. Incorrect order of atoms in the formula C21N2H29: C, N, H "
             "⚠ Note: According to IUPAC rules, elements should appear in the following order: Carbon (C), "
             "Hydrogen (H), and then the remaining elements in alphabetical order. If the compound is chiral, "
             f"ensure that chiral HPLC or optical rotation data is provided. comment id 32 {dimethylaniline} (5.33)",
             dimethylaniline, "5.33"),
            (f"⚠ 2. Invalid type of the stereodescription. comment id 39 {meso} (5.40)", meso, "5.40"),
            ("⚠ 5. Invalid number format in calculated composition (N)", "", ""),
            ("[α]D 20= -96.7 (c = 1.92, CH2Cl2) ; ee = 86% (S). parsed name: (S)-1-(1-bromoethyl) benzene (4.47)",
             "(S)-1-(1-bromoethyl) benzene", "4.47"),
        )
        for paragraph, name, identifier in cases:
            for mode in ("on", "off"):
                with self.subTest(paragraph=paragraph[:40], mode=mode):
                    worker = StubWorker()
                    result, details = self.resolve(paragraph, worker, make_config(mode=mode))
                    self.assertEqual(result[1], name)
                    if name:
                        self.assertEqual((result[2], details["transformations"]),
                                         (identifier, [{"kind": "isolate_name_segment"}]))
                    else:
                        self.assertEqual((details["status"], details["reason"]), ("refused", "mixed_context"))
                    # Every dispatch is a variant of the name, never report prose.
                    self.assertLessEqual({candidate[:3] for _, candidate in worker.calls}, {name[:3]})

    def test_locant_colons_survive_document_extraction(self):
        names = ("4'-(4-Bromophenyl)-2,2':6',2''-terpyridine", "5,5''-Dibromo-2,2':5',2''-terthiophene",
                 "4,8-Bis(octyloxy)benzo[1,2-b:4,5-b']dithiophene", "Dithieno[3,2-b:2',3'-d]thiophene",
                 "1,2,3,4,10,10-Hexachloro-1,4,4a,5,8,8a-hexahydro-1,4:5,8-dimethanonaphthalene")
        description = "1H NMR (400 MHz, CDCl3): δ 3.50 (s, 2H), 1.20 (s, 6H). 13C NMR (100 MHz, CDCl3): δ 60.0, 20.0."
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "colon-names.docx"
            document = Document()
            for index, name in enumerate(names + ("Compound 3a:methyl benzoate",), start=3):
                document.add_paragraph(f"{name} ({index})")
                document.add_paragraph(description)
            document.save(path)
            paragraphs = paragraphs_from_input(str(path), header_margin=0, footer_margin=0)
        name_paragraphs = [paragraph for paragraph in paragraphs if not paragraph.startswith("1H NMR")]
        self.assertEqual(len(name_paragraphs), len(names) + 1)
        for paragraph in name_paragraphs:
            with self.subTest(paragraph=paragraph):
                worker = StubWorker()
                result, details = self.resolve(paragraph, worker)
                expected = paragraph.rsplit(" (", 1)[0]  # Extraction adds a space after each colon.
                if expected.startswith("Compound 3a"):
                    self.assertEqual((result[1], details["transformations"]),
                                     ("methyl benzoate", [{"kind": "isolate_name_segment"}]))
                else:
                    self.assertEqual((result[1], details["transformations"]), (expected, []))
                    self.assertEqual(worker.calls[0], ("local", expected))

    def test_salt_semicolons_and_hydrate_dots_do_not_split_names(self):
        for name, suffix in (("sodium; 2-hydroxypropanoate", "2-hydroxypropanoate"), ("NaOAc.3 H2O", "H2O")):
            with self.subTest(name=name):
                worker = StubWorker(lambda backend, candidate: "O" if candidate == suffix else None)
                result, details = self.resolve(name + " (3)", worker)
                self.assertEqual((result[0], result[1], details["transformations"]), (None, name, []))
                self.assertNotIn(("local", suffix), worker.calls)

    def test_whitespace_runs_do_not_exhaust_the_boundary_search(self):
        paragraph = ("ethyl" + " " * 60) * 400 + "ethanol (3)"
        _, details = self.resolve(paragraph, config=make_config(mode="off"))
        self.assertEqual((details["status"], details["reason"]), ("refused", "name_size_limit"))

    def test_mixed_clauses_are_refused_without_dispatch_or_lookup_findings(self):
        prefixes = (
            # data/test_real_article.pdf procedure wording ending at a compound number.
            "Et3N (0.24 mmol) was added to a solution of acyloxyazirine",
            "The reaction mixture was stirred for 12 h and concentrated to give ethyl acetate",
            "Colorless solid. The product was purified by chromatography to give ethyl acetate",
            "20 mL ethyl acetate",
            "12 h ethyl acetate",
            "A reaction of ethanol gave ethyl acetate",
            "An aliquot was dried to obtain ethyl acetate",
            "Ethyl acetate was obtained from the catalyst screen",
            "Contact author@example.test ethyl acetate",
            "δ = 7.26 ethyl acetate",
            "S3 The solution in dichloromethane produced ethyl acetate",
        )
        for prefix in prefixes:
            for mode in ("on", "off"):
                with self.subTest(prefix=prefix, mode=mode):
                    worker = StubWorker(lambda backend, name: "CCO")
                    result, details = self.resolve(prefix + " (3)", worker, make_config(mode=mode))
                    self.assertEqual(worker.calls, [])
                    self.assertEqual(result[1], "")
                    self.assertEqual(result[4], [])
                    self.assertEqual((details["status"], details["reason"]), ("refused", "mixed_context"))
                    self.assertEqual(details["original_name"], prefix)
                    self.assertEqual(details["transformations"], [])

    def test_mixed_extraction_falls_back_to_a_clean_extraction(self):
        paragraph = "methyl 4-bromobenzoate (1a) was stirred overnight (2)"
        for mode in ("on", "off"):
            with self.subTest(mode=mode):
                worker = StubWorker(lambda backend, name: "CC" if name == "methyl 4-bromobenzoate" else None)
                stats = {}
                details = {}
                with patch.object(resolver, "_ResolverWorker", return_value=worker):
                    result = resolver.smiles_and_name_from_paragraph(
                        make_config(mode=mode), ErrorTracker(), paragraph, stats, details=details)
                self.assertEqual(result[1:3], ("methyl 4-bromobenzoate", "1a"))
                self.assertEqual(details["original_name"], "methyl 4-bromobenzoate")
                self.assertEqual(result[4], [])
                if mode == "on":
                    self.assertEqual(result[0], "CC")
                    self.assertEqual(worker.calls, [("local", "methyl 4-bromobenzoate")])
                    self.assertEqual(stats, {"alphanumeric_id_match_count": 1})
                else:
                    self.assertEqual((worker.calls, details["status"]), ([], "disabled"))

    def test_heuristic_searches_are_deadline_bounded(self):
        # Each input reaches its heuristic regex, which must run with the deadline timeout.
        for name, method, paragraph in (("_MIXED_CONTEXT", "search", "ethanol (1)"),
                                        ("_LAST_BOUNDARY", "search", "Colorless oil. ethanol (1)"),
                                        ("_LAST_TOOL_LABEL", "search", "⚠ 1. comment id 1 ethanol (1)"),
                                        ("_PAGE_LABELS", "match", "S6 ethanol (1)"),
                                        ("_PROSE_PREFIX", "match", "ethanol (1)")):
            with self.subTest(pattern=name):
                bounded = unittest.mock.Mock(side_effect=TimeoutError)
                worker = StubWorker()
                with patch.object(resolver, name, unittest.mock.Mock(**{method: bounded})):
                    result, details = self.resolve(paragraph, worker)
                self.assertEqual((details["status"], details["reason"]), ("refused", "extraction_work_limit"))
                self.assertEqual((result[4], worker.calls), ([], []))
                self.assertLessEqual(bounded.call_args.kwargs["timeout"], resolver.POLL_SECONDS)
                self.assertTrue(bounded.call_args.kwargs["concurrent"])

    def test_fourth_real_extraction_is_tried_before_earlier_variants_fill_cap(self):
        paragraph = "Synthesis of a–b(1a)-c(1)-d(A1)-e (19-Z)"
        extracted = list(resolver._extracted_names(paragraph, resolver._Budget(30, None)))
        self.assertEqual(len(extracted), 4)
        fourth_original = extracted[3][0]
        for accepted in (fourth_original, fourth_original.replace("–", "-")):
            worker = StubWorker(lambda backend, name: "CC" if name == accepted else None)
            result, details = self.resolve(paragraph, worker)
            self.assertEqual(result[0], "CC")
            self.assertEqual(details["accepted_name"], accepted)
            self.assertEqual([name for backend, name in worker.calls if backend == "local"][:4], [item[0] for item in extracted])

    def test_later_original_and_mechanical_extractions_precede_speculative_hyphens(self):
        for later in ("4-Methylbiphenyl", "4–Methylbiphenyl"):
            worker = StubWorker(lambda backend, name: "CC" if name == "4-Methylbiphenyl" else None)
            extracted = [("a-b-c-d-e-f-g-h-i-j", "1a", "alphanumeric_id_match_count"),
                         (later, "2", "numeric_id_match_count")]
            with patch.object(resolver, "_extracted_names", return_value=iter(extracted)):
                result, _ = self.resolve("test", worker)
            self.assertEqual(result[0], "CC")
            self.assertNotIn(("local", "a-b-c-d-e-f-g-h-ij"), worker.calls)

    def test_default_cap_and_deduplication_bound_backend_attempts(self):
        worker = StubWorker()
        _, details = self.resolve("a-b-c-d-e-f-g-h-i-j (1)", worker)
        self.assertEqual(len(worker.calls), 8)
        self.assertEqual(details["status"], "budget_exhausted")
        self.assertEqual(details["reason"], "candidate_limit")
        worker = StubWorker()
        self.resolve("a---b (1)", worker)
        self.assertEqual(worker.calls, [("local", "a---b"), ("local", "a--b")])

    def test_duplicate_primed_identifier_is_reported_once(self):
        result, details = self.resolve("4-methylbenzaldehyde (1')", config=make_config(mode="off"))
        self.assertEqual(result[1:3], ("4-methylbenzaldehyde", "1'"))
        self.assertEqual(details["identifier"], "1'")

    def test_precancelled_operation_does_not_extract_dispatch_or_emit(self):
        token = threading.Event()
        token.set()
        with patch.object(resolver, "_extracted_names", side_effect=AssertionError("extraction started")):
            result, details = self.resolve("ethanol (1)", cancel_token=token)
        self.assertEqual(details["status"], "cancelled")
        self.assertEqual(result[4], [])

    def test_extraction_cancellation_prevents_dispatch(self):
        token, worker = threading.Event(), StubWorker()
        def interrupted(*args, **kwargs):
            token.set()
            return iter(())
        with patch.object(resolver.regex, "finditer", side_effect=interrupted):
            _, details = self.resolve("ethanol (1)", worker, cancel_token=token)
        self.assertEqual(details["status"], "cancelled")
        self.assertEqual(worker.calls, [])

    def test_extraction_refusals_are_explicit_in_both_modes(self):
        for mode in ("on", "off"):
            for paragraph, reason in (("x" * 66000, "paragraph_size_limit"),
                                      ("a" * (resolver.MAX_NAME_BYTES + 1) + " (1)", "name_size_limit")):
                worker = StubWorker()
                result, details = self.resolve(paragraph, worker, make_config(mode=mode))
                self.assertEqual(result[4], [])
                self.assertEqual(result[1], "")
                self.assertEqual(details["status"], "refused")
                self.assertEqual(details["reason"], reason)
                self.assertEqual(worker.calls, [])
                if reason == "paragraph_size_limit":
                    self.assertIsNone(details["original_name"])
            with patch.object(resolver.regex, "finditer", side_effect=TimeoutError):
                result, details = self.resolve("(1, x" * 100, config=make_config(mode=mode))
            self.assertEqual(details["reason"], "extraction_work_limit")
            self.assertIsNone(details["original_name"])
            self.assertEqual(result[4], [])

    def test_real_malformed_identifier_search_obeys_total_deadline(self):
        worker = StubWorker()
        with patch.object(resolver, "_TIMEOUT_SECONDS", 0.001):
            _, details = self.resolve("(1, x" * 2000, worker)
        self.assertIn(details["status"], ("refused", "budget_exhausted"))
        self.assertEqual(worker.calls, [])

    def test_timeout_engine_preserves_identifier_pattern_matches(self):
        for identifier in ("1a", "6aa-1", "4a'a", "1", "2.1", "A1", "PA-5", "iPr1a", "19-Z", "122-15N", "A", "10 mg", "2, isomer", "٣"):
            paragraph = f"Preparation of (R)-ethyl acetate ({identifier})"
            for pattern, _ in resolver._PATTERNS:
                with self.subTest(identifier=identifier, pattern=pattern):
                    standard = [(match.span(), match.groups()) for match in re.finditer(pattern, paragraph)]
                    bounded = [(match.span(), match.groups()) for match in resolver.regex.finditer(pattern, paragraph, timeout=1)]
                    self.assertEqual(bounded, standard)

    def test_cancellation_after_success_wins_without_finding_or_statistic(self):
        token = threading.Event()
        def local(backend, name):
            token.set()
            return "CCO"
        worker = StubWorker(local)
        result, details = self.resolve("ethanol (1)", worker, cancel_token=token)
        self.assertIsNone(result[0])
        self.assertEqual(details["status"], "cancelled")
        self.assertIsNone(details["accepted_name"])
        self.assertEqual(worker.calls, [("local", "ethanol")])
        self.assertEqual(result[3:], ({}, []))

    def test_deadline_after_local_prevents_later_candidates(self):
        now = [0.0]
        def local(backend, name):
            now[0] = 31.0
        worker = StubWorker(local)
        with patch.object(resolver.time, "monotonic", side_effect=lambda: now[0]):
            _, details = self.resolve("a-b-c (1)", worker)
        self.assertEqual(details["status"], "budget_exhausted")
        self.assertEqual(details["reason"], "deadline")
        self.assertEqual(worker.calls, [("local", "a-b-c")])

    def test_off_never_creates_worker_and_has_no_public_budget_hooks(self):
        with patch.object(resolver, "_ResolverWorker", side_effect=AssertionError("worker started")):
            result = resolver.smiles_and_name_from_paragraph(make_config(mode="off"), ErrorTracker(), "ethanol (1)", {})
        self.assertEqual(result[1], "ethanol")
        parameters = inspect.signature(resolver.smiles_and_name_from_paragraph).parameters
        self.assertNotIn("timeout_seconds", parameters)
        self.assertNotIn("max_attempts", parameters)

    def test_run_session_reuses_worker_caches_local_unavailability_and_logs_once(self):
        worker = StubWorker(lambda backend, name: {"status": "local_unavailable", "smiles": None})
        logs = []
        session = resolver._ResolverSession(logs.append)
        with patch.object(resolver, "_ResolverWorker", return_value=worker) as create:
            for name in ("ethanol", "methanol"):
                resolver.smiles_and_name_from_paragraph(make_config(), ErrorTracker(), name + " (1)", {}, log=logs.append, _session=session)
        self.assertTrue(session.close())
        self.assertEqual(create.call_count, 1)
        self.assertEqual(worker.calls, [("local", "ethanol")])
        self.assertEqual(sum("JAVA_HOME" in line for line in logs), 1)
        self.assertTrue(any("ethanol (1) | SMILES: (unresolved)" in line for line in logs))

    def test_run_session_keeps_worker_across_refused_and_capped_compounds(self):
        worker = StubWorker(lambda backend, name: "CO" if name == "methanol" else None)
        session = resolver._ResolverSession()
        now = [0.0]
        with patch.object(resolver, "_ResolverWorker", return_value=worker) as create, \
                patch.object(resolver.time, "monotonic", side_effect=lambda: now[0]):
            for paragraph in ("ethanol (1)", "The mixture was stirred to give ethanol (2)",
                              "a-b-c-d-e-f-g-h-i-j (3)", "methanol (4)"):
                details = {}
                resolver.smiles_and_name_from_paragraph(make_config(), ErrorTracker(), paragraph, {},
                                                        details=details, _session=session)
            self.assertEqual((details["status"], create.call_count, worker.closed), ("resolved", 1, 0))
            worker.resolve = lambda backend, name: now.__setitem__(0, now[0] + 31.0)
            resolver.smiles_and_name_from_paragraph(make_config(), ErrorTracker(), "propanol (5)", {},
                                                    details=details, _session=session)
        self.assertEqual((details["reason"], worker.closed), ("deadline", 1))
        self.assertIsNone(session.worker)

    def test_cleanup_failure_cannot_be_an_accepted_standalone_result(self):
        result, details = self.resolve("ethanol (1)", StubWorker(lambda backend, name: "CCO", cleanup=False))
        self.assertIsNone(result[0])
        self.assertEqual(details["reason"], "worker_cleanup_failed")
        self.assertIsNone(details["accepted_name"])
        self.assertEqual(result[3], {})


class ResolverWorkerTests(unittest.TestCase):
    """Run the production module entrypoint with a temporary PyOpsin boundary."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "pyopsin.py").write_text(
            "import os, time\nclass PyOpsin:\n"
            " def __init__(self):\n  print('Python backend diagnostic')\n  os.write(1, b'native backend diagnostic\\n')\n"
            "  if os.name == 'nt':\n"
            "   import ctypes\n   from ctypes import wintypes\n"
            "   native = ctypes.WinDLL('kernel32', use_last_error=True)\n"
            "   native.GetStdHandle.argtypes = (wintypes.DWORD,)\n   native.GetStdHandle.restype = wintypes.HANDLE\n"
            "   native.WriteFile.argtypes = (wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID)\n"
            "   native.WriteFile.restype = wintypes.BOOL\n"
            "   payload = ctypes.create_string_buffer(b'native Win32 backend diagnostic\\n')\n   count = wintypes.DWORD()\n"
            "   assert native.WriteFile(native.GetStdHandle(wintypes.DWORD(-11)), payload, ctypes.sizeof(payload) - 1, ctypes.byref(count), None)\n"
            "   assert count.value == ctypes.sizeof(payload) - 1\n"
            " def to_smiles(self, name):\n"
            "  if name == 'hang': time.sleep(60)\n"
            "  if name == 'invalid': return [123]\n"
            "  return [{'ethanol': 'CCO', 'methanol': 'CO'}.get(name)]\n",
            encoding="utf-8",
        )
        real_popen = subprocess.Popen
        def start(command, **kwargs):
            self.assertEqual(command, [sys.executable, "-I", "-u", "-m", "checksi.analysis.name_resolver"])
            # Only the test launch adds the external-backend stub. Production -I
            # keeps caller CWD/PYTHONPATH out of module resolution.
            wrapper = f"import sys, runpy; sys.path.insert(0, {str(self.root)!r}); runpy.run_module('checksi.analysis.name_resolver', run_name='__main__')"
            return real_popen([sys.executable, "-I", "-u", "-c", wrapper], **kwargs)
        self.popen = patch.object(resolver.subprocess, "Popen", side_effect=start)
        self.popen.start()
        self.addCleanup(self.popen.stop)
        self.worker = resolver._ResolverWorker()
        self.addCleanup(self.worker.close)

    def request(self, name, token=None, timeout=10):
        return self.worker.request("local", name, resolver._Budget(timeout, token))

    def test_real_worker_resolves_reuses_rejects_invalid_smiles_and_reaps(self):
        self.assertEqual(self.request("ethanol"), {"status": "resolved", "smiles": "CCO"})
        process = self.worker.process
        self.assertEqual(self.request("methanol"), {"status": "resolved", "smiles": "CO"})
        self.assertEqual(self.request("invalid"), {"status": "invalid_response", "smiles": None})
        self.assertIs(self.worker.process, process)
        self.assertTrue(self.worker.close())
        self.assertIsNotNone(process.returncode)
        self.assertFalse(self.worker.io_thread.is_alive())

    def test_hanging_local_work_is_reaped_after_deadline_and_cancellation(self):
        self.request("ethanol")
        for cancel in (False, True):
            token = threading.Event()
            timer = threading.Timer(0.05, token.set) if cancel else None
            if timer:
                timer.start()
            try:
                with self.assertRaises(resolver._ResolutionStopped) as stopped:
                    self.request("hang", token, 10 if cancel else 0.05)
            finally:
                if timer:
                    timer.join()
                self.assertTrue(self.worker.close())
            self.assertEqual(stopped.exception.status, "cancelled" if cancel else "budget_exhausted")
            self.assertFalse(self.worker.io_thread.is_alive())
            self.worker = resolver._ResolverWorker()
            self.request("ethanol")

    def test_real_worker_rejects_invalid_command_and_exits(self):
        process = subprocess.Popen([sys.executable, "-I", "-u", "-m", "checksi.analysis.name_resolver"],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        output, errors = process.communicate(json.dumps({"backend": "unknown", "name": "ethanol", "timeout": 1}).encode() + b"\n", timeout=10)
        self.assertEqual((process.returncode, output, errors), (0, b"", b""))

    def test_invalid_or_partial_protocol_does_not_block_parent(self):
        self.popen.stop()
        for payload in ("print('not-json', flush=True)", "import sys, time; sys.stdout.write('{'); sys.stdout.flush(); time.sleep(60)"):
            real_popen = subprocess.Popen
            with patch.object(resolver.subprocess, "Popen", side_effect=lambda command, **kwargs: real_popen([sys.executable, "-u", "-c", payload], **kwargs)):
                self.worker = resolver._ResolverWorker()
                with self.assertRaises((ValueError, resolver._ResolutionStopped)):
                    self.request("ethanol", timeout=0.1)
                self.assertTrue(self.worker.close())
                self.assertFalse(self.worker.io_thread.is_alive())

    @unittest.skipUnless(os.name == "posix", "POSIX process-group semantics")
    def test_early_worker_exit_still_cleans_its_discovery_helper(self):
        (self.root / "pyopsin.py").write_text(
            "import os, subprocess, sys\nclass PyOpsin:\n"
            f" def to_smiles(self, name):\n  subprocess.Popen([sys.executable, '-c', {('import time; from pathlib import Path; time.sleep(0.3); Path(' + repr(str(self.root / 'helper-survived')) + ').touch()')!r}])\n  os._exit(0)\n",
            encoding="utf-8",
        )
        with self.assertRaises(EOFError):
            self.request("ethanol")
        self.assertTrue(self.worker.close())
        time.sleep(0.4)
        self.assertFalse((self.root / "helper-survived").exists())

    @unittest.skipUnless(os.name == "posix", "POSIX signal semantics")
    def test_sigint_is_ignored_by_owned_worker(self):
        self.request("ethanol")
        os.kill(self.worker.process.pid, signal.SIGINT)
        self.assertEqual(self.request("methanol")["smiles"], "CO")

    def test_unguarded_library_script_runs_top_level_once(self):
        self.popen.stop()
        script = self.root / "integrator.py"
        marker = self.root / "top-level.txt"
        script.write_text(
            "from pathlib import Path\nfrom checksi.analysis.name_resolver import smiles_and_name_from_paragraph\n"
            "from checksi.config_schema import AppConfig\nfrom checksi.utils.error_handler import ErrorTracker\n"
            "with Path('top-level.txt').open('a') as stream: stream.write('once\\n')\n"
            f"cfg = AppConfig.model_validate({make_config().model_dump(mode='json')!r})\n"
            "import subprocess, runpy\nfrom unittest.mock import patch\nreal_popen = subprocess.Popen\n"
            f"wrapper = {('import sys, runpy; sys.path.insert(0, ' + repr(str(self.root)) + '); runpy.run_module(\'checksi.analysis.name_resolver\', run_name=\'__main__\')')!r}\n"
            "def start(command, **kwargs):\n return real_popen([command[0], '-I', '-u', '-c', wrapper], **kwargs)\n"
            "with patch('subprocess.Popen', side_effect=start):\n result = smiles_and_name_from_paragraph(cfg, ErrorTracker(), 'ethanol (1)', {})\nassert result[0] == 'CCO', result\n",
            encoding="utf-8",
        )
        result = subprocess.run([sys.executable, str(script)], cwd=self.root, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(marker.read_text(), "once\n")
        self.assertNotIn("Traceback", result.stderr)

    def test_worker_rejects_removed_web_backend(self):
        with self.assertRaises(EOFError):
            self.worker.request("web", "ethanol", resolver._Budget(1, None))
        self.assertTrue(self.worker.close())

    def test_worker_needs_no_socket_for_local_resolution(self):
        # Network access is forbidden inside the worker, including dependencies.
        with (self.root / "pyopsin.py").open("a", encoding="utf-8") as source:
            source.write("\nimport sys\ndef deny_network(event, args):\n"
                         " if event.startswith('socket.'): raise AssertionError('network attempted')\n"
                         "sys.addaudithook(deny_network)\n")
        self.assertEqual(self.request("ethanol"), {"status": "resolved", "smiles": "CCO"})
