"""
TPPL ERP — Google Sheets Dynamic Data Fetcher
================================================
This module fetches live data from the TPPL Google Sheets spreadsheet
and prepares it for injection into the ERP HTML frontend.

SETUP INSTRUCTIONS:
1. pip install gspread google-auth flask flask-cors
2. Place your Google Service Account JSON key as 'service_account.json'
3. Share your Google Sheet with the service account email
4. Set SPREADSHEET_ID below to your main data spreadsheet
5. Run: python tppl_sheets_fetcher.py
6. The HTML file fetches data from http://localhost:5000/api/erp-data

SHEET STRUCTURE EXPECTED:
  - "order"          → Sales Orders (Date, Client Name, Product, Qty, SO No)
  - "pending sales"  → Pending Orders (Order Date, Company Name, Product Name, Pending Qty, SO No)
  - "o2d"            → Order to Dispatch (Timestamp, SO_No, Client_Name, Product, Qty, SO_Date, Step, Agent_Name, Notes)
  - "Stock"          → Stock Register
  - "Dispatch"       → Dispatch Orders (Date, Party Name, Item Description, Qty, Amount)
  - "Production Requirement" → Production data
"""

import json
import os
from datetime import datetime, timedelta
from flask import Flask, jsonify
from flask_cors import CORS

# ── CONFIGURATION ──────────────────────────────────────────────────────────────
SPREADSHEET_ID       = "YOUR_MAIN_SPREADSHEET_ID_HERE"   # Replace with your Sheet ID
FMS_SHEET_ID         = "YOUR_FMS_SHEET_ID_HERE"           # Replace with your FMS/o2d Sheet ID
CALL_LATER_SHEET_ID  = "15CNKwJtUmGlZVHNJIJQiK27jVdk3RtycAOOoC75qACc"
DONE_SHEET_ID        = "1T0pj7dWZ8ixYaeLORVKtmO55TYCDBjFpNSp4KuJg9o4"
O2D_SOURCE_SHEET_ID  = "1A3wZ4PvmuNn3TWOI96W3IUK62oxOFzY6_JueiaXBuKA"
O2D_CALL_LATER_ID    = "19H9thoVTStj7kCBOoODvpGD7T2I9uj01FrQbqBQY6A0"
O2D_DONE_SHEET_ID    = "1T0pj7dWZ8ixYaeLORVKtmO55TYCDBjFpNSp4KuJg9o4"

SERVICE_ACCOUNT_FILE = "service_account.json"   # Your Google Service Account JSON key
O2D_PLAN_DAYS        = 3                         # Days after SO date to plan dispatch
PORT                 = 5000

# ── IMPORTS ────────────────────────────────────────────────────────────────────
try:
    import gspread
    from google.oauth2.service_account import Credentials
    GSPREAD_AVAILABLE = True
except ImportError:
    GSPREAD_AVAILABLE = False
    print("⚠ gspread not installed. Run: pip install gspread google-auth")

app = Flask(__name__)
CORS(app)  # Allow HTML frontend to call this API


# ── GOOGLE SHEETS CLIENT ───────────────────────────────────────────────────────
def get_gspread_client():
    """Return an authenticated gspread client using the service account."""
    if not GSPREAD_AVAILABLE:
        raise RuntimeError("gspread is not installed")
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive.readonly",
    ]
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=scopes)
    return gspread.authorize(creds)


# ── SHEET FETCHERS ─────────────────────────────────────────────────────────────

def fetch_sheet_as_records(spreadsheet_id: str, sheet_name: str) -> list[dict]:
    """Fetch all rows from a named sheet tab as a list of dicts."""
    client = get_gspread_client()
    spreadsheet = client.open_by_key(spreadsheet_id)
    worksheet = spreadsheet.worksheet(sheet_name)
    records = worksheet.get_all_records(empty2zero=False, default_blank="")
    return records


def fetch_sales_orders() -> list[dict]:
    """Fetch all rows from the 'order' sheet tab."""
    return fetch_sheet_as_records(SPREADSHEET_ID, "order")


