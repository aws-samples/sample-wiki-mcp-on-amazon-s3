# tests/security — the §12.8 security checklist, automated

Integration tests against a **deployed development instance** (docs/ARCHITECTURE.md
§12.8). They are marked `security`, the default `pytest` run deselects them
(`addopts` in `pyproject.toml`), and `make security` runs them:

```sh
make security
# equivalently
uv run pytest -m security -o addopts=""
```

Every test reads the variables below and **skips, with the variable named, when one it
needs is absent**. A skip is never a pass: read the summary line, and treat a run
that is mostly skips as a run that proved nothing.

## Environment contract

| Variable | Used by | Meaning |
| --- | --- | --- |
| `WIKI_BASE_URL` | every HTTP test | Instance origin, no trailing slash — e.g. `https://wiki-dev.example.com`. Must be `https`. |
| `WIKI_TEST_TOKEN` | tokens, zero-grant, item 8 | A live access token for the **bootstrap owner** (`own` on `/`), obtained through the §2 flow (a real authorization-code + PKCE round trip against the dev resource indicator). |
| `WIKI_TEST_TOKEN_NOGRANTS` | zero-grant | A live token for a **second user who holds zero grants**. Same AuthKit environment; no particular scope is required (custom scopes are not used — a CIMD-registered client can't be assigned them, and WorkOS issues no such scopes) — the tests exercise the grant layer, which is the only authority. |
| `WIKI_TEST_TOKEN_WRONG_AUD` | tokens (optional) | A genuine AuthKit token issued for a **different** resource indicator. Skipped when unset. |
| `WIKI_TEST_TOKEN_EXPIRED` | tokens (optional) | A once-valid token for this instance whose `exp` has passed (tokens live ≤ 15 min, AS-9 — keep one from an earlier session). Skipped when unset. |
| `WIKI_TEST_BUCKET` | storage isolation | The instance's bucket name. |
| `WIKI_TEST_TABLE` | storage isolation | The grant table name. |
| `WIKI_TEST_REGION` | storage isolation | Region of both. |

Storage-isolation tests also need the operator's **ambient AWS credentials** (profile,
SSO, or exported keys) for the bucket-policy, encryption, and IAM reads; the anonymous
attempts use a `botocore.UNSIGNED` client and never see them. Tests that need a
permission the ambient role lacks skip and name the permission.

The names are deliberately distinct from the runtime set (`WIKI_BUCKET`,
`GRANT_TABLE`, …) because `tests/conftest.py` overwrites those — and
`AWS_ACCESS_KEY_ID` and friends — with fakes for every test. `tests/security/conftest.py`
snapshots the `AWS_*` environment at import time and builds its `boto3.Session` from
the snapshot, so the fakes never reach a real call.

Tokens are secrets. Export them for the shell that runs the suite and nothing else;
never write them to a file in the repository (§12.3, §12.7).

### Getting the two tokens

`scripts/oauth_gate.py` runs the authorization-code + PKCE flow with the gate client.
With `--token-file` it writes the access token to a file only you can read (mode
0600) and never prints it. Access tokens live about 5 minutes (AS-9), so fetch both
and run the suite in one go, the zero-grant user first because that sign-in is the
slow one. That user signs in from a private window: `--no-browser` prints the URL
instead of opening the default browser, which would answer from your own AuthKit
session and hand back your token again. `--skip-negative` skips the gate's second
browser round, so its report shows check 4 as FAIL (skipped); that is expected here.

```sh
uv run python scripts/oauth_gate.py --authkit https://<slug>.authkit.app \
  --client-id <gate client id> --resource https://<hostname>/mcp \
  --skip-negative --no-browser --token-file /tmp/nogrants.tok   # paste the URL into a private window
uv run python scripts/oauth_gate.py --authkit https://<slug>.authkit.app \
  --client-id <gate client id> --resource https://<hostname>/mcp \
  --skip-negative --token-file /tmp/owner.tok                   # you, in the default browser
WIKI_TEST_TOKEN="$(cat /tmp/owner.tok)" WIKI_TEST_TOKEN_NOGRANTS="$(cat /tmp/nogrants.tok)" \
  make security
rm -f /tmp/owner.tok /tmp/nogrants.tok
```

Each run prints the token's `sub`; check the two differ before trusting the result.

For the optional expired-token check, fetch one more owner token with `--token-file`,
wait until it is more than 5 minutes old, and pass it as `WIKI_TEST_TOKEN_EXPIRED`.

## What each file proves

