#!/usr/bin/env bash
# 用真实 EcomNLU 替代 NLU 桩。先在 ../EcomNLU 起好服务（默认 :8003，需已有模型目录）：
#   cd ../EcomNLU && WEB_MODEL_DIR=models/<类别模型> WEB_SENTIMENT_MODEL_DIR=models/<情感模型> \
#     python -m uvicorn src.web.app:app --port 8003
# 本脚本在 :8001 起适配器（Pilot 仍按 :8001 调用），再起其余服务。
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-./.venv/bin/python}
export ECOMNLU_URL=${ECOMNLU_URL:-http://127.0.0.1:8003}
curl -sf "$ECOMNLU_URL/api/health" >/dev/null || { echo "EcomNLU 未就绪（$ECOMNLU_URL/api/health）：模型目录是否已就位？"; exit 1; }
mkdir -p .run
if ! { [ -f .run/ecomnlu_adapter.pid ] && kill -0 "$(cat .run/ecomnlu_adapter.pid)" 2>/dev/null; }; then
  nohup $PY -m uvicorn adapters.ecomnlu_adapter:app --port 8001 --log-level warning > .run/ecomnlu_adapter.log 2>&1 &
  echo $! > .run/ecomnlu_adapter.pid
fi
for i in $(seq 1 20); do curl -sf http://127.0.0.1:8001/health >/dev/null && break; sleep 1; done
curl -sf http://127.0.0.1:8001/health >/dev/null && echo "up :8001 (ecomnlu adapter)" || { echo "adapter FAILED (.run/ecomnlu_adapter.log)"; exit 1; }
SKIP_NLU_STUB=1 exec scripts/start_all.sh
