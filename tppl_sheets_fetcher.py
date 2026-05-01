/**
 * TPPL ERP — Google Sheets Dynamic Data Fetcher (Node.js)
 * =========================================================
 * Exact JS equivalent of tppl_sheets_fetcher.py
 *
 * SETUP:
 * 1. npm install express cors
 * 2. Place service_account.json next to this file  (or set env vars — see below)
 * 3. Share every sheet with the service account email
 * 4. node tppl_sheets_fetcher.js
 * 5. HTML frontend fetches from http://localhost:5000/api/erp-data
 *
 * ENV VARS (optional — overrides service_account.json, required for Vercel):
 *   GOOGLE_CLIENT_EMAIL   → client_email from service_account.json
 *   GOOGLE_PRIVATE_KEY    → private_key  from service_account.json
 *
 * SHEET MAP (confirmed from Google Drive):
 *   Main data spreadsheet    → "data spreadsheet"          1MgsPCBWo-GGbGf-I_Y0LRCtY64B1aVbVQQYpTqFI4NY
 *     Tabs: Dispatch, order, pending sales, Stock, Production Requirement
 *
 *   Dispatch FMS source      → "New Dispatch fms DEC 2025" 17JDVzgF7pK_7C25_k8VKIlC4gbizdASaYjob2JDQWzo
 *     Tab: DATA
 *
 *   O2D / FMS source         →                             1A3wZ4PvmuNn3TWOI96W3IUK62oxOFzY6_JueiaXBuKA
 *   Collection FMS log       → "TPPL Collection FMS"       1nqIlxfNARypJycUBCKL736Vm082gAOBC3ljdUUm6x4s
 *   FMS done / O2D done      →                             1T0pj7dWZ8ixYaeLORVKtmO55TYCDBjFpNSp4KuJg9o4
 *   O2D call-later           →                             19H9thoVTStj7kCBOoODvpGD7T2I9uj01FrQbqBQY6A0
 *   Dispatch FMS Hold log    →                             14tSrq3GAFtY144Wp9DbW3Q6_isIIr2u2PIJM5O7b478
 *   Dispatch FMS Done log    →                             1zhZQeU4nr2P8JUFJJK1a9gs1li34xZT-zpzAR9KEgoQ
 */

'use strict';

const fs      = require('fs');
const path    = require('path');
const express = require('express');
const cors    = require('cors');

// ══════════════════════════════════════════════════════════════════════════════
// SHEET IDs
// ══════════════════════════════════════════════════════════════════════════════

const SPREADSHEET_ID             = '1MgsPCBWo-GGbGf-I_Y0LRCtY64B1aVbVQQYpTqFI4NY';
const DISPATCH_FMS_SOURCE_ID     = '17JDVzgF7pK_7C25_k8VKIlC4gbizdASaYjob2JDQWzo';
const O2D_SOURCE_SHEET_ID        = '1A3wZ4PvmuNn3TWOI96W3IUK62oxOFzY6_JueiaXBuKA';
const FMS_SHEET_ID               = '1A3wZ4PvmuNn3TWOI96W3IUK62oxOFzY6_JueiaXBuKA';
const CALL_LATER_SHEET_ID        = '1nqIlxfNARypJycUBCKL736Vm082gAOBC3ljdUUm6x4s';
const DONE_SHEET_ID              = '1T0pj7dWZ8ixYaeLORVKtmO55TYCDBjFpNSp4KuJg9o4';
const O2D_CALL_LATER_ID          = '19H9thoVTStj7kCBOoODvpGD7T2I9uj01FrQbqBQY6A0';
const O2D_DONE_SHEET_ID          = '1T0pj7dWZ8ixYaeLORVKtmO55TYCDBjFpNSp4KuJg9o4';
const DISPATCH_FMS_HOLD_SHEET_ID = '14tSrq3GAFtY144Wp9DbW3Q6_isIIr2u2PIJM5O7b478';
const DISPATCH_FMS_DONE_SHEET_ID = '1zhZQeU4nr2P8JUFJJK1a9gs1li34xZT-zpzAR9KEgoQ';

const RATE_CL_SHEET_URL =
  'https://script.google.com/a/macros/takkarpolychem.com/s/' +
  'AKfycbysaa_5eoEQjD2G57IRnPzV0O2YNo-WfPWxweyoSAK5j1kwbmUe5Q4nvX6PiYz0cSQ/exec';

