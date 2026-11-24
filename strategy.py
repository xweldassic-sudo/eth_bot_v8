from dataclasses import dataclass, field
from datetime import timedelta, timezone

import pandas as pd

import config as C
from patterns import NAMES, detect, resolve, select
from swings import leg_origin, swing_points, trend_break, trend_of

MSK = timezone(timedelta(hours=3))
TF_LABEL = {"1h": "1H", "15m": "15m"}
ENTRY_HOW = {"close": "по закрытию свечи", "stop": "отложенный стоп-ордер", "limit": "лимитный ордер"}
TF_DELTA = {"1h": pd.Timedelta(hours=1), "15m": pd.Timedelta(minutes=15)}
TF_FREQ = {"1h": "1h", "15m": "15min"}
FINE_TF = "15m"  # по этим свечам ловим касания стопа, тейка и ордера


@dataclass
class Event:
    kind: str  # entry | skip | cancel | ppr_50 | ppr_cancel | exit | zone_deleted
    tf: str
    text: str
    levels: dict = field(default_factory=dict)
    mark: tuple | None = None  # (время первой свечи паттерна, время последней)
    chart: bool = True
    r: float | None = None
    key: str | None = None


def side_word(d):
    return "ПОКУПКА" if d > 0 else "ПРОДАЖА"


def on_side(d):
    return "на покупку" if d > 0 else "на продажу"


def fmt(x):
    return f"{x:.2f}"


def new_id(state):
    n = state.get("next_id", 1)
    state["next_id"] = n + 1
    return n


def tf_open(t, tf):
    """Время открытия свечи таймфрейма tf, в которую попадает момент t."""
    return t.floor(TF_FREQ[tf])


def armed_of(it, tf):
    """С какого момента цена «считается» для сделки или ордера (свечи с открытием раньше не смотрим)."""
    if it.get("armed"):
        return pd.Timestamp(it["armed"])
    created = pd.Timestamp(it["created"])
    delta = TF_DELTA[tf]
    return created + delta if created.value % delta.value == 0 else created


def _trade(state, d, entry, stop, tp, structure, tf, kind, created, armed, tid=None):
    return {"id": tid or new_id(state), "dir": d, "entry": entry, "stop": stop, "tp": tp,
            "structure": structure, "tf": tf, "kind": kind,
            "created": created.isoformat(), "armed": armed.isoformat()}


def _hit_stop(a, c):
    return (c.low <= a["stop"]) if a["dir"] > 0 else (c.high >= a["stop"])


def _hit_tp(a, c):
    return (c.high >= a["tp"]) if a["dir"] > 0 else (c.low <= a["tp"])


def _tp_r(a):
    """Реальный R:R тейка: у противоположной зоны он может быть больше C.RR."""
    risk = abs(a["entry"] - a["stop"])
    return abs(a["tp"] - a["entry"]) / risk if risk > 0 else C.RR


def candle_time(t, tf):
    return f"свеча {t.tz_convert(MSK):%d.%m %H:%M} МСК ({TF_LABEL[tf]})"


def reversal_confirmed(df, i, direction):
    """Разворот в сторону direction: контрсвеча, затем повторный тест экстремума с проколом без закрепления."""
    lo = max(0, i - C.REVERSAL_WINDOW)
    if i - lo < 3:
        return False, "мало свечей"
    o, h, l, c = df.open.values, df.high.values, df.low.values, df.close.values
    tol = C.RETEST_TOLERANCE
    if direction > 0:
        p = lo + int(l[lo:i].argmin())
        ext = l[p]
        k = next((k for k in range(p + 1, i) if c[k] > o[k]), None)
        if k is None:
            return False, "после минимума не было бычьей свечи"
        j = next((j for j in range(k + 1, i + 1) if l[j] <= ext * (1 + tol) and c[j] > ext), None)
        if j is None:
            return False, "не было повторного теста минимума"
        if any(c[m] < l[j] for m in range(j + 1, i + 1)):
            return False, "закрытие ниже минимума повторного теста"
        return True, f"бычья контрсвеча, повторный тест минимума {fmt(ext)} без закрепления ниже"
    p = lo + int(h[lo:i].argmax())
    ext = h[p]
    k = next((k for k in range(p + 1, i) if c[k] < o[k]), None)
    if k is None:
        return False, "после максимума не было медвежьей свечи"
    j = next((j for j in range(k + 1, i + 1) if h[j] >= ext * (1 - tol) and c[j] < ext), None)
    if j is None:
        return False, "не было повторного теста максимума"
    if any(c[m] > h[j] for m in range(j + 1, i + 1)):
        return False, "закрытие выше максимума повторного теста"
    return True, f"медвежья контрсвеча, повторный тест максимума {fmt(ext)} без закрепления выше"


