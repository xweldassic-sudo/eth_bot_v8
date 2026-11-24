import logging
import time

import pandas as pd
import requests

import config as C

log = logging.getLogger("eth_bot.bingx")
URL = "https://open-api.bingx.com/openApi/swap/v3/quote/klines"
INTERVAL_MS = {"1h": 3_600_000, "15m": 900_000}
RETRY_CODES = {100410}
RETRY_DELAYS = (3, 10, 30)


def _fetch(interval: str, limit: int) -> dict:
    for attempt, delay in enumerate((*RETRY_DELAYS, None), start=1):
        try:
            r = requests.get(URL, params={"symbol": C.SYMBOL, "interval": interval, "limit": limit}, timeout=15)
            r.raise_for_status()
            payload = r.json()
        except requests.RequestException as e:
            if delay is None:
                raise
            log.warning("%s: сетевая ошибка (попытка %d): %s, повтор через %d с", interval, attempt, e, delay)
            time.sleep(delay)
            continue
        if payload.get("code") == 0:
            if attempt > 1:
                log.info("%s: ответ получен на попытке %d", interval, attempt)
            return payload
        if payload.get("code") not in RETRY_CODES or delay is None:
            raise RuntimeError(f"BingX: {payload}")
        log.warning("%s: BingX code %s (попытка %d), повтор через %d с", interval, payload.get("code"), attempt, delay)
        time.sleep(delay)
    raise RuntimeError("BingX: исчерпаны повторные попытки")


def closed_candles(interval: str, limit: int = C.CANDLES_LIMIT) -> pd.DataFrame:
    payload = _fetch(interval, limit)
    df = pd.DataFrame(payload["data"])
    df["time"] = pd.to_datetime(df["time"].astype("int64"), unit="ms", utc=True)
    df = df.set_index("time").sort_index()[["open", "high", "low", "close", "volume"]].astype(float)
    now = pd.Timestamp.now(tz="UTC")
    return df[df.index + pd.Timedelta(milliseconds=INTERVAL_MS[interval]) <= now]
