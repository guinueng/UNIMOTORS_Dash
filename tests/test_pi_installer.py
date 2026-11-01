"""Exercise installer functions and the actual stage/validate/publish block, without sudo/APT."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "scripts/install-pi.sh").read_text(encoding="utf8")
BASH = os.environ.get("BASH_EXE") or shutil.which("bash")
if os.name == "nt" and not os.environ.get("BASH_EXE"):
    candidate = Path("C:/Program Files/Git/bin/bash.exe")
    BASH = str(candidate) if candidate.is_file() else BASH
pytestmark = pytest.mark.skipif(not BASH, reason="Bash is required")
HELPERS = SCRIPT.split("# BEGIN TESTABLE INSTALL HELPERS", 1)[1].split("# END TESTABLE INSTALL HELPERS", 1)[0]
RUN = SCRIPT[SCRIPT.index("run() {"):SCRIPT.index("put_root() {")]
BACKUP = SCRIPT[SCRIPT.index("backup() {"):SCRIPT.index("# BEGIN TESTABLE INSTALL HELPERS")]
STAGE = SCRIPT.split('step "코드 내려받기 → $HOME_DIR"', 1)[1].split('#  3. systemd 서비스', 1)[0]


def posix(path):
    text = Path(path).resolve().as_posix()
    return "/" + text[0].lower() + text[2:] if os.name == "nt" else text


def shell(code, tmp_path, **values):
    env = dict(os.environ, TEST_PYTHON=Path(sys.executable).as_posix(), PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    env.update({k: str(v) for k, v in values.items()})
    prelude = '''set -euo pipefail
DRY="${TEST_DRY:-0}"
ROLE="${TEST_ROLE:-demo}"
TS=fixture
step(){ :; }
ok(){ printf '%s\\n' "$*"; }
die(){ printf 'ERROR %s\\n' "$*" >&2; exit 1; }
python3(){ "$TEST_PYTHON" "$@"; }
sudo(){ "$@"; }
'''
    return subprocess.run([BASH, "-s"], input=prelude + RUN + BACKUP + HELPERS + code,
                          text=True, encoding="utf8", capture_output=True, cwd=tmp_path, env=env, timeout=20)


def test_token_preserves_other_settings_and_multiline_values(tmp_path):
    target = tmp_path / "environment"
    original = '# comment\nUNIMOTORS_PRESET=starex\nUNIMOTORS_TOKEN="old\nmultiline"\nOTHER="line1\nline2"\n'
    target.write_text(original, encoding="utf8")
    token = 'spaces $dollar `ticks` "quotes" \\backslash #hash=한글'
    result = shell('save_upload_token "$TEST_FILE" "$TEST_TOKEN"\n', tmp_path,
                   TEST_FILE=target.as_posix(), TEST_TOKEN=token)
    assert result.returncode == 0, result.stderr
    assert target.read_text(encoding="utf8").startswith(original)
    assert token not in result.stdout + result.stderr
    # This fixture is valid in both EnvironmentFile and Bash, with no command expansion.
    loaded = shell('. "$TEST_FILE"\nprintf "%s" "$UNIMOTORS_TOKEN"\n', tmp_path, TEST_FILE=target.as_posix())
    assert loaded.returncode == 0 and loaded.stdout == token
    if os.name != "nt":
        assert target.stat().st_mode & 0o777 == 0o600


def test_token_rerun_replaces_only_managed_tail(tmp_path):
    target = tmp_path / "environment"
    previous = None
    for token in ("first", "first", "second"):
        result = shell('save_upload_token "$TEST_FILE" "$TEST_TOKEN"\n', tmp_path,
                       TEST_FILE=target.as_posix(), TEST_TOKEN=token)
        assert result.returncode == 0, result.stderr
        content = target.read_bytes()
        if token == "first" and previous is not None:
            assert content == previous
        previous = content
    assert target.read_text().count("# UNIMOTORS installer managed upload token") == 1
    assert 'UNIMOTORS_TOKEN="second"' in target.read_text()
    assert not list(tmp_path.glob(".unimotors-env-*"))


@pytest.mark.parametrize("token", ["", "one\ntwo", "one\rtwo"])
def test_invalid_token_does_not_change_existing_file(tmp_path, token):
    target = tmp_path / "environment"
    target.write_text("KEEP=unchanged\n", encoding="utf8")
    result = shell('save_upload_token "$TEST_FILE" "$TEST_TOKEN"\n', tmp_path,
                   TEST_FILE=target.as_posix(), TEST_TOKEN=token)
    assert result.returncode != 0
    assert target.read_text() == "KEEP=unchanged\n"


def test_dry_token_update_does_not_write_or_print_token(tmp_path):
    target = tmp_path / "environment"
    result = shell('save_upload_token "$TEST_FILE" "$TEST_TOKEN"\n', tmp_path,
                   TEST_FILE=target.as_posix(), TEST_TOKEN="fixture-secret", TEST_DRY=1)
    assert result.returncode == 0 and not target.exists()
    assert "fixture-secret" not in result.stdout + result.stderr


def stage_fixture(tmp_path):
    source, home = tmp_path / "source", tmp_path / "home"
    source.mkdir(); home.mkdir()
    files = ["vehicle/gps_server.py", "vehicle/demo_sim.py", "vehicle/race_runtime.py",
             "vehicle/driver_dashboard.html", "shared/telemetry_protocol.py", "vehicle/bms_reader.py",
             "vehicle/backfill.py", "deploy/vehicle/lte_auto.sh"]
    for name in files:
        dest = source / Path(name).name
        shutil.copyfile(ROOT / name, dest)
        (home / dest.name).write_text("previous-install", encoding="utf8")
    return source, home


def stage_run(tmp_path, source, home, **extra):
    return shell('HOME_DIR="$TEST_HOME"\nSRC_DIR="$TEST_SOURCE"\nBASE_URL=""\n' + STAGE + '\necho SERVICE_STAGE_REACHED\n',
                 tmp_path, TEST_HOME=posix(home), TEST_SOURCE=posix(source), **extra)


@pytest.mark.parametrize("failure", ["missing_protocol", "invalid_python", "failed_model", "missing_dashboard", "legacy_demo"])
def test_invalid_bundle_never_replaces_existing_files(tmp_path, failure):
    source, home = stage_fixture(tmp_path)
    if failure == "missing_protocol":
        (source / "telemetry_protocol.py").unlink()
    elif failure == "invalid_python":
        (source / "gps_server.py").write_text("def broken(", encoding="utf8")
    elif failure == "failed_model":
        (source / "demo_sim.py").write_text(
            "def serve_v7(*args): pass\ndef demo_profile(*args): pass\n"
            "if __name__ == '__main__': raise SystemExit(17)\n", encoding="utf8")
    elif failure == "legacy_demo":
        (source / "demo_sim.py").write_text("# v6 model-only demo has no v7 input adapter\n", encoding="utf8")
    else:
        (source / "driver_dashboard.html").write_text("", encoding="utf8")
    result = stage_run(tmp_path, source, home)
    assert result.returncode != 0, result.stdout
    assert "SERVICE_STAGE_REACHED" not in result.stdout
    assert all(path.read_text(encoding="utf8") == "previous-install" for path in home.iterdir())
    assert not list(home.glob("*.bak-*")) and not list(home.glob(".unimotors-stage.*"))


@pytest.mark.parametrize("role", ["demo", "car"])
def test_v7_bundle_installs_flat_and_reruns(tmp_path, role):
    source, home = stage_fixture(tmp_path)
    for _ in range(2):
        result = stage_run(tmp_path, source, home, TEST_ROLE=role)
        assert result.returncode == 0, result.stderr
        assert "SERVICE_STAGE_REACHED" in result.stdout
    for name in ("gps_server.py", "race_runtime.py", "telemetry_protocol.py", "driver_dashboard.html"):
        assert (home / name).read_bytes() == (source / name).read_bytes()
    assert not list(home.glob(".unimotors-stage.*"))


def test_installer_dry_run_is_only_a_plan(tmp_path):
    source, home = stage_fixture(tmp_path)
    result = stage_run(tmp_path, source, home, TEST_DRY=1)
    assert result.returncode == 0, result.stderr
    assert "계산 검증 통과" not in result.stdout
    assert all(path.read_text() == "previous-install" for path in home.iterdir())
