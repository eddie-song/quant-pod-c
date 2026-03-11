from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from urllib.parse import urlparse

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


@dataclass(frozen=True)
class KalshiAuth:
    api_key_id: str
    private_key_path: str
    base_url: str

    @staticmethod
    def from_env() -> "KalshiAuth":
        api_key_id = os.getenv("KALSHI_API_KEY_ID", "").strip()
        private_key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "").strip()
        base_url = os.getenv("KALSHI_BASE_URL", "https://demo-api.kalshi.co/trade-api/v2").strip()

        missing = [k for k, v in [("KALSHI_API_KEY_ID", api_key_id), ("KALSHI_PRIVATE_KEY_PATH", private_key_path)] if not v]
        if missing:
            raise ValueError(f"Missing required env var(s): {', '.join(missing)}")

        return KalshiAuth(api_key_id=api_key_id, private_key_path=private_key_path, base_url=base_url)

    def load_private_key(self):
        with open(self.private_key_path, "rb") as f:
            return serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())

    def sign(self, private_key, timestamp_ms: str, method: str, endpoint_path: str) -> str:
        method = method.upper()
        sign_path = urlparse(self.base_url + endpoint_path).path
        sign_path = sign_path.split("?")[0]

        message = f"{timestamp_ms}{method}{sign_path}".encode("utf-8")
        signature = private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

