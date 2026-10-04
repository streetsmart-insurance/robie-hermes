"""Chat release installer scope, exercised only against temporary paths."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_chat_workflows_explicitly_preserve_policy_setup():
    for name in ('deploy-test.yml', 'deploy-production.yml'):
        text = (ROOT / '.github/workflows' / name).read_text()
        commands = [line for line in text.splitlines() if '--command=' in line and 'deploy-' in line]
        assert len(commands) == 1
        assert '--skip-policy-setup' in commands[0]


def test_chat_test_install_does_not_require_or_install_policy_skill(tmp_path):
    text = (ROOT / 'scripts/deploy-test-release.sh').read_text()
    block = text.split('policy_skill_source=""', 1)[1].split('install_gateway_runtime_config()', 1)[0]
    script = '''set -euo pipefail
install_policy_setup=false
release_root="$1/missing-release"
policy_skill_link="$1/existing-skill"
rollback_test() { exit 91; }
atomic_pointer() { exit 92; }
policy_skill_source=""
''' + block
    existing = tmp_path / 'existing-skill'
    existing.write_text('unchanged skill directory surrogate')
    subprocess.run(['bash', '-c', script, 'test', str(tmp_path)], check=True)
    assert existing.read_text() == 'unchanged skill directory surrogate'
    assert not (tmp_path / 'missing-release').exists()


def test_chat_production_install_skips_policy_module(tmp_path):
    text = (ROOT / 'scripts/deploy-production-release.sh').read_text()
    block = text.split('# This separately reviewed skill install', 1)[1].split('if ! systemctl restart', 1)[0]
    block = '# This separately reviewed skill install' + block
    script = '''set -euo pipefail
install_policy_setup=false
release_root="$1/missing-release"
OPT_ROOT="$1"
policy_skill_attempt=synthetic
python3() { exit 93; }
rollback_release() { exit 94; }
''' + block
    subprocess.run(['bash', '-c', script, 'test', str(tmp_path)], check=True)
    assert list(tmp_path.iterdir()) == []


def test_chat_test_rollback_preserves_unrelated_regular_skill(tmp_path):
    old = tmp_path / 'old-release'
    new = tmp_path / 'new-release'
    old.mkdir(); new.mkdir()
    current = tmp_path / 'current'
    releases_current = tmp_path / 'releases-current'
    current.symlink_to(new); releases_current.symlink_to(new)
    skill = tmp_path / 'existing-skill'
    skill.write_text('keep unchanged')
    script = '''set -euo pipefail
source "$1"
systemctl() { printf '%s\\n' "$*" >> "$2/systemctl.log"; }
rollback_test_release "$2/old-release" "$2/old-release" "" "$2/current" "$2/releases-current" "$2/existing-skill" robie-gateway false
'''
    # A shell stub avoids any actual service management.
    script = script.replace('systemctl() { printf', 'root="$2"\nsystemctl() { printf').replace('>> "$2/systemctl.log"', '>> "$root/systemctl.log"')
    subprocess.run(['bash', '-c', script, 'test', str(ROOT/'scripts/lib/test-release-rollback.sh'), str(tmp_path)], check=True)
    assert current.resolve() == old and releases_current.resolve() == old
    assert skill.read_text() == 'keep unchanged'
    assert (tmp_path/'systemctl.log').read_text().splitlines() == ['restart robie-gateway', 'is-active --quiet robie-gateway']


def test_chat_production_rollback_skips_policy_restore(tmp_path):
    text = (ROOT / 'scripts/deploy-production-release.sh').read_text()
    function = text.split('rollback_release() {', 1)[1].split('\n}\n\nbefore=', 1)[0]
    old = tmp_path/'old'; old.mkdir()
    new = tmp_path/'new'; new.mkdir()
    (tmp_path/'releases').mkdir()
    (tmp_path/'current').symlink_to(new)
    (tmp_path/'releases/current').symlink_to(new)
    skill = tmp_path/'policy-skill'; skill.write_text('preserve')
    script = '''set -euo pipefail
OPT_ROOT="$1"
old_current="$1/old"
old_releases_current="$1/old"
release_root="$1/new"
GATEWAY_UNIT=hermes-gateway
policy_skill_attempt=synthetic
rollback_started=false
install_policy_setup=false
python3() { if [[ "$1" == -m ]]; then exit 95; fi; command python3 "$@"; }
systemctl() { :; }
rollback_release() {''' + function + '\n}\nrollback_release\n'
    subprocess.run(['bash', '-c', script, 'test', str(tmp_path)], check=True)
    assert (tmp_path/'current').resolve() == old
    assert (tmp_path/'releases/current').resolve() == old
    assert skill.read_text() == 'preserve'