def fetch_pending_orders() -> list[dict]:
    """Fetch all rows from the 'pending sales' sheet tab."""
    return fetch_sheet_as_records(SPREADSHEET_ID, "pending sales")


def fetch_dispatch_orders() -> list[dict]:
    """Fetch all rows from the 'Dispatch' sheet tab."""
    return fetch_sheet_as_records(SPREADSHEET_ID, "Dispatch")


def fetch_stock_register() -> list[dict]:
    """Fetch all rows from the 'Stock' sheet tab."""
    return fetch_sheet_as_records(SPREADSHEET_ID, "Stock")


def fetch_production_requirements() -> list[dict]:
    """Fetch all rows from the 'Production Requirement' sheet tab."""
    return fetch_sheet_as_records(SPREADSHEET_ID, "Production Requirement")


def fetch_fms_advance_orders() -> list[dict]:
    """
    Fetch orders with Payment Terms = ADVANCE from the 'o2d' sheet.
    Groups individual item rows into order-level records with nested items list.
    """
    raw = fetch_sheet_as_records(FMS_SHEET_ID, "o2d")
    orders_map: dict[str, dict] = {}

    for row in raw:
        payment_terms = str(row.get("Payment Terms", "")).strip().upper()
        if payment_terms != "ADVANCE":
            continue

        so_no = str(row.get("SO No", "")).strip()
        if not so_no:
            continue

        if so_no not in orders_map:
            orders_map[so_no] = {
                "SO No":         row.get("SO No", ""),
                "Date":          row.get("Date", ""),
                "Client Name":   row.get("Client Name", ""),
                "Payment Terms": "ADVANCE",
                "PO Number":     row.get("PO Number", ""),
                "Total Qty":     0,
                "Amount":        0.0,
                "Total Bill":    0.0,
                "Items":         0,
                "CRM Status":    "Pending Call",
                "items":         [],
            }

        qty    = _to_float(row.get("Qty", 0))
        amount = _to_float(row.get("Amount", 0))
        total  = _to_float(row.get("Total", 0))

        orders_map[so_no]["Total Qty"]  += qty
        orders_map[so_no]["Amount"]     += amount
        orders_map[so_no]["Total Bill"] += total
        orders_map[so_no]["Items"]      += 1
        orders_map[so_no]["items"].append(row)

    return list(orders_map.values())


def fetch_o2d_pipeline() -> list[dict]:
    """
    Fetch Order-to-Dispatch pipeline rows from the O2D source sheet.
    Expected columns: Timestamp, SO_No, Client_Name, Product, Qty,
                      SO_Date, Step, Agent_Name, Notes
    Step values: "Product Planning" | "Full Kitting" | "Ready" | "Hold"
    """
    raw = fetch_sheet_as_records(O2D_SOURCE_SHEET_ID, "Sheet1")
    results = []
    today = datetime.today()

    for row in raw:
        # Normalise header names (replace spaces with underscores)
        normalised = {k.replace(" ", "_"): v for k, v in row.items()}

        so_date_str = str(normalised.get("SO_Date", "")).strip()
        plan_date_str = ""
        if so_date_str:
            try:
                so_date = datetime.strptime(so_date_str, "%Y-%m-%d")
                plan_date = so_date + timedelta(days=O2D_PLAN_DAYS)
                plan_date_str = plan_date.strftime("%Y-%m-%d")
            except ValueError:
                plan_date_str = ""

        results.append({
            "Timestamp":   normalised.get("Timestamp", ""),
            "SO_No":       str(normalised.get("SO_No", "")).strip(),
            "Client_Name": str(normalised.get("Client_Name", "")).strip(),
            "Product":     str(normalised.get("Product", "")).strip(),
            "Qty":         _to_float(normalised.get("Qty", 0)),
            "SO_Date":     so_date_str,
            "Plan_Date":   plan_date_str,
            "Step":        str(normalised.get("Step", "Product Planning")).strip(),
            "Agent_Name":  str(normalised.get("Agent_Name", "")).strip(),
            "Notes":       str(normalised.get("Notes", "")).strip(),
        })

    return results


