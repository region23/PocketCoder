from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any

import httpx


class DaemonAPIError(Exception):
    pass


@dataclass
class DaemonClient:
    base_url: str
    uds_path: str | None = None

    async def create_job(
        self,
        *,
        engine: str,
        repo: str,
        mode: str,
        prompt: str,
        timeout_seconds: float | None = None,
        requester_user_id: int | None = None,
        requester_chat_id: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "engine": engine,
            "repo": repo,
            "mode": mode,
            "prompt": prompt,
        }
        if timeout_seconds is not None:
            payload["timeout_seconds"] = timeout_seconds
        if requester_user_id is not None:
            payload["requester_user_id"] = requester_user_id
        if requester_chat_id is not None:
            payload["requester_chat_id"] = requester_chat_id
        return await self._request("POST", "/jobs", json=payload)

    async def list_jobs(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/jobs")

    async def list_engines(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/engines")

    async def get_job(self, job_id: int) -> dict[str, Any]:
        return await self._request("GET", f"/jobs/{job_id}")

    async def cancel_job(self, job_id: int) -> dict[str, Any]:
        return await self._request("POST", f"/jobs/{job_id}/cancel")

    async def send_input(self, job_id: int, text: str) -> dict[str, Any]:
        return await self._request("POST", f"/jobs/{job_id}/input", json={"text": text})

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> Any:
        headers = {}
        api_key = os.getenv("POCKETCODER_API_KEY")
        if api_key:
            headers["x-api-key"] = api_key
        transport = (
            httpx.AsyncHTTPTransport(uds=self.uds_path)
            if self.uds_path
            else None
        )
        async with httpx.AsyncClient(
            base_url=self.base_url,
            timeout=20.0,
            transport=transport,
        ) as client:
            response = await client.request(method, path, json=json, headers=headers)
        if response.status_code >= 400:
            detail = response.text
            try:
                payload = response.json()
                detail = payload.get("detail", detail)
            except ValueError:
                pass
            raise DaemonAPIError(f"{response.status_code}: {detail}")
        return response.json()
