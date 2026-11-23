#!/usr/bin/env bash
# Phase 1 verification. Run after: docker compose up -d --build
set -u; cd "$(dirname "$0")/.."; rc=0
chk(){ if eval "$2" >/dev/null 2>&1; then echo "PASS $1"; else echo "FAIL $1"; rc=1; fi; }
chk "schema.sql + state.py"  "python3 -I -c \"import sys;sys.path.insert(0,'db');import state,os;s=state.State(':memory:');i=s.add_streamer('x','@x');c=s.new_clip(i,1.0);assert len(c)==6;s.set_status(c,'rendering');s.close()\""
chk "compose config valid"   "RENDER_GID=1 N8N_ENCRYPTION_KEY=x docker compose config -q"
chk "dashboard :5175"        "curl -fsS -m 10 http://localhost:5175"
chk "api docs :8000/docs"    "curl -fsS -m 10 http://localhost:8000/docs"
chk "VA-API test encode in watcher" "docker compose exec -T watcher ffmpeg -v error -y -init_hw_device vaapi=va:/dev/dri/renderD128 -filter_hw_device va -f lavfi -i testsrc2=s=854x480:d=2 -vf format=nv12,hwupload -c:v h264_vaapi -f null -"
chk "openshorts is CPU/x264" "docker compose exec -T openshorts-backend printenv FFMPEG_ENCODER | grep -qx x264"
exit $rc
