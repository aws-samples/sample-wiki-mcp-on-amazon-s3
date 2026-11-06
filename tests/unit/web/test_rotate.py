# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Session-key rotation: the four Secrets Manager steps and the loader's
re-read, so warm containers pick the new key up."""

from __future__ import annotations

import base64
import json
import uuid
from typing import Any

import pytest
from botocore.exceptions import ClientError

from app.web import app as web_app
from app.web import rotate
from app.web import secrets as secrets_mod
from app.web import session as sess
from tests.unit.web.conftest import SESSION_KEY, SUBJECT, call, event

ARN = "arn:aws:secretsmanager:us-west-2:123456789012:secret:WebSecret-test-fixture"
OLD_KEY = base64.urlsafe_b64encode(b"k" * 36).decode()
SHH = "shh"  # the web app's client secret in these tests


class FakeSecretsManager:
    """Just enough of Secrets Manager's versions and staging labels."""

    def __init__(self, value: dict[str, str], rotation_enabled: bool = True) -> None:
        self.versions: dict[str, str | None] = {"v1": json.dumps(value)}
        self.stages: dict[str, list[str]] = {"v1": ["AWSCURRENT"]}
        self.rotation_enabled = rotation_enabled
        self.get_calls = 0

    def start_rotation(self) -> str:
        token = str(uuid.uuid4())
        self.versions[token] = None
        self.stages[token] = ["AWSPENDING"]
        return token

    def describe_secret(self, SecretId: str) -> dict[str, Any]:
        return {"RotationEnabled": self.rotation_enabled, "VersionIdsToStages": self.stages}

    def get_secret_value(
        self, SecretId: str, VersionId: str | None = None, VersionStage: str | None = None
    ) -> dict[str, Any]:
        self.get_calls += 1
        if VersionId is None and VersionStage is None:
            VersionStage = "AWSCURRENT"  # what Secrets Manager defaults to
        for vid, labels in self.stages.items():
            if (VersionId is None or vid == VersionId) and (
                VersionStage is None or VersionStage in labels
            ):
                if self.versions[vid] is not None:
                    return {"SecretString": self.versions[vid]}
        raise ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "GetSecretValue")

    def put_secret_value(
        self, SecretId: str, ClientRequestToken: str, SecretString: str, VersionStages: list[str]
    ) -> None:
        self.versions[ClientRequestToken] = SecretString
        self.stages[ClientRequestToken] = list(VersionStages)

    def update_secret_version_stage(
        self, SecretId: str, VersionStage: str, MoveToVersionId: str, RemoveFromVersionId: str
    ) -> None:
        self.stages[RemoveFromVersionId].remove(VersionStage)
        self.stages[RemoveFromVersionId].append("AWSPREVIOUS")
        self.stages[MoveToVersionId] = [VersionStage]

    def current(self) -> dict[str, str]:
        return dict(json.loads(self.get_secret_value(ARN)["SecretString"]))


def _rotate(sm: FakeSecretsManager, token: str) -> None:
    for step in ("createSecret", "setSecret", "testSecret", "finishSecret"):
        rotate.handle({"SecretId": ARN, "ClientRequestToken": token, "Step": step}, None, sm)


def test_rotation_replaces_session_key_and_keeps_client_secret() -> None:
    sm = FakeSecretsManager({"client_secret": "shh", "session_key": OLD_KEY})
    _rotate(sm, sm.start_rotation())
    after = sm.current()
    assert after["client_secret"] == "shh"
    assert after["session_key"] != OLD_KEY
    assert after["previous_session_key"] == OLD_KEY
    assert len(after["session_key"]) == 48
    assert len(base64.urlsafe_b64decode(after["session_key"])) == 36


def test_retried_create_keeps_the_first_pending_key() -> None:
    sm = FakeSecretsManager({"client_secret": "shh", "session_key": OLD_KEY})
    token = sm.start_rotation()
    event = {"SecretId": ARN, "ClientRequestToken": token, "Step": "createSecret"}
    rotate.handle(event, None, sm)
    first = sm.versions[token]
    rotate.handle(event, None, sm)
    assert sm.versions[token] == first


def test_finished_version_is_a_no_op() -> None:
    sm = FakeSecretsManager({"client_secret": "shh", "session_key": OLD_KEY})
    token = sm.start_rotation()
    _rotate(sm, token)
    rotated = sm.current()
    _rotate(sm, token)  # Secrets Manager may retry; nothing changes
    assert sm.current() == rotated