def pressure(df, start, direction):
    """Примерная сила давления на зону (%) и скорость подхода, по видео о давлении на уровень."""
    a = max(0, start - C.PRESSURE_BARS)
    seg = df.iloc[a:start]
    if seg.empty:
        return None
    rng = (seg.high - seg.low).sum()
    toward = (seg.open - seg.close) if direction > 0 else (seg.close - seg.open)
    pct = 0.0 if rng <= 0 else max(0.0, min(100.0, 100 * toward.sum() / rng))
    lb = max(0, start - 40)
    win = df.iloc[lb:start + 1]
    if direction > 0:
        peak = lb + int(win.high.values.argmax())
        base_win = df.iloc[max(0, peak - 60):peak + 1]
        base = max(0, peak - 60) + int(base_win.low.values.argmin())
    else:
        peak = lb + int(win.low.values.argmin())
        base_win = df.iloc[max(0, peak - 60):peak + 1]
        base = max(0, peak - 60) + int(base_win.high.values.argmax())
    return round(pct), start - peak, peak - base


def pressure_text(pr):
    if pr is None:
        return "Давление на зону: нет данных"
    pct, approach, counter = pr
    return f"Давление на зону: ~{pct}% (подход за {approach} свечей, до этого встречное движение {counter} свечей)"


def qty(entry, stop):
    risk = abs(entry - stop)
    return C.RISK_USD / risk if risk > 0 else 0.0


def _levels_text(entry, stop, tp, how, stop_note, tp_note):
    rr = abs(tp - entry) / abs(entry - stop)
    return (
        f"Вход: {fmt(entry)} ({how})\n"
        f"Стоп: {fmt(stop)} ({stop_note})\n"
        f"Тейк: {fmt(tp)} ({tp_note}), R:R 1:{rr:.1f}\n"
        f"Объём: {qty(entry, stop):.3f} ETH (риск {C.RISK_USD:.0f}$)"
    )


def _mark(df, i, p):
    return df.index[i - p.candles + 1], df.index[i]


def _add_pending(state, tf, p, d, entry, stop, t, extra=None):
    item = {
        "tf": tf, "kind": p.kind, "dir": d, "entry": entry, "stop": stop,
        "trigger": "above" if (d > 0) == (p.entry_type == "stop") else "below",
        "created": t.isoformat(), "armed": (t + TF_DELTA[tf]).isoformat(), "checked_next": p.kind != "ppr",
        "id": new_id(state),
    }
    item.update(extra or {})
    state.setdefault("pending", []).append(item)


def _expired_event(it, tf, t):
    d = it["dir"]
    return Event(
        "cancel", tf,
        f"ETHUSDT · {TF_LABEL[tf]} · ОТМЕНА ВХОДА ({NAMES[it['kind']]} {on_side(d)})\n"
        f"Ордер на {fmt(it['entry'])} не сработал в течение {C.PENDING_TTL_CANDLES} свечей: срок жизни ордера истёк. Ордер снять.\n"
        f"{candle_time(t, tf)}",
        {"entry": it["entry"], "stop": it["stop"]}, (t, t))


def _filled_event(it, tf, c, t, ctf=None):
    d = it["dir"]
    touch = c.high if it["trigger"] == "above" else c.low
    mt = tf_open(t, tf)
    return Event(
        "filled", tf,
        f"ETHUSDT · {TF_LABEL[tf]} · ОРДЕР СРАБОТАЛ · {side_word(d)}\n"
        f"{NAMES[it['kind']]}: ордер {fmt(it['entry'])} исполнен (цена {fmt(touch)}).\n"
        f"Стоп {fmt(it['stop'])}, тейк {fmt(it['tp'])}.\n"
        f"{candle_time(t, ctf or tf)}",
        {"entry": it["entry"], "stop": it["stop"], "tp": it["tp"]}, (mt, mt))


