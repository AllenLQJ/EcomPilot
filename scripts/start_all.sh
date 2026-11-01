#!/usr/bin/env bash
# 一键启动：MySQL(docker) + 导入 schema/seed + 3 桩 + 5 代理 + Pilot 主 API。日志与 pid 在 .run/。
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}
mkdir -p .run
export ECOMPILOT_DB_NAME=${ECOMPILOT_DB_NAME:-ecompilot}
export ECOMPILOT_DEV_TOKENS=1
export PILOT_BASE_URL=http://127.0.0.1:8085
export PILOT_INTERNAL_TOKEN=${ECOMPILOT_INTERNAL_TOKEN:-dev-internal-token}

docker compose -f deploy/docker-compose.yml up -d >/dev/null
for i in $(seq 1 60); do
  [ "$(docker inspect -f '{{.State.Health.Status}}' ecompilot-mysql)" = healthy ] && break; sleep 2
done
if [ "${RESET_DB:-0}" = 1 ] || ! docker exec ecompilot-mysql mysql -uroot -pecompilot_root -N -e "select 1 from information_schema.tables where table_schema='$ECOMPILOT_DB_NAME' and table_name='orders'" 2>/dev/null | grep -q 1; then
  docker exec ecompilot-mysql mysql -uroot -pecompilot_root -e "drop database if exists \`$ECOMPILOT_DB_NAME\`; create database \`$ECOMPILOT_DB_NAME\` character set utf8mb4" 2>/dev/null
  for f in contracts/schema.sql contracts/fixtures/seed.sql; do
    docker exec -i ecompilot-mysql mysql -uroot -pecompilot_root --default-character-set=utf8mb4 "$ECOMPILOT_DB_NAME" < "$f" 2>/dev/null
  done
  echo "db initialised: $ECOMPILOT_DB_NAME"
fi

start() { # name module port
  [ -f .run/$1.pid ] && kill -0 "$(cat .run/$1.pid)" 2>/dev/null && { echo "$1 already running"; return; }
  nohup $PY -m uvicorn "$2" --port "$3" --log-level warning > .run/$1.log 2>&1 &
  echo $! > .run/$1.pid
}
[ "${SKIP_OPS_STUB:-0}" = 1 ] || start ops_stub stubs.ops_stub:app 8000
start nlu_stub stubs.nlu_stub:app 8001
[ "${SKIP_RAG_STUB:-0}" = 1 ] || start rag_stub stubs.rag_stub:app 8002
start env_fit pilot.agents.env_fit_agent:app 8011
start selection pilot.agents.selection_agent:app 8012
start pricing_order pilot.agents.pricing_order_agent:app 8013
start aftersales pilot.agents.aftersales_agent:app 8014
start seeding_note pilot.agents.seeding_note_agent:app 8015
start pilot pilot.main:app 8085

PORTS="8001 8011 8012 8013 8014 8015 8085"
[ "${SKIP_OPS_STUB:-0}" = 1 ] || PORTS="8000 $PORTS"
[ "${SKIP_RAG_STUB:-0}" = 1 ] || PORTS="8002 $PORTS"
for p in $PORTS; do
  for i in $(seq 1 30); do curl -sf "http://127.0.0.1:$p/health" >/dev/null && break; sleep 1; done
  curl -sf "http://127.0.0.1:$p/health" >/dev/null && echo "up :$p" || { echo "FAILED :$p (see .run/*.log)"; exit 1; }
done
