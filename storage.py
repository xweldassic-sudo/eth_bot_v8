import json
from datetime import datetime, timezone

import config as C

ZONES_FILE = C.DATA_DIR / "zones.json"
STATE_FILE = C.DATA_DIR / "state.json"


def _read(path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _json_default(o):
    if hasattr(o, "isoformat"):  # pd.Timestamp, datetime
        return o.isoformat()
    if hasattr(o, "item"):  # numpy-числа
        return o.item()
    raise TypeError(f"не сериализуется в JSON: {type(o).__name__}")


def _write(path, data):
    C.DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    tmp.replace(path)


def load_zones():
    return _read(ZONES_FILE, {"next_id": 1, "zones": []})


def save_zones(data):
    _write(ZONES_FILE, data)


def add_zone(data, ztype, low, high):
    z = {"id": data["next_id"], "type": ztype, "low": low, "high": high,
         "created": datetime.now(timezone.utc).isoformat()}
    data["zones"].append(z)
    data["next_id"] += 1
    return z


def load_state():
    st = _read(STATE_FILE, {})
    for k, v in (("last", {}), ("pending", []), ("active", []), ("queue", [])):
        st.setdefault(k, v)
    return st


def save_state(st):
    _write(STATE_FILE, st)