const SERVICE_ACCOUNT_FILE = 'service_account.json';
const O2D_PLAN_DAYS        = 3;
const PORT                 = 5000;

const app = express();
app.use(cors());
app.use(express.json());


// ══════════════════════════════════════════════════════════════════════════════
// GOOGLE AUTH — JWT → access token  (replaces gspread / google-auth)
// ══════════════════════════════════════════════════════════════════════════════

/**
 * Load credentials from env vars (Vercel) or service_account.json (local).
 */
function loadCredentials() {
  if (process.env.GOOGLE_CLIENT_EMAIL && process.env.GOOGLE_PRIVATE_KEY) {
    return {
      client_email: process.env.GOOGLE_CLIENT_EMAIL,
      private_key:  process.env.GOOGLE_PRIVATE_KEY.replace(/\\n/g, '\n'),
    };
  }
  if (!fs.existsSync(SERVICE_ACCOUNT_FILE)) {
    throw new Error(
      `'${SERVICE_ACCOUNT_FILE}' not found and GOOGLE_CLIENT_EMAIL env var not set. ` +
      'Place service_account.json next to this file or set environment variables.'
    );
  }
  const sa = JSON.parse(fs.readFileSync(SERVICE_ACCOUNT_FILE, 'utf8'));
  return { client_email: sa.client_email, private_key: sa.private_key };
}

/**
 * Build a signed JWT and exchange it for a Google OAuth2 access token.
 * Uses Node's built-in crypto — no googleapis dependency needed.
 */
async function getAccessToken() {
  const { client_email, private_key } = loadCredentials();

  const now   = Math.floor(Date.now() / 1000);
  const claim = {
    iss:   client_email,
    scope: 'https://www.googleapis.com/auth/spreadsheets',
    aud:   'https://oauth2.googleapis.com/token',
    iat:   now,
    exp:   now + 3600,
  };

  // Build unsigned JWT
  const header  = Buffer.from(JSON.stringify({ alg: 'RS256', typ: 'JWT' })).toString('base64url');
  const payload = Buffer.from(JSON.stringify(claim)).toString('base64url');
  const unsigned = `${header}.${payload}`;

  // Sign with RS256 using Web Crypto (built into Node 18+)
  const keyPem = private_key
    .replace(/-----BEGIN PRIVATE KEY-----/g, '')
    .replace(/-----END PRIVATE KEY-----/g, '')
    .replace(/\s/g, '');

  const cryptoKey = await crypto.subtle.importKey(
    'pkcs8',
    Buffer.from(keyPem, 'base64'),
    { name: 'RSASSA-PKCS1-v1_5', hash: 'SHA-256' },
    false,
    ['sign']
  );

  const sigBuffer = await crypto.subtle.sign(
    'RSASSA-PKCS1-v1_5',
    cryptoKey,
    Buffer.from(unsigned)
  );

  const jwt = `${unsigned}.${Buffer.from(sigBuffer).toString('base64url')}`;

  // Exchange JWT for access token
  const res = await fetch('https://oauth2.googleapis.com/token', {
    method:  'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body:    new URLSearchParams({
      grant_type: 'urn:ietf:params:oauth:grant-type:jwt-bearer',
      assertion:  jwt,
    }),
  });

  const json = await res.json();
  if (!json.access_token) throw new Error('Failed to get access token: ' + JSON.stringify(json));
  return json.access_token;
}


// ══════════════════════════════════════════════════════════════════════════════
// LOW-LEVEL SHEET HELPERS  (replaces gspread calls)
// ══════════════════════════════════════════════════════════════════════════════

/**
 * Fetch all rows from a sheet tab as array-of-objects.
 * First row = headers (same as gspread get_all_records).
 */
async function fetchSheetAsRecords(spreadsheetId, sheetName) {
  const token = await getAccessToken();
  const url   = `https://sheets.googleapis.com/v4/spreadsheets/${spreadsheetId}/values/${encodeURIComponent(sheetName)}`;
  const res   = await fetch(url, { headers: { Authorization: `Bearer ${token}` } });
  if (!res.ok) throw new Error(`Sheets API error ${res.status}: ${await res.text()}`);
  const { values = [] } = await res.json();
  if (values.length < 2) return [];
  const [headers, ...rows] = values;
  return rows.map(row =>
    Object.fromEntries(headers.map((h, i) => [String(h).trim(), row[i] ?? '']))
  );
}

