import httpx
from loguru import logger

from api.constants import LAGO_API_KEY, LAGO_API_URL


class LagoClient:
    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        self.base_url = (base_url or LAGO_API_URL).rstrip("/")
        self.api_key = api_key or LAGO_API_KEY

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    async def send_event(self, *, transaction_id, external_subscription_id, code, value) -> None:
        payload = {
            "event": {
                "transaction_id": transaction_id,
                "external_subscription_id": external_subscription_id,
                "code": code,
                "properties": {"value": value},
            }
        }
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                f"{self.base_url}/api/v1/events", json=payload, headers=self._headers()
            )
            r.raise_for_status()

    async def get_current_usage(self, external_subscription_id: str) -> dict:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                f"{self.base_url}/api/v1/customers/{external_subscription_id}/current_usage",
                params={"external_subscription_id": external_subscription_id},
                headers=self._headers(),
            )
            r.raise_for_status()
            charges = r.json().get("customer_usage", {}).get("charges_usage", [])
        out: dict[str, float] = {"voice_minutes": 0.0, "ai_cost_cents": 0.0}
        for ch in charges:
            code = ch.get("billable_metric", {}).get("code")
            if code in out:
                out[code] = float(ch.get("units", 0))
        return out

    async def get_plan(self, plan_code: str) -> dict:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                f"{self.base_url}/api/v1/plans/{plan_code}", headers=self._headers()
            )
            r.raise_for_status()
            charges = r.json().get("plan", {}).get("charges", [])
        free: dict[str, int] = {"voice_minutes_free": 0, "ai_cost_cents_free": 0}
        for ch in charges:
            code = ch.get("billable_metric_code")
            props = ch.get("properties", {})
            if code == "voice_minutes":
                free["voice_minutes_free"] = int(float(props.get("free_units", 0)))
            elif code == "ai_cost_cents":
                free["ai_cost_cents_free"] = int(float(props.get("free_units", 0)))
        return free

    async def upsert_customer(self, external_id: str, **fields) -> str:
        payload = {"customer": {"external_id": external_id, **fields}}
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                f"{self.base_url}/api/v1/customers", json=payload, headers=self._headers()
            )
            r.raise_for_status()
        return external_id

    async def create_subscription(
        self, *, external_customer_id: str, external_id: str, plan_code: str
    ) -> None:
        payload = {
            "subscription": {
                "external_customer_id": external_customer_id,
                "external_id": external_id,
                "plan_code": plan_code,
            }
        }
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                f"{self.base_url}/api/v1/subscriptions", json=payload, headers=self._headers()
            )
            r.raise_for_status()


lago_client = LagoClient()
