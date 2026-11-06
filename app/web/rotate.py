# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Scheduled rotation of ``session_key`` in the web secret.

Secrets Manager invokes this four times per rotation with one ``ClientRequestToken``:
``createSecret``, ``setSecret``, ``testSecret``, ``finishSecret``. Only ``session_key``
changes; the outgoing value moves to ``previous_session_key``, which the web app
still accepts for verification (the dual-key window this asks for). ``client_secret``
and every other field are copied from AWSCURRENT, so the WorkOS client keeps working.
Nothing outside the secret holds the key, so ``setSecret`` has nothing to do.
"""

from __future__ import annotations

import base64
import json
import secrets
from typing import Any

import boto3
from botocore.exceptions import ClientError

#: 36 random bytes → 48 base64url characters: the same shape as the key CDK generates.
KEY_BYTES = 36


def new_session_key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(KEY_BYTES)).rstrip(b"=").decode()


def _secret(sm: Any, arn: str, **version: str) -> dict[str, Any]:
    return dict(json.loads(sm.get_secret_value(SecretId=arn, **version)["SecretString"]))


def _create(sm: Any, arn: str, token: str) -> None:
    try:
        sm.get_secret_value(SecretId=arn, VersionId=token, VersionStage="AWSPENDING")
        return  # a retried createSecret: the pending version already exists
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
    current = _secret(sm, arn, VersionStage="AWSCURRENT")
    # The outgoing key keeps verifying for one interval, so nobody is logged out.
    pending = {
        **current,
        "session_key": new_session_key(),
        "previous_session_key": current["session_key"],
    }
    sm.put_secret_value(
        SecretId=arn,
        ClientRequestToken=token,
        SecretString=json.dumps(pending),
        VersionStages=["AWSPENDING"],
    )


def _test(sm: Any, arn: str, token: str) -> None:
    pending = _secret(sm, arn, VersionId=token, VersionStage="AWSPENDING")
    key = str(pending.get("session_key", ""))
    if len(base64.urlsafe_b64decode(key + "=" * (-len(key) % 4))) < 32:
        raise ValueError("pending session_key is shorter than 32 bytes")


def _finish(sm: Any, arn: str, token: str, stages: dict[str, list[str]]) -> None:
    current = next((v for v, s in stages.items() if "AWSCURRENT" in s), None)
    sm.update_secret_version_stage(
        SecretId=arn,
        VersionStage="AWSCURRENT",
        MoveToVersionId=token,
        RemoveFromVersionId=current,
    )


def handle(event: dict[str, Any], context: Any, client: Any | None = None) -> None:
    sm = client or boto3.client("secretsmanager")
    arn, token, step = event["SecretId"], event["ClientRequestToken"], event["Step"]
    meta = sm.describe_secret(SecretId=arn)
    if not meta.get("RotationEnabled"):
        raise ValueError("rotation is not enabled on this secret")
    stages: dict[str, list[str]] = meta.get("VersionIdsToStages", {})
    if token not in stages:
        raise ValueError("no version of the secret has this token")
    if "AWSCURRENT" in stages[token]:
        return  # already finished
    if "AWSPENDING" not in stages[token]:
        raise ValueError("this version is not pending rotation")
    if step == "createSecret":
        _create(sm, arn, token)
    elif step == "setSecret":
        return
    elif step == "testSecret":
        _test(sm, arn, token)
    elif step == "finishSecret":
        _finish(sm, arn, token, stages)
    else:
        raise ValueError(f"unknown rotation step {step!r}")


__all__ = ["handle", "new_session_key"]
