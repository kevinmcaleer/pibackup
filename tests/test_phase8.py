"""Phase 8: background tuning, systemd units, installer, bootstrap one-liner."""

import shutil
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from pibackup.client.cli import app
from pibackup.client.engine import BackupEngine
from pibackup.common.config import Config, JobSpec
from pibackup.common.transfer import background_prefix

runner = CliRunner()
ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"


def _config(tmp_path, **extra) -> Config:
    repo = tmp_path / "repo"
    return Config(
        data_dir=tmp_path,
        repo_dir=repo,
        db_path=tmp_path / "pibackup.db",
        repo_target=str(repo),
        client_name="testpi",
        **extra,
    )


# ---- background tuning ----
def test_background_prefix():
    prefix = background_prefix()
    assert isinstance(prefix, list)
    if shutil.which("nice"):
        assert "nice" in prefix
    if shutil.which("ionice"):
        assert "ionice" in prefix


def test_engine_runs_with_and_without_background(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "f.txt").write_text("data")

    res_bg = BackupEngine(_config(tmp_path, background=True)).run_job(
        JobSpec(name="a", sources=[str(src)])
    )
    assert res_bg.ok, res_bg.message

    res_fg = BackupEngine(_config(tmp_path, background=False)).run_job(
        JobSpec(name="b", sources=[str(src)])
    )
    assert res_fg.ok, res_fg.message


# ---- systemd units ----
def test_systemd_units_present():
    for name in ("pibackup-backup.service", "pibackup-backup.timer", "pibackup-server.service"):
        text = (DEPLOY / name).read_text()
        assert "[Unit]" in text
    assert "pibackup run" in (DEPLOY / "pibackup-backup.service").read_text()
    assert "OnCalendar" in (DEPLOY / "pibackup-backup.timer").read_text()
    assert "pibackup serve" in (DEPLOY / "pibackup-server.service").read_text()


# ---- installer ----
def test_install_script_syntax():
    script = DEPLOY / "install.sh"
    assert script.exists()
    # POSIX sh syntax check
    assert subprocess.run(["sh", "-n", str(script)]).returncode == 0
    text = script.read_text()
    assert "github.com/kevinmcaleer/pibackup" in text
    assert "git+$REPO" in text  # installs from the repo
    assert "connect" in text and "--timer" in text
    # PEP 668-safe: never the blocked system `pip install --user`; pipx or venv.
    assert "pip install --user" not in text
    assert "pipx" in text and "venv" in text
    # Runs as root, installing a system timer so backups read every file.
    assert "id -u" in text  # refuses to run unprivileged
    assert "/etc/systemd/system" in text  # system (not --user) timer


# ---- bootstrap one-liner ----
def test_cli_enroll_includes_install_oneliner(tmp_path):  # XDG isolated => own db
    result = runner.invoke(app, ["enroll", "kitchen-pi"])
    assert result.exit_code == 0, result.output
    assert "install.sh" in result.output
    assert "curl -fsSL" in result.output
    assert "sudo sh -s --" in result.output  # installs as root
    assert "pibackup connect" in result.output
