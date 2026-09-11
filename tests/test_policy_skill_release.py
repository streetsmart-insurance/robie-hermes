import shutil
from pathlib import Path
import pytest
from robie_job_engine.policy_skill_release import install, restore


@pytest.mark.parametrize('prior', ['absent', 'directory', 'link'])
def test_install_and_rollback_preserve_exact_skill(tmp_path, prior):
    root, release = tmp_path / 'root', tmp_path / 'release'
    source = release / 'deploy/hermes/skills/ezlynx-policy-setup'
    shutil.copytree(Path('deploy/hermes/skills/ezlynx-policy-setup'), source)
    link = root / '.hermes/skills/ezlynx-policy-setup'
    link.parent.mkdir(parents=True)
    if prior == 'directory':
        link.mkdir()
        (link / 'SKILL.md').write_text('original owned text')
    if prior == 'link':
        old = tmp_path / 'old'
        old.mkdir()
        (old / 'SKILL.md').write_text('previous release')
        link.symlink_to(old)
    record = install(root, release)
    assert link.resolve() == source
    assert record['sha256']['SKILL.md']
    restore(root, release)
    if prior == 'absent':
        assert not link.exists()
    elif prior == 'directory':
        assert not link.is_symlink()
        assert (link / 'SKILL.md').read_text() == 'original owned text'
    else:
        assert link.resolve() == old
        assert (link / 'SKILL.md').read_text() == 'previous release'


def test_repeated_install_does_not_undo_prior_success_on_rollback(tmp_path):
    root, release = tmp_path / 'root', tmp_path / 'release'
    source = release / 'deploy/hermes/skills/ezlynx-policy-setup'
    shutil.copytree(Path('deploy/hermes/skills/ezlynx-policy-setup'), source)
    install(root, release, 'first')
    assert install(root, release, 'second')['already_installed'] is True
    restore(root, release, 'second')
    assert (root / '.hermes/skills/ezlynx-policy-setup').resolve() == source


def test_failure_after_backup_restores_original_directory(tmp_path, monkeypatch):
    root, release = tmp_path / 'root', tmp_path / 'release'
    source = release / 'deploy/hermes/skills/ezlynx-policy-setup'
    shutil.copytree(Path('deploy/hermes/skills/ezlynx-policy-setup'), source)
    link = root / '.hermes/skills/ezlynx-policy-setup'
    link.mkdir(parents=True)
    (link / 'SKILL.md').write_text('original')
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'symlink_to', lambda *a, **kw: (_ for _ in ()).throw(OSError('simulated failure')))
        with pytest.raises(OSError):
            install(root, release, 'failed')
    restore(root, release, 'failed')
    assert (link / 'SKILL.md').read_text() == 'original'


def test_rollback_restarts_gateway_even_when_skill_restore_fails(tmp_path):
    import subprocess
    installer = Path('scripts/deploy-production-release.sh').read_text()
    function = installer.split('rollback_release() {', 1)[1].split('\n}\n\nbefore=', 1)[0]
    script = '''set -e
rollback_started=false
old_current=old
old_releases_current=old
OPT_ROOT=unused
release_root=unused
policy_skill_attempt=attempt
GATEWAY_UNIT=hermes-gateway
python3() { if [ "$1" = -m ]; then return 1; else cat >/dev/null; fi; }
systemctl() { echo "$*" >> "$CHECK_LOG"; }
rollback_release() {''' + function + '\n}\nrollback_release\n'
    import os
    log = tmp_path / 'calls'
    result = subprocess.run(['bash', '-c', script], env={**os.environ, 'CHECK_LOG': str(log)}, capture_output=True, text=True)
    assert result.returncode == 1
    assert log.read_text().splitlines() == ['restart hermes-gateway', 'is-active --quiet hermes-gateway']


def test_rolled_back_candidate_can_be_installed_again(tmp_path):
    root, release = tmp_path / 'root', tmp_path / 'release'
    source = release / 'deploy/hermes/skills/ezlynx-policy-setup'
    shutil.copytree(Path('deploy/hermes/skills/ezlynx-policy-setup'), source)
    install(root, release, 'first')
    restore(root, release, 'first')
    assert len(list(release.glob('policy-skill-rollback-*.json'))) == 1
    assert install(root, release, 'retry')['attempt'] == 'retry'
    assert (root / '.hermes/skills/ezlynx-policy-setup').resolve() == source
