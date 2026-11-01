"""Frozen TensorFlow/legacy-fit diagnostics for the historical SDF-corpus campaign."""
from __future__ import annotations

import argparse
from hashlib import sha256
import io
import importlib.metadata
import json
import math
from pathlib import Path
import re
import sys
from types import SimpleNamespace
import warnings
import zipfile

sys.dont_write_bytecode=True
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
# Disable bytecode writes and resolve the sibling script before importing it.
from evaluate_dp5q import PILOT_IDS,SOURCE_REVISION  # noqa: E402

CASE_FILES = {'result.json','emitted.nmr','F.sdf','A-initial.sdf','A.sdf',
              'F-heads.npz','A-heads.npz','B-geometry.npz','C-geometry.npz'}
JUSTIFIED_SKIPS = {'strict_nmr_coverage_mismatch','unadjudicated_common_spectrum','legacy_geometry_failed'}


def bounded(path,maximum):
    with path.open('rb') as stream:
        data=stream.read(maximum+1)
    if len(data)>maximum:
        raise ValueError('Reference input exceeds its size bound.')
    return data


def _evidence(path,maximum,files):
    """Read and seal one bounded regular evidence file, never a symlink."""
    name=str(path.resolve())
    previous=files.get(name)
    if previous is not None:
        maximum=min(maximum,previous['maximum'])
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError('Missing regular evidence artifact: '+str(path))
        data=bounded(path,maximum)
    except OSError as exc:
        raise ValueError('Missing or unreadable evidence artifact: '+str(path)) from exc
    record={'bytes':len(data),'sha256':sha256(data).hexdigest(),'maximum':maximum}
    if previous is not None and (previous['bytes'],previous['sha256'])!=(record['bytes'],record['sha256']):
        raise ValueError('Owned evidence changed during preflight: '+name)
    files[name]=record
    return data


def _json_evidence(path,maximum,files):
    try:
        value=json.loads(_evidence(path,maximum,files))
        json.dumps(value,allow_nan=False)
    except (ValueError,RecursionError) as exc:
        raise ValueError('Invalid finite JSON evidence: '+str(path)) from exc
    if not isinstance(value,dict):
        raise ValueError('Expected an evidence object: '+str(path))
    return value


