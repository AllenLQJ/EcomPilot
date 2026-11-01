"""Deterministic sandbox NLU; run with uvicorn stubs.nlu_stub:app --port 8001."""
import re

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()


class AnalyzeIn(BaseModel):
    text: str
    scene_type: str = "AFTER_SALE_FEEDBACK"


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/v1/nlu/analyze")
def analyze(body: AnalyzeIn):
    if body.scene_type in {"PDF_BODY", "CS_APOLOGY"}:
        return dict(clauses=[], sentiment="NEUTRAL", sentiment_score=0,
                    priority_boost=False, scene_not_applicable=True)
    rules = [
        (("热水器",), "热水器", "HOME_APPLIANCE_HEATER"),
        (("平板",), "平板", "DIGITAL_TABLET"),
        (("洗发水",), "洗发水", "PERSONAL_SHAMPOO"),
        (("牛奶", "蒙牛"), "蒙牛", "DAIRY_MILK"),
        (("酒店",), "酒店", "TRAVEL_HOTEL"),
        (("书",), "书籍", "BOOKS"),
    ]
    clauses = []
    for part in re.split(r"以及|顺便|还要|[，。！？；和]", body.text):
        part = part.strip()
        if not part:
            continue
        label, category = "ABSTAIN", None
        for keywords, candidate, hint in rules:
            if any(word in part for word in keywords):
                label, category = candidate, hint
                break
        clauses.append(dict(text=part, predicted_label=label,
                            category_confidence=0.30 if category is None else 0.98,
                            catalog_category_hint=category,
                            brand_hint="蒙牛" if label == "蒙牛" else None,
                            item_type="SERVICE" if label == "酒店" else "PRODUCT"))
    negative = any(word in body.text for word in
                   ("漏水", "打不着火", "坑", "退货", "退款", "坏了", "差评"))
    return dict(clauses=clauses, sentiment="NEGATIVE" if negative else "POSITIVE",
                sentiment_score=0.96 if negative else 0.60,
                priority_boost=body.scene_type == "AFTER_SALE_FEEDBACK" and negative,
                scene_not_applicable=False)
