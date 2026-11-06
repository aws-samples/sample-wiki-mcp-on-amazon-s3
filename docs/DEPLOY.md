# Deploying an instance, from zero

The operator sequence for the sample's one environment, `dev`, in the order the
steps depend on each other. Design references are to `docs/ARCHITECTURE.md`. The sample deploys
with Object Lock and data retention off so it can be torn down; if you adapt it, see
[§9](#9-adapting-this-sample).

The four stacks, in dependency order: `wiki-<env>-storage` (key, bucket, grant
table, rate-limit table) → `wiki-<env>-compute` (authorizer, MCP data plane, web
application, storage role) → `wiki-<env>-api` (REST API, custom domain, throttle,
budget) → `wiki-<env>-ops` (trail, alerts, alarms, the optional break-glass role,
backups).
`make deploy` deploys all four.

---

## First-deploy checklist

Once, in this order, ticking as you go. Each item links to the section that
explains it; the sections below stay the reference. Items 1, 2 and 7 exist because a
deploy that lands in the wrong account is the failure this list is for (§9.6).

- [ ] **1. Account created and given at launch.** The AWS account for this env exists
      and its 12-digit id is passed on every command as `ACCOUNT=<id>` (`-c account=`)
      ([§1](#1-aws-accounts-and-credentials), [§3](#3-infraconfigpy)). Without it,
      synth refuses in every env.
- [ ] **2. Credentials point at it.** `aws sts get-caller-identity` shows that account
      id — before any `make` command, every time ([§1](#1-aws-accounts-and-credentials)).
      If the credentials resolve elsewhere, synth stops with
      `credentials resolve to account X but account Y was given for env Z`.
- [ ] **3. AuthKit domain and web client id given at launch.** `AUTHKIT_DOMAIN=` and
      `WORKOS_CLIENT_ID=` on every command ([§3](#3-infraconfigpy); the WorkOS side is
      [§5](#5-workos-the-authorization-server) steps 1 and 9). A missing AuthKit
      domain refuses to synth. An empty client id is a synth warning, and web
      logins fail.
- [ ] **4. Magic Auth on, password login off.** WorkOS defaults to Email +
      Password enabled and Magic Auth disabled; the web app's login page promises a
      passwordless emailed link (§15.2 step 3), so an environment left on the
      default shows a password prompt at first login instead, with nothing in the
      UI explaining why ([§5](#5-workos-the-authorization-server) step 8).
- [ ] **5. Gate script passed** — `GATE PASSED`, check 4 included. It talks only to
      AuthKit, so run it before deploying: check 4 decides whether this WorkOS
      environment can be used at all. Keep the `sub` from the token it decodes; step 11
      needs it ([§5](#5-workos-the-authorization-server) step 6).
- [ ] **6. Certificate, by hand (never CI).** Skip this item when `HOSTED_ZONE_NAME`
      is set: the api stack then issues and validates the certificate during the
      deploy ([§2](#2-domain-and-certificate)). Otherwise run `make cert-deploy
      ENV=<env>` with `ACCOUNT`, `DOMAIN` and `AUTHKIT_DOMAIN` exported; it pauses at
      "waiting for validation" — run `make cert-status ENV=<env>` in another shell,
      add the CNAME it prints at your DNS provider, and the deploy completes when ACM
      sees it (minutes to an hour). The ARN lands in SSM at
      `/wiki/<env>/certificate-arn` and the API stack reads it at deploy time, so
      `CERT_ARN=` is not needed. Renewals are automatic while the CNAME stays
      ([Certificate](#certificate)).
- [ ] **7. `make synth ENV=<env>`** succeeds, and the
      `wiki: env=<env> account=<id> region=us-west-2` line it prints shows the account
      from step 1 ([§4](#4-deploy)).
- [ ] **8. `make deploy ENV=<env>`**, with `ALERT_EMAIL=` so alarms reach a person;
      confirm the SNS subscription from the email ([§4](#4-deploy)).
- [ ] **9. DNS.** CNAME or ALIAS from the hostname to the api stack's `DomainTarget`
      (with `HOSTED_ZONE_NAME` the api stack creates it); the metadata URL answers
      ([§2](#2-domain-and-certificate), [§4](#4-deploy)).
- [ ] **10. Secret filled.** `client_secret` replaced in the secret at `WebSecretArn`
      ([§5](#5-workos-the-authorization-server) step 9). It is the only WorkOS
      credential any function holds; there is no management API key to fill.
- [ ] **11. `make grant-owner`** with that subject ([§6](#6-first-owner)).
- [ ] **12. `make aws-tests`** — the §8.5 negative tests: a minted credential
      must be refused by AWS outside its prefix, not by our code. Needs credentials the
      storage role trusts. Today it trusts only the MCP and web functions
      (`infra/stacks/compute.py`), so this step can't pass until a test principal is
      added to that trust. Also needs `WIKI_BUCKET` (`BucketName`), `STORAGE_ROLE_ARN`
      (`StorageRoleArn`, compute stack) and `KMS_KEY_ARN` (`KmsKeyArn`) exported
      ([§4](#4-deploy) outputs table). A skip is not a pass.
- [ ] **13. `make security`** against the deployed instance, no skips other than the
      optional wrong-audience and expired-token checks when their tokens are unset
      ([§8](#8-make-security--the-128-checklist)).
- [ ] **14. Restore rehearsal done and dated** in `docs/RUNBOOK.md` §7 "Record"
      ([§8](#8-make-security--the-128-checklist) item 7). The §12.8 checklist is not
      complete until that row exists.

Post-launch:

- [ ] **Flip `strict_mcp_headers` to `True`** in `infra/config.py` and redeploy, once
      a real Claude client has been observed sending the `Mcp-*` headers. docs/ARCHITECTURE.md §6.2
      calls them mandatory; the valve stays off until then because a client that omits
      them would be locked out with a `400`. Absent headers are tolerated silently
      while it is off, so there is nothing to look for in a log — observe by doing:
      flip it, add the connector ([§7](#7-add-the-connector)) and exercise a tool
      call; if that works, the client sends them.

---

## 0. Repository settings (once, before the first real push)

These are GitHub settings, not files, and §12.4 wants them on **before** the first
commit that could carry a credential.

| Setting | Where | Why |
| --- | --- | --- |
| Secret scanning **and** push protection | Settings → Code security | Rejects a credential-bearing commit at push time. [Enable push protection](https://docs.github.com/en/code-security/secret-scanning/enabling-secret-scanning-features/enabling-push-protection-for-your-repository). `gitleaks` in CI is the second net, not the first. |
| Branch protection on `main` | Settings → Branches | Require a pull request; require the `ci / checks` and `ci / secret scan (history)` status checks. |
| Dependabot | `.github/dependabot.yml` (already in the repo) | Weekly update checks for `uv`, `npm` (the CDK CLI) and Actions, with a pull request for each update found, patch releases grouped. None merges on its own: review it, let the checks pass, merge, then `make deploy`. |

## 1. AWS accounts and credentials

1. One AWS account for the sample, ideally under an Organization with IAM Identity
   Center (SSO). If you adapt the sample, keep that instance in its own account, apart
   from the one you try the sample in (§9.6, [§9](#9-adapting-this-sample)).
2. Sign in with SSO rather than long-lived keys (§8.8 "no long-lived access keys
   anywhere"): `aws configure sso`, then `export AWS_PROFILE=wiki-dev`. Confirm with
   `aws sts get-caller-identity`.
3. Region is `us-west-2` in `infra/config.py`. The ACM certificate must be issued in
   the **same region** as the API (regional endpoint).
4. Bootstrap CDK in each account/region once, with the pinned CLI (`make sync`
   installs it into `node_modules/`):
   `node_modules/.bin/cdk bootstrap aws://<account-id>/us-west-2`.
5. **Account-level public access block** (optional, recommended; §12.3, §12.5). The
   deploy does not need it: every bucket the stacks create already blocks public
   access. The account-level block is a second layer that also covers any other bucket
   in the account. No CloudFormation resource exists for it, so it is a command, run
   once per account:

   ```sh
   aws s3control put-public-access-block --account-id <account-id> \
     --public-access-block-configuration \
     BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
   ```

   If you want it, set it before the first deploy: changing it once the ops stack
   exists fires the bucket-guard alert (`PutAccountPublicAccessBlock` is on the list).
   `make security` checks it when it is set and skips that one check when it is not.

## 2. Domain and certificate

Pick one of the two routes `infra/stacks/api.py` supports:

- **Hosted zone in this account** — pass `HOSTED_ZONE_NAME=<zone>` (for example
  `example.com`) at launch ([§3](#3-infraconfigpy)). CDK issues a DNS-validated
  certificate and writes the alias record itself. Nothing else to do.
- **DNS lives elsewhere** (the default, with `HOSTED_ZONE_NAME` empty) — issue the
  certificate with the hand-deployed certificate stack: see
  [Certificate](#certificate). Its ARN reaches the API stack through SSM; `CERT_ARN=`
  is only for a certificate issued some other way. After the first deploy, create a
  CNAME (or ALIAS) from the hostname to the `DomainTarget` output of the api stack.

## 3. `infra/config.py`

The file holds no deployment identifiers. Seven values are given at launch, as make
variables (or exported environment variables of the same name) that become CDK
context. `ACCOUNT` and `AUTHKIT_DOMAIN` are required; the table says what happens
when each of the others is missing:

```sh
make deploy ENV=dev ACCOUNT=<12-digit id> DOMAIN=wiki-dev.example.com \
  AUTHKIT_DOMAIN=https://<slug>.authkit.app WORKOS_CLIENT_ID=client_...
```

| make variable | CDK context | Field | If missing |
| --- | --- | --- | --- |
| `ACCOUNT` | `-c account=` | `account` | synth refuses |
| `DOMAIN` | `-c domain=` | `domain` | no custom domain; `-c canonicalMcpUrl=` is needed instead |
| `AUTHKIT_DOMAIN` | `-c authkitDomain=` | `authkit_domain` | synth refuses |
| `WORKOS_CLIENT_ID` | `-c workosWebClientId=` | `workos_web_client_id` | synth warning; web logins fail |
| `ALERT_EMAIL` | `-c alertEmail=` | — | alarms and the budget alert have no subscriber; a synth warning |
| `HOSTED_ZONE_NAME` | `-c hostedZoneName=` | `hosted_zone_name` | Route B: the hand-deployed certificate ([§2](#2-domain-and-certificate)) |
| `BREAK_GLASS_PRINCIPAL_ARN` | `-c breakGlassPrincipalArn=` | `break_glass_principal_arn` | no break-glass role, so the hard-delete procedure (`docs/RUNBOOK.md` §3) has no role to use; a synth info message |

`BREAK_GLASS_PRINCIPAL_ARN` names who may assume the break-glass role. It is an IAM
role or user ARN in this account, and wildcards are allowed. For an IAM Identity
Center permission set use
`arn:aws:iam::<account-id>:role/aws-reserved/sso.amazonaws.com/*AWSReservedSSO_<permission-set>_*`.
The `*` after `sso.amazonaws.com/` also covers the region segment some accounts
have. Quote the value in the shell. Name one role, not a pattern that matches every
administrator. Pass it on every deploy, because a deploy without it removes the
role. Synth refuses a value that is not an IAM role or user ARN in `ACCOUNT`. It
also refuses a name that is only a wildcard, such as `role/*`, because that matches
every role in the account.

The rest are set in `ENVIRONMENTS`:

| Field | Value |
| --- | --- |
| `hosted_zone_name` | `None`; the value here is the default, and the `HOSTED_ZONE_NAME` launch value overrides it |
| `object_lock` | `False`; turn on if you adapt the sample ([§9](#9-adapting-this-sample)) |
| `retain_data` | `False`; turn on if you adapt the sample ([§9](#9-adapting-this-sample)) |
| `allowed_origins` | `("https://claude.ai",)` |
| `strict_mcp_headers` | `False` until a real client is seen sending the `Mcp-*` headers (checklist, post-launch item) |
| `log_retention_days` | 90 (§12.7) |
| `backup_retention_days` | 35 |

`account` is the guard against deploying to the wrong account (§9.6). `infra/app.py`
checks it before any stack is built (`guard_account` in `infra/config.py`):

- **Every env needs it.** Without an account, synth stops with
  `env 'dev' has no AWS account. Pass -c account=<12-digit id> (make deploy ENV=dev ACCOUNT=<id>).`
  Without an AuthKit domain it stops with
  `env 'dev' has no AuthKit domain. Pass -c authkitDomain=https://<slug>.authkit.app (make deploy ENV=dev AUTHKIT_DOMAIN=https://<slug>.authkit.app).`
- **The account must match the credentials.** The CDK CLI tells the app which
  account its credentials resolve to (`CDK_DEFAULT_ACCOUNT`); when that differs from
  the given account, synth stops with `credentials resolve to account X but account
  Y was given for env Z`.
- **CI passes placeholders.** `.github/workflows/ci.yml` synths with
  `-c account=000000000000`, `example.com` domains, `https://example.authkit.app`
  and a placeholder certificate ARN, so no real value is in the repo.

## 4. Deploy

```sh
make sync
make test
make deploy ENV=dev CERT_ARN=arn:aws:acm:us-west-2:<account>:certificate/<id>
```

`make synth ENV=<env> CERT_ARN=...` is the dry run: the same build and synthesis
with no deploy, printing `wiki: env=<env> account=<id> region=<region>` on the way
so the account can be read against `aws sts get-caller-identity` (checklist step 7).
`make deploy` runs `make build` (packages both functions for arm64 / Python 3.13),
then `cdk deploy --all --require-approval broadening`. Before it deploys a stack that
adds IAM permissions or security-group rules, CDK lists those changes and asks
`Do you wish to deploy these changes?`. A first deploy adds every permission, so each
stack that has any asks. `REQUIRE_APPROVAL=` overrides the setting for `make deploy`
and `make cert-deploy`: `REQUIRE_APPROVAL=any-change` asks before every change, and
`REQUIRE_APPROVAL=never` deploys without asking. To attach an email to the alerts
topic and the budget in the same deploy, add `ALERT_EMAIL=`:

```sh
make deploy ENV=dev ALERT_EMAIL=you@example.com
```

Then **confirm the SNS subscription** from the email SNS sends; until you do, every
alarm fires into nothing.

Note the outputs; the later steps use them:

| Stack | Output | Used for |
| --- | --- | --- |
| storage | `BucketName`, `GrantTableName`, `RateLimitTableName`, `KmsKeyArn` | `grant_owner.py`, `make security` |
| compute | `WebSecretArn`, `StorageRoleArn`, `McpFunctionName`, `WebFunctionName` | web client secret ([§5](#5-workos-the-authorization-server) step 9), `make aws-tests` |
| api | `CanonicalMcpUrl`, `ResourceMetadataUrl`, `DomainTarget`, `AccessLogGroupName` | WorkOS resource indicator, DNS, runbook (access log) |
| ops | `AlertsTopicArn`, `TrailArn`, `TrailLogBucketName`, `BackupVaultName`, `BreakGlassRoleArn` (only with `BREAK_GLASS_PRINCIPAL_ARN`) | runbook |

Create the DNS record from `DomainTarget` (step 2; with `HOSTED_ZONE_NAME` the api
stack has already created it) and wait for
`curl -i https://wiki-dev.example.com/.well-known/oauth-protected-resource/mcp` to
return the metadata document.

## 5. WorkOS (the authorization server)

§15.1 lists the dashboard steps; `scripts/oauth_gate.py` runs the checks that
decide whether WorkOS is acceptable at all (§2). In order:

1. Create the WorkOS account; the staging environment exists immediately. Record the
   AuthKit domain (`https://<slug>.authkit.app`) — it is `AUTHKIT_DOMAIN=` at
   launch (§3). If you change it after the first deploy, deploy again.
2. Connect → Configuration: enable **CIMD**; leave DCR off (AS-8).
3. Connect → Configuration: add `CanonicalMcpUrl` (exactly, byte for byte) as a
   **resource indicator** and make it the default. Each instance is registered
   separately, in its own WorkOS environment (AS-7).
4. Create the gate script's client under **Connect → Applications → Create
   application → "OAuth application"**, with **Use PKCE** ticked — that is what
   makes it a public client — and redirect URI `http://127.0.0.1:8765/callback`.
   Do not create it from the top-level **Applications** nav item: that produces a
   `client_id` the `/oauth2/*` endpoints reject with `error=application_not_found`.
5. **Custom scopes are not used.** `wiki.read` / `wiki.write` as a coarse tier above
   the grant layer is not possible on this stack — WorkOS issues scopes only from
   permissions assigned per-application, and a
   client registered by Client ID Metadata Document (how the gate script, and
   Claude, register) is read-only there, no Scopes section to assign from. Request
   `openid profile email` (plus `offline_access` where a refresh token is wanted);
   there is nothing to create or assign on this page. The protected resource
   metadata lists exactly these four in `scopes_supported`, so MCP clients that read
   it ask for them, `offline_access` included.
6. Run the gate:

   ```sh
   uv run python scripts/oauth_gate.py \
     --authkit https://<slug>.authkit.app \
     --client-id client_01... \
     --resource https://wiki-dev.example.com/mcp
   ```

   Check 4 (a token for an **unregistered** resource must be refused) decides
   whether the design proceeds. If it issues a token, stop; §2 says what replaces
   WorkOS.
7. Disable self-signup at **Authentication → Features → Sign-up → Manage** (§2
   check 7) and restrict CIMD origins if the dashboard allows it (check 6) — as of
   this writing WorkOS offers no such allowlist, so that row of §4.9 is a wish
   rather than a control.
8. **Authentication → Methods → Magic Auth → Enable**, then **Authentication →
   Methods → Email + Password → Manage → Enable off → Save changes**. WorkOS
   defaults to the opposite — Email + Password on, Magic Auth off — but the web
   app's login page promises a passwordless emailed link (§15.2 step 3); left on
   the default, the dashboard shows a password prompt instead and the first login
   fails with no explanation.
9. **The web application's own client.** In WorkOS create a second OAuth client —
   confidential, authorization code + PKCE, redirect URI exactly
   `https://<domain>/app/callback`. Pass its client id at launch
   (`WORKOS_CLIENT_ID=`, §3). After `make deploy`, open the secret named by the
   `WebSecretArn` output and replace the one `REPLACE-ME` value, `client_secret`,
   with that client's secret. `session_key` is generated for you; leave it. Until
   the placeholder is replaced, login fails at the token exchange. Opening the
   secret in the console reads it, so `wiki-<env>-websecretread` fires once; that is
   expected and clears by itself a few minutes later.

   That client secret is the only WorkOS credential any function holds, and it can
   do nothing but exchange this client's own login codes. **Do not create a
   management API key for the application** (docs/ARCHITECTURE.md §11.6 "Adding a person"): that
   key can change any user's email address, and nothing needs it — people are
   created in the dashboard, by you, and recorded in the console
   ([§6](#6-first-owner), "Adding everyone else").

## Certificate

The one piece of infrastructure deployed by a person and never by CI: a regional ACM
certificate for the environment's domain, in its own CDK app (`infra/cert_app.py`,
stack `infra/stacks/certificate.py`).

```sh
make cert-deploy ENV=dev ACCOUNT=<id> DOMAIN=<host> AUTHKIT_DOMAIN=https://<slug>.authkit.app  # requests the certificate; CloudFormation waits for validation
make cert-status ENV=dev DOMAIN=<host>  # in another shell: prints Status and the validation CNAME
# add the CNAME (name → value) at the DNS provider for your domain; wait for Status: ISSUED
```

The deploy completes on its own once ACM validates. The stack publishes the ARN to SSM
(`/wiki/<env>/certificate-arn`); `make deploy` reads it at deploy time. To use a
certificate issued elsewhere instead, pass `CERT_ARN=arn:aws:acm:...` to `make synth`/
`make deploy` — the override wins over the parameter.

Why it is separate: issuance needs a human to place a DNS record, and an automated
redeploy must never be able to replace or delete the certificate the live domain is
using. The stack's certificate carries `Retain`, and neither `make deploy` nor the CI
synth includes it. The same account guard and launch values apply (§3).

## 6. First owner

The bootstrap grant is written with **operator** credentials, never a function's —
the MCP role cannot write grants, by design (§8.8).

```sh
make grant-owner SUBJECT=user_01H... EMAIL=you@example.com NAME="Your Name" \
  TABLE=<GrantTableName output>
# or directly:
uv run python scripts/grant_owner.py --bootstrap \
  --subject user_01H... --email you@example.com --name "Your Name" \
  --table <GrantTableName output>
```

`--subject` is the WorkOS user id (the token's `sub`) — sign in once through the
gate script and read it from the decoded token. `--bootstrap` bypasses the owner
guard for exactly this row (`own` on `/` in an empty table), writes the PROFILE row
the web application requires at login, and logs loudly. It refuses once `/` has an
owner. Later grants go through the admin console or the same script with `--granter`.

### Adding everyone else

Two steps, dashboard then console. The application never calls WorkOS (docs/ARCHITECTURE.md
§11.6 "Adding a person", §15.2).

1. **WorkOS dashboard → Users → Create user**, with their email address; then
   **Invite**. WorkOS sends them the sign-in email. Copy the user id it shows
   (`user_…`).
2. **Admin console → People → "Add a person"**: paste that id, their email, how their
   name should appear, and where they get access. The console writes their profile
   (status `invited` until their first sign-in) and the first grant, and shows the
   connector block to send them.

An id that already has a profile simply gets the grant. The status becomes `active`
when they first sign in through the invitation email.

## 7. Add the connector

In Claude (web or desktop — this step cannot start on a phone, §15.3): Settings →
Connectors → Add custom connector → URL `https://wiki-dev.example.com/mcp`. Complete
the consent screen. Also do it once from Claude Code, which is the surface most
likely to surface a redirect-URI or client-authentication defect (§15.1 step 6).

## 8. `make security` — the §12.8 checklist

`tests/security/` runs the checklist against the deployed instance. The environment
contract, copied from `tests/security/README.md`:

| Variable | Used by | Meaning |
| --- | --- | --- |
| `WIKI_BASE_URL` | every HTTP test | Instance origin, no trailing slash — e.g. `https://wiki-dev.example.com`. Must be `https`. |
| `WIKI_TEST_TOKEN` | tokens, zero-grant, item 8 | A live access token for the **bootstrap owner** (`own` on `/`), obtained through the §2 flow. |
| `WIKI_TEST_TOKEN_NOGRANTS` | zero-grant | A live token for a **second user who holds zero grants**. Same AuthKit environment; no particular scope is required (custom scopes are not used; see §5 step 5). |
| `WIKI_TEST_TOKEN_WRONG_AUD` | tokens (optional) | A genuine AuthKit token issued for a **different** resource indicator. Skipped when unset. |
| `WIKI_TEST_TOKEN_EXPIRED` | tokens (optional) | A once-valid token for this instance whose `exp` has passed. Skipped when unset. |
| `WIKI_TEST_BUCKET` | storage isolation | The instance's bucket name (`BucketName` output). |
| `WIKI_TEST_TABLE` | storage isolation | The grant table name (`GrantTableName` output). |
| `WIKI_TEST_REGION` | storage isolation | Region of both. |

Storage-isolation tests also use the operator's ambient AWS credentials. Tokens are
secrets: export them for the shell that runs the suite and nothing else. Get them
with `scripts/oauth_gate.py --token-file` (it writes the token to a mode-0600 file and
never prints it; the zero-grant user signs in from a private window with
`--no-browser`). The commands are in `tests/security/README.md` ("Getting the two
tokens"). **A skip is never a pass** — read the summary line.

```sh
export WIKI_BASE_URL=https://wiki-dev.example.com WIKI_TEST_TOKEN=... WIKI_TEST_TOKEN_NOGRANTS=... \
       WIKI_TEST_BUCKET=... WIKI_TEST_TABLE=... WIKI_TEST_REGION=us-west-2
make security
```

The checklist, item by item, mapped to what proves it:

| §12.8 | How it is verified |
| --- | --- |
| 1. Route table | `make security` → `test_routes.py` |
| 2. Wrong audience / `alg: none` / expired rejected | `test_tokens.py` (expired and wrong-aud need their optional variables) |
| 3. `aud` matches the canonical URI; unregistered URI refused | `test_tokens.py` for the first half; `scripts/oauth_gate.py` check 4 for the refusal (needs a browser) |
| 4. Storage answers only to the storage role | `test_storage_isolation.py` (anonymous access, bucket policy, encryption, IAM sweep); cross-account `GetObject` is a manual check from the other account |
| 5. Zero-grant user sees nothing, mints nothing | `test_zero_grant.py` for the observable half; `tests/unit/tools` asserts `mint_count == 0` |
| 6. No credentials in repo or package | `ci.yml`: gitleaks over the history, the package scan after `make build`; push protection (step 0) |
| 7. Account PAB on, backups under the CMK, **a restore performed** | `test_storage_isolation.py` for the block and the key; `tests/unit/infra/test_ops.py` for the vault key; the restore is a drill — `docs/RUNBOOK.md` "Restore rehearsal", record the date there |
| 8. A 5xx reveals nothing | `test_routes.py` covers the 400 path; the `DEFAULT_5XX` body is fixed in `infra/stacks/api.py`; `wiki-<env>-api5xx` alarms on any real one |

## 9. Adapting this sample

This is a sample. It ships one environment, `dev`, with Object Lock and data
retention off so it can be torn down with `make destroy` (the README's
[Clean up](../README.md#clean-up) lists what outlives it). If you adapt it, this
section lists what to consider. Deciding what your use needs, and testing it, is up
to you. Deploy a fresh instance, in its own account, with:

- **`object_lock=True`** in `infra/config.py` — the bucket is created with Object
  Lock on, governance mode, one-year default retention (§8.9). `ObjectLockEnabled` is
  a create-only property: it cannot be turned on later without replacing the bucket,
  so set it before the instance's first deploy. Nothing that runs code can delete a
  version for a year; hard delete is the break-glass procedure in the runbook.
- `BREAK_GLASS_PRINCIPAL_ARN=` naming the one role or permission set that may
  assume the break-glass role ([§3](#3-infraconfigpy)). The role's trust
  accepts only that principal. It carries no MFA condition, because IAM Identity
  Center and other federated role sessions do not include
  `aws:MultiFactorAuthPresent`, so the condition would refuse them. Require MFA
  where people sign in instead: Identity Center's MFA setting, or your identity
  provider's policy (for an IAM user, the user's own policy or an SCP).
  Governance-mode Object Lock yields to anyone holding
  `s3:BypassGovernanceRetention`, and `AdministratorAccess` includes it. If the
  account is in AWS Organizations, attach a service control policy that denies it
  on the wiki bucket to everyone but the break-glass role:

  ```json
  {
    "Version": "2012-10-17",
    "Statement": [{
      "Sid": "OnlyBreakGlassBypassesRetention",
      "Effect": "Deny",
      "Action": "s3:BypassGovernanceRetention",
      "Resource": "arn:aws:s3:::<BucketName>/*",
      "Condition": {"ArnNotLike": {"aws:PrincipalArn": "arn:aws:iam::<account-id>:role/wiki-<env>-break-glass"}}
    }]
  }
  ```

  SCPs do not apply to the Organization's management account, so do not run an
  instance there.
- **`retain_data=True`** in `infra/config.py` — the bucket, both tables, the key, the
  web secret, the trail bucket, the backup vault and every log group carry `Retain`,
  and a deletion guard is set on the grant table. `make destroy` leaves them all in
  place; that is the point.
- **Its own WorkOS environment** (a WorkOS production environment needs a payment
  method) with its own AuthKit domain, its own resource indicator registered for its
  canonical MCP URL, and the §2 gate run again against it. A token from the instance
  you tried the sample in must not validate against it and cannot: the audience
  differs.
- **A vault account and replica bucket** (Object Lock compliance mode, own key, no
  application), set up before the first article is written.
- **Alerts go to a person.** Pass `ALERT_EMAIL=` and confirm the subscription. Check
  that the first daily backup job (09:00 UTC) completes in the AWS Backup console; a
  KMS permission problem shows up there and nowhere else.
- Run `make security` with an owner token before you add any content (§12.8).

## 10. Automating deploys

Deploys are run by hand with the `make` targets in this guide. Dependency updates
arrive as Dependabot pull requests that a person reviews and merges; nothing in the
repository merges or deploys on its own. After merging one, run `make deploy` so the
functions carry it.

The sample ships no deploy workflow: deploying from GitHub Actions needs an IAM role
the workflow assumes through GitHub's OIDC provider, a deployer-specific setup the
sample does not provision. If you add one:

1. Create the OIDC identity provider for `token.actions.githubusercontent.com` in
   the account.
2. Create a role trusting it with `sub` pinned to `repo:<owner>/<repo>:ref:refs/heads/main`,
   allowed to assume the CDK bootstrap roles (`cdk-hnb659fds-*`) and nothing else.
3. Run that job only on `main` after `checks` passes, with `id-token: write` and
   `contents: read`, and have it run `make deploy` with your launch values (§3).
   A job has no terminal to answer the approval question (§4), so a change that adds
   permissions fails the job. Deploy that change by hand, or pass
   `REQUIRE_APPROVAL=never` to the job.
   `tests/unit/infra/test_supply_chain.py` allows only read scopes in every workflow,
   so `checks` fails until you extend that test to allow `id-token: write` in this one
   workflow.
