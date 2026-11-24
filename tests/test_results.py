import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import results  # noqa: E402
import strategy as S  # noqa: E402


def df_of(rows):
    idx = pd.date_range("2026-01-01", periods=len(rows), freq="h", tz="UTC")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx).assign(volume=1.0)


def trade(created="2026-01-01T00:00:00+00:00"):
    return {"dir": 1, "entry": 100.0, "stop": 95.0, "tp": 110.0, "structure": None, "tf": "1h", "created": created}


def closed_event(df, a):
    return S.follow_active(df, {"active": [a]}, "1h")


def use_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(results, "PATH", tmp_path / "results.jsonl")


def test_stop_and_tp_events_carry_r_and_key():
    stop_df = df_of([(100, 101, 99, 100), (100, 101, 99, 100), (100, 101, 94, 96)])
    ev = closed_event(stop_df, trade())
    assert ev[0].kind == "stop" and ev[0].r == -1.0 and ev[0].key
    tp_df = df_of([(100, 101, 99, 100), (100, 101, 99, 100), (100, 111, 99, 110.5)])
    ev = closed_event(tp_df, trade())
    assert ev[0].kind == "tp" and ev[0].r == 2.0


def test_record_dedupes_same_close(tmp_path, monkeypatch):
    use_tmp(tmp_path, monkeypatch)
    df = df_of([(100, 101, 99, 100), (100, 101, 99, 100), (100, 101, 94, 96)])
    ev = closed_event(df, trade())
    results.record(ev)
    results.record(ev)
    assert len(results._read()) == 1


def test_running_total_and_restart(tmp_path, monkeypatch):
    use_tmp(tmp_path, monkeypatch)
    stop_df = df_of([(100, 101, 99, 100), (100, 101, 99, 100), (100, 101, 94, 96)])
    tp_df = df_of([(100, 101, 99, 100), (100, 101, 99, 100), (100, 111, 99, 110.5)])
    results.record(closed_event(stop_df, trade("2026-01-01T00:00:00+00:00")))
    results.record(closed_event(tp_df, trade("2026-01-01T01:00:00+00:00")))
    text, png = results.report()
    assert "Итог: +1.00R" in text and "+10.00$" in text
    assert "→ -1.00R" in text and "→ +1.00R" in text
    assert png and Path(png).exists()
    text2, _ = results.report()
    assert text2 == text


def test_non_close_events_not_recorded(tmp_path, monkeypatch):
    use_tmp(tmp_path, monkeypatch)
    results.record([S.Event("entry", "1h", "x", {}, None), S.Event("skip", "1h", "y", {}, None)])
    assert results.report()[0] == "Закрытых сделок пока нет."
    assert not (tmp_path / "results.jsonl").exists()
