import asyncio
import logging
import logging.handlers
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from telegram import Update
from telegram.error import BadRequest, NetworkError
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, ExtBot, TypeHandler
from telegram.request import HTTPXRequest

import bingx
import chart
import health
import results
import config as C
import storage
import strategy as S

log = logging.getLogger("eth_bot")
MSK = timezone(timedelta(hours=3))
HEARTBEAT_EVERY = 60
CAPTION_LIMIT = 1024  # предел подписи к фото в Telegram
_ticks = 0
# Все изменения state.json и zones.json (и в цикле, и в командах) идут под этим замком:
# «прочитал, подождал, записал» без него затирало команды, пришедшие в паузе.
STATE_LOCK = asyncio.Lock()


def setup_logging():
    C.LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_h = logging.handlers.RotatingFileHandler(C.LOG_DIR / "bot.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    file_h.setFormatter(fmt)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers[:] = [file_h, console]
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)

    def on_crash(exc_type, exc, tb):
        log.critical("необработанное исключение, процесс завершается", exc_info=(exc_type, exc, tb))
    sys.excepthook = on_crash
TYPES = {"R": "R", "С": "R", "C": "R", "S": "S", "П": "S", "P": "S"}


def chart_title(tf):
    return f"{C.SYMBOL} · {S.TF_LABEL[tf]}"


def _rm(path):
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def allowed(update):
    """Команды принимаются только из чата CHAT_ID. Если CHAT_ID не задан, не принимаются никакие."""
    chat, msg = update.effective_chat, getattr(update, "message", None)
    return bool(C.CHAT_ID) and chat is not None and msg is not None and chat.id == C.CHAT_ID


def _mark_to_json(mark):
    return [pd.Timestamp(x).isoformat() for x in mark] if mark else None


def _mark_from_json(mark):
    return tuple(pd.Timestamp(x) for x in mark) if mark else None


async def _render_chart(ev):
    path = C.DATA_DIR / f"chart_{uuid.uuid4().hex}.png"
    try:
        zdata = storage.load_zones()
        df = await asyncio.to_thread(bingx.closed_candles, ev.tf)
        await asyncio.to_thread(chart.render, df, zdata["zones"], str(path),
                                chart_title(ev.tf), ev.levels, ev.mark)
        return path
    except Exception:
        log.exception("не удалось построить график, отправляю текст без него")
        _rm(path)
        return None


async def _send(ctx, ev):
    """Отправка с повторами. True, если доставлено; при неудаче уведомление нужно сохранить в очередь."""
    png = await _render_chart(ev) if ev.chart else None
    long_text = len(ev.text) > CAPTION_LIMIT
    photo_sent = False
    try:
        for attempt, delay in enumerate((*C.SEND_RETRY_DELAYS, None), start=1):
            try:
                if png and not photo_sent:
                    with open(png, "rb") as fh:
                        await ctx.bot.send_photo(chat_id=C.CHAT_ID, photo=fh,
                                                 caption=ev.text.split("\n")[0] if long_text else ev.text)
                    photo_sent = True
                if not png or long_text:
                    await ctx.bot.send_message(chat_id=C.CHAT_ID, text=ev.text)
                return True
            except Exception:
                if delay is None:
                    log.exception("не удалось отправить уведомление (попыток: %d)", attempt)
                    return False
                log.warning("отправка не удалась (попытка %d), повтор через %d с", attempt, delay, exc_info=True)
                await asyncio.sleep(delay)
    finally:
        if png:
            _rm(png)
    return False


async def _enqueue(ev, why="retry"):
    """Кладёт уведомление в постоянную очередь (Telegram не принял), flush_queue досылает его позже."""
    async with STATE_LOCK:
        st = storage.load_state()
        st["queue"].append({"qid": uuid.uuid4().hex, "why": why, "attempts": 0, "kind": ev.kind,
                            "text": ev.text, "levels": ev.levels, "mark": _mark_to_json(ev.mark),
                            "chart": ev.chart, "tf": ev.tf})
        storage.save_state(st)


