"""Screenshots of every admin page for the manual - against the local mock
API (sample data only, never the live site)."""
import base64, json, sys, time
from pathlib import Path
from playwright.sync_api import sync_playwright

# Run from the repo root: python docs/admin-manual/screenshots.py docs/admin-manual/img
sys.path.insert(0, ".")
import tests.frontend.mock_api as mock_api_module
from tests.frontend.mock_api import MockApi

# A Monday, like a normal pickup week (the mock reads this at request time).
READY_DATE = mock_api_module.READY_DATE = "2026-11-02"

OUT = Path(sys.argv[1])
M = lambda c, p, cb, f: {"calories": c, "proteinG": p, "carbsG": cb, "fatG": f}
Z = M(0, 0, 0, 0)

ADDONS = [
    ("a1", "Cauliflower Rice", 100, "Replaces the rice with cauliflower rice"),
    ("a2", "Extra Protein", 150, "+1 oz protein"),
    ("a3", "Extra Protein with Cauliflower", 250, "+1 oz protein, cauliflower rice instead of rice"),
    ("a4", "Large", 300, "+2 oz protein, +1 oz vegetables"),
    ("a5", "Large with Cauliflower Rice", 400, "+2 oz protein, +1 oz vegetables, cauliflower rice"),
]

def details(rows):
    return {aid: {"name": n, "priceCents": None, "description": d, "macros": m}
            for (aid, n, _, _), (d, m) in zip(ADDONS, rows) if d or any(m.values())}

state = {
    "meals": [
        {"mealId": "m1", "name": "Chicken Enchilada Bowl", "description": "Shredded chicken, Mexican rice, black beans, enchilada sauce",
         "priceCents": 1000, "macros": M(520, 45, 50, 14), "staple": True, "available": True, "weekOf": "",
         "addOnDetails": details([("Replaces the Mexican rice with cauliflower rice", M(450, 44, 30, 14)),
                                  ("+1 oz chicken", M(575, 52, 50, 15)), ("+1 oz chicken, cauliflower rice", M(495, 51, 30, 15)),
                                  ("+2 oz chicken, +1 oz rice", M(645, 60, 58, 17)), ("+2 oz chicken, cauliflower rice", M(565, 59, 38, 17))])},
        {"mealId": "m2", "name": "Cowboy Butter Chicken", "description": "Grilled chicken, roasted baby potatoes, green beans",
         "priceCents": 1000, "macros": M(450, 48, 35, 12), "staple": True, "available": True, "weekOf": "",
         "addOnDetails": details([("Replaces the potatoes with cauliflower rice", M(340, 48, 16, 10)),
                                  ("+1 oz chicken", M(500, 58, 37, 14)), ("", Z), ("+2 oz chicken, +1 oz potatoes", M(605, 68, 49, 16)), ("", Z)])},
        {"mealId": "m3", "name": "Smash Burger Tacos", "description": "Seasoned beef, cheddar, pickles, special sauce, flour tortillas",
         "priceCents": 1100, "macros": M(610, 38, 42, 30), "staple": False, "available": True, "weekOf": READY_DATE,
         "addOnDetails": {"a2": {"name": "Extra Protein", "priceCents": None, "description": "+1 oz beef", "macros": M(680, 45, 42, 35)},
                          "a6": {"name": "Lettuce & Tomato Swap", "priceCents": 0, "description": "Lettuce wraps and tomato instead of tortillas", "macros": M(480, 36, 12, 28)}}},
        {"mealId": "m4", "name": "Honey Sriracha Chicken Meatballs", "description": "Chicken meatballs, white rice, broccoli",
         "priceCents": 1000, "macros": M(610, 38, 70, 22), "staple": False, "available": False, "weekOf": READY_DATE, "addOnDetails": {}},
    ],
    "addOns": [{"addOnId": a, "name": n, "priceCents": p, "description": d, "available": True} for a, n, p, d in ADDONS]
              + [{"addOnId": "a6", "name": "Lettuce & Tomato Swap", "priceCents": 0, "description": "Lettuce and tomato instead of tortillas", "available": True}],
    "extras": [
        {"extraId": "e1", "name": "BBQ Sauce", "priceCents": 25, "available": True, "description": "1 oz. cup of smoky-sweet BBQ sauce", "macros": M(60, 0, 14, 0)},
        {"extraId": "e2", "name": "Salsa Verde", "priceCents": 25, "available": True, "description": "1 oz. cup of tangy tomatillo salsa verde", "macros": M(10, 0, 2, 0)},
        {"extraId": "e3", "name": "Sriracha", "priceCents": 25, "available": True, "description": "1 oz. cup of spicy chili-garlic sriracha", "macros": M(30, 0, 6, 0)},
    ],
    "customers": [
        {"username": "u1", "email": "jordan.rivera@example.com", "name": "Jordan Rivera", "phone": "5125550142", "address": "",
         "preferredContact": "email", "unsubscribed": False, "pickupLocation": ""},
        {"username": "u2", "email": "sam.patel@example.com", "name": "Sam Patel", "phone": "5125550178", "address": "",
         "preferredContact": "text", "unsubscribed": False, "pickupLocation": "Elgin"},
        {"username": "u3", "email": "alex.chen@example.com", "name": "Alex Chen", "phone": "", "address": "",
         "preferredContact": "email", "unsubscribed": True, "pickupLocation": ""},
    ],
}