def preflight_owned(manifest_path: Path,owned: Path) -> dict:
    """Validate complete frozen owned evidence before importing any oracle runtime."""
    files={}
    manifest=_json_evidence(manifest_path,2*1024*1024,files)
    run=_json_evidence(owned/'run.json',2*1024*1024,files)
    execution=_json_evidence(owned/'execution.json',2*1024*1024,files)
    cases=manifest.get('cases',[])
    if (manifest.get('source_revision')!=SOURCE_REVISION or manifest.get('candidate_count')!=370 or len(cases)!=370
            or len({case['case_id'] for case in cases})!=370):
        raise ValueError('Expected the complete frozen 370-case manifest.')
    positions={case['case_id']:index for index,case in enumerate(cases)}
    mode=run.get('mode')
    if mode not in ('full','pilot8'):
        raise ValueError('Unknown owned campaign mode.')
    expected=[case['case_id'] for case in cases] if mode=='full' else list(PILOT_IDS)
    if run.get('case_ids')!=expected or not set(expected)<=positions.keys():
        raise ValueError('Owned submission does not match the exact declared campaign selection.')
    if (run.get('manifest_sha256')!=files[str(manifest_path.resolve())]['sha256']
            or run.get('source_revision')!=SOURCE_REVISION
            or any(execution.get(key)!=value for key,value in run.items())):
        raise ValueError('Manifest, run and execution evidence disagree.')
    if (execution.get('status')!='PASS owned campaign execution'
            or type(execution.get('returncode')) is not int or execution['returncode']!=0
            or execution.get('timed_out') is not False
            or type(execution.get('completed')) is not int or execution['completed']!=len(expected)):
        raise ValueError('Owned execution is incomplete or unsuccessful.')
    cpu=execution.get('child_CPU_seconds')
    if (type(cpu) not in (int,float) or not math.isfinite(cpu) or not 0<=cpu<=36000
            or run.get('CPU_budget_seconds')!=36000 or run.get('output_budget_bytes')!=512*1024*1024
            or run.get('per_case_wall_limit_seconds')!=600 or run.get('reference_CPU_reserve_seconds')!=3600):
        raise ValueError('Owned execution does not satisfy the declared resource bounds.')
    hashes=run.get('code_hashes')
    if (not isinstance(hashes,dict) or not hashes
            or any(not isinstance(digest,str) or not re.fullmatch('[a-f0-9]{64}',digest) for digest in hashes.values())
            or not isinstance(run.get('git_commit'),str) or not re.fullmatch('[a-f0-9]{40}',run['git_commit'])):
        raise ValueError('Owned execution lacks its frozen source identity.')
    paths={name:f'case-{positions[name]:03d}' for name in expected}
    if execution.get('case_paths')!=paths:
        raise ValueError('Owned result paths do not match every expected manifest case.')
    if {path.name for path in owned.glob('case-*')}!=set(paths.values()):
        raise ValueError('Owned case directories differ from the declared campaign selection.')
    if sum(path.stat().st_size for path in owned.rglob('*') if path.is_file())>512*1024*1024:
        raise ValueError('Owned artifact budget is exceeded.')
    comparisons=[]
    skips=[]
    for case_id in expected:
        case=cases[positions[case_id]]
        folder=owned/paths[case_id]
        if folder.is_symlink() or folder.resolve().parent!=owned.resolve():
            raise ValueError('Invalid owned case directory.')
        if {path.name for path in folder.iterdir()}-CASE_FILES:
            raise ValueError('Unexpected owned case artifacts.')
        result=_json_evidence(folder/'result.json',4*1024*1024,files)
        for key in ('case_id','family_id','spectrum_id','source_label_correct'):
            if result.get(key)!=case[key]:
                raise ValueError('Owned result identity differs from the manifest: '+case_id)
        if type(result['source_label_correct']) is not bool:
            raise ValueError('Owned source-label identity has changed type.')
        arms=result.get('arms')
        if not isinstance(arms,dict) or set(arms)!=set('FABC'):
            raise ValueError('Incomplete owned assessment arms.')
        for arm,value in arms.items():
            if not isinstance(value,dict):
                raise ValueError('Invalid owned arm evidence.')
            status=value.get('status')
            if status not in ('assessed','not_applicable','failed'):
                raise ValueError('Invalid owned arm status.')
            if 'compound_id' in value and value['compound_id']!=case_id:
                raise ValueError('Owned arm identity mismatch.')
            score=value.get('score_raw')
            log_score=value.get('log_score')
            if log_score is not None and (type(log_score) not in (int,float) or not math.isfinite(log_score) or log_score>0):
                raise ValueError('Invalid owned log-score evidence.')
            if status=='assessed':
                if value.get('compound_id')!=case_id:
                    raise ValueError('Assessed arm identity mismatch.')
                if type(score) not in (int,float) or not math.isfinite(score) or not 0<=score<=1:
                    raise ValueError('An assessed arm requires a finite score.')
            elif score is not None or log_score is not None or not value.get('reason'):
                raise ValueError('An unassessed arm needs a reason and cannot carry a score.')
        _evidence(folder/'emitted.nmr',16_384,files)
        _evidence(folder/'F.sdf',256*1024,files)
        for path in sorted(folder.iterdir()):
            _evidence(path,4*1024*1024,files)
        artifacts=result.get('artifacts',{})
        if not isinstance(artifacts,dict):
            raise ValueError('Invalid declared artifact inventory.')
        for name,declared in artifacts.items():
            if not isinstance(declared,dict):
                raise ValueError('Invalid declared artifact metadata.')
            if name not in CASE_FILES or name=='result.json':
                raise ValueError('Invalid declared case artifact.')
            data=_evidence(folder/name,4*1024*1024,files)
            if len(data)!=declared.get('bytes') or sha256(data).hexdigest()!=declared.get('sha256'):
                raise ValueError('Declared owned artifact hash/size mismatch.')
        for arm in ('F','A'):
            value=arms[arm]
            digest=value.get('heads_sha256')
            path=folder/(arm+'-heads.npz')
            if digest is not None:
                if not isinstance(digest,str) or not re.fullmatch('[a-f0-9]{64}',digest):
                    raise ValueError('Invalid declared head hash.')
                if sha256(_evidence(path,4*1024*1024,files)).hexdigest()!=digest:
                    raise ValueError('Owned head artifact hash mismatch.')
                _evidence(folder/(arm+'.sdf'),256*1024,files)
                comparisons.append({'case_id':case_id,'arm':arm})
            elif (value['status']!='assessed' and not path.exists()
                  and (value.get('prediction_refusal') or value.get('reason') in JUSTIFIED_SKIPS)):
                skips.append({'case_id':case_id,'arm':arm,'reason':value['reason']})
            else:
                raise ValueError('Missing declared prediction evidence or explicit applicability skip.')
    if not comparisons:
        raise ValueError('A reference campaign requires actual planned comparisons.')
    seal=sha256(json.dumps(files,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return {'manifest':manifest,'execution':execution,'case_ids':expected,'comparisons':comparisons,
            'skips':skips,'files':files,'seal_sha256':seal}


def validate_seal(plan: dict) -> None:
    """Reject changed evidence before a final reference PASS can be published."""
    if sha256(json.dumps(plan['files'],sort_keys=True,separators=(',',':')).encode()).hexdigest()!=plan['seal_sha256']:
        raise ValueError('Invalid evidence seal.')
    for name,record in plan['files'].items():
        data=_evidence(Path(name),record['maximum'],{})
        if len(data)!=record['bytes'] or sha256(data).hexdigest()!=record['sha256']:
            raise ValueError('Owned evidence changed after preflight: '+name)


def completed_comparisons(plan: dict,rows: list[dict]) -> int:
    """A reference PASS requires every planned comparison, not merely visited cases."""
    if [row['case_id'] for row in rows]!=plan['case_ids']:
        raise ValueError('Reference campaign is incomplete.')
    expected={(row['case_id'],row['arm']) for row in plan['comparisons']}
    skips={(row['case_id'],row['arm']):row['reason'] for row in plan['skips']}
    for row in rows:
        if set(row['arms'])!={'F','A'}:
            raise ValueError('Missing explicit reference arm outcomes.')
        for arm,value in row['arms'].items():
            key=(row['case_id'],arm)
            if key not in expected and (value.get('prediction_status')!='not_run' or value.get('reason')!=skips.get(key)):
                raise ValueError('Reference skip does not match its validated applicability reason.')
    actual={(row['case_id'],arm) for row in rows for arm,value in row['arms'].items() if value.get('prediction_status')=='PASS'}
    if not actual or actual!=expected:
        raise ValueError('Reference comparisons are incomplete.')
    return len(actual)


def record_reference_artifact(root: Path,path: Path,records: dict) -> None:
    """Bind a just-written reference output to its relative identity and bytes."""
    relative=path.relative_to(root).as_posix()
    maximum=16*1024*1024 if path.suffix=='.npz' else 4*1024*1024
    data=_evidence(path,maximum,{})
    if relative in records:
        raise ValueError('Reference artifact was already recorded.')
    records[relative]={'bytes':len(data),'sha256':sha256(data).hexdigest()}


def validate_reference_artifacts(root: Path,plan: dict,records: dict) -> dict:
    """Require exact reference output coverage and recheck it before PASS."""
    paths=plan['execution']['case_paths']
    expected={paths[name]+'/reference.json' for name in plan['case_ids']}
    expected.update(paths[row['case_id']]+'/'+row['arm']+'-legacy.npz' for row in plan['comparisons'])
    if set(records)!=expected:
        raise ValueError('Reference artifact set is incomplete or unexpected.')
    for relative,record in records.items():
        maximum=16*1024*1024 if relative.endswith('.npz') else 4*1024*1024
        data=_evidence(root/relative,maximum,{})
        if type(record.get('bytes')) is not int or len(data)!=record['bytes'] or sha256(data).hexdigest()!=record.get('sha256'):
            raise ValueError('Reference output artifact changed: '+relative)
    return {'reference_json_count':len(plan['case_ids']),'legacy_array_count':len(plan['comparisons']),
            'artifact_count':len(expected)}


def safe_heads(path,shapes):
    import numpy as np
    data=bounded(path,4*1024*1024)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if len(archive.infolist())!=len(shapes) or set(archive.namelist())!={name+'.npy' for name in shapes}:
            raise ValueError('Unexpected owned-head archive members.')
        for member in archive.infolist():
            shape,dtype=shapes[member.filename[:-4]]
            with archive.open(member) as stream:
                version=np.lib.format.read_magic(stream)
                reader=np.lib.format.read_array_header_1_0 if version==(1,0) else np.lib.format.read_array_header_2_0 if version==(2,0) else None
                if reader is None:
                    raise ValueError('Unsupported owned-head header.')
                actual,fortran,actual_dtype=reader(stream,max_header_size=1024)
                if actual!=shape or fortran or actual_dtype!=np.dtype(dtype) or stream.tell()+int(np.prod(shape))*np.dtype(dtype).itemsize!=member.file_size:
                    raise ValueError('Owned-head header disagrees with known source graph.')
    with np.load(io.BytesIO(data),allow_pickle=False) as archive:
        return {name:archive[name] for name in shapes}


def diagnostic_fit(cnn,quantile,mean_values,q_values,mol,description,assignment,analysis):
    import numpy as np
    import pandas as pd
    from scipy.stats import norm
    from scipy.special import log_ndtr
    carbons=np.array([a.GetIdx() for a in mol.GetAtoms() if a.GetAtomicNum()==6])
    stub=SimpleNamespace(atoms=[a.GetSymbol() for a in mol.GetAtoms()],C_labels=np.array([f'C{i+1}' for i in carbons]),
                         conformer_C_pred=mean_values.reshape(1,-1),populations=np.array([1.]))
    calls=[]
    solver=assignment.linear_sum_assignment
    def recorded_solver(cost):
        rows,columns=solver(cost)
        calls.append((rows.copy(),columns.copy()))
        return rows,columns
    assignment.linear_sum_assignment=recorded_solver
    try:
        assignment.assign_manual_carbon_strict(stub,description)
    except assignment.NMRAssignmentError as exc:
        return {'status':'not_applicable','reason':str(exc)},{}
    finally:
        assignment.linear_sum_assignment=solver
    grouped=np.array([q_values[positions].mean(axis=0) for positions in stub.C_group_carbon_positions])
    calculator=analysis.QuantileDP5ProbabilityCalculator.__new__(analysis.QuantileDP5ProbabilityCalculator)
    calculator.model=quantile
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        try:
            parameters=calculator.generate_distributions(pd.Series([grouped]))
        except (ValueError,RuntimeError,FloatingPointError) as exc:
            return {'status':'failed','reason':'legacy_fit_failed','message':str(exc),'warnings':[str(w.message) for w in caught]},{}
        mu,sigma=parameters.iloc[0]['mu'],parameters.iloc[0]['sigma']
        old_tails=1-np.abs(1-2*norm.cdf(stub.C_group_exp,mu,sigma))
        _,old_scores=calculator.rescale_probabilities([old_tails],None)
    arrays={'mu':mu,'sigma':sigma,'observed':stub.C_group_exp,'old_tails':old_tails,'legacy_score':old_scores}
    valid=bool(np.isfinite(mu).all() and np.isfinite(sigma).all() and (sigma>0).all() and np.isfinite(old_scores).all())
    result={'status':'assessed' if valid else 'failed','reason':None if valid else 'legacy_invalid_distribution',
            'legacy_score':float(old_scores[0]) if np.isfinite(old_scores[0]) else None,
            'assignment':stub.C_assignment_coverage,'warnings':[str(w.message) for w in caught],
            'diagnostic_only':True,'scope':'Reference-chosen assignment and fit; never substituted for an owned refusal.'}
    if calls and len(calls)==1 and len(calls[0][0])==len(carbons):
        permutation=np.empty(len(carbons),np.int64)
        permutation[calls[0][0]]=calls[0][1]
        arrays['assignment_indices']=permutation
    if valid:
        logs=np.log(2.)+log_ndtr(-np.abs((stub.C_group_exp-mu)/sigma))
        result.update(stable_score_same_fit=float(np.exp(logs.mean())),stable_log_score_same_fit=float(logs.mean()),
                      N1_threshold_changes={str(t):bool((float(old_scores[0])>=t)!=(np.exp(logs.mean())>=t)) for t in (.10,.20)})
        arrays['stable_group_log_scores']=logs
    return result,arrays


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('source','manifest','owned','output'):
        parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args()
    plan=preflight_owned(args.manifest,args.owned)
    import resource
    from dp5q_reference import load_reference,load_manual_modules,source_hashes
    resource.setrlimit(resource.RLIMIT_CPU,(3600,3600))
    # The inspected frozen loader extracts its 11-MB model.keras temporarily.
    resource.setrlimit(resource.RLIMIT_FSIZE,(16*1024*1024,16*1024*1024))
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    args.output.mkdir(parents=True,exist_ok=False)
    __import__('os').environ['KERAS_HOME']=str(args.output/'keras-cache')
    manifest_data=bounded(args.manifest,2*1024*1024)
    manifest=plan['manifest']
    execution=plan['execution']
    with (args.output/'owned-preflight.json').open('x') as stream:
        json.dump(plan,stream,indent=2)
        stream.write('\n')
    before=source_hashes(args.source)
    cnn,mean,quantile,preprocessor=load_reference(args.source)
    description_module,assignment,analysis=load_manual_modules(args.source)
    import numpy as np
    from rdkit import Chem
    cases={row['case_id']:row for row in manifest['cases']}
    rows=[]
    reference_artifacts={}
    preflight_records={}
    record_reference_artifact(args.output,args.output/'owned-preflight.json',preflight_records)
    for number,case_id in enumerate(plan['case_ids']):
        relative=execution['case_paths'][case_id]
        if not __import__('re').fullmatch(r'case-[0-9]{3}',relative):
            raise ValueError('Invalid owned result directory.')
        folder=args.owned/relative
        owned=json.loads(bounded(folder/'result.json',4*1024*1024))
        case=cases[case_id]
        destination=args.output/relative
        destination.mkdir(exist_ok=False)
        description=description_module.parse_manual_description(folder/'emitted.nmr')
        record={'case_id':case_id,'arms':{}}
        for arm in ('F','A'):
            path=folder/(arm+'-heads.npz')
            skipped=next((row for row in plan['skips'] if row['case_id']==case_id and row['arm']==arm),None)
            if skipped is not None:
                record['arms'][arm]={'prediction_status':'not_run','reason':skipped['reason']}
                continue
            mol=Chem.MolFromMolBlock(bounded(folder/(arm+'.sdf'),256*1024).decode(),removeHs=False)
            indices=np.array([a.GetIdx() for a in mol.GetAtoms() if a.GetAtomicNum()==6],np.int64)
            graph=preprocessor.construct_feature_matrices((mol,indices))
            shapes={'atom_indices':(indices.shape,'int64'),'mean':((len(indices),),'float32'),'quantiles':((len(indices),99),'float32'),
                    'graph_atom':((graph['n_atom'],),'int64'),'graph_atom_index':((graph['n_atom'],),'int64'),
                    'graph_connectivity':((graph['n_bond'],2),'int64'),'graph_distance':((graph['n_bond'],),'float32')}
            if sha256(bounded(path,4*1024*1024)).hexdigest()!=owned['arms'][arm]['heads_sha256']:
                raise ValueError('Owned head artifact hash mismatch.')
            predicted=safe_heads(path,shapes)
            for key in ('atom','atom_index','connectivity','distance'):
                np.testing.assert_array_equal(predicted['graph_'+key],graph[key],err_msg=case_id+':'+arm+':'+key)
            inputs=cnn.RBFSequence([graph],batch_size=1,shuffle=False)[0]
            mean_values=mean(inputs,training=False).numpy().reshape(-1)
            q_values=quantile.model(inputs,training=False).numpy()
            mean_error=float(np.max(np.abs(mean_values-predicted['mean'])))
            q_error=float(np.max(np.abs(q_values-predicted['quantiles'])))
            np.testing.assert_allclose(mean_values,predicted['mean'],atol=1e-3,rtol=0,err_msg=case_id+':'+arm+':mean')
            np.testing.assert_allclose(q_values,predicted['quantiles'],atol=1e-3,rtol=0,err_msg=case_id+':'+arm+':quantiles')
            diagnostic,arrays=diagnostic_fit(cnn,quantile,mean_values,q_values,mol,description,assignment,analysis)
            diagnostic.update(prediction_status='PASS',integer_graph_exact=True,mean_max_error_ppm=mean_error,quantile_max_error_ppm=q_error,
                              rbf_sha256=sha256(inputs['distance_rbf'].astype(np.float32).tobytes()).hexdigest(),
                              owned_assessment_status=owned['arms'][arm]['status'],owned_assessment_reason=owned['arms'][arm]['reason'])
            if arrays and 'assignment_indices'in arrays and diagnostic.get('stable_score_same_fit') is not None:
                from scipy.special import log_ndtr
                original=np.array(manifest['spectra'][case['spectrum_id']]['carbon_shifts'])[arrays['assignment_indices']]
                logs=np.log(2.)+log_ndtr(-np.abs((original-arrays['mu'])/arrays['sigma']))
                diagnostic['N8_stable_original_input_same_reference_assignment']=float(np.exp(logs.mean()))
                diagnostic['N8_threshold_changes_same_reference_assignment']={str(t):bool((diagnostic['stable_score_same_fit']>=t)!=(np.exp(logs.mean())>=t)) for t in (.10,.20)}
            np.savez_compressed(destination/(arm+'-legacy.npz'),mean=mean_values,quantiles=q_values,**arrays)
            record_reference_artifact(args.output,destination/(arm+'-legacy.npz'),reference_artifacts)
            record['arms'][arm]=diagnostic
        with (destination/'reference.json').open('x') as stream:
            json.dump(record,stream,indent=2,allow_nan=False)
            stream.write('\n')
        record_reference_artifact(args.output,destination/'reference.json',reference_artifacts)
        rows.append(record)
        print(json.dumps({'completed':number+1,'submitted':len(execution['case_paths'])}),flush=True)
    if source_hashes(args.source)!=before:
        raise ValueError('Frozen reference source changed.')
    comparison_count=completed_comparisons(plan,rows)
    validate_seal(plan)
    artifact_counts=validate_reference_artifacts(args.output,plan,reference_artifacts)
    final_preflight={}
    record_reference_artifact(args.output,args.output/'owned-preflight.json',final_preflight)
    if final_preflight!=preflight_records:
        raise ValueError('Owned preflight receipt changed during reference calculation.')
    receipt={'status':'PASS actual TensorFlow campaign parity','source_unchanged':True,'source_hashes':before,'manifest_sha256':sha256(manifest_data).hexdigest(),
             'source_revision':'8f8e22364654bbce71dcb6abb414eee793b00d92',
             'campaign_script_sha256':sha256(Path(__file__).read_bytes()).hexdigest(),
             'versions':{name:importlib.metadata.version(name) for name in ('tensorflow','keras','numpy','rdkit','scipy')},
             'case_count':len(rows),'comparison_count':comparison_count,'explicit_skipped_arm_count':len(plan['skips']),
             'owned_evidence_seal_sha256':plan['seal_sha256'],
             'reference_artifacts':reference_artifacts,**artifact_counts,
             'owned_preflight_file':{'path':'owned-preflight.json',**preflight_records['owned-preflight.json']},
             'case_directories':list(execution['case_paths'].values()),'resources':{'user_cpu_seconds':resource.getrusage(resource.RUSAGE_SELF).ru_utime,'system_cpu_seconds':resource.getrusage(resource.RUSAGE_SELF).ru_stime,'max_rss_native_units':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}}
    with (args.output/'receipt.json').open('x') as stream:
        json.dump(receipt,stream,indent=2)
        stream.write('\n')


if __name__=='__main__':
    main()