async def deliver(ctx, ev):
    """Отправляет уведомление в любое время суток. Если Telegram не принял, оно остаётся в очереди."""
    head = ev.text.split("\n")[0]
    ts = datetime.now(MSK).strftime("%H:%M")
    log.info("%s | %s", ts, head)
    ok = await _send(ctx, ev)
    if not ok:
        await _enqueue(ev)
        log.error("уведомление сохранено в очередь, повторю позже: %s", head)
    return ok


async def flush_queue(ctx):
    """Досылает очередь недоставленных. Запись удаляется только после успешной отправки."""
    async with STATE_LOCK:
        st = storage.load_state()
        if not st["queue"]:
            return
        for it in st["queue"]:
            it.setdefault("qid", uuid.uuid4().hex)
        storage.save_state(st)
        items = list(st["queue"])
    for it in items:
        ev = S.Event(it.get("kind", "queue"), it["tf"], it["text"], it["levels"],
                     _mark_from_json(it["mark"]), it["chart"])
        ok = await _send(ctx, ev)
        async with STATE_LOCK:
            st = storage.load_state()
            if ok:
                st["queue"] = [q for q in st["queue"] if q.get("qid") != it["qid"]]
            else:
                for q in st["queue"]:
                    if q.get("qid") == it["qid"]:
                        q["attempts"] = q.get("attempts", 0) + 1
                if any(q.get("attempts", 0) >= C.QUEUE_MAX_ATTEMPTS for q in st["queue"]):
                    log.error("уведомление не доставлено за %d попыток, удаляю: %s", C.QUEUE_MAX_ATTEMPTS, it["text"].split("\n")[0])
                    st["queue"] = [q for q in st["queue"] if q.get("attempts", 0) < C.QUEUE_MAX_ATTEMPTS]
            storage.save_state(st)
        if not ok:
            return  # Telegram недоступен: остальное на следующем тике


def _process(tf, df, zdata, st, missed):
    """Синхронная часть проверки (идёт в отдельном потоке)."""
    events = []
    zones = zdata["zones"] if zdata else None
    if tf == "15m":
        events += S.track_touches(df, st)
    for k in missed:  # пропущенные свечи: только сопровождение, без новых сигналов
        sub = df.iloc[:k + 1]
        events += S.catch_up_1h(sub, zones, st) if tf == "1h" else S.catch_up_15m(sub, st)
    if tf == "1h":
        events += S.process_1h(df, zones, st)
    else:
        events += S.process_15m(df, st)
    return events


def _due(tf, last_iso, now=None):
    """Пора ли запрашивать свечи у биржи: следующая свеча после последней обработанной уже должна закрыться.
    Раньше бот спрашивал биржу каждые 30 с; из-за этого BingX отвечал лимитом запросов (code 100410)."""
    if last_iso is None:
        return True
    now = now if now is not None else pd.Timestamp.now(tz="UTC")
    delta = S.TF_DELTA[tf]
    return now >= pd.Timestamp(last_iso) + 2 * delta + pd.Timedelta(seconds=C.CANDLE_FETCH_LAG_SEC)


async def check_timeframe(ctx, tf):
    if not _due(tf, storage.load_state()["last"].get(tf)):
        return
    df = await asyncio.to_thread(bingx.closed_candles, tf)  # сеть: вне замка
    if df.empty:
        return
    t = df.index[-1]
    async with STATE_LOCK:
        st = storage.load_state()
        last = st["last"].get(tf)
        if last == t.isoformat():
            return  # биржа ещё не отдала новую свечу: повторим на следующем тике
        if last is None:
            st["last"][tf] = t.isoformat()
            storage.save_state(st)
            log.info("первый запуск, %s: %s, сигналы не отправляем", tf, t)
            return
        last_ts = pd.Timestamp(last)
        missed = [k for k in range(len(df) - 1) if df.index[k] > last_ts]
        if missed:
            log.warning("%s: пропущено закрытых свечей: %d, догоняю без новых сигналов", tf, len(missed))
        zdata = storage.load_zones() if tf == "1h" else None
        events = await asyncio.to_thread(_process, tf, df, zdata, st, missed)
        if zdata is not None:
            storage.save_zones(zdata)
        results.record(events)
        st["last"][tf] = t.isoformat()
        storage.save_state(st)
    log.info("%s: новая закрытая свеча %s МСК, событий: %d", tf, t.tz_convert(MSK).strftime("%d.%m %H:%M"), len(events))
    for ev in events:
        await deliver(ctx, ev)


