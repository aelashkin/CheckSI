"""Report historical SDF-corpus campaigns with refusal-aware denominators."""
from __future__ import annotations

import argparse
from collections import Counter,defaultdict
import csv
from hashlib import sha256
import json
import math
from pathlib import Path

from checksi.dp5q.geometry import ENSEMBLE_POLICY,SINGLE_POLICY

ARMS=('F','A','B','C')
THRESHOLDS=(.10,.20)


def classification(result: dict,threshold: float) -> str:
    """An unassessed proposal is never accepted or silently called flagged."""
    if result.get('status')!='assessed':
        return 'unassessed'
    score=result.get('score_raw')
    if type(score) not in (int,float) or not math.isfinite(score) or not 0<=score<=1:
        raise ValueError('An assessed result must contain a finite compatibility score.')
    return 'compatible' if score>=threshold else 'flagged'


def class_metrics(rows: list[dict],arm: str,threshold: float) -> dict:
    """Keep all-submitted, assessed-only and coverage denominators explicit."""
    output={}
    for correct,name in ((True,'correct'),(False,'incorrect')):
        selected=[row for row in rows if row['source_label_correct'] is correct
                  and row['family_id'] not in ('stereo/JB13A','stereo/JB13B')]
        counts=Counter(classification(row['arms'][arm],threshold) for row in selected)
        submitted=len(selected)
        assessed=counts['compatible']+counts['flagged']
        output[name]={'submitted':submitted,'assessed':assessed,'unassessed':counts['unassessed'],
                      'compatible':counts['compatible'],'flagged':counts['flagged'],
                      'coverage':assessed/submitted if submitted else None,
                      'compatible_per_assessed':counts['compatible']/assessed if assessed else None,
                      'flagged_per_assessed':counts['flagged']/assessed if assessed else None,
                      'compatible_per_submitted':counts['compatible']/submitted if submitted else None,
                      'flagged_per_submitted':counts['flagged']/submitted if submitted else None,
                      'unassessed_reasons':dict(Counter(row['arms'][arm].get('reason','unknown') for row in selected if row['arms'][arm]['status']!='assessed'))}
    return output


def _ranking_value(result: dict) -> float:
    if result.get('log_score') is not None:
        value=result['log_score']
        if type(value) not in (int,float) or not math.isfinite(value) or value>0:
            raise ValueError('A ranked log score must be finite, numeric and nonpositive.')
        return float(value)
    return math.log(result['score_raw']) if result['score_raw']>0 else -math.inf


def family_ranks(rows: list[dict],manifest: dict,arm: str) -> dict:
    """Rank complete source families; report best/worst positions for ties."""
    expected=Counter(case['family_id'] for case in manifest['cases'])
    grouped=defaultdict(list)
    for row in rows:
        grouped[row['family_id']].append(row)
    output={}
    for family,candidates in grouped.items():
        if family in ('stereo/JB13A','stereo/JB13B'):
            output[family]={'status':'unadjudicated_labels'}
            continue
        if len(candidates)!=expected[family]:
            output[family]={'status':'incomplete_selection'}
            continue
        if family=='revision/S13':
            output[family]={'status':'unscorable_common_spectrum'}
            continue
        refused=[row['case_id'] for row in candidates if row['arms'][arm]['status']!='assessed']
        if refused:
            output[family]={'status':'unassessed','unassessed_candidates':refused}
            continue
        correct=[row for row in candidates if row['source_label_correct']]
        if len(correct)!=1:
            raise ValueError('A ranked source family needs exactly one source-labeled correct candidate.')
        score=_ranking_value(correct[0]['arms'][arm])
        other=[_ranking_value(row['arms'][arm]) for row in candidates if row is not correct[0]]
        best=1+sum(value>score for value in other)
        worst=1+sum(value>=score for value in other)
        output[family]={'status':'ranked','correct_case_id':correct[0]['case_id'],'rank_best':best,'rank_worst':worst,
                        'candidate_count':len(candidates),'tie_count':worst-best+1,'unique_correct_first':worst==1}
    return output


