"""A stand-in backend for frontend tests: serves frontend/ as static files,
a /config.json pointing the pages at itself, and an in-memory version of
the /api/* routes the admin pages call. No AWS, no Docker.

Seed data includes a markup payload in customer-entered fields, so every
page test also proves that text is escaped rather than run (see XSS).
"""
import copy
import json
import threading
import time
import uuid
from datetime import date, timedelta
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"

# Relative to today, not a fixed date: the menu page closes ordering within
# 2 days of the ready date, so a hardcoded date would expire.
READY_DATE = (date.today() + timedelta(days=14)).isoformat()

# Sets window.__xss if it ever runs - tests assert it never does.
XSS = '<img src=x onerror="window.__xss=1">'

SEED = {
    # The signed-in customer's own profile (GET/PUT /profile).
    "profile": {"name": "Test Customer", "email": "customer@example.com", "phone": "(555) 222-3333",
                "address": "5 Pine Rd", "preferredContact": "text", "unsubscribed": False},
    "settings": {"nextReadyDate": READY_DATE, "smsEnabled": False, "contactEmail": "admin@example.com",
                 "orderingMode": "auto"},
    # Stands in for the real API's clock: whether Monday 11 AM - Friday 4 PM
    # is "now". Set False to simulate the weekend.
    "schedule_open": True,
    # Whether customers' orders are past their Friday change deadline.
    "changes_locked": False,
    "meals": [
        {"mealId": "m1", "name": "Chicken Teriyaki Bowl", "description": "Rice, broccoli", "priceCents": 1299,
         "macros": {"calories": 550, "proteinG": 42, "carbsG": 60, "fatG": 12}, "staple": True, "available": True},
        {"mealId": "m2", "name": "Turkey Chili", "description": "", "priceCents": 1150,
         "macros": {"calories": 480, "proteinG": 38, "carbsG": 40, "fatG": 14},
         "staple": False, "weekOf": READY_DATE, "available": False},
    ],
    "addOns": [
        {"addOnId": "a1", "name": "Large", "priceCents": 300,
         "description": "Includes 2 extra ounces of protein and 1 extra ounce of vegetables.",
         "macros": {"calories": 200, "proteinG": 15, "carbsG": 20, "fatG": 5}, "available": True},
    ],
    "orders": [
        {"orderId": "o1", "customerSub": "u1", "createdAt": "2026-10-17T15:04:00Z", "status": "placed",
         "customerName": "Jane " + XSS, "contactPhone": "(555) 555-1234", "pickupName": "",
         "readyDate": READY_DATE, "totalCents": 3198,
         "items": [{"mealId": "m1", "name": "Chicken Teriyaki Bowl", "quantity": 2, "unitPriceCents": 1599,
                    "note": "hold the sauce " + XSS,
                    "selectedAddOns": [{"addOnId": "a1", "name": "Large", "macros": {"calories": 200}}]}]},
        {"orderId": "o2", "customerSub": "u2", "createdAt": "2026-10-16T12:00:00Z", "status": "placed",
         "customerName": "Bob Smith", "contactPhone": "(555) 555-9876", "pickupName": "",
         "readyDate": READY_DATE, "totalCents": 1150,
         "items": [{"mealId": "m2", "name": "Turkey Chili", "quantity": 1, "unitPriceCents": 1150}]},
    ],
    "customers": [
        {"username": "u1", "email": "jane@example.com", "name": "Jane " + XSS, "phone": "5555551234",
         "address": "1 Main St", "preferredContact": "email", "unsubscribed": False},
        {"username": "u2", "email": "bob@example.com", "name": "Bob Smith", "phone": "",
         "address": "22 Oak Ave", "preferredContact": "text", "unsubscribed": True},
    ],
}


