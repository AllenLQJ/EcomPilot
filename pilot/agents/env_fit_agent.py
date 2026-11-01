"""C 级环境适配代理：固定城市天气与确定性建议。"""
from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictInt, StrictStr, ValidationError

from pilot.agents.base import make_agent_app


class EnvInput(BaseModel):
    city: StrictStr = Field(min_length=1)
    days: StrictInt = Field(default=1, ge=1)


_WEATHER = {"杭州": (18, 75), "北京": (8, 30), "广州": (26, 80)}


def handle(data: dict, trace_id: str) -> dict:
    try:
        request = EnvInput.model_validate(data)
    except ValidationError as exc:
        raise HTTPException(400, detail={"code": "INVALID_REQUEST", "message": "city 或 days 格式错误"}) from exc
    temperature, humidity = _WEATHER.get(request.city, (20, 50))
    return {
        "city": request.city, "temperature_c": temperature, "humidity_pct": humidity,
        "advice": {
            "clothing": "建议厚外套，注意保暖" if temperature < 15 else (
                "建议轻薄透气衣物" if temperature >= 25 else "建议薄外套，方便增减"),
            "shampoo_spec": "建议清爽控油洗发水" if humidity >= 70 else (
                "建议滋润保湿洗发水" if humidity < 40 else "建议温和日常洗发水"),
        },
    }


app = make_agent_app("env_fit", 8011, ["env_fit"], handle)


@app.exception_handler(RequestValidationError)
async def invalid_request(request, exc):
    return JSONResponse(status_code=400, content={"error": {"code": "INVALID_REQUEST", "message": "请求格式错误"}})
