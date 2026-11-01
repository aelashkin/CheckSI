"""Historical SDF-corpus development campaign; not the product acceptance path."""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
import math
import io
import os
from pathlib import Path
import re
import sys
import subprocess
import time
from time import perf_counter

SOURCE_REVISION = '8f8e22364654bbce71dcb6abb414eee793b00d92'
SOURCE_URL = 'https://github.com/aelashkin/DP5/tree/'+SOURCE_REVISION
PAPER_URL = 'https://pubs.rsc.org/en/content/articlehtml/2026/sc/d5sc06988b'
MAX_INPUT_BYTES = 16_384
EXCLUDED_STEREO = {'AT3','TS3A','TS4','NL1A'}
PILOT_IDS = ('revision/S1a','revision/S1b','revision/S11a','revision/S11b',
             'stereo/AT1_1','stereo/AT1_2','revision/S2a','revision/S2b')
OUTPUT_BUDGET_BYTES = 512*1024*1024
ARTIFACT_FILE_BYTES = 4*1024*1024
CASE_ARTIFACT_RESERVE_BYTES = 9*ARTIFACT_FILE_BYTES
CONTROL_RESERVE_BYTES = 16*1024*1024
CASE_ARTIFACT_NAMES = {'F.sdf','emitted.nmr','F-heads.npz','A-initial.sdf',
                       'A.sdf','A-heads.npz','B-geometry.npz','C-geometry.npz'}


class CampaignBudgetError(ValueError):
    """Execution cannot remain inside its declared resource reservation."""


def output_bytes(root: Path) -> int:
    """Account for live regular files, including partial files and the child log."""
    total=0
    for path in root.rglob('*'):
        try:
            if path.is_file():
                total+=path.stat().st_size
        except FileNotFoundError:
            # result.json.tmp may atomically become result.json during a parent poll.
            # The child's stable pre-case reservation supplies the hard size bound.
            continue
    return total


def reserve_case_output(root: Path) -> None:
    """Reserve all nine hard-limited case files plus bounded control/log headroom."""
    if output_bytes(root)+CASE_ARTIFACT_RESERVE_BYTES+CONTROL_RESERVE_BYTES>OUTPUT_BUDGET_BYTES:
        raise CampaignBudgetError('Campaign output budget cannot reserve another complete case.')


def _write_control(path: Path, value: dict) -> None:
    """Publish one bounded terminal/control record without replacing evidence."""
    data=json.dumps(value,indent=2,allow_nan=False)+'\n'
    if len(data.encode())>2*1024*1024:
        raise ValueError('Campaign control receipt exceeds its reservation.')
    with path.open('x') as stream:
        stream.write(data)


def _reap(process) -> int:
    """Bounded termination is shared by budget, timeout and interruption paths."""
    if process.poll() is None:
        process.terminate()
        try:
            return process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            return process.wait(timeout=3)
    return process.wait()


def publish_case(manifest: dict,index: int,source: Path,root: Path,models,*,batch: bool=True) -> None:
    """Reserve before computation and atomically publish one bounded case result."""
    try:
        reserve_case_output(root)
        output=root/f'case-{index:03d}' if batch else root
        if batch:
            output.mkdir(exist_ok=False)
        result=calculate_case(manifest,manifest['cases'][index],source,output,models)
        artifacts={}
        for path in output.iterdir():
            if path.name not in CASE_ARTIFACT_NAMES or path.is_symlink() or not path.is_file():
                raise ValueError('Unexpected or nonregular case artifact: '+path.name)
            data=_bounded_bytes(path,ARTIFACT_FILE_BYTES)
            artifacts[path.name]={'bytes':len(data),'sha256':sha256(data).hexdigest()}
        if len(artifacts)>8:
            raise CampaignBudgetError('A case exceeded its reserved artifact count.')
        result['artifacts']=artifacts
        data=json.dumps(result,allow_nan=False,separators=(',',':'))+'\n'
        if len(data.encode())>ARTIFACT_FILE_BYTES:
            raise CampaignBudgetError('Case result exceeds its explicit artifact reservation.')
        if output_bytes(root)+ARTIFACT_FILE_BYTES+CONTROL_RESERVE_BYTES>OUTPUT_BUDGET_BYTES:
            raise CampaignBudgetError('Case result cannot retain the reserved control headroom.')
        with (output/'result.json.tmp').open('x') as stream:
            stream.write(data)
        if (output/'result.json').exists():
            raise ValueError('Result evidence already exists.')
        os.replace(output/'result.json.tmp',output/'result.json')
    except CampaignBudgetError as exc:
        _write_control(root/'budget-failure.json',{'status':'FAILED_BUDGET','reason':str(exc),'index':index})
        raise


