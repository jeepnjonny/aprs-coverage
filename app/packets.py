"""Pure packet logic: parse, credit the first RF hop, run the quality checks.

No I/O lives here, so all of it can be unit-tested.
"""
import fnmatch
import re
from dataclasses import dataclass, field

import aprslib

from . import config
from .grid import approx_km

RF_QCONSTRUCTS = {"qAR", "qAr", "qAO", "qAo"}
QCONSTRUCT_RE = re.compile(r"^qA[A-Za-z]$")
POSITION_FORMATS = {"uncompressed", "compressed", "mic-e"}

# Generic path aliases that are not station callsigns.
_ALIAS_RE = re.compile(
    r"^(WIDE|RELAY|TRACE|TEMP|GATE|ECHO|HOP|RFONLY|NOGATE|BEACON|TCPIP|TCPXX)\d?(-\d{1,2})?$"
)


# Area and special-purpose aliases that never identify one station. Digis named
# with plain words (RBERRY, GIRARD) are real stations, so the rule "no digit means
# alias" is not used.
_ALIAS_WORDS = {
    "SAR", "ARISS", "SPACE", "SGATE", "LOCAL", "DIRECT", "IDAHO", "OREGON", "UTAH",
    "NEVADA", "WASH", "MONT", "WYO", "CALIF", "NORCAL", "SOCAL", "WIDE", "RELAY",
    "TRACE", "NOGATE", "RFONLY", "TCPIP", "TCPXX", "CQ", "QST", "APRS",
}


def is_alias(hop: str) -> bool:
    """True for generic path aliases (WIDEn-N, TRACEn-N, state codes like ID-1)
    that don't name the station that repeated the packet."""
    h = hop.upper()
    if _ALIAS_RE.match(h):
        return True
    base = h.split("-", 1)[0]
    if base in _ALIAS_WORDS:
        return True
    # Two-letter state or section codes (ID, OR, WA, ID-1)
    return len(base) <= 2 and base.isalpha()


@dataclass
class Route:
    qconstruct: str
    igate: str
    heard_by: str | None  # station that heard the mobile directly; None = untraced
    hops: int  # number of used digipeater hops
    digis: list[str]  # identified digipeaters, in order


def route_from_path(path: list[str]) -> Route | None:
    """Work out the RF route from an APRS-IS path list.

    The path holds the digi hops, then the q-construct, then the igate. The '*'
    marks the last used hop, so every hop up to and including it was used.
    Digipeaters that trace insert their own callsign, which is how we know who
    heard the mobile.
    """
    qi = next((i for i, h in enumerate(path) if QCONSTRUCT_RE.match(h)), None)
    if qi is None or qi + 1 >= len(path):
        return None
    hops = path[:qi]
    igate = path[qi + 1].upper()

    last_used = max((i for i, h in enumerate(hops) if h.endswith("*")), default=-1)
    used = [h.rstrip("*").upper() for h in hops[: last_used + 1]]
    digis = [h for h in used if not is_alias(h)]

    if not used:
        heard_by = igate  # heard directly by the igate
    elif not is_alias(used[0]):
        heard_by = used[0]  # first hop was a traced digi
    else:
        heard_by = None  # first hop was an untraced alias, so unknown

    # A traced digi plus the alias it used count as one hop.
    n_hops = len(digis) if digis else (1 if used else 0)
    return Route(path[qi], igate, heard_by, n_hops, digis)


@dataclass
class LastFix:
    ts: int
    lat: float
    lon: float
    heard_by: str | None


def mobile_matches(key: str, pattern: str) -> bool:
    """Match a mobile key against a registered pattern. Plain patterns (K7ABC*)
    match only callsigns; SENDER:OBJECT patterns match only object keys, so a
    callsign wildcard never pulls in objects that station sends."""
    k, p = key.upper(), pattern.upper()
    if (":" in k) != (":" in p):
        return False
    return fnmatch.fnmatchcase(k, p)


def object_key(sender: str, name: str) -> str:
    """Mobile key for an APRS object: 'K7SWI:CAR01'."""
    return f"{sender.upper()}:{name.strip().upper()}"


