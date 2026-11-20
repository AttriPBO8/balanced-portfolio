"""Alerts (§15.4). The pipeline writes .alerts/alerts.json; the workflow turns each entry
into a GitHub Issue. No email or push notifications in v2 (OD-6)."""
from __future__ import annotations

import json
from pathlib import Path


class Alerts:
    def __init__(self, root: Path):
        self.path = Path(root) / ".alerts" / "alerts.json"
        self.items: list[dict] = []

    def add(self, title: str, body: str) -> None:
        self.items.append({"title": title, "body": body})

    def flush(self) -> None:
        if not self.items:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        old = json.loads(self.path.read_text()) if self.path.exists() else []
        self.path.write_text(json.dumps(old + self.items, indent=2) + "\n")
        self.items = []
