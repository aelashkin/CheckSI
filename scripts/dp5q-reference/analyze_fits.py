"""Refit historical campaign quantiles for N2 diagnostics, never product scoring."""
from __future__ import annotations

import argparse
from hashlib import sha256
import io
import json
import math
from pathlib import Path
import zipfile


def refit_reference(quantiles,legacy_mu,legacy_sigma,observed,levels) -> dict:
    """Refit identical paired inputs; results are diagnostic, never an assessment fallback."""
    import numpy as np
    from checksi.dp5q.scoring import ScoringError,aggregate_compatibility,fit_gaussian
    quantiles=np.asarray(quantiles,dtype=np.float64)
    mu=np.asarray(legacy_mu,dtype=np.float64)
    sigma=np.asarray(legacy_sigma,dtype=np.float64)
    observed=np.asarray(observed,dtype=np.float64)
    if (quantiles.ndim!=2 or not 1<=len(quantiles)<=512 or quantiles.shape[1]!=99 or mu.shape!=sigma.shape or mu.shape!=observed.shape
            or mu.shape!=(len(quantiles),) or not np.isfinite(mu).all() or not np.isfinite(sigma).all()
            or not np.isfinite(observed).all() or np.any(sigma<=0)):
        raise ValueError('Reference fits and quantile groups must be finite, valid and paired.')
    groups=[]
    fits=[]
    for index,values in enumerate(quantiles):
        try:
            fit=fit_gaussian(values,levels)
        except ScoringError as exc:
            groups.append({'index':index,'status':'refused','reason':exc.reason})
            continue
        fits.append(fit)
        groups.append({'index':index,'status':'fitted','mu':fit.mu,'sigma':fit.sigma,
                       'legacy_mu':float(mu[index]),'legacy_sigma':float(sigma[index]),
                       'residual_rms':fit.residual_rms,'crossing_count':fit.crossing_count,
                       'evaluations':fit.evaluations,'condition_number':fit.condition_number})
    result={'status':'compared' if len(fits)==len(quantiles) else 'owned_refit_refused',
            'groups':groups,'diagnostic_only':True,'input_scope':'Exact saved TensorFlow quantiles and same reference-chosen observed assignment; no owned prediction or reassignment.'}
    if len(fits)==len(quantiles):
        refit=aggregate_compatibility(observed,np.array([[fit.mu for fit in fits]]),np.array([[fit.sigma for fit in fits]]),np.array([1.]))
        old=aggregate_compatibility(observed,mu[None,:],sigma[None,:],np.array([1.]))
        result.update(mu_max_error_ppm=max(abs(fit.mu-mu[index]) for index,fit in enumerate(fits)),
                      sigma_max_error_ppm=max(abs(fit.sigma-sigma[index]) for index,fit in enumerate(fits)),
                      reference_fit_stable_score=old.score,refitted_score_same_reference_assignment=refit.score,
                      N2_threshold_changes={str(t):bool((old.score>=t)!=(refit.score>=t)) for t in (.10,.20)})
    return result


def safe_reference_arrays(path: Path) -> dict:
    """Validate all fixed-size NumPy headers before loading reference diagnostic data."""
    import numpy as np
    with path.open('rb') as stream:
        data=stream.read(1024*1024+1)
    if len(data)>1024*1024:
        raise ValueError('Reference fit archive exceeds bound.')
    allowed={'mean','quantiles','mu','sigma','observed','old_tails','legacy_score','assignment_indices','stable_group_log_scores'}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members=archive.infolist()
        names={member.filename[:-4] for member in members if member.filename.endswith('.npy')}
        if len(names)!=len(members) or not names<=allowed or not {'mean','quantiles'}<=names:
            raise ValueError('Unexpected reference fit archive members.')
        for member in members:
            with archive.open(member) as stream:
                version=np.lib.format.read_magic(stream)
                reader=np.lib.format.read_array_header_1_0 if version==(1,0) else np.lib.format.read_array_header_2_0 if version==(2,0) else None
                if reader is None:
                    raise ValueError('Unsupported reference fit array version.')
                shape,fortran,dtype=reader(stream,max_header_size=1024)
                if (fortran or dtype not in (np.dtype('float32'),np.dtype('float64'),np.dtype('int64'))
                        or not 1<=len(shape)<=2 or any(type(value) is not int or not 1<=value<=512 for value in shape)
                        or (len(shape)==2 and (member.filename!='quantiles.npy' or shape[1]!=99))
                        or stream.tell()+math.prod(shape)*dtype.itemsize!=member.file_size):
                    raise ValueError('Invalid or oversized reference fit array header.')
    with np.load(io.BytesIO(data),allow_pickle=False) as archive:
        return {name:archive[name] for name in names}


