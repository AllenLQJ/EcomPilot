"""二级 A2A 种草代理：并行获取环境与选品，再获取报价。"""
import os
from concurrent.futures import ThreadPoolExecutor

from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictInt, StrictStr, ValidationError

from pilot.agents import a2a_client
from pilot.agents.base import make_agent_app
from pilot.core.db import get_engine
from pilot.mcp_tools.image_fallback import resolve_image


class Need(BaseModel):
    text: StrictStr = Field(min_length=1)
    category_hint: StrictStr | None = None
    budget_cents: StrictInt | None = Field(default=None, ge=0)


class SeedingInput(BaseModel):
    auth_token: StrictStr = Field(min_length=1)
    tenant_id: StrictStr = Field(min_length=1)
    city: StrictStr = Field(min_length=1)
    needs: list[Need] = Field(min_length=1)


def _invoke(agent: str, port: int, data: dict, trace_id: str) -> dict:
    # Read per call so deployment/test overrides do not require a module reload.
    url = os.environ.get(f"ECOMPILOT_AGENT_{agent}_URL", f"http://127.0.0.1:{port}")
    return a2a_client.invoke(url, data, trace_id)


def _money(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def _savings_formula(quote: dict) -> str | None:
    addons = quote.get("suggested_addons") or []
    if not addons:
        return None
    names = "、".join(addon["sku_name"] for addon in addons)
    # Each addon repeats the same bundle totals; never sum those fields.
    return (f"加购 {names} 后实付 ¥{_money(quote['total_payable_cents'])} → "
            f"¥{_money(addons[0]['payable_with_addon_cents'])}，"
            f"省 ¥{_money(addons[0]['saving_cents'])}")


def handle(data: dict, trace_id: str) -> dict:
    try:
        request = SeedingInput.model_validate(data)
    except ValidationError as exc:
        raise HTTPException(400, detail={"code": "INVALID_REQUEST", "message": "种草请求格式错误"}) from exc
    degraded = []
    results = {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {
            "env_fit": pool.submit(_invoke, "ENV_FIT", 8011, {"city": request.city}, trace_id),
            "selection": pool.submit(_invoke, "SELECTION", 8012, {
                "tenant_id": request.tenant_id,
                "needs": [need.model_dump(exclude_none=True) for need in request.needs],
            }, trace_id),
        }
        for name, future in futures.items():
            try:
                results[name] = future.result()
            except a2a_client.A2AError:
                results[name] = None
                degraded.append(name)

    candidates = (results["selection"] or {}).get("candidates", [])
    first_by_need = {}
    for candidate in candidates:
        first_by_need.setdefault(candidate["need_index"], candidate)
    picks = [first_by_need[index] for index in range(len(request.needs)) if index in first_by_need]
    quote = {}
    if picks:
        try:
            quote = _invoke("PRICING", 8013, {
                "auth_token": request.auth_token,
                "items": [{"sku_id": pick["sku_id"], "qty": 1} for pick in picks],
            }, trace_id)["quote"]
        except a2a_client.A2AError:
            degraded.append("pricing_order")

    images = [{"sku_id": pick["sku_id"], **resolve_image(get_engine(), pick["sku_id"])} for pick in picks]
    evidence = [{"sku_id": pick["sku_id"], "review": next(iter(pick.get("reviews") or []), None)} for pick in picks]
    city_advice = (results["env_fit"] or {}).get("advice")
    title = f"{request.city}采购种草清单"
    paragraphs = [title]
    if city_advice:
        paragraphs.append(f"城市建议：{city_advice['clothing']}；{city_advice['shampoo_spec']}。")
    for pick, item in zip(picks, evidence):
        paragraphs.append(f"推荐：{pick['sku_name']}（¥{_money(pick['unit_price_cents'])}）")
        if item["review"] is not None:
            paragraphs.append(f"买家评论：“{item['review']}”")
    formula = _savings_formula(quote)
    if formula:
        paragraphs.append(formula)
        paragraphs.append("凑单建议需勾选确认后才加入结算单。")
    return {"note": {
        "title": title, "body": "\n".join(paragraphs), "images": images,
        "quote_id": quote.get("quote_id"), "savings_formula": formula,
        "evidence": {"city_advice": city_advice, "picks": evidence}, "degraded": degraded,
    }}


app = make_agent_app("seeding_note", 8015, ["seeding_note"], handle)


@app.exception_handler(RequestValidationError)
async def invalid_request(request, exc):
    return JSONResponse(status_code=400, content={"error": {"code": "INVALID_REQUEST", "message": "请求格式错误"}})
