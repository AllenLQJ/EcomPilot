import asyncio
import importlib
from contextlib import asynccontextmanager

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel

from pilot.core.auth import issue_token
from pilot.core.db import get_engine
from pilot.core import hardening, observability
from pilot.core.config import get_settings
from pilot.core.errors import install_error_handlers
from pilot.orders import service
from pilot.orders.router import router as orders_router


async def _expire_loop():
    while True:
        await asyncio.sleep(30)
        try:
            await asyncio.to_thread(service.expire_unpaid_orders, get_engine())
        except Exception:
            pass


@asynccontextmanager
async def lifespan(_: FastAPI):
    hardening.enforce(get_settings())
    task = asyncio.create_task(_expire_loop())
    yield
    task.cancel()


app = FastAPI(title="EcomPilot", lifespan=lifespan)
install_error_handlers(app)
observability.install(app)
app.include_router(orders_router)
for _mod in ("pilot.refunds.router", "pilot.aftersales.router", "pilot.aftersales.internal", "pilot.chat.router"):
    try:
        app.include_router(importlib.import_module(_mod).router)
    except ModuleNotFoundError as e:
        if e.name != _mod:
            raise


@app.get("/health")
def health():
    return {"status": "ok"}


class _DevTokenIn(BaseModel):
    user_id: str
    tenant_id: str = "SHOP_SELF_01"


if os.environ.get("ECOMPILOT_DEV_TOKENS") == "1":
    @app.post("/api/v1/dev/token")
    def dev_token(body: _DevTokenIn):
        return {"token": issue_token(body.user_id, body.tenant_id)}

_INDEX = Path(__file__).resolve().parents[1] / "static" / "index.html"


@app.get("/", include_in_schema=False)
def index():
    if not _INDEX.exists():
        return {"status": "ok", "ui": "static/index.html not built yet"}
    return FileResponse(_INDEX)
