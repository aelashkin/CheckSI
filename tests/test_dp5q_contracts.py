"""Finite, bounded contracts for the owned DP5q worker."""

from __future__ import annotations

import unittest

from checksi.dp5q.contracts import (
    MAX_FRAME_BYTES,
    MAX_SMILES_LENGTH,
    MAX_TEXT_LENGTH,
    CheckRequest,
    CheckResult,
    Frame,
    decode_frame,
    encode_frame,
)


class DP5qContractTests(unittest.TestCase):
    def test_source_smiles_provenance_retains_request_budget_through_frames(self) -> None:
        for length in (MAX_TEXT_LENGTH + 1, MAX_SMILES_LENGTH):
            with self.subTest(length=length):
                # One valid eight-atom diol plus trailing spaces, with no model
                # or chemistry inference needed to exercise the transport edge.
                smiles = 'CC(O)C(C)C(C)O'.ljust(length)
                request = CheckRequest(compound_id='source-text', smiles=smiles, carbon_shifts=[10.])
                result = CheckResult(compound_id=request.compound_id, status='assessed', score_raw=.5,
                                     provenance={'source_smiles': request.smiles})
                frame = Frame(kind='result', run_id='source-text', generation=1, sequence=1,
                              payload=result.model_dump(mode='json'))
                decoded = CheckResult.model_validate(decode_frame(encode_frame(frame)).payload)
                self.assertEqual(decoded.provenance['source_smiles'], request.smiles)

    def test_source_smiles_budget_remains_bounded_and_path_specific(self) -> None:
        too_long = 'CC(O)C(C)C(C)O'.ljust(MAX_SMILES_LENGTH + 1)
        with self.assertRaises(ValueError):
            CheckRequest(compound_id='source-text', smiles=too_long, carbon_shifts=[10.])
        for change in (
            {'provenance': {'source_smiles': too_long}},
            {'provenance': {'source_smiles': 'CCO\x00'}},
            {'provenance': {'other_text': 'x' * (MAX_TEXT_LENGTH + 1)}},
            {'diagnostics': {'source_smiles': 'x' * (MAX_TEXT_LENGTH + 1)}},
            {'provenance': {'nested': {'source_smiles': 'x' * (MAX_TEXT_LENGTH + 1)}}},
        ):
            with self.subTest(change=tuple(change)), self.assertRaises(ValueError):
                CheckResult(compound_id='source-text', status='assessed', score_raw=.5, **change)

    def test_unique_carbons_need_no_explicit_equivalence_group(self) -> None:
        request = CheckRequest(compound_id="1", smiles="CCO", carbon_shifts=[18.2, 58.1])
        self.assertEqual(request.carbon_groups, ())
        self.assertEqual(request.carbon_shifts, (18.2, 58.1))

    def test_carbon_inventory_is_unique_original_order_and_covers_explicit_groups(self):
        valid = CheckRequest(compound_id="1", smiles="COC", carbon_shifts=(10.,20.), carbon_labels=("C1","C3"))
        self.assertEqual(valid.carbon_labels, ("C1","C3"))
        for labels in (("C1","C1"), ("C3","C1"), ("C0",), ("C999",), (b"C1",), ()):
            with self.subTest(labels=labels), self.assertRaises(ValueError):
                CheckRequest(compound_id="1", smiles="CC", carbon_shifts=(10.,20.), carbon_labels=labels)
        with self.assertRaises(ValueError):
            CheckRequest(compound_id="1", smiles="CCC", carbon_shifts=(10.,20.),
                carbon_labels=("C1","C3"), carbon_groups=(("C1","C2"),))

    def test_request_rejects_nonfinite_boolean_and_duplicate_group_members(self) -> None:
        for shifts in ([float("nan")], [float("inf")], [True], [10 ** 400]):
            with self.subTest(shifts=shifts), self.assertRaises(ValueError):
                CheckRequest(compound_id="1", smiles="C", carbon_shifts=shifts)
        for groups in ([["C1"]], [["C1", "C1"]], [["C1", "O2"]], [["C1", "C2"], ["C2", "C3"]]):
            with self.subTest(groups=groups), self.assertRaises(ValueError):
                CheckRequest(compound_id="1", smiles="CCC", carbon_shifts=[1.0], carbon_groups=groups)

    def test_frame_round_trip_preserves_ids_and_unicode(self) -> None:
        frame = Frame(kind="check", run_id="run-1", generation=1, sequence=2,
                      payload={"compound_id": "α", "carbon_shifts": [18.2]})
        self.assertEqual(decode_frame(encode_frame(frame)), frame)

    def test_frame_refuses_unknown_version_kind_and_nonfinite_payload(self) -> None:
        for update in ({"version": 3}, {"kind": "exec"}, {"generation": True}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                Frame.model_validate({"kind": "check", "run_id": "r", "generation": 1, "sequence": 0, **update})
        with self.assertRaises(ValueError):
            encode_frame(Frame(kind="result", run_id="r", generation=1, sequence=0,
                               payload={"score": float("nan")}))
        with self.assertRaises(ValueError):
            decode_frame(b'{"kind":"result","run_id":"r","generation":1,"sequence":0,"payload":{"score":NaN}}\n')

    def test_frame_is_bounded_and_requires_one_complete_json_object(self) -> None:
        for raw in (b"x" * (MAX_FRAME_BYTES + 1), b"{}\n{}\n", b"{\n", b"\xff\n"):
            with self.subTest(size=len(raw)), self.assertRaises(ValueError):
                decode_frame(raw)

    def test_unassessed_result_cannot_carry_a_score(self) -> None:
        with self.assertRaises(ValueError):
            CheckResult(compound_id="1", status="not_applicable", reason="assignment_ambiguous", score_raw=0.8)
        with self.assertRaises(ValueError):
            CheckResult(compound_id="1", status="assessed")
        result = CheckResult(compound_id="1", status="assessed", score_raw=0.0,
                             log_score=None, diagnostics={"zero_reason": "underflow"})
        self.assertEqual(result.score_raw, 0.0)

    def test_result_rejects_nonfinite_nested_diagnostics_before_json_coercion(self):
        for nested in ({"raw_mae": float("inf")}, {"fits": [{"value": float("nan")}]}):
            with self.subTest(nested=nested), self.assertRaises(ValueError):
                CheckResult(compound_id="1", status="assessed", score_raw=0.5, diagnostics=nested)

    def test_overflowing_scores_timings_and_xml_unsafe_text_are_value_errors(self):
        cases = ({"timings": {"total": 10 ** 400}}, {"score_raw": 10 ** 400},
                 {"diagnostics": {"message": "unsafe\x00text"}},
                 {"diagnostics": {"message": "surrogate\ud800"}},
                 {"diagnostics": {"message": "x" * 4097}})
        for change in cases:
            with self.subTest(fields=tuple(change)), self.assertRaises(ValueError):
                CheckResult(compound_id="1", status="assessed", **{"score_raw": .5, **change})


if __name__ == "__main__":
    unittest.main()
