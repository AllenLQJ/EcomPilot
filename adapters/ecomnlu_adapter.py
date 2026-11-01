"""把真实 EcomNLU 适配成 EcomPilot 约定的 NLU 契约（替代 stubs/nlu_stub.py）。

  POST /api/v1/nlu/analyze  ->  EcomNLU POST /api/predict（单条评论：十类商品 + 正负向情感）

EcomNLU 是评论分类模型，没有子句切分、品牌与 catalog 品类：这些在这里补齐。
它对导购需求、售后吐槽这类非评论文本的效果没有验证过，所以低置信一律 ABSTAIN，宁可不给提示。
"""
import os
import re

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

ECOMNLU_URL = os.environ.get("ECOMNLU_URL", "http://127.0.0.1:8003").rstrip("/")
TIMEOUT_S = float(os.environ.get("ECOMNLU_TIMEOUT_S", "2.0"))
CONFIDENCE_THRESHOLD = float(os.environ.get("ECOMNLU_CONFIDENCE_THRESHOLD", "0.65"))  # 与 pilot/aftersales/router.py 一致
MAX_CLAUSES = 5

# EcomNLU 十类 -> 本店 catalog_category_id；没有对应品类的不给 hint（None）
CATEGORY_MAP = {
    "热水器": "HOME_APPLIANCE_HEATER",
    "平板": "DIGITAL_TABLET",
    "洗发水": "PERSONAL_SHAMPOO",
    "蒙牛": "DAIRY_MILK",
    "酒店": "TRAVEL_HOTEL",
    "书籍": "BOOKS",
}
BRAND_MAP = {"蒙牛": "蒙牛"}
SERVICE_LABELS = {"酒店"}
NOT_APPLICABLE_SCENES = {"PDF_BODY", "CS_APOLOGY"}
# Pilot 实际发送 aftersales / shopping；桩只认 AFTER_SALE_FEEDBACK，这里两种写法都认
AFTER_SALE_SCENES = {"AFTER_SALE_FEEDBACK", "aftersales"}

app = FastAPI()


class AnalyzeIn(BaseModel):
    text: str
    scene_type: str = "AFTER_SALE_FEEDBACK"


class Upstream(Exception):
    pass


def _predict(client: httpx.Client, review: str) -> dict:
    try:
        r = client.post(ECOMNLU_URL + "/api/predict", json={"review": review, "top_k": 1})
    except httpx.HTTPError as exc:
        raise Upstream(f"EcomNLU unreachable: {exc}"[:300]) from exc
    if r.status_code != 200:
        raise Upstream(f"EcomNLU status {r.status_code}: {r.text[:200]}")
    try:
        data = r.json()
        data["category"]["label"], data["category"]["confidence"]
    except (ValueError, KeyError, TypeError) as exc:
        raise Upstream("EcomNLU malformed response") from exc
    return data


def _clauses(text: str) -> list[str]:
    parts = [p.strip() for p in re.split(r"以及|顺便|还要|[，,。！!？?；;、和]", text)]
    return [p for p in parts if p][:MAX_CLAUSES]


def _clause(text: str, prediction: dict) -> dict:
    label, confidence = prediction["category"]["label"], float(prediction["category"]["confidence"])
    if confidence < CONFIDENCE_THRESHOLD:
        return dict(text=text, predicted_label="ABSTAIN", category_confidence=round(confidence, 4),
                    catalog_category_hint=None, brand_hint=None, item_type="PRODUCT")
    return dict(text=text, predicted_label=label, category_confidence=round(confidence, 4),
                catalog_category_hint=CATEGORY_MAP.get(label), brand_hint=BRAND_MAP.get(label),
                item_type="SERVICE" if label in SERVICE_LABELS else "PRODUCT")


@app.post("/api/v1/nlu/analyze")
def analyze(body: AnalyzeIn):
    if body.scene_type in NOT_APPLICABLE_SCENES:
        return dict(clauses=[], sentiment="NEUTRAL", sentiment_score=0,
                    priority_boost=False, scene_not_applicable=True)
    text = body.text.strip()
    parts = _clauses(text)
    try:
        with httpx.Client(timeout=TIMEOUT_S) as client:
            whole = _predict(client, text[:2000])
            clauses = []
            for part in parts:
                prediction = whole if part == text else _predict(client, part)
                clauses.append(_clause(part, prediction))
    except Upstream as exc:
        # 5xx 让调用方（Pilot）走既有降级路径；不要伪造"没有信号"的 200
        return JSONResponse({"error": {"code": "UPSTREAM_ERROR", "message": str(exc)}}, status_code=502)
    sentiment = whole.get("sentiment")
    if isinstance(sentiment, dict) and sentiment.get("label") in ("正向", "负向"):
        negative = sentiment["label"] == "负向"
        label, score = ("NEGATIVE" if negative else "POSITIVE"), float(sentiment.get("confidence", 0))
    else:    # 情感模型未配置：不猜
        negative, label, score = False, "NEUTRAL", 0.0
    return dict(clauses=clauses, sentiment=label, sentiment_score=round(score, 4),
                priority_boost=negative and body.scene_type in AFTER_SALE_SCENES,
                scene_not_applicable=False)


@app.get("/health")
def health():
    try:
        r = httpx.get(ECOMNLU_URL + "/api/health", timeout=3.0)
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        ok = r.status_code == 200
        return JSONResponse({"status": "ok" if ok else "degraded", "ecomnlu": body}, status_code=200 if ok else 503)
    except (httpx.HTTPError, ValueError):
        return JSONResponse({"status": "degraded", "ecomnlu": None}, status_code=503)
