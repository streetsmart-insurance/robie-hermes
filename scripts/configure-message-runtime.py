#!/usr/bin/env python3
"""Configure message services with reference-only environment; rollback on failure."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import socket
import stat
import sys
import sqlite3
import subprocess

REF = re.compile(r'projects/[\w-]+/secrets/[\w-]+/versions/[1-9]\d*')

def settings(environment, login_refs, api_reference=None):
    if environment not in ('TEST', 'PRODUCTION'):
        raise ValueError('Explicit TEST or PRODUCTION environment required')
    suffix = '-test' if environment == 'TEST' else ''
    root = '/opt/streetsmart-hermes' + suffix
    expected = {'ROBIE_EZLYNX_USERNAME_SECRET', 'ROBIE_EZLYNX_PASSWORD_SECRET'}
    if set(login_refs) != expected or any(not REF.fullmatch(v) for v in login_refs.values()):
        raise ValueError('Both pinned login secret references are required')
    api = 'UAT' if environment == 'TEST' else 'PROD'
    api_reference = api_reference or f'projects/751771086524/secrets/ezlynx-api-{api.lower()}/versions/latest'
    if not re.fullmatch(r'projects/[\w-]+/secrets/ezlynx-api-'+api.lower()+r'/versions/(?:latest|[1-9]\d*)',api_reference):
        raise ValueError('Invalid or cross-environment API reference')
    return dict(ROBIE_ENV=environment, ROBIE_OPT_ROOT=root,
                HERMES_HOME=root+'/.hermes', ROBIE_JOB_DB=root+'/robie-job-engine/data/jobs.db',
                ROBIE_CANONICAL_JOB_ENGINE_ROOT=root+'/releases/current',
                **{f'ROBIE_EZLYNX_API_{api}_SECRET': api_reference},
                **login_refs)

def apply_files(files, backup_dir, activate):
    """Snapshot before any edit; restore every prior file if activation fails."""
    backup_dir.mkdir(parents=True, exist_ok=False)
    backup_dir.chmod(0o700)
    previous = {}
    metadata = {}
    for i, path in enumerate(files):
        previous[path] = path.read_bytes() if path.exists() else None
        if path.exists():
            info=path.stat(); metadata[path]=(stat.S_IMODE(info.st_mode),info.st_uid,info.st_gid)
        if previous[path] is not None:
            snapshot = backup_dir / str(i)
            snapshot.write_bytes(previous[path]); snapshot.chmod(0o600)
    (backup_dir/'manifest.json').write_text(json.dumps({str(p): {'snapshot': i if b is not None else None, 'metadata': metadata.get(p)} for i,(p,b) in enumerate(previous.items())}))
    try:
        for path, text in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_name(path.name+'.new')
            temp.write_text(text); temp.chmod(0o600); temp.replace(path)
        activate()
    except BaseException:
        for path, value in previous.items():
            if value is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(value)
                mode,uid,gid=metadata[path]
                path.chmod(mode); os.chown(path,uid,gid)
        raise

def run(*args):
    return subprocess.check_output(args, text=True, timeout=30).strip()

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--environment', required=True, choices=['TEST','PRODUCTION'])
    parser.add_argument('--backup-dir', required=True)
    parser.add_argument('--release-sha256', required=True)
    args=parser.parse_args()
    host='hermes-test-01' if args.environment=='TEST' else 'hermes-poc-01'
    if os.geteuid()!=0 or socket.gethostname().split('.')[0]!=host:
        raise SystemExit('Wrong host or not root')
    root=Path('/opt/streetsmart-hermes'+('-test' if args.environment=='TEST' else ''))
    release=(root/'releases/current').resolve()
    if not re.fullmatch(r'[0-9a-f]{64}',args.release_sha256) or (release/'.release-sha256').read_text().strip()!=args.release_sha256:
        raise SystemExit('Running release does not match the approved digest')
    db=root/'robie-job-engine/data/jobs.db'
    with sqlite3.connect(f'file:{db}?mode=ro',uri=True) as con:
        count=con.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('RUNNING','VERIFYING') OR lease_owner IS NOT NULL").fetchone()[0]
        if count:
            raise SystemExit('Active jobs/leases exist; refuse service restart')
    gateway='robie-gateway' if args.environment=='TEST' else 'hermes-gateway'
    python=root/'.hermes/hermes-agent/venv/bin/python'
    resolver=root/'releases/current/scripts/read-login-secret-versions.py'
    raw=run('env', 'PYTHONPATH='+str(root/'releases/current'), str(python), str(resolver))
    refs=dict(line.split('=',1) for line in raw.splitlines() if '=' in line)
    from milestone_preflight import _unit_environment
    prior_env, _ = _unit_environment(gateway)
    api_key='ROBIE_EZLYNX_API_'+('UAT' if args.environment=='TEST' else 'PROD')+'_SECRET'
    values=settings(args.environment, refs, prior_env.get(api_key))
    env_path=Path('/etc')/root.name/'robie-message-runtime.env'
    files={env_path: ''.join(f'{k}={v}\n' for k,v in values.items())}
    units=[gateway, 'hermes-email-watcher', 'robie-scheduler']
    loaded=[]
    for unit in units:
        if run('systemctl','show',unit,'-p','LoadState','--value')=='loaded':
            loaded.append(unit)
            files[Path('/etc/systemd/system')/(unit+'.service.d')/'zz-robie-message-runtime.conf']='[Service]\nEnvironmentFile='+str(env_path)+'\n'
    if gateway not in loaded:
        raise SystemExit('Gateway unit is missing')
    def activate():
        subprocess.run(['systemctl','daemon-reload'],check=True, timeout=30)
        subprocess.run(['systemctl','restart',gateway],check=True, timeout=30)
        subprocess.run(['systemctl','is-active','--quiet',gateway],check=True, timeout=30)
        pid=run('systemctl','show',gateway,'-p','MainPID','--value')
        effective=dict(item.split('=',1) for item in Path('/proc',pid,'environ').read_text().split('\0') if '=' in item)
        if any(effective.get(k)!=v for k,v in values.items()):
            raise RuntimeError('Gateway did not load the expected message environment')
    sys.path.insert(0,str(release))
    from robie_job_engine.runs import IsolatedRunStore
    runs=IsolatedRunStore(db)
    reservation=runs.start(owner='message-runtime-configuration',job_id='message-runtime-configuration',lease_seconds=1800)
    paused=[]
    try:
        email='hermes-email-watcher'
        if email in loaded:
            triggers=run('systemctl','show',email,'-p','TriggeredBy','--value').split()
            for timer in triggers:
                if timer.endswith('.timer') and run('systemctl','show',timer,'-p','ActiveState','--value')=='active':
                    subprocess.run(['systemctl','stop',timer],check=True, timeout=30); paused.append(timer)
            if run('systemctl','show',email,'-p','ActiveState','--value') not in ('inactive','failed'):
                raise RuntimeError('Email execution is active; refuse configuration until idle')
        with sqlite3.connect(f'file:{db}?mode=ro',uri=True) as con:
            if con.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('RUNNING','VERIFYING') OR lease_owner IS NOT NULL").fetchone()[0]:
                raise RuntimeError('Work started before maintenance reservation; refuse restart')
        try:
            apply_files(files,Path(args.backup_dir),activate)
        except BaseException:
            subprocess.run(['systemctl','daemon-reload'],check=True, timeout=30)
            subprocess.run(['systemctl','restart',gateway],check=True, timeout=30)
            subprocess.run(['systemctl','is-active','--quiet',gateway],check=True, timeout=30)
            raise
    finally:
        cleanup_errors=[]
        try:
            runs.terminate(reservation['id'],'COMPLETE')
        except Exception as exc:
            cleanup_errors.append(f"reservation cleanup failed: {type(exc).__name__}")
        for timer in paused:
            try:
                subprocess.run(['systemctl','start',timer],check=True, timeout=30)
            except Exception as exc:
                cleanup_errors.append(f"timer {timer} recovery failed: {type(exc).__name__}")
        if cleanup_errors:
            raise RuntimeError('; '.join(cleanup_errors))
    print(json.dumps({'environment':args.environment,'configured_units':loaded,'gateway_effective_environment_verified':True,'keys':sorted(values),'backup_dir':args.backup_dir,'email_takes_effect_on_next_service_start':True}))

if __name__=='__main__':
    main()