def geometry_method_evidence(rows: list[dict]) -> dict:
    """Keep executed method provenance separate from the current implementation."""
    output={}
    for arm,current in (('B',SINGLE_POLICY),('C',ENSEMBLE_POLICY)):
        generated=[row for row in rows if row.get('shared_generation_audit')
                   or row['arms'][arm].get('status')=='assessed']
        policies=[row['arms'][arm].get('provenance',{}).get('geometry_policy') for row in generated]
        recorded=sorted({value for value in policies if isinstance(value,str) and value})
        missing=[row['case_id'] for row,value in zip(generated,policies) if not isinstance(value,str) or not value]
        if not generated:
            status='NOT_RUN'
        elif missing:
            status='NOT_ESTABLISHED'
        elif len(recorded)!=1:
            status='MIXED'
        elif recorded==[current]:
            status='ESTABLISHED'
        elif (arm,recorded[0]) in {('B','converged-single-v1'),('C','bounded-ensemble-v1'),('C','bounded-ensemble-v2')}:
            status='STALE'
        else:
            status='NOT_ESTABLISHED'
        output[arm]={'status':status,'current_policy':current,'recorded_policies':recorded,
                     'geometry_case_count':len(generated),'missing_method_case_ids':missing}
    return output


def gate_report(metrics: dict,ranks: dict,timing: dict,geometry_methods: dict) -> dict:
    """Apply the approved non-regression rule; unresolved evidence keeps B."""
    primary=metrics['0.1']
    b,c=primary['B'],primary['C']
    b_ranked={key:value for key,value in ranks['B'].items() if value['status']=='ranked'}
    c_ranked={key:value for key,value in ranks['C'].items() if value['status']=='ranked'}
    same=bool(b_ranked) and set(b_ranked)==set(c_ranked)
    b_total=sum(value['rank_worst'] for value in b_ranked.values())
    c_total=sum(value['rank_worst'] for value in c_ranked.values())
    losses=[family for family,value in b_ranked.items() if family.startswith('revision/') and value['unique_correct_first']
            and not c_ranked.get(family,{}).get('unique_correct_first',False)]
    ratio=timing['C_warm_calculation_seconds']/timing['B_warm_calculation_seconds'] if timing['B_warm_calculation_seconds'] else None
    gates={'current_geometry_methods_established':all(value['status']=='ESTABLISHED' for value in geometry_methods.values()),
           'incorrect_compatible_not_increased':c['incorrect']['compatible']<=b['incorrect']['compatible'],
           'correct_flagged_not_increased':c['correct']['flagged']<=b['correct']['flagged'],
           'correct_coverage_not_reduced':c['correct']['assessed']>=b['correct']['assessed'],
           'incorrect_coverage_not_reduced':c['incorrect']['assessed']>=b['incorrect']['assessed'],
           'aggregate_correct_rank_improved':c_total<b_total if same else None,
           'no_lost_successful_revision':not losses,
           'warm_calculation_ratio_at_most_2':ratio<=2 if ratio is not None else None,
           'full_article_time_gate':None,'independent_peak_rss_ratio_at_most_1_5':None}
    selected=(geometry_methods['C']['recorded_policies'][0] if all(value is True for value in gates.values()) else
              geometry_methods['B']['recorded_policies'][0] if geometry_methods['B']['status']=='ESTABLISHED' else 'NOT_ESTABLISHED')
    return {'selected_policy':selected,'geometry_methods':geometry_methods,
            'gates':gates,'lost_successful_revision_families':losses,'same_complete_family_rank_cohort':same,
            'B_ranked_family_count':len(b_ranked),'C_ranked_family_count':len(c_ranked),
            'B_worst_tied_rank_sum':b_total,'C_worst_tied_rank_sum':c_total,
            'warm_calculation_time_ratio':ratio,
            'unresolved':'Cumulative case RSS and generation-plus-calculation timing do not establish independent full-article B/C resource gates.'}


