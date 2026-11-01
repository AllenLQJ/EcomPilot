import httpx
import pytest
from mcp.client import Client

from protocols import mcp_server
from pilot.mcp_tools import weather_tool


@pytest.mark.parametrize('online', [False, True])
async def test_weather_read_only_without_database(monkeypatch, online):
    monkeypatch.setenv('ECOMPILOT_WEATHER', 'online' if online else 'static')
    def forbidden_db():
        pytest.fail('weather must not open database')
    monkeypatch.setattr(mcp_server, 'get_engine', forbidden_db)
    def transport(request):
        assert online, 'static weather must never contact network'
        if 'geocoding' in request.url.host:
            return httpx.Response(200,json={'results':[{'latitude':30,'longitude':120}]})
        return httpx.Response(200,json={'current':{'temperature_2m':22.5,'relative_humidity_2m':65}})
    get_weather = weather_tool.get_weather
    with httpx.Client(transport=httpx.MockTransport(transport)) as weather_client:
        monkeypatch.setattr(weather_tool, 'get_weather', lambda city: get_weather(city, client=weather_client))
        async with Client(mcp_server.mcp) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            assert tools['get_weather'].annotations.read_only_hint
            result = await client.call_tool('get_weather', {'city':'杭州'})
    assert not result.is_error
    assert result.structured_content == dict(city='杭州', temperature_c=22 if online else 18,
                                            humidity_pct=65 if online else 75,
                                            source='open-meteo' if online else 'static')
