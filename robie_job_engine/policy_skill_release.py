"""Install only the reviewed policy drill skill, preserving its exact predecessor."""
import hashlib
import json
import os
import uuid
from pathlib import Path

FILES = ('SKILL.md', 'references/profiles.json', 'references/selector-inventory.md')


def install(root: Path, release: Path, attempt: str = ""):
    source = release / 'deploy/hermes/skills/ezlynx-policy-setup'
    for name in FILES:
        if not (source / name).is_file():
            raise ValueError('Incomplete policy skill package')
    from .ezlynx_policy_setup_profiles import load_profiles
    load_profiles(source / 'references/profiles.json')
    link = root / '.hermes/skills/ezlynx-policy-setup'
    receipt = release / 'policy-skill-install.json'
    if receipt.exists():
        record = json.loads(receipt.read_text())
        if record['source'] != str(source) or not link.is_symlink() or link.resolve() != source.resolve():
            raise ValueError('Previous skill installation is not intact')
        for name in FILES:
            if hashlib.sha256((link / name).read_bytes()).hexdigest() != record['sha256'][name]:
                raise ValueError('Previous skill installation bytes changed')
        return {**record, 'already_installed': True}
    previous = {'kind': 'absent'}
    if link.is_symlink():
        previous = {'kind': 'link', 'target': os.readlink(link)}
    elif link.exists():
        if not link.is_dir():
            raise ValueError('Existing policy skill is not a directory or link')
        backup = root / '.hermes/skill-backups' / uuid.uuid4().hex / 'ezlynx-policy-setup'
        previous = {'kind': 'directory', 'backup': str(backup)}
    record = {'source': str(source), 'previous': previous, 'attempt': attempt,
              'sha256': {name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in FILES}}
    receipt.write_text(json.dumps(record, indent=2))
    link.parent.mkdir(parents=True, exist_ok=True)
    if previous['kind'] == 'directory':
        backup.parent.mkdir(parents=True, exist_ok=True)
        link.rename(backup)
    pending = link.with_name(link.name + '.release-new')
    if pending.is_symlink():
        pending.unlink()
    pending.symlink_to(source)
    pending.replace(link)
    for name, expected in record['sha256'].items():
        if hashlib.sha256((link / name).read_bytes()).hexdigest() != expected:
            raise ValueError('Installed policy skill differs from candidate')
    return record


def restore(root: Path, release: Path, attempt: str = ""):
    receipt = release / 'policy-skill-install.json'
    if not receipt.exists():
        return
    record = json.loads(receipt.read_text())
    if record.get('attempt', '') != attempt:
        return  # This deployment invocation did not change the skill.
    link = root / '.hermes/skills/ezlynx-policy-setup'
    previous = record['previous']
    if link.is_symlink() and os.readlink(link) == record['source']:
        link.unlink()
    elif link.exists() or link.is_symlink():
        # A failed install may not have replaced the original yet.
        if previous['kind'] == 'link' and link.is_symlink() and os.readlink(link) == previous['target']:
            return
        if previous['kind'] == 'directory' and not Path(previous['backup']).exists():
            return
        raise ValueError('Policy skill changed after installation; refuse overwrite')
    if previous['kind'] == 'link':
        link.symlink_to(previous['target'])
    elif previous['kind'] == 'directory':
        Path(previous['backup']).rename(link)


if __name__ == '__main__':
    import sys
    action, root, release, attempt = sys.argv[1:]
    if action not in {'install', 'restore'}:
        raise SystemExit('Unknown policy skill release action')
    result = (install if action == 'install' else restore)(Path(root), Path(release), attempt)
    print(json.dumps(result or {'restored': True}))
