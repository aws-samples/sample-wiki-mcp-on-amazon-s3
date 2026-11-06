# Runbook

What to do when something needs doing by hand. Section references are to
`docs/ARCHITECTURE.md`. Every procedure here assumes `AWS_PROFILE` points at the right account
(§9.6 — each instance has its own account; check `aws sts get-caller-identity`
before anything below) and that the stack outputs are at hand (`docs/DEPLOY.md` §4).

Naming used throughout: `<bucket>` is the storage stack's `BucketName` output,
`<key-arn>` its `KmsKeyArn`, `<table>` its `GrantTableName`, `<env>` is `dev`. Article `/racing/setup.md` is object key `a/racing/setup.md` (§8.2).

---

## 1. Restore a single article version

Nothing is ever overwritten in place: every write appends a version to the key
(§8.2, §8.9). Restoring is therefore **re-putting an older body as the newest
version**. The old versions stay underneath.

### 1a. Through the MCP tools (preferred)

Works for anyone with `write` on the path, keeps attribution honest, and needs no
AWS access. From any connected client (or `scripts/` with an owner token):

1. `list_versions(path)` — pick the `version_id` you want back. Entries carry
   `kind` and `actor`, so "the last version before the agent rewrote it" is
   readable.
2. `read_version(path, version_id)` — returns `content` and `frontmatter`.
3. `read_article(path)` — returns the **current** `version` token.
4. `update_article(path, content=<step 2 content>, if_version=<step 3 version>,
   frontmatter=<step 2 frontmatter if wanted>)`.

The result is a new version with `kind: write` and your subject as `actor`. If the
article is archived, `unarchive_article` first; if it was moved, the path in step 1
is the new one (`read_article` on the old path returns a forward reference).

### 1b. Through S3 directly

