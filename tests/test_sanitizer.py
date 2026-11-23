"""Synthetic check: 0-6s music only | 6-16s speech over music | explosion SFX at 11s (no music bed change).
  python -m tests.test_sanitizer"""
import os, subprocess, tempfile, re, sys
from pathlib import Path
from watcher import audio_sanitizer as A

def sh(*c): 
    r = subprocess.run(list(c), capture_output=True, text=True); 
    if r.returncode: raise RuntimeError(r.stderr[-500:]); 
    return r.stderr
def rms(path, ss, t):
    o = sh("ffmpeg", "-v", "info", "-ss", str(ss), "-t", str(t), "-i", path, "-vn", "-af", "astats=metadata=0", "-f", "null", "-")
    m = re.findall(r"RMS level dB: (-?[\d.inf]+)", o)
    return float(m[-1]) if m and m[-1] != "-inf" else -90.0

with tempfile.TemporaryDirectory(dir="/data/tmp") as td:
    td = Path(td)
    sh("espeak-ng", "-v", "en-us", "-s", "150", "-w", str(td/"sp.wav"),
       "Oh my god did you see that? He just drove straight into the helicopter. Chat, clip it, clip it right now.")
    # music: chord pad + 120bpm kick + bass line, 16 s
    music = ("sine=f=110:d=16,volume=0.25[b];sine=f=262:d=16,volume=0.12[c1];sine=f=330:d=16,volume=0.12[c2];"
             "sine=f=392:d=16,volume=0.12[c3];anoisesrc=d=16:c=pink:a=0.02,volume=0.5[n]")
    sh("ffmpeg","-v","error","-y","-f","lavfi","-i","sine=f=110:d=16","-f","lavfi","-i","sine=f=262:d=16",
       "-f","lavfi","-i","sine=f=330:d=16","-f","lavfi","-i","sine=f=392:d=16","-f","lavfi","-i","anoisesrc=d=16:c=pink:a=0.03",
       "-filter_complex","[0]volume=0.3[a];[1]volume=0.12[b];[2]volume=0.12[c];[3]volume=0.12[d];[4]volume=0.5[n];"
       "[a][b][c][d][n]amix=inputs=5:normalize=0,tremolo=f=2:d=0.6[m]","-map","[m]","-ac","2",str(td/"music.wav"))
    sh("ffmpeg","-v","error","-y","-f","lavfi","-i","anoisesrc=d=1.2:c=brown:a=0.9","-af","afade=t=out:st=0.1:d=1.1,volume=1.5","-ac","2",str(td/"boom.wav"))
    sh("ffmpeg","-v","error","-y","-i",str(td/"music.wav"),"-i",str(td/"sp.wav"),"-i",str(td/"boom.wav"),
       "-filter_complex","[1]adelay=6000|6000,apad[s];[2]adelay=11000|11000,apad[x];[0][s][x]amix=inputs=3:normalize=0:duration=first[a]",
       "-map","[a]","-t","16",str(td/"mix.wav"))
    sh("ffmpeg","-v","error","-y","-f","lavfi","-i","testsrc2=s=640x360:r=30:d=16","-i",str(td/"mix.wav"),
       "-c:v","libx264","-preset","ultrafast","-c:a","aac","-shortest",str(td/"in.mp4"))
    for keep in (["vocals","other"], ["vocals"]):
        out = str(td/f"out_{'_'.join(keep)}.mp4")
        A.sanitize(str(td/"in.mp4"), out, keep=keep)
        print(f"\nKEEP={keep}")
        for name, ss, t in (("music only 0-6s", 0, 6), ("speech+music 6-10.5s", 6.2, 4.3), ("SFX 11-12s", 11, 1)):
            a, b = rms(str(td/"in.mp4"), ss, t), rms(out, ss, t)
            print(f"  {name:22s} in={a:6.1f} dB out={b:6.1f} dB  delta={b-a:+6.1f}")
