import asyncio
import importlib
from contextlib import asynccontextmanager

from fastapi import FastAPI

from pilot.core.db import get_engine
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
    task = asyncio.create_task(_expire_loop())
    yield
    task.cancel()


app = FastAPI(title="EcomPilot", lifespan=lifespan)
install_error_handlers(app)
app.include_router(orders_router)
for _mod in ("pilot.refunds.router", "pilot.aftersales.router", "pilot.chat.router"):
    try:
        app.include_router(importlib.import_module(_mod).router)
    except ModuleNotFoundError as e:
        if e.name != _mod:
            raise


@app.get("/health")
def health():
    return {"status": "ok"}
