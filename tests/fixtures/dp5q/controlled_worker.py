"""Controlled subprocess boundary for supervisor lifecycle tests only."""

import argparse
from copy import deepcopy
import math
import os
import sys
import time

from checksi.dp5q.contracts import CAPABILITIES, CheckResult, Frame, decode_frame, encode_frame

READY = {"capabilities": list(CAPABILITIES), "model_id": "fixture", "score_version": "score", "preprocessing_id": "graph",
         "geometry_policy": "single", "weights_sha256": "a" * 64}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--generation", type=int, required=True)
    args = parser.parse_args()

    def send(kind, sequence=0, payload=None):
        sys.stdout.buffer.write(encode_frame(Frame(kind=kind, run_id=args.run_id,
            generation=args.generation, sequence=sequence, payload=payload or {})))
        sys.stdout.buffer.flush()

    if args.mode == "startup_stall":
        time.sleep(60)
    if args.mode == "startup_error":
        print("/private/worker-test/setup failed", file=sys.stderr, flush=True)
        send("error", payload={"reason": "model_load_failed", "details": "/private/worker-test/setup failed"})
        return
    metadata = {**READY, "weights_sha256": "b" * 64} if args.mode == "bad_ready" else READY
    if args.mode == 'missing_capability':
        metadata = {key: value for key, value in READY.items() if key != 'capabilities'}
    elif args.mode == 'omission_capability':
        metadata = {**READY, 'capabilities': ['coincident-observations-v1', 'direct-CB-omissions-v1']}
    if args.mode == "multiple_conformers_single":
        metadata = {**READY, "geometry_policy": "converged-single-v1"}
    elif args.mode == "multiple_conformers_ensemble":
        metadata = {**READY, "geometry_policy": "ensemble-fixture"}
    send("ready", payload=metadata)
    if args.mode in ("never_read", "partial_read"):
        if args.mode == "partial_read":
            sys.stdin.buffer.read(20)
        time.sleep(60)
    for line in sys.stdin.buffer:
        frame = decode_frame(line)
        if frame.kind == "shutdown":
            send("closed", frame.sequence)
            return
        if frame.kind == "cancel":
            return
        if args.mode == "request_rejected":
            send("error", frame.sequence, {"reason": "protocol_error", "details": "controlled request schema rejection"})
            return
        if args.mode == "crash" or (args.mode == "crash_first" and args.generation == 1):
            os._exit(7)
        if args.mode == "stall":
            time.sleep(60)
        if args.mode == "ignore_terminate":
            import signal
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            send("progress", frame.sequence, {"stage": "native-block"})
            time.sleep(60)
        if args.mode == "stderr_flood":
            for _ in range(256):
                os.write(sys.stderr.fileno(), b"x" * 8192)
            sys.stderr.write("/private/worker-test/input.docx FINAL-STDERR-MARKER\n")
            sys.stderr.flush()
            time.sleep(0.1)
            os._exit(7)
        if args.mode == "short_stderr_fail":
            sys.stderr.write('Traceback (most recent call last):\n  File "/private/worker-test/secret.py", line 7, in check\nRuntimeError: controlled short traceback\n')
            sys.stderr.flush()
            send("progress", frame.sequence, {"stage": "short-stderr-ready"})
            sys.stdin.buffer.readline()  # Parent confirms stderr drain by closing its input.
            os._exit(7)
        if args.mode in ("partial", "partial_eof", "oversized", "malformed", "nonfinite"):
            raw = {"partial": b'{"version":2', "partial_eof": b'{"version":2',
                   "oversized": b"x" * (1024 * 1024 + 1), "malformed": b"not JSON\n",
                   "nonfinite": b'{"value":NaN}\n'}[args.mode]
            sys.stdout.buffer.write(raw)
            sys.stdout.buffer.flush()
            if args.mode == "partial_eof":
                return
            time.sleep(60)
        if args.mode == "progress_flood":
            for _ in range(15000):
                send("progress", frame.sequence, {"stage": "inference"})
        if args.mode == "progress_until_deadline":
            # Keep progress active beyond the test deadline on fast and slow CPUs.
            finish = time.monotonic() + 0.5
            while time.monotonic() < finish:
                send("progress", frame.sequence, {"stage": "inference"})
                time.sleep(0.005)
        if args.mode in ("stale", "wrong_run", "wrong_generation", "wrong_version"):
            broken = Frame(kind="progress", run_id=args.run_id, generation=args.generation,
                sequence=frame.sequence, payload={"stage": "inference"}).model_dump(mode="json")
            field, value = {"stale": ("sequence", frame.sequence + 1), "wrong_run": ("run_id", "other"),
                "wrong_generation": ("generation", args.generation + 1), "wrong_version": ("version", 1)}[args.mode]
            broken[field] = value
            import json
            sys.stdout.write(json.dumps(broken) + "\n")
            sys.stdout.flush()
            time.sleep(60)
        request = frame.payload
        if args.mode == "timeout_compounds" and request["compound_id"].startswith("slow"):
            time.sleep(60)
        compound_id = "unexpected" if args.mode == "wrong_compound" else request["compound_id"]
        explicit = [sorted(group, key=lambda label: (len(label), label)) for group in request.get("carbon_groups", [])]
        used = {label for group in explicit for label in group}
        labels = request.get("carbon_labels") or [f"C{i+1}" for i in range(len(request["carbon_shifts"]))]
        groups = explicit + [[label] for label in labels if label not in used]
        observation_count = len(request["carbon_shifts"])
        n = len(groups)
        fixed = {int(key): value for key, value in request.get("fixed_assignments", {}).items()}
        remaining = iter(index for index in range(observation_count) if index not in fixed.values())
        observations = [fixed[index] if index in fixed else next(remaining) for index in range(n)]
        counts = {peak: observations.count(peak) for peak in range(observation_count)}
        shared = {str(peak): [index for index, value in enumerate(observations) if value == peak]
                  for peak, count in counts.items() if count > 1}
        reason = ("insufficient_signals" if observation_count < 2 else "constant_experimental_shifts"
                  if len(set(request["carbon_shifts"])) == 1 else None)
        rows = [{"atom_label": ",".join(group), "atom_labels": group,
                 "calculated_shift": request["carbon_shifts"][observations[index]],
                 "experimental_shift": request["carbon_shifts"][observations[index]],
                 "observation_index": observations[index], "evidence_weight": 1/counts[observations[index]], "scaled_error": None if reason else 0.,
                 "score_percent": 75., "atom_probability_percent": 75.}
                for index, group in enumerate(groups)]
        result = CheckResult(compound_id=compound_id, status="assessed", score_raw=0.75,
            log_score=math.log(.75), assignments=rows, provenance={**metadata, "pid": os.getpid(), "generation": args.generation,
                "coordinate_partition_verified": True, "stereo_verification_complete": True,
                "configuration_status": "admissible",
                "partition_constraint": "exact-original-atom-partition-v1",
                "selected_isomer_smiles": {"OCC": "CCO"}.get(request["smiles"], request["smiles"])},
            diagnostics={"full_environment_count": n, "observation_count": observation_count,
                "scored_environment_count": n, "coincident_observations": shared,
                "score_scope": "coincident_observations" if shared else "complete_spectrum",
                "scope_note": "Controlled external terminal fixture; no scientific calculation is claimed.",
                "raw_mae": 0., "corrected_mae": None if reason else 0.,
                "scaled_errors": None if reason else [0.] * n, "regression_reason": reason,
                "assignment_objective": 0., "assignment_exchangeable": False,
                "conformer_count": 1, "energies_kj": [0.], "populations": [1.],
                "fit_residual_rms": [[0.] * n], "quantile_crossings": [[0] * n],
                # Independent normal two-tail(.31863936396437514) == .75.
                # The prediction mean head intentionally differs from fitted mu.
                "fit_locations": [[row['experimental_shift'] + .31863936396437514 for row in rows]],
                "fit_sigmas": [[1.] * n],
                "geometry": {"requested_count": 1, "generated_count": 1, "converged_count": 1,
                    "eligible_count": 1, "retained_count": 1}},
            timings={name: .01 if name == "total" else 0. for name in
                ("coverage", "geometry", "inference", "assignment", "scoring", "total")})
        payload = deepcopy(result.model_dump(mode="python"))
        if args.mode == 'contradictory_fit':
            payload['diagnostics'].update(
                fit_locations=[[row['experimental_shift'] for row in rows]], fit_sigmas=[[1.] * n])
        if args.mode == 'missing_fit':
            payload['diagnostics'].pop('fit_locations')
            payload['diagnostics'].pop('fit_sigmas')
        if args.mode == "huge_timing":
            payload["timings"]["total"] = 10 ** 400
        if args.mode == "inconsistent_timing":
            payload["timings"]["coverage"] = 1.
        if args.mode in ("unassessed_rows", "unassessed_message"):
            payload.update(status="not_applicable", reason="fixture_refusal", score_raw=None, log_score=None)
            payload["diagnostics"] = {"message": "fixture refusal"}
            if args.mode == "unassessed_message":
                payload["assignments"] = []
                payload["diagnostics"]["message"] = ""
        if args.mode == "negative_observation":
            for row, observation in zip(payload["assignments"], (-1, 0)):
                row["observation_index"] = observation
                row["experimental_shift"] = row["calculated_shift"] = request["carbon_shifts"][observation]
        if args.mode == "percent_over_100":
            for row, percent in zip(payload["assignments"], (101., 75. ** 2 / 101.)):
                row["score_percent"] = row["atom_probability_percent"] = percent
        if args.mode == "incorrect_corrected_mae":
            payload["diagnostics"]["corrected_mae"] = 1.
        if args.mode == "degenerate_regression":
            payload["diagnostics"].update(corrected_mae=0., regression_reason=None, scaled_errors=[0.] * n)
            for row in payload["assignments"]:
                row["scaled_error"] = 0.
        if args.mode == "invalid_crossings":
            payload["diagnostics"]["quantile_crossings"] = [[99] * n]
        if args.mode == "unnormalized_population":
            payload["diagnostics"]["populations"] = [.9]
        if args.mode == "unordered_geometry_counts":
            payload["diagnostics"]["geometry"]["eligible_count"] = 2
        if args.mode in ("integer_sdf", "unexpected_sdf", "malformed_sdf"):
            payload["diagnostics"]["geometry_sdf"] = 7 if args.mode == "integer_sdf" else "not an SDF"
        if args.mode in ("nul_message", "oversized_message"):
            payload.update(status="not_applicable", reason="fixture_refusal", score_raw=None, log_score=None, assignments=[])
            payload["diagnostics"] = {"message": "unsafe\x00message" if args.mode == "nul_message" else "x" * 5000}
        if args.mode in ("multiple_conformers_single", "multiple_conformers_ensemble"):
            diagnostics = payload["diagnostics"]
            diagnostics.update(conformer_count=2, energies_kj=[0.,0.], populations=[.5,.5],
                fit_locations=diagnostics['fit_locations'] * 2, fit_sigmas=[[1.] * n] * 2,
                fit_residual_rms=[[0.] * n] * 2, quantile_crossings=[[0] * n] * 2,
                geometry={name: 2 for name in ("requested_count", "generated_count", "converged_count", "eligible_count", "retained_count")})
            if args.mode == "multiple_conformers_ensemble":
                diagnostics["corrected_mae"] = 4/3
        mode = args.mode.removesuffix("_first") if args.generation == 1 else args.mode
        if mode == "missing_assignments":
            payload["assignments"] = []
        if mode == "malformed_assignment":
            payload["assignments"][0] = {}
        if mode == "duplicate_observation":
            payload["assignments"][1]["observation_index"] = 0
            payload["assignments"][1]["experimental_shift"] = request["carbon_shifts"][0]
        if mode == "wrong_observed_value":
            payload["assignments"][1]["experimental_shift"] = 999.
        if mode == "wrong_original_label":
            payload["assignments"][1]["atom_label"] = "C999"
            payload["assignments"][1]["atom_labels"] = ["C999"]
        if mode == "wrong_provenance":
            payload["provenance"]["model_id"] = "different-model"
        if mode == "inconsistent_log_score":
            payload["log_score"] = 0.
        if mode == "inconsistent_group_score":
            payload["score_raw"] = .5
            payload["log_score"] = math.log(.5)
        if mode == "wrong_inventory_label":
            payload["assignments"][1]["atom_label"] = "C3"
            payload["assignments"][1]["atom_labels"] = ["C3"]
        if mode == "wrong_group_order":
            payload["assignments"].reverse()
        if mode == "wrong_fixed":
            payload["assignments"][0]["observation_index"] = 0
            payload["assignments"][0]["experimental_shift"] = request["carbon_shifts"][0]
        if mode == "split_equivalence":
            payload["assignments"][0]["atom_label"] = "C1"
            payload["assignments"][0]["atom_labels"] = ["C1"]
        if mode.startswith("wrong_id_"):
            payload["provenance"][mode.removeprefix("wrong_id_")] = "different"
        if mode == "boolean_index":
            payload["assignments"][0]["observation_index"] = False
        if mode == "boolean_shift":
            payload["assignments"][0]["calculated_shift"] = True
        if mode == "boolean_scaled":
            payload["assignments"][0]["scaled_error"] = False
        if mode == "bad_percent":
            payload["assignments"][0]["score_percent"] = 101.
        if mode == "bad_percent_alias":
            payload["assignments"][0]["atom_probability_percent"] = 50.
        if mode == "missing_diagnostics":
            payload["diagnostics"] = {}
        if mode == "missing_timings":
            payload["timings"] = {}
        if mode == "bad_regression_reason":
            payload["diagnostics"]["corrected_mae"] = None
            payload["diagnostics"]["regression_reason"] = []
        if mode == "boolean_scaled_diagnostic":
            payload["diagnostics"]["scaled_errors"][0] = False
        if mode == "overflow_diagnostic":
            for row in payload["assignments"]:
                row["calculated_shift"] = 1e308
        if mode in ("display_underflow", "tail_beyond_float64", "missing_zero_reason", "invalid_zero_log"):
            from checksi.dp5q.scoring import aggregate_compatibility
            payload['diagnostics']['fit_locations'] = [[row['experimental_shift'] +
                (1e308 if mode == 'tail_beyond_float64' else 45.) for row in rows]]
            fitted = aggregate_compatibility([row['experimental_shift'] for row in rows],
                payload['diagnostics']['fit_locations'], [[1.] * n], [1.])
            payload["score_raw"] = 0.
            payload["log_score"] = None if mode == "tail_beyond_float64" else fitted.log_score
            if mode != "missing_zero_reason":
                payload["diagnostics"]["zero_score_reason"] = "tail_beyond_float64" if mode == "tail_beyond_float64" else "display_underflow"
            if mode == "invalid_zero_log":
                payload["log_score"] = -1.
            for row in payload["assignments"]:
                row["score_percent"] = row["atom_probability_percent"] = 0.
        if mode == "null_positive_log":
            payload["log_score"] = None
        if mode == "tiny_positive_score":
            from checksi.dp5q.scoring import aggregate_compatibility
            payload['diagnostics']['fit_locations'] = [[row['experimental_shift'] + 37. for row in rows]]
            fitted = aggregate_compatibility([row['experimental_shift'] for row in rows],
                payload['diagnostics']['fit_locations'], [[1.] * n], [1.])
            payload["score_raw"] = fitted.score
            payload["log_score"] = fitted.log_score
            for row in payload["assignments"]:
                row["score_percent"] = row["atom_probability_percent"] = fitted.score * 100
        if mode == "tiny_contradictory_score":
            payload["score_raw"] = math.exp(-700.)
            payload["log_score"] = -1000.
        if args.mode == "duplicate":
            raw = encode_frame(Frame(kind="result", run_id=args.run_id, generation=args.generation,
                sequence=frame.sequence, payload=payload))
            sys.stdout.buffer.write(raw + raw)
            sys.stdout.buffer.flush()
            time.sleep(60)
        else:
            send("result", frame.sequence, payload)


if __name__ == "__main__":
    main()
