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
    uni = candidate_list(cfg)
    missing = [t for t in uni if t not in cfg["sectors"]]
    if missing:
        raise ConfigError(f"tickers without a sector in sectors.yaml: {missing}")


def universe(cfg: dict) -> list[str]:
    """Every ticker the run scores: the TSX-Core list, the optional US list, and (set at run time
    by with_holdings) anything the owner holds that is on neither list, so no holding goes unreviewed."""
    u = cfg["universe"]
    out: list[str] = []
    for t in list(u.get("tsx_core") or []) + list(u.get("us_track") or []) + list(u.get("held_extra") or []):
        if t not in out:
            out.append(t)
    return out


def candidate_list(cfg: dict) -> list[str]:
    """The owner's buy lists only (TSX-Core + US track), without the held-only additions."""
    u = cfg["universe"]
    return list(dict.fromkeys(list(u.get("tsx_core") or []) + list(u.get("us_track") or [])))


def buy_list(cfg: dict) -> list[str]:
    """Tickers that may be suggested as new buys: TSX-Core only (us_track is research only, §13.7)."""
    return list(cfg["universe"].get("tsx_core") or [])


def with_holdings(cfg: dict, held: set[str] | list[str]) -> list[str]:
    """Add held tickers that are not on the buy lists to this run's universe. Returns them."""
    listed = set(candidate_list(cfg))
    extra = sorted(t for t in set(held) if t not in listed)
    cfg["universe"]["held_extra"] = extra
    for t in extra:
        cfg["sectors"].setdefault(t, "Unclassified")
    return extra


def enabled_accounts(cfg: dict) -> list[str]:
    accts = ["TFSA", "RRSP"]
    if (cfg["accounts"].get("NONREG") or {}).get("enabled"):
        accts.append("NONREG")
    return accts