def summarize(manifest: dict,rows: list[dict],references: dict) -> dict:
    metrics={str(t):{arm:class_metrics(rows,arm,t) for arm in ARMS} for t in THRESHOLDS}
    ranks={arm:family_ranks(rows,manifest,arm) for arm in ARMS}
    transitions=[]
    threshold_changes=[]
    n1=[]
    n8=[]
    inference=[]
    legacy_coverage=[]
    legacy_policy=[]
    fit_diagnostics=[]
    regression_diagnostics=[]
    timing={'B_warm_calculation_seconds':0.,'C_warm_calculation_seconds':0.,'shared_candidate_seconds':0.}
    for row in rows:
        for before,after in (('F','A'),('A','B'),('B','C')):
            old,new=row['arms'][before],row['arms'][after]
            if (old['status'],old.get('reason'))!=(new['status'],new.get('reason')):
                transitions.append({'case_id':row['case_id'],'comparison':before+'->'+after,'before_status':old['status'],'before_reason':old.get('reason'),'after_status':new['status'],'after_reason':new.get('reason'),'before_score':old.get('score_raw'),'after_score':new.get('score_raw'),'source_label_correct':row['source_label_correct']})
            if old['status']==new['status']=='assessed':
                for threshold in THRESHOLDS:
                    if classification(old,threshold)!=classification(new,threshold):
                        threshold_changes.append({'case_id':row['case_id'],'comparison':before+'->'+after,'threshold':threshold,'before':old['score_raw'],'after':new['score_raw'],'source_label_correct':row['source_label_correct']})
        audit=row.get('shared_generation_audit',{}).get('timings',{})
        common=sum(audit.get(name,0.) for name in ('embedding','optimization','validation'))
        timing['shared_candidate_seconds']+=common
        timing['B_warm_calculation_seconds']+=common+row['arms']['B'].get('timings',{}).get('total',0.)
        timing['C_warm_calculation_seconds']+=common+audit.get('selection',0.)+row['arms']['C'].get('timings',{}).get('total',0.)
        for arm in ARMS:
            diagnostic=row['arms'][arm].get('diagnostics',{})
            if row['arms'][arm]['status']!='assessed':
                continue
            crossings=[value for conformer in diagnostic.get('quantile_crossings',[]) for value in conformer]
            residuals=[value for conformer in diagnostic.get('fit_residual_rms',[]) for value in conformer]
            fit_diagnostics.append({'case_id':row['case_id'],'arm':arm,'scope':'Assessed distributions only; no crossing-based refusal threshold.',
                'distribution_count':len(crossings),'crossed_distribution_count':sum(value>0 for value in crossings),
                'total_adjacent_crossings':sum(crossings),'maximum_residual_rms':max(residuals) if residuals else None})
            regression_diagnostics.append({'case_id':row['case_id'],'arm':arm,'raw_mae':diagnostic.get('raw_mae'),
                'corrected_mae':diagnostic.get('corrected_mae'),'regression_reason':diagnostic.get('regression_reason')})
        reference=references.get(row['case_id'],{}).get('arms',{})
        for arm,value in reference.items():
            if value.get('prediction_status')=='PASS' and value.get('status')!=row['arms'][arm]['status']:
                legacy_policy.append({'case_id':row['case_id'],'arm':arm,'legacy_status':value.get('status'),
                    'legacy_reason':value.get('reason'),'legacy_score':value.get('legacy_score'),
                    'owned_status':row['arms'][arm]['status'],'owned_reason':row['arms'][arm].get('reason'),
                    'owned_score':row['arms'][arm].get('score_raw'),'source_label_correct':row['source_label_correct']})
            if value.get('status')=='assessed' and row['arms'][arm]['status']!='assessed':
                legacy_coverage.append({'case_id':row['case_id'],'arm':arm,'legacy_score':value.get('legacy_score'),
                    'owned_status':row['arms'][arm]['status'],'owned_reason':row['arms'][arm].get('reason'),
                    'interpretation':'Prediction parity is separate from this explicit assessment-policy refusal.'})
            if value.get('prediction_status')=='PASS':
                inference.append({'case_id':row['case_id'],'arm':arm,'mean_error_ppm':value['mean_max_error_ppm'],
                                  'quantile_error_ppm':value['quantile_max_error_ppm'],
                                  'rbf_hash_equal':value['rbf_sha256']==row['arms'][arm]['rbf_sha256']})
            if value.get('stable_score_same_fit') is not None:
                n1.append({'case_id':row['case_id'],'arm':arm,'legacy_score':value['legacy_score'],
                           'stable_score_same_fit_assignment':value['stable_score_same_fit'],'threshold_changes':value['N1_threshold_changes'],
                           'owned_status':row['arms'][arm]['status'],'owned_reason':row['arms'][arm].get('reason'),'diagnostic_only':True})
            if value.get('N8_stable_original_input_same_reference_assignment') is not None:
                n8.append({'case_id':row['case_id'],'arm':arm,'rounded_score_same_reference_fit_assignment':value['stable_score_same_fit'],
                           'original_score_same_reference_fit_assignment':value['N8_stable_original_input_same_reference_assignment'],
                           'threshold_changes':value['N8_threshold_changes_same_reference_assignment'],'maximum_input_rounding_ppm':row['legacy_rounding_max_ppm']})
    return {'submitted_candidates':len(rows),'source_family_count':len({row['family_id'] for row in rows}),
            'label_exclusions':{'families':['stereo/JB13A','stereo/JB13B'],
                                'case_ids':[row['case_id'] for row in rows if row['family_id'] in ('stereo/JB13A','stereo/JB13B')],
                                'reason':'Conflicting shared-structure correct labels require domain adjudication; excluded from class metrics and ranks.'},
            'geometry_input_paths':sorted({row.get('geometry_input_path','unrecorded') for row in rows}),
            'source_spectrum_count':len({row['spectrum_id'] for row in rows}),'metrics':metrics,'family_ranks':ranks,
            'assessment_transitions':transitions,'threshold_changes':threshold_changes,'N1_frozen_fit_assignment':n1,
            'N8_frozen_fit_assignment':n8,'inference_parity':inference,'timing':timing,
            'legacy_finite_to_owned_unassessed':legacy_coverage,'legacy_assessment_policy_changes':legacy_policy,'N3_assessed_fit_diagnostics':fit_diagnostics,
            'N4_optional_regression_diagnostics':regression_diagnostics,
            'geometry_scope':{'F':'Provided pose after exact legacy AddHs/SDF parser boundary.',
                'A':'Exact frozen checker ETKDGv1/status!=-1 selection and SDF roundtrips.',
                'B':'Direct full-precision converged ETKDGv3/MMFF geometry; one selected conformer.',
                'C':'Same candidate generator as B with bounded ensemble retention and compatibility mixing.',
                'roundtrip_precision':'A->B changes geometry policy and removes SDF coordinate rounding together; that score change is not attributed solely to either cause.',
                'B_audit':'B selected_count/conformer_count=1; shared_candidate_audit.retained_count describes C candidate retention, not B averaging.'},
            'peak_owned_child_rss_bytes':max(row['resources']['max_rss_bytes'] for row in rows),
            'selection':gate_report(metrics,ranks,timing,geometry_method_evidence(rows))}


