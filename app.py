import csv, io, json, os, re, sqlite3
from datetime import datetime, timedelta
import requests
from flask import Flask, abort, g, jsonify, render_template, request, send_file
from werkzeug.middleware.proxy_fix import ProxyFix

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("DATA_DIR", BASE)
API_KEY = os.environ.get("API_KEY", "demo-key")
CUSTOMER_API_URL = os.environ.get("CUSTOMER_API_URL", "").rstrip("/")
IN, OUT = (os.path.join(DATA, d) for d in ("received_orders", "generated_results"))
DB = os.path.join(DATA, "restaurant.db")
REQUIRED = ["order_id", "customer_id", "restaurant_id", "item", "quantity", "unit_price"]
for d in (IN, OUT):
    os.makedirs(d, exist_ok=True)

app = Flask(__name__, template_folder=os.path.join(BASE, "templates"), static_folder=os.path.join(BASE, "static"))
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.config["MAX_CONTENT_LENGTH"] = 1_000_000
app.jinja_env.globals.update(min=min, max=max)

INITIAL_INVENTORY = [
    ("R001", "Chicken Biryani", 20, 5, 180.0),
    ("R001", "Paneer Rice", 15, 4, 150.0),
    ("R001", "Coke", 30, 8, 40.0),
    ("R002", "Masala Dosa", 25, 5, 90.0),
    ("R002", "Idli Sambar", 20, 5, 60.0),
    ("R002", "Filter Coffee", 35, 10, 30.0),
    ("R003", "Veg Burger", 18, 4, 99.0),
    ("R003", "French Fries", 25, 6, 70.0),
    ("R003", "Cold Coffee", 20, 5, 80.0),
]

RESTAURANT_NAMES = {
    "R001": "Royal Restaurant",
    "R002": "South Indian Restaurant",
    "R003": "Fast Food Corner",
}


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_):
    conn = g.pop("db", None)
    if conn:
        conn.close()


def now():
    return datetime.now().isoformat(timespec="seconds")


