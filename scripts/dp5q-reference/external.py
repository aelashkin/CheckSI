"""Freeze small BMRB applicability cases without inventing scientific labels."""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import shlex

MAXIMUM_FILE_BYTES=256*1024
ACCESSIONS=('bmse000028','bmse000967','bmse000041','bmse000042')


def selected_loops(text: str,prefix: str) -> list[dict[str,str]]:
    """Read bounded selected simple STAR loops; refuse unsupported multiline fields."""
    if len(text.encode('utf-8'))>MAXIMUM_FILE_BYTES:
        raise ValueError('STAR file exceeds pinned input bound.')
    lines=text.splitlines()
    output=[]
    position=0
    while position<len(lines):
        if lines[position].strip()!='loop_':
            position+=1
            continue
        position+=1
        tags=[]
        while position<len(lines) and (not lines[position].strip() or lines[position].strip().startswith('_')):
            if lines[position].strip():
                tags.append(lines[position].strip())
            position+=1
        selected=bool(tags) and tags[0].startswith(prefix)
        tokens=[]
        while position<len(lines) and lines[position].strip()!='stop_':
            line=lines[position]
            if selected:
                if line.startswith(';'):
                    raise ValueError('Selected STAR multiline fields require explicit adjudication.')
                tokens.extend(shlex.split(line,comments=True,posix=True))
                if len(tokens)>4096*max(1,len(tags)):
                    raise ValueError('Selected STAR loop exceeds row bound.')
            position+=1
        if position==len(lines):
            raise ValueError('STAR loop has no stop marker.')
        position+=1
        if not selected:
            continue
        if not all(tag.startswith(prefix) for tag in tags) or len(set(tags))!=len(tags):
            raise ValueError('Selected STAR loop has inconsistent tags.')
        if len(tokens)%len(tags):
            raise ValueError('Selected STAR loop contains a partial row.')
        names=[tag[len(prefix):] for tag in tags]
        output.extend(dict(zip(names,tokens[start:start+len(tags)])) for start in range(0,len(tokens),len(tags)))
    return output


def scalar_fields(text: str,prefix: str) -> list[dict[str,str]]:
    """Keep simple relevant scalar values, including their original STAR tag."""
    output=[]
    for line in text.splitlines():
        if not line.strip().startswith(prefix):
            continue
        fields=shlex.split(line,comments=True,posix=True)
        if len(fields)==2:
            output.append({'tag':fields[0],'value':fields[1]})
        else:
            raise ValueError('Relevant STAR scalar has an unsupported representation.')
    return output


def topology_mapping(text: str,mol_path: Path) -> dict:
    """Enumerate heavy-atom topology matches; this does not adjudicate solution state."""
    from rdkit import Chem
    from rdkit.Chem import rdMolDescriptors
    mol=Chem.MolFromMolFile(str(mol_path),removeHs=False)
    if mol is None:
        raise ValueError('Pinned BMRB MOL does not parse.')
    for atom in mol.GetAtoms():
        atom.SetIntProp('_bmrb_mol_index',atom.GetIdx()+1)
    heavy=Chem.RemoveHs(mol)
    atoms=selected_loops(text,'_Chem_comp_atom.')
    bonds=selected_loops(text,'_Chem_comp_bond.')
    query=Chem.RWMol()
    indices={}
    for row in atoms:
        if row['Type_symbol']=='H':
            continue
        atom=Chem.Atom(row['Type_symbol'])
        atom.SetNoImplicit(True)
        if row['Atom_ID'] in indices:
            raise ValueError('Duplicate BMRB heavy atom ID.')
        indices[row['Atom_ID']]=query.AddAtom(atom)
    for row in bonds:
        left,right=row['Atom_ID_1'],row['Atom_ID_2']
        if left not in indices or right not in indices:
            continue
        order={'SING':Chem.BondType.SINGLE,'DOUB':Chem.BondType.DOUBLE}.get(row['Value_order'])
        if order is None:
            raise ValueError('Unreviewed BMRB bond order.')
        query.AddBond(indices[left],indices[right],order)
    matches=heavy.GetSubstructMatches(query.GetMol(),uniquify=False,useChirality=False,maxMatches=65)
    if len(matches)>64:
        raise ValueError('BMRB topology mapping exceeds bounded ambiguity limit.')
    if heavy.GetNumAtoms()!=len(indices):
        matches=()
    carbon={row['Atom_ID']:sorted({heavy.GetAtomWithIdx(match[indices[row['Atom_ID']]]).GetIntProp('_bmrb_mol_index') for match in matches})
            for row in atoms if row['Type_symbol']=='C'}
    return {'method':'Exact heavy element/bond topology; all graph isomorphisms up to64, chirality ignored. STAR PDBX_ordinal is not MOL index.',
            'status':'topology_matches_unadjudicated' if matches else 'topology_unresolved',
            'match_count':len(matches),'carbon_bmrb_id_to_possible_mol_indices_1based':carbon,
            'mol_formula':rdMolDescriptors.CalcMolFormula(mol),'provided_depiction_smiles':Chem.MolToSmiles(heavy),
            'limitations':['No solution protonation or tautomer adjudication.','Topology matches do not certify stereochemistry or experimental assignment.']}