# ── DERIVED METRICS ────────────────────────────────────────────────────────────

def compute_dashboard_metrics(
    orders: list[dict],
    pending: list[dict],
    dispatch: list[dict],
    stock: list[dict],
    production: list[dict],
    fms: list[dict],
) -> dict:
    """Compute the top-level dashboard metric cards."""
    total_order_lines = len(orders)
    total_qty_ordered = sum(_to_float(r.get("Qty", 0)) for r in orders)

    pending_lines = len(pending)
    pending_bags  = sum(_to_float(r.get("Pending Qty", 0)) for r in pending)
    pending_customers = len({str(r.get("Company Name", "")).strip() for r in pending if r.get("Company Name")})

    dispatched_lines = len(dispatch)
    dispatched_bags  = sum(_to_float(r.get("Qty", 0)) for r in dispatch)

    prod_lines = len(production)
    prod_bags  = sum(_to_float(r.get("Qty", 0) or r.get("Pending Qty", 0)) for r in production)

    stock_items = len(stock)

    fms_count = len(fms)
    fms_value = sum(_to_float(r.get("Total Bill", 0)) for r in fms)

    return {
        "order_lines":        total_order_lines,
        "total_qty_ordered":  total_qty_ordered,
        "pending_lines":      pending_lines,
        "pending_bags":       pending_bags,
        "pending_customers":  pending_customers,
        "dispatched_lines":   dispatched_lines,
        "dispatched_bags":    dispatched_bags,
        "production_lines":   prod_lines,
        "production_bags":    prod_bags,
        "stock_items":        stock_items,
        "fms_advance_count":  fms_count,
        "fms_advance_value":  fms_value,
        "last_updated":       datetime.now().strftime("%d %b %Y %H:%M"),
    }


# ── HELPER ─────────────────────────────────────────────────────────────────────

def _to_float(value) -> float:
    try:
        return float(str(value).replace(",", "").strip())
    except (ValueError, TypeError):
        return 0.0


# ── WRITE BACK TO SHEET ────────────────────────────────────────────────────────

def append_row_to_sheet(spreadsheet_id: str, sheet_name: str, row_data: list):
    """Append a single row to a Google Sheet."""
    client = get_gspread_client()
    spreadsheet = client.open_by_key(spreadsheet_id)
    worksheet = spreadsheet.worksheet(sheet_name)
    worksheet.append_row(row_data, value_input_option="USER_ENTERED")


# ── FLASK API ENDPOINTS ────────────────────────────────────────────────────────

@app.route("/api/erp-data", methods=["GET"])
def get_erp_data():
    """
    Master endpoint — returns ALL ERP data in one JSON response.
    The HTML frontend calls this once on load and renders everything.
    """
    try:
        orders     = fetch_sales_orders()
        pending    = fetch_pending_orders()
        dispatch   = fetch_dispatch_orders()
        stock      = fetch_stock_register()
        production = fetch_production_requirements()
        fms        = fetch_fms_advance_orders()
        o2d        = fetch_o2d_pipeline()
        metrics    = compute_dashboard_metrics(orders, pending, dispatch, stock, production, fms)

        return jsonify({
            "ok":         True,
            "metrics":    metrics,
            "orders":     orders,
            "pending":    pending,
            "dispatch":   dispatch,
            "stock":      stock,
            "production": production,
            "fms":        fms,
            "o2d":        o2d,
        })

    except FileNotFoundError:
        return jsonify({"ok": False, "error": "service_account.json not found — see setup instructions"}), 500
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/orders", methods=["GET"])
def get_orders():
    return jsonify(fetch_sales_orders())


@app.route("/api/pending", methods=["GET"])
def get_pending():
    return jsonify(fetch_pending_orders())


@app.route("/api/fms", methods=["GET"])
def get_fms():
    return jsonify(fetch_fms_advance_orders())


@app.route("/api/o2d", methods=["GET"])
def get_o2d():
    return jsonify(fetch_o2d_pipeline())


@app.route("/api/dispatch", methods=["GET"])
def get_dispatch():
    return jsonify(fetch_dispatch_orders())


