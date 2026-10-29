"""Cloudflare Queues REST client (HTTP pull consumer + producer)."""

import base64
import binascii
import json
from dataclasses import dataclass
from typing import Any

import httpx

API_BASE = "https://api.cloudflare.com/client/v4"
_PAGE_SIZE = 100


class CloudflareError(Exception):
    pass


@dataclass(frozen=True)
class PulledMessage:
    id: str
    lease_id: str
    attempts: int
    body: Any


def decode_body(body: Any) -> Any:
    """Pull consumers receive ``json`` bodies base64-encoded; accept plain JSON too."""
    if not isinstance(body, str):
        return body
    try:
        return json.loads(base64.b64decode(body, validate=True))
    except (binascii.Error, ValueError):
        return json.loads(body)


class Queues:
    def __init__(self, account_id: str, api_token: str, client: httpx.AsyncClient | None = None):
        self._base = f"{API_BASE}/accounts/{account_id}/queues"
        self._client = client or httpx.AsyncClient(timeout=30)
        self._headers = {"Authorization": f"Bearer {api_token}"}

    async def _call(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        resp = await self._client.request(method, self._base + path, headers=self._headers, **kwargs)
        try:
            data = resp.json()
        except ValueError:
            raise CloudflareError(f"{method} queues{path}: HTTP {resp.status_code}") from None
        if resp.status_code >= 400 or not data.get("success", False):
            raise CloudflareError(f"{method} queues{path}: HTTP {resp.status_code} {data.get('errors')}")
        return data

    async def find_queue_id(self, name: str) -> str | None:
        page = 1
        while True:
            data = await self._call("GET", "", params={"page": page, "per_page": _PAGE_SIZE})
            queues = data.get("result") or []
            for q in queues:
                if q.get("queue_name") == name:
                    return q["queue_id"]
            info = data.get("result_info") or {}
            total_pages = info.get("total_pages")
            more = page < total_pages if total_pages else len(queues) >= _PAGE_SIZE
            if not queues or not more:
                return None
            page += 1

    async def pull(
        self, queue_id: str, *, batch_size: int = 10, visibility_timeout_ms: int = 120_000
    ) -> list[PulledMessage]:
        data = await self._call(
            "POST",
            f"/{queue_id}/messages/pull",
            json={"batch_size": batch_size, "visibility_timeout_ms": visibility_timeout_ms},
        )
        return [
            PulledMessage(
                id=m["id"], lease_id=m["lease_id"], attempts=m.get("attempts", 0), body=m["body"]
            )
            for m in (data.get("result") or {}).get("messages") or []
        ]

    async def ack(self, queue_id: str, lease_id: str) -> None:
        await self._call(
            "POST", f"/{queue_id}/messages/ack", json={"acks": [{"lease_id": lease_id}], "retries": []}
        )

    async def retry(self, queue_id: str, lease_id: str, delay_seconds: int) -> None:
        await self._call(
            "POST",
            f"/{queue_id}/messages/ack",
            json={"acks": [], "retries": [{"lease_id": lease_id, "delay_seconds": delay_seconds}]},
        )

    async def push(self, queue_id: str, body: dict[str, Any]) -> None:
        await self._call(
            "POST", f"/{queue_id}/messages", json={"body": body, "content_type": "json"}
        )
