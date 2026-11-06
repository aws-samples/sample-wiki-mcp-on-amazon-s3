# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""CDK app for the certificate stack only. Deployed by hand (``make cert-deploy``),
never by CI — see ``infra/stacks/certificate.py`` for why.

    node_modules/.bin/cdk --app "uv run python -m infra.cert_app" deploy -c env=dev \\
        -c account=<id> -c domain=<host> -c authkitDomain=https://<slug>.authkit.app

Context keys: ``env`` (``dev``, the default), ``account``, ``domain`` and
``authkitDomain``, the same launch values as the main app. The same account guard
applies: an env with no account, or whose account differs from the CLI's
credentials, is refused before any stack exists.
"""

from __future__ import annotations

import os
import sys

import aws_cdk as cdk

from infra.config import EnvConfig, guard_account, load_from_context
from infra.stacks.certificate import CertificateStack


def build(app: cdk.App) -> EnvConfig:
    """Add the certificate stack for the env named in context.

    Raises:
        SystemExit: Unknown env, or ``guard_account`` refused the launch values.
        ValueError: The env has no domain.
    """
    cfg = load_from_context(app)
    guard_account(cfg, os.environ.get("CDK_DEFAULT_ACCOUNT"))
    aws_env = cdk.Environment(account=cfg.account, region=cfg.region)
    CertificateStack(app, f"wiki-{cfg.name}-certificate", cfg=cfg, env=aws_env)
    return cfg


if __name__ == "__main__":
    app = cdk.App()
    cfg = build(app)
    print(
        f"wiki certificate: env={cfg.name} domain={cfg.domain} "
        f"account={cfg.account} region={cfg.region}",
        file=sys.stderr,
    )
    app.synth()