def _fill(state, it, tf, c, t, ctf=None):
    """Ордер сработал на свече c (открытие t). Возвращает (события, сделка).
    Сделка None, если она закрылась на этой же свече. Порядок внутри свечи неизвестен, поэтому
    стоп считаем всегда (худший случай), а тейк только для стоп-ордеров: цена шла вперёд через вход."""
    trade = _trade(state, it["dir"], it["entry"], it["stop"], it["tp"], it["structure"], tf, it["kind"],
                   t, t + TF_DELTA[ctf or tf], it.get("id"))
    trade["seen"] = t.isoformat()
    events = [_filled_event(it, tf, c, t, ctf)]
    if _hit_stop(trade, c):
        events.append(_close_event("stop", tf, trade, -1.0, "СТОП-ЛОСС", c, t, trade["stop"], ctf=ctf))
        return events, None
    continuation = (it["trigger"] == "above") == (it["dir"] > 0)
    if continuation and _hit_tp(trade, c):
        events.append(_close_event("tp", tf, trade, _tp_r(trade), "ТЕЙК-ПРОФИТ", c, t, trade["tp"], ctf=ctf))
        return events, None
    return events, trade


def _fakey_against_now(df, d):
    return any(p.kind == "fakey" and p.direction == -d for p in detect(df, len(df) - 1))


def follow_pending(df, state, tf):
    events, keep = [], []
    i = len(df) - 1
    c, t = df.iloc[i], df.index[i]
    for it in state.get("pending", []):
        if it["tf"] != tf or pd.Timestamp(it["created"]) >= t:
            keep.append(it)
            continue
        d = it["dir"]
        age = int(round((t - pd.Timestamp(it["created"])) / TF_DELTA[tf]))
        if age > C.PENDING_TTL_CANDLES:
            events.append(_expired_event(it, tf, t))
            continue
        if not it["checked_next"]:
            it["checked_next"] = True
            opposite = (c.close > c.open) if d < 0 else (c.close < c.open)
            beyond_half = c.close > it["half"] if d < 0 else c.close < it["half"]
            back_to_entry = c.close >= it["entry"] if d < 0 else c.close <= it["entry"]
            if opposite and beyond_half:
                events.append(Event(
                    "ppr_50", tf,
                    f"ETHUSDT · {TF_LABEL[tf]} · PPR {on_side(d)} ОТМЕНЁН\n"
                    f"Следующая свеча закрепилась {'выше' if d < 0 else 'ниже'} 50% свечи PPR ({fmt(it['half'])}), закрытие {fmt(c.close)}.\n"
                    f"По правилам автора это сигнал {on_side(-d)}, первая цель: исходный стоп PPR {fmt(it['stop'])}.\n"
                    f"Лимитный ордер PPR снять; если ордер уже сработал, закрыть сделку.\n"
                    f"{candle_time(t, tf)}",
                    {"entry": it["entry"], "stop": it["stop"]}, (t, t)))
                continue
            if opposite and back_to_entry:
                events.append(Event(
                    "ppr_cancel", tf,
                    f"ETHUSDT · {TF_LABEL[tf]} · PPR {on_side(d)} ОТМЕНЁН\n"
                    f"Следующая свеча {'бычья' if d < 0 else 'медвежья'} и вернулась к точке входа {fmt(it['entry'])}, закрытие {fmt(c.close)}.\n"
                    f"Потенциал движения, скорее всего, исчерпан: снять лимитный ордер или закрыть сделку с частью убытка.\n"
                    f"{candle_time(t, tf)}",
                    {"entry": it["entry"], "stop": it["stop"]}, (t, t)))
                continue
        triggered = c.high >= it["entry"] if it["trigger"] == "above" else c.low <= it["entry"]
        if triggered and it["kind"] == "inside" and _fakey_against_now(df, d):
            events.append(Event(
                "cancel", tf,
                f"ETHUSDT · {TF_LABEL[tf]} · ОТМЕНА ВХОДА ({NAMES[it['kind']]} {on_side(d)})\n"
                f"Лимитка {fmt(it['entry'])} коснулась цены, но на этой свече сформировался фейк против направления. Вход не открываю.\n"
                f"{candle_time(t, tf)}",
                {"entry": it["entry"], "stop": it["stop"]}, (t, t)))
            continue
        if triggered:
            ev, trade = _fill(state, it, tf, c, t)
            events += ev
            if trade is not None:
                state.setdefault("active", []).append(trade)
            continue
        invalid = c.close < it["stop"] if d > 0 else c.close > it["stop"]
        if invalid:
            events.append(Event(
                "cancel", tf,
                f"ETHUSDT · {TF_LABEL[tf]} · ОТМЕНА ВХОДА ({NAMES[it['kind']]} {on_side(d)})\n"
                f"Ордер на {fmt(it['entry'])} не сработал, а свеча закрылась за стопом {fmt(it['stop'])} (закрытие {fmt(c.close)}). Ордер снять.\n"
                f"{candle_time(t, tf)}",
                {"entry": it["entry"], "stop": it["stop"]}, (t, t)))
            continue
        if age >= C.PENDING_TTL_CANDLES:
            events.append(_expired_event(it, tf, t))
            continue
        keep.append(it)
    state["pending"] = keep
    return events