def read_json(path: Path,maximum: int=4*1024*1024) -> dict:
    with path.open('rb') as stream:
        data=stream.read(maximum+1)
    if len(data)>maximum:
        raise ValueError('Report input exceeds its explicit bound.')
    return json.loads(data,parse_constant=lambda value:(_ for _ in ()).throw(ValueError('Nonfinite JSON in report evidence.')))


def _campaign_guard():
    import importlib.util
    spec=importlib.util.spec_from_file_location('dp5q_campaign_guard',Path(__file__).with_name('campaign.py'))
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sealed_reference(path: Path,files: dict,maximum: int=4*1024*1024) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError('Reference evidence is not a regular file.')
    with path.open('rb') as stream:
        data=stream.read(maximum+1)
    if len(data)>maximum:
        raise ValueError('Reference evidence exceeds its explicit bound.')
    files[str(path.resolve())]={'bytes':len(data),'sha256':sha256(data).hexdigest(),'maximum':maximum}
    return data


def campaign_inputs(manifest_path: Path,owned: Path,reference: Path) -> tuple:
    """Use the authoritative owned preflight and verify every declared reference output."""
    guard=_campaign_guard()
    bundle=guard.preflight_owned(manifest_path,owned)
    execution=bundle['execution']
    files={}
    if reference.is_symlink() or not reference.is_dir():
        raise ValueError('Invalid reference evidence directory.')
    receipt=json.loads(_sealed_reference(reference/'receipt.json',files))
    json.dumps(receipt,allow_nan=False)
    if (receipt.get('status')!='PASS actual TensorFlow campaign parity' or receipt.get('source_unchanged') is not True
            or receipt.get('source_revision')!=bundle['manifest']['source_revision']
            or receipt.get('manifest_sha256')!=sha256(manifest_path.read_bytes()).hexdigest()
            or receipt.get('owned_evidence_seal_sha256')!=bundle['seal_sha256']):
        raise ValueError('Reference receipt does not certify this exact complete owned campaign.')
    cpu=receipt.get('resources',{})
    seconds=sum(cpu.get(name,float('inf')) for name in ('user_cpu_seconds','system_cpu_seconds'))
    if not math.isfinite(seconds) or not 0<=seconds<=3600:
        raise ValueError('Reference CPU evidence exceeds its reserved bound.')
    folders=[execution['case_paths'][case] for case in bundle['case_ids']]
    if receipt.get('case_directories')!=folders or type(receipt.get('case_count')) is not int or receipt['case_count']!=len(folders):
        raise ValueError('Reference case coverage differs from the complete owned campaign.')
    for folder in folders:
        directory=reference/folder
        if directory.is_symlink() or directory.resolve().parent!=reference.resolve():
            raise ValueError('Invalid reference case directory.')
    expected={folder+'/reference.json' for folder in folders}
    expected.update(execution['case_paths'][row['case_id']]+'/'+row['arm']+'-legacy.npz' for row in bundle['comparisons'])
    inventory=receipt.get('reference_artifacts')
    if not isinstance(inventory,dict) or set(inventory)!=expected:
        raise ValueError('Reference artifact inventory is incomplete or unexpected.')
    for key,count in (('reference_json_count',len(folders)),('legacy_array_count',len(bundle['comparisons'])),('artifact_count',len(expected))):
        if type(receipt.get(key)) is not int or receipt[key]!=count:
            raise ValueError('Reference artifact counts disagree.')
    for relative,record in inventory.items():
        data=_sealed_reference(reference/relative,files,1024*1024 if relative.endswith('.npz') else 4*1024*1024)
        if len(data)!=record.get('bytes') or sha256(data).hexdigest()!=record.get('sha256'):
            raise ValueError('Reference artifact hash or byte count changed.')
    preflight=receipt.get('owned_preflight_file',{})
    if not isinstance(preflight,dict) or preflight.get('path','owned-preflight.json')!='owned-preflight.json':
        raise ValueError('Invalid reference preflight evidence identity.')
    data=_sealed_reference(reference/'owned-preflight.json',files)
    if len(data)!=preflight.get('bytes') or sha256(data).hexdigest()!=preflight.get('sha256'):
        raise ValueError('Reference owned preflight evidence changed.')
    if json.loads(data)!=bundle:
        raise ValueError('Reference preflight does not match the exact current owned seal.')
    rows=[read_json(owned/execution['case_paths'][case]/'result.json') for case in bundle['case_ids']]
    reference_rows=[read_json(reference/folder/'reference.json') for folder in folders]
    comparisons=guard.completed_comparisons(bundle,reference_rows)
    if receipt.get('comparison_count')!=comparisons:
        raise ValueError('Reference comparison count disagrees with validated evidence.')
    return bundle,rows,{row['case_id']:row for row in reference_rows},receipt,files


