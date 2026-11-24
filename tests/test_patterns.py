import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import patterns as P  # noqa: E402
import strategy as S  # noqa: E402


def df_of(rows):
    idx = pd.date_range("2026-01-01", periods=len(rows), freq="h", tz="UTC")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx).assign(volume=1.0)


def test_pin_bar_buy_and_sell():
    buy = P.pin_bar(df_of([(100, 101, 94, 100.5)]).iloc[0])
    assert buy and buy.direction == 1 and buy.levels["stop_ref"] == 94
    sell = P.pin_bar(df_of([(100, 106, 99.5, 99.8)]).iloc[0])
    assert sell and sell.direction == -1 and sell.levels["stop_ref"] == 106


def test_pin_bar_needs_2x_opposite_wick():
    assert P.pin_bar(df_of([(100, 103.5, 95, 100.5)]).iloc[0]) is None


def test_engulfing_direction():
    d = df_of([(100, 101, 99, 99.5), (99.2, 102, 98.5, 101.5)])
    e = P.engulfing(d.iloc[0], d.iloc[1])
    assert e.direction == 1 and e.levels["entry"] == 101.5


def test_inside_bar_levels():
    d = df_of([(100, 105, 95, 103), (102, 104, 97, 101)])
    p = P.inside_bar(d.iloc[0], d.iloc[1])
    assert P.resolve(p, 1) == (104, 95)
    assert P.resolve(p, -1) == (97, 105)


def test_ppr_sell_and_buy():
    d = df_of([(100, 103, 99, 102.5), (102, 102.8, 97, 98)])
    p = P.ppr(d.iloc[0], d.iloc[1])
    assert p.direction == -1 and p.levels["entry"] == 99 and p.levels["stop_ref"] == 102.8
    d = df_of([(102, 103, 99, 99.5), (99.6, 105, 99.2, 104)])
    assert P.ppr(d.iloc[0], d.iloc[1]).direction == 1


def test_fakey_valid_and_worst_order():
    d = df_of([(100, 106, 94, 104), (103, 105, 97, 99), (99, 107, 98, 100)])
    f = P.fakey(*[d.iloc[k] for k in range(3)])
    assert f.direction == -1 and f.levels["entry"] == 100 and "материнской" in f.note
    worst = df_of([(100, 106, 94, 104), (103, 105, 97, 99), (99, 106, 95, 96)])
    assert P.fakey(*[worst.iloc[k] for k in range(3)]) is None


def test_inside_pin_bar_treated_as_inside():
    d = df_of([(100, 110, 90, 105), (101, 109, 100.5, 101.5)])
    pats = P.detect(d, 1)
    assert {p.kind for p in pats} >= {"pin", "inside"}
    best, _ = P.select(pats, 1)
    assert best.kind == "inside"


def test_pin_vs_opposite_engulfing_pin_wins():
    # Свеча перекрыла предыдущую, но у неё крошечное тело и длинный нижний шип: это пин-бар на покупку,
    # а не поглощение на продажу
    d = df_of([(100, 101, 99.5, 100.6), (100.9, 101.2, 97, 100.7)])
    pats = P.detect(d, 1)
    kinds = {(p.kind, p.direction) for p in pats}
    assert ("engulfing", -1) not in kinds and ("pin", 1) in kinds
    best, why = P.select(pats, 1)
    assert best.kind == "pin" and why is None
    assert P.select(pats, -1) == (None, None)


def test_zone_deleted_on_close_beyond():
    d = df_of([(100, 101, 99, 100), (100, 103, 99.5, 102.5)])
    zones = [{"id": 1, "type": "R", "low": 101, "high": 102}, {"id": 2, "type": "S", "low": 95, "high": 96}]
    ev = S.delete_broken_zones(d, zones)
    assert [z["id"] for z in zones] == [2] and ev[0].kind == "zone_deleted"


def test_zone_kept_when_only_wick_beyond():
    d = df_of([(100, 101, 99, 100), (100, 103, 99.5, 101.5)])
    zones = [{"id": 1, "type": "R", "low": 101, "high": 102}]
    S.delete_broken_zones(d, zones)
    assert len(zones) == 1


def test_1h_pin_at_support_gives_entry_with_rr_check():
    rows = [(110 - k, 111 - k, 109 - k, 109.5 - k) for k in range(10)]
    rows.append((100.2, 100.6, 96.5, 100.4))
    d = df_of(rows)
    zones = [{"id": 1, "type": "S", "low": 97, "high": 99}, {"id": 2, "type": "R", "low": 120, "high": 121}]
    ev = S.process_1h(d, zones, {"pending": [], "active": []})
    entry = [e for e in ev if e.kind == "entry"]
    assert entry and entry[0].levels["tp"] == 120
    zones[1] = {"id": 2, "type": "R", "low": 103, "high": 104}
    ev = S.process_1h(d, zones, {"pending": [], "active": []})
    assert [e.kind for e in ev] == ["skip"] and "R:R" in ev[0].text


def test_ppr_followup_50_percent():
    rows = [(100, 101, 99, 100)] * 5 + [(100, 103, 99.5, 102.5), (102, 102.8, 97, 97.5), (97.6, 101.5, 97.4, 101)]
    d = df_of(rows)
    st = {"pending": [{"tf": "1h", "kind": "ppr", "dir": -1, "entry": 99.5, "stop": 102.9, "half": 99.9,
                       "trigger": "above", "created": d.index[6].isoformat(), "checked_next": False,
                       "tp": 90, "structure": None}]}
    ev = S.follow_pending(d, st, "1h")
    assert ev[0].kind == "ppr_50" and st["pending"] == []
