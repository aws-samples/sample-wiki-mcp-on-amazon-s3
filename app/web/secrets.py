# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The web function's one secret (docs/ARCHITECTURE.md §12.3 "no secrets in files").

One Secrets Manager JSON secret holds ``client_secret`` (the AuthKit confidential
client), ``session_key`` (cookie signing) and, once rotation has run,
``previous_session_key`` (verify only). Fetched once per container and again
after ``CACHE_SECONDS``, so every warm container picks up a rotated value within
the hour.

There is deliberately no WorkOS management API key here or anywhere (§11.6 "Adding a
person"): people are created in the WorkOS dashboard, and the console only records
them. The client secret can do nothing but exchange this client's own login codes.
"""

from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass
from typing import Any

import boto3


@dataclass(frozen=True)
class WebSecrets:
    client_secret: str
    session_key: bytes
    #: The key before the last rotation (``app.web.rotate``); still verifies, never signs.
    previous_session_key: bytes = b""

    @property
    def verify_keys(self) -> tuple[bytes, ...]:
        """Keys a cookie or CSRF token may be signed with, current first."""
        if self.previous_session_key:
            return (self.session_key, self.previous_session_key)
        return (self.session_key,)


def _key_bytes(data: dict[str, Any], field: str, *, required: bool) -> bytes:
    key = str(data.get(field, "") or "")
    if not key and not required:
        return b""
    key_bytes = base64.urlsafe_b64decode(key + "=" * (-len(key) % 4)) if key else b""
    if len(key_bytes) < 32:
        raise RuntimeError(f"{field} must be at least 32 random bytes, base64url")
    return key_bytes


#: How long a container trusts its copy before reading the secret again.
CACHE_SECONDS = 3600

_cache: WebSecrets | None = None
_loaded_at = 0.0


def _reset() -> None:
    global _cache
    _cache = None


def load(secret_arn: str, client: Any | None = None) -> WebSecrets:
    """Fetch and cache for ``CACHE_SECONDS``. With an empty ARN (unit tests, local runs)
    the values come from ``WEB_DEV_SECRETS`` — a JSON string — or are empty."""
    global _cache, _loaded_at
    if _cache is not None and time.monotonic() - _loaded_at < CACHE_SECONDS:
        return _cache
    if not secret_arn:
        raw = os.environ.get("WEB_DEV_SECRETS", "{}")
        data = json.loads(raw)
    else:
        sm = client or boto3.client("secretsmanager")
        data = json.loads(sm.get_secret_value(SecretId=secret_arn)["SecretString"])
    _cache = WebSecrets(
        client_secret=str(data.get("client_secret", "")),
        session_key=_key_bytes(data, "session_key", required=True),
        previous_session_key=_key_bytes(data, "previous_session_key", required=False),
    )
    _loaded_at = time.monotonic()
    return _cache


__all__ = ["WebSecrets", "load"]
