"""Commit and push a job's outputs without failing on a race with another commit.

Every workflow used `git commit; git pull --rebase; git push`. If anything else changed the
same file meanwhile (most often state/run_state.json), the rebase stopped on a conflict, the
job failed and nothing was saved. This helper retries, and resolves conflicts itself:

  * state/run_state.json: the two versions are merged key by key (newest last_success per job,
    union of weekly entries, earliest first_run_date), so neither job's record is lost;
  * any other conflicted file: this job's freshly written version wins (they are generated
    outputs; owner files are never written by more than one job).

Usage: python -m pipeline.gitsync "commit message" path [path ...]
"""
from __future__ import annotations

import json
import subprocess
import sys
import time

STATE = "state/run_state.json"


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], check=check, text=True, capture_output=True)


def _later(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def merge_state(upstream: dict, mine: dict) -> dict:
    out = dict(upstream)
    out.update({k: v for k, v in mine.items() if k not in ("last_success", "weekly", "first_run_date")})
    firsts = [d for d in (upstream.get("first_run_date"), mine.get("first_run_date")) if d]
    out["first_run_date"] = min(firsts) if firsts else None
    ls = dict(upstream.get("last_success") or {})
    for k, v in (mine.get("last_success") or {}).items():
        ls[k] = _later(ls.get(k), v)
    out["last_success"] = ls
    weekly = dict(upstream.get("weekly") or {})
    weekly.update(mine.get("weekly") or {})
    out["weekly"] = dict(sorted(weekly.items()))
    return out


def _show(stage: int, path: str) -> str | None:
    r = git("show", f":{stage}:{path}", check=False)
    return r.stdout if r.returncode == 0 else None


def resolve_conflicts() -> list[str]:
    """During a rebase, stage 2 is upstream ("ours") and stage 3 is this job's commit ("theirs")."""
    files = [f for f in git("diff", "--name-only", "--diff-filter=U").stdout.split() if f]
    for f in files:
        up, mine = _show(2, f), _show(3, f)
        if f == STATE and up and mine:
            merged = merge_state(json.loads(up), json.loads(mine))
            with open(f, "w") as fh:
                fh.write(json.dumps(merged, indent=2, sort_keys=True) + "\n")
            git("add", f)
        elif mine is not None:
            git("checkout", "--theirs", "--", f)
            git("add", f)
        else:
            git("rm", "-q", "--", f)
    return files


def sync(message: str, paths: list[str], attempts: int = 4) -> bool:
    git("add", "--", *paths)
    if git("diff", "--cached", "--quiet", check=False).returncode == 0:
        print("nothing to commit")
        return False
    git("commit", "-q", "-m", message)
    for n in range(attempts):
        r = git("pull", "--rebase", check=False)
        while r.returncode != 0 and git("rev-parse", "-q", "--verify", "REBASE_HEAD", check=False).returncode == 0:
            fixed = resolve_conflicts()
            print("resolved conflicts in: " + ", ".join(fixed))
            r = subprocess.run(["git", "-c", "core.editor=true", "rebase", "--continue"], text=True, capture_output=True)
        if r.returncode != 0:
            print(r.stdout + r.stderr)
            git("rebase", "--abort", check=False)   # leave this job's files intact for later steps
            raise SystemExit("git pull --rebase failed")
        p = git("push", check=False)
        if p.returncode == 0:
            print("pushed")
            return True
        print(f"push rejected (attempt {n + 1}): {p.stderr.strip()}")
        time.sleep(2 * (n + 1))
    raise SystemExit("could not push after several attempts")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    sync(sys.argv[1], sys.argv[2:])
