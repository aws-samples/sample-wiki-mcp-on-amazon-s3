# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Signed session cookie and CSRF token (docs/ARCHITECTURE.md §4.9 cookie row, §11.6).

The session is a signed, not encrypted, cookie: ``<base64url(json)>.<hmac-sha256>``.
It carries the subject, display name, expiry and the profile's ``session_epoch`` at
issue — nothing secret. The signing key comes from Secrets Manager
(``app.web.secrets``). Revocation is prospective (§12.9): a disabled user is refused
on the next request because ``app.web.app`` checks the PROFILE status per request,
and the same read refuses a cookie whose epoch is behind the profile's — "Sign out
everywhere" (§11.6) bumps the epoch. The cookie itself is only a proof of login.

CSRF: every state-changing form carries ``HMAC(key, "csrf:" + session signature)``;
the POST handler compares it against the same derivation. A token is therefore bound
to one session and cannot be replayed across logins.

Key rotation: signing always uses the current key. Verification takes
``Keys`` — the current key and, for one rotation interval, the previous one — so a
scheduled rotation logs nobody out. Rotating twice drops a leaked key from both.

Step-up: the cookie also carries ``auth_time``, when this person's sign-in through
AuthKit last completed (set by the login callback; not the IdP's claim, which
AuthKit does not keep current; 0 in a cookie that predates it). Admin actions
require ``recently_authenticated``; anything older is sent through AuthKit again
(``app.web.app.view``).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

SESSION_COOKIE = "__Host-wiki_session"
OAUTH_COOKIE = "__Host-wiki_oauth"
#: How recent a sign-in an admin action needs (step-up).
STEP_UP_SECONDS = 300

#: One key, or the keys a signature may verify against (current first).
Keys = bytes | tuple[bytes, ...]


def _keys(keys: Keys) -> tuple[bytes, ...]:
    return (keys,) if isinstance(keys, bytes) else keys


@dataclass(frozen=True)
class Principal:
    subject: str
    email: str
    display_name: str
    expires_at: int
    epoch: int = 0  # the profile's session_epoch when issued; 0 for cookies from before
    signature: str = ""  # the cookie's HMAC; the CSRF token derives from it
    auth_time: int = 0  # when their sign-in through AuthKit completed; 0 when unknown

    @property
    def actor(self) -> str:
        return f"human:{self.subject}"


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _sign(key: bytes, payload: str) -> str:
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def encode(key: bytes, data: dict[str, object]) -> str:
    """Serialise and sign any JSON-able mapping."""
    payload = _b64e(json.dumps(data, separators=(",", ":"), sort_keys=True).encode())
    return f"{payload}.{_sign(key, payload)}"


def decode(keys: Keys, token: str, *, now: float | None = None) -> dict[str, object] | None:
    """Verify signature (against any of ``keys``) and ``exp``; ``None`` on any failure."""
    if not token or "." not in token:
        return None
    payload, sig = token.rsplit(".", 1)
    if not any(hmac.compare_digest(sig, _sign(k, payload)) for k in _keys(keys)):
        return None
    try:
        data = json.loads(_b64d(payload))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    exp = data.get("exp")
    if not isinstance(exp, int | float) or (now if now is not None else time.time()) >= exp:
        return None
    return data


def issue_session(
    key: bytes,
    principal_subject: str,
    email: str,
    display_name: str,
    hours: int,
    epoch: int = 0,
    auth_time: int = 0,
) -> str:
    exp = int(time.time()) + hours * 3600
    return encode(
        key,
        {
            "sub": principal_subject,
            "email": email,
            "name": display_name,
            "exp": exp,
            "epoch": epoch,
            "auth_time": auth_time,
        },
    )


def _int_or_zero(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def load_session(keys: Keys, cookie: str | None) -> Principal | None:
    if not cookie:
        return None
    data = decode(keys, cookie)
    if not data:
        return None
    sub = data.get("sub")
    exp = data.get("exp")
    if not isinstance(sub, str) or not sub or not isinstance(exp, int | float):
        return None
    return Principal(
        subject=sub,
        email=str(data.get("email", "")),
        display_name=str(data.get("name", "")),
        expires_at=int(exp),
        epoch=_int_or_zero(data.get("epoch")),
        signature=cookie.rsplit(".", 1)[1],
        auth_time=_int_or_zero(data.get("auth_time")),
    )


def recently_authenticated(principal: Principal, *, now: float | None = None) -> bool:
    """True when this person's sign-in through AuthKit completed within
    ``STEP_UP_SECONDS``. A cookie without ``auth_time`` (0) is never recent: it fails
    closed."""
    current = now if now is not None else time.time()
    return principal.auth_time > 0 and current - principal.auth_time <= STEP_UP_SECONDS


def csrf_token(key: bytes, principal: Principal) -> str:
    return _sign(key, "csrf:" + principal.signature)


def csrf_valid(keys: Keys, principal: Principal, submitted: str | None) -> bool:
    return bool(submitted) and any(
        hmac.compare_digest(submitted or "", csrf_token(k, principal)) for k in _keys(keys)
    )


__all__ = [
    "Keys",
    "OAUTH_COOKIE",
    "SESSION_COOKIE",
    "STEP_UP_SECONDS",
    "Principal",
    "csrf_token",
    "csrf_valid",
    "decode",
    "encode",
    "issue_session",
    "load_session",
    "recently_authenticated",
]
