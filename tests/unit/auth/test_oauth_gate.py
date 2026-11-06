# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``scripts/oauth_gate.py``: the ``--authkit`` check, ``--token-file`` and ``--no-browser``.

The browser round trip itself needs a person; these pin the parts that refuse a
non-https AuthKit domain, keep a token out of the terminal and let a second user sign
in from a private window.
"""

from __future__ import annotations

import os
import stat
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from scripts import oauth_gate

AUTHKIT = "https://example-staging.authkit.app"
RESOURCE = "https://wiki-dev.example.com/mcp"
META = {
    "issuer": AUTHKIT,
    "authorization_endpoint": f"{AUTHKIT}/oauth2/authorize",
    "token_endpoint": f"{AUTHKIT}/oauth2/token",
    "jwks_uri": f"{AUTHKIT}/oauth2/jwks",
    "client_id_metadata_document_supported": True,
    "token_endpoint_auth_methods_supported": ["none"],
    "code_challenge_methods_supported": ["S256"],
}


@pytest.fixture
def rsa_key() -> Any:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class _FakeJwks:
    """Stands in for ``jwt.PyJWKClient``: returns one key regardless of ``kid``."""

    def __init__(self, key: Any) -> None:
        self._key = key

    def get_signing_key_from_jwt(self, token: str) -> Any:
        class K:
            key = self._key.public_key()

        return K()


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_write_token_creates_an_owner_only_file(tmp_path: Path) -> None:
    target = tmp_path / "owner.tok"
    oauth_gate.write_token(str(target), "token-value-1")
    assert target.read_text() == "token-value-1"
    assert _mode(target) == 0o600


def test_write_token_narrows_and_truncates_an_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "owner.tok"
    target.write_text("an older and much longer value")
    os.chmod(target, 0o644)
    oauth_gate.write_token(str(target), "token-value-2")
    assert target.read_text() == "token-value-2"
    assert _mode(target) == 0o600


@pytest.mark.parametrize(
    "authkit",
    [
        "http://example-staging.authkit.app",
        "example-staging.authkit.app",
        "ftp://example-staging.authkit.app",
        "https://",
        "https://[bad",
    ],
)
def test_authkit_must_be_an_https_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], authkit: str
) -> None:
    def no_network(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a refused --authkit must not reach the network")

    monkeypatch.setattr(oauth_gate.httpx, "get", no_network)
    argv = ["oauth_gate.py", "--authkit", authkit, "--client-id", "client_x"]
    monkeypatch.setattr(sys, "argv", [*argv, "--resource", RESOURCE])
    with pytest.raises(SystemExit) as exc:
        oauth_gate.main()
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "argument --authkit" in err and repr(authkit) in err


def test_no_browser_prints_the_url_and_opens_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(url: str) -> None:
        raise AssertionError("--no-browser must not open the default browser")

    monkeypatch.setattr(oauth_gate.webbrowser, "open", refuse)
    tok, why, refused = oauth_gate._authorize(
        META, "client_x", None, RESOURCE, "openid", 0, 0, open_browser=False
    )
    assert tok is None and not refused and "timed out" in why
    out = capsys.readouterr().out
    assert "open this URL" in out and META["authorization_endpoint"] + "?" in out


def test_token_file_run_writes_the_token_and_never_prints_it(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    rsa_key: Any,
) -> None:
    now = int(time.time())
    access = jwt.encode(
        {"iss": AUTHKIT, "aud": RESOURCE, "sub": "user_x", "iat": now, "exp": now + 300},
        rsa_key,
        algorithm="RS256",
    )
    monkeypatch.setattr(
        oauth_gate.httpx,
        "get",
        lambda url, timeout: httpx.Response(200, json=META),
    )
    monkeypatch.setattr(oauth_gate.jwt, "PyJWKClient", lambda uri: _FakeJwks(rsa_key))

    def fake_authorize(*args: Any, **kwargs: Any) -> tuple[dict[str, str], str, bool]:
        assert kwargs.get("open_browser") is False
        return {"access_token": access, "refresh_token": "r"}, "", False

    monkeypatch.setattr(oauth_gate, "_authorize", fake_authorize)
    target = tmp_path / "tester.tok"
    argv = [
        "oauth_gate.py",
        "--authkit",
        AUTHKIT,
        "--client-id",
        "client_x",
        "--resource",
        RESOURCE,
        "--skip-negative",
        "--no-browser",
        "--token-file",
        str(target),
    ]
    monkeypatch.setattr(sys, "argv", argv)
    oauth_gate.main()
    out = capsys.readouterr().out
    assert target.read_text() == access and _mode(target) == 0o600
    assert access not in out
    assert f"access token written to {target}" in out
    assert "PASS  3 round trip: aud equals the resource byte for byte" in out
