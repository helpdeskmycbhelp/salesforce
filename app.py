from flask import Flask, render_template, jsonify, request
import os, time, requests
from dotenv import load_dotenv

# Rate limiting
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "change_me")
PORT = int(os.getenv("PORT", "5000"))

# ---------------- Security headers ----------------
CSP = (
    "default-src 'self' https: data:; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "img-src 'self' data: https:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)

@app.after_request
def add_security_headers(resp):
    resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains; preload"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer-when-downgrade"
    resp.headers["Content-Security-Policy"] = CSP
    return resp

# ---------------- Rate limiter ----------------
RATE_LIMIT = os.getenv("RATE_LIMIT", "60 per minute")
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[RATE_LIMIT],
    storage_uri=os.getenv("LIMITER_STORAGE_URI", "memory://"),  # use Redis in multi-instance
)

@app.errorhandler(429)
def ratelimit_handler(e):
    return jsonify(ok=False, error="Rate limit exceeded. Try again soon."), 429

# ---------------- Salesforce creds ----------------
SF_LOGIN_URL     = os.getenv("SF_LOGIN_URL", "https://test.salesforce.com").rstrip("/")
SF_CLIENT_ID     = os.getenv("SF_CLIENT_ID", "")
SF_CLIENT_SECRET = os.getenv("SF_CLIENT_SECRET", "")
SF_REFRESH_TOKEN = os.getenv("SF_REFRESH_TOKEN", "")
SF_INSTANCE_URL  = (os.getenv("SF_INSTANCE_URL") or "").strip()

# Access token cache (single-process)
_token = {"access_token": None, "instance_url": SF_INSTANCE_URL or None, "issued_at": 0}

# Simple in-memory cache (use Redis for multi-instance)
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "60"))
_cache = {}  # key -> (expires_at, value)

def cache_get(key: str):
    rec = _cache.get(key)
    if not rec:
        return None
    exp, val = rec
    if time.time() > exp:
        _cache.pop(key, None)
        return None
    return val

def cache_set(key: str, value, ttl: int = CACHE_TTL_SECONDS):
    _cache[key] = (time.time() + ttl, value)

def have_creds():
    missing = [k for k, v in {
        "SF_CLIENT_ID": SF_CLIENT_ID,
        "SF_CLIENT_SECRET": SF_CLIENT_SECRET,
        "SF_REFRESH_TOKEN": SF_REFRESH_TOKEN,
    }.items() if not v]
    if missing:
        raise RuntimeError(f"Missing in environment: {', '.join(missing)}")

def refresh_access_token():
    """Use the long-lived refresh token to get a fresh access token."""
    have_creds()
    token_url = f"{SF_LOGIN_URL}/services/oauth2/token"
    data = {
        "grant_type": "refresh_token",
        "refresh_token": SF_REFRESH_TOKEN,
        "client_id": SF_CLIENT_ID,
        "client_secret": SF_CLIENT_SECRET,
    }
    r = requests.post(token_url, data=data, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Refresh failed: {r.status_code} {r.text}")
    j = r.json()
    _token["access_token"] = j.get("access_token")
    if j.get("instance_url"):
        _token["instance_url"] = j["instance_url"]
    _token["issued_at"] = int(time.time())

def sf_get(path: str, params=None):
    """GET wrapper with 401 retry once after refresh."""
    if not _token["access_token"]:
        refresh_access_token()
    base = _token["instance_url"]
    if not base:
        refresh_access_token()
        base = _token["instance_url"]
    url = path if path.startswith("http") else f"{base.rstrip('/')}/{path.lstrip('/')}"
    headers = {"Authorization": f"Bearer {_token['access_token']}"}
    r = requests.get(url, params=params, headers=headers, timeout=30)
    if r.status_code == 401:
        refresh_access_token()
        headers = {"Authorization": f"Bearer {_token['access_token']}"}
        r = requests.get(url, params=params, headers=headers, timeout=30)
    return r

# ---------------- Unit Type mapping (API code -> label) ----------------
UNIT_TYPE_MAP = {
    "AP": "Apartment",
    "BU": "Bulk Units",
    "BW": "Bungalow",
    "CD": "Compound",
    "DX": "Duplex",
    "FF": "Full Floor",
    "HF": "Half Floor",
    "HA": "Hotel & Hotel Apartment",
    "PH": "Penthouse",
    "TH": "Townhouse",
    "BC": "Business Center",
    "CW": "Co-working space",
    "FA": "Factory",
    "FM": "Farm",
    "LC": "Labor Camp",
}

# ---------------- Routes ----------------
@app.route("/")
@limiter.exempt
def index():
    return render_template("index.html")

@app.get("/api/units")
@limiter.limit(RATE_LIMIT)
def api_units():
    """Public endpoint. Returns Unit__c rows (cached) and includes Unit_Type_Label."""
    force_refresh = request.args.get("refresh") == "1"
    CACHE_KEY = "units:list:v2"  # bump key when changing payload structure

    if not force_refresh:
        cached = cache_get(CACHE_KEY)
        if cached:
            return jsonify(cached), 200

    # SOQL – keep clean; no comments
    soql = """
      SELECT Id, Name, Reference_Number__c, RecordType.Name,
             Unit_Type__c, Beds__c, Floor__c, Unit_No__c,
             Built_up_Area__c, Status__c, Price__c, Community__c,
             Building__r.Name, LastModifiedDate
      FROM Unit__c
      ORDER BY LastModifiedDate DESC
      LIMIT 200
    """.strip().replace("\n", " ").replace("  ", " ")

    r = sf_get("/services/data/v61.0/query", params={"q": soql})
    if r.status_code != 200:
        fallback = cache_get(CACHE_KEY)
        if fallback:
            return jsonify(fallback), 200
        return jsonify(ok=False, error=r.text), r.status_code

    data = r.json()
    records = data.get("records", [])

    # Map Unit_Type__c codes to human labels
    for rec in records:
        code = rec.get("Unit_Type__c")
        rec["Unit_Type_Label"] = UNIT_TYPE_MAP.get(code, code)

    payload = {
        "ok": True,
        "fromCache": False,
        "cacheTtl": CACHE_TTL_SECONDS,
        "totalSize": data.get("totalSize", 0),
        "records": records,
    }
    cache_set(CACHE_KEY, {**payload, "fromCache": True})
    return jsonify(payload), 200

@app.get("/api/units/describe")
@limiter.limit(RATE_LIMIT)
def api_units_describe():
    """Helpful metadata (cached ~5 min)."""
    force_refresh = request.args.get("refresh") == "1"
    CACHE_KEY = "units:describe:v1"

    if not force_refresh:
        cached = cache_get(CACHE_KEY)
        if cached:
            return jsonify(cached), 200

    r = sf_get("/services/data/v61.0/sobjects/Unit__c/describe")
    if r.status_code != 200:
        fallback = cache_get(CACHE_KEY)
        if fallback:
            return jsonify(fallback), 200
        return jsonify(ok=False, error=r.text), r.status_code

    d = r.json()
    fields = [{"name": f["name"], "label": f["label"], "type": f["type"]} for f in d.get("fields", [])]
    ttl = max(CACHE_TTL_SECONDS, 300)
    payload = {"ok": True, "fromCache": False, "cacheTtl": ttl, "fields": fields}
    cache_set(CACHE_KEY, {**payload, "fromCache": True}, ttl=ttl)
    return jsonify(payload), 200

@app.get("/healthz")
@limiter.exempt
def healthz():
    return "ok", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
