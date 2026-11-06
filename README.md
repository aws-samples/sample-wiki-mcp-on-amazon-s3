# sample-wiki-mcp-on-amazon-s3

**A shared knowledge base your AI agents can read and write, where every person sees
and changes only what they've been given.**

This sample is a tree of markdown articles that people reach through their AI agent,
connected to Claude as a remote MCP server. You ask Claude a question and it searches
and reads the tree for you. You ask it to record something and it writes an article,
attributed to you. Access is set per folder or per article: an owner gives someone
`read`, `write` or `own` on part of the tree, and that part is all they, and their
agent, can see or change.

It runs on your own AWS account (S3, DynamoDB, Lambda), with WorkOS handling sign-in.
There are no servers to run, and every change is kept as a version. The sample ships
with Object Lock and data retention off so it can be torn down; with both on
([docs/DEPLOY.md](docs/DEPLOY.md) §9), nothing can be deleted by accident.

This is sample code. It demonstrates an approach, for learning and experimenting,
and is provided to you as AWS Content under the AWS Customer Agreement, or the
relevant written agreement between you and AWS (whichever applies). You should not
use this AWS Content in your production accounts, or on production or other
critical data. Before any production use, it needs your own security testing and a
review of the code and the deployment against your security, privacy and compliance
requirements. Whether to use it in production is your decision, and you are
responsible for testing, securing, and optimizing it as appropriate for production
grade use, based on your specific quality control practices and standards.
Deploying it incurs AWS charges ([Cost](#cost)). If you adapt this sample,
[docs/DEPLOY.md](docs/DEPLOY.md) §9 lists what to consider.

## Why

Teams and families want one shared memory their agents can use, but not everyone
should see or edit all of it. Ordinary wikis make that a choice between sharing the
whole wiki and sharing nothing, and they give an agent no safe way to write with the
writer's identity attached. This sample makes **the tree itself the permission
model**:

- **Broad read, governed write.** Everyone can read the plan; only the person
  accountable for a part can change that part. The agent helping them has exactly
  their rights and nothing more.
- **Every version is kept and attributed** to the signed-in person the agent was
  acting for, in metadata the agent can't rewrite.
- **Default deny.** A new person sees nothing, not even that the tree exists, until
  an owner grants them something. Content they can't see and content that doesn't
  exist look the same to them.

Typical uses: a product team's decision records (each role owns its own section and
everyone reads across), a household's records, a small business's shared knowledge.
An instance starts empty; what it's for is whatever you put in it.

## Using an instance

You need a Claude account (web, desktop or Claude Code), and someone who owns the
instance needs to grant you access.

1. **Connect.** In Claude, go to Settings → Connectors → Add custom connector and
   enter `https://<instance>/mcp`. Sign in with the emailed link. Do this on web or
   desktop; once connected it also works on your phone. In Claude Code, add the
   same URL as a remote MCP server.
2. **Ask an owner for access.** They add you in the admin console and grant you a
   folder or an article.
3. **Talk to Claude.** It picks the right tools itself. For example:

| You say | What happens |
|---|---|
| "What did we decide about the storage design, and why?" | `search_and_read` finds the decision record and returns it in one call |
| "Add a row to the roadmap's open questions for the pricing question." | `edit_article` appends the row; it doesn't resend the whole page |
| "Draft an ADR for the new search design as proposed." | `create_article` writes it under your name, as a draft |
| "Move the old notes into /archive/2025." | `move_article` moves it and leaves a pointer at the old path |
| "What's been shared with me? Put Dana's racing folder in my tree." | `shared_with_me` lists your grants; a `link` article places the folder in your own tree |
| "What did this page say last month?" | `list_versions` and `read_version` show the history |

### Concepts in two minutes

- **Tree.** Folders and markdown articles with YAML frontmatter (title, description,
  tags, status), in the Open Knowledge Format (OKF) shape.
- **Grants.** `read`, `write` or `own` on a folder (which covers everything beneath
  it) or on a single article. Grants add up; there are no deny rules. Only people
  holding `own` grant access, and only in the web console. No agent can grant.
- **Versions.** Every write is a new, immutable version. **Archive** hides an article
  without destroying anything. **Move** leaves a forwarding pointer, and since access
  follows position, moving something changes who can see it (a move that would widen
  access has to be confirmed by a person in the console).
- **Links.** A `link` article points at a folder or article shared with you, so your
  tree becomes your own content plus the things others have shared. A link grants
  nothing: the target still checks your access every time.
- **The web app** at `https://<instance>/app`: browse and search what you can read,
  view history, and, for owners, the admin console (add people, grant and revoke,
  the audit log, confirming moves that widen access).

### What agents can do

Fourteen MCP tools. Every read is a search or a targeted fetch; results are bounded
so they fit in an agent's context.

| Find | Read | Write | History | Sharing |
|---|---|---|---|---|
| `search` | `read_article` (whole, one section, or a byte range) | `create_article` | `list_versions` | `shared_with_me` |
| `search_and_read` | `list_folder` | `edit_article` (patch edits) | `read_version` | `resolve_reference` |
| | | `update_article` (full replace) · `move_article` · `archive_article` · `unarchive_article` | | |

Agents can't grant access, add people, read the audit log or hard-delete. Those
exist only in the web console, and the role the MCP server runs under can't write
grants even if a tool tried.

## Running your own instance

**Start with [docs/SETUP.md](docs/SETUP.md).** It walks through setting up WorkOS,
deploying the four AWS stacks with CDK, and connecting the two, ending with Claude
reading your tree. [docs/DEPLOY.md](docs/DEPLOY.md) is the full reference, including
§9 on adapting this sample; [docs/RUNBOOK.md](docs/RUNBOOK.md) covers restore,
break-glass, revoking access and alarms.

You need an AWS account, a WorkOS account, a domain you control, and about a day for
the first setup. [Cost](#cost) says what an instance bills, and
[Clean up](#clean-up) how to remove one.

**Security, briefly:**
- Each operation gets short-lived AWS credentials scoped to the one path it touches,
  so a bug in the storage code can't read past that path. A bug in grant resolution
  or in building that scope could; see [Security design](#security-design).
- Tokens are bound to your instance's URL.
- The bucket is encrypted with your own KMS key. Object Lock and retain-on-delete
  are off in the sample; turn them on if you adapt it
  ([docs/DEPLOY.md](docs/DEPLOY.md) §9).
- Every MCP call (reads included), every page of the web console's read view, write,
  grant and denial is audited for 90 days, and the API keeps an access log.
- The design is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), and a short threat
  model (assets, trust boundaries, threats, residual risks) in
  [docs/SECURITY.md](docs/SECURITY.md).
- What the controls don't cover, and what's left to you as the deployer, is in
  [Security design](#security-design) below.

## Cost

You pay for the AWS resources an instance uses; the sample itself is free. Most of
it is billed per request, so a quiet instance costs little. Prices below are for US
West (Oregon), the region `infra/config.py` sets, as of October 2026. Check
[AWS Pricing](https://aws.amazon.com/pricing/) or the
[AWS Pricing Calculator](https://calculator.aws/) for current rates.

These are billed every month, used or not:

| What | USD a month |
| --- | --- |
| KMS key `alias/wiki-<env>` | 1.00. Its first and second yearly rotations each add 1.00. |
| The web secret in Secrets Manager | 0.40 |
| Eight CloudWatch alarms | 0.80 (0.10 each) |
| The three metric filters' custom metrics, namespace `wiki/<env>` | up to 0.90 (0.30 each) |
| Contributor Insights rule `wiki-<env>-mcp-write-bursts` | 0.50, plus 0.02 per million write calls it matches |

That is about 3.60 USD a month in the first year, before any CloudWatch free-tier
allowance your account has left.

Billed by use:

- API Gateway requests, Lambda invocations and duration (arm64), DynamoDB
  on-demand reads and writes and point-in-time recovery storage, S3 storage and
  requests (every article version is kept), KMS requests past the free 20,000 a
  month, Secrets Manager API calls, X-Ray traces past the free 100,000 a month
  (tracing is on for the stage and every function), and AWS Backup storage for the
  daily grant-table backups, kept 35 days.
- CloudWatch Logs, at 0.50 USD per GB ingested and 0.03 USD per GB-month stored:
  the function logs, the API access log (one line per request) and the trail's log
  group. The trail's log group receives every management event in the account,
  from every Region, so its volume follows the account's activity, not this
  instance's.
- API Gateway's detailed CloudWatch metrics, which the stage turns on for every
  method. Each of the API's eight methods publishes about five to seven metrics,
  and CloudWatch bills each one as a custom metric at 0.30 USD a month, prorated by
  the hours it receives data. If every method sees traffic every hour, that is
  about 12 to 17 USD a month, several times the fixed charges above.
- CloudTrail. The ops stack creates a multi-Region trail of every management event
  in the account, plus write data events on the wiki bucket at 0.10 USD per
  100,000. If it is the account's only trail, CloudTrail doesn't charge for its
  management events; their copy in the trail's log group is still billed as
  CloudWatch Logs. If the account already has a trail, for example an AWS
  Organizations trail, this one is a second copy at 2.00 USD per 100,000
  management events, which in a busy account can outweigh everything else here.

Not on your AWS bill from these stacks: the domain and its DNS zone, the public
ACM certificate (free), WorkOS (billed by WorkOS; its staging environment needs no
payment method, its production environments do) and Claude plans.

The api stack's monthly budget `wiki-<env>-monthly` (20 USD) covers all spend in
the account, not only this instance. It emails at 80% only if you deploy with
`ALERT_EMAIL=`, and it never stops spend.

## Clean up

`make destroy` deletes the four stacks. Run it from a terminal, because the CDK
CLI asks you to confirm. Pass the account and AuthKit domain you deployed with;
synth refuses to run without them:

```sh
make destroy ENV=dev ACCOUNT=<12-digit id> AUTHKIT_DOMAIN=https://<slug>.authkit.app
```

With the sample's defaults (`retain_data` off) it deletes what the stacks created,
including the bucket with every article version in it and both tables. That is
permanent. These outlive it:

- The backup vault `wiki-<env>-grants` can't be deleted while it holds a recovery
  point, and the daily backup runs at 09:00 UTC. If the ops stack fails to delete
  for that reason, delete the vault's recovery points in the AWS Backup console
  and run `make destroy` again ([docs/RUNBOOK.md](docs/RUNBOOK.md) §8).
- The KMS key behind `alias/wiki-<env>` is scheduled for deletion and deleted 30
  days later. There is no charge while it waits. You can cancel the deletion in
  that time if you still need something encrypted with it, but then the key is
  billed for the waiting period as if the deletion had never been scheduled.
- API Gateway's account-level CloudWatch Logs role setting and the role it names
  (`wiki-<env>-api-ApiCloudWatchRole…`) are kept, because that one setting serves
  every REST API in the account and Region. If nothing else there logs to
  CloudWatch, clear the CloudWatch log role ARN in the API Gateway console's
  settings, then delete the role.
- Log groups that no stack owns, with no expiry: `API-Gateway-Execution-Logs_<api-id>/v1`
  (the stage's error log), `/aws/apigateway/welcome`, and the two
  `/aws/lambda/wiki-<env>-*-CustomS3AutoDeleteObjects*` groups of the functions
  that empty the buckets. Delete the ones nothing else uses.
- DNS and the certificate. With `HOSTED_ZONE_NAME`, the api stack deletes its alias
  record but the certificate's validation CNAME stays in the zone; delete it. With
  the hand-deployed certificate, `make destroy` doesn't touch the
  `wiki-<env>-certificate` stack. Remove it with
  `node_modules/.bin/cdk --app "uv run python -m infra.cert_app" destroy -c env=dev -c account=<id> -c domain=<host> -c authkitDomain=https://<slug>.authkit.app`.
  That deletes the SSM parameter and keeps the certificate, so that a stack delete
  can never take a live domain's certificate; delete the certificate in ACM
  afterwards. Then remove the records you added at your DNS provider: the
  validation CNAME and the one pointing at `DomainTarget`.
- The CDK bootstrap stack `CDKToolkit`, which every CDK app in that account and
  Region shares. If you bootstrapped only for this sample, delete the stack, then
  empty and delete its staging bucket `cdk-hnb659fds-assets-<account-id>-us-west-2`,
  which the stack keeps.
- Outside AWS: in WorkOS, delete the applications, the resource indicator and the
  users you created (or the whole environment); in Claude, remove the connector.

With `retain_data` on, `make destroy` also leaves the bucket, both tables, the key,
the web secret, the trail bucket, the backup vault and every log group in place,
and the grant table keeps its deletion protection
([docs/DEPLOY.md](docs/DEPLOY.md) §9). Delete them yourself when you mean to. With
Object Lock on, each article version is kept until its one-year retention ends.

## Security design

The bullets below cover what the built-in controls do: supply-chain pinning,
per-path credential scoping, encrypted secrets with rotation and alarms,
a recent AuthKit sign-in and CSRF-checked forms on admin actions, and audited writes.
They also say where those controls stop, for example rate limiting, backup
handling and log retention. Closing those gaps, and anything particular to
how you run your instance, is on you as the deployer.

- `make build` installs the Lambda dependencies from `uv.lock` with hashes, so a file
  PyPI serves that doesn't match the lock fails the build.
- GitHub Actions are pinned to commit SHAs, the gitleaks image to a digest, and the
  CDK CLI to an exact version in `package-lock.json`. The Makefile and CI run that CLI
  from `node_modules/` (installed by `npm ci`, which `make sync` runs), never a fresh
  download of the latest.
- Dependabot checks the Python lock, the CDK CLI and the Actions for updates every
  week and opens pull requests for the ones it finds. Nothing merges them
  automatically, and every CI job has a read-only token: each update waits for a
  person to review it and for CI to pass.
  Branch protection on `main` is a repository setting ([docs/DEPLOY.md](docs/DEPLOY.md) §0).
- CI runs `pip-audit` on the exact Lambda requirements and fails on a known
  vulnerability. Each run uploads a CycloneDX SBOM as the `sbom` artifact.
- Per-path isolation comes from session policies built in `app/auth/credentials.py`.
  The storage role itself can reach all of `a/*`, so IAM won't catch a bug in grant
  resolution (`app/auth/grants.py`) or in building the policy. Unit tests fuzz the
  path grammar and the ancestor walk on every CI run. `make security` and
  `make aws-tests` are verification you run against your own deployment, after any
  change to `app/auth` or `app/storage`; a skipped test is not a pass. `make aws-tests`
  assumes the storage role, which trusts only the MCP and web functions, so to run it
  temporarily add your own test principal to that role's trust policy in a
  test account, run it, then remove the principal again
  ([docs/DEPLOY.md](docs/DEPLOY.md) step 12).
- Enforce MFA in WorkOS for every member, and require it for anyone holding `own`.
  This is a dashboard setting, and WorkOS documents no `amr` claim, so the app can't
  check it. Keep access-token lifetimes short. Web sessions last 12 hours
  (`SESSION_HOURS` in `infra/stacks/compute.py`).
- Admin actions in the console (adding a person, granting, revoking, enabling or
  disabling someone, moving an article) need a sign-in through AuthKit that
  completed in the last 5 minutes. An older session is sent back through AuthKit,
  and you submit again. That stops someone who has only copied your session
  cookie, because they can't finish the sign-in. It is not a fresh password or code
  prompt: AuthKit usually answers from its own session without asking, since it
  doesn't re-prompt Magic Auth users for `max_age` and doesn't keep `auth_time`
  current, so the app can't rely on either ([docs/RUNBOOK.md](docs/RUNBOOK.md) §9).
- The console templates autoescape by default and render markdown with raw HTML
  disabled, so article content can't inject a script tag into the page. A
  Content-Security-Policy header restricts where scripts and frames can come from,
  and every admin form carries an HMAC CSRF token that's checked before the POST
  runs.
- Adding a person in the console takes the WorkOS subject id and the email or name
  you type; it doesn't check that email against the WorkOS identity. A typo or a
  deliberately wrong email records a profile that looks right but points at the
  wrong person, and other owners then grant to that profile believing it's who it
  says. Copy the subject id straight from WorkOS rather than retyping it.
- Anyone who can read the web secret can sign in to the console as anyone. The
  secret is encrypted with your KMS key, and only the web role and the rotation
  function are granted read. Any other read, an administrator's included, raises the
  `wiki-<env>-websecretread` alarm.
  `session_key` rotates every 30 days, and the previous key keeps working for one
  interval, so nobody is logged out. If you suspect a leak, rotate twice
  ([docs/RUNBOOK.md](docs/RUNBOOK.md) §4d). That logs everyone out.
- Article text is untrusted input to your agent. Anyone with write on a folder you
  read can plant instructions. The server stops privilege changes, but it can't stop
  your agent copying content you can already read. Keep write grants narrow. Every
  MCP tool that returns article text says in its description that the text is data,
  not instructions, and `read_article` and `search_and_read` return who last wrote
  each article and when (`actor`, `at`). The `wiki-<env>-writeburst` alarm fires
  when one person's agent makes 30 or more write calls in 5 minutes, and a
  CloudWatch Contributor Insights rule on the MCP tool-call log names who it was
  ([docs/RUNBOOK.md](docs/RUNBOOK.md) §6). A few quiet writes stay under it. The
  rule and the alarm are listed under [Cost](#cost). Nothing alerts a folder's
  owners when someone outside the folder writes into it; that is optional
  hardening for you to add.
- Owners are trusted. Anyone with `own` on a folder can share it, including
  re-delegating `own`. Every grant records who made it and when. Review grants
  periodically on the console's Grants page. An owner of `/` should also check
  Unowned grants, which lists grants whose granter is disabled or no longer owns the
  place. A move that widens access shows who gains and who loses before you confirm
  it, and agents can't make one. Nothing alerts when someone is granted `own`;
  adding that alert is optional hardening for you, and it isn't built.
- No AWS WAF is attached. `/mcp` and the `/app` console share one stage throttle of
  20 requests a second with a burst of 50 (`infra/stacks/api.py`), so an
  unauthenticated flood can use it up for everyone. The per-person MCP limits (60
  calls a minute, 200 writes an hour, `app/auth/ratelimit.py`) fail open if their
  table errors; five such errors in 5 minutes raise the
  `wiki-<env>-ratelimitunavailable` alarm. The console has no per-user limit. If you
  adapt this sample, attach a web ACL with a per-IP rate-based rule and the AWS
  managed IP reputation rules, and keep the stage throttle. The 20 USD monthly budget
  emails at 80% only if you deploy with `ALERT_EMAIL=` (`-c alertEmail=`), and it
  never stops spend.
- Audit logs are kept 90 days. The MCP function logs one line per tool call (who,
  which tool, which path, allow or deny, and the grants it relied on). The web
  function logs one `web_read` line per page of the read view (who, which page,
  which path and version, allow or deny), plus grants, revokes, people changes and
  moves. The API stage writes an access log with the request id, time, source IP,
  method, path, status and size, and the token subject on `/mcp`. It never records
  headers, cookies or query strings, and the request id joins its lines to the
  functions' lines. These log groups have a fixed 90-day retention
  (`APP_LOG_RETENTION` in `infra/stacks/compute.py`), so keeping them longer is a
  code change. `log_retention_days` in `infra/config.py` sets only the CloudTrail
  log group and trail bucket. CloudTrail records S3 writes but not reads, so after
  90 days you can't show who read what. The console's audit page shows only the
  MCP tool-call lines of the last 30 days for one path. Console reads and the
  access log aren't on it, so query them with CloudWatch Logs Insights
  ([docs/RUNBOOK.md](docs/RUNBOOK.md) §8). Who wrote each article version stays in
  that version's metadata. If you have compliance needs, export the logs before
  they expire, for example to an Object Lock bucket.
- Backups and any exports carry no permission model, so treat them as full-tree
  access. Anyone who can read the bucket directly sees every article, and anyone who
  can restore a grant-table backup sees every grant, whatever their own grants. The
  bucket, the grant table, the backup vault `wiki-<env>-grants` and the trail bucket
  are encrypted with your KMS key, and the bucket refuses principals from other
  accounts (`infra/stacks/storage.py`, `infra/stacks/ops.py`). There is no
  snapshot-share alarm, because DynamoDB recovery points have no share call to watch
  ([docs/RUNBOOK.md](docs/RUNBOOK.md) §6). A copy you take out of AWS, such as the
  `/tmp` files the runbook's restore steps use, is plaintext with no grants attached;
  delete it when you're done. Rehearse a restore once, then yearly, and record the
  date ([docs/RUNBOOK.md](docs/RUNBOOK.md) §7).
- Reads of paths you can't see look like "not found", in MCP tools and in the
  console. Writes return "forbidden" when you lack write. Both answers come from the
  grant check before storage is touched, so neither tells you whether the path
  exists. Some metadata does show: a link or moved-article pointer in a folder you
  can read names its target even if you can't open it, and a move preview shows
  which folder an access change comes through but names only people you administer.
  Content never leaks this way.
- Revocation is prospective. Content already in someone's agent history stays there,
  and so do their notes and anything they copied. Disabling a person in the console
  takes effect on their next MCP call or click: their grants resolve to nothing and
  their console session is refused. Their grant rows stay, so revoke those too, and
  deactivate them in WorkOS yourself; nothing here does that for you
  ([docs/RUNBOOK.md](docs/RUNBOOK.md) §5).

## Scope

The sample covers one organization per instance, with people added by an owner and
every write made by an agent acting for a signed-in person.

Not in this sample: semantic search over article bodies, subscriptions (your agent
tells you when something you watch changes), and multi-tenant hosting.

The design reference is [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), the spec the code
is built against. Where the code and that document disagree, fix the code.

## Developing

| Path | What |
| --- | --- |
| `app/authorizer/` | API Gateway REQUEST authorizer: JWT validation, nothing else |
| `app/mcp/` | MCP transport (`server.py`) and the fourteen tools (`tools/`) |
| `app/auth/` | Grant resolution, per-operation credential minting, grant administration, rate limits |
| `app/storage/` | S3 reads and conditional writes, frontmatter, the listing projection |
| `app/web/` | The web application: login, read view, admin console |
| `infra/` | CDK: storage, compute, api, ops stacks |
| `scripts/` | `oauth_gate.py` (the WorkOS gate), `grant_owner.py` (bootstrap the first owner) |
| `tests/unit/` | Unit tests, no AWS (moto) |
| `tests/security/` | The security checklist as integration tests against a deployed instance |
| `docs/` | `SETUP.md` (start here), `DEPLOY.md` (reference), `RUNBOOK.md` (operations), `ARCHITECTURE.md` (design), `SECURITY.md` (threat model) |

```
make sync        # dependencies
make test        # unit tests
make lint        # ruff
make typecheck   # mypy
make build       # package the Lambda functions (arm64, py3.13)
make synth ENV=dev
make deploy ENV=dev  # asks before new IAM permissions; REQUIRE_APPROVAL=never skips that
make security    # after deploy; env contract in tests/security/README.md
make aws-tests   # the storage-isolation negative tests, with real credentials
```

Changes go through pull requests; see [CONTRIBUTING](CONTRIBUTING.md). Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) §1, §2, §4 and §11 before touching
authorization or storage.

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

Note: This project depends on the certifi package, which is licensed under MPL-2.0, a
copyleft license. Its development and test tools also depend on hypothesis and
pathspec, which are licensed under MPL-2.0.

This library is licensed under the MIT-0 License. See the LICENSE file.