def _labels(text: str) -> list[str]:
    labels = []
    for token in re.split(r',|\bor\b',text,flags=re.IGNORECASE):
        token = token.strip()
        if not token or token.lower() == 'any':
            continue
        match = re.fullmatch(r'([A-Za-z]+)\s*([1-9][0-9]*)',token)
        if not match:
            raise ValueError('Invalid atom label in manual NMR input.')
        labels.append(match[1].capitalize()+match[2])
    return labels


def parse_manual_text(text: str) -> dict:
    """Parse bounded carbon input and explicit constraints without inference."""
    if len(text.encode('utf-8')) > MAX_INPUT_BYTES:
        raise ValueError('Manual NMR input exceeds its explicit size limit.')
    sections, current = [], []
    for line in text.splitlines():
        if line.strip():
            current.append(line.strip())
        elif current:
            sections.append(current)
            current = []
    if current:
        sections.append(current)
    if not sections:
        raise ValueError('Manual NMR input is empty.')
    line = ' '.join(sections[0])
    entries, start, depth = [], 0, 0
    for index, character in enumerate(line):
        if character == '(':
            depth += 1
        elif character == ')':
            depth -= 1
        elif character == ',' and depth == 0:
            entries.append(line[start:index])
            start = index+1
        if depth < 0:
            raise ValueError('Unbalanced manual NMR parentheses.')
    if depth:
        raise ValueError('Unbalanced manual NMR parentheses.')
    entries.append(line[start:])
    shifts, assignments = [], []
    for entry in entries:
        match = re.fullmatch(r'\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*(?:\((.*?)\))?\s*',entry)
        if not match:
            raise ValueError('Invalid carbon shift in manual NMR input.')
        value = float(match[1])
        if not math.isfinite(value):
            raise ValueError('Experimental shifts must be finite.')
        shifts.append(value)
        assignments.append(_labels(match[2] or ''))
    groups, omitted = [], []
    for section in sections[2:]:
        for line in section:
            match = re.match(r'^OMIT\b\s*(.*)',line,re.IGNORECASE)
            if match:
                omitted.extend(_labels(match[1]))
            else:
                group = _labels(line)
                if group:
                    groups.append(group)
    return {'carbon_shifts':shifts,'carbon_assignments':assignments,
            'equivalence_groups':groups,'omitted_labels':omitted}


def emitted_nmr(spectrum: dict) -> str:
    """Match the checker adapter's four-decimal carbon-only transcript."""
    def rendered(value):
        text = f'{value:.4f}'.rstrip('0').rstrip('.')
        return text if '.' in text else text+'.0'
    entries = [rendered(value)+'('+(','.join(labels) or 'any')+')'
               for value,labels in zip(spectrum['carbon_shifts'],spectrum['carbon_assignments'])]
    text = ','.join(entries)+'\n\n0.0(any)\n\n'
    text += ''.join(','.join(group)+'\n' for group in spectrum['equivalence_groups'])
    if spectrum['omitted_labels']:
        text += 'OMIT '+','.join(spectrum['omitted_labels'])+'\n'
    return text


def request_for_case(case: dict, spectrum: dict):
    """Map explicit source labels to the complete original-index group order."""
    from checksi.dp5q.assignment import build_groups
    from checksi.dp5q.contracts import CheckRequest

    if case['identity'] is None or 'input_refusal' in spectrum:
        raise ValueError('Unparsed structure or carbon input.')
    if any(label.startswith('C') and re.fullmatch(r'C[1-9][0-9]*',label) for label in spectrum['omitted_labels']):
        raise ValueError('Carbon omissions are not complete-spectrum product requests.')
    explicit = [group for group in spectrum['equivalence_groups'] if all(re.fullmatch(r'C[1-9][0-9]*',label) for label in group)]
    groups = build_groups(case['identity']['carbon_indices'],explicit)
    lookup = {label:index for index,group in enumerate(groups) for label in group.labels}
    fixed = {}
    for peak,labels in enumerate(spectrum['carbon_assignments']):
        if labels:
            indices = {lookup[label] for label in labels}
            if len(indices)!=1 or next(iter(indices)) in fixed:
                raise ValueError('Source labels do not identify one complete nonduplicated group.')
            fixed[next(iter(indices))] = peak
    return CheckRequest(compound_id=case['case_id'],smiles=case['identity']['isomeric_smiles'],
                        carbon_shifts=spectrum['carbon_shifts'],carbon_groups=explicit,fixed_assignments=fixed)


