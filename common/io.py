"""Deterministic JSON/CSV writing, hashing, schema validation and staged (atomic) writes."""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

import jsonschema
import numpy as np
import pandas as pd

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "tests" / "schemas"


def _clean(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating, float)):
        f = float(obj)
        if math.isnan(f) or math.isinf(f):
            return None
        return round(f, 6)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, (pd.Timestamp,)):
        return obj.date().isoformat()
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return obj


def dumps(obj: Any) -> str:
    return json.dumps(_clean(obj), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps(obj))


def read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text())


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def sha256_obj(obj: Any) -> str:
    return sha256_text(dumps(obj))


def sha256_file(path: Path) -> str:
    p = Path(path)
    if not p.exists():
        return "absent"
    return hashlib.sha256(p.read_bytes()).hexdigest()


def frame_hash(df: pd.DataFrame) -> str:
    return sha256_text(df.to_csv(float_format="%.8g"))


def new_run_id(seed_text: str | None = None) -> str:
    if seed_text is not None:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, seed_text))
    return str(uuid.uuid4())


def git_sha(root: Path) -> str:
    env = os.environ.get("GITHUB_SHA")
    if env:
        return env
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                              text=True, check=True).stdout.strip()
    except Exception:
        return "unknown"


def validate(obj: Any, schema_name: str) -> None:
    schema = json.loads((SCHEMA_DIR / f"{schema_name}.schema.json").read_text())
    jsonschema.validate(_clean(obj), schema)


class StagedWrite:
    """Collect every output of a run in a staging dir, validate, then move into place.

    If anything fails before commit(), nothing lands in the repository (§5.4 step 7-8).
    """

    def __init__(self, root: Path, run_id: str):
        self.root = Path(root)
        self.stage = self.root / ".staging" / run_id
        if self.stage.exists():
            shutil.rmtree(self.stage)
        self.stage.mkdir(parents=True)
        self.files: list[str] = []
        self._schemas: dict[str, str] = {}

    def json(self, rel: str, obj: Any, schema: str | None = None) -> None:
        if schema:
            validate(obj, schema)
            self._schemas[rel] = schema
        write_json(self.stage / rel, obj)
        self.files.append(rel)

    def csv(self, rel: str, df: pd.DataFrame) -> None:
        p = self.stage / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(p, float_format="%.6g")
        self.files.append(rel)

    def text(self, rel: str, text: str) -> None:
        p = self.stage / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        self.files.append(rel)

    def commit(self, last: tuple[str, ...] = ()) -> list[str]:
        """Move staged files into place. Files in `last` (e.g. run_state.json) move at the end."""
        for rel, schema in self._schemas.items():
            validate(read_json(self.stage / rel), schema)
        order = [f for f in self.files if f not in last] + [f for f in self.files if f in last]
        for rel in order:
            dst = self.root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.replace(self.stage / rel, dst)
        self.abort()
        return order

    def abort(self) -> None:
        if self.stage.exists():
            shutil.rmtree(self.stage)
        parent = self.stage.parent
        if parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
