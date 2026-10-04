"""Measured synthetic evaluation via FastAPI; application never reads answer key."""
import argparse
from importlib.metadata import version
import json
import os
import platform
from pathlib import Path
import time

from .generate_evaluation_pack import generate


def run(output, count):
    output = output.resolve()
    if (output/'benchmark.db').exists():
        raise ValueError('Choose a fresh output directory; existing benchmark data is preserved')
    truth = generate(output/'inputs', count)
    os.environ.update(DATABASE_URL=f'sqlite:///{output / "benchmark.db"}', STORAGE_DIR=str(output/'storage'),
                      JWT_SECRET='isolated-evaluation-secret-at-least-32-characters', AUTO_BOOTSTRAP='true', CELERY_BROKER_URL='')
    from fastapi.testclient import TestClient
    from app.main import app
    elapsed = {}
    start = time.perf_counter()
    with TestClient(app) as client:
        def call(method, path, **kwargs):
            response = getattr(client, method)(path, **kwargs)
            response.raise_for_status()
            return response.json()
        token = call('post', '/api/auth/token', json={'username':'admin','password':'admin'})['access_token']
        client.headers['Authorization'] = f'Bearer {token}'
        project = call('post','/api/projects',json={'name':'Synthetic measured evaluation'})['id']
        base = f'/api/projects/{project}'
        datasets, features = {}, {}
        for name, capture in [('reference','2024-01-01'),('alternate','2025-01-01'),('revision_before','2024-01-01'),('revision_after','2025-01-01')]:
            filename = f'{name}.geojson'
            dataset = call('post',base+'/datasets/upload',files={'file':(filename,(output/'inputs'/filename).read_bytes(),'application/geo+json')},data={'capture_date':capture})
            datasets[name] = dataset
            fields = {'parcel_id': 'khasra_no' if name == 'alternate' else 'parcel_id',
                      'village_code': 'admin_code' if name == 'alternate' else 'village_code',
                      'recorded_area': 'area_sq_m' if name == 'alternate' else 'recorded_area',
                      'area_units': 'units' if name == 'alternate' else 'area_units'}
            call('post',base+f"/datasets/{dataset['id']}/mapping",json={'mapping':fields,'confirm':True})
            features[name] = {f['id']:f for f in call('get',base+f"/datasets/{dataset['id']}/features")}
        elapsed['ingestion_mapping'] = time.perf_counter()-start
        stage = time.perf_counter()
        result = call('post',base+'/match',json={'left_dataset_id':datasets['reference']['id'],'right_dataset_id':datasets['alternate']['id']})
        elapsed['matching'] = time.perf_counter()-stage
        proposals = result['proposals']
        expected_pairs = truth['identity_pairs']
        eligible = sum(bool(ids) for ids in expected_pairs.values())
        top_correct = top_count = candidate_found = expedited = expedited_correct = abstained = 0
        for p in proposals:
            left = features['reference'][p['left_feature_id']]['original_id']
            candidates = ([p['right_feature_id']] if p.get('right_feature_id') else []) + [a['feature_id'] for a in p['evidence'].get('alternatives', [])]
            candidate_found += bool(set(features['alternate'][fid]['original_id'] for fid in candidates).intersection(expected_pairs[left]))
            if p.get('right_feature_id'):
                top_count += 1
                correct = features['alternate'][p['right_feature_id']]['original_id'] in expected_pairs[left]
                top_correct += correct
                if p['status'] == 'proposed' and p['score'] >= .98:
                    expedited += 1; expedited_correct += correct
            abstained += p['status'] in {'ambiguous','unmatched'}
        stage = time.perf_counter()
        call('post',base+f"/topology/{datasets['reference']['id']}")
        conflicts = call('get',base+'/conflicts')
        detected_conflicts = set()
        conflict_cases = set()
        for conflict in conflicts:
            kind = conflict['type']
            source_id = features['reference'].get(conflict.get('feature_id'), {}).get('original_id')
            other_id = features['reference'].get(conflict['details'].get('other_feature_id'), {}).get('original_id')
            if source_id:
                conflict_cases.add((kind, tuple(sorted([source_id, other_id] if other_id else [source_id]))))
            if kind == 'invalid_geometry':
                detected_conflicts.add(kind)
            if kind in {'overlap','duplicate_geometry'}:
                detected_conflicts.add(kind)
        elapsed['topology'] = time.perf_counter()-stage
        expected_conflicts = {('invalid_geometry', (fid,)) for fid in truth['invalid_ids']}
        expected_conflicts |= {('duplicate_geometry', tuple(sorted(pair))) for pair in truth['duplicate_pairs']}
        expected_conflicts |= {('overlap', tuple(sorted(pair))) for pair in truth['overlap_pairs']}
        stage = time.perf_counter()
        revision_matches = call('post',base+'/match',json={'left_dataset_id':datasets['revision_before']['id'],
                                'right_dataset_id':datasets['revision_after']['id']})
        # Simulated reviewer decisions are evaluation setup, not measured human
        # effort or an auto-approval path in the application.
        for proposal in revision_matches['proposals']:
            if proposal['status'] == 'proposed':
                call('post',base+f"/reviews/match/{proposal['id']}",json={'decision':'accepted',
                     'rationale':'Simulated review for generated dated snapshots','expected_revision':1})
        elapsed['snapshot_identity_review_simulation'] = time.perf_counter()-stage
        stage = time.perf_counter()
        changes_result = call('post',base+'/changes/detect',json={'before_dataset_id':datasets['revision_before']['id'],
                                'after_dataset_id':datasets['revision_after']['id'],'geometry_tolerance':.5})
        changes = call('get',base+'/changes')
        measured_geometry_ids = {features['revision_after'][c['source_feature_id']]['original_id'] for c in changes
                                 if c['change_type'] == 'geometry_changed' and c['source_feature_id'] in features['revision_after']}
        expected_geometry_ids = {fid for fid, kind in truth['changes'].items() if kind == 'geometry_changed'}
        tp = len(measured_geometry_ids & expected_geometry_ids)
        change_precision = tp / len(measured_geometry_ids) if measured_geometry_ids else None
        change_recall = tp / len(expected_geometry_ids) if expected_geometry_ids else None
        elapsed['change_detection'] = time.perf_counter()-stage
        # Select a small, independently reviewed clean publication subset.
        stage = time.perf_counter()
        for p in [p for p in proposals if p['status']=='proposed' and p['score'] == 1.0][:3]:
            call('post',base+f"/reviews/match/{p['id']}",json={'decision':'accepted','rationale':'Synthetic evaluation subset identity check','expected_revision':1})
            call('post',base+f"/features/{p['left_feature_id']}/baseline",json={'geometry_source_id':p['left_feature_id'],
                      'attribute_source_id':p['left_feature_id'],'rationale':'Explicit reference selection'})
        report = call('post',base+'/validate')
        if not report['valid']:
            raise ValueError(f'Evaluation publication failed validation: {report["failures"]}')
        published = call('post',base+'/publish')
        exported = call('get',base+f"/versions/{published['id']}/export")['features']
        lineage_complete = sum(bool(f['properties']['_lineage']['sources'] and f['properties']['_lineage']['review_decisions'] and
                              all(s['schema_mapping_id'] and s['crs_transformation'] and s['sha256'] for s in f['properties']['_lineage']['sources'])) for f in exported)
        elapsed['review_validation_publication_subset'] = time.perf_counter()-stage
    return {'generator_version':'pack-v2','measured_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),
            'hardware':{'platform':platform.platform(),'processor':platform.processor(),'logical_cpus':os.cpu_count()},
            'dependencies':{p:version(p) for p in ['fastapi','sqlalchemy','shapely','pyproj','fiona']},'python':platform.python_version(),
            'input_checksums':truth['checksums'],'feature_counts':truth['counts'],
            'vertex_counts':{filename: sum(vertex_count(feature['geometry'].get('coordinates', [])) for feature in json.loads((output/'inputs'/filename).read_text())['features']) for filename in truth['counts']},
            'stage_seconds':{k:round(v,4) for k,v in elapsed.items()},'total_seconds':round(time.perf_counter()-start,4),
            'candidate_recall':candidate_found/eligible if eligible else None,
            'top_match_precision':top_correct/top_count if top_count else None,'top_match_recall':top_correct/eligible if eligible else None,
            'expedited_rule_threshold':.98,'expedited_precision':expedited_correct/expedited if expedited else None,
            'expedited_coverage':expedited/count,'abstention_fraction':abstained/count,
            'human_review_required_fraction':1.0,'conflict_types_detected':sorted(detected_conflicts),
            'conflict_recall':len(conflict_cases & expected_conflicts)/len(expected_conflicts) if expected_conflicts else None,
            'conflict_precision':len(conflict_cases & expected_conflicts)/len(conflict_cases) if conflict_cases else None,
            'change_counts':changes_result,'geometry_change_precision':change_precision,'geometry_change_recall':change_recall,
            'geometry_change_f1':2*change_precision*change_recall/(change_precision+change_recall) if change_precision and change_recall else 0,
            'published_subset_features':len(exported),'publication_lineage_completeness':lineage_complete/len(exported),
            'limitations':['Synthetic SQLite benchmark, not cadastral accuracy','Expedited threshold is uncalibrated; every publication still requires officer selection',
                           'Conflict precision/recall cover only the three labeled conflict cases','Geometry-change metrics cover one positive and one below-tolerance case; exclude split/merge and identifier changes',
                           'No manual-effort or savings measurement','Publication completeness measured on three clean reviewed features only']}


def vertex_count(coordinates):
    if coordinates and isinstance(coordinates[0], (int, float)):
        return 1
    return sum(vertex_count(value) for value in coordinates)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True); parser.add_argument('--count',type=int,default=1000)
    args=parser.parse_args(); report=run(args.output,args.count)
    (args.output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report,indent=2))


if __name__=='__main__':
    main()
