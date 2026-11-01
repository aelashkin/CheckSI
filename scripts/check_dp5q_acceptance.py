"""Opt-in replay of the actual DOCX-derived 14 pairs and Part B requests."""

from __future__ import annotations

import argparse
from collections import Counter
from hashlib import sha256
import json
import math
from pathlib import Path


DEFAULT_FIXTURE = Path(__file__).resolve().parents[1] / 'tests/fixtures/dp5q/acceptance-requests.json'


def evaluate_criteria(fixture: dict, outcomes: list[dict]) -> dict:
    """Require every assessment, every revised/original pair, and Part B order."""
    expected = [case['case_id'] for case in fixture['cases']]
    counts = Counter(row['case_id'] for row in outcomes)
    failures = []
    scores = {}
    for case_id in expected:
        if counts[case_id] != 1:
            failures.append(f'{case_id}: missing or duplicate outcome ({counts[case_id]}).')
    for case_id in counts.keys() - set(expected):
        failures.append(f'{case_id}: unexpected outcome.')
    for row in outcomes:
        case_id, score = row['case_id'], row.get('score_percent')
        if counts[case_id] != 1 or case_id not in expected:
            continue
        if row.get('status') != 'assessed':
            failures.append(f"{case_id}: {row.get('status')} ({row.get('reason')}).")
        elif (isinstance(score, bool) or not isinstance(score, (int, float))
              or not math.isfinite(score) or not 0 <= score <= 100):
            failures.append(f'{case_id}: invalid assessed score {score!r}.')
        else:
            scores[case_id] = score
    pairs = []
    for pair in fixture['part_a_pairs']:
        original, revised = pair['original'], pair['revised']
        passed = original in scores and revised in scores and scores[revised] > scores[original]
        pairs.append({**pair, 'original_percent': scores.get(original),
                      'revised_percent': scores.get(revised), 'passed': passed})
        if not passed:
            failures.append(f'{revised}: must score higher than {original}.')
    order = fixture['part_b_order']
    ordered = all(left in scores and right in scores and scores[left] > scores[right]
                  for left, right in zip(order, order[1:]))
    if not ordered:
        failures.append('Part B must satisfy ' + ' > '.join(order) + '.')
    threshold = fixture['score_threshold_percent']
    incorrect = order[-1]
    below_threshold = incorrect in scores and scores[incorrect] < threshold
    if not below_threshold:
        failures.append(f'Part B {incorrect}: score must be below {threshold:g}%.')
    return {'passed': not failures, 'record_count': len(expected), 'assessed_count': len(scores),
            'pairs': pairs, 'part_b': {'order': order, 'scores_percent': {key: scores.get(key) for key in order},
                                     'ordered': ordered, 'incorrect_below_threshold': below_threshold,
                                     'score_threshold_percent': threshold}, 'failures': failures}


def run(fixture_path: Path, output: Path) -> dict:
    """Replay captured typed requests through the real worker and parent checks."""
    from checksi.dp5q.client import DP5qSession, DP5qWorkerError
    from checksi.dp5q.contracts import CheckRequest

    with fixture_path.open('rb') as stream:
        fixture_bytes = stream.read(1024 * 1024 + 1)
    if len(fixture_bytes) > 1024 * 1024:
        raise ValueError('Acceptance fixture exceeds 1 MiB.')
    fixture = json.loads(fixture_bytes)
    requests = [CheckRequest.model_validate(case['request']) for case in fixture['cases']]
    output.mkdir(parents=True, exist_ok=False)
    outcomes = []
    with DP5qSession() as session:
        for index, (case, request) in enumerate(zip(fixture['cases'], requests), 1):
            try:
                result = session.check(request)
                row = {'case_id': case['case_id'], 'compound_id': request.compound_id,
                       'status': result.status, 'reason': result.reason,
                       'score_percent': None if result.score_raw is None else result.score_raw * 100}
                artifact = {'request': case['request'], 'result': result.model_dump(mode='json')}
            except DP5qWorkerError as error:
                row = {'case_id': case['case_id'], 'compound_id': request.compound_id,
                       'status': 'failed', 'reason': error.reason, 'score_percent': None}
                artifact = {'request': case['request'], 'error': str(error)}
            with (output / f'case-{index:02d}.json').open('x', encoding='utf-8') as stream:
                json.dump(artifact, stream, indent=2, allow_nan=False)
                stream.write('\n')
            outcomes.append(row)
            print(f"{index}/{len(requests)} {case['case_id']}: {row['status']} {row['score_percent']}", flush=True)
    report = {'fixture_sha256': sha256(fixture_bytes).hexdigest(), 'source': fixture['source'],
              'worker_metadata': session.metadata, 'outcomes': outcomes,
              **evaluate_criteria(fixture, outcomes)}
    with (output / 'acceptance.json').open('x', encoding='utf-8') as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
        stream.write('\n')
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture', type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument('--output', type=Path, required=True, help='New output directory; never overwritten.')
    args = parser.parse_args()
    report = run(args.fixture, args.output)
    for failure in report['failures']:
        print(failure)
    print('PASS' if report['passed'] else 'FAIL')
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