def sdf_roundtrip(mol):
    """Use the actual RDKit writer/parser boundary, without manual rounding."""
    from rdkit import Chem
    stream = io.StringIO()
    with Chem.SDWriter(stream) as writer:
        writer.write(mol)
    text = stream.getvalue()
    restored = Chem.MolFromMolBlock(text,removeHs=False,sanitize=True)
    if restored is None:
        raise ValueError('The exact SDF roundtrip could not be read.')
    return restored,text


def legacy_geometry(mol):
    """Frozen checker ETKDG (v1) 30-to-one selection; reference evaluation only."""
    import numpy as np
    from rdkit import Chem
    from rdkit.Chem import AllChem

    work = Chem.AddHs(Chem.Mol(mol))
    if not AllChem.MMFFHasAllMoleculeParams(work):
        raise ValueError('legacy_mmff_parameters_missing')
    parameters = AllChem.ETKDG()
    parameters.randomSeed = 0xD05
    parameters.numThreads = 1
    ids = list(AllChem.EmbedMultipleConfs(work,30,parameters))
    if not ids:
        raise ValueError('legacy_embedding_failed')
    optimized = AllChem.MMFFOptimizeMoleculeConfs(work,numThreads=1,mmffVariant='MMFF94s',maxIters=500)
    eligible = [(energy,index) for (status,energy),index in zip(optimized,ids) if status!=-1 and math.isfinite(energy)]
    best = min(eligible)[1] if eligible else ids[0]
    selected = Chem.Mol(work,confId=best)
    initial, initial_sdf = sdf_roundtrip(selected)
    final, final_sdf = sdf_roundtrip(Chem.AddHs(initial))
    audit = {'algorithm':'ETKDG (v1)','requested':30,'seed':0xD05,'mmff_variant':'MMFF94s','max_iterations':500,
             'selected_conformer_id':best,'fallback_to_first':not bool(eligible),
             'candidates':[{'conformer_id':index,'status':int(status),'energy_kcal':float(energy) if np.isfinite(energy) else None} for (status,energy),index in zip(optimized,ids)]}
    return final,initial_sdf,final_sdf,audit


def _bounded_bytes(path: Path, maximum: int) -> bytes:
    with path.open('rb') as stream:
        data = stream.read(maximum+1)
    if len(data)>maximum:
        raise ValueError('Input file exceeds its explicit size limit: '+path.name)
    return data


def _source_record(root: Path, path: Path, inventory: dict | None = None) -> dict:
    """Read only bounded, explicitly located public corpus files."""
    relative = path.resolve().relative_to(root.resolve()).as_posix()
    data = _bounded_bytes(path,256*1024)
    digest = sha256(data).hexdigest()
    if inventory is not None and (relative not in inventory or inventory[relative]['sha256']!=digest):
        raise ValueError('Corpus differs from its frozen inventory: '+relative)
    return {'path':relative,'bytes':len(data),'sha256':digest,'url':SOURCE_URL+'/'+relative}


