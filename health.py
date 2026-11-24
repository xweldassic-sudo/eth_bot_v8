"""Состояние Telegram-полинга и сторож.

Бот получает команды через getUpdates (длинный запрос), а уведомления отправляет отдельными запросами.
Эти каналы ломаются независимо: уведомления могут идти, а команды нет. Здесь мы отслеживаем, когда
getUpdates последний раз отвечал успешно, и если связь потеряна надолго, перезапускаем процесс."""
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

import config as C

log = logging.getLogger("eth_bot.health")


def restart_mark_path():
    return C.DATA_DIR / "restart_reason.json"


class PollState:
    """Когда getUpdates отвечал успешно в последний раз и сколько ошибок подряд после этого."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.reset()

    def reset(self):
        self.last_ok = self.clock()
        self.errors = 0
        self.error_since = None

    def ok(self):
        """Успешный getUpdates. Возвращает (ошибок подряд, секунд перерыва), если связь только что восстановилась."""
        recovered = (self.errors, self.clock() - self.error_since) if self.errors else None
        self.last_ok = self.clock()
        self.errors = 0
        self.error_since = None
        return recovered

    def error(self):
        self.errors += 1
        if self.error_since is None:
            self.error_since = self.clock()
        return self.errors

    def silent_for(self):
        return self.clock() - self.last_ok


POLL = PollState()


def check_stall(poll=None, exit_fn=os._exit):
    """Если getUpdates молчит дольше POLL_STALL_SEC, записывает причину и завершает процесс кодом EXIT_STALL.
    True, если перезапуск запрошен."""
    poll = poll or POLL
    silent = poll.silent_for()
    if silent < C.POLL_STALL_SEC:
        return False
    reason = {"at": datetime.now(timezone.utc).isoformat(), "silent_sec": int(silent), "errors": poll.errors}
    try:
        C.DATA_DIR.mkdir(parents=True, exist_ok=True)
        restart_mark_path().write_text(json.dumps(reason), encoding="utf-8")
    except OSError:
        log.exception("не удалось записать причину перезапуска")
    log.critical("приём команд Telegram не отвечал %d с (ошибок подряд: %d), перезапускаю процесс",
                 silent, poll.errors)
    for h in logging.getLogger().handlers:
        h.flush()
    exit_fn(C.EXIT_STALL)  # os._exit: работает и при зависшем цикле событий
    return True


def start_watchdog(poll=None, sleep=time.sleep):
    """Запускает сторож в отдельном потоке (он не зависит от цикла событий asyncio)."""
    def run():
        while True:
            sleep(C.WATCHDOG_CHECK_SEC)
            try:
                check_stall(poll)
            except Exception:
                log.exception("ошибка сторожа")
    t = threading.Thread(target=run, name="poll-watchdog", daemon=True)
    t.start()
    return t


def read_restart_reason():
    """Причина последнего перезапуска сторожем (dict) или None."""
    try:
        return json.loads(restart_mark_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def clear_restart_reason():
    try:
        restart_mark_path().unlink(missing_ok=True)
    except OSError:
        pass
