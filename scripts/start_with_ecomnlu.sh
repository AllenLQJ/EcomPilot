#!/usr/bin/env bash
# 用真实 EcomNLU（原生接口 POST /api/v1/nlu/analyze）替代 NLU 桩。先在 ../EcomNLU 起好服务（默认 :8001，需已有模型目录）：
#   cd ../EcomNLU && WEB_MODEL_DIR=models/<类别模型> WEB_SENTIMENT_MODEL_DIR=models/<情感模型> \
#     python -m uvicorn src.web.app:app --port 8001
set -euo pipefail
cd "$(dirname "$0")/.."
NLU_URL=${ECOMPILOT_NLU_URL:-http://127.0.0.1:8001}
curl -sf "$NLU_URL/api/health" >/dev/null || { echo "EcomNLU 未就绪（$NLU_URL/api/health）：模型目录是否已就位？"; exit 1; }
SKIP_NLU_STUB=1 ECOMPILOT_NLU_URL="$NLU_URL" exec scripts/start_all.sh
