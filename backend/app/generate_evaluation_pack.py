"""Deterministic synthetic inputs; truth is emitted separately from application data."""
import argparse
from copy import deepcopy
import csv
import hashlib
import json
from pathlib import Path


def polygon(x, y, width=.004, height=.003):
    return {"type": "Polygon", "coordinates": [[[x,y],[x+width,y],[x+width,y+height],[x,y+height],[x,y]]]}


def collection(features, crs="EPSG:4326"):
    document = {"type": "FeatureCollection", "features": features}
    if crs:
        document["crs"] = {"type": "name", "properties": {"name": crs}}
    return document


def generate(output: Path, count=1000):
    if count < 1:
        raise ValueError("Count must be positive")
    output.mkdir(parents=True, exist_ok=True)
    reference, alternate, revenue = [], [], []
    expected = {}
    for index in range(count):
        row, col = divmod(index, 40)
        x, y = 73.7 + col * .006, 19.9 + row * .006
        fid, pid, village = f"R-{index+1:05}", f"{index % 250 + 1:05}", f"V-{index // 250 + 1:02}"
        geometry = polygon(x,y)
        if index == 49:  # valid interior hole
            geometry['coordinates'].append(polygon(x+.001,y+.001,.001,.001)['coordinates'][0])
        ref = {"type": "Feature", "id": fid, "properties": {"parcel_id": pid, "village_code": village,
               "recorded_area": '120000', "area_units": 'm2', "synthetic": True}, "geometry": geometry}
        alt = deepcopy(ref)
        alt['id'] = fid.replace('R-', 'A-')
        alt['properties'] = {"khasra_no": pid, "admin_code": village, "area_sq_m": '120000', "units": 'm2', "synthetic": True}
        reference.append(ref)
        if index != 101:
            alternate.append(alt)
            expected[fid] = [alt['id']]
        else:
            expected[fid] = []
        revenue.append({'Khasra_No': pid, 'Village_Code': village, 'Recorded_Area': '120000', 'Area_Units': 'm2'})
    # Same number in incompatible villages, at the same location: no match.
    if count > 700:
        alternate[699]['properties']['admin_code'] = 'V-OTHER'
        expected[alternate[699]['id'].replace('A-', 'R-')] = []
    if count > 150:
        ambiguous = deepcopy(next(f for f in alternate if f['id'] == 'A-00150'))
        ambiguous['id'] = 'A-00150-alternative'
        alternate.append(ambiguous)
        expected['R-00150'] += [ambiguous['id']]
    invalid_ids, duplicate_pairs, overlap_pairs = [], [], []
    if count > 304:
        x,y = reference[299]['geometry']['coordinates'][0][0]
        reference[299]['geometry'] = {'type':'Polygon','coordinates':[[[x,y],[x+.004,y+.003],[x,y+.003],[x+.004,y],[x,y]]]}
        invalid_ids.append('R-00300')
        reference[300]['geometry'] = deepcopy(reference[301]['geometry'])
        duplicate_pairs.append(['R-00301','R-00302'])
        x,y = reference[302]['geometry']['coordinates'][0][0]
        reference[303]['geometry'] = polygon(x+.001,y+.001,.001,.001)  # containment
        overlap_pairs.append(['R-00303','R-00304'])
    before = deepcopy(reference)
    after = deepcopy(reference)
    expected_changes = {}
    if count > 501:
        x,y = after[499]['geometry']['coordinates'][0][0]
        after[499]['geometry'] = polygon(x+.0000005,y)  # <0.5 metre
        x,y = after[500]['geometry']['coordinates'][0][0]
        after[500]['geometry'] = polygon(x+.00009,y)  # about 9 metres
        expected_changes['R-00501'] = 'geometry_changed'
        after[500]['properties']['recorded_area'] = '121000'
    if count > 600:
        x,y = after[599]['geometry']['coordinates'][0][0]
        after[599]['geometry'] = polygon(x,y,.002,.003)
        split = deepcopy(after[599]); split['id'] += '-split'
        split['geometry'] = polygon(x+.002,y,.002,.003)
        after.append(split)
        expected_changes['R-00600'] = 'ambiguous_identity'
    if count > 88:
        after[87]['properties']['parcel_id'] += '-B'
        expected_changes['R-00088'] = 'identifier_changed'
    sample = deepcopy(reference[0])
    wrong = deepcopy(sample); wrong['geometry'] = polygon(1000,2000,10,10)
    files = {'reference.geojson': collection(reference), 'alternate.geojson': collection(alternate),
             'revision_before.geojson': collection(before), 'revision_after.geojson': collection(after),
             'unknown_crs.geojson': collection([sample], None), 'wrong_crs.geojson': collection([wrong])}
    checksums, counts = {}, {}
    for name, document in files.items():
        payload = json.dumps(document, sort_keys=True, separators=(',', ':')).encode()
        (output/name).write_bytes(payload)
        checksums[name] = hashlib.sha256(payload).hexdigest()
        counts[name] = len(document['features'])
    with (output/'revenue_attribute_only.csv').open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(revenue[0])); writer.writeheader(); writer.writerows(revenue)
    checksums['revenue_attribute_only.csv'] = hashlib.sha256((output/'revenue_attribute_only.csv').read_bytes()).hexdigest()
    truth = {'synthetic': True, 'generator_version': 'pack-v2', 'identity_pairs': expected, 'changes': expected_changes,
             'invalid_ids': invalid_ids, 'duplicate_pairs': duplicate_pairs, 'overlap_pairs': overlap_pairs,
             'checksums': checksums, 'counts': counts}
    (output/'answer_key.json').write_text(json.dumps(truth, indent=2), encoding='utf-8')
    return truth


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--count', type=int, default=1000)
    args = parser.parse_args()
    print(json.dumps(generate(args.output, args.count)['checksums'], indent=2))


if __name__ == '__main__':
    main()
