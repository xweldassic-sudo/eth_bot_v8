def swing_points(df, n):
    """Подтверждённые swing-точки: экстремум среди n свечей слева и справа."""
    h, l = df.high.values, df.low.values
    highs, lows = [], []
    for i in range(n, len(df) - n):
        if h[i - n:i + n + 1].argmax() == n:
            highs.append((i, h[i]))
        if l[i - n:i + n + 1].argmin() == n:
            lows.append((i, l[i]))
    return highs, lows


def trend_break(df, highs, lows):
    if len(highs) < 2 or len(lows) < 2:
        return 0
    close = df.close.iloc[-1]
    if lows[-1][1] > lows[-2][1] and close > highs[-1][1]:
        return 1
    if highs[-1][1] < highs[-2][1] and close < lows[-1][1]:
        return -1
    return 0


def trend_of(highs, lows):
    if len(highs) < 2 or len(lows) < 2:
        return 0
    (_, h1), (_, h2) = highs[-2], highs[-1]
    (_, l1), (_, l2) = lows[-2], lows[-1]
    if h2 > h1 and l2 > l1:
        return 1
    if h2 < h1 and l2 < l1:
        return -1
    return 0


def leg_origin(df, ext_i, base_i, base_val, up, mult, lookback):
    """Начало последней волны к экстремуму ext_i: минимум (для роста) между двумя последними микро-вершинами.
    Откат от микро-вершины должен быть значимым (не меньше mult типичных свечей), иначе берётся база base_val.
    Если цена в откате уходит ниже этого уровня, тренд сломан раньше, чем подтвердится swing-точка."""
    h, l = df.high.values, df.low.values
    a = max(0, ext_i - lookback + 1)
    typical = float(sorted(h[a:ext_i + 1] - l[a:ext_i + 1])[(ext_i - a) // 2])
    need = mult * typical
    for k in range(ext_i - 1, max(base_i, 1) - 1, -1):
        if up:
            if not (h[k] > h[k - 1] and h[k] >= h[k + 1]) or k + 1 >= ext_i:
                continue
            level = l[k + 1:ext_i].min()
            if level > base_val and h[k] - level >= need:
                return level
        else:
            if not (l[k] < l[k - 1] and l[k] <= l[k + 1]) or k + 1 >= ext_i:
                continue
            level = h[k + 1:ext_i].max()
            if level < base_val and level - l[k] >= need:
                return level
    return base_val
