#!/usr/bin/env bash
# Verifies host prerequisites. Does not change the system.
set -u
rc=0; ok(){ echo "OK   $*"; }; bad(){ echo "FAIL $*"; rc=1; }
cd "$(dirname "$0")/.."
command -v docker >/dev/null && ok "$(docker --version)" || bad "docker missing: sudo zypper in docker docker-compose"
docker compose version >/dev/null 2>&1 && ok "compose $(docker compose version --short)" || bad "docker compose missing"
systemctl is-active --quiet docker && ok "docker daemon active" || bad "docker inactive: sudo systemctl enable --now docker"
docker info >/dev/null 2>&1 && ok "docker usable by $USER" || bad "cannot talk to docker: sudo usermod -aG docker $USER (re-login)"
D=/dev/dri/renderD128
[ -c $D ] && ok "$D exists" || bad "$D missing"
[ -r $D ] && [ -w $D ] && ok "$D rw for $USER" || bad "$D not accessible: sudo usermod -aG render $USER"
echo "RENDER_GID=$(stat -c %g $D 2>/dev/null)  (put in .env)"
python3 --version
mkdir -p data/buffer data/clips data/output && ok "data dirs"
[ -f .env ] || { cp .env.example .env; echo "created .env from template; fill secrets"; }
exit $rc