Only when the tool path is unavailable (the API is down, or the article sits
outside anyone's grants). Use an administrator session; the break-glass role has
no `PutObject` on purpose — restores are not what it is for.

```sh
KEY=a/racing/setup.md
aws s3api list-object-versions --bucket <bucket> --prefix "$KEY" \
  --query 'Versions[?Key==`'"$KEY"'`].{id:VersionId,at:LastModified,latest:IsLatest,size:Size}' \
  --output table

# inspect the candidate's attribution before trusting it
aws s3api head-object --bucket <bucket> --key "$KEY" --version-id <VersionId> \
  --query '{actor:Metadata.actor,kind:Metadata.kind,ct:ContentType}'

aws s3api get-object --bucket <bucket> --key "$KEY" --version-id <VersionId> /tmp/restore.md

# re-put as the newest version. Metadata is REQUIRED: without it the version
# reads as `actor: unknown` in list_versions (§8.2).
aws s3api put-object --bucket <bucket> --key "$KEY" --body /tmp/restore.md \
  --content-type 'text/markdown; charset=utf-8' \
  --metadata actor=human:<your-subject>,kind=write
rm /tmp/restore.md
```

Bucket default encryption applies (SSE-KMS under `<key-arn>`), so no encryption
flags are needed. The listing projection for the folder refreshes itself on the
next read (area A); if it looks stale, any write in the folder forces it.

## 2. Restore a folder

The same operation for every key under a prefix, choosing "the newest version
before time T" for each. Object Lock (when on) is irrelevant here — nothing is deleted.

```sh
PREFIX=a/racing/
T=2026-09-01T00:00:00Z
aws s3api list-object-versions --bucket <bucket> --prefix "$PREFIX" --output json \
  > /tmp/versions.json

# For each key, the newest version whose LastModified <= T. Skip _listing.json;
# it is rebuilt. Review this list BEFORE running the restore loop.
jq -r --arg t "$T" '
  .Versions
  | map(select(.LastModified <= $t and (.Key | endswith("/_listing.json") | not)))
  | group_by(.Key)
  | map(max_by(.LastModified))
  | .[] | "\(.Key)\t\(.VersionId)"' /tmp/versions.json | tee /tmp/restore-plan.tsv

while IFS=$'\t' read -r key vid; do
  aws s3api get-object --bucket <bucket> --key "$key" --version-id "$vid" /tmp/body >/dev/null
  aws s3api put-object --bucket <bucket> --key "$key" --body /tmp/body \
    --content-type 'text/markdown; charset=utf-8' \
    --metadata actor=human:<your-subject>,kind=write >/dev/null
  echo "restored $key from $vid"
done < /tmp/restore-plan.tsv
rm -f /tmp/body /tmp/versions.json /tmp/restore-plan.tsv
```

Caveats worth reading twice:

- A key whose newest-before-T version is a **pointer** (`type: pointer`) or an
  **archive tombstone** (`type: archived`) is restored to that state, which is
  usually right — it was moved or archived before T. If you want the content
  back, pick an earlier content version for that key (1b) or use
  `unarchive_article`.
- Keys created **after** T are left as they are; a folder restore does not delete.
  Archive them through the tools if they should not exist.
- Do this in dev first with the same script and a synthetic folder. It is a loop of
  `put-object`; the only thing that can go wrong is the plan, so read the plan.

## 3. Hard delete — the break-glass procedure

**There is deliberately no function, tool, or admin-console button that can do
this.** §8.9 puts hard delete where a person has to go looking for it, and §8.8
gives the permission to one role that nothing running code can assume:
`wiki-<env>-break-glass` (`BreakGlassRoleArn` output). Every assumption of it fires
the `wiki-<env>-break-glass-used` alert; every delete it performs is a write data
event in the trail.

The role exists only when the deploy names who may assume it, with
`BREAK_GLASS_PRINCIPAL_ARN=` (`-c breakGlassPrincipalArn=`, DEPLOY §3). Its trust
policy accepts only that principal, through an `aws:PrincipalArn` condition, so no
other administrator in the account can switch to it. Without the value there is no
role, and nothing these stacks create can hard-delete from the article bucket except
two things. One is the bucket's auto-delete custom resource: with `retain_data` off,
it deletes every version Object Lock does not hold when the stack deletes the bucket,
as `make destroy` does. The other is the bucket's lifecycle rule, which expires a
`_listing.json` version 365 days after a newer one replaces it. Listings are
projections rebuilt from the articles, and the articles' own versions are untouched.
Deploy again with the value, and keep passing it on every deploy, because a deploy
without it removes the role. The trust has no MFA condition: require MFA where you
sign in (IAM Identity Center or your identity provider). DEPLOY §9 explains why and
gives a service control policy that keeps `s3:BypassGovernanceRetention` to this role.

When it is warranted: content that must not exist in any version (something pasted
by mistake that is not the family's to keep), or a legal request. Not for "it is
untidy" — that is `archive_article`.

1. **Open a ticket** (an issue, a note in the ops log — anything with a date and
   your name). Record: the path(s), the version ids or "all", why, who asked.
   The alert email that arrives when you assume the role is the second half of the
   record; keep both.
2. **Switch role in the console**, signed in as the principal named in
   `BREAK_GLASS_PRINCIPAL_ARN`: account id, role name `wiki-<env>-break-glass`.
   Sessions last one hour. Or on the CLI:

   ```sh
   CREDS=$(aws sts assume-role --role-arn <BreakGlassRoleArn> \
     --role-session-name "hard-delete-$(date +%Y%m%d)-<ticket>" --query Credentials --output json)
   export AWS_ACCESS_KEY_ID=$(jq -r .AccessKeyId <<<"$CREDS") \
          AWS_SECRET_ACCESS_KEY=$(jq -r .SecretAccessKey <<<"$CREDS") \
          AWS_SESSION_TOKEN=$(jq -r .SessionToken <<<"$CREDS")
   ```

   The session name lands in CloudTrail; put the ticket reference in it.
3. **List every version** of the key, including delete markers. Hard delete means
   every one of them; leaving one means it is not gone.

   ```sh
   KEY=a/racing/setup.md
   aws s3api list-object-versions --bucket <bucket> --prefix "$KEY" \
     --query '{v:Versions[?Key==`'"$KEY"'`].VersionId, d:DeleteMarkers[?Key==`'"$KEY"'`].VersionId}'
   ```

4. **Delete each version**. With Object Lock on, the versions are under governance
   retention for a year (§8.9); the role holds `s3:BypassGovernanceRetention`, and
   the flag must be passed explicitly or S3 refuses:

   ```sh
   for vid in <VersionId ...>; do
     aws s3api delete-object --bucket <bucket> --key "$KEY" --version-id "$vid" \
       --bypass-governance-retention
   done
   ```

   In dev (no Object Lock) omit the flag. `delete-object` without `--version-id`
   only adds a delete marker; that is not a hard delete.
5. **Confirm** the list in step 3 is empty, then `unset AWS_ACCESS_KEY_ID
   AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN` (or sign out of the switched role).
6. The folder's `_listing.json` still names the article until the next rebuild;
   force one with any write in that folder, or delete the listing object the same
   way (it is regenerated). Search results follow the listing.
7. **Close the ticket** with the alert email attached and the count of versions
   removed. Note in it whether the content had reached anyone's context before the
   delete — §12.9: revocation and deletion are prospective; a hard delete does not
   pull text back out of a conversation.

Why the role has no `PutObject`: so that a break-glass session cannot quietly
rewrite history as well as erase it. Restores are section 1.

## 4. Rotations

### 4a. The KMS key

Automatic. `enable_key_rotation=True` on `wiki-<env>` rotates the backing material
yearly; the key ARN, alias, and every existing ciphertext stay valid, and nothing
in the application or the stacks needs to change. There is no manual step and no
alarm for it. Do **not** replace the key with a new one: every object, both tables,
the trail bucket, the backup vault and the web secret are bound to this ARN.

### 4b. The WorkOS client secret (web application)

The MCP path has no client secret — the authorizer validates tokens against the
JWKS endpoint (AS-4). The web application (area F) is a confidential OAuth
client whose secret lives in one Secrets Manager secret (`WEB_SECRET_ARN`). To
rotate:

1. WorkOS dashboard → the web application's client → generate a new secret. Both
   the old and the new are valid until you delete the old.
2. `aws secretsmanager put-secret-value --secret-id <WEB_SECRET_ARN>
   --secret-string '{"client_secret":"<new>","session_key":"<current>","previous_session_key":"<current previous>"}'`.
   `put-secret-value` replaces the whole string, so read the current value first
   (`get-secret-value`) and carry `session_key` and, if present,
   `previous_session_key` over unchanged. A changed `session_key` logs everyone out
   (4d). That read fires `wiki-<env>-websecretread` (section 6); expected.
3. Wait an hour for warm containers to re-read the secret (or redeploy the compute
   stack), then confirm a fresh browser login works.
4. Delete the old secret in the WorkOS dashboard. A login that fails at this point
   means a container still holds the old value; repeat step 3.

### 4c. Anything else

There are no long-lived AWS access keys (§8.8), and no function holds a WorkOS
management API key (docs/ARCHITECTURE.md §11.6 — people are created in the WorkOS dashboard and
recorded in the console), so there is nothing else to rotate. If an access key ever
exists, `make security` reports it as a warning; treat that as a finding. If a
management API key ever exists, that is a design regression, not a rotation item.

### 4d. Rotation cadence

Nothing alarms on an overdue rotation; put the dates in a calendar that is read.

| What | When | How |
| --- | --- | --- |
| WorkOS client secret (`client_secret` in the web secret) | Every **90 days**, and on any suspicion of exposure | 4b: create the new secret in the WorkOS dashboard → `put-secret-value` with the new value, other fields carried over → verify a fresh browser login → revoke the old secret in the dashboard. |
| WorkOS management API key | — | None exists: no function holds one (docs/ARCHITECTURE.md §11.6). People are created in the WorkOS dashboard and recorded through the console. |
| Session key (`session_key` in the same secret) | Automatic, every **30 days**; by hand on a suspected leak, an operator leaving | Scheduled: nothing to do. The rotation function (`app/web/rotate.py`) writes a new key and moves the old one to `previous_session_key`. The web app signs with the new key and still accepts the previous one, so a scheduled rotation logs nobody out. Containers re-read the secret within an hour. **Suspected leak: rotate twice.** Run `aws secretsmanager rotate-secret --secret-id <WEB_SECRET_ARN>`, wait for it to finish (`list-secret-version-ids` shows the new version as `AWSCURRENT`; `AWSPENDING` stays on that same version, which is how the rotation function leaves it and counts as finished), run it again, then redeploy the compute stack or wait an hour. The leaked key is then in neither slot. **This logs everyone out**, so tell people before, not after. One person's sessions end without a key change: their own "Sign out everywhere" button bumps their profile's `session_epoch`, and every cookie issued before it is refused on its next request (docs/ARCHITECTURE.md §11.6). |
| KMS key `wiki-<env>` | Automatic, yearly | Nothing to do (4a). Never replace the key. |
| Long-lived AWS access keys | — | None exist (4c). |

## 5. Revoke a person

Three places, in this order; each is independent and each is necessary.

1. **Revoke their grants.** Every row for the subject, through the console or
   `GrantAdmin.revoke` as an owner of each node. This is what stops access: the
   data plane resolves grants on every call (AS-6, cache TTL zero), so a token they
   still hold answers `404`/`403` from the next call on. Zero grants also means
   nothing is minted for them (§4.8).
2. **Disable the profile.** In the admin console (or through `GrantAdmin.set_status`
   with operator credentials): status → `disabled`. This takes effect on the next
   call. The data plane reads the PROFILE row in the same lookup as the grants and
   resolves a disabled person to no grants (`_grants_on` and `all_grants` in
   `app/auth/grants.py`), and the console refuses their session on the next click
   and at login. Their grant rows stay, so re-enabling the profile brings every
   grant back; that is why step 1 still matters. Keep doing both.
3. **Deactivate them at WorkOS** (dashboard → Users → deactivate, or delete). Steps 1
   and 2 already stop access from the next call on, whatever token their client
   has. WorkOS's documentation doesn't say whether AuthKit then refuses a refresh
   token their MCP client already holds, so confirm that in your WorkOS environment
   before you count on this step to stop new access tokens. Nothing here does this
   step for you. Do it too.

Then say this plainly to whoever asked, because §12.9 requires it: **revocation is
prospective.** It stops future reads. It does not remove anything already placed in
the person's conversation history, notes, or an agent's context. If the concern is
what they already saw, the answer is "they saw it", not a setting.

## 6. Alarms — what each one means and what to do

All of them publish to `wiki-<env>-alerts` (`AlertsTopicArn`). If nobody receives
that topic, none of this matters: confirm the subscription after every fresh deploy.

| Alarm / rule | Fires when | First response |
| --- | --- | --- |
| `wiki-<env>-api5xx` | API Gateway returned any 5xx in 5 min | Check `wiki-<env>-mcperrors` first; if it is quiet, the 5xx is the gateway's own (integration timeout at 29 s, authorizer crash rather than rejection). Look at the API's execution log for the request id. |
| `wiki-<env>-authfailures` | ≥ 20 authorizer errors in 5 min | Every rejection is a raise, so this is normally a client retrying a bad token — one person's stale connector. Find the subject in the authorizer log. Sustained from many sources with no known user: check that the JWKS endpoint is reachable (an outage there rejects everyone) and that the last deploy used the same `AUTHKIT_DOMAIN`. |
| `wiki-<env>-mcperrors` | any unhandled crash in the data plane | The tool boundary logs `tool_crash` with a stack (§12.3: the response body says nothing, the log says everything). Search the MCP log group for `tool_crash`. |
| `wiki-<env>-mcpthrottles` | Lambda throttled the MCP function | Concurrency, not the gateway throttle (that returns 429 without invoking). A loop somewhere: find the top subject in the application log (`subject`, `tool` per line) and rate-limit or disable them. Raise the account's concurrency only if the traffic is legitimate. |
| `wiki-<env>-writeburst` | One person's agent made ≥ 30 MCP write calls (create, update, edit, move, archive, unarchive; refused and rate-limited ones count) in 5 min | Find who and what with the commands below the table. If they asked for it (an import, a reorganisation), it's expected; if that's routine, raise `WRITE_BURST_THRESHOLD` in `infra/stacks/ops.py`. If not, treat it as an agent following instructions it read: disable the person (section 5, step 2) until they've checked, look at what their agent read just before the burst for the article that steered it, and put changed articles back from version history (section 1). |
| `wiki-<env>-listingfailures` | ≥ 5 `listing_refresh_failed` / `listing_rebuilt_unpersisted` in 15 min | Readers still get correct answers from a live rebuild; the projection just is not being saved. Usually two writers racing on one folder — benign, self-clearing. Sustained: check the storage role's `s3:PutObjectTagging` on listing keys and the function's log for the S3 error. |
| `wiki-<env>-ratelimitunavailable` | ≥ 5 `ratelimit_unavailable` lines in 5 min: the per-person MCP limits couldn't update their DynamoDB table, so those calls were allowed uncounted (a write call logs two lines) | Calls still succeed and authorization is unaffected; only the per-person limits are off, and the stage throttle still applies. Search the MCP log group for `ratelimit_unavailable`; `exc_class` names the error. `ResourceNotFoundException` means the table `wiki-<env>-ratelimit` is missing. A plain `ClientError` is usually access: check the MCP role's `dynamodb:UpdateItem` on that table and its use of the KMS key. A throttle, `InternalServerError`, or a connection or timeout error is DynamoDB itself: check the AWS Health Dashboard. Until it clears, watch `wiki-<env>-writeburst` and the budget. |
| `wiki-<env>-websecretread` | `GetSecretValue` on the web secret by any principal other than the web role or the rotation function | Expected when you are doing 4b. Otherwise treat it as a leak of `session_key` and `client_secret`: the trail event names the principal. Rotate the session key twice (4d) and the client secret (4b), then check CloudTrail for what else that principal did. |
| `wiki-<env>-bucket-guard` (rule) | A bucket policy, ACL, public-access block, versioning, Object Lock, encryption or lifecycle change on **any** bucket in the account, or the account-level block | If it was you deploying, it matches the deploy. If not, treat it as an incident: the event carries the principal; check CloudTrail for what else that principal did, and restore the setting. |
| `wiki-<env>-break-glass-used` (rule) | Someone assumed the break-glass role (the rule exists only with the role, section 3) | There should be a ticket (section 3). If there is not, someone the role trusts used it without the procedure — find out who from the `AssumeRole` event and what they deleted from the trail's S3 write events. |
| Budget `wiki-<env>-monthly` | 80 % of 20 USD in the month | Almost always CloudTrail data events or a loop. Cost Explorer by service, then the application log by subject. |

For `wiki-<env>-writeburst`: the rule `wiki-<env>-mcp-write-bursts` (CloudWatch
console → Contributor Insights) lists the busiest subjects for any window, or from
the CLI:

```
aws cloudwatch get-insight-rule-report --rule-name wiki-<env>-mcp-write-bursts \
  --start-time <alarm time minus 10 min> --end-time <alarm time> --period 300 \
  --max-contributor-count 5
```

The subject is a WorkOS user id; the console's `/app/admin/people/<subject>` page
shows the person. What their agent did, reads included, is in the MCP function's
log group (Logs Insights):

```
fields @timestamp, tool, path, decision, status
| filter message = "tool_call" and subject = "<subject>"
| sort @timestamp asc
```

Not alarmed, by design: 4xx counts (noisy, and every denial is already a log line —
§12.7), and DynamoDB backups shared outside the account (§12.5 asks for it, but
AWS Backup recovery points for DynamoDB **cannot be shared** to another account —
there is no API call to alarm on; the vault's key is the control, and the vault
access policy is the place to look if that ever changes).

## 7. Backups and the restore rehearsal (§12.8 item 7)

What exists:

- **Articles**: S3 versioning is the backup, Object Lock (when on) is what stops it
  being deleted (§8.9). There is no second copy of the bucket; a second copy would
  be the disclosure path §12.5 warns about, and versioning already survives every
  failure short of losing the bucket.
- **Grants**: DynamoDB point-in-time recovery (35 days, continuous) plus an AWS
  Backup daily snapshot at 09:00 UTC into vault `wiki-<env>-grants`, kept 35 days,
  encrypted with the same customer-managed key. Two mechanisms because they fail
  differently: PITR dies with the table; the vault does not.
- **Rate-limit counters**: not backed up. They expire on their own.
- **CloudTrail**: the trail bucket, 90 days.

**Rehearse the restore once, then once a year, and write the date below.** A backup
that has never been restored is a hope.

### Rehearsal (dev account, ~20 minutes)

1. Article: pick a synthetic article, `update_article` it twice, then restore the
   first version by 1a. Confirm `list_versions` shows three content versions and
   the newest body equals the first.
2. Folder: run section 2 against a synthetic folder with `T` set between two
   writes. Confirm the plan matched expectation before the loop ran.
3. Grant table, from the vault: AWS Backup console → vault `wiki-dev-grants` → newest
   recovery point → Restore → **new table name** (`wiki-dev-grants-rehearsal`).
   Wait for it. `aws dynamodb scan --table-name wiki-dev-grants-rehearsal --select COUNT`
   should match the live table's count. Then delete the rehearsal table.
   (A real restore would then repoint `GRANT_TABLE` on the compute stack at the
   restored table or copy the rows back; the point of the rehearsal is knowing the
   recovery point is readable and the key permits it.)
4. Grant table, from PITR: `aws dynamodb restore-table-to-point-in-time
   --source-table-name <table> --target-table-name wiki-dev-grants-pitr
   --use-latest-restorable-time`, confirm the count, delete it.
5. Hard delete: run section 3 on a synthetic article in **dev** (deploy dev with
   `BREAK_GLASS_PRINCIPAL_ARN` first) so that the
   procedure has been executed by a person before it is needed in anger, and so
   the `break-glass-used` alert is known to arrive.

### Record

| Date | Environment | Who | Steps done (1–5) | Notes |
| --- | --- | --- | --- | --- |
| _none recorded_ | | | | The §12.8 checklist is not complete until a row exists here. |

## 8. Things that are out of scope, stated so nobody looks for them

- **AWS Config** — not deployed. The bucket-guard rule and the trail cover the
  changes that matter to this design; Config rules would add a monthly charge and
  a second console to read.
- **Snapshot-sharing alarm** — N/A for DynamoDB, see section 6.
- **CloudTrail S3 read events** — off, on purpose (§12.7). The application logs
  record every read an agent or the console's read view makes: `tool_call` lines in
  the MCP function's log group, `web_read` lines in the web function's log group,
  and the API access log (api stack output `AccessLogGroupName`), all kept 90 days.
  The console's audit page shows only `tool_call` lines. For console reads, run a
  Logs Insights query on the web function's log group, for example
  `fields @timestamp, subject, view, path, version, decision | filter message = "web_read" and path = "/racing/setup/rear-bar.md"`.
- **`make destroy ENV=dev`** with a non-empty backup vault fails: AWS refuses to
  delete a vault holding recovery points. Delete them in the console first, or
  wait for the 35-day lifecycle. With `retain_data` on, the vault is retained regardless.

## 9. Admin actions keep failing at sign-in

Admin POSTs (add a person, grant, revoke, enable or disable, move) need a sign-in
through AuthKit that completed in the last 5 minutes (`STEP_UP_SECONDS`,
`app/web/session.py`). The app records that time itself when the login callback
succeeds; it does not use the IdP's `auth_time`, because AuthKit sends none on the
ID token and does not advance the one on its access token after a Magic Auth
sign-in. An older session is sent through AuthKit with `max_age=300`. AuthKit
usually answers at once from its own session, without a prompt, and the person
lands on the admin page and submits again; the second submit runs.

If the round trip keeps ending at "The login could not be verified", the web log
line `login_failed` gives the reason: a state, nonce or token check failed, or the
code exchange was refused (check the web client secret, 4b). "The login did not
complete" means the browser came back without the short-lived sign-in cookie, for
example after more than 10 minutes at AuthKit: start again.