def validate_input_seals(bundle: dict,reference_files: dict) -> None:
    """Recheck both evidence sets immediately before a derived artifact is written."""
    _campaign_guard().validate_seal(bundle)
    for name,record in reference_files.items():
        data=_sealed_reference(Path(name),{},record['maximum'])
        if len(data)!=record['bytes'] or sha256(data).hexdigest()!=record['sha256']:
            raise ValueError('Reference evidence changed after report preflight.')


def validate_fit_results(analysis: dict,bundle: dict) -> list[dict]:
    """Every planned reference comparison needs an explicit numerical-control outcome."""
    expected={(row['case_id'],row['arm']) for row in bundle['comparisons']}
    rows=analysis.get('results',[])
    if (analysis.get('status')!='COMPLETE diagnostic same-input fitter and assignment controls'
            or type(analysis.get('case_arm_count')) is not int or analysis['case_arm_count']!=len(expected)
            or len(rows)!=len(expected) or {(row['case_id'],row['arm']) for row in rows}!=expected):
        raise ValueError('Numerical controls are incomplete or have unexpected comparison identities.')
    return rows


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('manifest','owned','reference','output'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--fit-analysis',type=Path)
    args=parser.parse_args()
    bundle,rows,references,reference_receipt,reference_files=campaign_inputs(args.manifest,args.owned,args.reference)
    manifest=bundle['manifest']
    execution=bundle['execution']
    digest=sha256(args.manifest.read_bytes()).hexdigest()
    report=summarize(manifest,rows,references)
    report.update(manifest_sha256=digest,owned_code_hashes=execution.get('code_hashes'),
                  source_revision=manifest['source_revision'],report_script_sha256=sha256(Path(__file__).read_bytes()).hexdigest(),
                  interpretation='Descriptive source-labeled regression only; no probability calibration or independent generalization claim.')
    report['owned_evidence_seal_sha256']=bundle['seal_sha256']
    report['reference_input_files']=reference_files
    if args.fit_analysis:
        fit_data=_sealed_reference(args.fit_analysis,reference_files,16*1024*1024)
        fit_analysis=json.loads(fit_data)
        if (fit_analysis.get('manifest_sha256')!=digest or fit_analysis.get('owned_evidence_seal_sha256')!=bundle['seal_sha256']
                or fit_analysis.get('reference_receipt_sha256')!=sha256((args.reference/'receipt.json').read_bytes()).hexdigest()):
            raise ValueError('N2 fit analysis belongs to different evidence.')
        controls=validate_fit_results(fit_analysis,bundle)
        if fit_analysis.get('script_sha256')!=sha256(Path(__file__).with_name('analyze_fits.py').read_bytes()).hexdigest():
            raise ValueError('Numerical controls use a different analysis implementation.')
        report['N2_same_reference_input_refit']=fit_analysis
        report['N5_same_reference_predictions_assignment']=[{'case_id':row['case_id'],'arm':row['arm'],
            **row['assignment_controls']['N5_reference_parsed_input']} for row in controls if 'assignment_controls' in row]
        report['N8_assignment_precision_control']=[{'case_id':row['case_id'],'arm':row['arm'],
            **row['assignment_controls']['N8_observation_precision']} for row in controls if 'assignment_controls' in row]
    validate_input_seals(bundle,reference_files)
    args.output.mkdir(parents=True,exist_ok=False)
    with (args.output/'report.json').open('x') as stream:
        json.dump(report,stream,indent=2,allow_nan=False)
        stream.write('\n')
    with (args.output/'cases.csv').open('x',newline='') as stream:
        fields=['case_id','family_id','source_label_correct']+[arm+'_'+field for arm in ARMS for field in ('status','reason','score','log_score')]
        writer=csv.DictWriter(stream,fieldnames=fields)
        writer.writeheader()
        for row in rows:
            item={key:row[key] for key in fields[:3]}
            for arm in ARMS:
                item.update({arm+'_status':row['arms'][arm]['status'],arm+'_reason':row['arms'][arm].get('reason'),
                             arm+'_score':row['arms'][arm].get('score_raw'),arm+'_log_score':row['arms'][arm].get('log_score')})
            writer.writerow(item)
    primary=report['metrics']['0.1']
    lines=[f"# DP5q campaign: {len(rows)} submitted candidates",'',
        'Source labels are development regression evidence. Refusals are unassessed, never acceptance. '
        'JB13A/B are excluded from labeled metrics/ranks pending domain adjudication. '
        'Source-SDF-order results do not establish product-SMILES coverage.','',
        '| Arm | Correct assessed/submitted | Incorrect assessed/submitted | Incorrect compatible at0.10 | Correct flagged at0.10 |',
        '|---|---:|---:|---:|---:|']
    for arm in ARMS:
        a,b=primary[arm]['correct'],primary[arm]['incorrect']
        lines.append(f"| {arm} | {a['assessed']}/{a['submitted']} | {b['assessed']}/{b['submitted']} | {b['compatible']} | {a['flagged']} |")
    lines+=['', '## Geometry-method evidence', '',
            '| Arm | Recorded policy | Current policy | Evidence status |',
            '|---|---|---|---|']
    for arm,method in report['selection']['geometry_methods'].items():
        recorded=', '.join(method['recorded_policies']) or 'None recorded'
        lines.append(f"| {arm} | {recorded} | {method['current_policy']} | {method['status']} |")
    if any(method['status']!='ESTABLISHED' for method in report['selection']['geometry_methods'].values()):
        lines+=['', 'Stale, mixed or unidentified geometry results do not validate the current method. '
                'The table above retains historical observations; it does not relabel them as a new campaign.']
    lines+=['',f"Selected policy: **{report['selection']['selected_policy']}**.",'',
            'Full details, both thresholds, ranks/ties, every coverage/threshold reversal and separate N1/N8 ledgers are in report.json; cases.csv retains every submitted candidate.','',
            'The C resource gates remain unresolved without independent full-article timing/RSS. An unresolved gate keeps B.']
    (args.output/'report.md').write_text('\n'.join(lines)+'\n')
    print(json.dumps({'submitted':len(rows),'selected_policy':report['selection']['selected_policy'],
                      'worst_head_error_ppm':max(max(row['mean_error_ppm'],row['quantile_error_ppm']) for row in report['inference_parity'])}))


if __name__=='__main__':
    main()