def freeze_manifest(source: Path, inventory_path: Path) -> dict:
    """Freeze all 370 source candidates before observing comparison outcomes."""
    from rdkit import Chem

    inventory_data = json.loads(_bounded_bytes(inventory_path,2*1024*1024))
    if inventory_data.get('revision') != SOURCE_REVISION:
        raise ValueError('Unexpected corpus inventory revision.')
    inventory = {row['path']:row for group in inventory_data['sets'].values() for row in group['file_manifest']}
    spectra, cases, exclusions = {}, [], []
    for dataset in ('reassignments','stereo_examples'):
        folders = sorted(path for path in (source/dataset).iterdir() if path.is_dir())
        for folder in folders:
            family = folder.name
            if dataset=='stereo_examples' and family in EXCLUDED_STEREO:
                exclusions.append({'family_id':'stereo/'+family,'reason':'source README excludes missing carbon NMR',
                                   'files':[_source_record(source,p,inventory) for p in sorted(folder.iterdir()) if p.is_file()]})
                continue
            pattern = re.compile(re.escape(family)+(r'([ab])_\.sdf' if dataset=='reassignments' else r'_([1-9][0-9]*)\.sdf'))
            for sdf in sorted(folder.glob('*.sdf')):
                match = pattern.fullmatch(sdf.name)
                if not match:
                    exclusions.append({'file':_source_record(source,sdf,inventory),'reason':'not a numbered candidate; excluded extra file'})
                    continue
                suffix = match[1]
                nmr = folder/(family+suffix+'_NMR' if family=='S13' and dataset=='reassignments' else family+'_NMR' if dataset=='reassignments' else family+'NMR')
                spectrum_id = nmr.relative_to(source).as_posix()
                if spectrum_id not in spectra:
                    record = _source_record(source,nmr,inventory)
                    try:
                        record.update(parse_manual_text(_bounded_bytes(nmr,MAX_INPUT_BYTES).decode('utf-8')))
                    except ValueError as exc:
                        record['input_refusal'] = str(exc)
                    spectra[spectrum_id] = record
                file_record = _source_record(source,sdf,inventory)
                mol = Chem.MolFromMolBlock(_bounded_bytes(sdf,256*1024).decode('utf-8'),removeHs=False,sanitize=True)
                identity = None if mol is None else {
                    'isomeric_smiles':Chem.MolToSmiles(Chem.RemoveHs(mol)),
                    'atom_count':mol.GetNumAtoms(),
                    'carbon_indices':[atom.GetIdx() for atom in mol.GetAtoms() if atom.GetAtomicNum()==6],
                    'formal_charge':sum(atom.GetFormalCharge() for atom in mol.GetAtoms()),
                    'title':mol.GetProp('_Name') if mol.HasProp('_Name') else '',
                }
                revision = dataset=='reassignments'
                case_id = ('revision/'+family+suffix) if revision else ('stereo/'+family+'_'+suffix)
                cases.append({'case_id':case_id,'family_id':('revision/' if revision else 'stereo/')+family,
                              'dataset':dataset,'sdf':file_record,'spectrum_id':spectrum_id,'identity':identity,
                              'source_label_correct':suffix=='b' if revision else suffix=='1',
                              'label_evidence':PAPER_URL+'#sect5.3' if revision else SOURCE_URL+'/stereo_examples/README.md',
                              'label_status':'source-labeled regression example; not independently re-adjudicated',
                              'pairing_status':'unscorable_different_spectra' if revision and family=='S13' else 'shared_family_spectrum',
                              'training_exposure':'public developmental corpus; run audit_dp5q_corpus.py for measured per-split overlap'})
    counts = {dataset:sum(case['dataset']==dataset for case in cases) for dataset in ('reassignments','stereo_examples')}
    if counts!={'reassignments':48,'stereo_examples':322} or len({case['case_id'] for case in cases})!=370:
        raise ValueError('The source inventory does not match the approved 370-candidate campaign.')
    return {'format_version':1,'source_revision':SOURCE_REVISION,'source_url':SOURCE_URL,
            'inventory_sha256':sha256(inventory_path.read_bytes()).hexdigest(),
            'license_url':SOURCE_URL.replace('/tree/','/blob/')+'/license.md',
            'source_label_evidence':{'revision':PAPER_URL+'#sect5.3','stereo':SOURCE_URL+'/stereo_examples/README.md'},
            'readme_hashes':{dataset:_source_record(source,source/dataset/'README.md',inventory) for dataset in counts},
            'candidate_count':370,'family_counts':{'reassignments':24,'stereo_examples':42},'candidate_counts':counts,
            'interpretation':'Descriptive regression only; not an independent holdout. Dataset overlap is measured separately by audit_dp5q_corpus.py. Refusals are unassessed, never acceptance. S13 lacks a common authenticated spectrum. JB13A/B labels require adjudication and are excluded from aggregate labeled metrics.',
            'spectra':spectra,'cases':cases,'exclusions':exclusions,
            'gates':{'thresholds':[.10,.20],'primary_threshold':.10,'max_warm_article_time_ratio':2.,'max_peak_rss_ratio':1.5,'required_correct_rank_improvement':True,'no_lost_successful_revision_family':True}}


