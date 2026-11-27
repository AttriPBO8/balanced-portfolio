import json
import os
import subprocess

from pipeline.gitsync import merge_state, sync


def sh(cwd, *a):
    return subprocess.run(list(a), cwd=cwd, check=True, text=True, capture_output=True).stdout


def test_merge_state_keeps_both_jobs_records():
    up = {"first_run_date": "2026-11-19", "last_success": {"daily": "2026-11-21", "weekly": None}, "version": 1, "weekly": {}}
    mine = {"first_run_date": "2026-11-21", "last_success": {"daily": "2026-11-20", "weekly": "2026-11-21"}, "version": 1,
            "weekly": {"2026-11-21": {"status": "published"}}}
    m = merge_state(up, mine)
    assert m["first_run_date"] == "2026-11-19"
    assert m["last_success"] == {"daily": "2026-11-21", "weekly": "2026-11-21"}
    assert m["weekly"] == {"2026-11-21": {"status": "published"}}


def test_sync_resolves_a_run_state_race(tmp_path, monkeypatch):
    remote, a, b = tmp_path / "remote.git", tmp_path / "a", tmp_path / "b"
    sh(tmp_path, "git", "init", "-q", "--bare", "-b", "main", str(remote))
    for d in (a, b):
        sh(tmp_path, "git", "clone", "-q", str(remote), str(d))
        sh(d, "git", "config", "user.email", "t@example.com")
        sh(d, "git", "config", "user.name", "t")
        sh(d, "git", "checkout", "-q", "-B", "main")
    (a / "README").write_text("x\n")
    sh(a, "git", "add", "README")
    sh(a, "git", "commit", "-qm", "init")
    sh(a, "git", "push", "-q", "-u", "origin", "main")
    sh(b, "git", "pull", "-q", "origin", "main")
    sh(b, "git", "branch", "-q", "--set-upstream-to=origin/main", "main")
    # job A (daily) adds run_state first and pushes
    (a / "state").mkdir()
    (a / "state/run_state.json").write_text(json.dumps({"first_run_date": "2026-11-19",
                                                        "last_success": {"daily": "2026-11-21", "weekly": None}, "weekly": {}}))
    sh(a, "git", "add", "state")
    sh(a, "git", "commit", "-qm", "daily")
    sh(a, "git", "push", "-q")
    # job B (weekly) started from the old checkout and writes its own run_state + an artifact
    (b / "state").mkdir()
    (b / "state/run_state.json").write_text(json.dumps({"first_run_date": "2026-11-21",
                                                        "last_success": {"daily": None, "weekly": "2026-11-21"},
                                                        "weekly": {"2026-11-21": {"status": "published"}}}))
    (b / "artifacts").mkdir()
    (b / "artifacts/callouts.json").write_text("{}\n")
    monkeypatch.chdir(b)
    assert sync("weekly", ["artifacts", "state"])
    sh(a, "git", "pull", "-q")
    st = json.loads((a / "state/run_state.json").read_text())
    assert st["last_success"] == {"daily": "2026-11-21", "weekly": "2026-11-21"}
    assert "2026-11-21" in st["weekly"] and (a / "artifacts/callouts.json").exists()
    assert os.path.exists(a / "README")