async def announce_restart(ctx):
    """После перезапуска сторожем сообщает об этом в чат (если Telegram не принял, уйдёт через очередь)."""
    info = health.read_restart_reason()
    if info is None:
        return
    try:
        at = pd.Timestamp(info["at"]).tz_convert(MSK).strftime("%d.%m %H:%M")
        mins = max(1, round(info.get("silent_sec", 0) / 60))
    except Exception:
        at, mins = "?", "?"
    text = (f"Бот перезапущен: приём команд Telegram не отвечал около {mins} мин (перезапуск {at} МСК).\n"
            "Команды, отправленные в это время, не выполнены: повторите их. Уведомления при этом могли приходить.")
    await deliver(ctx, S.Event("restart", "1h", text, chart=False))
    health.clear_restart_reason()


async def _safe(name, fn, *args):
    try:
        await fn(*args)
    except Exception:
        log.exception("ошибка в %s, следующая попытка через %d с", name, C.CHECK_INTERVAL_SEC)


async def loop(ctx):
    global _ticks
    _ticks += 1
    if _ticks % HEARTBEAT_EVERY == 0:
        log.info("бот работает, проверок с запуска: %d; приём команд: ответ %d с назад, ошибок подряд: %d",
                 _ticks, health.POLL.silent_for(), health.POLL.errors)
    await _safe("отправке очереди", flush_queue, ctx)
    await _safe("уведомлении о перезапуске", announce_restart, ctx)
    # 15m раньше 1h: касания по 15m-свечам должны обработаться до правил закрытия часа
    for tf in ("15m", "1h"):
        await _safe(f"проверке {tf}", check_timeframe, ctx, tf)


def parse_zone(args):
    if len(args) != 3 or args[0].upper() not in TYPES:
        return None, "Формат: /zone R 2300 2320 или /zone S 2100 2120 (R сопротивление, S поддержка)"
    try:
        a, b = float(args[1].replace(",", ".")), float(args[2].replace(",", "."))
    except ValueError:
        return None, "Границы должны быть числами"
    low, high = sorted((a, b))
    if low <= 0:
        return None, "Границы должны быть больше нуля"
    return (TYPES[args[0].upper()], low, high), None


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message is None or update.effective_chat is None:
        return
    if not C.CHAT_ID:
        await update.message.reply_text(
            f"ID этого чата: {update.effective_chat.id}\n"
            "Впишите его в CHAT_ID (файл .env или переменные окружения хостинга) и перезапустите бота. "
            "Пока CHAT_ID не задан, остальные команды выключены.")
        return
    if not allowed(update):
        return
    await update.message.reply_text(
        f"ID этого чата: {update.effective_chat.id}\n\n"
        "Команды:\n"
        "/zone R 2300 2320 — добавить сопротивление\n"
        "/zone S 2100 2120 — добавить поддержку\n"
        "/zones — список зон\n"
        "/del 3 — удалить зону #3\n"
        "/chart — свежий график с зонами\n"
        "/addtrade L 2400 2380 2440 1h — записать сделку вручную (L лонг, S шорт)\n"
        "/trades — открытые сделки и ожидающие ордера\n"
        "/deltrade 3 — убрать сделку из отслеживания\n"
        "/status — настройки и последние проверки\n"
        "/results — итог по закрытым сделкам с нарастающим итогом\n"
        "/addresult L 2400 2380 2440 tp 19.11 14:30 — записать уже закрытую сделку в итоги")


