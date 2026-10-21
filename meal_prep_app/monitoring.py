"""CloudWatch alarms, a dashboard and email alerts for every AWS service the
app uses.

Alerts go out by email through SES, from a small Lambda that an EventBridge
rule triggers on each alarm's state change - the usual route, an SNS topic,
isn't available because this project's SCP blocks sns:Publish (see README).

Not alarmed directly:
- CloudFront (and the S3 bucket behind it): its metrics only exist in
  us-east-1, where this project can't deploy. A health check that loads the
  site through CloudFront every few minutes covers it instead.
- Cognito: it publishes no error metrics without its paid advanced security
  features. Sign-in failures that reach the API show up in the API's logs.
- DynamoDB throttling, SES spam complaints and the monitoring Lambdas' own
  errors: left off to keep within CloudWatch's 10 free alarms. Throttling
  is unlikely at this traffic; complaints and throttles are on the dashboard.
"""
from aws_cdk import (
    Duration,
    RemovalPolicy,
    aws_budgets as budgets,
    aws_cloudwatch as cloudwatch,
    aws_dynamodb as dynamodb,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
)
from constructs import Construct

ALARM_PREFIX = "GTXMeals-"
DASHBOARD_NAME = "GTXMeals"
METRIC_NAMESPACE = "GTXMeals"
# Lines in the API's logs that count as errors: Python's own "[ERROR]" for an
# unhandled exception, the API's "ERROR ..." lines for errors it catches (a
# failed email to one customer, say), and Lambda's timeout and crash messages.
ERROR_PATTERN = '?ERROR ?"Task timed out" ?"Runtime exited"'
# SES's sandbox allows 200 emails a day. Warn at 80% so a big menu send
# doesn't get cut off part way. Raise this after SES production access.
SES_DAILY_SEND_WARNING = 160
# Monthly AWS spend that triggers the budget emails.
MONTHLY_BUDGET_USD = 20
# The API Lambda's timeout is 10 seconds.
API_SLOW_MS = 8000

FIVE_MINUTES = Duration.minutes(5)
HEALTH_CHECK_EVERY = Duration.minutes(15)


