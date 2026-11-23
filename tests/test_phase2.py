"""Phase 2 offline tests (run inside the watcher container):
  docker compose exec -T watcher python -m tests.test_phase2   (tests/ mounted via /app/tests)
"""
import os, subprocess, sys, tempfile, time
from pathlib import Path
from db.state import State
from watcher.irc_watcher import SpikeDetector, IrcWatcher, parse_privmsg
from watcher.buffer_manager import BufferManager, probe, SEG_COUNT

def test_parse():
    assert parse_privmsg(":bob!bob@bob.tmi.twitch.tv PRIVMSG #c :hello world") == ("bob", "hello world")
    assert parse_privmsg("@badge=1;x=y :bob!bob@bob.tmi.twitch.tv PRIVMSG #c :a :b") == ("bob", "a :b")
    assert parse_privmsg(":tmi.twitch.tv 001 justinfan1234 :Welcome") is None

def test_detector():
    t = [0.0]
    d = SpikeDetector(clock=lambda: t[0])
    # 5 minutes at 2 msgs/10s => baseline 2; no trigger
    for _ in range(30):
        for _ in range(2):
            assert d.add(t[0]) is None
        t[0] += 10
    # 3x burst but under absolute floor (6 < 10) -> no trigger
    fired = [d.add(t[0] + i * .1) for i in range(6)]
    assert not any(fired)
    t[0] += 10
    # real burst: 12 msgs in one bin vs baseline ~2 => fires once, then cooldown
    fired = [d.add(t[0] + i * .1) for i in range(40)]
    hits = [f for f in fired if f]
    assert len(hits) == 1 and hits[0][0] >= 3, hits
    # inside cooldown (90s): another burst must not fire
    t[0] += 20
    assert not any(d.add(t[0] + i * .1) for i in range(40))
    # after cooldown, history is now elevated; a bigger burst still fires
    t[0] += 100
    assert any(d.add(t[0] + i * .01) for i in range(400))

def test_warmup():
    d = SpikeDetector(clock=lambda: 0.0)
    assert not any(d.add(i * .01) for i in range(100))   # no history yet

def make_fake_buffer(root: Path, now: float):
    """3 x 60s segments with timestamps that restart at a non-zero offset (as Streamlink 8 does)."""
    d = root / "chan"; d.mkdir(parents=True)
    for i in range(3):
        f = d / f"seg_{i:02d}.ts"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=60",
                        "-f", "lavfi", "-i", "sine=f=440:d=60", "-c:v", "libx264", "-preset", "ultrafast",
                        "-g", "30", "-c:a", "aac", "-output_ts_offset", "7200.5", "-f", "mpegts", str(f)], check=True)
        assert probe(f)["start_time"] > 7000           # confirms the non-zero start_time hazard
        end = now - (2 - i) * 60                       # seg_02 newest, ends 'now'
        os.utime(f, (end, end))
    return d

def test_slice_and_db():
    with tempfile.TemporaryDirectory() as td:
        now = time.time()
        make_fake_buffer(Path(td), now)
        buf = BufferManager("chan", td)
        db = State(":memory:")
        sid_streamer = db.add_streamer("chan", "@chan")
        spike = now - 20                                # window [now-50, now-5] spans seg_01 + seg_02
        got = []
        w = IrcWatcher("chan", lambda ts, r, ex: got.append((ts, r, ex)))
        # mock 3x chat burst through the real line handler
        for i in range(30 * 6):
            w.handle_line(f":u{i}!u@u.tmi.twitch.tv PRIVMSG #chan :msg{i}", now=1000 + i * 10 / 2)
        base = 1000 + 180 * 5
        for i in range(40):
            w.handle_line(f":u{i}!u@u.tmi.twitch.tv PRIVMSG #chan :KEKW {i}", now=base + i * .1)
        assert len(got) == 1 and got[0][1] >= 3 and "KEKW" in got[0][2]
        sid = db.new_clip(sid_streamer, spike, got[0][1], got[0][2])
        out = Path(td) / "out" / f"{sid}.mp4"
        info = buf.slice(spike, str(out), 30, 15)
        assert out.exists() and info["has_video"] and info["has_audio"]
        assert abs(info["duration"] - 45) < 1.5, info
        assert info["start_time"] < 1.0, info           # timestamps normalised
        db.execute("UPDATE clips SET raw_path=? WHERE short_id=?", (str(out), sid))
        assert db.query("SELECT raw_path FROM clips")[0]["raw_path"] == str(out)
        print("slice:", info)

if __name__ == "__main__":
    fails = 0
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            try: f(); print("PASS", n)
            except Exception as e:
                import traceback; traceback.print_exc(); print("FAIL", n); fails += 1
    sys.exit(fails)
