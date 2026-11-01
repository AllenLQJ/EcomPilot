"""Sandbox demo policies; run with uvicorn stubs.rag_stub:app --port 8002."""
from fastapi import FastAPI
from pydantic import BaseModel, Field

app = FastAPI()


class SearchIn(BaseModel):
    query: str
    catalog_category_id: str | None = None
    top_k: int = Field(default=5, ge=0)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/v1/rag/search")
def search(body: SearchIn):
    hits = [dict(chunk_id="DEMO_GENERAL_7D", text="沙箱演示：商品签收后 7 天内，符合完好条件可申请无理由退货。",
                 rule_source="通用7天无理由退货规则（沙箱演示）", is_demo_rule=True, score=0.8)]
    if body.catalog_category_id == "HOME_APPLIANCE_HEATER":
        hits.append(dict(chunk_id="DEMO_HEATER_WARRANTY", text="沙箱演示：热水器整机保修期内发生漏水或打不着火等质量问题，可凭订单及证据申请退换。",
                         rule_source="热水器整机保修与漏水退换细则", is_demo_rule=True, score=0.9))
    return {"hits": sorted(hits, key=lambda hit: hit["score"], reverse=True)[:body.top_k]}
