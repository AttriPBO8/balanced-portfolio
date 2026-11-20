"""Shared fixtures. Tests run on SampleSource (deterministic synthetic prices), never the network."""
from __future__ import annotations

import datetime as dt
import shutil
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

TEST_UNIVERSE = [
    "RY.TO", "TD.TO", "BNS.TO", "BMO.TO", "CM.TO", "MFC.TO", "SLF.TO", "IFC.TO", "ENB.TO", "TRP.TO",
    "CNQ.TO", "SU.TO", "CNR.TO", "CP.TO", "WCN.TO", "ATD.TO", "L.TO", "DOL.TO", "SHOP.TO", "CSU.TO",
    "BCE.TO", "T.TO", "FTS.TO", "EMA.TO", "AQN.TO", "NTR.TO", "ABX.TO", "AEM.TO", "QBR-B.TO", "CCL-B.TO",
]


def utc(y, m, d, hh=15, mm=0) -> dt.datetime:
    return dt.datetime(y, m, d, hh, mm, tzinfo=dt.timezone.utc)


def make_root(tmp: Path, owner: bool = True, validation: bool = False, extra: dict | None = None) -> Path:
    root = tmp / "repo"
    shutil.copytree(REPO / "config", root / "config")
    for d in ("data/positions", "data/ledger"):
        (root / d).mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO / "data/delisting_log.csv", root / "data/delisting_log.csv")
    src = REPO / "tests/fixtures/owner" if owner else REPO / "data"
    if owner:
        shutil.copy(src / "positions.csv", root / "data/positions/positions.csv")
        for f in ("trades.csv", "cash.csv", "contributions.csv"):
            shutil.copy(src / f, root / "data/ledger" / f)
    else:
        shutil.copy(REPO / "data/positions/positions.csv", root / "data/positions/positions.csv")
        for f in ("trades.csv", "cash.csv", "contributions.csv"):
            shutil.copy(REPO / "data/ledger" / f, root / "data/ledger" / f)
    s = yaml.safe_load((root / "config/settings.yaml").read_text())
    s["data"]["mode"] = "sample"
    s["validation"]["run_in_weekly"] = validation
    for k, v in (extra or {}).items():
        s[k].update(v)
    (root / "config/settings.yaml").write_text(yaml.safe_dump(s, sort_keys=False))
    u = {"tsx_core": TEST_UNIVERSE, "us_track": []}
    (root / "config/universe.yaml").write_text(yaml.safe_dump(u))
    return root


@pytest.fixture
def root(tmp_path):
    return make_root(tmp_path)


@pytest.fixture
def empty_root(tmp_path):
    return make_root(tmp_path, owner=False)
