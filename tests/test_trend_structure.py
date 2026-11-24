"""Случаи с графиков пользователя: 15m, тренд ломается раньше, чем подтвердятся swing-точки,
и поглощение с крошечным телом, которое на деле пин-бар."""
import random
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config as C  # noqa: E402
import patterns as P  # noqa: E402
import strategy as S  # noqa: E402
from swings import leg_origin, swing_points  # noqa: E402


def df_of(rows):
    idx = pd.date_range("2026-11-22 14:00", periods=len(rows), freq="15min", tz="UTC")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx).assign(volume=1.0)


def prefix():
    """Боковик около 1720 с провалом до 1695 и всплеском до 1751, как на графиках."""
    random.seed(7)
    path = ([1716] * 10 + [1720, 1724, 1722, 1718, 1714, 1710, 1704, 1700, 1697, 1699, 1703, 1710, 1716, 1722, 1728, 1735,
            1742, 1738, 1732, 1730, 1728, 1731, 1733, 1729, 1725, 1723, 1726, 1724, 1722, 1721, 1723, 1725, 1722, 1720,
            1719, 1721, 1723, 1724, 1722, 1720, 1718, 1716, 1715, 1717, 1719, 1721, 1723, 1722, 1720, 1718, 1717, 1719,
            1721, 1722, 1720, 1719, 1718, 1720, 1722, 1721, 1719, 1718, 1717, 1719, 1720, 1722, 1723, 1722, 1720, 1719])
    rows, p = [], 1716.0
    for c in path:
        o = p
        r = random.uniform(2, 5)
        rows.append((o, max(o, c) + random.uniform(0.5, r), min(o, c) - random.uniform(0.5, r), c))
        p = c
    rows[24] = (1710, 1712, 1695.4, 1704)
    rows[34] = (1735, 1751.8, 1733, 1740)
    return rows


# Свечи A..I: импульс 1717→1845 (A, B-пауза, C), затем снижение
IMPULSE = [
    (1724, 1810.1, 1717, 1810), (1809.6, 1810.0, 1785.9, 1804.4), (1804.4, 1844.9, 1800.5, 1835.6),
    (1835.6, 1835.6, 1791.7, 1798.0), (1798.5, 1800.3, 1770.6, 1777.8), (1777.8, 1789.6, 1775.0, 1783.0),
    (1783.0, 1795.7, 1778.0, 1788.0), (1788.0, 1790.5, 1777.0, 1777.8), (1777.8, 1778.5, 1765.4, 1773.0)]
PIN_J = [(1773.0, 1776.0, 1760.8, 1771.4)]  # фото 3: пин-бар на покупку у нового минимума
DROP = [(1772.8, 1773.5, 1760.5, 1769.8), (1769.8, 1775.5, 1763.4, 1773.5), (1773.5, 1776.7, 1755.5, 1761.0),
        (1761.0, 1768.5, 1750.9, 1767.5), (1768.0, 1768.5, 1756.2, 1765.6), (1766.3, 1767.0, 1752.0, 1758.7),
        (1755.2, 1757.0, 1730.2, 1734.3), (1734.3, 1744.8, 1727.2, 1740.9), (1738.0, 1742.0, 1735.5, 1741.0),
        (1744.0, 1745.5, 1739.9, 1744.4)]  # последняя: пин-бар на покупку (фото 4)


def mirror(rows, k=3500.0):
    return [(k - o, k - l, k - h, k - c) for o, h, l, c in rows]


def run(rows):
    st = {"pending": [], "active": []}
    ev = S.process_15m(df_of(rows), st)
    return [e for e in ev if e.kind == "entry"], st


def test_photo2_inside_bar_buy_after_lower_low_is_not_opened():
    # внутренний бар после нового минимума ниже минимума паузы B (1785.9)
    entries, st = run(prefix() + IMPULSE[:6])
    assert entries == [] and st["pending"] == [] and st["active"] == []


def test_photo3_pin_buy_after_lower_low_is_not_opened():
    entries, st = run(prefix() + IMPULSE + PIN_J)
    assert entries == [] and st["active"] == []


def test_photo4_pin_buy_deep_in_downtrend_is_not_opened():
    entries, st = run(prefix() + IMPULSE + DROP)
    assert entries == [] and st["active"] == []