def has_open_dir(state, d, tf):
    return any(x["dir"] == d and x.get("tf", "15m") == tf for x in state.get("active", []) + state.get("pending", []))


def delete_broken_zones(df, zones):
    c, t = df.iloc[-1], df.index[-1]
    events, keep = [], []
    for z in zones:
        broken = (z["type"] == "R" and c.close > z["high"]) or (z["type"] == "S" and c.close < z["low"])
        if broken:
            kind = "сопротивления" if z["type"] == "R" else "поддержки"
            where = "выше" if z["type"] == "R" else "ниже"
            events.append(Event(
                "zone_deleted", "1h",
                f"ETHUSDT · 1H · ЗОНА УДАЛЕНА\n"
                f"Зона {kind} #{z['id']} {fmt(z['low'])}–{fmt(z['high'])}: свеча закрылась {where} зоны ({fmt(c.close)}).\n"
                f"{candle_time(t, '1h')}", chart=False))
        else:
            keep.append(z)
    zones[:] = keep
    return events


def fakey_alerts(df, i, pats, zones):
    c, t = df.iloc[i], df.index[i]
    out = []
    for p in pats:
        if p.kind != "fakey":
            continue
        near = [z for z in zones if c.low <= z["high"] and c.high >= z["low"]]
        zone_txt = "".join(
            f"Зона рядом: {'поддержка' if z['type'] == 'S' else 'сопротивление'} #{z['id']} {fmt(z['low'])}–{fmt(z['high'])}\n"
            for z in near)
        entry, stop = resolve(p, p.direction)
        out.append(Event(
            "fakey", "1h",
            f"ETHUSDT · 1H · ФЕЙК {side_word(p.direction)} (только уведомление)\n{p.note}\n{zone_txt}"
            f"Вход не открываю. Ориентир: вход {fmt(entry)}, стоп {fmt(stop)}.\n{candle_time(t, '1h')}",
            {}, _mark(df, i, p)))
    return out


def block_inside_by_fakey(df, state, pats_all):
    events, keep = [], []
    t = df.index[-1]
    for it in state.get("pending", []):
        opp = [f for f in pats_all if f.kind == "fakey" and f.direction == -it["dir"]]
        if it["kind"] != "inside" or it["tf"] != "1h" or pd.Timestamp(it["created"]) >= t or not opp:
            keep.append(it)
            continue
        events.append(Event(
            "cancel", "1h",
            f"ETHUSDT · 1H · ОТМЕНА ВХОДА (внутренний бар {side_word(it['dir']).lower()})\n"
            f"Фейк {side_word(opp[0].direction).lower()} блокирует вход. Ордер {fmt(it['entry'])} снять.\n"
            f"{candle_time(t, '1h')}",
            {"entry": it["entry"], "stop": it["stop"], "tp": it["tp"]}, (t, t)))
    state["pending"] = keep
    return events