/**
 * Append one row to a sheet tab (same as gspread append_row).
 */
async function appendRowToSheet(spreadsheetId, sheetName, rowData) {
  const token = await getAccessToken();
  const url   = `https://sheets.googleapis.com/v4/spreadsheets/${spreadsheetId}/values/` +
                `${encodeURIComponent(sheetName)}:append?valueInputOption=USER_ENTERED&insertDataOption=INSERT_ROWS`;
  const res   = await fetch(url, {
    method:  'POST',
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    body:    JSON.stringify({ values: [rowData] }),
  });
  if (!res.ok) throw new Error(`Sheets append error ${res.status}: ${await res.text()}`);
}

/** Safe float conversion — same as Python _to_float() */
function toFloat(value) {
  const n = parseFloat(String(value ?? '').replace(/,/g, '').trim());
  return isNaN(n) ? 0.0 : n;
}

/**
 * Shared handler for all POST /api/append/* routes.
 * Equivalent to Python _append_endpoint().
 */
async function appendEndpoint(req, res, sheetId, sheetName = 'Sheet1') {
  const row = (req.body || {}).row || [];
  if (!row.length) return res.status(400).json({ ok: false, error: 'No row data provided' });
  try {
    await appendRowToSheet(sheetId, sheetName, row);
    res.json({ ok: true });
  } catch (err) {
    res.status(500).json({ ok: false, error: err.message });
  }
}


// ══════════════════════════════════════════════════════════════════════════════
// DATA FETCH FUNCTIONS  (1-to-1 with Python fetch_* functions)
// ══════════════════════════════════════════════════════════════════════════════

/** Tab 'order' in main spreadsheet. */
async function fetchSalesOrders() {
  return fetchSheetAsRecords(SPREADSHEET_ID, 'order');
}

/** Tab 'pending sales' in main spreadsheet. */
async function fetchPendingOrders() {
  return fetchSheetAsRecords(SPREADSHEET_ID, 'pending sales');
}

/**
 * Tab 'Dispatch' in main spreadsheet.
 * Columns: Date of Dispatch, Party Name, PO Number, SO No, Invoice No,
 *          Item Name, Item Description, BatchName, Qty, Rate, Amount, Total
 */
async function fetchDispatchOrders() {
  return fetchSheetAsRecords(SPREADSHEET_ID, 'Dispatch');
}

/**
 * Tab 'DATA' in 'New Dispatch fms DEC 2025'.
 * Normalises column names for the frontend.
 */
async function fetchDispatchFms() {
  const records = await fetchSheetAsRecords(DISPATCH_FMS_SOURCE_ID, 'DATA');
  return records.map(r => ({
    'Timestamp':    r['Timestamp']                              || '',
    'Date':         r['Date of Dispatch'] || r['Date']         || '',
    'Party Name':   r['Party Name']                            || '',
    'PO':           r['PO']                                    || '',
    'SO':           r['SO']                                    || '',
    'Invoice No':   r['Invoice No']                            || '',
    'Item Name':    r['Item Name']                             || '',
    'Qty':          toFloat(r['Qty']),
    'Machine No':   r['Machine no']                            || '',
    'Product Name': r['PRODUCT NAME'] || r['Product Name']     || '',
    'WA Status':    r['WA Status']                             || '',
  }));
}

/** Tab 'Stock' in main spreadsheet. */
async function fetchStockRegister() {
  return fetchSheetAsRecords(SPREADSHEET_ID, 'Stock');
}

/** Tab 'Production Requirement' in main spreadsheet. */
async function fetchProductionRequirements() {
  return fetchSheetAsRecords(SPREADSHEET_ID, 'Production Requirement');
}

/**
 * Tab 'o2d' in FMS_SHEET_ID.
 * Filters Payment Terms = ADVANCE and groups rows into order-level records
 * with a nested 'items' array. Exact equivalent of Python fetch_fms_advance_orders().
 */