with sqlite3.connect(DB) as c:
    c.executescript("""
    CREATE TABLE IF NOT EXISTS orders(order_id TEXT PRIMARY KEY, customer_id TEXT, restaurant_id TEXT,
        input_file TEXT, result_file TEXT, status TEXT DEFAULT 'PENDING', callback_url TEXT,
        delivered INT DEFAULT 0, received_at TEXT, updated_at TEXT);

    CREATE TABLE IF NOT EXISTS order_items(id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT,
        item_name TEXT, quantity INT, unit_price REAL, status TEXT DEFAULT 'PENDING');

    CREATE TABLE IF NOT EXISTS inventory(
        restaurant_id TEXT,
        item_name TEXT,
        available_qty INT DEFAULT 20,
        alert_threshold INT DEFAULT 5,
        unit_price REAL,
        updated_at TEXT,
        PRIMARY KEY (restaurant_id, item_name)
    );

    CREATE TABLE IF NOT EXISTS donations(
        id TEXT PRIMARY KEY,
        restaurant_id TEXT,
        restaurant_name TEXT,
        item_name TEXT,
        food_items TEXT,
        total_qty INT DEFAULT 10,
        available_qty INT DEFAULT 10,
        quantity_desc TEXT,
        publish_at TEXT,
        pickup_deadline TEXT,
        safety_notes TEXT,
        status TEXT DEFAULT 'AVAILABLE',
        charity_name TEXT,
        charity_contact TEXT,
        claims_json TEXT DEFAULT '[]',
        created_at TEXT,
        updated_at TEXT
    );

    CREATE TABLE IF NOT EXISTS scheduled_reservations(
        id TEXT PRIMARY KEY,
        customer_id TEXT,
        restaurant_id TEXT,
        meal_type TEXT,
        scheduled_date TEXT,
        scheduled_time TEXT,
        items_json TEXT,
        status TEXT DEFAULT 'RESERVED',
        decline_reason TEXT,
        callback_url TEXT,
        created_at TEXT,
        updated_at TEXT
    );

    CREATE TABLE IF NOT EXISTS group_offers(
        id TEXT PRIMARY KEY,
        restaurant_id TEXT,
        restaurant_name TEXT,
        item_name TEXT,
        original_price REAL,
        group_price REAL,
        min_participants INT,
        deadline TEXT,
        status TEXT DEFAULT 'ACTIVE',
        created_at TEXT
    );

    CREATE TABLE IF NOT EXISTS group_offer_participants(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        offer_id TEXT,
        customer_id TEXT,
        customer_name TEXT,
        joined_at TEXT,
        UNIQUE(offer_id, customer_id)
    );
    """)

    cur = c.cursor()
    # Migration for donations columns
    cur.execute("PRAGMA table_info(donations)")
    d_cols = [r[1] for r in cur.fetchall()]
    for col, c_type in [
        ("publish_at", "TEXT"),
        ("item_name", "TEXT"),
        ("total_qty", "INT DEFAULT 10"),
        ("available_qty", "INT DEFAULT 10"),
        ("claims_json", "TEXT DEFAULT '[]'"),
    ]:
        if col not in d_cols:
            try:
                cur.execute(f"ALTER TABLE donations ADD COLUMN {col} {c_type}")
            except Exception:
                pass

    # Populate initial inventory if table is empty
    cur.execute("SELECT COUNT(*) FROM inventory")
    if cur.fetchone()[0] == 0:
        now_ts = datetime.now().isoformat(timespec="seconds")
        for rid, item, qty, thres, price in INITIAL_INVENTORY:
            cur.execute(
                "INSERT INTO inventory(restaurant_id, item_name, available_qty, alert_threshold, unit_price, updated_at) VALUES(?,?,?,?,?,?)",
                (rid, item, qty, thres, price, now_ts),
            )
        # Sample starter donation
        cur.execute(
            """INSERT INTO donations(id, restaurant_id, restaurant_name, item_name, food_items, total_qty, available_qty, quantity_desc, publish_at, pickup_deadline, safety_notes, status, claims_json, created_at, updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "DON001",
                "R001",
                "Royal Restaurant",
                "Paneer Rice",
                "15 Portions of Paneer Rice",
                15,
                15,
                "15 fresh sealed portions",
                now_ts[:16].replace("T", " "),
                (datetime.now() + timedelta(hours=6)).strftime("%Y-%m-%d %H:%M"),
                "Prepared fresh today at 1:00 PM. Sealed insulated food grade containers.",
                "AVAILABLE",
                "[]",
                now_ts,
                now_ts,
            ),
        )
        c.commit()


def err(msg, code):
    return jsonify(success=False, message=msg), code


def get_order(oid):
    return db().execute("SELECT * FROM orders WHERE order_id=?", (oid,)).fetchone()


def items_of(oid):
    return db().execute("SELECT * FROM order_items WHERE order_id=?", (oid,)).fetchall()


def evaluate_donations():
    """Auto-update status based on publish time, pickup deadline, and remaining quantity."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    rows = db().execute("SELECT * FROM donations").fetchall()
    for d in rows:
        st = d["status"]
        pub = d["publish_at"] or (d["created_at"][:16].replace("T", " ") if d["created_at"] else now_str)
        dl = d["pickup_deadline"]
        avail = d["available_qty"] if d["available_qty"] is not None else 0
        if st in ("COLLECTED", "CANCELLED"):
            continue

        new_st = st
        if now_str < pub:
            new_st = "SCHEDULED"
        elif dl and now_str > dl:
            new_st = "EXPIRED"
        elif avail <= 0:
            new_st = "CLAIMED"
        elif d["total_qty"] and avail < d["total_qty"]:
            new_st = "PARTIALLY_CLAIMED"
        else:
            new_st = "AVAILABLE"

        if new_st != st:
            db().execute("UPDATE donations SET status=?, updated_at=? WHERE id=?", (new_st, now(), d["id"]))
    db().commit()


def evaluate_group_offers():
    """Auto-evaluate group offers: mark EXPIRED if deadline passed without meeting target."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    offers = db().execute("SELECT * FROM group_offers WHERE status='ACTIVE'").fetchall()
    for o in offers:
        dl = o["deadline"]
        if dl and now_str > dl:
            parts = db().execute("SELECT COUNT(*) FROM group_offer_participants WHERE offer_id=?", (o["id"],)).fetchone()[0]
            new_st = "SUCCESS" if parts >= o["min_participants"] else "EXPIRED"
            db().execute("UPDATE group_offers SET status=? WHERE id=?", (new_st, o["id"]))
            if new_st == "EXPIRED":
                # Proactively notify customer portal to process automatic wallet refunds
                try:
                    cb = CUSTOMER_API_URL or "http://127.0.0.1:5000"
                    requests.post(f"{cb}/api/group-deals/deal-expired", json={"offer_id": o["id"]}, timeout=3)
                except Exception:
                    pass
    db().commit()



# ---------- Pages ----------
@app.route("/")
def dashboard():
    evaluate_donations()
    evaluate_group_offers()
    orders = db().execute("SELECT * FROM orders ORDER BY received_at DESC").fetchall()
    count = lambda s: sum(1 for o in orders if o["status"] == s)
    stats = {k: count(k) for k in ("PENDING", "ACCEPTED", "PARTIALLY ACCEPTED", "REJECTED")}

    low_stock = db().execute(
        "SELECT * FROM inventory WHERE available_qty <= alert_threshold ORDER BY available_qty ASC"
    ).fetchall()

    active_donations = db().execute("SELECT COUNT(*) FROM donations WHERE status IN ('AVAILABLE', 'PARTIALLY_CLAIMED')").fetchone()[0]
    active_offers = db().execute("SELECT COUNT(*) FROM group_offers WHERE status='ACTIVE'").fetchone()[0]

    # Fetch advance scheduled reservations
    reservations = db().execute("SELECT * FROM scheduled_reservations ORDER BY scheduled_date ASC, scheduled_time ASC").fetchall()
    enriched_res = []
    for r in reservations:
        items = json.loads(r["items_json"]) if r["items_json"] else []
        enriched_res.append({**dict(r), "items": items, "dish_items": items, "rname": RESTAURANT_NAMES.get(r["restaurant_id"], r["restaurant_id"])})

    return render_template(
        "dashboard.html",
        orders=orders,
        stats=stats,
        low_stock=low_stock,
        active_donations=active_donations,
        active_offers=active_offers,
        reservations=enriched_res,
        names=RESTAURANT_NAMES,
    )


@app.route("/orders/<oid>")
def order_page(oid):
    o = get_order(oid) or abort(404)
    return render_template("order.html", o=o, items=items_of(oid))


@app.route("/inventory")
def inventory_page():
    rid = request.args.get("rid", "R001")
    items = db().execute("SELECT * FROM inventory WHERE restaurant_id=? ORDER BY item_name ASC", (rid,)).fetchall()
    return render_template("inventory.html", items=items, current_rid=rid, restaurants=RESTAURANT_NAMES)


@app.route("/donations")
def donations_page():
    evaluate_donations()
    rows = db().execute("SELECT * FROM donations ORDER BY created_at DESC").fetchall()
    enriched = []
    for d in rows:
        claims = json.loads(d["claims_json"]) if d["claims_json"] else []
        enriched.append({**dict(d), "claims": claims})
    # Also fetch inventory for currently selected restaurant so dropdown can show current stock
    rid = request.args.get("rid", "R001")
    inv = db().execute("SELECT * FROM inventory WHERE restaurant_id=? ORDER BY item_name ASC", (rid,)).fetchall()
    return render_template("donations.html", donations=enriched, current_rid=rid, inventory=inv, restaurants=RESTAURANT_NAMES)


@app.route("/charity")
def charity_public_page():
    """Public portal for registered charities: shows active, partially claimed, and recently rescued donations."""
    evaluate_donations()
    rows = db().execute(
        """SELECT * FROM donations
        WHERE status != 'CANCELLED'
        ORDER BY CASE status
            WHEN 'AVAILABLE' THEN 1
            WHEN 'PARTIALLY_CLAIMED' THEN 2
            WHEN 'CLAIMED' THEN 3
            WHEN 'COLLECTED' THEN 4
            WHEN 'SCHEDULED' THEN 5
            ELSE 6
        END, created_at DESC"""
    ).fetchall()
    enriched = []
    for d in rows:
        claims = json.loads(d["claims_json"]) if d["claims_json"] else []
        enriched.append({**dict(d), "claims": claims})
    return render_template("charity_public.html", donations=enriched)


@app.route("/group-offers")
def group_offers_page():
    evaluate_group_offers()
    offers = db().execute("SELECT * FROM group_offers ORDER BY created_at DESC").fetchall()
    enriched = []
    for o in offers:
        parts = db().execute("SELECT * FROM group_offer_participants WHERE offer_id=?", (o["id"],)).fetchall()
        pct = min(100, int((len(parts) / o["min_participants"]) * 100)) if o["min_participants"] else 0
        enriched.append({**dict(o), "participants": [dict(p) for p in parts], "count": len(parts), "progress_percent": pct})
    return render_template("group_offers.html", offers=enriched, restaurants=RESTAURANT_NAMES)


# ---------- Core Order API (CSV Protocol) ----------
@app.post("/api/orders/upload")
def receive_order():
    if request.headers.get("X-API-Key") != API_KEY:
        return err("Unauthorized", 401)
    f = request.files.get("file")
    if not f or not (f.filename or "").lower().endswith(".csv"):
        return err("Only .csv files are allowed", 400)
    try:
        text = f.read().decode("utf-8-sig")
        rows = list(csv.DictReader(io.StringIO(text)))
        assert rows and all(c in rows[0] for c in REQUIRED)
        for r in rows:
            assert int(r["quantity"]) > 0 and float(r["unit_price"]) >= 0 and r["item"].strip()
        oid = rows[0]["order_id"].strip()
        assert re.fullmatch(r"[A-Za-z0-9_-]{1,40}", oid) and all(r["order_id"] == oid for r in rows)
    except Exception:
        return err("Invalid CSV format", 400)
    if get_order(oid):
        return err("Order already received", 409)

    fname = f"order_{oid}.csv"
    with open(os.path.join(IN, fname), "w", newline="") as out:
        out.write(text)

    db().execute(
        "INSERT INTO orders(order_id,customer_id,restaurant_id,input_file,callback_url,received_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        (
            oid,
            rows[0]["customer_id"],
            rows[0]["restaurant_id"],
            fname,
            (request.form.get("callback_url") or "").rstrip("/"),
            now(),
            now(),
        ),
    )
    db().executemany(
        "INSERT INTO order_items(order_id,item_name,quantity,unit_price) VALUES(?,?,?,?)",
        [(oid, r["item"].strip(), int(r["quantity"]), float(r["unit_price"])) for r in rows],
    )
    db().commit()
    return jsonify(success=True, order_id=oid, message="Order received successfully")


@app.get("/api/orders")
def list_orders():
    return jsonify([dict(o) for o in db().execute("SELECT * FROM orders ORDER BY received_at DESC")])


@app.get("/api/orders/<oid>")
def api_order(oid):
    o = get_order(oid) or abort(404)
    return jsonify({**dict(o), "items": [dict(i) for i in items_of(oid)]})


def serve(oid, col, folder):
    o = get_order(oid)
    if not o or not o[col]:
        abort(404)
    return send_file(
        os.path.join(folder, o[col]),
        mimetype="text/csv",
        as_attachment=request.args.get("download") == "1",
        download_name=o[col],
    )


@app.get("/api/orders/<oid>/input")
def get_input(oid):
    return serve(oid, "input_file", IN)


@app.get("/api/orders/<oid>/result")
def get_result(oid):
    return serve(oid, "result_file", OUT)


def deliver(oid):
    o = get_order(oid)
    base = CUSTOMER_API_URL or o["callback_url"]
    if not base:
        return False, "No customer API URL configured"
    try:
        with open(os.path.join(OUT, o["result_file"]), "rb") as f:
            r = requests.post(
                f"{base}/api/orders/result",
                files={"file": (o["result_file"], f, "text/csv")},
                headers={"X-API-Key": API_KEY},
                timeout=90,
            )
        body = r.json()
        ok = bool(r.ok and body.get("success"))
        msg = body.get("message", "")
    except Exception as e:
        return False, f"Customer unreachable: {e}"
    if ok:
        db().execute("UPDATE orders SET delivered=1, updated_at=? WHERE order_id=?", (now(), oid))
        db().commit()
    return ok, msg


@app.post("/api/orders/<oid>/process")
def process(oid):
    o = get_order(oid) or abort(404)
    if o["status"] != "PENDING":
        return err("Order already processed", 409)
    dec = (request.get_json(silent=True) or {}).get("decisions", {})
    items = items_of(oid)
    for i in items:
        if str(dec.get(i["item_name"], "")).upper() not in ("ACCEPTED", "REJECTED"):
            return err(f"Decision missing for {i['item_name']}", 400)

    rid = o["restaurant_id"]
    for i in items:
        decision = dec[i["item_name"]].upper()
        if decision == "ACCEPTED":
            db().execute(
                "UPDATE inventory SET available_qty = MAX(0, available_qty - ?), updated_at=? WHERE restaurant_id=? AND item_name=?",
                (i["quantity"], now(), rid, i["item_name"]),
            )

    fname = f"order_{oid}_result.csv"
    with open(os.path.join(OUT, fname), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["order_id", "item", "quantity", "unit_price", "total_price", "status"])
        for i in items:
            w.writerow(
                [
                    oid,
                    i["item_name"],
                    i["quantity"],
                    int(i["unit_price"]),
                    int(i["quantity"] * i["unit_price"]),
                    dec[i["item_name"]].upper(),
                ]
            )
    states = {dec[i["item_name"]].upper() for i in items}
    status = "ACCEPTED" if states == {"ACCEPTED"} else "REJECTED" if states == {"REJECTED"} else "PARTIALLY ACCEPTED"
    for i in items:
        db().execute("UPDATE order_items SET status=? WHERE id=?", (dec[i["item_name"]].upper(), i["id"]))
    db().execute("UPDATE orders SET status=?, result_file=?, updated_at=? WHERE order_id=?", (status, fname, now(), oid))
    db().commit()
    ok, msg = deliver(oid)
    return jsonify(success=True, order_id=oid, status=status, delivered=ok, message=msg)


@app.post("/api/orders/result")
def send_result():
    oid = (request.get_json(silent=True) or {}).get("order_id")
    o = get_order(oid)
    if not o or not o["result_file"]:
        return err("No result to send", 404)
    ok, msg = deliver(oid)
    return jsonify(success=ok, order_id=oid, message=msg), (200 if ok else 502)


# ---------- Feature 1: Advance Scheduled Meals & Reservation API ----------
@app.post("/api/scheduled/notify")
def api_scheduled_notify():
    """Customer Portal notifies restaurant that meals have been planned in advance."""
    data = request.get_json(silent=True) or {}
    sid = data.get("scheduled_id")
    cid = data.get("customer_id", "C001")
    rid = data.get("restaurant_id")
    meal = data.get("meal_type", "LUNCH")
    s_date = data.get("scheduled_date")
    s_time = data.get("scheduled_time")
    items = data.get("items", [])
    cb = data.get("callback_url", "")

    if not sid or not rid or not items or not s_date:
        return err("Missing scheduled notification parameters", 400)

    # Reserve inventory items for the scheduled date
    for it in items:
        dish = it.get("item")
        qty = int(it.get("quantity", 1))
        db().execute(
            "UPDATE inventory SET available_qty = MAX(0, available_qty - ?), updated_at=? WHERE restaurant_id=? AND item_name=?",
            (qty, now(), rid, dish),
        )

    db().execute(
        """INSERT OR REPLACE INTO scheduled_reservations(id, customer_id, restaurant_id, meal_type,
        scheduled_date, scheduled_time, items_json, status, callback_url, created_at, updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (sid, cid, rid, meal, s_date, s_time, json.dumps(items), "RESERVED", cb, now(), now()),
    )
    db().commit()
    return jsonify(success=True, message="Scheduled order received and food portions reserved in kitchen.")


@app.post("/api/scheduled/<sid>/reject")
def api_scheduled_reject(sid):
    """Restaurant notifies customer in advance that it cannot prepare the scheduled food."""
    data = request.get_json(silent=True) or {}
    reason = data.get("reason", "Kitchen cannot prepare scheduled item(s) on selected date.")

    res = db().execute("SELECT * FROM scheduled_reservations WHERE id=?", (sid,)).fetchone()
    if not res:
        return err("Scheduled reservation not found", 404)
    if res["status"] != "RESERVED":
        return err(f"Order is already {res['status']}", 409)

    items = json.loads(res["items_json"]) if res["items_json"] else []
    # Restore reserved inventory portions
    for it in items:
        dish = it.get("item")
        qty = int(it.get("quantity", 1))
        db().execute(
            "UPDATE inventory SET available_qty = available_qty + ?, updated_at=? WHERE restaurant_id=? AND item_name=?",
            (qty, now(), res["restaurant_id"], dish),
        )

    db().execute(
        "UPDATE scheduled_reservations SET status='DECLINED_BY_KITCHEN', decline_reason=?, updated_at=? WHERE id=?",
        (reason, now(), sid),
    )
    db().commit()

    # Dispatch advance notification to customer portal callback
    cb = res["callback_url"] or CUSTOMER_API_URL or "http://127.0.0.1:5000"
    notice_sent = False
    try:
        resp = requests.post(
            f"{cb}/api/scheduled/advance-notice",
            json={"scheduled_id": sid, "reason": reason, "status": "REJECTED_BY_RESTAURANT"},
            headers={"X-API-Key": API_KEY},
            timeout=5,
        )
        notice_sent = resp.ok
    except Exception as e:
        print("Customer advance notification error:", e)

    return jsonify(success=True, message=f"Advance cancellation confirmed. Customer notified: {notice_sent}")


# ---------- Feature 3: Real-Time Inventory API ----------
@app.get("/api/inventory")
def api_get_inventory():
    rid = request.args.get("restaurant_id")
    if rid:
        rows = db().execute("SELECT * FROM inventory WHERE restaurant_id=?", (rid,)).fetchall()
    else:
        rows = db().execute("SELECT * FROM inventory").fetchall()
    res = {}
    for r in rows:
        res.setdefault(r["restaurant_id"], {})[r["item_name"]] = {
            "available_qty": r["available_qty"],
            "alert_threshold": r["alert_threshold"],
            "unit_price": r["unit_price"],
            "is_low_stock": r["available_qty"] <= r["alert_threshold"],
            "is_sold_out": r["available_qty"] <= 0,
        }
    return jsonify(success=True, inventory=res)


@app.post("/api/inventory/update")
def api_update_inventory():
    data = request.get_json(silent=True) or {}
    rid = data.get("restaurant_id")
    item = data.get("item_name")
    qty = data.get("available_qty")
    threshold = data.get("alert_threshold", 5)
    if not rid or not item or qty is None:
        return err("restaurant_id, item_name and available_qty are required", 400)
    try:
        qty = int(qty)
        threshold = int(threshold)
    except ValueError:
        return err("Quantities must be integers", 400)

    db().execute(
        """INSERT INTO inventory(restaurant_id, item_name, available_qty, alert_threshold, unit_price, updated_at)
        VALUES(?, ?, ?, ?, (SELECT unit_price FROM inventory WHERE restaurant_id=? AND item_name=?), ?)
        ON CONFLICT(restaurant_id, item_name) DO UPDATE SET
            available_qty=?, alert_threshold=?, updated_at=?""",
        (rid, item, qty, threshold, rid, item, now(), qty, threshold, now()),
    )
    db().commit()
    return jsonify(success=True, message=f"Inventory updated for {item} ({qty} in stock)")


# ---------- Feature 2: Restaurant-to-Charity Food Donation API ----------
@app.get("/api/donations")
def api_list_donations():
    evaluate_donations()
    rows = db().execute("SELECT * FROM donations ORDER BY created_at DESC").fetchall()
    enriched = []
    for r in rows:
        claims = json.loads(r["claims_json"]) if r["claims_json"] else []
        enriched.append({**dict(r), "claims": claims})
    return jsonify(success=True, donations=enriched)


@app.post("/api/donations/create")
def api_create_donation():
    data = request.get_json(silent=True) or {}
    rid = data.get("restaurant_id")
    item_name = data.get("item_name", "").strip()
    qty = data.get("quantity")
    publish_at = data.get("publish_at", "").strip()
    pickup_deadline = data.get("pickup_deadline", "").strip()
    notes = data.get("safety_notes", "Handle with care, consume within 4 hours.")

    if not rid or not item_name or not qty or not pickup_deadline:
        return err("Restaurant, food item, quantity, and collection deadline are required", 400)

    try:
        qty = int(qty)
        assert qty > 0
    except (ValueError, AssertionError):
        return err("Donation quantity must be a positive integer", 400)

    # Check stock against restaurant inventory
    inv = db().execute("SELECT * FROM inventory WHERE restaurant_id=? AND item_name=?", (rid, item_name)).fetchone()
    if not inv or inv["available_qty"] < qty:
        curr_stock = inv["available_qty"] if inv else 0
        return err(f"Cannot donate more than available inventory. Current stock of {item_name}: {curr_stock} portions.", 400)

    # Reserve/deduct donated food from inventory
    db().execute(
        "UPDATE inventory SET available_qty = available_qty - ?, updated_at=? WHERE restaurant_id=? AND item_name=?",
        (qty, now(), rid, item_name),
    )

    did = "DON" + datetime.now().strftime("%m%d%H%M%S")
    rname = RESTAURANT_NAMES.get(rid, "Partner Restaurant")
    ts = now()
    now_min = ts[:16].replace("T", " ")
    if not publish_at:
        publish_at = now_min

    initial_status = "SCHEDULED" if now_min < publish_at else "AVAILABLE"

    db().execute(
        """INSERT INTO donations(id, restaurant_id, restaurant_name, item_name, food_items,
        total_qty, available_qty, quantity_desc, publish_at, pickup_deadline, safety_notes,
        status, claims_json, created_at, updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            did,
            rid,
            rname,
            item_name,
            f"{qty} Portions of {item_name}",
            qty,
            qty,
            f"{qty} packaged portions",
            publish_at,
            pickup_deadline,
            notes,
            initial_status,
            "[]",
            ts,
            ts,
        ),
    )
    db().commit()
    return jsonify(success=True, donation_id=did, message=f"Food donation created and {qty} portions reserved from inventory!")


@app.post("/api/donations/<did>/claim")
def api_claim_donation(did):
    evaluate_donations()
    data = request.get_json(silent=True) or {}
    charity_name = data.get("charity_name", "").strip()
    charity_contact = data.get("charity_contact", "").strip()
    try:
        req_qty = int(data.get("requested_qty", 1))
        assert req_qty > 0
    except (ValueError, AssertionError):
        return err("Requested quantity must be a positive integer", 400)

    if not charity_name or not charity_contact:
        return err("Charity organization name and contact details are required", 400)

    don = db().execute("SELECT * FROM donations WHERE id=?", (did,)).fetchone()
    if not don:
        return err("Donation not found", 404)
    if don["status"] not in ("AVAILABLE", "PARTIALLY_CLAIMED"):
        return err(f"Donation is currently {don['status']} and cannot be claimed.", 409)

    avail = don["available_qty"] if don["available_qty"] is not None else 10
    if req_qty > avail:
        return err(f"Requested quantity ({req_qty}) exceeds available portions ({avail})", 400)

    new_avail = avail - req_qty
    new_status = "CLAIMED" if new_avail == 0 else "PARTIALLY_CLAIMED"

    claims = json.loads(don["claims_json"]) if don["claims_json"] else []
    claims.append({
        "charity_name": charity_name,
        "charity_contact": charity_contact,
        "quantity_claimed": req_qty,
        "claimed_at": now(),
    })

    db().execute(
        """UPDATE donations SET available_qty=?, status=?, charity_name=?, charity_contact=?,
        claims_json=?, updated_at=? WHERE id=?""",
        (new_avail, new_status, charity_name, charity_contact, json.dumps(claims), now(), did),
    )
    db().commit()
    return jsonify(
        success=True,
        claimed_qty=req_qty,
        remaining_qty=new_avail,
        status=new_status,
        message=f"Claim confirmed for {req_qty} portions by {charity_name}! Remaining: {new_avail} portions.",
    )


@app.post("/api/donations/<did>/collect")
def api_collect_donation(did):
    don = db().execute("SELECT * FROM donations WHERE id=?", (did,)).fetchone()
    if not don:
        return err("Donation not found", 404)
    db().execute("UPDATE donations SET status='COLLECTED', updated_at=? WHERE id=?", (now(), did))
    db().commit()
    return jsonify(success=True, message="Donation verified as collected successfully")


# ---------- Feature 4: Group Buying and Time-Limited Offers API ----------
@app.get("/api/group-offers")
def api_get_group_offers():
    evaluate_group_offers()
    offers = db().execute("SELECT * FROM group_offers ORDER BY created_at DESC").fetchall()
    result = []
    for o in offers:
        parts = db().execute("SELECT * FROM group_offer_participants WHERE offer_id=?", (o["id"],)).fetchall()
        pct = min(100, int((len(parts) / o["min_participants"]) * 100)) if o["min_participants"] else 0
        result.append(
            {
                **dict(o),
                "current_participants": len(parts),
                "participants": [dict(p) for p in parts],
                "progress_percent": pct,
            }
        )
    return jsonify(success=True, offers=result)


@app.post("/api/group-offers/create")
def api_create_group_offer():
    data = request.get_json(silent=True) or {}
    rid = data.get("restaurant_id")
    item = data.get("item_name")
    orig_price = data.get("original_price")
    grp_price = data.get("group_price")
    min_parts = data.get("min_participants")
    deadline = data.get("deadline")

    if not all([rid, item, orig_price, grp_price, min_parts, deadline]):
        return err("All group offer fields are required", 400)

    try:
        orig_price = float(orig_price)
        grp_price = float(grp_price)
        min_parts = int(min_parts)
    except ValueError:
        return err("Prices and participant counts must be valid numbers", 400)

    gid = "GRP" + datetime.now().strftime("%m%d%H%M%S")
    rname = RESTAURANT_NAMES.get(rid, "Partner Restaurant")
    ts = now()

    db().execute(
        """INSERT INTO group_offers(id, restaurant_id, restaurant_name, item_name, original_price,
        group_price, min_participants, deadline, status, created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (gid, rid, rname, item, orig_price, grp_price, min_parts, deadline, "ACTIVE", ts),
    )
    db().commit()
    return jsonify(success=True, offer_id=gid, message=f"Group Deal created for {item} at ₹{int(grp_price)}!")


@app.post("/api/group-offers/<gid>/join")
def api_join_group_offer(gid):
    evaluate_group_offers()
    data = request.get_json(silent=True) or {}
    cid = data.get("customer_id", "C001")
    cname = data.get("customer_name", "Customer " + cid)

    offer = db().execute("SELECT * FROM group_offers WHERE id=?", (gid,)).fetchone()
    if not offer:
        return err("Offer not found", 404)
    if offer["status"] != "ACTIVE":
        return err(f"Offer is not active (Status: {offer['status']})", 409)

    existing = db().execute(
        "SELECT * FROM group_offer_participants WHERE offer_id=? AND customer_id=?", (gid, cid)
    ).fetchone()
    if existing:
        return err("You have already joined this group deal!", 409)

    db().execute(
        "INSERT INTO group_offer_participants(offer_id, customer_id, customer_name, joined_at) VALUES(?,?,?,?)",
        (gid, cid, cname, now()),
    )
    db().commit()

    count = db().execute("SELECT COUNT(*) FROM group_offer_participants WHERE offer_id=?", (gid,)).fetchone()[0]
    activated = False
    if count >= offer["min_participants"]:
        db().execute("UPDATE group_offers SET status='SUCCESS' WHERE id=?", (gid,))
        db().commit()
        activated = True

    return jsonify(
        success=True,
        offer_id=gid,
        current_participants=count,
        min_participants=offer["min_participants"],
        activated=activated,
        message="Joined group deal successfully!" + (" Goal reached! Deal Activated!" if activated else ""),
    )


if __name__ == "__main__":
    app.run(port=5001, debug=True)
