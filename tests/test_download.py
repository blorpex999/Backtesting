"""Download: CSV parsing, BID/ASK merge, duplicates, incremental updates, failures."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pandas as pd
import pytest

from btlab.data.download import (
    Chunk,
    Downloader,
    DownloadError,
    DukascopyNodeSource,
    merge_sides,
    month_chunks,
    parse_candles_csv,
)
from btlab.data.settings import DownloadSettings
from btlab.data.synthetic import synthetic_m1
from tests.conftest import FakeSource


def _utc(s: str) -> pd.Timestamp:
    return pd.Timestamp(s, tz="UTC")


def _downloader(ctx, source, now="2020-02-10 15:30") -> Downloader:
    settings = ctx.settings.download.model_copy(
        update={"history_start": pd.Timestamp("2019-11-01").date()}
    )
    return Downloader(source, ctx.paths.raw, ctx.store, settings, now=lambda: _utc(now))


def test_month_chunks():
    chunks = month_chunks(_utc("2019-11-15"), _utc("2020-02-10"))
    assert [c.key for c in chunks] == ["2019-11", "2019-12", "2020-01", "2020-02"]
    assert Chunk(2019, 12).end == _utc("2020-01-01")
    assert month_chunks(_utc("2020-01-01"), _utc("2020-01-01")) == []


def test_parse_dukascopy_csv(tmp_path: Path):
    path = tmp_path / "x.csv"
    path.write_text(
        "timestamp,open,high,low,close,volume\n"
        "1577916000000,1.12123,1.1213,1.1211,1.12125,12.34\n"
        "1577916060000,1.12125,1.1214,1.1212,1.1213,3.5\n"
        "1580515200000,1.2,1.2,1.2,1.2,1\n",  # 2020-02-01: outside the requested month
        encoding="utf-8",
    )
    df, outside = parse_candles_csv(path, _utc("2020-01-01"), _utc("2020-02-01"))
    assert outside == 1
    assert list(df.columns) == ["ts_utc", "o", "h", "l", "c", "v"]
    assert df["ts_utc"].iloc[0] == _utc("2020-01-01 22:00")
    assert str(df["ts_utc"].dtype) == "datetime64[ns, UTC]"
    assert df["h"].iloc[0] == pytest.approx(1.1213)


def test_parse_empty_file_and_unexpected_header(tmp_path: Path):
    empty = tmp_path / "empty.csv"
    empty.write_text("", encoding="utf-8")
    df, outside = parse_candles_csv(empty)
    assert df.empty and outside == 0
    bad = tmp_path / "bad.csv"
    bad.write_text("time,o,h,l,c\n1,1,1,1,1\n", encoding="utf-8")
    with pytest.raises(DownloadError, match="Format CSV inattendu"):
        parse_candles_csv(bad)


def _side(ts: list[str], price: float) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts_utc": pd.DatetimeIndex([_utc(t) for t in ts]).as_unit("ns"),
            "o": price,
            "h": price,
            "l": price,
            "c": price,
            "v": 1.0,
        }
    )


def test_merge_reports_duplicates_and_keeps_missing_side_visible():
    bid = pd.concat(
        [
            _side(["2020-01-02 10:00", "2020-01-02 10:01", "2020-01-02 10:02"], 1.1),
            _side(["2020-01-02 10:01"], 1.1),  # exact duplicate -> removed, reported
            _side(["2020-01-02 10:02"], 1.2),  # conflicting duplicate -> dropped, reported
        ],
        ignore_index=True,
    )
    ask = _side(["2020-01-02 10:00", "2020-01-02 10:01", "2020-01-02 10:03"], 1.1001)
    merged, issues = merge_sides(bid, ask)
    assert list(merged["ts_utc"].dt.strftime("%H:%M")) == ["10:00", "10:01", "10:03"]
    assert merged.loc[2, "bid_c"] != merged.loc[2, "bid_c"]  # NaN: ASK only at 10:03
    kinds = dict(zip(issues["kind"], issues["ts_utc"].dt.strftime("%H:%M"), strict=True))
    assert kinds == {"exact_duplicate": "10:01", "conflicting_duplicate": "10:02"}


def test_first_download_then_incremental_update(ctx, eurusd_frame):
    inst = ctx.instrument("EURUSD")
    source = FakeSource({"eurusd": eurusd_frame})
    dl = _downloader(ctx, source, now="2020-02-10 15:30")

    report = dl.download(inst, log=lambda _: None)
    assert report.ok and report.planned == ["2019-11", "2019-12", "2020-01", "2020-02"]
    assert report.years_built == [2019, 2020]
    manifest = json.loads(dl.manifest_path("EURUSD").read_text())
    assert manifest["chunks"]["2020-01"]["complete"] is True
    feb = manifest["chunks"]["2020-02"]
    assert feb["complete"] is False and feb["end"].startswith("2020-02-10")  # complete days only
    stored = ctx.store.read_years("EURUSD", [2019, 2020])
    expected = eurusd_frame[eurusd_frame["ts_utc"] < _utc("2020-02-10")]
    assert len(stored) == len(expected)
    pd.testing.assert_series_equal(
        stored["bid_c"], expected["bid_c"].reset_index(drop=True), check_names=False
    )

    # Next day: only the partial month is fetched again.
    source.calls.clear()
    dl.now = lambda: _utc("2020-02-11 08:00")
    report = dl.download(inst, log=lambda _: None)
    assert report.planned == ["2020-02"]
    assert {(c[1], c[2], c[3]) for c in source.calls} == {
        ("bid", _utc("2020-02-01"), _utc("2020-02-11")),
        ("ask", _utc("2020-02-01"), _utc("2020-02-11")),
    }
    assert report.years_built == [2020]

    # Nothing new: no call at all.
    source.calls.clear()
    assert dl.download(inst, log=lambda _: None).planned == ["2020-02"]  # still partial month
    dl.now = lambda: _utc("2020-03-05")
    dl.download(inst, end=_utc("2020-03-01"), log=lambda _: None)
    source.calls.clear()
    assert dl.plan(inst, end=_utc("2020-03-01")) == []


def test_first_month_starting_mid_month_is_complete(ctx):
    # US30 history starts on 2013-09-30: that first (short) month must not be fetched again.
    inst = ctx.instrument("US30")
    frame = synthetic_m1(inst, "2013-09-30", "2013-11-05", seed=2, price=15000.0)
    dl = Downloader(
        FakeSource({"usa30idxusd": frame}),
        ctx.paths.raw,
        ctx.store,
        ctx.settings.download,
        now=lambda: _utc("2013-11-05 10:00"),
    )
    report = dl.download(inst, log=lambda _: None)
    assert report.planned == ["2013-09", "2013-10", "2013-11"]
    assert [c.key for c in dl.plan(inst)] == ["2013-11"]  # only the current month
    # A download that stopped mid-month (--to) is not complete either.
    dl2 = _downloader(
        ctx,
        FakeSource(
            {"eurusd": synthetic_m1(ctx.instrument("EURUSD"), "2019-11-01", "2019-12-01", seed=3)}
        ),
        now="2020-01-10",
    )
    dl2.download(ctx.instrument("EURUSD"), end=_utc("2019-11-15"), log=lambda _: None)
    assert "2019-11" in [c.key for c in dl2.plan(ctx.instrument("EURUSD"))]


def test_failed_chunk_is_retried_on_next_run(ctx, eurusd_frame):
    inst = ctx.instrument("EURUSD")
    source = FakeSource({"eurusd": eurusd_frame})
    source.fail_on = {("2019-12", "ask")}
    dl = _downloader(ctx, source, now="2020-01-15")
    report = dl.download(inst, log=lambda _: None)
    assert not report.ok and report.failed[0][:2] == ("2019-12", "ask")
    assert "2019-12" not in json.loads(dl.manifest_path("EURUSD").read_text())["chunks"]
    assert any("mois non téléchargés 2019-12" in w for w in report.warnings)

    source.fail_on = set()
    source.calls.clear()
    report = dl.download(inst, log=lambda _: None)
    assert report.ok and "2019-12" in report.planned and "2019-11" not in report.planned


def test_empty_month_while_quotes_expected_is_flagged(ctx, eurusd_frame):
    inst = ctx.instrument("EURUSD")
    frame = eurusd_frame[
        (eurusd_frame["ts_utc"] < _utc("2019-12-01"))
        | (eurusd_frame["ts_utc"] >= _utc("2020-01-01"))
    ]
    dl = _downloader(ctx, FakeSource({"eurusd": frame}), now="2020-01-15")
    report = dl.download(inst, log=lambda _: None)
    assert any("2019-12 : aucune bougie reçue" in w for w in report.warnings)


def test_build_records_duplicate_issues(ctx, eurusd_frame):
    inst = ctx.instrument("EURUSD")
    dl = _downloader(ctx, FakeSource({"eurusd": eurusd_frame}), now="2020-01-01")
    dl.download(inst, log=lambda _: None)
    raw = dl.raw_path("EURUSD", "bid", Chunk(2019, 12))
    lines = raw.read_text().splitlines()
    raw.write_text("\n".join([*lines, lines[5]]) + "\n")  # duplicate one candle
    dl.build(inst, [2019], log=lambda _: None)
    issues = ctx.store.read_issues("EURUSD", [2019])
    assert list(issues["kind"]) == ["exact_duplicate"]


# --- the real subprocess plumbing, with a stand-in for the dukascopy-node CLI ------------
FAKE_CLI = r"""
const fs = require('fs'); const path = require('path');
const a = process.argv.slice(2); const get = (k) => a[a.indexOf(k) + 1];
const out = path.join(get('-dir'), get('-fn') + '.csv');
fs.mkdirSync(get('-dir'), {recursive: true});
if (process.env.FAKE_FAIL) {
  fs.writeFileSync(out, '');  // the real CLI leaves an empty file behind
  console.error('Request failed with status 403'); process.exit(1);
}
fs.writeFileSync(out, 'timestamp,open,high,low,close,volume\n1577916000000,1.1,1.2,1.0,1.15,2\n');
fs.writeFileSync(out + '.args.json', JSON.stringify(a));
"""


@pytest.fixture
def fake_node_source(tmp_path: Path) -> DukascopyNodeSource:
    if shutil.which("node") is None:
        pytest.skip("Node.js absent")
    source = DukascopyNodeSource(tmp_path / "tools", DownloadSettings())
    source.cli_js.parent.mkdir(parents=True)
    source.cli_js.write_text(FAKE_CLI, encoding="utf-8")
    return source


def test_node_source_command_and_output(fake_node_source, tmp_path):
    dest = tmp_path / "raw" / "EURUSD" / "bid" / "2020-01.csv"
    fake_node_source.fetch("eurusd", "bid", _utc("2020-01-01"), _utc("2020-02-01"), dest)
    df, _ = parse_candles_csv(dest)
    assert len(df) == 1
    args = json.loads(next((dest.parent / ".tmp").glob("*.args.json")).read_text())
    assert args[args.index("-i") + 1] == "eurusd"
    assert args[args.index("-from") + 1] == "2020-01-01"
    assert args[args.index("-to") + 1] == "2020-02-01"
    assert args[args.index("-t") + 1] == "m1" and args[args.index("-p") + 1] == "bid"
    assert "-v" in args, "volumes are required for flat candles to be filtered"
    assert int(args[args.index("-r") + 1]) >= 1
    assert args[args.index("-utc") + 1] == "0"


def test_node_source_failure_raises(fake_node_source, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_FAIL", "1")
    dest = tmp_path / "raw" / "x.csv"
    with pytest.raises(DownloadError, match="status 403"):
        fake_node_source.fetch("eurusd", "ask", _utc("2020-01-01"), _utc("2020-02-01"), dest)
    assert not dest.exists()
    assert not list((dest.parent / ".tmp").glob("*.csv")), "no leftover temporary file"


def test_missing_node_gives_install_hint(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(DownloadError, match=r"winget install OpenJS\.NodeJS\.LTS"):
        DukascopyNodeSource.node_executable()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_windows_paths_with_spaces(fake_node_source, tmp_path):  # pragma: no cover
    dest = tmp_path / "dossier avec espaces" / "2020-01.csv"
    fake_node_source.fetch("eurusd", "bid", _utc("2020-01-01"), _utc("2020-02-01"), dest)
    assert dest.exists()