async function fetchFmsAdvanceOrders() {
  const raw       = await fetchSheetAsRecords(FMS_SHEET_ID, 'o2d');
  const ordersMap = {};

  for (const row of raw) {
    if (String(row['Payment Terms'] || '').trim().toUpperCase() !== 'ADVANCE') continue;
    const soNo = String(row['SO No'] || '').trim();
    if (!soNo) continue;

    if (!ordersMap[soNo]) {
      ordersMap[soNo] = {
        'SO No':         row['SO No']      || '',
        'Date':          row['Date']        || '',
        'Client Name':   row['Client Name'] || '',
        'Payment Terms': 'ADVANCE',
        'PO Number':     row['PO Number']   || '',
        'Total Qty':     0,
        'Amount':        0.0,
        'Total Bill':    0.0,
        'Items':         0,
        'CRM Status':    'Pending Call',
        'items':         [],
      };
    }

    const qty    = toFloat(row['Qty']);
    const amount = toFloat(row['Amount']);
    const total  = toFloat(row['Total']);

    ordersMap[soNo]['Total Qty']  += qty;
    ordersMap[soNo]['Amount']     += amount;
    ordersMap[soNo]['Total Bill'] += total;
    ordersMap[soNo]['Items']      += 1;
    ordersMap[soNo]['items'].push(row);
  }

  return Object.values(ordersMap);
}

/**
 * Tab 'Sheet1' in O2D_SOURCE_SHEET_ID.
 * Computes Plan_Date = SO_Date + O2D_PLAN_DAYS.
 * Exact equivalent of Python fetch_o2d_pipeline().
 */
async function fetchO2dPipeline() {
  const raw     = await fetchSheetAsRecords(O2D_SOURCE_SHEET_ID, 'Sheet1');
  const results = [];

  for (const row of raw) {
    // Normalise: replace spaces with underscores in keys
    const norm = Object.fromEntries(
      Object.entries(row).map(([k, v]) => [k.replace(/ /g, '_'), v])
    );

    const soDateStr = String(norm['SO_Date'] || '').trim();
    let planDateStr = '';
    if (soDateStr) {
      try {
        const soDate   = new Date(soDateStr);
        soDate.setDate(soDate.getDate() + O2D_PLAN_DAYS);
        planDateStr    = soDate.toISOString().slice(0, 10);  // YYYY-MM-DD
      } catch (_) {}
    }

    results.push({
      'Timestamp':   norm['Timestamp']   || '',
      'SO_No':       String(norm['SO_No']       || '').trim(),
      'Client_Name': String(norm['Client_Name'] || '').trim(),
      'Product':     String(norm['Product']     || '').trim(),
      'Qty':         toFloat(norm['Qty']),
      'SO_Date':     soDateStr,
      'Plan_Date':   planDateStr,
      'Step':        String(norm['Step'] || 'Product Planning').trim(),
      'Agent_Name':  String(norm['Agent_Name']  || '').trim(),
      'Notes':       String(norm['Notes']       || '').trim(),
    });
  }

  return results;
}


// ══════════════════════════════════════════════════════════════════════════════
// DASHBOARD METRICS  (same as Python compute_dashboard_metrics)
// ══════════════════════════════════════════════════════════════════════════════

function computeDashboardMetrics(orders, pending, dispatch, stock, production, fms) {
  const pendingCustomers = new Set(
    pending.map(r => String(r['Company Name'] || '').trim()).filter(Boolean)
  ).size;

  const now = new Date();
  const lastUpdated = now.toLocaleDateString('en-IN', {
    day: '2-digit', month: 'short', year: 'numeric',
  }) + ' ' + now.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', hour12: false });

  return {
    order_lines:        orders.length,
    total_qty_ordered:  orders.reduce((s, r) => s + toFloat(r['Qty']), 0),
    pending_lines:      pending.length,
    pending_bags:       pending.reduce((s, r) => s + toFloat(r['Pending Qty']), 0),
    pending_customers:  pendingCustomers,
    dispatched_lines:   dispatch.length,
    dispatched_bags:    dispatch.reduce((s, r) => s + toFloat(r['Qty']), 0),
    production_lines:   production.length,
    production_bags:    production.reduce((s, r) => s + toFloat(r['Qty'] || r['Pending Qty']), 0),
    stock_items:        stock.length,
    fms_advance_count:  fms.length,
    fms_advance_value:  fms.reduce((s, r) => s + toFloat(r['Total Bill']), 0),
    last_updated:       lastUpdated,
  };
}


