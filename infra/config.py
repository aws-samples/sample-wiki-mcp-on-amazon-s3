# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Infrastructure configuration — docs/ARCHITECTURE.md §9.6.

The sample ships one environment, ``dev``. ``object_lock`` and ``retain_data`` are
off so the sample can be torn down; turn both on if you adapt it
(``docs/DEPLOY.md`` §9, adapting this sample). Domain and hosted zone are optional so the
prototype can synth and deploy before DNS exists; when absent the API's default
execute-api URL is used and CANONICAL_MCP_URL must be set explicitly at deploy.

This file holds no deployment identifiers. The account, domain, AuthKit domain and
web client id are given at launch as CDK context (``-c account=``, ``-c domain=``,
``-c authkitDomain=``, ``-c workosWebClientId=``), or as the make variables
``ACCOUNT``, ``DOMAIN``, ``AUTHKIT_DOMAIN`` and ``WORKOS_CLIENT_ID``. The hosted zone
is optional at launch too (``-c hostedZoneName=``, or ``HOSTED_ZONE_NAME``). So is the
break-glass principal (``-c breakGlassPrincipalArn=``, or ``BREAK_GLASS_PRINCIPAL_ARN``):
the IAM role or user ARN, wildcards allowed, that may assume the break-glass role.
Without it the ops stack creates no break-glass role.

``account`` is the guard against deploying to the wrong account (§9.6).
``guard_account`` refuses to synth without one, and refuses an account that
differs from the one the CDK CLI's credentials resolve to.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any

ACCOUNT_ID_RE = re.compile(r"\d{12}")
#: The break-glass trust condition's value: an IAM role or user ARN, wildcards allowed.
#: The name after the last ``/`` must not be only wildcards: ``role/*`` would match every
#: role in the account. A wildcard suffix such as ``AWSReservedSSO_<name>_*`` is fine.
#: Never an ``sts`` assumed-role ARN: ``aws:PrincipalArn`` is the role's ARN, not a session's.
PRINCIPAL_ARN_RE = re.compile(
    r"arn:aws[a-z-]*:iam::(\d{12}):(?:role|user)/(?:\S*/)?(?![*?]+$)[^/\s]+"
)


@dataclass(frozen=True)
class EnvConfig:
    """One deployable environment.

    Attributes:
        name: ``dev``, the only shipped environment.
        account: AWS account id, given at launch. ``guard_account`` refuses ``None``.
        region: AWS region.
        domain: Public hostname, e.g. ``wiki-dev.example.com``; ``None`` for no custom domain.
        hosted_zone_name: Route53 zone that owns ``domain``, optionally given at launch
            (it overrides the value here); ``None`` to use the hand-deployed certificate
            and skip DNS records.
        authkit_domain: Token issuer for this environment, given at launch.
            ``guard_account`` refuses an empty one.
        object_lock: Enable Object Lock (governance, 1-year default retention). Off in
            the sample; turn on if you adapt it.
        retain_data: RETAIN removal policy on bucket/table/key. Off in the sample; turn
            on if you adapt it.
        allowed_origins: CORS + Origin-validation allowlist.
        credential_cache_seconds: Passed through to the function.
        workos_web_client_id: The web app's own AuthKit client (increment F), given
            at launch. Empty is a synth warning.
        strict_mcp_headers: Reject POSTs that omit the ``Mcp-*`` headers §6.2 calls
            mandatory (``MCP_STRICT_HEADERS``). Off until a real client is observed
            sending them; see the first-deploy checklist in ``docs/DEPLOY.md``.
        log_retention_days: CloudTrail log group and log-bucket retention (§12.7: ninety).
        backup_retention_days: How long each daily grant-table snapshot is kept (§12.5).
        break_glass_principal_arn: Who may assume the break-glass role
            (``aws:PrincipalArn``, ``ArnLike``), given at launch. ``None``: no
            break-glass role.
    """

    name: str
    account: str | None
    region: str
    domain: str | None
    hosted_zone_name: str | None
    authkit_domain: str
    object_lock: bool
    retain_data: bool
    allowed_origins: tuple[str, ...] = ("https://claude.ai",)
    credential_cache_seconds: int = 900
    workos_web_client_id: str = ""
    strict_mcp_headers: bool = False
    log_retention_days: int = 90
    backup_retention_days: int = 35
    break_glass_principal_arn: str | None = None

    @property
    def canonical_mcp_url(self) -> str | None:
        return f"https://{self.domain}/mcp" if self.domain else None