@dataclass
class Registry:
    # Plain callsign patterns (K7ABC-9, K7ABC*) and object patterns (K7SWI:CAR01).
    mobile_patterns: list[str] = field(default_factory=list)
    infra: set[str] = field(default_factory=set)

    def is_mobile(self, call: str) -> bool:
        return any(mobile_matches(call, p) for p in self.mobile_patterns)

    def is_object_sender(self, call: str) -> bool:
        c = call.upper()
        return any(fnmatch.fnmatchcase(c, p.split(":", 1)[0])
                   for p in self.mobile_patterns if ":" in p)

    def aprs_filter(self) -> str:
        # b/ matches the sending station, so object trackers subscribe by sender.
        senders = {p.split(":", 1)[0] for p in self.mobile_patterns}
        calls = sorted(senders | self.infra)
        return ("b/" + "/".join(calls)) if calls else ""


@dataclass
class Outcome:
    kind: str  # 'ignore' | 'infra' | 'reject' | 'accept'
    reason: str = ""
    source: str = ""
    record: dict | None = None


def evaluate(line: str, now: int, registry: Registry, last: dict[str, LastFix]) -> Outcome:
    """Classify one raw APRS-IS line. On 'accept', the record is ready to insert."""
    if not line or line.startswith("#"):
        return Outcome("ignore")
    try:
        pkt = aprslib.parse(line)
    except Exception:  # aprslib raises several exception types
        src = line.split(">", 1)[0] if ">" in line else ""
        if src.upper() in registry.infra:
            return Outcome("ignore")  # igate/digi telemetry etc.; not coverage data
        if src and (registry.is_mobile(src) or registry.is_object_sender(src)):
            return Outcome("reject", "unparseable", src)
        return Outcome("ignore")

    src = pkt.get("from", "").upper()
    fmt = pkt.get("format")
    has_pos = fmt in POSITION_FORMATS and "latitude" in pkt and "longitude" in pkt

    if fmt == "object":
        # A tracker reporting itself as an object: credit the object, keyed by
        # sender so nobody else can move it by sending the same name.
        key = object_key(src, pkt.get("object_name", ""))
        if not registry.is_mobile(key) or not pkt.get("alive", True):
            return Outcome("ignore")  # unregistered object, or a kill
        if "latitude" not in pkt or "longitude" not in pkt:
            return Outcome("ignore")
        src = key
    elif src in registry.infra:
        # A registered igate/digi is never a mobile, even if a mobile wildcard
        # (K7SWI*) also matches it. Its own beacons only place its marker, and
        # they usually arrive over TCPIP, so the RF checks don't apply.
        if has_pos and pkt.get("posambiguity", 0) == 0:
            return Outcome("infra", source=src,
                           record={"lat": pkt["latitude"], "lon": pkt["longitude"], "ts": now})
        return Outcome("ignore")
    elif not registry.is_mobile(src):
        return Outcome("ignore")
    elif not has_pos:
        return Outcome("ignore")  # status, telemetry, messages, other objects, items

    def reject(reason):
        return Outcome("reject", reason, src)

    route = route_from_path(pkt.get("path", []))
    if route is None:
        return reject("no_qconstruct")
    if route.qconstruct not in RF_QCONSTRUCTS:
        return reject("not_rf")
    if any(h.upper().startswith(("TCPIP", "TCPXX")) for h in pkt.get("path", [])):
        return reject("not_rf")

    lat, lon = float(pkt["latitude"]), float(pkt["longitude"])
    if pkt.get("posambiguity", 0):
        return reject("ambiguous")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (abs(lat) < 0.01 and abs(lon) < 0.01):
        return reject("bad_position")
    bb = config.REGION_BBOX
    if bb and not (bb[0] <= lat <= bb[2] and bb[1] <= lon <= bb[3]):
        return reject("out_of_region")

    prev = last.get(src)
    if prev is not None:
        dt = now - prev.ts
        km = approx_km(prev.lat, prev.lon, lat, lon)
        if (dt <= config.DUPLICATE_WINDOW_S and km < 0.001
                and prev.heard_by == route.heard_by):
            return reject("duplicate")
        if 0 <= dt < 3600:
            # Allow 1 km slack for GPS jitter and for back-to-back packets.
            if km > 1.0 and km / max(dt, 1) * 3600 > config.MAX_SPEED_KMH:
                return reject("speed_jump")

    chain = route.digis + [route.igate]
    record = {
        "ts": now,
        "mobile": src,
        "lat": lat,
        "lon": lon,
        "alt": pkt.get("altitude"),
        "speed": pkt.get("speed"),
        "course": pkt.get("course"),
        "igate": route.igate,
        "heard_by": route.heard_by,
        "hops": route.hops,
        "chain": " " + " ".join(chain) + " ",
        "raw": line,
    }
    return Outcome("accept", source=src, record=record)