def test_refuses_when_rotation_is_off_or_the_token_is_unknown() -> None:
    sm = FakeSecretsManager({"session_key": OLD_KEY}, rotation_enabled=False)
    with pytest.raises(ValueError, match="not enabled"):
        _rotate(sm, sm.start_rotation())
    sm = FakeSecretsManager({"session_key": OLD_KEY})
    with pytest.raises(ValueError, match="no version"):
        _rotate(sm, "not-a-token")


def test_test_step_rejects_a_short_pending_key() -> None:
    sm = FakeSecretsManager({"session_key": OLD_KEY})
    token = sm.start_rotation()
    sm.put_secret_value(ARN, token, json.dumps({"session_key": "c2hvcnQ"}), ["AWSPENDING"])
    event = {"SecretId": ARN, "ClientRequestToken": token, "Step": "testSecret"}
    with pytest.raises(ValueError, match="32 bytes"):
        rotate.handle(event, None, sm)


def test_loader_rereads_the_secret_after_the_cache_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sm = FakeSecretsManager({"client_secret": "shh", "session_key": OLD_KEY})
    clock = [1000.0]
    monkeypatch.setattr(secrets_mod.time, "monotonic", lambda: clock[0])
    secrets_mod._reset()
    first = secrets_mod.load(ARN, sm)
    _rotate(sm, sm.start_rotation())
    clock[0] += secrets_mod.CACHE_SECONDS - 1
    assert secrets_mod.load(ARN, sm) is first  # still cached
    clock[0] += 1
    fresh = secrets_mod.load(ARN, sm)
    assert fresh.session_key != first.session_key
    assert fresh.previous_session_key == first.session_key
    assert fresh.verify_keys == (fresh.session_key, first.session_key)
    assert fresh.client_secret == SHH
    secrets_mod._reset()


# --- the dual-key verify window --------------------------------------------------------

NEW = b"n" * 36
PREV = b"p" * 36
STRANGER = b"s" * 36


def test_a_cookie_signed_with_the_previous_key_still_verifies() -> None:
    old_cookie = sess.issue_session(PREV, "user_a", "a@x", "A", 1)
    new_cookie = sess.issue_session(NEW, "user_a", "a@x", "A", 1)
    for cookie in (old_cookie, new_cookie):
        assert sess.load_session((NEW, PREV), cookie) is not None
    assert sess.load_session((NEW,), old_cookie) is None  # rotated twice: gone
    assert sess.load_session((NEW, PREV), sess.issue_session(STRANGER, "u", "e", "n", 1)) is None


def test_csrf_tokens_from_either_key_verify_and_others_do_not() -> None:
    principal = sess.load_session((NEW, PREV), sess.issue_session(PREV, "user_a", "e", "n", 1))
    assert principal is not None
    for key in (NEW, PREV):
        assert sess.csrf_valid((NEW, PREV), principal, sess.csrf_token(key, principal))
    assert not sess.csrf_valid((NEW, PREV), principal, sess.csrf_token(STRANGER, principal))
    assert not sess.csrf_valid((NEW,), principal, sess.csrf_token(PREV, principal))


def _dev_secret(monkeypatch: pytest.MonkeyPatch, **fields: bytes | str) -> None:
    encoded = {
        k: base64.urlsafe_b64encode(v).decode() if isinstance(v, bytes) else v
        for k, v in fields.items()
    }
    monkeypatch.setenv("WEB_DEV_SECRETS", json.dumps(encoded))
    secrets_mod._reset()


def test_loader_reads_an_optional_previous_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _dev_secret(monkeypatch, session_key=NEW)
    assert secrets_mod.load("").verify_keys == (NEW,)
    _dev_secret(monkeypatch, session_key=NEW, previous_session_key=PREV)
    assert secrets_mod.load("").verify_keys == (NEW, PREV)
    _dev_secret(monkeypatch, session_key=NEW, previous_session_key=b"short")
    with pytest.raises(RuntimeError, match="previous_session_key"):
        secrets_mod.load("")
    secrets_mod._reset()


def test_web_app_keeps_a_session_across_a_rotation(
    monkeypatch: pytest.MonkeyPatch, profile: Any
) -> None:
    """Signed before the rotation, presented after it: still signed in, and the
    page's CSRF token is minted with the new key."""
    cookie = sess.issue_session(SESSION_KEY, SUBJECT, "test@example.com", "Test Person", 12)
    _dev_secret(monkeypatch, client_secret=SHH, session_key=NEW, previous_session_key=SESSION_KEY)
    web_app._reset()
    assert call(event("GET", "/app/", cookies={sess.SESSION_COOKIE: cookie}))["statusCode"] == 200
    _dev_secret(monkeypatch, client_secret=SHH, session_key=NEW)  # rotated again
    web_app._reset()
    assert call(event("GET", "/app/", cookies={sess.SESSION_COOKIE: cookie}))["statusCode"] == 303