ENVIRONMENTS: dict[str, EnvConfig] = {
    "dev": EnvConfig(
        name="dev",
        account=None,  # given at launch: -c account=<id>
        region="us-west-2",
        domain=None,  # given at launch: -c domain=<host>
        hosted_zone_name=None,  # optional at launch: -c hostedZoneName=<zone>
        authkit_domain="",  # given at launch: -c authkitDomain=https://<slug>.authkit.app
        # Off so the sample can be torn down. Turn both on if you adapt it:
        # docs/DEPLOY.md §9, adapting this sample.
        object_lock=False,
        retain_data=False,
        # Flip to True once a real Claude client has been observed sending the Mcp-*
        # headers — docs/ARCHITECTURE.md §6.2 calls them mandatory; the safety valve stays off until
        # then. First-deploy checklist, post-launch item.
        strict_mcp_headers=False,
    ),
}


def load(
    name: str,
    *,
    account: str | None = None,
    domain: str | None = None,
    authkit_domain: str | None = None,
    hosted_zone_name: str | None = None,
    break_glass_principal_arn: str | None = None,
) -> EnvConfig:
    """Return the config for ``name`` with the launch-time values applied.

    Args:
        name: Environment name, a key of ``ENVIRONMENTS``.
        account: The CDK context key ``account``.
        domain: The CDK context key ``domain``.
        authkit_domain: The CDK context key ``authkitDomain``.
        hosted_zone_name: The CDK context key ``hostedZoneName``.
        break_glass_principal_arn: The CDK context key ``breakGlassPrincipalArn``.

    Empty values are ignored, so an unset make variable leaves the field unset.

    Raises:
        SystemExit: Unknown environment name.
    """
    try:
        cfg = ENVIRONMENTS[name]
    except KeyError as e:
        raise SystemExit(f"unknown env {name!r}; expected one of {sorted(ENVIRONMENTS)}") from e
    if account:
        cfg = replace(cfg, account=account)
    if domain:
        cfg = replace(cfg, domain=domain)
    if authkit_domain:
        cfg = replace(cfg, authkit_domain=authkit_domain)
    if hosted_zone_name:
        cfg = replace(cfg, hosted_zone_name=hosted_zone_name)
    if break_glass_principal_arn:
        cfg = replace(cfg, break_glass_principal_arn=break_glass_principal_arn)
    return cfg


def load_from_context(app: Any) -> EnvConfig:
    """``load`` with the env name and launch values read from a CDK app's context."""
    ctx = app.node.try_get_context
    return load(
        ctx("env") or "dev",
        account=ctx("account"),
        domain=ctx("domain"),
        authkit_domain=ctx("authkitDomain"),
        hosted_zone_name=ctx("hostedZoneName"),
        break_glass_principal_arn=ctx("breakGlassPrincipalArn"),
    )


def guard_account(cfg: EnvConfig, resolved_account: str | None) -> None:
    """Refuse to synth without launch values or against the wrong account (§9.6).

    Args:
        cfg: The environment about to be synthesized.
        resolved_account: The account the CDK CLI's credentials resolve to
            (``CDK_DEFAULT_ACCOUNT``), or ``None`` when the CLI had no credentials —
            a credential-less synth in CI, where only the given id itself can be checked.

    Raises:
        SystemExit: No account; an account that is not a 12-digit id; no AuthKit
            domain; a break-glass principal that is not an IAM role or user ARN in
            this account, or whose name is only a wildcard; or credentials that
            resolve to a different account than the one given for this env.
    """
    if cfg.account is None:
        raise SystemExit(
            f"env {cfg.name!r} has no AWS account. Pass -c account=<12-digit id> "
            f"(make deploy ENV={cfg.name} ACCOUNT=<id>)."
        )
    if not ACCOUNT_ID_RE.fullmatch(cfg.account):
        raise SystemExit(
            f"account={cfg.account!r} for env {cfg.name}; an AWS account id is exactly 12 digits."
        )
    if not cfg.authkit_domain:
        raise SystemExit(
            f"env {cfg.name!r} has no AuthKit domain. "
            "Pass -c authkitDomain=https://<slug>.authkit.app "
            f"(make deploy ENV={cfg.name} AUTHKIT_DOMAIN=https://<slug>.authkit.app)."
        )
    principal = cfg.break_glass_principal_arn
    if principal is not None:
        match = PRINCIPAL_ARN_RE.fullmatch(principal)
        if match is None or match.group(1) != cfg.account:
            raise SystemExit(
                f"breakGlassPrincipalArn={principal!r} for env {cfg.name}; name an IAM role "
                f"or user in account {cfg.account}, e.g. arn:aws:iam::{cfg.account}:role/<name> "
                "(wildcards allowed, but not a name that is only a wildcard)."
            )
    if resolved_account and resolved_account != cfg.account:
        raise SystemExit(
            f"credentials resolve to account {resolved_account} but account {cfg.account} "
            f"was given for env {cfg.name}"
        )
