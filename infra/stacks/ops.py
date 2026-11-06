# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Ops stack — docs/ARCHITECTURE.md §8.8, §12.5, §12.7, increment G.

Everything here watches or copies; nothing here serves a request.

* **Alerts topic** ``wiki-<env>-alerts`` — every alarm and rule below publishes to it.
  An email subscription is attached when context ``alertEmail`` is given. The topic
  is not KMS-encrypted on purpose: messages carry resource names and metric values,
  never article content, and an encrypted topic needs key-policy grants for three
  more service principals. Its resource policy lets ``events.amazonaws.com`` publish
  **only on behalf of this stack's rules** (``aws:SourceArn`` lists their ARNs) — the
  CDK ``SnsTopic`` target would grant the service principal unconditionally, which
  is any rule in any account, so the rules bind through ``_AlertsTarget`` instead.
  The CloudWatch alarms need no statement: same-account alarm actions are allowed
  without one, and none is written. A deny refuses any publish not made over TLS
  (``enforce_ssl``).
* **CloudTrail** — one multi-region trail: management events (all), **S3 data events
  for the wiki bucket, write-only** (§12.7 — "what touched the bucket outside the
  application"), log-file validation on. Delivered to its own log bucket (SSE-KMS with
  the storage key, public access blocked, TLS enforced, CloudTrail admitted only for
  this trail through ``aws:SourceArn``, expires after ``cfg.log_retention_days``) and
  to a CloudWatch log group with the same retention. CDK's ``Trail`` only names the
  key; the two statements CloudTrail needs on the key policy (``GenerateDataKey*``
  under its encryption context, ``DescribeKey``; both pinned to this trail by
  ``aws:SourceArn``) are added here to the storage stack's key.
