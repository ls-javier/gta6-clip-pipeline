"""python -m tests.test_supervisor (inside watcher container)"""
from watcher import twitch_api
from watcher.supervisor import Supervisor
from watcher.irc_watcher import SpikeDetector

def test_is_gta6():
    assert twitch_api.is_gta6("Grand Theft Auto VI")
    assert twitch_api.is_gta6("Grand Theft Auto 6")
    assert not twitch_api.is_gta6("Grand Theft Auto V")
    assert not twitch_api.is_gta6("Just Chatting") and not twitch_api.is_gta6("")

def test_select_top2_only_gta6_and_sticky():
    sup = Supervisor(None, "/tmp", None)
    group = [{"twitch_channel": c} for c in ("a", "b", "c", "d", "e")]
    G, V = "Grand Theft Auto VI", "Just Chatting"
    live = {"a": {"viewers": 100, "game": G}, "b": {"viewers": 900, "game": V}, "c": {"viewers": 500, "game": G},
            "d": {"viewers": 300, "game": G}, "e": {"viewers": 50, "game": G}}
    assert sup.select_group(group, live) == ["c", "d"] or sup.select_group(group, live) == ["c", "d"][:2]
    assert set(sup.select_group(group, live)) == {"c", "d"}            # b is the biggest but not on GTA VI
    sup.running = {"d": None}                                          # d already tracked: keep it, add best other
    live["a"]["viewers"] = 2000
    assert set(sup.select_group(group, live)) == {"d", "a"}            # sticky d; a (top) fills the free slot
    live["d"]["game"] = V
    assert set(sup.select_group(group, live)) == {"a", "c"}            # d stopped playing -> replaced
    assert sup.select_group(group, {}) == []                           # nobody live

def test_per_streamer_ratio():
    t = [0.0]
    for ratio, expect in ((1.5, True), (2.0, False)):
        d = SpikeDetector(clock=lambda: t[0], ratio=ratio, min_count=8)
        for _ in range(30):
            for _ in range(20): d.add(t[0])
            t[0] += 10
        fired = [d.add(t[0] + i * .01) for i in range(32)]           # 32 msgs vs baseline 20 -> 1.6x
        assert bool(any(fired)) is expect, (ratio, fired)
        t[0] += 1000

if __name__ == "__main__":
    import sys
    f = 0
    for n, fn in list(globals().items()):
        if n.startswith("test_"):
            try: fn(); print("PASS", n)
            except Exception as e:
                import traceback; traceback.print_exc(); print("FAIL", n); f += 1
    sys.exit(f)
