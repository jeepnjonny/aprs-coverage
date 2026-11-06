from app.packets import LastFix, Registry, evaluate, is_alias, route_from_path

REG = Registry(mobile_patterns=["K7ABC-9", "W7MOB*"], infra={"K7SWI-1", "KG7KMV-10"})
POS = "=4336.66N/11612.40W>test"


def ev(line, now=1_000_000, last=None):
    return evaluate(line, now, REG, last if last is not None else {})


def test_alias_detection():
    assert is_alias("WIDE1-1") and is_alias("WIDE2") and is_alias("IDAHO") and is_alias("RELAY")
    assert is_alias("ID-1") and is_alias("SAR")
    assert not is_alias("K7SWI-1") and not is_alias("KG7KMV")
    assert not is_alias("RBERRY") and not is_alias("GIRARD")  # word-named digis are stations


def test_route_word_named_digi():
    r = route_from_path(["RBERRY*", "WIDE1", "WIDE2-1", "qAR", "DURNGO"])
    assert r.heard_by == "RBERRY" and r.hops == 1


def test_route_direct_to_igate():
    r = route_from_path(["WIDE1-1", "WIDE2-1", "qAR", "KG7KMV-10"])
    assert r.heard_by == "KG7KMV-10" and r.hops == 0 and r.igate == "KG7KMV-10"


def test_route_traced_digi():
    r = route_from_path(["K7SWI-1", "WIDE1*", "WIDE2-1", "qAR", "KG7KMV-10"])
    assert r.heard_by == "K7SWI-1" and r.hops == 1 and r.digis == ["K7SWI-1"]


def test_route_two_digis_first_wins():
    r = route_from_path(["K7SWI-1", "WIDE1", "N7DIG-2", "WIDE2*", "qAR", "KG7KMV-10"])
    assert r.heard_by == "K7SWI-1" and r.digis == ["K7SWI-1", "N7DIG-2"] and r.hops == 2


def test_route_untraced():
    r = route_from_path(["WIDE2*", "qAR", "KG7KMV-10"])
    assert r.heard_by is None and r.hops == 1


def test_accept_and_record():
    o = ev(f"K7ABC-9>APDR16,K7SWI-1,WIDE1*,WIDE2-1,qAR,KG7KMV-10:{POS}")
    assert o.kind == "accept"
    assert o.record["heard_by"] == "K7SWI-1"
    assert o.record["chain"] == " K7SWI-1 KG7KMV-10 "


def test_wildcard_mobile():
    assert ev(f"W7MOB-7>APRS,qAR,KG7KMV-10:{POS}").kind == "accept"


def test_unregistered_ignored():
    assert ev(f"N0XYZ-9>APRS,qAR,KG7KMV-10:{POS}").kind == "ignore"


def test_internet_packets_rejected():
    o = ev(f"K7ABC-9>APRS,TCPIP*,qAC,T2TEST:{POS}")
    assert o.kind == "reject" and o.reason == "not_rf"


def test_ambiguous_rejected():
    o = ev("K7ABC-9>APRS,qAR,KG7KMV-10:!4336.6 N/11612.4 W-")
    assert o.reason == "ambiguous"


def test_zero_position_rejected():
    o = ev("K7ABC-9>APRS,qAR,KG7KMV-10:!0000.00N/00000.00W-")
    assert o.reason == "bad_position"


def test_speed_jump_rejected():
    last = {"K7ABC-9": LastFix(1_000_000 - 60, 44.5, -116.2, None)}  # ~100 km away a minute ago
    o = ev(f"K7ABC-9>APRS,qAR,KG7KMV-10:{POS}", last=last)
    assert o.reason == "speed_jump"


def test_duplicate_rejected_but_new_heard_by_kept():
    last = {"K7ABC-9": LastFix(1_000_000 - 5, 43.611, -116.20666666666666, "KG7KMV-10")}
    assert ev(f"K7ABC-9>APRS,qAR,KG7KMV-10:{POS}", last=last).reason == "duplicate"
    o = ev(f"K7ABC-9>APRS,K7SWI-1*,qAR,KG7KMV-10:{POS}", last=last)
    assert o.kind == "accept" and o.record["heard_by"] == "K7SWI-1"


def test_infra_beacon_updates_position():
    o = ev("K7SWI-1>APRS,qAR,KG7KMV-10:!4340.00N/11615.00W#digi")
    assert o.kind == "infra" and round(o.record["lat"], 3) == 43.667


def test_filter_string():
    assert REG.aprs_filter() == "b/K7ABC-9/K7SWI-1/KG7KMV-10/W7MOB*"
