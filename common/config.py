"""Load the YAML config files in /config into one dict (§3.2, §4).

Each file becomes a top-level key named after the file stem, e.g. config/risk.yaml -> cfg["risk"].
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

REQUIRED_FILES = (
    "settings", "risk", "checks", "accounts", "universe", "sectors",
    "sources", "issuer_aliases", "tunable",
)


class ConfigError(ValueError):
    pass


def load_config(root: Path | str, overrides: dict | None = None) -> dict[str, Any]:
    root = Path(root)
    cfg: dict[str, Any] = {}
    for name in REQUIRED_FILES:
        path = root / "config" / f"{name}.yaml"
        if not path.exists():
            raise ConfigError(f"missing config file: {path}")
        with path.open() as fh:
            cfg[name] = yaml.safe_load(fh) or {}
    if overrides:
        cfg = deep_merge(cfg, overrides)
    validate_config(cfg)
    return cfg


def deep_merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def validate_config(cfg: dict) -> None:
    s = cfg["settings"]
    for key in ("data", "schedule", "callouts", "transaction_caps", "models", "starter_phase"):
        if key not in s:
            raise ConfigError(f"settings.yaml is missing '{key}'")
    if s["data"]["mode"] not in ("live", "sample"):
        raise ConfigError("settings.data.mode must be live or sample")
    w = s["models"]["blend"]
    if abs(w["w_lgbm"] + w["w_ridge"] - 1.0) > 1e-9:
        raise ConfigError("blend weights must sum to 1")
    caps = s["transaction_caps"]
    for acct in ("TFSA", "RRSP", "NONREG"):
        if int(caps[acct]["max_per_month"]) < 0:
            raise ConfigError(f"transaction cap for {acct} must be >= 0")
    if caps["weekly_spread"] not in ("even", "front_loaded"):
        raise ConfigError("weekly_spread must be even or front_loaded")
    if caps["risk_exit_over_cap"] not in ("warn", "allow", "block"):
        raise ConfigError("risk_exit_over_cap must be warn, allow or block")
    uni = cfg["universe"].get("tsx_core") or []
    missing = [t for t in uni if t not in cfg["sectors"]]
    if missing:
        raise ConfigError(f"tickers without a sector in sectors.yaml: {missing}")


def universe(cfg: dict) -> list[str]:
    return list(cfg["universe"].get("tsx_core") or [])


def enabled_accounts(cfg: dict) -> list[str]:
    accts = ["TFSA", "RRSP"]
    if (cfg["accounts"].get("NONREG") or {}).get("enabled"):
        accts.append("NONREG")
    return accts
