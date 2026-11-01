"""Optional Open-Meteo weather with a deterministic offline fallback."""
import math
import os
import time

import httpx

_STATIC = {"杭州": (18, 75), "北京": (8, 30), "广州": (26, 80)}


def get_weather(city, *, client=None):
    temperature, humidity = _STATIC.get(city, (20, 50))
    fallback = {"city": city, "temperature_c": temperature, "humidity_pct": humidity, "source": "static"}
    if os.environ.get("ECOMPILOT_WEATHER", "static") != "online":
        return fallback
    owned = client is None
    deadline = time.monotonic() + 2.0
    try:
        if owned:
            client = httpx.Client()

        def fetch(url, params):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("weather deadline")
            response = client.get(url, params=params, timeout=remaining)
            response.raise_for_status()
            if time.monotonic() >= deadline:
                raise TimeoutError("weather deadline")
            return response.json()

        geo = fetch("https://geocoding-api.open-meteo.com/v1/search",
                    {"name": city, "count": 1, "language": "zh"})["results"][0]
        current = fetch("https://api.open-meteo.com/v1/forecast",
                        {"latitude": geo["latitude"], "longitude": geo["longitude"],
                         "current": "temperature_2m,relative_humidity_2m"})["current"]
        temperature = float(current["temperature_2m"])
        humidity = float(current["relative_humidity_2m"])
        if not math.isfinite(temperature) or not math.isfinite(humidity) or not 0 <= humidity <= 100:
            return fallback
        return {"city": city, "temperature_c": int(temperature), "humidity_pct": int(humidity),
                "source": "open-meteo"}
    except Exception:
        return fallback
    finally:
        if owned and client is not None:
            client.close()
