import json
import uuid
from datetime import datetime, timedelta, timezone

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

import config as C

MSK = timezone(timedelta(hours=3))
PATH = C.DATA_DIR / "results.jsonl"
CLOSE_KINDS = {"stop", "tp", "exit"}
LIST_LIMIT = 40


def _read():
    if not PATH.exists():
        return []
    return [json.loads(line) for line in PATH.read_text(encoding="utf-8").splitlines() if line.strip()]


def record(events):
    done = {row["key"] for row in _read()}
    fresh = []
    for e in events:
        if e.kind in CLOSE_KINDS and e.key and e.key not in done:
            done.add(e.key)
            when = e.mark[1] if e.mark else datetime.now(timezone.utc)
            fresh.append({"key": e.key, "time": when.isoformat(), "tf": e.tf, "kind": e.kind,
                          "r": round(e.r, 4), "usd": round(e.r * C.RISK_USD, 2)})
    if not fresh:
        return
    C.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with PATH.open("a", encoding="utf-8") as fh:
        for row in fresh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def add_manual(side, entry, stop, tp, kind, when, tf, price=None):
    risk = abs(entry - stop)
    if kind == "stop":
        r = -1.0
    elif kind == "tp":
        r = abs(tp - entry) / risk
    else:
        r = (price - entry) * side / risk
    key = f"manual|{tf}|{side}|{entry}|{stop}|{tp}|{kind}|{price}|{when.isoformat()}"
    if key in {row["key"] for row in _read()}:
        return False
    C.DATA_DIR.mkdir(parents=True, exist_ok=True)
    row = {"key": key, "time": when.isoformat(), "tf": tf, "kind": kind,
           "r": round(r, 4), "usd": round(r * C.RISK_USD, 2)}
    with PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return True


def _label(kind):
    return {"stop": "стоп", "tp": "тейк", "exit": "закрытие"}[kind]


def report(with_chart=True):
    rows = sorted(_read(), key=lambda r: r["time"])
    if not rows:
        return "Закрытых сделок пока нет.", None
    cum, cum_usd, running = 0.0, 0.0, []
    for row in rows:
        cum += row["r"]
        cum_usd += row["usd"]
        running.append((row, cum, cum_usd))
    wins = sum(1 for r in rows if r["r"] > 0)
    losses = sum(1 for r in rows if r["r"] < 0)
    lines = [
        f"Закрытых сделок: {len(rows)} (плюс {wins}, минус {losses}, без результата {len(rows) - wins - losses})",
        f"Итог: {cum:+.2f}R, {cum_usd:+.2f}$ при риске {C.RISK_USD:.0f}$ на сделку",
        "",
        "Нарастающий итог:" + ("" if len(rows) <= LIST_LIMIT else f" (последние {LIST_LIMIT})"),
    ]
    for row, c_r, c_usd in running[-LIST_LIMIT:]:
        when = datetime.fromisoformat(row["time"]).astimezone(MSK).strftime("%d.%m %H:%M")
        lines.append(f"{when} {row['tf']} {_label(row['kind'])} {row['r']:+.2f}R → {c_r:+.2f}R ({c_usd:+.2f}$)")
    return "\n".join(lines), (_chart(running) if with_chart else None)


def _chart(running):
    xs = list(range(1, len(running) + 1))
    ys = [c_r for _, c_r, _ in running]
    C.DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = C.DATA_DIR / f"results_{uuid.uuid4().hex}.png"
    with C.PLOT_LOCK:
        fig, ax = plt.subplots(figsize=(9, 4))
        ax.plot([0] + xs, [0] + ys, marker="o", color="#1565c0")
        ax.axhline(0, color="#999", lw=0.8)
        ax.set_xlabel("сделка №")
        ax.set_ylabel("нарастающий итог, R")
        ax.grid(alpha=0.3)
        fig.savefig(path, dpi=110, bbox_inches="tight")
        plt.close(fig)
    return str(path)
