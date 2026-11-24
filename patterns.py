from dataclasses import dataclass, field

import config as C

NAMES = {
    "pin": "Пин-бар",
    "engulfing": "Поглощение",
    "inside": "Внутренний бар",
    "ppr": "PPR",
    "fakey": "Fakey",
}
# Среди паттернов с одинаковым числом свечей
PRIORITY = {"fakey": 5, "engulfing": 4, "ppr": 3, "inside": 2, "pin": 1}


@dataclass
class Pattern:
    kind: str
    direction: int  # 1 покупка, -1 продажа, 0 внутренний бар (направление задаёт контекст)
    candles: int
    entry_type: str  # close | stop | limit
    note: str
    levels: dict = field(default_factory=dict)

    def title(self, direction: int) -> str:
        side = "на покупку" if direction > 0 else "на продажу"
        return f"{NAMES[self.kind]} {side}"


def _parts(c):
    body = abs(c.close - c.open)
    upper = c.high - max(c.open, c.close)
    lower = min(c.open, c.close) - c.low
    return body, upper, lower


def _ratio(a, b):
    return "∞" if b <= 0 else f"{a / b:.1f}"


def pin_bar(c):
    if c.high <= c.low:
        return None
    body, upper, lower = _parts(c)
    if lower > 0 and lower >= 2 * body and lower >= 2 * upper:
        note = f"нижний шип {_ratio(lower, body)}x тела и {_ratio(lower, upper)}x противоположного шипа"
        return Pattern("pin", 1, 1, "close", note, {"entry": c.close, "stop_ref": c.low})
    if upper > 0 and upper >= 2 * body and upper >= 2 * lower:
        note = f"верхний шип {_ratio(upper, body)}x тела и {_ratio(upper, lower)}x противоположного шипа"
        return Pattern("pin", -1, 1, "close", note, {"entry": c.close, "stop_ref": c.high})
    return None


def engulfing(c1, c2):
    if not (c2.high > c1.high and c2.low < c1.low):
        return None
    rng = c2.high - c2.low
    # Крошечное тело: на другой бирже эта свеча могла быть противоположного цвета, сигнал ненадёжен
    if rng <= 0 or abs(c2.close - c2.open) < C.ENGULF_MIN_BODY * rng:
        return None
    # Длинный шип против направления поглощения: это скорее пин-бар в другую сторону
    pin = pin_bar(c2)
    if pin is not None and pin.direction == (-1 if c2.close > c2.open else 1):
        return None
    if c2.close > c2.open:
        strong = c2.close > c1.open
        note = "перекрыла максимум и минимум предыдущей свечи" + ("; закрытие выше открытия предыдущей (надёжнее)" if strong else "")
        return Pattern("engulfing", 1, 2, "close", note, {"entry": c2.close, "stop_ref": c2.low})
    if c2.close < c2.open:
        strong = c2.close < c1.open
        note = "перекрыла максимум и минимум предыдущей свечи" + ("; закрытие ниже открытия предыдущей (надёжнее)" if strong else "")
        return Pattern("engulfing", -1, 2, "close", note, {"entry": c2.close, "stop_ref": c2.high})
    return None


def inside_bar(c1, c2):
    if not (c2.high <= c1.high and c2.low >= c1.low) or (c2.high == c1.high and c2.low == c1.low):
        return None
    levels = {
        "buy_entry": c2.high, "buy_stop": c1.low,
        "sell_entry": c2.low, "sell_stop": c1.high,
    }
    return Pattern("inside", 0, 2, "stop", "свеча внутри диапазона материнской", levels)


def ppr(c1, c2):
    rng = c2.high - c2.low
    if c1.close > c1.open and c2.close < c1.low:
        levels = {"entry": c1.low, "stop_ref": c2.high, "half": c2.low + rng / 2, "ppr_high": c2.high, "ppr_low": c2.low}
        return Pattern("ppr", -1, 2, "limit", "закрытие ниже минимума предыдущей бычьей свечи", levels)
    if c1.close < c1.open and c2.close > c1.high:
        levels = {"entry": c1.high, "stop_ref": c2.low, "half": c2.low + rng / 2, "ppr_high": c2.high, "ppr_low": c2.low}
        return Pattern("ppr", 1, 2, "limit", "закрытие выше максимума предыдущей медвежьей свечи", levels)
    return None


def fakey(c1, c2, c3):
    if inside_bar(c1, c2) is None:
        return None
    inside_close = c2.low <= c3.close <= c2.high
    if c3.high > c2.high and c3.low >= c2.low and inside_close:
        both = c3.high > c1.high
        note = "ложный прокол максимума внутреннего бара" + (" и материнской свечи (надёжнее)" if both else "")
        return Pattern("fakey", -1, 3, "close", note,
                       {"entry": c3.close, "stop_ref": c3.high, "target1": c2.low, "target2": c1.low})
    if c3.low < c2.low and c3.high <= c2.high and inside_close:
        both = c3.low < c1.low
        note = "ложный прокол минимума внутреннего бара" + (" и материнской свечи (надёжнее)" if both else "")
        return Pattern("fakey", 1, 3, "close", note,
                       {"entry": c3.close, "stop_ref": c3.low, "target1": c2.high, "target2": c1.high})
    return None


def detect(df, i):
    out = []
    c = df.iloc[i]
    p = pin_bar(c)
    if p:
        out.append(p)
    if i >= 1:
        prev = df.iloc[i - 1]
        for f in (engulfing, inside_bar, ppr):
            r = f(prev, c)
            if r:
                out.append(r)
    if i >= 2:
        r = fakey(df.iloc[i - 2], df.iloc[i - 1], c)
        if r:
            out.append(r)
    return out


def _key(p):
    return p.candles, PRIORITY[p.kind]


def select(patterns, direction):
    """Паттерн для нужного направления по иерархии; (None, причина) при конфликте."""
    if any(p.kind == "inside" for p in patterns):
        patterns = [p for p in patterns if p.kind != "pin"]
    same = [p for p in patterns if p.direction in (direction, 0)]
    if not same:
        return None, None
    best = max(same, key=_key)
    opposite = [p for p in patterns if p.direction == -direction]
    if opposite:
        o = max(opposite, key=_key)
        if o.candles >= best.candles:
            return None, f"конфликт паттернов: {best.title(direction)} против {o.title(-direction)}"
    return best, None


def resolve(p, direction):
    """(вход, точка для стопа) с учётом направления."""
    if p.kind == "inside":
        if direction > 0:
            return p.levels["buy_entry"], p.levels["buy_stop"]
        return p.levels["sell_entry"], p.levels["sell_stop"]
    return p.levels["entry"], p.levels["stop_ref"]
