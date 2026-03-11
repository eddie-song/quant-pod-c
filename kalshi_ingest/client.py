from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterator, Optional, Tuple

import requests

from .auth import KalshiAuth


@dataclass
class KalshiClient:
    auth: KalshiAuth
    timeout_s: int = 30

    def __post_init__(self) -> None:
        self._private_key = self.auth.load_private_key()
        self._session = requests.Session()

    def _headers(self, method: str, endpoint_path: str) -> Dict[str, str]:
        timestamp_ms = str(int(time.time() * 1000))
        signature = self.auth.sign(self._private_key, timestamp_ms, method, endpoint_path)
        return {
            "KALSHI-ACCESS-KEY": self.auth.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": signature,
        }

    def get(self, endpoint_path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = self.auth.base_url.rstrip("/") + endpoint_path
        resp = self._session.get(
            url,
            params=params or {},
            headers=self._headers("GET", endpoint_path),
            timeout=self.timeout_s,
        )
        resp.raise_for_status()
        return resp.json()

    def paginate(
        self,
        endpoint_path: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        cursor_param: str = "cursor",
        limit_param: str = "limit",
        limit: int = 1000,
        cursor_field: str = "cursor",
    ) -> Iterator[Tuple[Dict[str, Any], str]]:
        p = dict(params or {})
        if limit_param not in p:
            p[limit_param] = limit

        cursor = p.get(cursor_param, "") or ""
        while True:
            if cursor:
                p[cursor_param] = cursor
            page = self.get(endpoint_path, params=p)
            yield page, cursor

            cursor = page.get(cursor_field, "") or ""
            if not cursor:
                break

    @staticmethod
    def now_utc_iso() -> str:
        return dt.datetime.now(dt.timezone.utc).isoformat()

