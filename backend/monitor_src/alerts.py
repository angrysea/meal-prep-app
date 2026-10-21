"""Emails an alert when one of the app's CloudWatch alarms changes state.

Triggered by an EventBridge rule on "CloudWatch Alarm State Change" events
for the GTXMeals-* alarms (see monitoring.py). Alarms normally notify through
SNS, but this project's SCP blocks sns:Publish (see README), so this sends
through SES instead, from the site's verified domain.

One email when an alarm goes off, another when it clears. For alarms about
API errors, the email includes the latest matching lines from the API's logs.
"""
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import boto3

ALERT_EMAIL = os.environ.get("ALERT_EMAIL", "")
FROM_EMAIL = os.environ.get("FROM_EMAIL", "")
API_LOG_GROUP = os.environ.get("API_LOG_GROUP", "")
# Same pattern as the API's log-errors metric filter, so the email shows the
# lines that set the alarm off.
ERROR_PATTERN = os.environ.get("ERROR_PATTERN", "")
DASHBOARD_NAME = os.environ.get("DASHBOARD_NAME", "")
REGION = os.environ.get("AWS_REGION", "us-east-2")

LOG_LOOKBACK = timedelta(minutes=15)
MAX_LOG_LINES = 10
MAX_LOG_LINE_CHARS = 1500

ses = boto3.client("ses")
logs = boto3.client("logs")


def handler(event, context):
    detail = event["detail"]
    state = detail["state"]["value"]
    previous = (detail.get("previousState") or {}).get("value")
    # Only "went off" and "cleared" - not INSUFFICIENT_DATA flapping, or OK
    # right after the alarm is first created.
    if state not in ("ALARM", "OK") or (state == "OK" and previous != "ALARM"):
        return {"sent": False}
    if not (ALERT_EMAIL and FROM_EMAIL):
        raise RuntimeError("ALERT_EMAIL and FROM_EMAIL must both be set to send alerts")

    subject, body = build_email(event)
    ses.send_email(
        Source=FROM_EMAIL,
        Destination={"ToAddresses": [ALERT_EMAIL]},
        Message={"Subject": {"Data": subject}, "Body": {"Text": {"Data": body}}},
    )
    return {"sent": True, "subject": subject}


def build_email(event):
    detail = event["detail"]
    name = detail["alarmName"]
    state = detail["state"]["value"]
    # Descriptions are "Title\nWhat it means and what to do" (see monitoring.py).
    description = (detail.get("configuration") or {}).get("description") or name
    title, _, explanation = description.partition("\n")
    when = _parse_time(detail["state"].get("timestamp") or event.get("time"))

    if state == "ALARM":
        subject = f"[GTX Meals] ALARM: {title}"
        lines = [
            f"{title} - this alarm went off at {_central_text(when)}.",
            "",
            explanation.strip(),
            "",
            f"Why: {detail['state'].get('reason', '')}",
        ]
        if _shows_log_errors(detail):
            lines += ["", *_recent_error_lines(when)]
    else:
        subject = f"[GTX Meals] Resolved: {title}"
        lines = [
            f"{title} - back to normal at {_central_text(when)}.",
            "",
            f"Why: {detail['state'].get('reason', '')}",
        ]

    console = f"https://{REGION}.console.aws.amazon.com/cloudwatch/home?region={REGION}"
    lines += ["", f"Alarm: {console}#alarmsV2:alarm/{quote(name, safe='')}"]
    if DASHBOARD_NAME:
        lines.append(f"Dashboard: {console}#dashboards/dashboard/{quote(DASHBOARD_NAME, safe='')}")
    return subject, "\n".join(lines) + "\n"


def _shows_log_errors(detail):
    # Alarms that watch the API's own errors get the log lines attached.
    name = detail["alarmName"]
    return bool(API_LOG_GROUP and ERROR_PATTERN) and name in (
        "GTXMeals-ApiLogErrors", "GTXMeals-ApiErrors", "GTXMeals-Api5xx", "GTXMeals-ApiNearTimeout",
    )


def _recent_error_lines(when):
    start = when - LOG_LOOKBACK
    try:
        resp = logs.filter_log_events(
            logGroupName=API_LOG_GROUP,
            filterPattern=ERROR_PATTERN,
            startTime=int(start.timestamp() * 1000),
            endTime=int((when + timedelta(minutes=1)).timestamp() * 1000),
            limit=50,
        )
    except Exception as e:  # noqa: BLE001 - the alert still goes out without the log lines
        print(f"ERROR reading the API logs: {e!r}")
        return [f"(Couldn't read the API logs: {e})"]
    events = resp.get("events", [])[-MAX_LOG_LINES:]
    header = f"Latest errors in the API logs ({API_LOG_GROUP}, last {int(LOG_LOOKBACK.total_seconds() // 60)} minutes):"
    if not events:
        return [header, "(none found)"]
    out = [header]
    for e in events:
        stamp = _central_text(datetime.fromtimestamp(e["timestamp"] / 1000, timezone.utc))
        message = e["message"].strip()
        if len(message) > MAX_LOG_LINE_CHARS:
            message = message[:MAX_LOG_LINE_CHARS] + " ..."
        out += ["", f"--- {stamp}", message]
    return out


def _parse_time(text):
    if not text:
        return datetime.now(timezone.utc)
    # CloudWatch sends e.g. "2026-10-21T17:05:00.123+0000" or "...Z".
    text = text.replace("Z", "+00:00")
    if len(text) > 5 and text[-5] in "+-" and text[-3] != ":":
        text = text[:-2] + ":" + text[-2:]
    return datetime.fromisoformat(text)


def _central_text(when):
    # US Central with daylight saving time (second Sunday of March to first
    # Sunday of November, 2:00 AM local) - same rule as the API, no zoneinfo.
    utc = when.astimezone(timezone.utc)
    year = utc.year
    march_1 = datetime(year, 3, 1, tzinfo=timezone.utc)
    dst_start = march_1 + timedelta(days=(6 - march_1.weekday()) % 7 + 7, hours=8)  # 2 AM CST
    nov_1 = datetime(year, 11, 1, tzinfo=timezone.utc)
    dst_end = nov_1 + timedelta(days=(6 - nov_1.weekday()) % 7, hours=7)  # 2 AM CDT
    dst = dst_start <= utc < dst_end
    local = utc + timedelta(hours=-5 if dst else -6)
    hour = local.hour % 12 or 12
    return f"{local:%a %b} {local.day}, {hour}:{local:%M %p} {'CDT' if dst else 'CST'}"
