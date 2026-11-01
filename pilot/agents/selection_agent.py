"""Deterministic catalog selection with optional NLU ranking hints."""
import re

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, Field, StrictInt, ValidationError
from sqlalchemy import text, bindparam

from pilot.catalog.labels import resolve_label

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
            # NLU 自带的提示优先；它没给（如手机/水果等目录里原本没有的品类）才用 Pilot 的映射表补。读表失败只是少一个来源。
            if not (isinstance(clause.get("catalog_category_hint"), str) and clause["catalog_category_hint"]) or \
                    not isinstance(clause.get("brand_hint"), str):
                try:
                    mapped = resolve_label(get_engine(), clause.get("predicted_label"))
                except Exception:
                    mapped = None
                if mapped is not None:
                    clause = {**clause,
                              "catalog_category_hint": clause.get("catalog_category_hint") or mapped["catalog_category_id"],
                              "brand_hint": clause.get("brand_hint") or mapped["brand_hint"]}
            if isinstance(clause.get("catalog_category_hint"), str):
                categories.add(clause["catalog_category_hint"])
            if isinstance(clause.get("brand_hint"), str):
                brands.add(clause["brand_hint"])
        return categories, brands
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
        return set(), set()


def _rag_skus(query: str) -> set[str]:
    settings = get_settings()
    if not settings.rag_internal_token:
        return set()
    try:
        response = httpx.post(settings.rag_url.rstrip("/") + "/api/v1/rag/search",
                              headers={"X-Internal-Token": settings.rag_internal_token},
                              json={"query": query, "top_k": 5}, timeout=settings.rag_timeout_s)
        if response.status_code != 200:
            return set()
        hits = response.json()["hits"]
        if not isinstance(hits, list):
            return set()
        result = set()
        for hit in hits:
            skus = hit["skus"]
            if skus is None:
                continue
            if not isinstance(skus, list) or any(not isinstance(sku, str) for sku in skus):
                return set()
            result.update(skus)
        return result
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return set()


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
        rag_skus = _rag_skus(need.text)
        with get_engine().connect() as connection:
            rows = [dict(row) for row in connection.execute(text(
                "SELECT p.sku_id, p.sku_name, p.shop_id, p.catalog_category_id, "
                "p.unit_price_cents, p.positive_review_ratio, p.sellable_stock, p.brand_name "
                "FROM products_sku p JOIN catalog_categories c "
                "ON c.catalog_category_id=p.catalog_category_id "
                "WHERE p.tenant_id=:tenant AND p.sellable_stock>0 AND p.is_addon_pool=0 "
                "AND (:budget IS NULL OR p.unit_price_cents<=:budget) AND ("
                + " OR ".join(matches) + ")"), params).mappings()]
            missing = rag_skus - {row["sku_id"] for row in rows}
            if missing:
                rows.extend(dict(row) for row in connection.execute(text(
                    "SELECT sku_id, sku_name, shop_id, catalog_category_id, unit_price_cents, "
                    "positive_review_ratio, sellable_stock, brand_name FROM products_sku "
                    "WHERE tenant_id=:tenant AND sellable_stock>0 AND is_addon_pool=0 "
                    "AND (:budget IS NULL OR unit_price_cents<=:budget) AND sku_id IN :skus"
                ).bindparams(bindparam("skus", expanding=True)),
                    {**params, "skus": sorted(missing)}).mappings())
        rows.sort(key=lambda row: (
            -(2 * int(row["sku_id"] in rag_skus) + int(row["catalog_category_id"] in categories) + int(row["brand_name"] in brands)),
            -row["positive_review_ratio"], row["sku_id"]))
        for row in rows[:3]:
            row.pop("brand_name")
            row["positive_review_ratio"] = float(row["positive_review_ratio"])
            row.update(need_index=index, reviews=[f"{row['sku_name']}使用体验良好，值得推荐。"])
            candidates.append(row)
    return {"candidates": candidates}


app = make_agent_app("selection", 8012, ["selection"], handle)

install_error_handlers(app)