def calculate_case(manifest: dict, case: dict, source: Path, output: Path, models=None) -> dict:
    """Run controlled F/A/B/C arms; only this developer child loads models."""
    from dataclasses import replace
    import resource
    import numpy as np
    from rdkit import Chem
    from checksi.dp5q.assignment import build_groups,group_predictions
    from checksi.dp5q.engine import check_compound
    from checksi.dp5q.geometry import GeometryError,prepare_geometry,SINGLE_POLICY
    from checksi.dp5q.network import load_models
    from checksi.dp5q.graph import build_graph
    from checksi.dp5q.scoring import fit_gaussian

    start=perf_counter()
    usage_start=resource.getrusage(resource.RUSAGE_SELF)
    spectrum=manifest['spectra'][case['spectrum_id']]
    for item in (case['sdf'],spectrum):
        _source_record(source,source/item['path'],{item['path']:item})
    mol=Chem.MolFromMolBlock(_bounded_bytes(source/case['sdf']['path'],256*1024).decode(),removeHs=False)
    if mol is None:
        raise ValueError('The pinned input structure cannot be parsed.')
    mol=Chem.AddHs(mol)
    frozen,frozen_sdf=sdf_roundtrip(mol)
    (output/'F.sdf').write_text(frozen_sdf)
    (output/'emitted.nmr').write_text(emitted_nmr(spectrum))
    load_start=perf_counter()
    if models is None:
        models=load_models()
    load_seconds=perf_counter()-load_start
    request=request_for_case(case,spectrum)
    result={'case_id':case['case_id'],'family_id':case['family_id'],'source_label_correct':case['source_label_correct'],
            'geometry_input_path':'source_sdf_atom_order; development-only, not product SMILES',
            'spectrum_id':case['spectrum_id'],'model_id':models.metadata['model_id'],'arms':{},'model_load_seconds':load_seconds,
            'legacy_rounding_max_ppm':max(abs(a-b) for a,b in zip(spectrum['carbon_shifts'],parse_manual_text(emitted_nmr(spectrum))['carbon_shifts']))}

    class RecordingModels:
        def __init__(self):
            self.metadata=models.metadata
            self.predictions=[]
        def predict(self,value):
            prediction=models.predict(value)
            self.predictions.append(prediction)
            return prediction

    def assess(arm,*,frozen_mol=None,prepared_geometry=None):
        recorded=RecordingModels()
        assessed=check_compound(request,recorded,frozen_mol=frozen_mol,prepared_geometry=prepared_geometry)
        value=assessed.model_dump(mode='json')
        if arm in ('F','A'):
            try:
                prediction=recorded.predictions[0] if recorded.predictions else models.predict(frozen_mol)
            except ValueError as exc:
                value['prediction_refusal']=getattr(exc,'code',str(exc))
            else:
                graph=build_graph(frozen_mol,models.tokens)
                np.savez_compressed(output/(arm+'-heads.npz'),atom_indices=prediction.atom_indices,
                                    mean=prediction.mean,quantiles=prediction.quantiles,
                                    graph_atom=graph.atom_tokens,graph_atom_index=graph.atom_index,
                                    graph_connectivity=graph.connectivity,graph_distance=graph.distances)
                value['heads_sha256']=sha256((output/(arm+'-heads.npz')).read_bytes()).hexdigest()
                value['rbf_sha256']=sha256(graph.distance_rbf.tobytes()).hexdigest()
        if assessed.status=='assessed':
            groups=build_groups(recorded.predictions[0].atom_indices,request.carbon_groups)
            _,quantiles=group_predictions(np.stack([p.mean for p in recorded.predictions]),
                                         np.stack([p.quantiles for p in recorded.predictions]),groups)
            fitted=[[fit_gaussian(q,models.metadata['quantile_levels']) for q in row] for row in quantiles]
            value['fit_parameters']=[[{'mu':fit.mu,'sigma':fit.sigma} for fit in row] for row in fitted]
        result['arms'][arm]=value
        return value

    fixed=assess('F',frozen_mol=frozen)
    if case['pairing_status']!='shared_family_spectrum':
        for arm in ('A','B','C'):
            result['arms'][arm]={'status':'not_applicable','reason':'unadjudicated_common_spectrum','score_raw':None,'log_score':None}
        fixed.update(status='not_applicable',reason='unadjudicated_common_spectrum',score_raw=None,log_score=None)
    elif fixed['reason']=='strict_nmr_coverage_mismatch':
        for arm in ('A','B','C'):
            result['arms'][arm]={'status':'not_applicable','reason':fixed['reason'],'score_raw':None,'log_score':None,
                                  'diagnostics':{'message':'Invariant input coverage failed before geometry generation.'}}
    else:
        legacy_start=perf_counter()
        try:
            legacy,initial_sdf,final_sdf,audit=legacy_geometry(mol)
        except (ValueError,RuntimeError) as exc:
            result['arms']['A']={'status':'not_applicable','reason':'legacy_geometry_failed','score_raw':None,'log_score':None,'diagnostics':{'message':str(exc)}}
        else:
            (output/'A-initial.sdf').write_text(initial_sdf)
            (output/'A.sdf').write_text(final_sdf)
            result['legacy_geometry']=audit
            result['legacy_geometry_seconds']=perf_counter()-legacy_start
            assess('A',frozen_mol=legacy)
        generation_start=perf_counter()
        try:
            common=prepare_geometry(mol,ensemble=True)
        except GeometryError as exc:
            for arm in ('B','C'):
                result['arms'][arm]={'status':'not_applicable','reason':exc.reason,'score_raw':None,'log_score':None,'diagnostics':{'message':str(exc)}}
        else:
            result['shared_generation_seconds']=perf_counter()-generation_start
            single=replace(common,conformer_ids=(common.conformer_ids[0],),energies_kj=common.energies_kj[:1],
                           populations=np.array([1.]),policy=SINGLE_POLICY,
                           diagnostics={'shared_candidate_audit':common.diagnostics,'selected_count':1})
            assess('B',prepared_geometry=single)
            assess('C',prepared_geometry=common)
            result['shared_generation_audit']=common.diagnostics
            for arm,geometry in (('B',single),('C',common)):
                coordinates=np.stack([geometry.mol.GetConformer(i).GetPositions() for i in geometry.conformer_ids])
                np.savez_compressed(output/(arm+'-geometry.npz'),coordinates=coordinates,
                                    conformer_ids=np.array(geometry.conformer_ids),energies_kj=geometry.energies_kj,populations=geometry.populations)
    usage=resource.getrusage(resource.RUSAGE_SELF)
    result['resources']={'wall_seconds':perf_counter()-start,'user_cpu_seconds':usage.ru_utime-usage_start.ru_utime,'system_cpu_seconds':usage.ru_stime-usage_start.ru_stime,
                         'max_rss_bytes':usage.ru_maxrss if sys.platform=='darwin' else usage.ru_maxrss*1024,
                         'rss_scope':'Cumulative child F/A/B/C peak; not independent B/C RSS evidence.'}
    return result


