# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Supply-chain settings in the repo files.

Each of these deploys fine when absent: a tag-pinned action, an unhashed build export,
an unpinned CLI or a workflow token that can write only matters on the day an upstream
is compromised.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))

# Matches commented-out steps too, so a step disabled by commenting it out stays pinned.
USES = re.compile(r"^\s*#?\s*-?\s*uses:\s*(\S+)", re.M)
IMAGE = re.compile(r"\b(?:ghcr\.io|docker\.io|public\.ecr\.aws)/\S+")


def _load(path: Path) -> Any:
    return yaml.safe_load(path.read_text())


def test_workflows_exist() -> None:
    names = {p.name for p in WORKFLOWS}
    assert "ci.yml" in names


def test_every_action_is_pinned_to_a_commit_sha() -> None:
    found = 0
    for path in WORKFLOWS:
        for ref in USES.findall(path.read_text()):
            found += 1
            assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", ref), f"{path.name}: {ref}"
    assert found > 0


def test_every_container_image_is_pinned_to_a_digest() -> None:
    for path in WORKFLOWS:
        for image in IMAGE.findall(path.read_text()):
            assert re.search(r"@sha256:[0-9a-f]{64}$", image), f"{path.name}: {image}"


def test_lambda_build_export_keeps_hashes() -> None:
    makefile = (ROOT / "Makefile").read_text()
    export = [line for line in makefile.splitlines() if "uv export" in line]
    assert export, "Makefile no longer exports requirements"
    for line in export:
        assert "--no-hashes" not in line


def test_dependabot_groups_patch_updates_only() -> None:
    config = _load(ROOT / ".github" / "dependabot.yml")
    for update in config["updates"]:
        for group in update.get("groups", {}).values():
            assert group["update-types"] == ["patch"], update["package-ecosystem"]


CDK_TARGETS = ("synth", "deploy", "destroy", "cert-synth", "cert-deploy")


def test_every_workflow_token_is_read_only() -> None:
    """Workflows run code nobody has reviewed yet (pull requests, dependency updates), so
    none holds a token that can push, merge or open a pull request."""
    for path in WORKFLOWS:
        workflow = _load(path)
        scopes = [workflow.get("permissions")]
        scopes += [job["permissions"] for job in workflow["jobs"].values() if "permissions" in job]
        for scope in scopes:
            assert isinstance(scope, dict), f"{path.name}: permissions must be explicit"
            assert set(scope.values()) <= {"read", "none"}, f"{path.name}: {scope}"


def test_cdk_cli_is_pinned_to_one_locked_version() -> None:
    version = json.loads((ROOT / "package.json").read_text())["devDependencies"]["aws-cdk"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), version
    lock = json.loads((ROOT / "package-lock.json").read_text())
    entry = lock["packages"]["node_modules/aws-cdk"]
    assert entry["version"] == version
    assert entry["integrity"].startswith("sha512-")
    assert entry["resolved"].startswith("https://registry.npmjs.org/")


def test_makefile_and_ci_run_the_locked_cdk_cli() -> None:
    makefile = (ROOT / "Makefile").read_text()
    assert re.search(r"^CDK \?= node_modules/\.bin/cdk$", makefile, re.M)
    for target in CDK_TARGETS:
        rule = re.search(rf"^{re.escape(target)}:([^#\n]*)", makefile, re.M)
        assert rule and "$(NODE_DEPS)" in rule.group(1), target
    for path in [ROOT / "Makefile", *WORKFLOWS]:
        assert "npx" not in path.read_text(), path.name
    steps = _load(ROOT / ".github" / "workflows" / "ci.yml")["jobs"]["checks"]["steps"]
    runs = [s.get("run", "") for s in steps]
    synth = next(i for i, r in enumerate(runs) if " synth " in r)
    assert runs[synth].startswith("node_modules/.bin/cdk synth ")
    assert "npm ci" in runs[:synth]


def test_deploy_targets_ask_before_new_permissions() -> None:
    """Every cdk deploy in the Makefile asks before a stack adds IAM permissions or
    security-group rules, unless the deployer passes REQUIRE_APPROVAL=never."""
    makefile = (ROOT / "Makefile").read_text()
    assert re.search(r"^REQUIRE_APPROVAL \?= broadening$", makefile, re.M)
    deploys = re.findall(r"^\t\$\(CDK\) .*\bdeploy\b.*$", makefile, re.M)
    assert len(deploys) == 2, deploys
    for line in deploys:
        assert line.endswith(" --require-approval $(REQUIRE_APPROVAL)"), line
    assert "--require-approval never" not in makefile


def test_ci_audits_dependencies_and_uploads_an_sbom() -> None:
    steps = _load(ROOT / ".github" / "workflows" / "ci.yml")["jobs"]["checks"]["steps"]
    runs = [s.get("run", "") for s in steps]
    build = next(i for i, r in enumerate(runs) if r == "make build")
    audit = [i for i, r in enumerate(runs) if r.startswith("uv run pip-audit")]
    assert all("--require-hashes" in runs[i] for i in audit)
    assert audit and min(audit) > build
    gate = runs[min(audit)]
    assert "|| true" not in gate and "cyclonedx" not in gate
    assert any("cyclonedx-json" in r for r in runs)
    upload = [s for s in steps if s.get("uses", "").startswith("actions/upload-artifact@")]
    assert upload and upload[0]["with"]["path"] == "build/sbom.cdx.json"
