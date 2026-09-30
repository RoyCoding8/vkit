"""Probe policy fingerprint semantics and duplicate-receipt settlement."""
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
from vkit.integration.verify import _settle

scratch = Path(json.loads((ROOT / 'review/probe-results.json').read_text())['scratch'])
project = open_project(scratch / 'stale-source')
manifest = parse_manifest(project, scratch / 'audit-digest')
check = next(iter(manifest.checks.values()))

def digest(**fields):
    changed = dataclasses.replace(check, **fields)
    return dataclasses.replace(manifest, checks={changed.id: changed}).digest()

wheel = next((ROOT / 'review/dist').glob('*.whl'), None)
schema_matches = None
if wheel is not None:
    with zipfile.ZipFile(wheel) as z:
        schema_matches = {
            Path(name).name: hashlib.sha256(z.read(name)).hexdigest() ==
                            hashlib.sha256((ROOT / 'schemas' / Path(name).name).read_bytes()).hexdigest()
            for name in z.namelist() if name.startswith('vkit/_schemas/')
        }
else:
    # `review/dist` is deliberately untracked: a wheel is rebuildable and
    # `wheel-inspection.json` is its receipt. So the schema comparison is
    # reported as not run rather than crashing the probe before the receipt
    # checks below, which need nothing but the scratch repository.
    schema_matches = 'not run: review/dist holds no wheel; see review/wheel-inspection.json'
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

# A duplicate receipt id must come back as one decision, never two. Two cases:
# an observation that AGREES with the record, and one that CONTRADICTS it.
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

agreeing = _settle(store, acceptance_id, dict(old_record, decision='ACCEPTED'))
result['duplicate_acceptance_agreement'] = {
    'acceptance_id_is_the_recorded_one': agreeing.acceptance_id == acceptance_id,
    'decision': agreeing.decision,
    'decided_at': agreeing.record['decided_at'],
    'returned_the_stored_record': agreeing.record == store.load_acceptance(acceptance_id),
    'rows_in_table': len(store.list_acceptances()),
}

contradicting = _settle(store, acceptance_id, dict(
    old_record, decision='REJECTED', decided_at='audit-second',
    integration_id='audit-again',
    gaps=['the required checks were not run: the trusted verification code and the '
          'approved policy do not both hold for this candidate'],
))
result['duplicate_acceptance_disagreement'] = {
    'answers': contradicting.decision,
    'id_differs_from_the_recorded_one': contradicting.acceptance_id != acceptance_id,
    'recorded_decision_untouched': store.load_acceptance(acceptance_id)['decision'],
    'contradiction_recorded': store.load_acceptance(contradicting.acceptance_id)['decision'],
    'both_rows_retained': len(store.list_acceptances()),
    'contradiction_names_both': contradicting.record['gaps'][0].startswith('CONFLICT:'),
}
(ROOT / 'review/identity-results.json').write_text(json.dumps(result, indent=2))
print(json.dumps(result, indent=2))