def run_campaign(args) -> dict:
    """Supervise a serial child; publish PASS only after every execution/resource gate."""
    import resource
    manifest=json.loads(_bounded_bytes(args.manifest,2*1024*1024))
    if (manifest.get('source_revision')!=SOURCE_REVISION or len(manifest.get('cases',[]))!=370
            or len({case['case_id'] for case in manifest['cases']})!=370):
        raise ValueError('The campaign requires the frozen 370-candidate manifest.')
    ids={case['case_id']:index for index,case in enumerate(manifest['cases'])}
    indices=list(range(370)) if args.full else [ids[name] for name in PILOT_IDS]
    args.output.mkdir(parents=True,exist_ok=False)
    receipt={'manifest_sha256':sha256(args.manifest.read_bytes()).hexdigest(),'case_ids':[manifest['cases'][i]['case_id'] for i in indices],
             'geometry_input_path':'source_sdf_atom_order; development-only, not product SMILES',
             'source_revision':SOURCE_REVISION,'mode':'full' if args.full else 'pilot8','per_case_wall_limit_seconds':600,
             'CPU_budget_seconds':36000,'reference_CPU_reserve_seconds':3600,'output_budget_bytes':OUTPUT_BUDGET_BYTES,
             'case_artifact_reserve_bytes':CASE_ARTIFACT_RESERVE_BYTES,'control_reserve_bytes':CONTROL_RESERVE_BYTES,
             'scope':'Offline descriptive regression; original source labels, no calibration/general-accuracy claim.'}
    checkout=Path(__file__).resolve().parents[1]
    code_paths=[*sorted((checkout/'src/checksi/dp5q').glob('*.py')),Path(__file__).resolve(),
                checkout/'scripts/dp5q-reference/campaign.py',checkout/'scripts/dp5q_reference.py']
    code_hashes={path.relative_to(checkout).as_posix():sha256(_bounded_bytes(path,512*1024)).hexdigest() for path in code_paths}
    receipt['code_hashes']=code_hashes
    receipt['git_commit']=subprocess.run(['git','rev-parse','HEAD'],cwd=checkout,capture_output=True,text=True,check=True).stdout.strip()
    _write_control(args.output/'run.json',receipt)
    command=[sys.executable,str(Path(__file__).resolve()),'_batch','--manifest',str(args.manifest.resolve()),
             '--source',str(args.source.resolve()),'--output',str(args.output.resolve()),
             '--indices',','.join(map(str,indices))]
    child_env=os.environ.copy()
    child_env.update(OPENBLAS_NUM_THREADS='1',OMP_NUM_THREADS='1',VECLIB_MAXIMUM_THREADS='1',PYTHONDONTWRITEBYTECODE='1',PYTHONNOUSERSITE='1')
    completed=0
    point=time.monotonic()
    timed_out=False
    process=None
    failure=None
    budget_failure=None
    returncode=None
    before=resource.getrusage(resource.RUSAGE_CHILDREN)
    try:
        reserve_case_output(args.output)
        with (args.output/'owned-child.log').open('xb') as log:
            process=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,env=child_env,shell=False)
            try:
                while True:
                    if output_bytes(args.output)+CONTROL_RESERVE_BYTES>OUTPUT_BUDGET_BYTES:
                        raise CampaignBudgetError('Campaign artifact budget exhausted its reserved control headroom.')
                    marker=args.output/'budget-failure.json'
                    if marker.exists():
                        detail=json.loads(_bounded_bytes(marker,2*1024*1024))
                        raise CampaignBudgetError(detail.get('reason','Child output reservation failed.'))
                    if process.poll() is not None:
                        break
                    count=completed
                    while count<len(indices) and (args.output/f'case-{indices[count]:03d}'/'result.json').is_file():
                        count+=1
                    if count!=completed:
                        completed=count
                        point=time.monotonic()
                        print(json.dumps({'completed':completed,'submitted':len(indices)}),flush=True)
                    if time.monotonic()-point>600:
                        timed_out=True
                        raise RuntimeError('Owned case exceeded its 600-second wall deadline.')
                    time.sleep(.2)
            finally:
                returncode=_reap(process)
    except CampaignBudgetError as exc:
        failure=str(exc)
        budget_failure=str(exc)
    except BaseException as exc:
        failure=str(exc) or type(exc).__name__
        if isinstance(exc,(KeyboardInterrupt,SystemExit)):
            failure='Interrupted: '+failure
    after=resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu=after.ru_utime+after.ru_stime-before.ru_utime-before.ru_stime
    results=[]
    paths={}
    try:
        if any(sha256(_bounded_bytes(checkout/path,512*1024)).hexdigest()!=digest for path,digest in code_hashes.items()):
            raise ValueError('Calculation source changed during the campaign; evidence is stale.')
        for index in indices:
            path=args.output/f'case-{index:03d}'/'result.json'
            if path.is_file():
                result=json.loads(_bounded_bytes(path,ARTIFACT_FILE_BYTES))
                if result.get('case_id')!=manifest['cases'][index]['case_id']:
                    raise ValueError('Owned result identity differs from its submitted case.')
                results.append(result)
                paths[result['case_id']]=f'case-{index:03d}'
        if cpu>36000:
            raise CampaignBudgetError('Owned CPU budget exceeded 36000 seconds.')
        if output_bytes(args.output)+CONTROL_RESERVE_BYTES>OUTPUT_BUDGET_BYTES:
            raise CampaignBudgetError('Owned artifacts exceeded the reserved output budget.')
        if len(results)!=len(indices) or returncode!=0 or timed_out:
            raise RuntimeError('Campaign execution incomplete; not a scientific refusal.')
    except CampaignBudgetError as exc:
        failure=str(exc)
        budget_failure=str(exc)
    except (OSError,ValueError,RuntimeError) as exc:
        failure=failure or str(exc)
    receipt.update(completed=len(results),returncode=returncode,timed_out=timed_out,
                   child_CPU_seconds=cpu,case_paths=paths,
                   status=('FAILED_BUDGET: '+budget_failure if budget_failure else
                           'INCOMPLETE execution; not a scientific refusal' if failure else 'PASS owned campaign execution'))
    if failure:
        receipt['failure']=failure
    _write_control(args.output/'execution.json',receipt)
    if budget_failure:
        raise CampaignBudgetError(budget_failure)
    if failure:
        raise RuntimeError(failure)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command',required=True)
    freeze = commands.add_parser('freeze',help='Freeze public source hashes/labels before evaluation')
    freeze.add_argument('--source',type=Path,required=True)
    freeze.add_argument('--inventory',type=Path,required=True)
    freeze.add_argument('--output',type=Path,required=True)
    one=commands.add_parser('_case',help=argparse.SUPPRESS)
    one.add_argument('--manifest',type=Path,required=True)
    one.add_argument('--source',type=Path,required=True)
    one.add_argument('--index',type=int,required=True)
    one.add_argument('--output',type=Path,required=True)
    batch=commands.add_parser('_batch',help=argparse.SUPPRESS)
    batch.add_argument('--manifest',type=Path,required=True)
    batch.add_argument('--source',type=Path,required=True)
    batch.add_argument('--indices',required=True)
    batch.add_argument('--output',type=Path,required=True)
    run=commands.add_parser('run',help='Run a bounded eight-case pilot or the explicitly selected full campaign')
    run.add_argument('--manifest',type=Path,required=True)
    run.add_argument('--source',type=Path,required=True)
    run.add_argument('--output',type=Path,required=True)
    run.add_argument('--full',action='store_true')
    args = parser.parse_args()
    if args.command=='freeze':
        manifest = freeze_manifest(args.source.resolve(),args.inventory)
        args.output.parent.mkdir(parents=True,exist_ok=True)
        with args.output.open('x') as stream:
            json.dump(manifest,stream,indent=2,allow_nan=False)
            stream.write('\n')
        print(json.dumps({'candidate_count':manifest['candidate_count'],'candidate_counts':manifest['candidate_counts'],'sha256':sha256(args.output.read_bytes()).hexdigest()}))
    elif args.command in ('_case','_batch'):
        import resource
        import faulthandler
        resource.setrlimit(resource.RLIMIT_CPU,(600,600) if args.command=='_case' else (36000,36000))
        resource.setrlimit(resource.RLIMIT_FSIZE,(ARTIFACT_FILE_BYTES,ARTIFACT_FILE_BYTES))
        resource.setrlimit(resource.RLIMIT_CORE,(0,0))
        faulthandler.dump_traceback_later(600,repeat=True)
        for name in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','VECLIB_MAXIMUM_THREADS'):
            os.environ[name]='1'
        manifest=json.loads(_bounded_bytes(args.manifest,2*1024*1024))
        indices=[args.index] if args.command=='_case' else [int(value) for value in args.indices.split(',')]
        from checksi.dp5q.network import load_models
        import importlib.metadata
        models=load_models()
        if args.command=='_batch':
            startup={'model_initializations':1,'model_id':models.metadata['model_id'],
                     'weights_sha256':models.metadata['files']['weights.npz']['sha256'],
                     'python':sys.version,'versions':{name:importlib.metadata.version(name) for name in ('numpy','scipy','rdkit')},
                     'native_threads_requested':1,'native_threads_measured':False}
            with (args.output/'startup.json').open('x') as stream:
                json.dump(startup,stream,indent=2)
                stream.write('\n')
        try:
            for index in indices:
                publish_case(manifest,index,args.source,args.output,models,batch=args.command=='_batch')
        finally:
            faulthandler.cancel_dump_traceback_later()
    elif args.command=='run':
        print(json.dumps(run_campaign(args),indent=2))


if __name__=='__main__':
    main()
