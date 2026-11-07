"""Remote configuration of LoRa FieldOps igates/digis over APRS messages.

Implements the client side of the CSR/CSU/CSW protocol in
KJ7NYE/LoRa_FieldOps_APRS_Tracker (src/remote_cfg_utils.cpp), the same one
CourseSentry uses:

    CSR                 read every field (no auth)
    CSR RO,BR           read some fields
    CSU <token>         unlock a timed write window for this sender
    CSW RO=2,BR=600     write (only while unlocked; all-or-nothing)

Commands are sent as numbered APRS messages from the operator's own callsign,
entered on the admin page with each command (never stored on the server). They
go out on a separate APRS-IS connection logged in as that callsign with its
passcode; the coverage feed's connection stays read-only. The node answers with
a message "CS ..." addressed to that callsign, which arrives on the same
connection. An igate gets commands over its own APRS-IS connection; a digi only
if an igate with IS->RF downlink has heard it directly in the last 30 minutes.

Firmware behavior this works around:
  * It answers "ERR TOOFAST" to a second command from one sender within 3 s.
  * It acks but silently ignores the same text from one sender within 60 s (a
    dedup filter meant for retries), so a repeat command would just time out.
  * It never retries its replies, so we retry our command until it's acked.
"""
import asyncio
import json
import logging
import random
import re
import time

from . import config, db
from .logbuf import APRS as aprs_log
from .logbuf import mask as logbuf_mask

log = logging.getLogger("remotecfg")

VERSION = "aprs-coverage-cfg 0.1"
TOCALL = "APZCOV"  # APZ = experimental software
TIMEOUT_S = 45  # whole command, including retries (nginx's default proxy timeout is 60 s)
RETRY_S = 12  # resend until the node acks
MAX_SENDS = 3
MIN_GAP_S = 3.5  # node's per-sender cooldown is 3 s
DEDUP_S = 62  # node's duplicate filter is 60 s
MAX_TEXT = 67  # APRS message text limit (before the {msgno})
LOG_KEEP = 500
LOGIN_TIMEOUT_S = 15
IDLE_CLOSE_S = 300  # close the sending connection after this long without a command

FIELDS = {
    "RO": {"label": "Device role", "type": "enum", "values": {"0": "Tracker", "1": "iGate", "2": "Digipeater"},
           "reboot": True},
    "TC": {"label": "Tactical callsign", "type": "string", "max_len": 9,
           "help": "Up to 9 characters. Empty to clear."},
    "SY": {"label": "Symbol", "type": "string", "min_len": 2, "max_len": 2,
           "help": "Two characters: table or overlay, then symbol code (e.g. /# or L#)."},
    "BP": {"label": "Beacon path", "type": "path",
           "help": "Path for the node's own beacons, e.g. WIDE1-1 or WIDE1-1,WIDE2-1."},
    "DM": {"label": "Digi mode", "type": "enum",
           "values": {"0": "Off", "1": "WIDE1 fill-in", "2": "WIDE1 + WIDE2 (infrastructure)"}},
    "BR": {"label": "Beacon rate (seconds)", "type": "int", "min": 1, "max": 86400},
    "GS": {"label": "GPS source", "type": "enum", "values": {"0": "Internal GPS", "1": "Fixed position", "2": "None"},
           "reboot": True},
    "LA": {"label": "Fixed latitude", "type": "float", "min": -90, "max": 90},
    "LO": {"label": "Fixed longitude", "type": "float", "min": -180, "max": 180},
    "EL": {"label": "Fixed elevation (m)", "type": "float", "min": -500, "max": 9000},
}
ALL_CODES = list(FIELDS)

ERRORS = {
    "BADTOKEN": "Wrong unlock token.",
    "LOCKED_OUT": "Too many wrong tokens. The node refuses unlocks for {detail} more seconds.",
    "DISABLED": ("Writes are disabled on the node: it has no unlock token set, or (for an igate reached "
                 "over APRS-IS) its own APRS-IS login is unverified."),
    "LOCKED": "Not unlocked from this callsign, or the write window expired. Unlock again.",
    "BADFIELD": "The node rejected field {detail}.",
    "BADVALUE": "The node rejected the value for {detail}.",
    "TOOFAST": "The node got two commands less than 3 s apart. Try again.",
}

