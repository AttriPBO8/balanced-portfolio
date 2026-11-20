import datetime as dt
import json

from common.config import load_config
from ingest.news import NewsCollector, classify, match_tickers
from tests.conftest import REPO

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>Royal Bank of Canada Reports Fourth Quarter Results</title><link>https://x/1</link>
<description>TORONTO (TSX: RY) quarterly results ...</description><pubDate>Thu, 19 Nov 2026 12:00:00 GMT</pubDate><guid>1</guid></item>
<item><title>Tiny Miner Announces Private Placement</title><link>https://x/2</link>
<description>(TSXV: ABC) bought deal</description><pubDate>Thu, 19 Nov 2026 13:00:00 GMT</pubDate><guid>2</guid></item>
<item><title>Canadian Tire to acquire retailer</title><link>https://x/3</link>
<description>(TSX: CTC.A) acquisition</description><pubDate>Thu, 19 Nov 2026 14:00:00 GMT</pubDate><guid>3</guid></item>
</channel></rss>"""


def test_ticker_matching():
    cfg = load_config(REPO)
    uni = set(cfg["universe"]["tsx_core"])
    assert match_tickers("(TSX: RY)", uni, {}) == (["RY.TO"], "exchange_pattern")
    assert match_tickers("(TSX: CTC.A) deal", uni, {}) == (["CTC-A.TO"], "exchange_pattern")
    assert match_tickers("(TSXV: ABC)", uni, {})[0] == []
    assert match_tickers("Enbridge raises dividend", uni, cfg["issuer_aliases"])[0] == ["ENB.TO"]


def test_classify():
    assert classify("XYZ lowers guidance for 2027") == "guidance_cut"
    assert classify("XYZ announces $50M bought deal") == "financing"
    assert classify("XYZ reports third quarter results") == "earnings"


def test_collect_appends_and_dedups(tmp_path):
    import shutil
    shutil.copytree(REPO / "config", tmp_path / "config")
    cfg = load_config(tmp_path)
    cfg["sources"]["news"]["google_news"]["enabled"] = False
    nc = NewsCollector(cfg, tmp_path, fetch=lambda url, params=None: RSS)
    now = dt.datetime(2026, 11, 19, 20, 0, tzinfo=dt.timezone.utc)
    s1 = nc.collect(now)
    s2 = nc.collect(now)
    assert s1["new"] == 3 and s2["new"] == 0
    lines = (tmp_path / "artifacts/news/2026/2026-11.jsonl").read_text().splitlines()
    items = [json.loads(l) for l in lines]
    assert all(i["published_utc"] and i["first_seen_utc"] == "2026-11-19T20:00:00Z" for i in items)
    assert s1["by_source"]["newsfile"]["match_rate"] == round(2 / 3, 4)


def test_failing_source_is_skipped(tmp_path):
    import shutil
    shutil.copytree(REPO / "config", tmp_path / "config")
    cfg = load_config(tmp_path)
    cfg["sources"]["news"]["google_news"]["enabled"] = False

    def boom(url, params=None):
        raise ConnectionError("403")
    s = NewsCollector(cfg, tmp_path, fetch=boom).collect(dt.datetime(2026, 11, 19, tzinfo=dt.timezone.utc))
    assert s["collected"] == 0