def line(meal_id, name, qty, unit, addon=None, note=""):
    a = [{"addOnId": addon[0], "name": addon[1], "macros": Z}] if addon else []
    return {"mealId": meal_id, "name": name, "quantity": qty, "unitPriceCents": unit, "note": note, "selectedAddOns": a}

state["orders"] = [
    {"orderId": "o1", "customerSub": "u1", "createdAt": "2026-10-19T16:20:00Z", "status": "placed", "customerName": "Jordan Rivera",
     "contactPhone": "(512) 555-0142", "pickupName": "", "pickupLocation": "", "readyDate": READY_DATE, "mealSuggestions": "Lasagna soup!",
     "payment": "venmo", "amountPaidCents": 5000,
     "items": [line("m1", "Chicken Enchilada Bowl", 2, 1000), line("m1", "Chicken Enchilada Bowl", 1, 1100, ("a1", "Cauliflower Rice")),
               line("m3", "Smash Burger Tacos", 2, 1100, ("a6", "Lettuce & Tomato Swap"), "no pickles")],
     "extras": [{"extraId": "e2", "name": "Salsa Verde", "priceCents": 25, "quantity": 2, "description": "", "macros": Z}],
     "totalCents": 5350},
    {"orderId": "o2", "customerSub": "u2", "createdAt": "2026-10-20T13:05:00Z", "status": "placed", "customerName": "Sam Patel",
     "contactPhone": "(512) 555-0178", "pickupName": "Taylor Patel", "pickupLocation": "Elgin", "readyDate": READY_DATE, "mealSuggestions": "",
     "payment": "unpaid",
     "items": [line("m2", "Cowboy Butter Chicken", 3, 1300, ("a4", "Large")), line("m1", "Chicken Enchilada Bowl", 1, 1000)],
     "extras": [], "totalCents": 4900},
]

def session():
    b = lambda o: base64.urlsafe_b64encode(json.dumps(o).encode()).decode().rstrip("=")
    tok = f"{b({'alg': 'none'})}.{b({'cognito:groups': ['Admins'], 'email': 'gtxmeals@gmail.com'})}.sig"
    return json.dumps({"idToken": tok, "refreshToken": None, "expiresAt": int(time.time() * 1000) + 86_400_000})

api = MockApi().start()
api.state.update(state)
with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome", headless=True)
    ctx = browser.new_context(viewport={"width": 1280, "height": 900}, device_scale_factor=2)
    ctx.add_init_script("window.print = () => {};" f"localStorage.setItem('mealprep.session', {json.dumps(session())});")
    page = ctx.new_page()

    def go(path):
        page.goto(api.base_url + path); page.wait_for_load_state("networkidle"); page.wait_for_timeout(300)

    def shot(name, selector=None, full=False):
        if selector:
            page.locator(selector).first.screenshot(path=str(OUT / f"{name}.png"))
        else:
            page.screenshot(path=str(OUT / f"{name}.png"), full_page=full)
        print("saved", name)

    go("/admin.html")
    shot("nav", "header#nav")
    shot("settings", ".settings-row")
    box = page.locator("section.admin-section").first.bounding_box()
    page.screenshot(path=str(OUT / "meals.png"), full_page=True,
                    clip={"x": box["x"], "y": box["y"], "width": box["width"], "height": 1000})
    print("saved meals")
    shot("addons", "section.admin-section >> nth=1")
    shot("extras", "section.admin-section >> nth=2")
    page.click('tr[data-meal-id="m3"] .edit-meal')
    page.wait_for_timeout(300)
    shot("edit-meal", "#meal-edit-dialog")
    page.keyboard.press("Escape")

    go("/admin-orders.html")
    shot("orders", "main")

    go("/admin-prep.html")
    shot("prep", "main")
    page.emulate_media(media="print")
    page.set_viewport_size({"width": 586, "height": 1100})
    page.wait_for_timeout(300)
    shot("labels", "#print-labels .label-sheet")
    page.emulate_media(media="screen")
    page.set_viewport_size({"width": 1280, "height": 900})

    go("/customers.html")
    shot("customers", "main")
    page.click('tr[data-username="u2"] .edit-customer')
    page.wait_for_timeout(300)
    shot("edit-customer", "#edit-dialog")
    page.keyboard.press("Escape")

    go("/menu-flyer.html?period=weekly")
    shot("flyer", "body", full=False)

    go("/index.html")
    shot("menu-admin", "main")
    browser.close()
api.stop()
