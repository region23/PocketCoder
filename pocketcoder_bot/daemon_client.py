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

    async def create_job(
        self,
        *,
        engine: str,
        repo: str,
        mode: str,
        prompt: str,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "engine": engine,
            "repo": repo,
            "mode": mode,
            "prompt": prompt,
        }
        if timeout_seconds is not None:
            payload["timeout_seconds"] = timeout_seconds
        return await self._request("POST", "/jobs", json=payload)

    async def list_jobs(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/jobs")

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
        async with httpx.AsyncClient(base_url=self.base_url, timeout=20.0) as client:
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
