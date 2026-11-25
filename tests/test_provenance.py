import stat
import subprocess

import pytest

from folego.ingest import common, sih_national


def _fake_git(porcelain):
    def run(args, **_):
        if "status" in args:
            assert "--untracked-files=no" not in args
            return subprocess.CompletedProcess(args, 0, stdout=porcelain, stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="c0ffee\n", stderr="")

    return run


@pytest.mark.parametrize(("porcelain", "dirty"), [("", False), ("?? new_module.py\n", True)])
def test_untracked_file_marks_provenance_dirty(monkeypatch, porcelain, dirty):
    monkeypatch.setattr(sih_national.subprocess, "run", _fake_git(porcelain))
    result = sih_national.software_provenance()
    assert result["git_commit"] == "c0ffee"
    assert result["git_dirty"] is dirty
    assert result["git_dirty_includes_untracked"] is True


def test_missing_git_leaves_dirty_unknown(monkeypatch):
    def run(*_, **__):
        raise OSError("git not found")

    monkeypatch.setattr(sih_national.subprocess, "run", run)
    result = sih_national.software_provenance()
    assert result["git_commit"] is None and result["git_dirty"] is None


def test_failure_mid_write_keeps_old_file(tmp_path, monkeypatch):
    target = tmp_path / "report.json"
    target.write_text("old")

    def broken(_):
        raise OSError("disk full")

    monkeypatch.setattr(common.os, "fsync", broken)
    with pytest.raises(OSError):
        common.atomic_write_text(target, "new")
    assert target.read_text() == "old"
    assert list(tmp_path.iterdir()) == [target]


def test_report_is_private_whatever_the_umask(tmp_path):
    import os

    previous = os.umask(0o022)
    try:
        common.atomic_write_text(tmp_path / "report.json", "{}")
    finally:
        os.umask(previous)
    assert stat.S_IMODE((tmp_path / "report.json").stat().st_mode) == 0o600
