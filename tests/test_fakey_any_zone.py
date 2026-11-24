import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import strategy as S  # noqa: E402


def df_of(rows):
    idx = pd.date_range("2026-01-01", periods=len(rows), freq="h", tz="UTC")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx).assign(volume=1.0)


FAKEY_SELL = [(100, 106, 94, 104), (103, 105, 97, 99), (99, 107, 98, 100)]


def test_fakey_at_support_zone_is_notification_only():
    zones = [{"id": 2, "type": "S", "low": 95, "high": 101}]
    ev = S.process_1h(df_of(FAKEY_SELL), zones, {"pending": [], "active": []})
    assert [e.kind for e in ev] == ["fakey"]
    assert "только уведомление" in ev[0].text and "Зона рядом: поддержка #2" in ev[0].text


def test_fakey_far_from_zone_still_notifies():
    zones = [{"id": 2, "type": "S", "low": 50, "high": 60}]
    ev = S.process_1h(df_of(FAKEY_SELL), zones, {"pending": [], "active": []})
    assert [e.kind for e in ev] == ["fakey"]
    assert "Зона рядом" not in ev[0].text


def test_fakey_without_zones_notifies():
    ev = S.process_1h(df_of(FAKEY_SELL), [], {"pending": [], "active": []})
    assert [e.kind for e in ev] == ["fakey"]


def test_pin_sell_at_support_zone_still_ignored():
    rows = [(100, 101, 99, 100), (100, 101, 99, 100), (100, 107, 98.5, 100)]
    zones = [{"id": 2, "type": "S", "low": 95, "high": 101}]
    ev = S.process_1h(df_of(rows), zones, {"pending": [], "active": []})
    assert [e.kind for e in ev if e.kind == "entry"] == []



def _inside_pending(dir_, created, entry):
    return {"tf": "1h", "kind": "inside", "dir": dir_, "entry": entry, "stop": 95.0 if dir_ > 0 else 110.0,
            "tp": 115.0 if dir_ > 0 else 85.0, "trigger": "above" if dir_ > 0 else "below",
            "created": created.isoformat(), "checked_next": True, "structure": None}


def test_sell_fakey_cancels_pending_inside_buy_before_fill():
    df = df_of([(100, 106, 94, 104), (103, 105, 97, 99), (99, 107, 98, 100)])
    st = {"pending": [_inside_pending(1, df.index[1], 105.0)], "active": []}
    ev = S.process_1h(df, [{"id": 2, "type": "S", "low": 95, "high": 101}], st)
    kinds = [e.kind for e in ev]
    assert "cancel" in kinds and "filled" not in kinds and "entry" not in kinds
    assert st["pending"] == [] and st["active"] == []


def test_buy_fakey_cancels_pending_inside_sell():
    df = df_of([(100, 106, 90, 96), (97, 99, 92, 95), (96, 96, 90, 93)])
    st = {"pending": [_inside_pending(-1, df.index[1], 92.0)], "active": []}
    ev = S.process_1h(df, [{"id": 3, "type": "R", "low": 105, "high": 110}], st)
    assert "cancel" in [e.kind for e in ev] and st["pending"] == []


def test_same_direction_fakey_does_not_cancel():
    df = df_of([(100, 106, 94, 104), (103, 105, 97, 99), (99, 107, 98, 100)])
    st = {"pending": [_inside_pending(-1, df.index[1], 90.0)], "active": []}
    ev = S.process_1h(df, [{"id": 2, "type": "S", "low": 95, "high": 101}], st)
    assert "cancel" not in [e.kind for e in ev] and len(st["pending"]) == 1
