"""Alarms and alert emails (meal_prep_app/monitoring.py, backend/monitor_src)."""
import io
import json
import os
from datetime import datetime, timezone

import aws_cdk as core
import aws_cdk.assertions as assertions
import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

from backend.monitor_src import alerts, health  # noqa: E402
from meal_prep_app.meal_prep_app_stack import MealPrepAppStack  # noqa: E402


# ---------- the stack ----------

@pytest.fixture(scope="module")
def template():
    app = core.App()
    stack = MealPrepAppStack(app, "meal-prep-app", domain_name="example.com", alert_email="alerts@example.org")
    return assertions.Template.from_stack(stack)


def test_every_service_has_its_alarms(template):
    names = {a["Properties"]["AlarmName"] for a in template.find_resources("AWS::CloudWatch::Alarm").values()}
    assert names == {f"GTXMeals-{n}" for n in (
        "ApiErrors", "ApiThrottles", "ApiNearTimeout", "ApiLogErrors", "Api5xx",
        "DatabaseErrors",
        "EmailBounceRate", "EmailDailyLimit",
        "SiteDown", "ApiDown",
    )}  # 10 - CloudWatch's free allowance


def test_api_log_errors_are_counted(template):
    template.has_resource_properties("AWS::Logs::MetricFilter", {
        "FilterPattern": '?ERROR ?"Task timed out" ?"Runtime exited"',
        "MetricTransformations": [assertions.Match.object_like({
            "MetricNamespace": "GTXMeals", "MetricName": "ApiLogErrors",
        })],
    })


def test_alarm_changes_trigger_the_alert_email(template):
    template.has_resource_properties("AWS::Events::Rule", {
        "EventPattern": {
            "source": ["aws.cloudwatch"],
            "detail-type": ["CloudWatch Alarm State Change"],
            "detail": {"alarmName": [{"prefix": "GTXMeals-"}], "state": {"value": ["ALARM", "OK"]}},
        },
    })
    template.has_resource_properties("AWS::Lambda::Function", {
        "Handler": "alerts.handler",
        "Environment": {"Variables": assertions.Match.object_like({
            "ALERT_EMAIL": "alerts@example.org",
            "FROM_EMAIL": "GTX Meals Alerts <alerts@example.com>",
        })},
    })


def test_health_check_runs_every_fifteen_minutes(template):
    template.has_resource_properties("AWS::Events::Rule", {"ScheduleExpression": "rate(15 minutes)"})
    template.has_resource_properties("AWS::CloudWatch::Alarm", {
        "AlarmName": "GTXMeals-SiteDown", "Period": 900, "EvaluationPeriods": 2,
    })
    template.has_resource_properties("AWS::Lambda::Function", {"Handler": "health.handler"})


def test_dashboard_created(template):
    template.has_resource_properties("AWS::CloudWatch::Dashboard", {"DashboardName": "GTXMeals"})


# ---------- alert emails ----------

class FakeSes:
    def __init__(self):
        self.sent = []

    def send_email(self, **kwargs):
        self.sent.append(kwargs)


