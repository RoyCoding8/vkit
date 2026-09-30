"""Check that both public adapters enforce the registered policy floor."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
for key in tuple(os.environ):
    if key.startswith('GIT_'):
        os.environ.pop(key)
os.environ['PATH'] = str(Path(sys.executable).parent) + os.pathsep + os.environ['PATH']
from vkit.cli import main
from vkit.mcp import Server

target = Path(tempfile.mkdtemp(prefix='vkit-floor-audit-'))
shutil.copytree(ROOT / 'examples/python-cli', target, dirs_exist_ok=True,
                ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
manifest_path = target / 'verification/manifest.json'
manifest = json.loads(manifest_path.read_text())
second = dict(manifest['checks'][0], id='mandatory-second')
manifest['checks'].append(second)
manifest_path.write_text(json.dumps(manifest))
contract_path = target / 'contract.json'
contract_path.write_text(json.dumps({'required_checks': ['totals-behavior']}))
for args in (['init', '-q'], ['add', '-A'], ['-c', 'user.email=audit@example.invalid',
             '-c', 'user.name=Audit', 'commit', '-qm', 'audit fixture']):
    subprocess.run(['git', *args], cwd=target, check=True, capture_output=True)

def cli(*args):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = main([*args, '--project', str(target), '--json'])
    return {'exit_code': code, 'payload': json.loads(output.getvalue())}

opened = cli('task', 'begin', '--contract', str(contract_path), '--request-id', 'floor')
task_id = opened['payload']['task_id']
server = Server(target)
run = server.call_tool('check_start', {'task_id': task_id, 'check_ids': ['totals-behavior'],
                                      'request_id': 'floor-run'}).content
cli_verdict = cli('task', 'finalize', '--task', task_id)
mcp_verdict = server.call_tool('task_finalize', {'task_id': task_id}).content
manifest_path.unlink()
after_removal = server.call_tool('task_finalize', {'task_id': task_id}).content
result = {'scratch': str(target), 'registered_checks': [c['id'] for c in manifest['checks']],
          'run': run, 'cli_finalize': cli_verdict, 'mcp_finalize': mcp_verdict,
          'mcp_after_manifest_removal': after_removal}
(ROOT / 'review/baseline-results.json').write_text(json.dumps(result, indent=2))
print(json.dumps({key: value for key, value in result.items() if key != 'run'}, indent=2))
