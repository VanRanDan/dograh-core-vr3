import pytest
import respx
import httpx

from api.services.billing.lago_client import LagoClient

BASE = "http://lago-api:3000"


@pytest.mark.asyncio
async def test_send_event_posts_expected_payload():
    client = LagoClient(base_url=BASE, api_key="k")
    with respx.mock:
        route = respx.post(f"{BASE}/api/v1/events").mock(
            return_value=httpx.Response(200, json={"event": {}})
        )
        await client.send_event(
            transaction_id="7:voice_minutes:0",
            external_subscription_id="org-7",
            code="voice_minutes",
            value=1.5,
        )
        assert route.called
        body = route.calls.last.request.read().decode()
        assert "voice_minutes" in body and "1.5" in body


@pytest.mark.asyncio
async def test_get_current_usage_parses_response():
    client = LagoClient(base_url=BASE, api_key="k")
    lago_response = {
        "customer_usage": {
            "charges_usage": [
                {
                    "billable_metric": {"code": "voice_minutes"},
                    "units": "42.5",
                },
                {
                    "billable_metric": {"code": "ai_cost_cents"},
                    "units": "750.0",
                },
            ]
        }
    }
    with respx.mock:
        respx.get(f"{BASE}/api/v1/customers/org-7/current_usage").mock(
            return_value=httpx.Response(200, json=lago_response)
        )
        result = await client.get_current_usage("org-7")
    assert result["voice_minutes"] == pytest.approx(42.5)
    assert result["ai_cost_cents"] == pytest.approx(750.0)


@pytest.mark.asyncio
async def test_get_plan_parses_free_units():
    client = LagoClient(base_url=BASE, api_key="k")
    lago_response = {
        "plan": {
            "code": "starter",
            "charges": [
                {
                    "billable_metric_code": "voice_minutes",
                    "properties": {"free_units": "100"},
                },
                {
                    "billable_metric_code": "ai_cost_cents",
                    "properties": {"free_units": "500"},
                },
            ],
        }
    }
    with respx.mock:
        respx.get(f"{BASE}/api/v1/plans/starter").mock(
            return_value=httpx.Response(200, json=lago_response)
        )
        result = await client.get_plan("starter")
    assert result["voice_minutes_free"] == 100
    assert result["ai_cost_cents_free"] == 500
