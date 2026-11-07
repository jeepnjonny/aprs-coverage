"""In-memory ring-buffer logs for the admin Logs tab.

Two buffers live only in memory and start empty after a restart:
  * APRS: every line to and from APRS-IS, on both the read-only feed connection
    and the remote-config sending connection.
  * APP: this app's own Python log records.

Entries are {id, ts, level, tag, text, note}; ids increase, so the page can
ask for everything after the last id it has. `epoch` changes on every restart,
which tells the page its ids are stale.
"""
import itertools
import logging
import re
import threading
import time
from collections import deque

EPOCH = int(time.time() * 1000)

# Secrets that must never reach a log: APRS-IS passcodes in login lines (the
# read-only "-1" is shown) and remote-config unlock tokens in CSU messages.
_PASS_RE = re.compile(r"(\bpass\s+)(?!-1\b)\S+", re.IGNORECASE)
_CSU_RE = re.compile(r"(:CSU\s+)[^{]*")


def mask(text: str) -> str:
    return _CSU_RE.sub(r"\1****", _PASS_RE.sub(r"\1*****", text))


class RingLog:
    def __init__(self, maxlen: int):
        self._buf: deque[dict] = deque(maxlen=maxlen)
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def add(self, level: str, tag: str, text: str, note: str = "") -> None:
        entry = {"id": 0, "ts": time.time(), "level": level, "tag": tag, "text": mask(text), "note": note}
        with self._lock:
            entry["id"] = next(self._ids)
            self._buf.append(entry)

    def since(self, after: int | None, limit: int) -> list[dict]:
        with self._lock:
            items = list(self._buf)
        if after is not None:
            items = [e for e in items if e["id"] > after]
            return items[:limit]
        return items[-limit:]


APRS = RingLog(5000)
APP = RingLog(2000)


class _Handler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = record.getMessage()
            if record.exc_info:
                text += "\n" + logging.Formatter().formatException(record.exc_info)
            APP.add(record.levelname, record.name, text)
        except Exception:
            self.handleError(record)


def install_app_handler() -> None:
    """Capture this app's log records (root logger) and uvicorn's errors."""
    h = _Handler(logging.INFO)
    for name in ("", "uvicorn.error"):
        lg = logging.getLogger(name)
        if not any(isinstance(x, _Handler) for x in lg.handlers):
            lg.addHandler(h)