def assignment_controls(mean,quantiles,original_observed,rounded_observed,reference_indices,*,fixed=None) -> dict:
    """Isolate N5 assignment and N8 precision with identical reference predictions."""
    import numpy as np
    from checksi.dp5q.assignment import AssignmentError,assign_groups
    mean=np.asarray(mean,dtype=np.float64)
    quantiles=np.asarray(quantiles,dtype=np.float64)
    original=np.asarray(original_observed,dtype=np.float64)
    rounded=np.asarray(rounded_observed,dtype=np.float64)
    reference=np.asarray(reference_indices)
    if (mean.shape!=original.shape or mean.shape!=rounded.shape or reference.shape!=mean.shape
            or reference.dtype.kind not in ('i','u') or quantiles.shape!=(len(mean),99)
            or sorted(reference.tolist())!=list(range(len(mean)))):
        raise ValueError('Same-input assignment controls require complete paired original-index groups.')

    def assign(values):
        try:
            value=assign_groups(mean,values,distributions=quantiles,fixed=fixed)
        except AssignmentError as exc:
            return {'status':'unassessed','reason':exc.reason}
        return {'status':'assigned','reason':None,'indices':value.indices.tolist(),'objective_ppm':value.objective}

    before=assign(original)
    after=assign(rounded)
    n5=dict(after)
    reference_objective=float(np.abs(mean-rounded[reference]).sum())
    n5['reference_objective_ppm']=reference_objective
    if after['status']=='assigned':
        n5['same_observed_values_as_reference']=bool(np.array_equal(rounded[after['indices']],rounded[reference]))
        n5['objective_error_ppm']=abs(after['objective_ppm']-reference_objective)
    changed=None
    if before['status']==after['status']=='assigned':
        changed=not np.array_equal(original[before['indices']],original[after['indices']])
    return {'diagnostic_only':True,'assignment_policy':'squared-error-first-optimum',
            'input_scope':'Exact saved TensorFlow mean/quantiles; only original versus emitted-four-decimal observations change. Frozen reference assignments may differ from the squared-error policy.',
            'N5_reference_parsed_input':n5,
            'N8_observation_precision':{'original':before,'rounded':after,
                'status_or_reason_changed':(before['status'],before['reason'])!=(after['status'],after['reason']),
                'assigned_original_values_changed':None if changed is None else bool(changed),
                'distinct_original_values':len(np.unique(original)),'distinct_rounded_values':len(np.unique(rounded))}}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('manifest','owned','reference','output'):
        parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args()
    import resource
    resource.setrlimit(resource.RLIMIT_CPU,(1800,1800))
    resource.setrlimit(resource.RLIMIT_FSIZE,(16*1024*1024,16*1024*1024))
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    # Imported only by the explicitly invoked analysis, never by product code.
    from report import campaign_inputs,validate_input_seals
    import numpy as np
    bundle,rows,references,reference_receipt,reference_files=campaign_inputs(args.manifest,args.owned,args.reference)
    from checksi.dp5q.assets import asset_metadata
    levels=np.array(asset_metadata()['quantile_levels'],dtype=np.float64)
    output=[]
    cases={case['case_id']:case for case in bundle['manifest']['cases']}
    import sys
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    from evaluate_dp5q import emitted_nmr,parse_manual_text,request_for_case
    for owned in rows:
        case_id=owned['case_id']
        reference=references[case_id]
        folder=bundle['execution']['case_paths'][case_id]
        for arm,record in reference['arms'].items():
            if record.get('prediction_status')!='PASS':
                continue
            path=args.reference/folder/(arm+'-legacy.npz')
            values=safe_reference_arrays(path)
            spectrum=bundle['manifest']['spectra'][cases[case_id]['spectrum_id']]
            carbon_omits=any(label.startswith('C') and label[1:].isdigit() for label in spectrum['omitted_labels'])
            if spectrum['equivalence_groups'] or carbon_omits:
                result={'status':'unresolved_group_mapping','diagnostic_only':True}
            elif record.get('status')!='assessed':
                result={'status':'not_compared_invalid_reference_fit','legacy_status':record.get('status'),'legacy_reason':record.get('reason'),'diagnostic_only':True}
            elif len(values['quantiles'])!=len(values['mu']):
                result={'status':'unresolved_group_mapping','diagnostic_only':True}
            else:
                result=refit_reference(values['quantiles'],values['mu'],values['sigma'],values['observed'],levels)
            if not spectrum['equivalence_groups'] and not carbon_omits and 'assignment_indices' in values:
                parsed=parse_manual_text(emitted_nmr(spectrum))['carbon_shifts']
                request=request_for_case(cases[case_id],spectrum)
                result['assignment_controls']=assignment_controls(values['mean'],values['quantiles'],spectrum['carbon_shifts'],parsed,
                    values['assignment_indices'],fixed=request.fixed_assignments)
            result.update(case_id=case_id,arm=arm,reference_arrays_sha256=sha256(path.read_bytes()).hexdigest(),
                          owned_assessment_status=owned['arms'][arm]['status'],owned_assessment_reason=owned['arms'][arm].get('reason'))
            output.append(result)
    validate_input_seals(bundle,reference_files)
    import checksi.dp5q.scoring as scoring
    import scipy
    receipt={'status':'COMPLETE diagnostic same-input fitter and assignment controls','case_arm_count':len(output),'results':output,
             'manifest_sha256':sha256(args.manifest.read_bytes()).hexdigest(),'owned_evidence_seal_sha256':bundle['seal_sha256'],
             'reference_receipt_sha256':sha256((args.reference/'receipt.json').read_bytes()).hexdigest(),
             'scoring_sha256':sha256(Path(scoring.__file__).read_bytes()).hexdigest(),
             'versions':{'python':sys.version,'numpy':np.__version__,'scipy':scipy.__version__},
             'script_sha256':sha256(Path(__file__).read_bytes()).hexdigest(),
             'scope':'Diagnostic-only same-reference-input fitter/N5 controls and N8 observation precision control. Does not replace an owned refusal or claim final score parity.'}
    with args.output.open('x') as stream:
        json.dump(receipt,stream,indent=2,allow_nan=False)
        stream.write('\n')
    print(json.dumps({'case_arm_count':len(output),'refit_refusals':sum(row['status']=='owned_refit_refused' for row in output)}))


if __name__=='__main__':
    main()
