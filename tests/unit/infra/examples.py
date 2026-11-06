# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Example launch values for infra tests. Real values are given at deploy time."""

from __future__ import annotations

from infra.config import EnvConfig, load

ACCOUNT = "123456789012"
AUTHKIT_DOMAIN = "https://example.authkit.app"
DOMAINS = {"dev": "wiki-dev.example.com"}


def context(name: str) -> dict[str, str]:
    """CDK context a deployer would pass for ``name``."""
    return {
        "env": name,
        "account": ACCOUNT,
        "domain": DOMAINS[name],
        "authkitDomain": AUTHKIT_DOMAIN,
    }


def example(name: str) -> EnvConfig:
    """``load(name)`` with the example launch values applied."""
    return load(name, account=ACCOUNT, domain=DOMAINS[name], authkit_domain=AUTHKIT_DOMAIN)