| File | §12.8 item | Assertions |
| --- | --- | --- |
| `test_routes.py` | 1 | Both metadata documents: 200, `resource` == `WIKI_BASE_URL + "/mcp"` byte for byte, exactly one `authorization_servers` entry (AS-2), `scopes_supported` exactly `["openid", "profile", "email", "offline_access"]` (AuthKit's standard scopes; custom scopes are not used). `OPTIONS /mcp` unauthenticated with CORS headers exposing `WWW-Authenticate` and allowing the MCP header set. `GET`/`DELETE /mcp` → 405. `POST /mcp` without a token → 401 with `Bearer resource_metadata="<metadata URL>"`, no `scope`, and `Access-Control-Expose-Headers` on the gateway response itself (§9.3). `/`, `/health`, `/metrics`, `/admin`, `/mcp/x`, `/.well-known/nope`, a random path → 404 with no "Missing Authentication Token", no traceback, no request echo. |
| `test_routes.py` | 8 | A non-JSON body with a valid token → 400 whose body carries no `Traceback`, no `/var/task`, no `Exception`, and does not echo the request. |
| `test_tokens.py` | 2 | 401 for: garbage bearer; a self-signed RS256 JWT with the real `iss`/`aud`; an `alg: none` JWT; a genuine wrong-audience token (optional var); a genuine expired token (optional var). Each rejection must carry the discovery challenge (AS-3). |
| `test_tokens.py` | 3 | `WIKI_TEST_TOKEN` decoded **without** verification: `aud` equals the metadata document's `resource` byte for byte (§2 check 3 as an assertion), `iss` is the advertised authorization server. §2 check 5 (scope claim) does not apply — custom scopes are not used. Plus the positive path: `ping` → JSON-RPC result, `tools/list` contains `read_article`. |
| `test_zero_grant.py` | 5 (observable half) | With the zero-grant token: `list_folder /` → tool error 404; `read_article /anything.md` → 404, **not** 403 (§10.4 indistinguishability); `search` (when deployed) → empty hits; `create_article /probe.md` → tool error `403 forbidden` whose message names no scope (§6.5 row 2), all inside HTTP 200 with no `WWW-Authenticate`. Then, as the owner, `/probe.md` does not exist. |
| `test_storage_isolation.py` | 4, 7 (part) | Anonymous `GetObject a/probe.md` and `ListObjectsV2` → `AccessDenied`. Bucket policy status not public; policy contains a Deny over `Principal: *` conditioned on the caller's account/ARN/org; the bucket's public-access block all four on, and the account-level block all four on when one is configured (skipped when none is; it is optional). Bucket SSE-KMS and table SSE `KMS`, both under a **customer-managed** key. IAM sweep: no user holds — inline, attached, or via a group — a policy whose document names the bucket or the table. |

One skip is expected and does not mean a control is missing: the account-level public
access block is optional (`docs/DEPLOY.md` §1 step 5), so
`test_account_public_access_block_all_on` skips when the account has none. The
bucket-level block is always asserted. If an account-level block exists with any flag
off, that test fails.

Every response is also checked, informationally, for stack-naming headers (`server`,
`x-powered-by`, …). Those surface as pytest warnings, never failures.

### The probe article

`test_zero_grant.py` tries to create `/probe.md` as the zero-grant user and then
confirms, as the owner, that it does not exist. If the create ever succeeds the file
stays in the bucket and every later run fails at the precondition with "stale
/probe.md exists". That is the intended behaviour: it is a security finding, not a
flaky test. Remove the object and find out why the create went through.

## What is not automated here, and why

Item by item, so nobody reads a green run as more than it is.

- **Item 2, expired token.** Only genuine when the token was genuinely valid once, so
  it needs `WIKI_TEST_TOKEN_EXPIRED` from the operator; without it the test skips. The
  expiry check in isolation is a unit test in `tests/unit/authorizer`.
- **Item 3, "request one for an unregistered URI and confirm refusal".** Driving an
  authorization-code + PKCE flow needs a browser login; it stays a manual §2 check 4.
  What *is* automated is the consequence: the owner's `aud` equals the published
  `resource` byte for byte.
- **Item 4, "GetObject from a role in another account".** Needs a second AWS account.
  Not automated. The bucket-policy Deny statement and the bucket's public-access block
  are the controls that make it true and are asserted directly (the optional
  account-level block is asserted when configured).
- **Item 4, "GetItem on the grant table from any principal other than the two
  application roles".** Cannot be asserted from the operator's own role, which is
  allowed by design; DynamoDB rejects unsigned requests before authorization, so an
  anonymous attempt proves nothing. Asserted instead: SSE-KMS under the customer key,
  and the IAM sweep. A table resource policy, if one is added, would be the thing to
  read here.
- **Item 4, the IAM sweep, honestly.** It matches policy documents against the bucket
  and table *names*. A user with `Resource: "*"` (AdministratorAccess) names neither
  and is not caught. Users with active access keys are reported as a warning because
  §8.8 says there should be none — it is a warning rather than a failure so an operator
  who has not yet migrated to SSO can still run the rest of the suite. The sweep skips
  when the ambient role lacks `iam:ListUsers`.
- **Item 5, "no `AssumeRole` call was made".** CloudTrail delivers minutes later and
  cannot be asserted on inside a test. This half is a unit test in `tests/unit/tools`
  against `CredentialMinter.mint_count == 0` for a subject with no grants; the tests
  here cover what a zero-grant user can observe.
- **Item 6, credential scan of the repository and package.** Not an HTTP or AWS
  assertion; belongs to CI (push protection plus a secret scanner over the build
  directory), not to this suite.
- **Item 7, backups encrypted with the customer key and a restore actually
  performed.** The backup vault (encrypted with the storage key) and the daily
  grant-table plan are defined in `infra/stacks/ops.py` but not asserted here, and a
  restore is a drill someone performs, not a test. The customer-managed key on bucket
  and table *is* asserted, and so is the account-level public access block when one is
  configured.
- **Item 8, a genuine 5xx.** There is no way to trigger a server error on demand from
  outside without a fault-injection hook, and a hook would be a security surface of its
  own. The 400 path is asserted; the `DEFAULT_5XX` gateway response body is fixed in
  `infra/stacks/api.py` and is the same generic shape.