def close_inside_by_fakey(df, state, pats_all):
    events, keep = [], []
    i = len(df) - 1
    c, t = df.iloc[i], df.index[i]
    for a in state.get("active", []):
        opp = any(f.kind == "fakey" and f.direction == -a["dir"] for f in pats_all)
        if a.get("kind") != "inside" or a.get("tf") != "1h" or pd.Timestamp(a["created"]) >= t or not opp:
            keep.append(a)
            continue
        risk = abs(a["entry"] - a["stop"])
        r = (c.close - a["entry"]) * a["dir"] / risk
        events.append(_close_event("exit", "1h", a, r, "ЗАКРЫТЬ ПО ФЕЙКУ", c, t, c.close,
                                   "против сделки сформировался фейк"))
    state["active"] = keep
    return events


def process_1h(df, zones, state):
    i = len(df) - 1
    t = df.index[i]
    pats_all = detect(df, i)
    prev_pats = detect(df, i - 1) if i >= 1 else []

    def fakey_against(d):
        return any(f.kind == "fakey" and f.direction == -d for f in pats_all + prev_pats)

    events = []
    events += block_inside_by_fakey(df, state, pats_all)
    events += close_inside_by_fakey(df, state, pats_all)
    events += follow_pending(df, state, "1h") + follow_active(df, state, "1h")
    events += delete_broken_zones(df, zones)
    events += fakey_alerts(df, i, pats_all, zones)
    pats = [p for p in pats_all if p.kind != "fakey"]
    if not pats:
        return events
    used_dirs = set()
    for z in sorted(zones, key=lambda z: z["low"]):
        d = 1 if z["type"] == "S" else -1
        if d in used_dirs:
            continue
        cand = [p for p in pats if p.kind != "inside"] if fakey_against(d) else pats
        p, why = select(cand, d)
        span = max(pp.candles for pp in pats)
        seg = df.iloc[i - span + 1:i + 1]
        touched = seg.low.min() <= z["high"] if d > 0 else seg.high.max() >= z["low"]
        if not touched:
            continue
        zname = f"{'поддержка' if d > 0 else 'сопротивление'} #{z['id']} {fmt(z['low'])}–{fmt(z['high'])}"
        head = f"ETHUSDT · 1H · {side_word(d)} (разворот от зоны)"
        if p is None:
            if why:
                used_dirs.add(d)
                events.append(Event("skip", "1h", f"{head}: ВХОД ОТМЕНЁН\nЗона: {zname}\nПричина: {why}.\n{candle_time(t, '1h')}", mark=(seg.index[0], t)))
            continue
        used_dirs.add(d)
        start = i - p.candles + 1
        mark = _mark(df, i, p)
        pr = pressure(df, start, d)
        if C.CONFIRM_REVERSAL_1H:
            highs, lows = swing_points(df, C.SWING_N)
            if trend_of(highs, lows) == -d:
                ok, reason = reversal_confirmed(df, i, d)
                if not ok:
                    events.append(Event("skip", "1h", f"{head}: ВХОД ОТМЕНЁН\nЗона: {zname}\nПаттерн: {p.title(d)}\nПричина: нет подтверждения разворота ({reason}).\n{candle_time(t, '1h')}", mark=mark))
                    continue
        entry, sref = resolve(p, d)
        if d > 0:
            stop = min(z["low"], sref) * (1 - C.STOP_BUFFER_1H)
        else:
            stop = max(z["high"], sref) * (1 + C.STOP_BUFFER_1H)
        risk = abs(entry - stop)
        if risk <= 0:
            continue
        opp = [x for x in zones if x["type"] == ("R" if d > 0 else "S") and (x["low"] > entry if d > 0 else x["high"] < entry)]
        if opp:
            target_zone = min(opp, key=lambda x: x["low"]) if d > 0 else max(opp, key=lambda x: x["high"])
            tp = target_zone["low"] if d > 0 else target_zone["high"]
            tp_note = f"у зоны #{target_zone['id']}"
        else:
            tp = entry + d * C.RR * risk
            tp_note = f"1:{C.RR:.0f}, противоположной зоны нет"
        rr = (tp - entry) * d / risk
        levels = {"entry": entry, "stop": stop, "tp": tp}
        desc = f"Паттерн: {p.title(d)} ({p.note})\nЗона: {zname}"
        if has_open_dir(state, d, "1h"):
            events.append(Event(
                "skip", "1h",
                f"{head}: ВХОД ПРОПУЩЕН\n{desc}\n"
                f"Причина: уже есть открытая сделка или ожидающий ордер {'на покупку' if d > 0 else 'на продажу'}.\n"
                f"{candle_time(t, '1h')}", levels, mark))
            continue
        if rr < C.RR:
            events.append(Event(
                "skip", "1h",
                f"{head}: ВХОД ПРОПУЩЕН\n{desc}\n"
                f"Причина: до противоположной зоны R:R 1:{rr:.1f}, меньше 1:{C.RR:.0f}.\n"
                f"Вход был бы {fmt(entry)}, стоп {fmt(stop)}, зона-цель {fmt(tp)}.\n"
                f"{pressure_text(pr)}\n{candle_time(t, '1h')}", levels, mark))
            continue
        text = (
            f"{head}\n{desc}\n"
            + _levels_text(entry, stop, tp, ENTRY_HOW[p.entry_type], "за зоной/паттерном + запас", tp_note)
            + f"\n{pressure_text(pr)}\n{candle_time(t, '1h')}"
        )
        events.append(Event("entry", "1h", text, levels, mark))
        if p.entry_type == "close":
            state.setdefault("active", []).append(
                _trade(state, d, entry, stop, tp, None, "1h", p.kind, t, t + TF_DELTA["1h"]))
        else:
            extra = {"tp": tp, "structure": None}
            if p.kind == "ppr":
                extra["half"] = p.levels["half"]
            _add_pending(state, "1h", p, d, entry, stop, t, extra)
    return events


