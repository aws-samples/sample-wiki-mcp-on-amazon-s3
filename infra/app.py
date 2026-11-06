# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""CDK app. ``cdk synth -c env=dev -c account=<id> -c authkitDomain=https://<slug>.authkit.app``.

Context keys:
    env              ``dev`` (default; the only shipped environment).
    account          Required. The 12-digit AWS account id for this env.
    authkitDomain    Required. The AuthKit domain that issues this env's tokens.
    domain           Optional public hostname; without it ``canonicalMcpUrl`` is needed.
    codeRoot         Directory holding ``authorizer/`` and ``mcp/`` (default ``build``).
    canonicalMcpUrl  Required when the environment has no ``domain``.
    hostedZoneName   Optional Route 53 zone that owns ``domain``; when set the API stack
                     issues its own certificate and writes the alias record.
    certificateArn   Optional override; without it the API stack reads the ARN that
                     ``infra/cert_app.py`` (deployed by hand) published to SSM.
    alertEmail       Optional subscriber for the budget notification and the alerts topic.
    breakGlassPrincipalArn
                     Optional IAM role or user ARN (wildcards allowed) that may assume the
                     break-glass role; without it no break-glass role is created.
    workosWebClientId
                     The web application's AuthKit client id.

Before any stack is built, ``guard_account`` (``infra/config.py``) refuses an env
with no account or AuthKit domain, and any env whose account differs from the one
the CLI's credentials resolve to (``CDK_DEFAULT_ACCOUNT``) — docs/ARCHITECTURE.md §9.6.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import aws_cdk as cdk

from infra.config import EnvConfig, guard_account, load_from_context
from infra.stacks.api import ApiStack
from infra.stacks.compute import ComputeStack
from infra.stacks.ops import OpsStack
from infra.stacks.storage import StorageStack


def build(app: cdk.App) -> EnvConfig:
    """Add the four stacks for the env named in context to ``app``.

    Args:
        app: The CDK app; context is read from it.

    Returns:
        The environment config the stacks were built from.

    Raises:
        SystemExit: Unknown env, or ``guard_account`` refused the account.
    """
    cfg = load_from_context(app)
    guard_account(cfg, os.environ.get("CDK_DEFAULT_ACCOUNT"))
    aws_env = cdk.Environment(account=cfg.account, region=cfg.region)
    prefix = f"wiki-{cfg.name}"
    code_root = Path(app.node.try_get_context("codeRoot") or "build")

    storage = StorageStack(app, f"{prefix}-storage", cfg=cfg, env=aws_env)
    compute = ComputeStack(
        app, f"{prefix}-compute", cfg=cfg, storage=storage, code_root=code_root, env=aws_env
    )
    api = ApiStack(app, f"{prefix}-api", cfg=cfg, compute=compute, env=aws_env)
    OpsStack(app, f"{prefix}-ops", cfg=cfg, storage=storage, compute=compute, api=api, env=aws_env)
    return cfg


if __name__ == "__main__":
    app = cdk.App()
    cfg = build(app)
    # One line the operator can check against `aws sts get-caller-identity`
    # (docs/DEPLOY.md, first-deploy checklist step 7). stderr: `cdk synth` owns stdout.
    print(
        f"wiki: env={cfg.name} account={cfg.account} region={cfg.region}",
        file=sys.stderr,
    )
    app.synth()