def test_mirrored_uptrend_sell_is_not_opened():
    for part in (IMPULSE[:6], IMPULSE + PIN_J, IMPULSE + DROP):
        entries, st = run(mirror(prefix() + part))
        assert entries == [] and st["active"] == [] and st["pending"] == []


def test_leg_origin_is_start_of_last_wave():
    df = df_of(prefix() + IMPULSE)
    highs, lows = swing_points(df, C.SWING_N_15M)
    hp = max(i for i, _ in highs)
    base = [x for x in lows if x[0] < hp][-1]
    assert df.high.iloc[hp] == 1844.9 and base[1] == 1717
    assert leg_origin(df, hp, base[0], base[1], True, C.LEG_DIP_MULT, C.LEG_LOOKBACK) == 1785.9
    # слишком строгий порог значимости: берётся база импульса
    assert leg_origin(df, hp, base[0], base[1], True, 100.0, C.LEG_LOOKBACK) == 1717


def test_pullback_reports_broken_structure():
    df = df_of(prefix() + IMPULSE + PIN_J)
    highs, lows = swing_points(df, C.SWING_N_15M)
    assert S._pullback(df, highs, lows, 1, len(df) - 1)[0] == "broken"


def test_normal_pullback_above_last_wave_start_still_gives_buy():
    # Чистая волна 1717→1845 одной серией свечей и откат ~50%, не ломающий начало волны: покупка по тренду остаётся
    wave = [(1717 + 16 * k, 1717 + 16 * k + 18, 1717 + 16 * k - 2, 1717 + 16 * k + 16) for k in range(8)]
    top = wave[-1][1]
    pull = [(top - 8, top - 6, top - 24, top - 22), (top - 22, top - 20, top - 38, top - 36),
            (top - 36, top - 34, top - 52, top - 50), (top - 50, top - 48, top - 62, top - 60),
            (top - 60, top - 58, top - 64, top - 59), (top - 59, top - 57, top - 62, top - 58)]
    pin = [(top - 58, top - 55.5, top - 72.0, top - 56.5)]
    entries, st = run(prefix() + wave + pull + pin)
    assert [e.text.splitlines()[0] for e in entries] == ["ETHUSDT · 15m · ПОКУПКА (по тренду)"]


# --- поглощение на фото 1 --------------------------------------------------

PHOTO1_C1 = (1722.9, 1726.5, 1720.7, 1725.2)  # бычья
PHOTO1_C2 = (1725.2, 1726.8, 1718.0, 1724.3)  # перекрыла диапазон, но тело ~10% диапазона, шип вниз ~6 при теле ~1


def test_photo1_tiny_body_candle_is_not_engulfing_it_is_a_pin():
    d = df_of([PHOTO1_C1, PHOTO1_C2])
    assert P.engulfing(d.iloc[0], d.iloc[1]) is None
    kinds = {(p.kind, p.direction) for p in P.detect(d, 1)}
    assert ("engulfing", -1) not in kinds and ("pin", 1) in kinds


def test_real_engulfing_with_solid_body_is_kept():
    d = df_of([(100, 101, 99, 99.5), (99.2, 103.5, 98.5, 103.0)])
    e = P.engulfing(d.iloc[0], d.iloc[1])
    assert e is not None and e.direction == 1
    d = df_of([(100, 101, 99, 100.5), (100.8, 101.5, 96.5, 97.0)])
    e = P.engulfing(d.iloc[0], d.iloc[1])
    assert e is not None and e.direction == -1


def test_engulfing_with_long_opposite_wick_is_rejected_even_with_decent_body():
    # тело 31% диапазона (порог пройден), но нижний шип вдвое длиннее тела и верхнего шипа: пин-бар на покупку
    d = df_of([(100, 101, 99, 100.5), (104.3, 105.0, 95.0, 101.2)])
    c2 = d.iloc[1]
    assert abs(c2.close - c2.open) >= C.ENGULF_MIN_BODY * (c2.high - c2.low)
    pin = P.pin_bar(c2)
    assert pin is not None and pin.direction == 1
    assert P.engulfing(d.iloc[0], c2) is None


def test_engulfing_doji_candle_rejected_by_body_rule_alone():
    # шипы одинаковые (пин-баром это не считается), но тело крошечное: цвет зависит от биржи
    d = df_of([(100, 101, 99, 100.5), (100.0, 103.0, 97.0, 99.8)])
    assert P.pin_bar(d.iloc[1]) is None
    assert P.engulfing(d.iloc[0], d.iloc[1]) is None