class Monitoring(Construct):

    def __init__(
        self, scope: Construct, construct_id: str, *,
        api_fn: lambda_.Function,
        http_api,
        table: dynamodb.Table,
        alert_email: str,
        from_email: str,
        site_url: str,
        api_url: str,
    ) -> None:
        super().__init__(scope, construct_id)

        # The API's log group already exists (Lambda created it on first run),
        # so it's referenced by name rather than created here.
        api_log_group = logs.LogGroup.from_log_group_name(
            self, "ApiLogGroup", f"/aws/lambda/{api_fn.function_name}"
        )
        log_errors_metric = logs.MetricFilter(
            self, "ApiLogErrorsFilter",
            log_group=api_log_group,
            filter_pattern=logs.FilterPattern.literal(ERROR_PATTERN),
            metric_namespace=METRIC_NAMESPACE,
            metric_name="ApiLogErrors",
            metric_value="1",
            default_value=0,
        ).metric(statistic="Sum", period=FIVE_MINUTES)

        code = lambda_.Code.from_asset("backend/monitor_src", exclude=["__pycache__", "*.pyc"])

        def monitor_function(id_, handler, environment, timeout):
            log_group = logs.LogGroup(
                self, f"{id_}Logs",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=RemovalPolicy.DESTROY,
            )
            return lambda_.Function(
                self, id_,
                runtime=lambda_.Runtime.PYTHON_3_12,
                handler=handler,
                code=code,
                timeout=timeout,
                memory_size=128,
                environment=environment,
                log_group=log_group,
            )

        # ---------- Health check: website (CloudFront + S3) and API ----------
        health_fn = monitor_function(
            "HealthCheckFunction", "health.handler",
            {"SITE_URL": site_url, "API_URL": api_url},
            Duration.seconds(30),
        )
        health_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["cloudwatch:PutMetricData"],
            resources=["*"],  # PutMetricData has no resource-level scoping...
            conditions={"StringEquals": {"cloudwatch:namespace": METRIC_NAMESPACE}},  # ...so limit the namespace
        ))
        events.Rule(
            self, "HealthCheckSchedule",
            schedule=events.Schedule.rate(HEALTH_CHECK_EVERY),
            targets=[targets.LambdaFunction(health_fn)],
        )

        def health_metric(check):
            return cloudwatch.Metric(
                namespace=METRIC_NAMESPACE, metric_name="HealthCheck",
                dimensions_map={"Check": check}, statistic="Minimum", period=HEALTH_CHECK_EVERY,
            )

        # ---------- Alert emails ----------
        alerts_fn = monitor_function(
            "AlertEmailFunction", "alerts.handler",
            {
                "ALERT_EMAIL": alert_email,
                "FROM_EMAIL": from_email,
                "API_LOG_GROUP": api_log_group.log_group_name,
                "ERROR_PATTERN": ERROR_PATTERN,
                "DASHBOARD_NAME": DASHBOARD_NAME,
            },
            Duration.seconds(30),
        )
        # SES has no per-resource scoping for sending (same as the API's grant).
        alerts_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["ses:SendEmail"], resources=["*"],
        ))
        api_log_group.grant(alerts_fn, "logs:FilterLogEvents")

        events.Rule(
            self, "AlarmStateChangeRule",
            description="Emails an alert when a GTXMeals alarm goes off or clears",
            event_pattern=events.EventPattern(
                source=["aws.cloudwatch"],
                detail_type=["CloudWatch Alarm State Change"],
                detail={
                    "alarmName": [{"prefix": ALARM_PREFIX}],
                    "state": {"value": ["ALARM", "OK"]},
                },
            ),
            targets=[targets.LambdaFunction(alerts_fn, retry_attempts=4)],
        )

        # ---------- Alarms ----------
        self.alarms = []

        def alarm(name, metric, threshold, title, explanation, *,
                  comparison=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                  periods=1, missing=cloudwatch.TreatMissingData.NOT_BREACHING):
            # The description's first line is the alert email's subject.
            a = cloudwatch.Alarm(
                self, name,
                alarm_name=f"{ALARM_PREFIX}{name}",
                alarm_description=f"{title}\n{explanation}",
                metric=metric,
                threshold=threshold,
                comparison_operator=comparison,
                evaluation_periods=periods,
                datapoints_to_alarm=periods,
                treat_missing_data=missing,
            )
            self.alarms.append(a)
            return a

        # Lambda (the API)
        alarm(
            "ApiErrors", api_fn.metric_errors(statistic="Sum", period=FIVE_MINUTES), 1,
            "The API crashed",
            "The API Lambda failed on at least one request, so a customer or admin likely saw an "
            "error. The log lines below show what went wrong.",
        )
        alarm(
            "ApiThrottles", api_fn.metric_throttles(statistic="Sum", period=FIVE_MINUTES), 1,
            "The API is being throttled",
            "Lambda turned away requests because too many ran at once. Customers may have seen "
            "errors. If it keeps happening, the project's Lambda concurrency limit needs raising.",
        )
        alarm(
            "ApiNearTimeout", api_fn.metric_duration(statistic="Maximum", period=FIVE_MINUTES), API_SLOW_MS,
            "The API is close to timing out",
            f"A request took over {API_SLOW_MS // 1000} seconds; the API gives up at 10. Slow "
            "AWS calls or a large email send are the usual causes.",
        )
        # Lambda logs
        alarm(
            "ApiLogErrors", log_errors_metric, 1,
            "Errors in the API logs",
            "The API logged an error - including ones it recovers from, such as an email or text "
            "that failed to send to one customer. The log lines below show what happened.",
        )
        # API Gateway
        alarm(
            "Api5xx", http_api.metric_server_error(statistic="Sum", period=FIVE_MINUTES), 1,
            "The API returned server errors",
            "API Gateway answered at least one request with a 5xx error, so someone saw the site "
            "fail to load or save.",
        )
        # DynamoDB
        db_ops = [
            dynamodb.Operation.GET_ITEM, dynamodb.Operation.PUT_ITEM, dynamodb.Operation.UPDATE_ITEM,
            dynamodb.Operation.DELETE_ITEM, dynamodb.Operation.QUERY, dynamodb.Operation.SCAN,
        ]
        alarm(
            "DatabaseErrors",
            table.metric_system_errors_for_operations(operations=db_ops, period=FIVE_MINUTES), 1,
            "The database had errors",
            "DynamoDB failed requests on its side. These are usually brief AWS issues; check "
            "the AWS Health Dashboard if it continues.",
        )
        # SES
        alarm(
            "EmailBounceRate",
            cloudwatch.Metric(namespace="AWS/SES", metric_name="Reputation.BounceRate",
                              statistic="Maximum", period=Duration.hours(1)), 0.05,
            "Too many emails are bouncing",
            "Over 5% of emails sent have bounced. AWS reviews accounts at 5% and can pause "
            "sending at 10%. Check the Customers page for mistyped email addresses.",
        )
        alarm(
            "EmailDailyLimit",
            cloudwatch.Metric(namespace="AWS/SES", metric_name="Send",
                              statistic="Sum", period=Duration.days(1)), SES_DAILY_SEND_WARNING,
            "Close to the daily email limit",
            f"Over {SES_DAILY_SEND_WARNING} emails were sent today. SES allows 200 a day while the "
            "project is in the SES sandbox; emails past the limit fail.",
        )
        # Website (CloudFront + S3) and API, end to end
        alarm(
            "SiteDown", health_metric("Site"), 1,
            "The website is down",
            "gtxmeals.com failed to load twice in a row (30 minutes). CloudFront, the S3 bucket "
            "or the domain's DNS may have a problem.",
            comparison=cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD, periods=2,
            missing=cloudwatch.TreatMissingData.MISSING,
        )
        alarm(
            "ApiDown", health_metric("Api"), 1,
            "The API is down",
            "The API failed to answer twice in a row (30 minutes), so the menu can't load and "
            "orders can't be placed.",
            comparison=cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD, periods=2,
            missing=cloudwatch.TreatMissingData.MISSING,
        )
        # ---------- Budget ----------
        # AWS Budgets emails alert_email directly - no SNS or SES involved.
        # Credits are left out so it reports real usage: with them counted,
        # the bill reads $0 until the project's credits run out.
        budgets.CfnBudget(
            self, "MonthlyBudget",
            budget=budgets.CfnBudget.BudgetDataProperty(
                budget_name="GTXMeals-Monthly",
                budget_type="COST",
                time_unit="MONTHLY",
                budget_limit=budgets.CfnBudget.SpendProperty(amount=MONTHLY_BUDGET_USD, unit="USD"),
                cost_types=budgets.CfnBudget.CostTypesProperty(include_credit=False),
            ),
            notifications_with_subscribers=[
                budgets.CfnBudget.NotificationWithSubscribersProperty(
                    notification=budgets.CfnBudget.NotificationProperty(
                        notification_type=kind,
                        comparison_operator="GREATER_THAN",
                        threshold=percent,
                        threshold_type="PERCENTAGE",
                    ),
                    subscribers=[budgets.CfnBudget.SubscriberProperty(
                        subscription_type="EMAIL", address=alert_email,
                    )],
                )
                # Spent 80%, spent 100%, and on course to pass 100% this month.
                for kind, percent in (("ACTUAL", 80), ("ACTUAL", 100), ("FORECASTED", 100))
            ],
        )

        # ---------- Dashboard ----------
        def graph(title, *metrics, **kwargs):
            return cloudwatch.GraphWidget(title=title, left=list(metrics), width=8, height=6, **kwargs)

        cloudwatch.Dashboard(
            self, "Dashboard",
            dashboard_name=DASHBOARD_NAME,
            default_interval=Duration.days(1),
            widgets=[
                [cloudwatch.AlarmStatusWidget(title="Alarms", alarms=self.alarms, width=24, height=4)],
                [
                    graph("Health checks (1 = up)", health_metric("Site"), health_metric("Api")),
                    graph("API requests and errors",
                          http_api.metric_count(statistic="Sum", period=FIVE_MINUTES),
                          http_api.metric_client_error(statistic="Sum", period=FIVE_MINUTES),
                          http_api.metric_server_error(statistic="Sum", period=FIVE_MINUTES)),
                    graph("API latency (ms)",
                          http_api.metric_latency(statistic="p50", period=FIVE_MINUTES),
                          http_api.metric_latency(statistic="Maximum", period=FIVE_MINUTES)),
                ],
                [
                    graph("API Lambda",
                          api_fn.metric_invocations(statistic="Sum", period=FIVE_MINUTES),
                          api_fn.metric_errors(statistic="Sum", period=FIVE_MINUTES),
                          api_fn.metric_throttles(statistic="Sum", period=FIVE_MINUTES),
                          log_errors_metric),
                    graph("API Lambda duration (ms)",
                          api_fn.metric_duration(statistic="p50", period=FIVE_MINUTES),
                          api_fn.metric_duration(statistic="Maximum", period=FIVE_MINUTES)),
                    graph("Database",
                          table.metric_consumed_read_capacity_units(statistic="Sum", period=FIVE_MINUTES),
                          table.metric_consumed_write_capacity_units(statistic="Sum", period=FIVE_MINUTES),
                          table.metric("ReadThrottleEvents", statistic="Sum", period=FIVE_MINUTES),
                          table.metric("WriteThrottleEvents", statistic="Sum", period=FIVE_MINUTES)),
                ],
                [
                    graph("Emails",
                          cloudwatch.Metric(namespace="AWS/SES", metric_name="Send", statistic="Sum",
                                            period=Duration.hours(1)),
                          cloudwatch.Metric(namespace="AWS/SES", metric_name="Delivery", statistic="Sum",
                                            period=Duration.hours(1)),
                          cloudwatch.Metric(namespace="AWS/SES", metric_name="Bounce", statistic="Sum",
                                            period=Duration.hours(1)),
                          cloudwatch.Metric(namespace="AWS/SES", metric_name="Complaint", statistic="Sum",
                                            period=Duration.hours(1))),
                    cloudwatch.LogQueryWidget(
                        title="Latest API log errors",
                        log_group_names=[api_log_group.log_group_name],
                        query_lines=[
                            "fields @timestamp, @message",
                            "filter @message like /ERROR|Task timed out|Runtime exited/",
                            "sort @timestamp desc",
                            "limit 20",
                        ],
                        width=16, height=6,
                    ),
                ],
            ],
        )