def _close_event(kind, tf, a, r, title, c, t, price, reason=None, ctf=None):
    d = a["dir"]
    usd = r * C.RISK_USD
    money = f"{'+' if usd >= 0 else '-'}{abs(usd):.2f}$"
    head = f"ETHUSDT · {TF_LABEL[tf]} · {title} · {side_word(d)}"
    if kind == "stop":
        body = f"Стоп {fmt(price)} сработал: цена дошла до {fmt(c.low if d > 0 else c.high)}."
    elif kind == "tp":
        body = f"Тейк {fmt(price)} достигнут: цена дошла до {fmt(c.high if d > 0 else c.low)}."
    else:
        body = f"Причина: {reason}. Закрытие по {fmt(price)}."
    key = f"{kind}|{tf}|{d}|{a['entry']}|{a['stop']}|{a['tp']}|{a.get('created')}"
    mt = tf_open(t, tf)
    return Event(kind, tf, f"{head}\n{body}\nВход был {fmt(a['entry'])}. Результат: {r:+.1f}R, {money}.\n{candle_time(t, ctf or tf)}",
                 {"entry": a["entry"], "stop": a["stop"], "tp": a["tp"]}, (mt, mt), r=r, key=key)


def follow_active(df, state, tf):
    events, keep = [], []
    i = len(df) - 1
    c, t = df.iloc[i], df.index[i]
    for a in state.get("active", []):
        if a.get("tf", "15m") != tf or pd.Timestamp(a["created"]) >= t:
            keep.append(a)
            continue
        d = a["dir"]
        risk = abs(a["entry"] - a["stop"])
        if (c.low <= a["stop"]) if d > 0 else (c.high >= a["stop"]):
            events.append(_close_event("stop", tf, a, -1.0, "СТОП-ЛОСС", c, t, a["stop"]))
            continue
        if (c.high >= a["tp"]) if d > 0 else (c.low <= a["tp"]):
            events.append(_close_event("tp", tf, a, _tp_r(a), "ТЕЙК-ПРОФИТ", c, t, a["tp"]))
            continue
        reason = None
        if a.get("structure") is not None and (c.close < a["structure"] if d > 0 else c.close > a["structure"]):
            reason = f"закрытие {'ниже' if d > 0 else 'выше'} последнего swing-{'минимума' if d > 0 else 'максимума'} {fmt(a['structure'])}, структура сломана"
        elif tf == "15m":
            ok, why = reversal_confirmed(df, i, -d)
            if ok:
                p, _ = select(detect(df, i), -d)
                if p is not None:
                    reason = f"подтверждён разворот: {why}, паттерн {p.title(-d)}"
        if reason:
            r = (c.close - a["entry"]) * d / risk
            events.append(_close_event("exit", tf, a, r, "ЗАКРЫТЬ ПО СМЕНЕ ТРЕНДА", c, t, c.close, reason))
            continue
        keep.append(a)
    state["active"] = keep
    return events


