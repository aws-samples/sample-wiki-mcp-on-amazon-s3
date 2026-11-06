# Security overview

What this sample protects, where its trust boundaries are, the main threats, the
controls against them, and the risks that remain. The design itself is in
[ARCHITECTURE.md](ARCHITECTURE.md) (§4, §8 and §12). What the controls don't cover,
and what's left to you as the deployer, is in the README's
[Security design](../README.md#security-design).

## Reporting a security issue

If you discover a potential security issue in this project, notify AWS/Amazon Security
through the [vulnerability reporting page](https://aws.amazon.com/security/vulnerability-reporting/).
Please don't create a public GitHub issue. See
[CONTRIBUTING](../CONTRIBUTING.md#security-issue-notifications).

## What it protects

- Articles and their version history: markdown in the versioned S3 bucket, under `a/`.
  It holds whatever the organization records, so treat it as confidential.
- The grant and profile table (DynamoDB): who may read, write or own which path, and
  each person's email, display name, status and session epoch. It decides who sees what.
- The web secret (Secrets Manager): the web application's AuthKit client secret and the
  key that signs console sessions. Whoever holds that key can sign in to the console as
  anyone.
- Credentials in use: WorkOS access tokens presented to `/mcp`, the WorkOS refresh
  tokens AuthKit issues to MCP clients (the server's metadata advertises
  `offline_access`), and the short-lived STS credentials minted for each operation. A
  refresh token is held by the person's MCP client and sent only to AuthKit, never to
  `/mcp`.
- Audit logs and the CloudTrail trail: who did what, when, and what was decided.
- Backups: the grant table's point-in-time recovery and its daily AWS Backup recovery
  points. They carry no permission model.

## Trust boundaries

| Boundary | Between | What holds it |
| --- | --- | --- |
| Internet edge | Claude clients, browsers and anyone else on the internet, and the API's custom domain | TLS 1.2, one stage throttle, unknown routes answering 404, generic error bodies, CORS on `/mcp` limited to `https://claude.ai`. No AWS WAF is attached. |
| Identity provider | The application and its users, and the WorkOS AuthKit environment | Tokens bound to the instance's MCP URL by a resource indicator and checked for that audience, PKCE with state and nonce, and self-signup turned off in WorkOS. |
| Request authentication | API Gateway and the functions | On `/mcp`, an authorizer that pins RS256 and checks issuer, audience, expiry and subject on every request, with no result cache. In the console, a signed `__Host-` session cookie, the person's profile status checked on every request, a CSRF token on every form, and an AuthKit sign-in in the last 5 minutes for admin actions. |
| Data plane | The functions and the stores | STS credentials per operation, with a session policy that reaches only the path the operation needs. The MCP function has no S3 access of its own and only reads grants; only the web function writes grants. The bucket refuses other accounts and requests without TLS. |
| Operations | Account operators and the running system | CloudTrail, alarms to the alerts topic, an alarm on any read of the web secret by something other than the web and rotation functions, an alert on bucket policy, public-access and retention changes, and a break-glass role that exists only for a principal the deployer names and alerts when assumed. |
| Supply chain | Upstream packages and actions, and what CI builds | Python dependencies installed from `uv.lock` with hashes, the CDK CLI pinned in `package-lock.json`, Actions pinned to commit SHAs, `pip-audit` and an SBOM on every CI run, read-only CI tokens, and no automatic merges. |

## Main threats and controls

| Threat | Controls | What remains |
| --- | --- | --- |
| An article carries instructions that make the reader's agent copy content or change articles (prompt injection) | No MCP tool creates a person or changes a grant, and the MCP function's role can't write grants. Tool descriptions say returned article text is data, not instructions, and `read_article` and `search_and_read` return who last wrote each article and when. Every version is kept and attributed. An alarm fires when one person's agent makes 30 or more write calls in 5 minutes. | The agent can still copy or change whatever its person can reach. |
| A bug in grant resolution or in building a session policy mints too broad a credential | Session policies scoped to one path (`app/auth/credentials.py`); unit tests fuzz the path grammar and the ancestor walk; `make security` and `make aws-tests` are checks you run against your own deployment. | The storage role's own policy spans all of `a/*`, so IAM doesn't catch such a bug. |
| A token for another audience, an `alg: none` token or an expired token reaches `/mcp` | The authorizer checks above; `scripts/oauth_gate.py` checks that WorkOS refuses a token for an unregistered resource. | Nothing beyond the residual risks below. |
| Script injection or cross-site request forgery in the console | Autoescaped templates, markdown rendered with raw HTML off, a Content-Security-Policy header, and a CSRF token checked before every POST. | Nothing beyond the residual risks below. |
| A leaked session key, or someone at an owner's signed-in browser | The web secret is encrypted with the instance's KMS key, only the web and rotation functions may read it, and any other read raises an alarm. The key rotates every 30 days. Admin actions need a recent AuthKit sign-in. | A leaked key forges sessions until it is rotated twice. AuthKit usually completes the recent sign-in without a prompt, so it stops a copied cookie but not someone using the owner's browser. |
| A member's email or Claude account is taken over | Narrow grants; MFA, which you turn on in WorkOS. | The attacker gets exactly that member's grants. |
| An owner over-grants, or gives `own` to the wrong person | Every grant records who made it; the console lists unowned grants; a move that widens access needs a person to confirm it. | Nothing alerts on a new `own` grant. |
| A flood of requests | The stage throttle (20 requests a second, burst 50) and per-person MCP limits (60 calls a minute, 200 writes an hour), with an alarm when those limits can't reach their table. | No WAF, the per-person limiter fails open, and the console has no per-person limit. |
| Disclosure through logs, backups or copies | Audit lines record paths and decisions, never tokens, cookies or article bodies; the API access log records no headers, cookies or query strings. The bucket, tables, backup vault and trail bucket are encrypted with the instance's KMS key. | A backup, or a copy taken out of AWS, has no permission model. |
| An account administrator, or the break-glass role, reads or deletes outside the application | The break-glass role exists only for a named principal and alerts when assumed; CloudTrail; the bucket change alert. | Account administrators are outside the application's permission model. |
| Access that has to be investigated later | One audit line per MCP call and per page of the console's read view, an API access log, and CloudTrail write events on the bucket. | Audit logs are kept 90 days, and CloudTrail doesn't record S3 reads. |

## Residual risks

These remain with the controls above in place. Running an instance means accepting them,
or adding your own controls:

- Prompt injection can't be removed while agents read content other people wrote. The
  damage is bounded by the reader's own grants, and every change is kept with its author.
- WorkOS AuthKit issues the tokens and holds the directory. A compromised or
  misconfigured WorkOS environment means compromised identity for the instance.
- The per-person rate limiter fails open when its table errors, by design, so during a
  DynamoDB incident cost is bounded only by the stage throttle. The sample sets no
  reserved concurrency on its functions.
- A bug in grant resolution or session-policy building isn't caught by IAM, because the
  storage role spans all of `a/*`.
- Revocation is prospective: content already in someone's agent history, notes or copies
  stays there.
- Some path names show: a link, a move pointer or a move preview can name a path you
  can't open.
- Adding a person records the email typed in the console without checking it against the
  WorkOS identity.
- Audit logs last 90 days unless you export them.
- Anyone with administrator access to the AWS account is outside the application's
  permission model.

If you adapt this sample, [DEPLOY.md](DEPLOY.md) §9 lists what to consider (Object Lock
and retained data, a break-glass principal, alert email), and the README's
[Security design](../README.md#security-design) lists the deployer steps (MFA in
WorkOS, a web ACL).