def freeze(source: Path,receipt: dict,destination: Path) -> dict:
    """Copy only eight already pinned public inputs and retain raw selected evidence."""
    if sum(item['bytes'] for item in receipt['files'])>1024*1024:
        raise ValueError('External download receipt exceeds total bound.')
    by_key={(item['id'],item['kind']):item for item in receipt['files']}
    if len(receipt['files'])!=8 or len(by_key)!=8 or set(by_key)!={(item,kind) for item in ACCESSIONS for kind in ('mol','str')}:
        raise ValueError('External source inventory is not the exact eight pinned files.')
    checked={}
    for key,item in by_key.items():
        path=source/f'{key[0]}.{key[1]}'
        with path.open('rb') as stream:
            data=stream.read(MAXIMUM_FILE_BYTES+1)
        if len(data)!=item['bytes'] or len(data)>MAXIMUM_FILE_BYTES or sha256(data).hexdigest()!=item['sha256']:
            raise ValueError('Pinned external input failed byte/hash validation.')
        checked[key]=data
    destination.mkdir(parents=True,exist_ok=False)
    for key,data in checked.items():
        (destination/f'{key[0]}.{key[1]}').write_bytes(data)
    records=[]
    for accession in ACCESSIONS:
        text=checked[accession,'str'].decode('utf-8')
        carbon=[row for row in selected_loops(text,'_Atom_chem_shift.') if row['Atom_type']=='C' and row['Atom_isotope_number']=='13']
        record={'accession':accession,'doi':'10.13018/'+accession.upper(),
            'source_page':f'https://bmrb.io/metabolomics/mol_summary/show_data.php?id={accession}',
            'files':{kind:{key:by_key[accession,kind][key] for key in ('source','bytes','sha256')} for kind in ('str','mol')},
            'carbon_assignment_rows':carbon,'chemical_equivalence_groups':[],
            'carbon_assignment_status':'alternative_methyl_assignments_unadjudicated' if any(row['Ambiguity_code']!='1' for row in carbon) else 'explicit_rows_unadjudicated',
            'assigned_shift_list_scalars':scalar_fields(text,'_Assigned_chem_shift_list.'),
            'conditions':selected_loops(text,'_Sample_condition_variable.'),
            'shift_references':selected_loops(text,'_Chem_shift_ref.'),
            'shift_experiments':selected_loops(text,'_Chem_shift_experiment.'),
            'experiments':selected_loops(text,'_Experiment.'),
            'sample_components':selected_loops(text,'_Sample_component.'),
            'topology_mapping':topology_mapping(text,destination/f'{accession}.mol')}
        records.append(record)
    requests=[]
    for left,right in ((ACCESSIONS[0],ACCESSIONS[1]),(ACCESSIONS[2],ACCESSIONS[3])):
        for spectrum in (left,right):
            for candidate in (left,right):
                requests.append({'case_id':spectrum+'__'+candidate,'spectrum_accession':spectrum,'candidate_accession':candidate,
                    'relation':'same_deposition' if spectrum==candidate else 'same_formula_swap',
                    'scientific_status':'unscorable_adjudication_pending','source_label_correct':None,
                    'assessment_executed':False,'unscorable_reason':'solution_state_and_assignment_adjudication_pending'})
    manifest={'schema_version':1,'purpose':'Four public external applicability/mapping cases and eight candidate requests; separate from370 source-labeled regression candidates.',
        'adjudication_status':'NOT RUN: domain reviewer must check identity, solution protonation, shift-set/sample joins and methyl assignment ambiguity before any scientific label.',
        'pretraining_exposure':'Unknown; no untouched holdout or accuracy/calibration claim.',
        'source_notice':'BMRB public archive data; retain accession/DOI/source links. Archive data policy: https://bmrb.io/metabolomics/data_policy.shtml . No raw time-domain archive is included.',
        'records':records,'requests':requests}
    (destination/'manifest.json').write_text(json.dumps(manifest,indent=2,allow_nan=False)+'\n')
    return manifest


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('source','receipt','output'):
        parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args()
    if args.receipt.stat().st_size>64*1024:
        raise ValueError('External source receipt exceeds bound.')
    manifest=freeze(args.source,json.loads(args.receipt.read_text()),args.output)
    print(json.dumps({'record_count':len(manifest['records']),'request_count':len(manifest['requests']),
                      'scientific_status':'unscorable_adjudication_pending'}))


if __name__=='__main__':
    main()
