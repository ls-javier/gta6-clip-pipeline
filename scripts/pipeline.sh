#!/usr/bin/env bash
# Usage: scripts/pipeline.sh start|stop|status|logs
cd "$(dirname "$0")/.." || exit 1
D="docker compose"; docker info >/dev/null 2>&1 || D="sg docker -c"
run(){ if [ "$D" = "docker compose" ]; then docker compose "$@"; else sg docker -c "docker compose $*"; fi; }
case "${1:-status}" in
  start)  run up -d n8n watcher openshorts-backend openshorts-frontend ;;
  stop)   run stop watcher n8n openshorts-backend openshorts-frontend ;;   # stops recording + processing, keeps all data
  status) run ps; sqlite3 data/pipeline.db "select status,count(*) from clips group by status" ;;
  logs)   run logs -f --tail 50 watcher ;;
  *) echo "usage: $0 start|stop|status|logs"; exit 1 ;;
esac
