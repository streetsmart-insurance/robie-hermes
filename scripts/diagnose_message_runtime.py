#!/usr/bin/env python3
"""Read service runtime metadata without printing credentials or message bodies."""
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from milestone_preflight import _unit_property, _unit_environment, _service_python, _service_pythonpath


def inspect_unit(unit):
    state = _unit_property(unit, 'ActiveState')
    pid = _unit_property(unit, 'MainPID')
    env, sources = _unit_environment(unit)
    if pid.isdigit() and int(pid):
        try:
            raw = Path(f'/proc/{pid}/environ').read_bytes()
            env = dict(item.decode().split('=', 1) for item in raw.split(b'\0') if b'=' in item)
            sources = ['running process environment']
        except OSError:
            pass
    interpreter, _ = _service_python(unit)
    command = _unit_property(unit, 'ExecStart')
    scripts = re.findall(r'(/[\w/.-]+\.py)\b', command)
    files = []
    for name in scripts:
        path = Path(name)
        files.append({'path': name, 'resolved': str(path.resolve()), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None})
    refs = {k: v if re.fullmatch(r'projects/[\w-]+/secrets/[\w-]+/versions/[\w-]+', v) else 'INVALID_REFERENCE' for k,v in env.items() if k in {'ROBIE_EZLYNX_API_PROD_SECRET','ROBIE_EZLYNX_API_UAT_SECRET','ROBIE_EZLYNX_USERNAME_SECRET','ROBIE_EZLYNX_PASSWORD_SECRET'}}
    return {'unit': unit, 'state': state, 'pid': pid, 'user': _unit_property(unit,'User'), 'interpreter': interpreter, 'environment_sources': sources, 'robie_env': env.get('ROBIE_ENV'), 'pythonpath': env.get('PYTHONPATH'), 'job_db': env.get('ROBIE_JOB_DB'), 'secret_references': refs, 'scripts': files}


def main():
    listed = subprocess.run(['systemctl','list-unit-files','--type=service','--no-legend','--no-pager'],capture_output=True,text=True,check=True).stdout
    units = {'hermes-gateway.service','robie-scheduler.service'}
    units.update(line.split()[0] for line in listed.splitlines() if re.search(r'robie|hermes|gmail',line.split()[0],re.I) and re.search(r'email|mail',line.split()[0],re.I))
    print(json.dumps({'release': str(Path('/opt/streetsmart-hermes/releases/current').resolve()), 'services': [inspect_unit(u) for u in sorted(units)]}, indent=2))


if __name__ == '__main__':
    main()