class MockApi:
    def __init__(self):
        self.lock = threading.Lock()
        self.reset()
        handler = partial(_Handler, self, directory=str(FRONTEND_DIR))
        self.server = _Server(("127.0.0.1", 0), handler)
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def reset(self):
        self.state = copy.deepcopy(SEED)
        self.calls = []
        # (method, path) -> (status, message): make that route fail, to
        # exercise the pages' error handling.
        self.failures = {}
        # (method, path) -> seconds: hold that route's response, to catch
        # the page acting on a request that's still in flight.
        self.delays = {}

    def delay(self, method, path, seconds):
        self.delays[(method, path)] = seconds

    def fail(self, method, path, status=400, message="simulated failure"):
        self.failures[(method, path)] = (status, message)

    def writes(self):
        """Every non-GET call the page made, oldest first."""
        return [c for c in self.calls if c["method"] != "GET"]

    def handle(self, method, path, query, body, auth=None):
        if (method, path) in self.delays:
            time.sleep(self.delays[(method, path)])
        with self.lock:
            self.calls.append({"method": method, "path": path, "body": body, "auth": auth})
            if (method, path) in self.failures:
                status, message = self.failures[(method, path)]
                return status, {"message": message}
            return self._route(method, path, query, body)

    def _route(self, method, path, query, body):
        s = self.state
        parts = path.strip("/").split("/")

        if path == "/settings":
            if method == "PUT":
                s["settings"].update(body)
            mode = s["settings"].get("orderingMode", "auto")
            ordering = (
                {"open": s["schedule_open"], "reason": "schedule"} if mode == "auto"
                else {"open": mode == "open", "reason": "forced"}
            )
            return 200, {**s["settings"], "ordering": ordering}

        if path == "/profile":
            if method == "PUT":
                # Partial update, like the real API: omitted fields are kept.
                s["profile"].update(body)
            return 200, s["profile"]

        if path == "/orders" and method == "POST":
            # Like the real API: the name comes from the profile, never the
            # request, and every order needs a 10-digit contact phone.
            if not s["profile"].get("name"):
                return 400, {"message": "Add your name on My Account before placing an order."}
            digits = "".join(ch for ch in body.get("contactPhone", "") if ch.isdigit())
            if digits and len(digits) != 10:
                return 400, {"message": "Please enter a 10-digit contact phone number, or leave it blank."}
            meals = {m["mealId"]: m for m in s["meals"]}
            add_ons = {a["addOnId"]: a for a in s["addOns"]}
            items = [{"mealId": i["mealId"], "name": meals[i["mealId"]]["name"], "quantity": i["quantity"],
                      "unitPriceCents": meals[i["mealId"]]["priceCents"],
                      # Snapshots the add-on's name, like the real API.
                      "selectedAddOns": [
                          {"addOnId": a, "name": add_ons[a]["name"], "priceCents": add_ons[a]["priceCents"]}
                          for a in i.get("selectedAddOnIds", [])
                      ],
                      "note": i.get("note", "")} for i in body["items"]]
            order = {
                "items": items, "customerName": s["profile"]["name"],
                "contactPhone": body["contactPhone"], "pickupName": body.get("pickupName", ""),
                "orderId": uuid.uuid4().hex[:8], "status": "placed", "readyDate": READY_DATE,
                "createdAt": "2026-10-18T12:00:00Z", "totalCents": 1299,
            }
            s.setdefault("placed", []).append(order)
            return 201, order

        if path == "/orders" and method == "GET":
            # Stands in for the real API's per-order deadline check.
            return 200, {"orders": [
                {**o, "changeable": o["status"] == "placed" and not s["changes_locked"]}
                for o in s.get("placed", [])
            ]}

        if parts[0] == "orders" and len(parts) == 2 and method == "PUT":
            if s["changes_locked"]:
                return 400, {"message": "This order can no longer be changed or cancelled online - "
                                        "the deadline was Friday at 2:00 PM. Please call or email us."}
            order = next(o for o in s.get("placed", []) if o["orderId"] == parts[1])
            order["status"] = body["status"]
            return 200, order

        if parts[0] in ("meals", "addons"):
            key, id_key, required = (
                ("meals", "mealId", "name") if parts[0] == "meals" else ("addOns", "addOnId", "name")
            )
            if method == "GET":
                return 200, {key: s[key]}
            if method == "POST":
                if not body.get(required):
                    return 400, {"message": f"{required} is required"}
                item = {**body, id_key: uuid.uuid4().hex[:8], "available": True}
                s[key].append(item)
                return 201, item
            item = next((i for i in s[key] if i[id_key] == parts[1]), None)
            if item is None:
                return 404, {"message": "not found"}
            if method == "PUT":
                item.update(body)
                return 200, item
            s[key].remove(item)
            return 204, None

        if parts[:2] == ["admin", "orders"]:
            if method == "PUT":
                order = next(o for o in s["orders"] if o["orderId"] == parts[3])
                order["status"] = body["status"]
                return 200, order
            status = query.get("status", ["all"])[0]
            orders = s["orders"] if status == "all" else [o for o in s["orders"] if o["status"] == status]
            return 200, {"orders": orders}

        if parts[0] == "customers":
            if method == "GET":
                return 200, {"customers": s["customers"]}
            customer = next(c for c in s["customers"] if c["username"] == parts[1])
            if method == "PUT":
                customer.update(body)
                return 200, customer
            s["customers"].remove(customer)
            return 204, None

        if path == "/reminders/send":
            return 200, {"sentEmail": 1, "sentText": 0, "skippedUnsubscribed": 1, "failed": 0, "errors": []}

        if path == "/menu/send":
            return 200, {"sentEmail": 1, "sentText": 0, "skippedUnsubscribed": 1, "skippedSmsDisabled": 0,
                         "skippedNoContact": 0, "failed": 0}

        return 404, {"message": "not found"}


class _Server(ThreadingHTTPServer):
    # Chrome opens many connections at once while loading a page's module
    # scripts; the default listen backlog of 5 overflows, and a dropped
    # connection can silently stop a page's script from ever running.
    request_queue_size = 128
    daemon_threads = True


class _Handler(SimpleHTTPRequestHandler):
    def __init__(self, api, *args, **kwargs):
        self.api = api
        super().__init__(*args, **kwargs)

    def log_message(self, *args):
        pass

    def _send_json(self, status, body):
        if body is None:
            # Bare 204 like API Gateway's - no body, no body headers.
            self.send_response(status)
            self.end_headers()
            return
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _api(self, method):
        url = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}") if method in ("POST", "PUT") else None
        status, payload = self.api.handle(
            method, url.path[len("/api"):], parse_qs(url.query), body, self.headers.get("Authorization")
        )
        self._send_json(status, payload)

    def do_GET(self):
        if self.path.startswith("/api/"):
            return self._api("GET")
        if self.path == "/config.json":
            return self._send_json(200, {
                "apiUrl": f"{self.api.base_url}/api",
                "region": "us-east-2",
                "userPoolClientId": "test-client",
            })
        return super().do_GET()

    def do_POST(self):
        self._api("POST")

    def do_PUT(self):
        self._api("PUT")

    def do_DELETE(self):
        self._api("DELETE")