def _pullback(df, highs, lows, d, i):
    """Откат к тренду d после последней swing-точки.
    Возвращает (None, None), если сигнала быть не может; ("broken", None), если откат ушёл ниже начала
    последней волны (тренд сломан); ("ok", (bv, ext, pull, depth)) иначе."""
    h, l = df.high.values, df.low.values
    if d > 0:
        if not highs:
            return None, None
        hp, ext = highs[-1]
        base = [x for x in lows if x[0] < hp]
        if not base or hp >= i:
            return None, None
        bi, bv = base[-1]
        since_h, since_l = h[hp + 1:i + 1], l[hp + 1:i + 1]
        if since_h.max() > ext:
            return None, None
        pull = since_l.min()
        if pull <= leg_origin(df, hp, bi, bv, True, C.LEG_DIP_MULT, C.LEG_LOOKBACK):
            return "broken", None
        return "ok", (bv, ext, pull, (ext - pull) / (ext - bv))
    if not lows:
        return None, None
    lp, ext = lows[-1]
    base = [x for x in highs if x[0] < lp]
    if not base or lp >= i:
        return None, None
    bi, bv = base[-1]
    since_h, since_l = h[lp + 1:i + 1], l[lp + 1:i + 1]
    if since_l.min() < ext:
        return None, None
    pull = since_h.max()
    if pull >= leg_origin(df, lp, bi, bv, False, C.LEG_DIP_MULT, C.LEG_LOOKBACK):
        return "broken", None
    return "ok", (bv, ext, pull, (pull - ext) / (bv - ext))


def process_15m(df, state):
    events = follow_pending(df, state, "15m") + follow_active(df, state, "15m")
    i = len(df) - 1
    t = df.index[i]
    highs, lows = swing_points(df, C.SWING_N_15M)
    d = trend_of(highs, lows) or trend_break(df, highs, lows)
    if d == 0:
        return events
    pats = detect(df, i)
    if not pats:
        return events
    status, info = _pullback(df, highs, lows, d, i)
    if status == "broken":
        # Откат пробил начало последней волны: подтверждённые swing-точки ещё показывают старый тренд,
        # но структура уже сломана (новый минимум ниже прежнего). Работаем уже против бывшего тренда.
        d = -d
        status, info = _pullback(df, highs, lows, d, i)
    if status != "ok":
        return events
    p, _ = select(pats, d)
    if p is None:
        return events
    bv, ext, pull, depth = info
    if depth < C.PULLBACK_MIN:
        return events
    if has_open_dir(state, d, "15m"):
        return events
    entry, sref = resolve(p, d)
    if ext == pull or abs(entry - pull) / abs(ext - pull) > C.ENTRY_MAX_RETRACE:
        return events
    buf15 = C.STOP_BUFFER_1H if p.kind == "inside" else C.STOP_BUFFER_15M
    stop = sref * (1 - buf15) if d > 0 else sref * (1 + buf15)
    risk = abs(entry - stop)
    if risk <= 0:
        return events
    tp = entry + d * C.RR * risk
    trend_txt = "восходящий (HH+HL)" if d > 0 else "нисходящий (LH+LL)"
    text = (
        f"ETHUSDT · 15m · {side_word(d)} (по тренду)\n"
        f"Тренд: {trend_txt}, откат {depth * 100:.0f}% импульса {fmt(bv)}→{fmt(ext)}\n"
        f"Паттерн: {p.title(d)} ({p.note})\n"
        + _levels_text(entry, stop, tp, ENTRY_HOW[p.entry_type], f"за паттерном + {buf15 * 100:.1f}%", f"1:{C.RR:.0f}")
        + f"\nВыход раньше: при развороте пришлю сигнал закрыть.\n{candle_time(t, '15m')}"
    )
    events.append(Event("entry", "15m", text, {"entry": entry, "stop": stop, "tp": tp}, _mark(df, i, p)))
    if p.entry_type == "close":
        state.setdefault("active", []).append(
            _trade(state, d, entry, stop, tp, bv, "15m", p.kind, t, t + TF_DELTA["15m"]))
    else:
        extra = {"tp": tp, "structure": bv}
        if p.kind == "ppr":
            extra["half"] = p.levels["half"]
        _add_pending(state, "15m", p, d, entry, stop, t, extra)
    return events


