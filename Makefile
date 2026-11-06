# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
.DEFAULT_GOAL := help
ENV ?= dev
PY_VERSION := 3.13
LAMBDA_PLATFORM := aarch64-manylinux2014
BUILD := build
CDK ?= node_modules/.bin/cdk
# The CDK CLI is pinned in package-lock.json; npm ci installs exactly that version and
# rewrites npm's own install record below, so a newer lock re-installs on the next run.
NODE_DEPS := node_modules/.package-lock.json
# cdk deploy shows a stack's new IAM permissions or security-group rules and asks before
# deploying it. REQUIRE_APPROVAL=any-change asks for every change; never skips the question.
REQUIRE_APPROVAL ?= broadening
CERT_ARN ?=
# Launch values (docs/DEPLOY.md §3). Pass on the command line or export them.
ACCOUNT ?=
DOMAIN ?=
AUTHKIT_DOMAIN ?=
WORKOS_CLIENT_ID ?=
ALERT_EMAIL ?=
HOSTED_ZONE_NAME ?=
BREAK_GLASS_PRINCIPAL_ARN ?=
LAUNCH_CTX := -c env=$(ENV) $(if $(ACCOUNT),-c account=$(ACCOUNT),) \
  $(if $(DOMAIN),-c domain=$(DOMAIN),) $(if $(AUTHKIT_DOMAIN),-c authkitDomain=$(AUTHKIT_DOMAIN),)
CDK_CTX := $(LAUNCH_CTX) $(if $(CERT_ARN),-c certificateArn=$(CERT_ARN),) \
  $(if $(WORKOS_CLIENT_ID),-c workosWebClientId=$(WORKOS_CLIENT_ID),) \
  $(if $(ALERT_EMAIL),-c alertEmail=$(ALERT_EMAIL),) \
  $(if $(HOSTED_ZONE_NAME),-c hostedZoneName=$(HOSTED_ZONE_NAME),) \
  $(if $(BREAK_GLASS_PRINCIPAL_ARN),-c breakGlassPrincipalArn='$(BREAK_GLASS_PRINCIPAL_ARN)',)

CERT_APP := --app "uv run python -m infra.cert_app"

.PHONY: help sync test lint typecheck build synth deploy destroy security aws-tests clean grant-owner cert-synth cert-deploy cert-status

help: ## Show targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-14s %s\n", $$1, $$2}'

sync: $(NODE_DEPS) ## Install dependencies (app + dev + infra, and the pinned CDK CLI)
	uv sync

$(NODE_DEPS): package.json package-lock.json
	npm ci

test: ## Unit tests (no AWS, no network)
	uv run pytest

lint: ## Ruff
	uv run ruff check app infra tests scripts
	uv run ruff format --check app infra tests scripts

typecheck: ## mypy on app
	uv run mypy app

build: ## Package both Lambda functions for arm64
	rm -rf $(BUILD)
	mkdir -p $(BUILD)/authorizer $(BUILD)/mcp
	uv export --no-dev --no-group infra --no-emit-project --no-color --format requirements-txt -o $(BUILD)/requirements.txt
	uv pip install --quiet --no-deps --no-compile \
	  --python-platform $(LAMBDA_PLATFORM) --python-version $(PY_VERSION) \
	  --target $(BUILD)/mcp -r $(BUILD)/requirements.txt
	uv pip install --quiet --no-deps --no-compile \
	  --python-platform $(LAMBDA_PLATFORM) --python-version $(PY_VERSION) \
	  --target $(BUILD)/authorizer -r $(BUILD)/requirements.txt
	cp -R app $(BUILD)/mcp/app
	cp -R app $(BUILD)/authorizer/app
	find $(BUILD) -name '__pycache__' -type d -prune -exec rm -rf {} +

synth: build $(NODE_DEPS) ## cdk synth for ENV (default dev); HOSTED_ZONE_NAME=... or CERT_ARN=... for a custom domain
	$(CDK) synth $(CDK_CTX)

deploy: build $(NODE_DEPS) ## cdk deploy all stacks for ENV
	$(CDK) deploy $(CDK_CTX) --all --require-approval $(REQUIRE_APPROVAL)

destroy: $(NODE_DEPS) ## cdk destroy for ENV (data stores survive only if retain_data is on)
	$(CDK) destroy $(CDK_CTX) --all

cert-synth: $(NODE_DEPS) ## Synth the certificate stack (separate app; never run by CI)
	$(CDK) $(CERT_APP) synth $(LAUNCH_CTX)

cert-deploy: $(NODE_DEPS) ## Issue the ACM certificate for ENV by hand; then add the CNAME it prints (make cert-status)
	$(CDK) $(CERT_APP) deploy $(LAUNCH_CTX) --require-approval $(REQUIRE_APPROVAL)

cert-status: ## Show the certificate's validation CNAME and status for ENV (DOMAIN required)
	@if [ -z "$(DOMAIN)" ]; then echo "DOMAIN is not set; run make cert-status ENV=$(ENV) DOMAIN=<host>"; exit 1; fi; \
	region=$$(uv run python -c "from infra.config import load; print(load('$(ENV)').region)"); \
	domain="$(DOMAIN)"; \
	arns=$$(aws acm list-certificates --region $$region \
	  --query "CertificateSummaryList[?DomainName=='$$domain'].CertificateArn" --output text); \
	count=$$(echo $$arns | wc -w | tr -d ' '); \
	if [ "$$count" = "0" ]; then echo "no certificate requested yet for $$domain in $$region; run make cert-deploy"; exit 1; fi; \
	if [ "$$count" != "1" ]; then echo "$$count certificates exist for $$domain — the API uses the one in SSM /wiki/$(ENV)/certificate-arn:"; \
	  aws ssm get-parameter --region $$region --name /wiki/$(ENV)/certificate-arn --query Parameter.Value --output text 2>/dev/null || echo "(parameter not published yet)"; \
	  echo "all: $$arns"; exit 1; fi; \
	aws acm describe-certificate --region $$region --certificate-arn "$$arns" \
	  --query "Certificate.{Status:Status,Domain:DomainName,CNAME:DomainValidationOptions[0].ResourceRecord}" --output table

security: ## §12.8 checks against a deployed instance (WIKI_BASE_URL required)
	uv run pytest -m security -o addopts=""

aws-tests: ## Negative STS/S3 tests that need real credentials (§8.5)
	uv run pytest -m aws -o addopts=""

grant-owner: ## Bootstrap the first owner: make grant-owner SUBJECT=user_xxx EMAIL=you@x TABLE=...
	uv run python scripts/grant_owner.py --bootstrap --subject $(SUBJECT) --email $(EMAIL) --name "$(NAME)" --table $(TABLE)

clean:
	rm -rf $(BUILD) cdk.out .pytest_cache .ruff_cache .mypy_cache