// ══════════════════════════════════════════════════════════════════════════════
// FLASK → EXPRESS  READ ENDPOINTS
// ══════════════════════════════════════════════════════════════════════════════

/** GET /api/erp-data — master endpoint, all data in one call */
app.get('/api/erp-data', async (req, res) => {
  try {
    const [orders, pending, dispatch, dispfms, stock, production, fms, o2d] =
      await Promise.all([
        fetchSalesOrders(),
        fetchPendingOrders(),
        fetchDispatchOrders(),
        fetchDispatchFms(),
        fetchStockRegister(),
        fetchProductionRequirements(),
        fetchFmsAdvanceOrders(),
        fetchO2dPipeline(),
      ]);

    const metrics = computeDashboardMetrics(orders, pending, dispatch, stock, production, fms);

    res.json({
      ok:         true,
      metrics,
      orders,
      pending,
      dispatch,
      dispfms,       // ← Dispatch FMS source
      stock,
      production,
      fms,
      o2d,
    });
  } catch (err) {
    res.status(500).json({ ok: false, error: err.message });
  }
});

app.get('/api/orders',     async (req, res) => {
  try { res.json(await fetchSalesOrders());           } catch (e) { res.status(500).json({ ok: false, error: e.message }); }
});

app.get('/api/pending',    async (req, res) => {
  try { res.json(await fetchPendingOrders());         } catch (e) { res.status(500).json({ ok: false, error: e.message }); }
});

app.get('/api/dispatch',   async (req, res) => {
  try { res.json(await fetchDispatchOrders());        } catch (e) { res.status(500).json({ ok: false, error: e.message }); }
});

app.get('/api/dispfms',    async (req, res) => {
  try { res.json(await fetchDispatchFms());           } catch (e) { res.status(500).json({ ok: false, error: e.message }); }
});

app.get('/api/stock',      async (req, res) => {
  try { res.json(await fetchStockRegister());         } catch (e) { res.status(500).json({ ok: false, error: e.message }); }
});

app.get('/api/production', async (req, res) => {
  try { res.json(await fetchProductionRequirements()); } catch (e) { res.status(500).json({ ok: false, error: e.message }); }
});

app.get('/api/fms',        async (req, res) => {
  try { res.json(await fetchFmsAdvanceOrders());      } catch (e) { res.status(500).json({ ok: false, error: e.message }); }
});

app.get('/api/o2d',        async (req, res) => {
  try { res.json(await fetchO2dPipeline());           } catch (e) { res.status(500).json({ ok: false, error: e.message }); }
});

app.get('/api/health', (req, res) => {
  res.json({ ok: true, service: 'TPPL ERP Sheets Fetcher (JS)', time: new Date().toISOString() });
});


// ══════════════════════════════════════════════════════════════════════════════
// FLASK → EXPRESS  WRITE / APPEND ENDPOINTS
// ══════════════════════════════════════════════════════════════════════════════

// ── Collection FMS ────────────────────────────────────────────────────────────

/** POST /api/append/call-later — log Call Later from Collection FMS */
app.post('/api/append/call-later',     (req, res) => appendEndpoint(req, res, CALL_LATER_SHEET_ID));

/** POST /api/append/done — log Payment Done from Collection FMS */
app.post('/api/append/done',           (req, res) => appendEndpoint(req, res, DONE_SHEET_ID));

// ── O2D Pipeline ──────────────────────────────────────────────────────────────

/** POST /api/append/o2d-call-later */
app.post('/api/append/o2d-call-later', (req, res) => appendEndpoint(req, res, O2D_CALL_LATER_ID));

/** POST /api/append/o2d-done */
app.post('/api/append/o2d-done',       (req, res) => appendEndpoint(req, res, O2D_DONE_SHEET_ID));

// ── Dispatch FMS ──────────────────────────────────────────────────────────────

/**
 * POST /api/append/dispatch-hold
 * Body: { row: [logged_at, dispatch_date, party_name, invoice_no, item, qty, "HOLD", remark] }
 * Writes to Dispatch FMS Hold log sheet → Sheet1
 * Headers: Logged At | Dispatch Date | Party Name | Invoice No | Item | Qty | Status | Remark
 */