def _after(fine, start, seen):
    mask = fine.index >= start
    if seen is not None:
        mask &= fine.index > seen
    return fine[mask]


def _walk_active(a, fine, events):
    """Идёт по свечам fine после последней просмотренной; True, если сделка закрылась стопом или тейком."""
    if not a.get("armed") and not a.get("created"):
        return False
    tf = a.get("tf", "15m")
    seen = pd.Timestamp(a["seen"]) if a.get("seen") else None
    for t, c in _after(fine, armed_of(a, tf), seen).iterrows():
        if _hit_stop(a, c):
            events.append(_close_event("stop", tf, a, -1.0, "СТОП-ЛОСС", c, t, a["stop"], ctf=FINE_TF))
            return True
        if _hit_tp(a, c):
            events.append(_close_event("tp", tf, a, _tp_r(a), "ТЕЙК-ПРОФИТ", c, t, a["tp"], ctf=FINE_TF))
            return True
        a["seen"] = t.isoformat()
    return False


def track_touches(fine, state):
    """Касания стопа, тейка и входа по закрытым 15m-свечам для сделок и ордеров обоих таймфреймов.
    Уведомление приходит не позже чем через 15 минут (а не при закрытии часа) и догоняет пропущенные
    свечи: у каждой сделки и ордера запоминается последняя просмотренная свеча."""
    events = []
    state["active"] = [a for a in state.get("active", []) if not _walk_active(a, fine, events)]
    keep = []
    for it in state.get("pending", []):
        tf = it["tf"]
        # PPR до проверки следующей свечи (по закрытию её таймфрейма) не трогаем
        if not it.get("checked_next", True) or not it.get("created"):
            keep.append(it)
            continue
        end = pd.Timestamp(it["created"]) + (C.PENDING_TTL_CANDLES + 1) * TF_DELTA[tf]
        seen = pd.Timestamp(it["seen"]) if it.get("seen") else None
        sub = _after(fine, armed_of(it, tf), seen)
        hit = None
        for t, c in sub[sub.index < end].iterrows():
            if (c.high >= it["entry"]) if it["trigger"] == "above" else (c.low <= it["entry"]):
                hit = (t, c)
                break
            it["seen"] = t.isoformat()
        if hit is None:
            keep.append(it)
            continue
        t, c = hit
        ev, trade = _fill(state, it, tf, c, t, ctf=FINE_TF)
        events += ev
        if trade is not None and not _walk_active(trade, fine, events):
            state["active"].append(trade)
    state["pending"] = keep
    return events


def catch_up_1h(df, zones, state):
    """Пропущенная 1H-свеча: сопровождение (ордера, сделки, зоны) без новых сигналов."""
    pats_all = detect(df, len(df) - 1)
    return (block_inside_by_fakey(df, state, pats_all) + close_inside_by_fakey(df, state, pats_all)
            + follow_pending(df, state, "1h") + follow_active(df, state, "1h") + delete_broken_zones(df, zones))


def catch_up_15m(df, state):
    """Пропущенная 15m-свеча: сопровождение без новых сигналов."""
    return follow_pending(df, state, "15m") + follow_active(df, state, "15m")