# An amateur callsign (1-2 character prefix, a digit, 1-4 letters) with an
# optional APRS-IS SSID. The passcode depends only on the part before the '-'.
CALLSIGN_RE = re.compile(r"^[A-Z0-9]{1,2}[0-9][A-Z]{1,4}(-[A-Z0-9]{1,2})?$")
_PATH_TOKEN_RE = re.compile(r"^[A-Za-z0-9]{1,6}(-\d{1,2})?$")
_INT_RE = re.compile(r"^\d+$")
_FLOAT_RE = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)$")
# SRC>DEST,PATH::ADDRESSEE:text  (addressee is padded to 9 characters)
_MESSAGE_RE = re.compile(r"^([^>]+)>[^:]*::(.{9}):(.*)$")
# Characters that would break the message: separators of the protocol, the
# message-number marker, and the characters APRS messages may not carry.
_BAD_CHARS = set(" ,={}|~")


class CommandError(Exception):
    def __init__(self, message: str, code: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


def passcode(callsign: str) -> int:
    """The standard APRS-IS passcode: a 15-bit hash of the base callsign."""
    base = callsign.upper().split("-", 1)[0][:10]
    h = 0x73E2
    for i in range(0, len(base), 2):
        h ^= ord(base[i]) << 8
        if i + 1 < len(base):
            h ^= ord(base[i + 1])
    return h & 0x7FFF


def check_login(login: str, reserved: set[str]) -> str:
    """Validate the operator's sending callsign. `reserved` holds callsigns
    already logged in to APRS-IS by stations we care about: logging in as one
    of them would make the server drop that station's connection."""
    call = (login or "").strip().upper()
    if not CALLSIGN_RE.match(call):
        raise CommandError("Enter your callsign (e.g. K7ABC or K7ABC-CF) to send from", "BAD_LOGIN", 422)
    if call in reserved:
        raise CommandError(f"{call} is already logged in to APRS-IS by a station here; using it would knock "
                           f"that station off. Use another SSID.", "BAD_LOGIN", 422)
    return call


def split_msgno(text: str) -> tuple[str, str]:
    """'CS OK{12' -> ('CS OK', '12'). Also handles APRS 1.1 '{MM}AA'."""
    i = text.rfind("{")
    if i < 0:
        return text.rstrip(), ""
    num = text[i + 1:].split("}", 1)[0].strip()
    if not (1 <= len(num) <= 5 and num.isalnum()):
        return text.rstrip(), ""
    return text[:i].rstrip(), num


def _pairs(s: str) -> dict:
    out = {}
    for tok in s.split():
        k, eq, v = tok.partition("=")
        if eq:
            out[k] = v
    return out


def parse_reply(body: str) -> dict:
    """Parse the text of a 'CS ...' reply (message number already removed)."""
    rest = body[3:].strip() if body.startswith("CS ") else body.strip()
    if rest == "ERR" or rest.startswith("ERR "):
        parts = rest.split()
        code = parts[1] if len(parts) > 1 else ""
        detail = parts[2] if len(parts) > 2 else ""
        msg = ERRORS.get(code, "The node returned an error: {code} {detail}").format(code=code, detail=detail)
        return {"type": "ERR", "code": code, "detail": detail, "message": msg.strip()}
    if rest.startswith("UNLOCKED"):
        parts = rest.split()
        secs = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        return {"type": "UNLOCKED", "seconds": secs}
    if rest == "OK" or rest.startswith("OK "):
        fields = _pairs(rest[2:])
        reboot = fields.pop("REBOOT", None)
        return {"type": "OK", "fields": fields, "reboot": int(reboot) if reboot and reboot.isdigit() else None}
    return {"type": "READ", "fields": _pairs(rest)}


def validate_value(code: str, value) -> str:
    """Check one write against the firmware's rules. Returns the wire value."""
    spec = FIELDS.get(code)
    if spec is None:
        raise CommandError(f"Unknown field {code}", "BADFIELD", 422)
    v = str(value).strip()
    name = spec["label"]
    if any(ch in _BAD_CHARS for ch in v):
        raise CommandError(f"{name}: spaces and , = {{ }} | ~ are not allowed", "BADVALUE", 422)
    t = spec["type"]
    if t == "enum":
        if v not in spec["values"]:
            raise CommandError(f"{name}: must be one of {', '.join(spec['values'])}", "BADVALUE", 422)
    elif t == "int":
        if not _INT_RE.match(v) or not spec["min"] <= int(v) <= spec["max"]:
            raise CommandError(f"{name}: whole number {spec['min']} to {spec['max']}", "BADVALUE", 422)
    elif t == "float":
        if not _FLOAT_RE.match(v) or not spec["min"] <= float(v) <= spec["max"]:
            raise CommandError(f"{name}: number {spec['min']} to {spec['max']}", "BADVALUE", 422)
    elif t == "path":
        # The firmware separates path hops with commas, but a comma also
        # separates the fields of a write, so only a one-hop path can be sent.
        if not _PATH_TOKEN_RE.match(v):
            raise CommandError(f"{name}: one hop like WIDE1-1 (a multi-hop path can't be sent "
                               "remotely because the protocol uses commas between fields)", "BADVALUE", 422)
    elif t == "string":
        if not spec.get("min_len", 0) <= len(v) <= spec["max_len"]:
            n = spec["max_len"] if spec.get("min_len") == spec["max_len"] else f"up to {spec['max_len']}"
            raise CommandError(f"{name}: {n} characters", "BADVALUE", 422)
    return v


class AprsSender:
    """A verified APRS-IS connection as the operator's callsign, opened on
    demand and closed after IDLE_CLOSE_S without a command. Messages addressed
    to that callsign are passed to on_message(from_call, text)."""

    def __init__(self, on_message):
        self.on_message = on_message
        self.login: str | None = None
        self.server_line = ""
        self.last_used = 0.0
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task | None = None

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self, login: str) -> None:
        self.last_used = time.monotonic()
        if self.connected and self.login == login:
            return
        await self.close()
        tag = f"cfg {login}"
        aprs_log.add("sys", tag, f"connecting to {config.APRS_SERVER}:{config.APRS_PORT}")
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(config.APRS_SERVER, config.APRS_PORT), LOGIN_TIMEOUT_S)
        except (OSError, asyncio.TimeoutError) as e:
            aprs_log.add("sys", tag, f"connect failed: {e or 'timed out'}")
            raise CommandError(f"Could not connect to APRS-IS {config.APRS_SERVER}: {e or 'timed out'}",
                               "NO_TRANSPORT", 503)
        try:
            login_line = f"user {login} pass {passcode(login)} vers {VERSION} filter g/{login}"
            writer.write(f"{login_line}\r\n".encode())
            aprs_log.add("tx", tag, login_line)
            await writer.drain()
            deadline = time.monotonic() + LOGIN_TIMEOUT_S
            while True:
                raw = await asyncio.wait_for(reader.readline(), max(0.1, deadline - time.monotonic()))
                if not raw:
                    raise ConnectionError("server closed the connection")
                line = raw.decode("utf-8", errors="replace").strip()
                aprs_log.add("srv" if line.startswith("#") else "rx", tag, line)
                if "logresp" in line:
                    self.server_line = line.lstrip("# ")
                    if " verified" not in line or "unverified" in line:
                        raise CommandError(f"APRS-IS did not verify {login}: {self.server_line}",
                                           "NO_TRANSPORT", 503)
                    break
        except CommandError as e:
            writer.close()
            aprs_log.add("sys", tag, f"login refused: {e}")
            raise
        except (OSError, ConnectionError, asyncio.TimeoutError) as e:
            writer.close()
            aprs_log.add("sys", tag, f"login failed: {e or 'timed out'}")
            raise CommandError(f"APRS-IS login failed: {e or 'timed out'}", "NO_TRANSPORT", 503)
        self.login, self._reader, self._writer = login, reader, writer
        self._task = asyncio.create_task(self._listen(reader, writer))
        log.info("sending connection open as %s (%s)", login, self.server_line)

    async def _listen(self, reader, writer) -> None:
        login = self.login
        tag = f"cfg {login}"
        try:
            while True:
                # aprsc sends a keepalive about every 20 s
                raw = await asyncio.wait_for(reader.readline(), 90)
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                m = _MESSAGE_RE.match(line)
                to_us = bool(m and m.group(2).strip().upper() == login)
                aprs_log.add("srv" if line.startswith("#") else "rx", tag, line, "message to us" if to_us else "")
                if to_us:
                    try:
                        self.on_message(m.group(1).upper(), m.group(3))
                    except Exception:
                        log.exception("message handler failed: %r", line)
                if time.monotonic() - self.last_used > IDLE_CLOSE_S:
                    log.info("closing idle sending connection as %s", login)
                    break
        except (OSError, asyncio.TimeoutError) as e:
            log.info("sending connection as %s ended: %s", login, e)
        finally:
            writer.close()
            aprs_log.add("sys", tag, "disconnected")
            if self._writer is writer:
                self._writer = None

    def send(self, packet: str) -> None:
        if not self.connected:
            raise CommandError("The APRS-IS sending connection dropped", "NO_TRANSPORT", 503)
        self.last_used = time.monotonic()
        self._writer.write((packet + "\r\n").encode())
        aprs_log.add("tx", f"cfg {self.login}", packet)
        log.info("sent: %s", logbuf_mask(packet))

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
        if self._writer:
            self._writer.close()
            self._writer = None

    def status(self) -> dict:
        return {"login": self.login if self.connected else None, "server_line": self.server_line}