* **Bucket-policy watch** — an EventBridge rule on the CloudTrail management events
  that change a bucket's policy, ACL, public-access block, versioning, Object Lock,
  encryption or lifecycle, for any bucket in the account (§8.8: "an alarm on any
  bucket policy change or public grant"). The account-level public-access-block calls
  are included.
* **Alarms** (§12.7) — API ``5XXError`` ≥ 1 / 5 min; authorizer ``Errors`` ≥ 20 / 5 min
  (every rejection is a raise, so this is the authentication-failure spike); MCP
  ``Errors`` ≥ 1 / 5 min; MCP ``Throttles`` ≥ 1 / 5 min; a metric filter on the MCP
  log group for ``listing_refresh_failed`` / ``listing_rebuilt_unpersisted`` with an
  alarm at ≥ 5 / 15 min; a metric filter on the MCP log group for
  ``ratelimit_unavailable`` (the per-subject limiter could not update its table and
  allowed the call) with an alarm at ≥ 5 / 5 min; a metric filter on the trail log
  group for ``GetSecretValue`` on the web secret by anyone but the web role and the
  rotation function, alarm at ≥ 1 / 5 min. A Contributor Insights rule
  ``wiki-<env>-mcp-write-bursts`` on the MCP log group counts each subject's
  write-tool ``tool_call`` lines, refused ones included, and an alarm fires when the
  busiest subject reaches ≥ 30 / 5 min; the rule's report names the subject.
* **Break-glass role** (§8.8, §8.9) — ``wiki-<env>-break-glass``: the only principal
  holding ``s3:DeleteObjectVersion`` and ``s3:BypassGovernanceRetention`` on the wiki
  bucket. Created only when the deployer names who may assume it
  (``breakGlassPrincipalArn``); its trust is this account conditioned on
  ``aws:PrincipalArn``, so nobody else in the account can assume it. No MFA condition
  (federated sessions lack the key; MFA is the sign-in's). Without that value there is
  no role, rule or output. One-hour sessions, never attached to a function. An
  EventBridge rule alerts on every ``AssumeRole`` of it; every delete it performs is
  a write data event on the trail.
* **AWS Backup** (§12.5) — a vault encrypted with the storage key, a plan taking a
  daily snapshot of the grant table kept ``cfg.backup_retention_days`` days. The
  bucket's backup is its own versioning plus Object Lock; the rate-limit table holds
  nothing worth keeping. DynamoDB backups cannot be shared to another account, so
  "alarm on any snapshot shared beyond this account" has nothing to watch here
  (see ``docs/RUNBOOK.md``). AWS Config is out of scope.

Depends on ``StorageStack`` (bucket, key, table), ``ComputeStack`` (the two functions
and their log groups) and ``ApiStack`` (the REST API name). Nothing depends on this
stack, so it can be added or torn down independently.

Outputs: AlertsTopicArn, TrailArn, TrailLogBucketName, BackupVaultName, and
BreakGlassRoleArn when the role exists.
"""

from __future__ import annotations

import aws_cdk as cdk
import jsii
from aws_cdk import Duration, RemovalPolicy
from aws_cdk import aws_backup as backup
from aws_cdk import aws_cloudtrail as cloudtrail
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_events as events
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subscriptions
from constructs import Construct

from infra.config import EnvConfig
from infra.stacks.api import ApiStack
from infra.stacks.compute import ComputeStack
from infra.stacks.storage import StorageStack

#: CloudTrail management events that change who can reach a bucket or what can be
#: deleted from it. ``PutAccountPublicAccessBlock`` / ``DeleteAccountPublicAccessBlock``
#: are the account-level S3 Control calls; they also arrive with ``s3.amazonaws.com``
#: as the event source.
BUCKET_GUARD_EVENTS: tuple[str, ...] = (
    "PutBucketPolicy",
    "DeleteBucketPolicy",
    "PutBucketAcl",
    "PutBucketPublicAccessBlock",
    "DeleteBucketPublicAccessBlock",
    "PutAccountPublicAccessBlock",
    "DeleteAccountPublicAccessBlock",
    "PutBucketVersioning",
    "PutObjectLockConfiguration",
    "PutBucketEncryption",
    "DeleteBucketEncryption",
    "PutBucketLifecycle",
    "DeleteBucketLifecycle",
    "DeleteBucket",
)

#: Log lines from the listing projection (increment A) that mean the cache could not
#: be refreshed or persisted. The data plane still answers from a live rebuild, so a
#: burst is a warning, not an outage; one or two is normal contention.
LISTING_FAILURE_TERMS: tuple[str, ...] = ("listing_refresh_failed", "listing_rebuilt_unpersisted")

#: The ``message`` of the line ``app/auth/ratelimit.py`` logs when a counter update
#: fails and the call is allowed uncounted (the limiter fails open). Each failed
#: counter update logs it once, so a write call logs it twice; five in
#: ``ALARM_PERIOD`` is several unlimited calls, not one transient error.
RATELIMIT_UNAVAILABLE_EVENT = "ratelimit_unavailable"

ALARM_PERIOD = Duration.minutes(5)
API_5XX_THRESHOLD = 1
AUTHORIZER_ERROR_THRESHOLD = 20
MCP_ERROR_THRESHOLD = 1
MCP_THROTTLE_THRESHOLD = 1
LISTING_FAILURE_PERIOD = Duration.minutes(15)
LISTING_FAILURE_THRESHOLD = 5
RATELIMIT_UNAVAILABLE_THRESHOLD = 5
WEB_SECRET_READ_THRESHOLD = 1

#: The MCP tools that write (scope ``wiki.write``), in registry order. The
#: write-burst rule counts their ``tool_call`` lines per subject; a unit test keeps
#: this equal to the tools the MCP server registers with that scope. A Contributor
#: Insights ``In`` filter takes at most 10 values.
WRITE_TOOLS: tuple[str, ...] = (
    "create_article",
    "update_article",
    "edit_article",
    "move_article",
    "archive_article",
    "unarchive_article",
)
#: Write calls by one subject in ``ALARM_PERIOD``, any outcome, that raise the
#: write-burst alarm. At that pace one agent would use up the hourly write limit
#: (200, ``app/auth/ratelimit.py``) in under an hour.
WRITE_BURST_THRESHOLD = 30

#: When the daily grant-table snapshot starts (UTC). Chosen for a quiet hour in
#: US-Pacific; the table is tiny so the window barely matters.
BACKUP_HOUR_UTC = "09"

_RETENTION_BY_DAYS: dict[int, logs.RetentionDays] = {
    30: logs.RetentionDays.ONE_MONTH,
    60: logs.RetentionDays.TWO_MONTHS,
    90: logs.RetentionDays.THREE_MONTHS,
    120: logs.RetentionDays.FOUR_MONTHS,
    150: logs.RetentionDays.FIVE_MONTHS,
    180: logs.RetentionDays.SIX_MONTHS,
    365: logs.RetentionDays.ONE_YEAR,
}


def _retention(days: int) -> logs.RetentionDays:
    """Map a day count to the CloudWatch Logs enum; only the values Logs offers."""
    try:
        return _RETENTION_BY_DAYS[days]
    except KeyError as e:
        raise ValueError(
            f"log_retention_days={days} is not a CloudWatch Logs retention value; "
            f"expected one of {sorted(_RETENTION_BY_DAYS)}"
        ) from e


@jsii.implements(events.IRuleTarget)
class _AlertsTarget:
    """An SNS rule target that grants nothing.

    ``aws_events_targets.SnsTopic.bind`` calls ``topic.grant_publish`` for the bare
    ``events.amazonaws.com`` principal — no ``aws:SourceArn``, so every EventBridge
    rule anywhere could publish here. This target only wires the ARN; the stack writes
    the one conditioned statement itself once every rule exists
    (``OpsStack._restrict_alerts_publishers``).
    """

    def __init__(self, topic: sns.ITopic) -> None:
        self._topic = topic

    def bind(self, rule: events.IRule, id: str | None = None) -> events.RuleTargetConfig:
        del rule, id
        return events.RuleTargetConfig(arn=self._topic.topic_arn, target_resource=self._topic)


class OpsStack(cdk.Stack):
    """Trail, alerts, alarms, bucket-policy watch, and the grant-table backup plan."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        cfg: EnvConfig,
        storage: StorageStack,
        compute: ComputeStack,
        api: ApiStack,
        **kwargs,
    ) -> None:
        super().__init__(scope, id, **kwargs)
        self.cfg = cfg
        self.storage = storage
        self.compute = compute
        self.api = api
        self._removal = RemovalPolicy.RETAIN if cfg.retain_data else RemovalPolicy.DESTROY
        self._retention = _retention(cfg.log_retention_days)

        self.alerts = self._create_alerts_topic()
        self.trail_bucket = self._create_trail_bucket()
        self.trail_log_group = self._create_trail_log_group()
        self._grant_trail_key_use()
        self.trail = self._create_trail()
        self.bucket_guard = self._create_bucket_guard_rule()
        self.alarms = self._create_alarms()
        self.break_glass_role: iam.Role | None = None
        self.break_glass_watch: events.Rule | None = None
        if cfg.break_glass_principal_arn:
            self.break_glass_role = self._create_break_glass_role(cfg.break_glass_principal_arn)
            self.break_glass_watch = self._create_break_glass_rule(self.break_glass_role)
        else:
            cdk.Annotations.of(self).add_info(
                "No `breakGlassPrincipalArn` context, so no break-glass role: no principal "
                "these stacks define can hard-delete an article except the article bucket's "
                "auto-delete custom resource, which deletes every unlocked version when "
                "`make destroy` runs with retain_data off. See docs/RUNBOOK.md §3."
            )
        self._restrict_alerts_publishers(
            [r for r in (self.bucket_guard, self.break_glass_watch) if r is not None]
        )
        self.backup_vault = self._create_backup_vault()
        self.backup_plan = self._create_backup_plan()

        cdk.CfnOutput(self, "AlertsTopicArn", value=self.alerts.topic_arn)
        cdk.CfnOutput(self, "TrailArn", value=self.trail.trail_arn)
        cdk.CfnOutput(self, "TrailLogBucketName", value=self.trail_bucket.bucket_name)
        cdk.CfnOutput(self, "BackupVaultName", value=self.backup_vault.backup_vault_name)
        if self.break_glass_role is not None:
            cdk.CfnOutput(self, "BreakGlassRoleArn", value=self.break_glass_role.role_arn)

    # ---------------------------------------------------------------- alerts

    def _create_alerts_topic(self) -> sns.Topic:
        topic = sns.Topic(
            self,
            "Alerts",
            topic_name=f"wiki-{self.cfg.name}-alerts",
            display_name=f"wiki-{self.cfg.name} alerts",
            enforce_ssl=True,
        )
        alert_email = self.node.try_get_context("alertEmail")
        if alert_email:
            topic.add_subscription(subscriptions.EmailSubscription(alert_email))
        else:
            cdk.Annotations.of(self).add_warning_v2(
                "wiki:no-alert-email",
                "No `alertEmail` context; the alerts topic has no subscriber. "
                "Pass -c alertEmail=you@example.com or subscribe after deploy.",
            )
        return topic

    def _restrict_alerts_publishers(self, rules: list[events.Rule]) -> None:
        """The topic's one service statement: EventBridge may publish, for these rules.

        ``aws:SourceArn`` is the rule ARN EventBridge presents when it delivers; a rule
        in another account (or another stack here) is not on the list and is refused.
        ``aws:SourceAccount`` is belt and braces for the same thing.
        """
        self.alerts.add_to_resource_policy(
            iam.PolicyStatement(
                sid="EventBridgeRulesInThisStack",
                principals=[iam.ServicePrincipal("events.amazonaws.com")],
                actions=["sns:Publish"],
                resources=[self.alerts.topic_arn],
                conditions={
                    "ArnEquals": {"aws:SourceArn": [rule.rule_arn for rule in rules]},
                    "StringEquals": {"aws:SourceAccount": self.account},
                },
            )
        )

    # ----------------------------------------------------------------- trail

    def _create_trail_bucket(self) -> s3.Bucket:
        return s3.Bucket(
            self,
            "TrailBucket",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=self.storage.key,
            bucket_key_enabled=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="expire-trail-logs",
                    enabled=True,
                    expiration=Duration.days(self.cfg.log_retention_days),
                )
            ],
            removal_policy=self._removal,
            auto_delete_objects=not self.cfg.retain_data,
        )

    def _create_trail_log_group(self) -> logs.LogGroup:
        return logs.LogGroup(
            self,
            "TrailLogs",
            retention=self._retention,
            removal_policy=self._removal,
        )

    @property
    def _trail_name(self) -> str:
        return f"wiki-{self.cfg.name}"

    def _grant_trail_key_use(self) -> None:
        """Key-policy statements CloudTrail needs to write SSE-KMS log files.

        Documented shape (CloudTrail user guide, "Configure AWS KMS key policies"):
        ``GenerateDataKey*`` for the service principal, pinned to this trail by
        ``aws:SourceArn`` and to CloudTrail's encryption context; ``DescribeKey`` so the
        trail can be created, pinned to the trail the same way. Readers decrypt through
        the key's IAM delegation.
        """
        key = self.storage.key
        trail_arn = cdk.Stack.of(key).format_arn(
            service="cloudtrail", resource="trail", resource_name=self._trail_name
        )
        any_trail_here = cdk.Stack.of(key).format_arn(
            service="cloudtrail", region="*", resource="trail", resource_name="*"
        )
        cloudtrail_principal = iam.ServicePrincipal("cloudtrail.amazonaws.com")
        key.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowCloudTrailToEncryptLogs",
                principals=[cloudtrail_principal],
                actions=["kms:GenerateDataKey*"],
                resources=["*"],
                conditions={
                    "StringEquals": {"aws:SourceArn": trail_arn},
                    "StringLike": {"kms:EncryptionContext:aws:cloudtrail:arn": any_trail_here},
                },
            )
        )
        key.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowCloudTrailToDescribeKey",
                principals=[cloudtrail_principal],
                actions=["kms:DescribeKey"],
                resources=["*"],
                conditions={"StringEquals": {"aws:SourceArn": trail_arn}},
            )
        )

    def _create_trail(self) -> cloudtrail.Trail:
        trail = cloudtrail.Trail(
            self,
            "Trail",
            trail_name=self._trail_name,
            bucket=self.trail_bucket,
            encryption_key=self.storage.key,
            enable_file_validation=True,
            is_multi_region_trail=True,
            include_global_service_events=True,
            management_events=cloudtrail.ReadWriteType.ALL,
            send_to_cloud_watch_logs=True,
            cloud_watch_log_group=self.trail_log_group,
        )
        self._admit_only_this_trail()
        # §12.7: write operations only. Reads at S3 level cost more than they reveal; the
        # application logs record every read and denial instead: ``tool_call`` from the MCP
        # function, ``web_read`` from the console's read view, and the API access log.
        trail.add_s3_event_selector(
            [cloudtrail.S3EventSelector(bucket=self.storage.bucket)],
            read_write_type=cloudtrail.ReadWriteType.WRITE_ONLY,
            include_management_events=False,
        )
        return trail

    def _admit_only_this_trail(self) -> None:
        """Pin CloudTrail's two trail-bucket statements to this trail (``aws:SourceArn``).

        ``cloudtrail.Trail`` writes ``s3:GetBucketAcl`` and ``s3:PutObject`` for the bare
        service principal, which any trail in any account could use. CloudTrail's
        documented bucket policy conditions both on the trail's ARN, as the key policy
        does (``_grant_trail_key_use``). The ARN is built, not read from the trail: the
        trail depends on this policy, so a reference would be a cycle.
        """
        policy = self.trail_bucket.policy
        assert policy is not None
        cfn = policy.node.default_child
        assert isinstance(cfn, s3.CfnBucketPolicy)
        trail_arn = self.format_arn(
            service="cloudtrail", resource="trail", resource_name=self._trail_name
        )
        pinned = 0
        for i, statement in enumerate(self.resolve(policy.document)["Statement"]):
            if statement.get("Principal") == {"Service": "cloudtrail.amazonaws.com"}:
                cfn.add_property_override(
                    f"PolicyDocument.Statement.{i}.Condition.StringEquals.aws:SourceArn",
                    trail_arn,
                )
                pinned += 1
        if pinned != 2:
            raise ValueError(f"expected CloudTrail's two trail-bucket statements, found {pinned}")

    # ---------------------------------------------------------- bucket guard

    def _create_bucket_guard_rule(self) -> events.Rule:
        rule = events.Rule(
            self,
            "BucketGuard",
            rule_name=f"wiki-{self.cfg.name}-bucket-guard",
            description=(
                "S3 bucket policy / public-access / retention changes (docs/ARCHITECTURE.md §8.8)"
            ),
            event_pattern=events.EventPattern(
                source=["aws.s3"],
                detail_type=["AWS API Call via CloudTrail"],
                detail={
                    "eventSource": ["s3.amazonaws.com"],
                    "eventName": list(BUCKET_GUARD_EVENTS),
                },
            ),
        )
        rule.add_target(_AlertsTarget(self.alerts))
        return rule

    # ---------------------------------------------------------------- alarms

    def _alarm(
        self,
        id: str,
        *,
        metric: cloudwatch.IMetric,
        threshold: int,
        description: str,
        period_note: str,
    ) -> cloudwatch.Alarm:
        alarm = cloudwatch.Alarm(
            self,
            id,
            alarm_name=f"wiki-{self.cfg.name}-{id.lower()}",
            alarm_description=f"{description} ({period_note}). See docs/RUNBOOK.md.",
            metric=metric,
            threshold=threshold,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        alarm.add_alarm_action(cw_actions.SnsAction(self.alerts))
        return alarm

    def _create_write_burst_rule(self) -> cloudwatch.CfnInsightRule:
        """Contributor Insights over the MCP log: write ``tool_call`` lines per subject.

        Its report names the busiest subjects for any window (``docs/RUNBOOK.md`` §6);
        the write-burst alarm reads its ``MaxContributorValue``. Every outcome counts,
        refusals and rate-limited calls included: an agent steered by text it read
        tries before it succeeds, and the rate limiter fails open.
        """
        body = {
            "Schema": {"Name": "CloudWatchLogRule", "Version": 1},
            "LogGroupNames": [self.compute.mcp_fn.log_group.log_group_name],
            "LogFormat": "JSON",
            "Contribution": {
                "Keys": ["$.subject"],
                "Filters": [
                    {"Match": "$.message", "In": ["tool_call"]},
                    {"Match": "$.tool", "In": list(WRITE_TOOLS)},
                ],
            },
            "AggregateOn": "Count",
        }
        return cloudwatch.CfnInsightRule(
            self,
            "WriteBurstRule",
            rule_name=f"wiki-{self.cfg.name}-mcp-write-bursts",
            rule_state="ENABLED",
            rule_body=self.to_json_string(body),
        )

    def _create_alarms(self) -> dict[str, cloudwatch.Alarm]:
        api = self.api.api
        authorizer = self.compute.authorizer_fn
        mcp = self.compute.mcp_fn

        web_secret_reads = logs.MetricFilter(
            self,
            "WebSecretReadFilter",
            log_group=self.trail_log_group,
            filter_pattern=logs.FilterPattern.literal(self._web_secret_read_pattern()),
            metric_namespace=f"wiki/{self.cfg.name}",
            metric_name="WebSecretReadsByOthers",
            metric_value="1",
            default_value=0,
        )

        listing_failures = logs.MetricFilter(
            self,
            "ListingFailureFilter",
            log_group=mcp.log_group,
            filter_pattern=logs.FilterPattern.any_term(*LISTING_FAILURE_TERMS),
            metric_namespace=f"wiki/{self.cfg.name}",
            metric_name="ListingRebuildFailures",
            metric_value="1",
            default_value=0,
        )

        ratelimit_unavailable = logs.MetricFilter(
            self,
            "RateLimitUnavailableFilter",
            log_group=mcp.log_group,
            filter_pattern=logs.FilterPattern.string_value(
                "$.message", "=", RATELIMIT_UNAVAILABLE_EVENT
            ),
            metric_namespace=f"wiki/{self.cfg.name}",
            metric_name="RateLimitUnavailable",
            metric_value="1",
            default_value=0,
        )

        write_bursts = self._create_write_burst_rule()
        write_burst = self._alarm(
            "WriteBurst",
            metric=cloudwatch.MathExpression(
                expression=(
                    f"INSIGHT_RULE_METRIC('{write_bursts.rule_name}', 'MaxContributorValue')"
                ),
                using_metrics={},
                period=ALARM_PERIOD,
                label="MCP write calls by the busiest subject",
            ),
            threshold=WRITE_BURST_THRESHOLD,
            description="One subject's agent made a burst of MCP write calls",
            period_note=f"busiest subject >= {WRITE_BURST_THRESHOLD} over 5 min",
        )
        write_burst.node.add_dependency(write_bursts)

        return {
            "api_5xx": self._alarm(
                "Api5xx",
                metric=api.metric_server_error(period=ALARM_PERIOD, statistic="Sum"),
                threshold=API_5XX_THRESHOLD,
                description="API Gateway returned a 5xx",
                period_note=f"sum >= {API_5XX_THRESHOLD} over 5 min",
            ),
            "auth_failures": self._alarm(
                "AuthFailures",
                metric=authorizer.metric_errors(period=ALARM_PERIOD, statistic="Sum"),
                threshold=AUTHORIZER_ERROR_THRESHOLD,
                description="Authentication-failure spike: authorizer rejections",
                period_note=f"sum >= {AUTHORIZER_ERROR_THRESHOLD} over 5 min",
            ),
            "mcp_errors": self._alarm(
                "McpErrors",
                metric=mcp.metric_errors(period=ALARM_PERIOD, statistic="Sum"),
                threshold=MCP_ERROR_THRESHOLD,
                description="MCP function invocation errors",
                period_note=f"sum >= {MCP_ERROR_THRESHOLD} over 5 min",
            ),
            "mcp_throttles": self._alarm(
                "McpThrottles",
                metric=mcp.metric_throttles(period=ALARM_PERIOD, statistic="Sum"),
                threshold=MCP_THROTTLE_THRESHOLD,
                description="MCP function throttled by Lambda concurrency",
                period_note=f"sum >= {MCP_THROTTLE_THRESHOLD} over 5 min",
            ),
            "listing_failures": self._alarm(
                "ListingFailures",
                metric=listing_failures.metric(period=LISTING_FAILURE_PERIOD, statistic="Sum"),
                threshold=LISTING_FAILURE_THRESHOLD,
                description="Listing projection could not be refreshed or persisted",
                period_note=f"sum >= {LISTING_FAILURE_THRESHOLD} over 15 min",
            ),
            "ratelimit_unavailable": self._alarm(
                "RateLimitUnavailable",
                metric=ratelimit_unavailable.metric(period=ALARM_PERIOD, statistic="Sum"),
                threshold=RATELIMIT_UNAVAILABLE_THRESHOLD,
                description="Per-subject rate limits could not be counted; calls were allowed",
                period_note=f"sum >= {RATELIMIT_UNAVAILABLE_THRESHOLD} over 5 min",
            ),
            "web_secret_reads": self._alarm(
                "WebSecretRead",
                metric=web_secret_reads.metric(period=ALARM_PERIOD, statistic="Sum"),
                threshold=WEB_SECRET_READ_THRESHOLD,
                description="Something other than the web app read the web secret",
                period_note=f"sum >= {WEB_SECRET_READ_THRESHOLD} over 5 min",
            ),
            "write_burst": write_burst,
        }

    def _web_secret_read_pattern(self) -> str:
        """``GetSecretValue`` on the web secret by any principal but its two readers.

        The trail log group carries every management event, read ones included.
        ``secretId`` is whatever the caller passed (name, partial or full ARN); each
        contains the secret's name. Readers are matched by role name, so a person, an
        IAM user, root, or any other role all count.
        """
        compute = self.compute
        rotation_role = compute.rotation_fn.role
        assert rotation_role is not None
        readers = (compute.web_role.role_name, rotation_role.role_name)
        not_a_reader = " && ".join(
            f'($.userIdentity.sessionContext.sessionIssuer.userName != "{name}")'
            for name in readers
        )
        return (
            '{ ($.eventSource = "secretsmanager.amazonaws.com")'
            ' && ($.eventName = "GetSecretValue")'
            f' && ($.requestParameters.secretId = "*{compute.web_secret.secret_name}*")'
            f' && (($.userIdentity.type != "AssumedRole") || ({not_a_reader})) }}'
        )

    # ----------------------------------------------------------- break glass

    @property
    def _break_glass_role_name(self) -> str:
        return f"wiki-{self.cfg.name}-break-glass"

    def _create_break_glass_role(self, principal_arn: str) -> iam.Role:
        """Hard delete and Object Lock bypass, for a person in the console (§8.8).

        Trusts this account only for ``principal_arn``, through an ``aws:PrincipalArn``
        ``ArnLike`` condition (patterns allowed, and it survives an IAM Identity Center
        role being re-created), so no other principal in the account can assume it,
        whatever its own policy allows. Nothing that runs code is ever named. There is
        no ``aws:MultiFactorAuthPresent`` condition: Identity Center and other federated
        role sessions do not carry that key, so MFA belongs to the sign-in
        (``docs/DEPLOY.md`` §9). Scoped to article keys and the one key. The procedure
        is ``docs/RUNBOOK.md`` "Hard delete".
        """
        bucket = self.storage.bucket
        policy = iam.PolicyDocument(
            statements=[
                iam.PolicyStatement(
                    sid="HardDeleteArticleVersions",
                    actions=[
                        "s3:DeleteObjectVersion",
                        "s3:BypassGovernanceRetention",
                        "s3:GetObject",
                        "s3:GetObjectVersion",
                        "s3:GetObjectRetention",
                        "s3:PutObjectRetention",
                    ],
                    resources=[bucket.arn_for_objects("a/*")],
                ),
                iam.PolicyStatement(
                    sid="ListVersionsWithinArticles",
                    actions=["s3:ListBucket", "s3:ListBucketVersions"],
                    resources=[bucket.bucket_arn],
                    conditions={"StringLike": {"s3:prefix": ["a/*"]}},
                ),
                iam.PolicyStatement(
                    sid="KeyUse",
                    actions=["kms:Decrypt", "kms:DescribeKey"],
                    resources=[self.storage.key.key_arn],
                ),
            ]
        )
        return iam.Role(
            self,
            "BreakGlass",
            role_name=self._break_glass_role_name,
            assumed_by=iam.AccountRootPrincipal().with_conditions(
                {"ArnLike": {"aws:PrincipalArn": principal_arn}}
            ),
            max_session_duration=Duration.hours(1),
            description="wiki break-glass: hard delete + Object Lock bypass, humans only (§8.8)",
            inline_policies={"HardDelete": policy},
        )

    def _create_break_glass_rule(self, role: iam.Role) -> events.Rule:
        rule = events.Rule(
            self,
            "BreakGlassUsed",
            rule_name=f"wiki-{self.cfg.name}-break-glass-used",
            description="Someone assumed the break-glass role (docs/ARCHITECTURE.md §8.8)",
            event_pattern=events.EventPattern(
                source=["aws.sts"],
                detail_type=["AWS API Call via CloudTrail"],
                detail={
                    "eventSource": ["sts.amazonaws.com"],
                    "eventName": ["AssumeRole"],
                    "requestParameters": {"roleArn": [role.role_arn]},
                },
            ),
        )
        rule.add_target(_AlertsTarget(self.alerts))
        return rule

    # ---------------------------------------------------------------- backup

    def _create_backup_vault(self) -> backup.BackupVault:
        return backup.BackupVault(
            self,
            "Vault",
            backup_vault_name=f"wiki-{self.cfg.name}-grants",
            encryption_key=self.storage.key,
            removal_policy=self._removal,
        )

    def _create_backup_plan(self) -> backup.BackupPlan:
        plan = backup.BackupPlan(
            self,
            "Plan",
            backup_plan_name=f"wiki-{self.cfg.name}-grants-daily",
            backup_vault=self.backup_vault,
            backup_plan_rules=[
                backup.BackupPlanRule(
                    rule_name="daily",
                    backup_vault=self.backup_vault,
                    schedule_expression=events.Schedule.cron(hour=BACKUP_HOUR_UTC, minute="0"),
                    start_window=Duration.hours(1),
                    completion_window=Duration.hours(3),
                    delete_after=Duration.days(self.cfg.backup_retention_days),
                )
            ],
        )
        plan.add_selection(
            "Grants",
            backup_selection_name="grant-table",
            resources=[backup.BackupResource.from_dynamo_db_table(self.storage.table)],
            allow_restores=True,
        )
        return plan
