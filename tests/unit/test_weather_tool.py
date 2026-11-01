import httpx
import pytest

from pilot.mcp_tools.weather_tool import get_weather
from pilot.agents import env_fit_agent


@pytest.mark.parametrize('mode', ['static', None])
def test_static_never_network(monkeypatch, mode):
    if mode is None:
        monkeypatch.delenv('ECOMPILOT_WEATHER', raising=False)
    else:
        monkeypatch.setenv('ECOMPILOT_WEATHER', mode)
    def forbidden(request):
        pytest.fail('static must not contact weather service')
    with httpx.Client(transport=httpx.MockTransport(forbidden)) as client:
        assert get_weather('杭州', client=client) == dict(city='杭州', temperature_c=18, humidity_pct=75, source='static')
        assert get_weather('未知', client=client)['temperature_c'] == 20
    assert env_fit_agent.handle({'city': '杭州'}, '') == {
        'city': '杭州', 'temperature_c': 18, 'humidity_pct': 75,
        'advice': {'clothing': '建议薄外套，方便增减', 'shampoo_spec': '建议清爽控油洗发水'}}


@pytest.mark.parametrize('failure', [None, 'timeout', 'missing', 'invalid', 'status'])
def test_online(monkeypatch, failure):
    monkeypatch.setenv('ECOMPILOT_WEATHER', 'online')
    calls = []
    def transport(request):
        calls.append(request)
        assert 0 < request.extensions['timeout']['read'] <= 2
        if failure == 'timeout':
            raise httpx.ReadTimeout('slow')
        if failure == 'status':
            return httpx.Response(503)
        if 'geocoding' in request.url.host:
            assert request.url.params == httpx.QueryParams(name='北京', count=1, language='zh')
            return httpx.Response(200, json={'results': [] if failure == 'missing' else [{'latitude': 40, 'longitude': 116}]})
        assert request.url.params['current'] == 'temperature_2m,relative_humidity_2m'
        assert request.url.params['latitude'] == '40'
        return httpx.Response(200, json={'current': {} if failure == 'invalid' else {'temperature_2m': 21.7, 'relative_humidity_2m': 64.2}})
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        result = get_weather('北京', client=client)
        monkeypatch.setattr(env_fit_agent, 'get_weather', lambda city: get_weather(city, client=client))
        output = env_fit_agent.handle({'city': '北京'}, '')
    assert result == dict(city='北京', temperature_c=8 if failure else 21,
                          humidity_pct=30 if failure else 64, source='static' if failure else 'open-meteo')
    assert ('source' in output) == (failure is None)
