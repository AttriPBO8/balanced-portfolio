"""FinBERT headline sentiment (§8.3 T3-8). Runs locally on CPU in GitHub Actions.

Scores are written to artifacts/news/scores/YYYY/YYYY-MM.jsonl, keyed by news item id, so the
collected news files are never rewritten. Only items without a score are scored.

    pip install -r requirements-nlp.txt
    python -m ingest.finbert

If `transformers` is not installed, this logs a line and exits 0, so the rest of the
pipeline never depends on it.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)
MODEL = "ProsusAI/finbert"


def load_scores(root: Path) -> dict[str, dict]:
    out = {}
    for p in sorted((Path(root) / "artifacts/news/scores").glob("*/*.jsonl")):
        for line in p.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                out[r["id"]] = r
    return out


def _unscored(root: Path, have: set[str]) -> list[dict]:
    items = []
    for p in sorted((Path(root) / "artifacts/news").glob("[0-9]*/*.jsonl")):
        for line in p.read_text().splitlines():
            if line.strip():
                it = json.loads(line)
                if it["id"] not in have and it.get("title"):
                    items.append(it)
    return items


def finbert_scorer(batch_size: int = 32) -> Callable[[list[str]], list[dict]]:
    from transformers import pipeline  # optional dependency
    clf = pipeline("text-classification", model=MODEL, top_k=None, truncation=True, device=-1)

    def score(texts: list[str]) -> list[dict]:
        out = []
        for i in range(0, len(texts), batch_size):
            for res in clf(texts[i:i + batch_size]):
                d = {r["label"].lower(): float(r["score"]) for r in res}
                out.append({"pos": d.get("positive", 0.0), "neg": d.get("negative", 0.0), "neu": d.get("neutral", 0.0)})
        return out
    return score


def score_new(root: Path, scorer: Callable[[list[str]], list[dict]] | None = None, model_name: str = MODEL,
              limit: int = 2000) -> int:
    root = Path(root)
    have = set(load_scores(root))
    items = _unscored(root, have)[:limit]
    if not items:
        return 0
    if scorer is None:
        try:
            scorer = finbert_scorer()
        except ImportError:
            log.warning("transformers not installed; skipping FinBERT scoring (pip install -r requirements-nlp.txt)")
            return 0
    res = scorer([it["title"] for it in items])
    by_month: dict[Path, list[str]] = {}
    for it, r in zip(items, res):
        ts = it.get("first_seen_utc") or it.get("published_utc") or "1970-01"
        p = root / f"artifacts/news/scores/{ts[:4]}/{ts[:7]}.jsonl"
        rec = {"id": it["id"], "pos": round(r["pos"], 4), "neg": round(r["neg"], 4), "neu": round(r["neu"], 4),
               "score": round(r["pos"] - r["neg"], 4), "model": model_name}
        by_month.setdefault(p, []).append(json.dumps(rec, sort_keys=True))
    for p, lines in by_month.items():
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as fh:
            fh.write("\n".join(lines) + "\n")
    return len(items)


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    n = score_new(Path(a.root))
    print(json.dumps({"scored": n}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
