"""Checks every few minutes that the website and the API respond, the way a
customer's browser would - through CloudFront for the site, and API Gateway
for the API.

Publishes GTXMeals/HealthCheck (1 = healthy, 0 = failing) per check, which
the GTXMeals-SiteDown and GTXMeals-ApiDown alarms watch. This also stands in
for CloudFront and S3 alarms: CloudFront's metrics only exist in us-east-1,
where this project can't deploy (see README).
"""
import json
import os
import urllib.request

import boto3

SITE_URL = os.environ.get("SITE_URL", "")
API_URL = os.environ.get("API_URL", "")
NAMESPACE = "GTXMeals"
TIMEOUT_SECONDS = 10

cloudwatch = boto3.client("cloudwatch")


def handler(event, context):
    results = {
        "Site": check(f"{SITE_URL}/", _looks_like_the_site),
        "Api": check(f"{API_URL}/settings", _looks_like_settings),
    }
    cloudwatch.put_metric_data(
        Namespace=NAMESPACE,
        MetricData=[
            {"MetricName": "HealthCheck", "Dimensions": [{"Name": "Check", "Value": name}],
             "Value": 1 if ok else 0, "Unit": "Count"}
            for name, ok in results.items()
        ],
    )
    return results


def check(url, looks_right):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "GTXMeals-HealthCheck"})
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            body = resp.read()
            if resp.status != 200 or not looks_right(body):
                print(f"Health check failed for {url}: HTTP {resp.status}, unexpected body {body[:200]!r}")
                return False
    except Exception as e:  # noqa: BLE001 - any failure means "down"
        print(f"Health check failed for {url}: {e!r}")
        return False
    return True


def _looks_like_the_site(body):
    return b"<html" in body.lower()


def _looks_like_settings(body):
    try:
        return "nextReadyDate" in json.loads(body)
    except ValueError:
        return False