class FakeLogs:
    def __init__(self, events=(), error=None):
        self.events, self.error, self.calls = list(events), error, []

    def filter_log_events(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return {"events": self.events}


@pytest.fixture
def sent(monkeypatch):
    ses = FakeSes()
    monkeypatch.setattr(alerts, "ses", ses)
    monkeypatch.setattr(alerts, "logs", FakeLogs())
    monkeypatch.setattr(alerts, "ALERT_EMAIL", "alerts@example.org")
    monkeypatch.setattr(alerts, "FROM_EMAIL", "GTX Meals Alerts <alerts@example.com>")
    monkeypatch.setattr(alerts, "API_LOG_GROUP", "/aws/lambda/api")
    monkeypatch.setattr(alerts, "ERROR_PATTERN", "?ERROR")
    monkeypatch.setattr(alerts, "DASHBOARD_NAME", "GTXMeals")
    return ses.sent


def _event(name="GTXMeals-ApiErrors", state="ALARM", previous="OK"):
    return {
        "time": "2026-10-21T17:05:00Z",
        "detail": {
            "alarmName": name,
            "state": {"value": state, "reason": "Threshold Crossed: 1 datapoint [2.0] >= 1.0",
                      "timestamp": "2026-10-21T17:05:00.000+0000"},
            "previousState": {"value": previous},
            "configuration": {"description": "The API crashed\nThe API Lambda failed on a request."},
        },
    }


def test_alarm_sends_an_email(sent):
    alerts.handler(_event(name="GTXMeals-DatabaseErrors"), None)

    assert len(sent) == 1
    email = sent[0]
    assert email["Destination"] == {"ToAddresses": ["alerts@example.org"]}
    assert email["Source"] == "GTX Meals Alerts <alerts@example.com>"
    assert email["Message"]["Subject"]["Data"] == "[GTX Meals] ALARM: The API crashed"
    body = email["Message"]["Body"]["Text"]["Data"]
    assert "went off at Wed Oct 21, 12:05 PM CDT" in body
    assert "The API Lambda failed on a request." in body
    assert "Why: Threshold Crossed" in body
    assert "#alarmsV2:alarm/GTXMeals-DatabaseErrors" in body
    assert "#dashboards/dashboard/GTXMeals" in body
    assert "API logs" not in body  # not an API-error alarm


def test_api_error_alarm_includes_the_latest_log_errors(sent, monkeypatch):
    fake_logs = FakeLogs(events=[
        {"timestamp": 1792602000000 + i, "message": f"[ERROR] KeyError: 'x{i}'\n"} for i in range(12)
    ])
    monkeypatch.setattr(alerts, "logs", fake_logs)

    alerts.handler(_event(), None)

    body = sent[0]["Message"]["Body"]["Text"]["Data"]
    assert "Latest errors in the API logs (/aws/lambda/api, last 15 minutes):" in body
    assert "KeyError: 'x11'" in body and "KeyError: 'x2'" in body
    assert "KeyError: 'x1'\n" not in body  # only the last 10
    assert fake_logs.calls[0]["filterPattern"] == "?ERROR"
    assert fake_logs.calls[0]["logGroupName"] == "/aws/lambda/api"


def test_alert_still_sends_if_the_logs_cant_be_read(sent, monkeypatch):
    monkeypatch.setattr(alerts, "logs", FakeLogs(error=RuntimeError("AccessDenied")))

    alerts.handler(_event(), None)

    assert "Couldn't read the API logs: AccessDenied" in sent[0]["Message"]["Body"]["Text"]["Data"]


def test_cleared_alarm_sends_a_resolved_email(sent):
    alerts.handler(_event(state="OK", previous="ALARM"), None)

    assert sent[0]["Message"]["Subject"]["Data"] == "[GTX Meals] Resolved: The API crashed"
    assert "back to normal at" in sent[0]["Message"]["Body"]["Text"]["Data"]


@pytest.mark.parametrize("state,previous", [
    ("OK", "INSUFFICIENT_DATA"),  # a new alarm settling in - nothing was wrong
    ("INSUFFICIENT_DATA", "OK"),
])
def test_other_state_changes_send_nothing(sent, state, previous):
    assert alerts.handler(_event(state=state, previous=previous), None) == {"sent": False}
    assert sent == []


def test_central_time_follows_daylight_saving():
    assert alerts._central_text(datetime(2026, 1, 15, 18, 30, tzinfo=timezone.utc)) == "Thu Jan 15, 12:30 PM CST"
    assert alerts._central_text(datetime(2026, 7, 4, 3, 0, tzinfo=timezone.utc)) == "Fri Jul 3, 10:00 PM CDT"
    # 2026's switches: Mar 8 and Nov 1 at 2:00 AM local.
    assert alerts._central_text(datetime(2026, 3, 8, 7, 59, tzinfo=timezone.utc)).endswith("CST")
    assert alerts._central_text(datetime(2026, 3, 8, 8, 0, tzinfo=timezone.utc)).endswith("CDT")
    assert alerts._central_text(datetime(2026, 11, 1, 6, 59, tzinfo=timezone.utc)).endswith("CDT")
    assert alerts._central_text(datetime(2026, 11, 1, 7, 0, tzinfo=timezone.utc)).endswith("CST")


# ---------- health check ----------

class FakeCloudWatch:
    def __init__(self):
        self.data = []

    def put_metric_data(self, **kwargs):
        self.data.append(kwargs)


class FakeResponse(io.BytesIO):
    def __init__(self, body, status=200):
        super().__init__(body)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _run_health(monkeypatch, responses):
    cw = FakeCloudWatch()
    monkeypatch.setattr(health, "cloudwatch", cw)
    monkeypatch.setattr(health, "SITE_URL", "https://site.example")
    monkeypatch.setattr(health, "API_URL", "https://api.example")

    def fake_urlopen(req, timeout):
        result = responses[req.full_url]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(health.urllib.request, "urlopen", fake_urlopen)
    health.handler({}, None)
    return {d["Dimensions"][0]["Value"]: d["Value"] for d in cw.data[0]["MetricData"]}


def test_health_check_reports_both_up(monkeypatch):
    assert _run_health(monkeypatch, {
        "https://site.example/": FakeResponse(b"<!doctype html><html><title>GTX</title></html>"),
        "https://api.example/settings": FakeResponse(json.dumps({"nextReadyDate": "2026-10-26"}).encode()),
    }) == {"Site": 1, "Api": 1}


def test_health_check_reports_failures(monkeypatch):
    assert _run_health(monkeypatch, {
        "https://site.example/": OSError("connection refused"),
        "https://api.example/settings": FakeResponse(b'{"message": "Internal Server Error"}'),
    }) == {"Site": 0, "Api": 0}