@app.route("/api/stock", methods=["GET"])
def get_stock():
    return jsonify(fetch_stock_register())


@app.route("/api/production", methods=["GET"])
def get_production():
    return jsonify(fetch_production_requirements())


@app.route("/api/append/call-later", methods=["POST"])
def append_call_later():
    """Proxy endpoint to write a 'Call Later' row to Google Sheets."""
    from flask import request
    data = request.get_json()
    row  = data.get("row", [])
    try:
        append_row_to_sheet(CALL_LATER_SHEET_ID, "Sheet1", row)
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/append/done", methods=["POST"])
def append_done():
    """Proxy endpoint to write a 'Payment Done' row to Google Sheets."""
    from flask import request
    data = request.get_json()
    row  = data.get("row", [])
    try:
        append_row_to_sheet(DONE_SHEET_ID, "Sheet1", row)
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/append/o2d-call-later", methods=["POST"])
def append_o2d_call_later():
    from flask import request
    data = request.get_json()
    row  = data.get("row", [])
    try:
        append_row_to_sheet(O2D_CALL_LATER_ID, "Sheet1", row)
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/append/o2d-done", methods=["POST"])
def append_o2d_done():
    from flask import request
    data = request.get_json()
    row  = data.get("row", [])
    try:
        append_row_to_sheet(O2D_DONE_SHEET_ID, "Sheet1", row)
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/append/rate-checklist", methods=["POST"])
def append_rate_checklist():
    from flask import request
    RATE_CL_SHEET_URL = "https://script.google.com/a/macros/takkarpolychem.com/s/AKfycbysaa_5eoEQjD2G57IRnPzV0O2YNo-WfPWxweyoSAK5j1kwbmUe5Q4nvX6PiYz0cSQ/exec"
    data = request.get_json()
    row  = data.get("row", [])
    # Rate checklist uses the Google Apps Script web app endpoint
    import urllib.request, json as _json
    payload = _json.dumps({"action": "rate_checklist", "data": row}).encode()
    req = urllib.request.Request(RATE_CL_SHEET_URL, data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "service": "TPPL ERP Sheets Fetcher", "time": datetime.now().isoformat()})


# ── STANDALONE SCRIPT MODE ─────────────────────────────────────────────────────

def print_summary():
    """Quick CLI check — prints counts for all sheets."""
    print("── TPPL ERP Google Sheets Fetch Test ──")
    checks = [
        ("Sales Orders",            lambda: fetch_sales_orders()),
        ("Pending Orders",          lambda: fetch_pending_orders()),
        ("Dispatch",                lambda: fetch_dispatch_orders()),
        ("Stock Register",          lambda: fetch_stock_register()),
        ("Production Requirements", lambda: fetch_production_requirements()),
        ("FMS Advance Orders",      lambda: fetch_fms_advance_orders()),
        ("O2D Pipeline",            lambda: fetch_o2d_pipeline()),
    ]
    for name, fn in checks:
        try:
            rows = fn()
            print(f"  ✅ {name}: {len(rows)} rows")
        except Exception as exc:
            print(f"  ❌ {name}: {exc}")


if __name__ == "__main__":
    import sys

    if "--test" in sys.argv:
        print_summary()
    else:
        print(f"🚀 TPPL ERP Sheets API starting on http://localhost:{PORT}")
        print(f"   Endpoints:")
        print(f"     GET  /api/erp-data          → All ERP data (single call for HTML)")
        print(f"     GET  /api/orders             → Sales orders")
        print(f"     GET  /api/pending            → Pending orders")
        print(f"     GET  /api/fms               → FMS advance orders")
        print(f"     GET  /api/o2d               → O2D pipeline")
        print(f"     GET  /api/dispatch          → Dispatch orders")
        print(f"     GET  /api/stock             → Stock register")
        print(f"     GET  /api/production        → Production requirements")
        print(f"     POST /api/append/call-later → Append to call-later sheet")
        print(f"     POST /api/append/done       → Append to done sheet")
        print(f"     GET  /api/health            → Health check")
        app.run(host="0.0.0.0", port=PORT, debug=True)
