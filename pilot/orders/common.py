import json
import uuid
from datetime import datetime


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def new_order_id() -> str:
    return f"ORD_{datetime.now():%Y%m%d}_{uuid.uuid4().hex[:8].upper()}"


def iso(dt) -> str | None:
    return dt.strftime("%Y-%m-%dT%H:%M:%S") if dt else None


def jload(v):
    return json.loads(v) if isinstance(v, (str, bytes)) else v
