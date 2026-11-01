#!/usr/bin/env bash
# 官方协议层（独立环境 .venv-proto）：A2A 网关 :8090（五个代理的标准 Agent Card + JSON-RPC）、MCP streamable-http :8091。
# 需要先用 scripts/start_all.sh 起好五个代理。首次使用：uv venv --python 3.12 .venv-proto && uv pip install --python .venv-proto/bin/python -r requirements-protocols.txt
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PROTO_PY:-.venv-proto/bin/python}
[ -x "$PY" ] || { echo "缺少 $PY：先创建 .venv-proto（见脚本注释）"; exit 1; }
mkdir -p .run
export PYTHONPATH=$PWD
start() { # name port cmd...
  local name=$1 port=$2; shift 2
  [ -f .run/$name.pid ] && kill -0 "$(cat .run/$name.pid)" 2>/dev/null && { echo "$name already running"; return; }
  nohup "$@" > .run/$name.log 2>&1 &
  echo $! > .run/$name.pid
}
start a2a_gateway 8090 $PY -m uvicorn protocols.a2a_gateway:app --port 8090 --log-level warning
start mcp_server 8091 $PY -m protocols.mcp_server --transport streamable-http --port 8091
for i in $(seq 1 30); do curl -sf http://127.0.0.1:8090/health >/dev/null && break; sleep 1; done
curl -sf http://127.0.0.1:8090/health >/dev/null && echo "up :8090 (a2a gateway)" || { echo "a2a gateway FAILED (.run/a2a_gateway.log)"; exit 1; }
sleep 2; kill -0 "$(cat .run/mcp_server.pid)" 2>/dev/null && echo "up :8091 (mcp streamable-http)" || { echo "mcp FAILED (.run/mcp_server.log)"; exit 1; }
