"""The beaconing model fitted per tracker for estimating lost beacons."""
import math
import random

from app import beacons


def drive(smart=True, seed=1, fast=(90, 60), slow=(10, 600), fixed=120, turns=True):
    """Every beacon a tracker sends on a drive mixing town, highway and stops."""
    rnd = random.Random(seed)
    lat, lon, hdg, t, last_t, last_hdg = 43.6, -116.5, 90, 0, -10**6, 90
    out = []
    for _ in range(30):
        dur, v0, turny = rnd.choice([(600, 40, 0.3), (1200, 110, 0.02), (900, 75, 0.08), (120, 0, 0)])
        for _ in range(dur):
            v = max(0.0, v0 + rnd.gauss(0, 3)) if v0 else 0.0
            if turns and v and rnd.random() < turny / 60:
                hdg = (hdg + rnd.choice([-90, 90])) % 360
            lat += v / 3600 * math.cos(math.radians(hdg)) / 111.0
            lon += v / 3600 * math.sin(math.radians(hdg)) / 80.0
            t += 1
            since = t - last_t
            if smart:
                (fv, ft), (sv, st) = fast, slow
                fire = since >= (st if v <= sv else ft if v >= fv else ft * fv / v)
                if v > sv and abs((hdg - last_hdg + 180) % 360 - 180) > 10 + 255 / v and since >= 15:
                    fire = True
            else:
                fire = since >= fixed
            if fire:
                out.append({"ts": t, "lat": lat, "lon": lon, "speed": v, "course": hdg or 360})
                last_t, last_hdg = t, hdg
    return out


def lose(sent, p, seed=2):
    rnd = random.Random(seed)
    out, bad = [], False
    for s in sent:
        bad = rnd.random() < (0.6 if bad else p * 0.6)  # losses come in runs
        if not bad:
            out.append(dict(s, ts=s["ts"] + rnd.randint(0, 3)))  # arrival jitter
    return out


def estimate(heard):
    m = beacons.fit(heard)
    return m, len(heard) / (len(heard) + sum(m.missed(a, b) for a, b in zip(heard, heard[1:])))


def test_smart_beaconing_is_recognised_and_estimated():
    sent = drive()
    for p in (0.1, 0.4, 0.7):
        heard = lose(sent, p)
        m, share = estimate(heard)
        b = m.describe()
        assert b["kind"] == "smart"
        assert 55 <= b["fast_s"] <= 65 and 1.3 <= b["spacing_km"] <= 1.7  # 60 s at 90 km/h = 1.5 km
        assert abs(share - len(heard) / len(sent)) < 0.05


def test_smart_beaconing_without_gps_speed_or_course():
    sent = drive(seed=3)
    heard = [dict(h, speed=None, course=None) for h in lose(sent, 0.3)]
    m, share = estimate(heard)
    assert m.smart and abs(share - len(heard) / len(sent)) < 0.05


def test_fixed_rate_stays_fixed():
    sent = drive(smart=False, seed=4)
    heard = lose(sent, 0.3)
    m, share = estimate(heard)
    assert m.describe()["kind"] == "fixed" and 115 <= m.fast_s <= 125
    assert abs(share - len(heard) / len(sent)) < 0.02


def test_stopped_gaps_need_a_tracker_that_beacons_when_stopped():
    moving = [{"ts": i * 60, "lat": 43.6, "lon": -116.5 + i * 0.0124, "speed": None, "course": None} for i in range(10)]
    end = moving[-1]
    # quiet while parked, then heard again from the same spot: no model for stops, nothing lost
    m = beacons.fit(moving + [dict(end, ts=end["ts"] + 400)])
    assert m.stop_s is None and m.missed(end, dict(end, ts=end["ts"] + 400)) == 0
    # beacons every 60 s while parked: a 180 s gap there lost two
    parked = [dict(end, ts=end["ts"] + 60 * k) for k in range(1, 12)]
    m = beacons.fit(moving + parked)
    assert round(m.stop_s) == 60
    assert m.missed(end, dict(end, ts=end["ts"] + 180)) == 2
    assert m.missed(end, dict(end, ts=end["ts"] + 3000)) == 0  # that long: switched off


def test_too_little_data():
    assert beacons.fit([{"ts": 0, "lat": 43.6, "lon": -116.5}, {"ts": 60, "lat": 43.6, "lon": -116.49}]) is None