app.post('/api/append/dispatch-hold',  (req, res) => appendEndpoint(req, res, DISPATCH_FMS_HOLD_SHEET_ID));

/**
 * POST /api/append/dispatch-done
 * Body: { row: [logged_at, dispatch_date, party_name, invoice_no, item, qty, "DONE"] }
 * Writes to Dispatch FMS Done log sheet → Sheet1
 * Headers: Logged At | Dispatch Date | Party Name | Invoice No | Item | Qty | Status
 */
app.post('/api/append/dispatch-done',  (req, res) => appendEndpoint(req, res, DISPATCH_FMS_DONE_SHEET_ID));

// ── Rate Checklist (Apps Script proxy) ───────────────────────────────────────

/** POST /api/append/rate-checklist — forward to Google Apps Script web app */
app.post('/api/append/rate-checklist', async (req, res) => {
  const row = (req.body || {}).row || [];
  try {
    const response = await fetch(RATE_CL_SHEET_URL, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ action: 'rate_checklist', data: row }),
    });
    if (!response.ok) throw new Error(`Apps Script returned ${response.status}`);
    res.json({ ok: true });
  } catch (err) {
    res.status(500).json({ ok: false, error: err.message });
  }
});


// ══════════════════════════════════════════════════════════════════════════════
// CLI — CONNECTIVITY TEST  (equivalent of Python print_summary / --test flag)
// ══════════════════════════════════════════════════════════════════════════════

async function printSummary() {
  console.log('── TPPL ERP Google Sheets Connectivity Test ──');
  const checks = [
    ['Sales Orders          (order tab)',               fetchSalesOrders],
    ['Pending Orders        (pending sales tab)',       fetchPendingOrders],
    ['Dispatch              (Dispatch tab)',            fetchDispatchOrders],
    ['Dispatch FMS source   (New Dispatch fms sheet)',  fetchDispatchFms],
    ['Stock Register        (Stock tab)',               fetchStockRegister],
    ['Production Req.       (Production Req. tab)',     fetchProductionRequirements],
    ['FMS Advance Orders    (o2d tab, ADVANCE filter)', fetchFmsAdvanceOrders],
    ['O2D Pipeline          (Sheet1)',                  fetchO2dPipeline],
  ];
  for (const [name, fn] of checks) {
    try {
      const rows = await fn();
      console.log(`  ✅  ${name}: ${rows.length} rows`);
    } catch (err) {
      console.log(`  ❌  ${name}: ${err.message}`);
    }
  }
  console.log('── Done ──');
}


// ══════════════════════════════════════════════════════════════════════════════
// START
// ══════════════════════════════════════════════════════════════════════════════

if (process.argv.includes('--test')) {
  printSummary().then(() => process.exit(0));
} else {
  app.listen(PORT, '0.0.0.0', () => {
    console.log(`🚀  TPPL ERP Sheets API → http://localhost:${PORT}`);
    console.log('');
    console.log('  READ:');
    console.log('    GET  /api/erp-data              → All ERP data (single call)');
    console.log('    GET  /api/orders                → Sales orders');
    console.log('    GET  /api/pending               → Pending orders');
    console.log('    GET  /api/dispatch              → Dispatch register');
    console.log('    GET  /api/dispfms               → Dispatch FMS source');
    console.log('    GET  /api/stock                 → Stock register');
    console.log('    GET  /api/production            → Production requirements');
    console.log('    GET  /api/fms                   → Collection FMS advance orders');
    console.log('    GET  /api/o2d                   → O2D pipeline');
    console.log('    GET  /api/health                → Health check');
    console.log('');
    console.log('  WRITE:');
    console.log('    POST /api/append/call-later     → Collection FMS: call-later log');
    console.log('    POST /api/append/done           → Collection FMS: done log');
    console.log('    POST /api/append/o2d-call-later → O2D: call-later log');
    console.log('    POST /api/append/o2d-done       → O2D: done log');
    console.log('    POST /api/append/dispatch-hold  → Dispatch FMS: hold log');
    console.log('    POST /api/append/dispatch-done  → Dispatch FMS: done log');
    console.log('    POST /api/append/rate-checklist → Rate checklist (Apps Script)');
    console.log('');
    console.log('  Tip: node tppl_sheets_fetcher.js --test  to check all connections first.');
  });
}
