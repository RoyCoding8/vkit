"""Probe policy fingerprint semantics without launching commands."""
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
for key in tuple(os.environ):
    if key.startswith('GIT_'):
        os.environ.pop(key)
from vkit.manifest import parse_manifest, parse_manifest_bytes
from vkit.paths import open_project
from vkit.storage import Store
from vkit.integration.verify import _persist

scratch = Path(json.loads((ROOT / 'review/probe-results.json').read_text())['scratch'])
project = open_project(scratch / 'stale-source')
manifest = parse_manifest(project, scratch / 'audit-digest')
check = next(iter(manifest.checks.values()))

def digest(**fields):
    changed = dataclasses.replace(check, **fields)
    return dataclasses.replace(manifest, checks={changed.id: changed}).digest()

wheel = next((ROOT / 'review/dist').glob('*.whl'))
with zipfile.ZipFile(wheel) as z:
    schema_matches = {Path(name).name: hashlib.sha256(z.read(name)).hexdigest() ==
                      hashlib.sha256((ROOT / 'schemas' / Path(name).name).read_bytes()).hexdigest()
                      for name in z.namelist() if name.startswith('vkit/_schemas/')}
formal_matches = {}
for source, receipt, key in (
    ('formal/tla/OwnershipAcceptance.tla', 'formal/results/OwnershipAcceptance-receipt.json', 'model_sha256'),
    ('formal/tla/OwnershipAcceptance.cfg', 'formal/results/OwnershipAcceptance-receipt.json', 'config_sha256'),
    ('formal/lean/Acceptance.lean', 'formal/results/Acceptance-lean-receipt.json', 'module_sha256'),
):
    blob = (ROOT / source).read_bytes()
    saved = json.loads((ROOT / receipt).read_text())[key]
    formal_matches[source] = {
        'exact_bytes_match': hashlib.sha256(blob).hexdigest() == saved,
        'normalized_lf_matches': hashlib.sha256(blob.replace(b'\r\n', b'\n')).hexdigest() == saved,
    }
result = {
    'argv_boundary_collision': digest(argv=('python', 'a b', 'c')) ==
                               digest(argv=('python', 'a', 'b c')),
    'inputs_ignored': digest(inputs=('fixtures/one.json',)) ==
                      digest(inputs=('fixtures/two.json',)),
    'prerequisites_ignored': digest(prerequisites=()) == digest(prerequisites=check.prerequisites),
    'checkout_path_changes_policy_digest': digest(cwd=Path('C:/checkout-one')) !=
                                          digest(cwd=Path('C:/checkout-two')),
    'wheel_schemas_match_authoritative_source': schema_matches,
    'formal_receipt_identity': formal_matches,
}
raw = json.loads(project.manifest_path.read_text())
raw['checks'].append(raw['checks'][0])
try:
    parse_manifest_bytes(json.dumps(raw).encode(), project=project,
                         run_dir=scratch / 'duplicate', origin='audit duplicate')
except Exception as exc:
    result['duplicate_check_rejection'] = {'type': type(exc).__name__, 'message': str(exc)}
store = Store(scratch / 'receipt-probe.sqlite3')
acceptance_id = 'acc-' + uuid.uuid4().hex
old_record = {
    'integration_id': 'audit', 'context': 'local', 'decision': 'ACCEPTED',
    'candidate': 'c' * 40, 'target': 'a' * 40, 'candidate_parent': 'a' * 40,
    'source': {}, 'policy': {}, 'verifier': {}, 'environment': {}, 'fixture': {},
    'manifest': {}, 'required_checks': ['registered'], 'checks': [], 'gaps': [],
    'findings': [], 'decided_at': 'audit-first',
}
store.record_acceptance(acceptance_id, old_record)
new_record = dict(old_record, decision='REJECTED', decided_at='audit-second')
_persist(store, acceptance_id, new_record)
result['duplicate_acceptance_disagreement'] = {
    'returned_record_decision': new_record['decision'],
    'stored_record_decision': store.load_acceptance(acceptance_id)['decision'],
    'claims_reused_recorded_acceptance': new_record.get('reused_recorded_acceptance'),
}
(ROOT / 'review/identity-results.json').write_text(json.dumps(result, indent=2))
print(json.dumps(result, indent=2))