class _Pending:
    def __init__(self, login: str, callsign: str, future: asyncio.Future):
        self.login = login
        self.callsign = callsign
        self.msgnos: set[str] = set()
        self.acked = False
        self.future = future


class RemoteConfig:
    def __init__(self, sender=None):
        self.sender = sender or AprsSender(self.handle_message)
        self.pending: _Pending | None = None
        # The node rate-limits, dedups and unlocks per sender, so all of this
        # is keyed by (our login, node).
        self.sent: dict[tuple[str, str, str], float] = {}  # (login, node, text) -> last send
        self.last_to: dict[tuple[str, str], float] = {}  # (login, node) -> last send
        self.unlocked_until: dict[tuple[str, str], int] = {}  # (login, node) -> unix time
        self._msgno = random.randint(1, 50000)

    # ── incoming ─────────────────────────────────────────────────────────────
    def handle_message(self, from_call: str, text: str) -> None:
        """A message addressed to the sending login (from the sender's listener)."""
        body, msgno = split_msgno(text)
        p = self.pending
        low = body.lower()
        if low.startswith(("ack", "rej")) and len(body) <= 9:
            if p and from_call == p.callsign and body[3:].split("}", 1)[0] in p.msgnos:
                p.acked = True
                if low.startswith("rej") and not p.future.done():
                    p.future.set_exception(CommandError(f"{from_call} rejected the message", "REJECTED", 502))
            return
        if msgno:
            self._send_ack(from_call, msgno)
        if body.startswith("CS ") and p and from_call == p.callsign and not p.future.done():
            p.acked = True
            p.future.set_result(body)
            return
        # A late reply, a duplicate, or someone messaging the operator.
        self._log(self.sender.login or "", from_call, "(incoming)", "unsolicited", text)

    def _send_ack(self, to_call: str, msgno: str) -> None:
        try:
            self.sender.send(f"{self.sender.login}>{TOCALL},TCPIP*::{to_call:<9}:ack{msgno}")
        except Exception as e:
            log.warning("could not ack %s: %s", to_call, e)

    # ── outgoing ─────────────────────────────────────────────────────────────
    def _next_msgno(self) -> str:
        self._msgno = self._msgno % 99999 + 1
        return str(self._msgno)

    def recently_sent(self, login: str, callsign: str, text: str) -> float:
        """Seconds until the node would accept this exact text from us again (0 = now)."""
        t = self.sent.get((login, callsign, text))
        return 0 if t is None else max(0.0, DEDUP_S - (time.monotonic() - t))

    async def run(self, login: str, callsign: str, text: str) -> dict:
        if self.pending:
            raise CommandError("Another command is still waiting for a reply. Try again when it finishes.",
                               "BUSY", 409)
        wait = self.recently_sent(login, callsign, text)
        if wait:
            raise CommandError(f"The node ignores a repeat of the same command within 60 s. "
                               f"Wait {int(wait) + 1} s.", "REPEAT", 429)
        if len(text) > MAX_TEXT:
            raise CommandError("Too much for one message. Change fewer fields at a time.", "TOO_LONG", 422)

        loop = asyncio.get_running_loop()
        p = self.pending = _Pending(login, callsign, loop.create_future())
        sends = 0
        try:
            await self.sender.connect(login)
            gap = MIN_GAP_S - (time.monotonic() - self.last_to.get((login, callsign), -1e9))
            if gap > 0:
                await asyncio.sleep(gap)
            deadline = loop.time() + TIMEOUT_S
            next_send = loop.time()
            while not p.future.done():
                now = loop.time()
                if now >= deadline:
                    break
                if not p.acked and sends < MAX_SENDS and now >= next_send:
                    # A retry reuses the message number, so the node acks it
                    # again without running the command twice.
                    if not p.msgnos:
                        p.msgnos.add(self._next_msgno())
                    msgno = next(iter(p.msgnos))
                    self.sender.send(f"{login}>{TOCALL},TCPIP*::{callsign:<9}:{text}{{{msgno}")
                    sends += 1
                    self.sent[(login, callsign, text)] = self.last_to[(login, callsign)] = time.monotonic()
                    next_send = now + RETRY_S
                try:
                    await asyncio.wait_for(asyncio.shield(p.future), min(0.5, max(0.01, deadline - now)))
                except asyncio.TimeoutError:
                    pass

            if not p.future.done():
                if p.acked:
                    raise CommandError(
                        f"{callsign} acked the message but sent no reply. Remote config is probably "
                        f"turned off on the node (serial: remotecfg on), or the node saw this same "
                        f"command from {login} in the last 60 s and ignored it.", "NO_REPLY", 504)
                raise CommandError(
                    f"No ack or reply from {callsign} after {sends} tries. An igate needs its own APRS-IS "
                    f"connection; a digi needs an igate with IS-to-RF downlink that has heard it directly "
                    f"in the last 30 minutes.", "TIMEOUT", 504)
            body = p.future.result()
        except CommandError as e:
            self._log(login, callsign, text, f"error: {e.code}", str(e))
            raise
        finally:
            self.pending = None

        result = parse_reply(body)
        result["raw"] = body
        self._log(login, callsign, text, result["type"] if result["type"] != "ERR" else f"ERR {result['code']}", body)
        self._apply(login, callsign, result)
        return result

    # ── commands ─────────────────────────────────────────────────────────────
    async def read(self, login: str, callsign: str, codes: list[str] | None = None) -> dict:
        if codes:
            bad = [c for c in codes if c not in FIELDS]
            if bad:
                raise CommandError(f"Unknown field {bad[0]}", "BADFIELD", 422)
            return await self.run(login, callsign, "CSR " + ",".join(codes))
        # A full read can be spelled two ways, so a second read within the
        # node's 60 s duplicate window can still get an answer.
        texts = ["CSR", "CSR " + ",".join(ALL_CODES)]
        text = min(texts, key=lambda t: self.recently_sent(login, callsign, t))
        return await self.run(login, callsign, text)

    async def unlock(self, login: str, callsign: str, token: str) -> dict:
        token = token.strip()
        if not token or len(token) > 40 or any(ch in "{}|~" for ch in token):
            raise CommandError("Token must be 1-40 characters without { } | ~", "BADVALUE", 422)
        return await self.run(login, callsign, f"CSU {token}")

    async def write(self, login: str, callsign: str, fields: dict) -> dict:
        if not fields:
            raise CommandError("Nothing to write", "BADFIELD", 422)
        pairs = [f"{code}={validate_value(code, v)}" for code, v in fields.items()]
        return await self.run(login, callsign, "CSW " + ",".join(pairs))

    # ── state ────────────────────────────────────────────────────────────────
    def _apply(self, login: str, callsign: str, result: dict) -> None:
        now = int(time.time())
        t = result["type"]
        key = (login, callsign)
        if t == "UNLOCKED":
            self.unlocked_until[key] = now + (result["seconds"] or 0)
        elif t == "ERR" and result["code"] == "LOCKED":
            # A bad token doesn't end a window that is already open on the node.
            self.unlocked_until.pop(key, None)
        if t in ("READ", "OK") and result["fields"]:
            with db.session() as c:
                row = c.execute("SELECT fields FROM node_config WHERE callsign=?", (callsign,)).fetchone()
                fields = json.loads(row["fields"]) if row else {}
                fields.update(result["fields"])
                c.execute(
                    "INSERT INTO node_config(callsign, fields, updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(callsign) DO UPDATE SET fields=excluded.fields, updated_at=excluded.updated_at",
                    (callsign, json.dumps(fields), now),
                )
            if result.get("reboot"):
                # The node reboots after a role or GPS change; the write window goes with it.
                for k in [k for k in self.unlocked_until if k[1] == callsign]:
                    self.unlocked_until.pop(k)

    def _log(self, login: str, callsign: str, command: str, result: str, detail: str) -> None:
        if command.upper().startswith("CSU"):
            command = "CSU ****"
        with db.session() as c:
            cur = c.execute(
                "INSERT INTO node_config_log(ts, login, callsign, command, result, detail) VALUES(?,?,?,?,?,?)",
                (int(time.time()), login, callsign, command, result, detail),
            )
            if cur.lastrowid % 50 == 0:
                c.execute("DELETE FROM node_config_log WHERE id <= ?", (cur.lastrowid - LOG_KEEP,))

    def status(self, login: str | None) -> dict:
        now = int(time.time())
        return {
            "server": f"{config.APRS_SERVER}:{config.APRS_PORT}",
            "connection": self.sender.status(),
            "busy": self.pending.callsign if self.pending else None,
            "unlocked": {node: v - now for (lg, node), v in self.unlocked_until.items() if lg == login and v > now},
        }