async def cmd_zone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    parsed, err = parse_zone(ctx.args or [])
    if err:
        await update.message.reply_text(err)
        return
    ztype, low, high = parsed
    async with STATE_LOCK:
        data = storage.load_zones()
        z = storage.add_zone(data, ztype, low, high)
        storage.save_zones(data)
    kind = "сопротивление" if ztype == "R" else "поддержка"
    await update.message.reply_text(f"Зона #{z['id']} сохранена: {kind} {low:.2f}–{high:.2f}")


async def cmd_zones(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    zones = storage.load_zones()["zones"]
    if not zones:
        await update.message.reply_text("Список зон пуст")
        return
    lines = [f"#{z['id']} {'R' if z['type'] == 'R' else 'S'} {z['low']:.2f}–{z['high']:.2f}" for z in zones]
    await update.message.reply_text("Зоны:\n" + "\n".join(lines))


async def cmd_del(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    try:
        zid = int((ctx.args or [""])[0])
    except ValueError:
        await update.message.reply_text("Формат: /del 3")
        return
    async with STATE_LOCK:
        data = storage.load_zones()
        before = len(data["zones"])
        data["zones"] = [z for z in data["zones"] if z["id"] != zid]
        storage.save_zones(data)
    await update.message.reply_text("Зона удалена" if len(data["zones"]) < before else "Зона не найдена")


async def cmd_chart(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    tf = (ctx.args or ["1h"])[0].lower()
    tf = {"1ч": "1h", "15м": "15m", "15min": "15m"}.get(tf, tf)
    if tf not in S.TF_LABEL:
        await update.message.reply_text("Формат: /chart 1h или /chart 15m")
        return
    path = C.DATA_DIR / f"chart_{uuid.uuid4().hex}.png"
    try:
        df = await asyncio.to_thread(bingx.closed_candles, tf)
        await asyncio.to_thread(chart.render, df, storage.load_zones()["zones"], str(path), chart_title(tf))
        with open(path, "rb") as fh:
            await update.message.reply_photo(fh, caption=f"{chart_title(tf)}, закрытие {df.iloc[-1].close:.2f}")
    except Exception:
        log.exception("ошибка /chart")
        await update.message.reply_text("Не удалось построить график (биржа или Telegram не ответили). Попробуйте позже.")
    finally:
        _rm(path)


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    st = storage.load_state()
    z = storage.load_zones()["zones"]
    pend = len([p for p in st["pending"] if p["tf"] == "1h"])
    await update.message.reply_text(
        f"Инструмент: {C.SYMBOL}\nРиск: {C.RISK_USD:.0f}$, R:R 1:{C.RR:.0f}\n"
        f"Уведомления: круглосуточно\n"
        f"Приём команд Telegram: последний ответ {health.POLL.silent_for():.0f} с назад, ошибок подряд {health.POLL.errors}\n"
        f"Подтверждение разворота на 1H: {'вкл' if C.CONFIRM_REVERSAL_1H else 'выкл'}\n"
        f"Зон: {len(z)}, ожидающих ордеров 1H: {pend}, активных 15m: {len(st['active'])}, в очереди: {len(st['queue'])}\n"
        f"Последние свечи: 1H {st['last'].get('1h', '—')}, 15m {st['last'].get('15m', '—')}")


async def cmd_results(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    text, png = await asyncio.to_thread(results.report)
    try:
        await update.message.reply_text(text)
        if png:
            with open(png, "rb") as fh:
                await update.message.reply_photo(fh, caption="Нарастающий итог, R")
    finally:
        if png:
            _rm(png)


RESULT_KINDS = {"stop", "tp", "exit"}


def _parse_when(date_s, time_s):
    parts = date_s.split(".")
    if len(parts) not in (2, 3):
        return None, "Дата: ДД.ММ или ДД.ММ.ГГГГ"
    try:
        day, month = int(parts[0]), int(parts[1])
        year = int(parts[2]) if len(parts) == 3 else datetime.now(MSK).year
        hh, mm = (int(x) for x in time_s.split(":"))
        when = datetime(year, month, day, hh, mm, tzinfo=MSK)
    except ValueError:
        return None, "Дата или время не распознаны. Пример: 19.11 14:30 (время МСК)"
    if when > datetime.now(MSK):
        return None, "Дата в будущем. Укажите уже закрытую сделку"
    return when.astimezone(timezone.utc), None


def parse_result(args):
    usage = ("Формат: /addresult L 2400 2380 2440 tp 19.11 14:30 "
             "или /addresult L 2400 2380 2440 exit 2415 19.11 14:30 15m "
             "(результат stop, tp или exit с ценой закрытия; таймфрейм необязательно, по умолчанию 1h)")
    if len(args) < 6:
        return None, usage
    side = TRADE_SIDES.get(args[0].upper())
    if side is None:
        return None, "Направление: L (лонг) или S (шорт)"
    try:
        entry, stop, tp = (float(x.replace(",", ".")) for x in args[1:4])
    except ValueError:
        return None, "Цены должны быть числами"
    if not ((side > 0 and stop < entry < tp) or (side < 0 and tp < entry < stop)):
        return None, "Для лонга стоп ниже входа, тейк выше; для шорта наоборот"
    kind = args[4].lower()
    if kind not in RESULT_KINDS:
        return None, "Результат: stop, tp или exit"
    i, price = 5, None
    if kind == "exit":
        try:
            price = float(args[5].replace(",", "."))
        except (ValueError, IndexError):
            return None, "Для exit укажите цену закрытия: /addresult L 2400 2380 2440 exit 2415 19.11 14:30"
        i = 6
    if len(args) < i + 2 or len(args) > i + 3:
        return None, usage
    when, err = _parse_when(args[i], args[i + 1])
    if err:
        return None, err
    tf = args[i + 2].lower() if len(args) > i + 2 else "1h"
    if tf not in TRADE_TFS:
        return None, "Таймфрейм: 1h или 15m"
    return {"side": side, "entry": entry, "stop": stop, "tp": tp, "kind": kind,
            "when": when, "tf": tf, "price": price}, None


async def cmd_addresult(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    res, err = parse_result(ctx.args or [])
    if err:
        await update.message.reply_text(err)
        return
    if not results.add_manual(**res):
        await update.message.reply_text("Такая сделка уже записана в итогах.")
        return
    text, _ = results.report(with_chart=False)
    await update.message.reply_text("Записано в итоги.\n" + "\n".join(text.split("\n")[:2]))


async def cmd_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message is None:
        return
    await update.message.reply_text(f"ID этого чата: {update.effective_chat.id}")


TRADE_SIDES = {"L": 1, "Л": 1, "S": -1, "Ш": -1}
TRADE_TFS = ("1h", "15m")


def _assign_ids(st):
    st.setdefault("next_id", 1)
    for key in ("active", "pending"):
        for it in st.get(key, []):
            if "id" not in it:
                it["id"] = st["next_id"]
                st["next_id"] += 1


def _line(it, label):
    side = "LONG" if it["dir"] > 0 else "SHORT"
    return (f"#{it['id']} {label} {side} {it.get('tf', '15m')}: "
            f"вход {it['entry']:.2f}, стоп {it['stop']:.2f}, тейк {it['tp']:.2f}")


def parse_trade(args):
    if len(args) not in (4, 5):
        return None, "Формат: /addtrade L 2400 2380 2440 1h (L лонг, S шорт; таймфрейм 1h или 15m, по умолчанию 1h)"
    side = TRADE_SIDES.get(args[0].upper())
    if side is None:
        return None, "Направление: L (лонг) или S (шорт)"
    tf = args[4].lower() if len(args) == 5 else "1h"
    if tf not in TRADE_TFS:
        return None, "Таймфрейм: 1h или 15m"
    try:
        entry, stop, tp = (float(x.replace(",", ".")) for x in args[1:4])
    except ValueError:
        return None, "Цены должны быть числами"
    if not ((side > 0 and stop < entry < tp) or (side < 0 and tp < entry < stop)):
        return None, "Для лонга стоп ниже входа, тейк выше; для шорта наоборот"
    return {"dir": side, "entry": entry, "stop": stop, "tp": tp, "structure": None, "tf": tf}, None


async def cmd_addtrade(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    trade, err = parse_trade(ctx.args or [])
    if err:
        await update.message.reply_text(err)
        return
    async with STATE_LOCK:
        st = storage.load_state()
        _assign_ids(st)
        busy = S.has_open_dir(st, trade["dir"], trade["tf"])
        if not busy:
            now = datetime.now(timezone.utc).isoformat()
            trade |= {"id": st["next_id"], "created": now, "armed": now}
            st["next_id"] += 1
            st["active"].append(trade)
            storage.save_state(st)
    if busy:
        await update.message.reply_text(f"Уже есть открытая сделка или ожидающий ордер в эту сторону на {trade['tf']}. Сначала удалите её: /trades, /deltrade N")
        return
    await update.message.reply_text(f"Сделка добавлена, {_line(trade, 'сделка')}\nБот будет присылать стоп, тейк и закрытие по ней.")


async def cmd_trades(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    async with STATE_LOCK:
        st = storage.load_state()
        _assign_ids(st)
        storage.save_state(st)
    lines = [_line(a, "сделка") for a in st["active"]] + [_line(p, "ордер") for p in st["pending"]]
    if not lines:
        await update.message.reply_text("Открытых сделок и ожидающих ордеров нет.")
        return
    await update.message.reply_text("\n".join(lines) + "\nУдалить из отслеживания: /deltrade N")


async def cmd_deltrade(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    try:
        tid = int((ctx.args or [""])[0])
    except ValueError:
        await update.message.reply_text("Формат: /deltrade 3")
        return
    async with STATE_LOCK:
        st = storage.load_state()
        _assign_ids(st)
        before = len(st["active"]) + len(st["pending"])
        st["active"] = [a for a in st["active"] if a["id"] != tid]
        st["pending"] = [p for p in st["pending"] if p["id"] != tid]
        found = len(st["active"]) + len(st["pending"]) != before
        if found:
            storage.save_state(st)
    if not found:
        await update.message.reply_text("Сделка или ордер с таким номером не найден (список: /trades)")
        return
    await update.message.reply_text(f"#{tid} удалена из отслеживания.")


def _short(err):
    return f"{type(err).__name__}: {err}".replace("\n", " ")[:160]


async def on_error(update, ctx):
    err = ctx.error
    if isinstance(err, NetworkError) and not isinstance(err, BadRequest):
        # Сеть/прокси: PTB повторит запрос сам, полный трейсбек в логе не нужен
        if update is None:
            n = health.POLL.error()
            log.warning("Telegram: не удалось получить команды (%s), ошибок подряд: %d, повтор автоматически", _short(err), n)
        else:
            text = getattr(getattr(update, "message", None), "text", "") or "?"
            log.warning("Telegram: ответ на команду %r не отправлен (%s)", text[:60], _short(err))
        return
    log.error("ошибка обработчика команды", exc_info=err)


async def log_update(update, ctx):
    """Пишет в лог каждую входящую команду: так видно, дошла ли она до бота и приняла ли её проверка чата."""
    msg = getattr(update, "message", None)
    text = getattr(msg, "text", None)
    if not text or not text.startswith("/"):
        return
    chat = update.effective_chat
    if not C.CHAT_ID:
        verdict = "режим настройки"
    else:
        verdict = "принята" if allowed(update) else "отклонена: чужой чат"
    log.info("команда получена: %s | чат %s | %s", text[:80], chat.id if chat else "?", verdict)


class TrackedBot(ExtBot):
    """Бот, который запоминает каждый успешный getUpdates: по этому ведётся сторож полинга."""

    async def get_updates(self, *args, **kwargs):
        updates = await super().get_updates(*args, **kwargs)
        recovered = health.POLL.ok()
        if recovered:
            log.info("Telegram: приём команд восстановлен (ошибок подряд было %d, перерыв %d с)", *recovered)
        return updates


def _request(pool_size, read_timeout):
    kw = dict(connection_pool_size=pool_size, connect_timeout=C.TG_CONNECT_TIMEOUT, read_timeout=read_timeout,
              write_timeout=C.TG_WRITE_TIMEOUT, pool_timeout=C.TG_POOL_TIMEOUT)
    if C.TELEGRAM_PROXY:
        kw["proxy"] = C.TELEGRAM_PROXY
    return HTTPXRequest(**kw)


def build_app():
    """Приложение с увеличенными таймаутами, раздельными пулами (обычные запросы / getUpdates) и параллельной
    обработкой команд. Возвращает (приложение, ведётся ли учёт getUpdates для сторожа)."""
    try:
        bot = TrackedBot(C.TELEGRAM_TOKEN, request=_request(16, C.TG_READ_TIMEOUT),
                         get_updates_request=_request(1, C.TG_POLL_READ_TIMEOUT))
        return ApplicationBuilder().bot(bot).concurrent_updates(True).build(), True
    except Exception:
        log.exception("не удалось применить настройки соединения, запускаюсь со стандартными (сторож полинга выключен)")
        return ApplicationBuilder().token(C.TELEGRAM_TOKEN).concurrent_updates(True).build(), False


def _cleanup_temp():
    for pattern in ("chart_*.png", "results_*.png"):
        for f in C.DATA_DIR.glob(pattern):
            _rm(f)


def main():
    setup_logging()
    log.info("запуск бота, PID %d, лог: %s", os.getpid(), C.LOG_DIR / "bot.log")
    if not C.TELEGRAM_TOKEN:
        log.error("TELEGRAM_TOKEN не заполнен в %s", C.BASE_DIR / ".env")
        print("Заполните TELEGRAM_TOKEN в файле .env", file=sys.stderr)
        raise SystemExit(C.EXIT_CONFIG)
    C.DATA_DIR.mkdir(parents=True, exist_ok=True)
    _cleanup_temp()
    app, tracked = build_app()
    app.add_error_handler(on_error)
    app.add_handler(TypeHandler(Update, log_update, block=False), group=-1)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    if not C.CHAT_ID:
        log.warning("CHAT_ID не задан: режим настройки. Работают только /start и /id, сигналов нет. "
                    "Отправьте боту /start, впишите ID в CHAT_ID и перезапустите бота.")
    else:
        app.add_handler(CommandHandler("zone", cmd_zone))
        app.add_handler(CommandHandler("zones", cmd_zones))
        app.add_handler(CommandHandler("del", cmd_del))
        app.add_handler(CommandHandler("chart", cmd_chart))
        app.add_handler(CommandHandler("status", cmd_status))
        app.add_handler(CommandHandler("results", cmd_results))
        app.add_handler(CommandHandler("addresult", cmd_addresult))
        app.add_handler(CommandHandler("addtrade", cmd_addtrade))
        app.add_handler(CommandHandler("trades", cmd_trades))
        app.add_handler(CommandHandler("deltrade", cmd_deltrade))
        app.job_queue.run_repeating(loop, interval=C.CHECK_INTERVAL_SEC, first=5)
        log.info("бот запущен, %s, опрос очереди и свечей каждые %d с", C.SYMBOL, C.CHECK_INTERVAL_SEC)
    health.POLL.reset()
    if tracked:
        health.start_watchdog()
    try:
        # Старые команды, накопившиеся за время простоя, не выполняем; бесконечные повторы при старте
        # нужны, чтобы бот дождался Telegram, а не упал, если прокси ещё не поднялся.
        app.run_polling(drop_pending_updates=True, bootstrap_retries=-1, allowed_updates=["message"])
    finally:
        log.warning("бот остановлен")


if __name__ == "__main__":
    if "--worker" in sys.argv:
        try:
            main()
        except KeyboardInterrupt:
            pass
    else:
        # Обычный запуск `python bot.py`: супервизор держит бота живым и перезапускает его после сбоев
        import supervisor
        sys.exit(supervisor.run([sys.executable, os.path.abspath(__file__), "--worker"], cwd=str(C.BASE_DIR)))
