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
