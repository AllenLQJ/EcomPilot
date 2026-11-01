"""Deterministic catalog selection with optional NLU ranking hints."""
import re

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, Field, StrictInt, ValidationError
from sqlalchemy import text

from pilot.core.errors import install_error_handlers

from pilot.agents.base import make_agent_app
from pilot.core.config import get_settings
from pilot.core.db import get_engine


class Need(BaseModel):
    text: str = Field(min_length=1)
    category_hint: str | None = None
    budget_cents: StrictInt | None = Field(default=None, ge=0)


class SelectionInput(BaseModel):
    tenant_id: str = Field(min_length=1)
    needs: list[Need] = Field(min_length=1)


def _hints(need: str) -> tuple[set[str], set[str]]:
    try:
        response = httpx.post(get_settings().nlu_url.rstrip("/") + "/api/v1/nlu/analyze",
                              json={"text": need, "scene_type": "shopping"}, timeout=2.0)
        response.raise_for_status()
        clauses = response.json()["clauses"]
        categories, brands = set(), set()
        for clause in clauses:
            if clause.get("predicted_label") == "ABSTAIN":
                continue
            if isinstance(clause.get("catalog_category_hint"), str):
                categories.add(clause["catalog_category_hint"])
            if isinstance(clause.get("brand_hint"), str):
                brands.add(clause["brand_hint"])
        return categories, brands
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
        return set(), set()


def handle(data: dict, trace_id: str) -> dict:
    try:
        request = SelectionInput.model_validate(data)
        if any(not need.text.strip() for need in request.needs):
            raise ValueError("empty need")
    except (ValidationError, ValueError) as exc:
        raise HTTPException(400, detail="INVALID_REQUEST") from exc
    candidates = []
    for index, need in enumerate(request.needs):
        categories, brands = _hints(need.text)
        if need.category_hint:
            categories.add(need.category_hint)
        # INSTR treats user text literally, including SQL LIKE wildcard characters.
        words = re.findall(r"[\w]+", need.text)
        params = {"tenant": request.tenant_id, "need": need.text, "budget": need.budget_cents}
        matches = ["INSTR(:need, c.category_name)>0", "INSTR(:need, p.sku_name)>0"]
        for i, word in enumerate(words):
            params[f"word{i}"] = word
            matches.extend([f"INSTR(p.sku_name, :word{i})>0", f"INSTR(c.category_name, :word{i})>0"])
        with get_engine().connect() as connection:
            rows = [dict(row) for row in connection.execute(text(
                "SELECT p.sku_id, p.sku_name, p.shop_id, p.catalog_category_id, "
                "p.unit_price_cents, p.positive_review_ratio, p.sellable_stock, p.brand_name "
                "FROM products_sku p JOIN catalog_categories c "
                "ON c.catalog_category_id=p.catalog_category_id "
                "WHERE p.tenant_id=:tenant AND p.sellable_stock>0 AND p.is_addon_pool=0 "
                "AND (:budget IS NULL OR p.unit_price_cents<=:budget) AND ("
                + " OR ".join(matches) + ")"), params).mappings()]
        rows.sort(key=lambda row: (
            -(int(row["catalog_category_id"] in categories) + int(row["brand_name"] in brands)),
            -row["positive_review_ratio"], row["sku_id"]))
        for row in rows[:3]:
            row.pop("brand_name")
            row["positive_review_ratio"] = float(row["positive_review_ratio"])
            row.update(need_index=index, reviews=[f"{row['sku_name']}使用体验良好，值得推荐。"])
            candidates.append(row)
    return {"candidates": candidates}


app = make_agent_app("selection", 8012, ["selection"], handle)

install_error_handlers(app)
