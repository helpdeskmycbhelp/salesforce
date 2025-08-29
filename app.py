from flask import Flask, render_template, jsonify, request
import os, time, requests
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "change_me")
PORT = int(os.getenv("PORT", "5000"))

# ---- Salesforce integration creds (server-only; never sent to browser)
SF_LOGIN_URL   = os.getenv("SF_LOGIN_URL", "https://test.salesforce.com").rstrip("/")
SF_CLIENT_ID   = os.getenv("SF_CLIENT_ID", "")
SF_CLIENT_SECRET = os.getenv("SF_CLIENT_SECRET", "")
SF_REFRESH_TOKEN = os.getenv("SF_REFRESH_TOKEN", "")
SF_INSTANCE_URL  = (os.getenv("SF_INSTANCE_URL") or "").strip()

# ---- In-memory token cache (for single-process dev). Use Redis for multi-instance.
_token = {
    "access_token": None,
    "instance_url": SF_INSTANCE_URL or None,
    "issued_at": 0,
}

# ---- Simple TTL cache (in-memory). Use Redis in production for multi-instance.
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "60"))
_cache = {}  # key -> (expires_at, payload)

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
        raise RuntimeError(f"Missing in .env: {', '.join(missing)}")

def refresh_access_token():
    """Exchange long-lived refresh token for a new access token."""
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
    """GET wrapper. If 401, refresh and retry once."""
    if not _token["access_token"]:
        refresh_access_token()
    base = _token["instance_url"]
    if not base:
        # fetch a token to populate instance_url
        refresh_access_token()
        base = _token["instance_url"]
    url = path if path.startswith("http") else f"{base.rstrip('/')}/{path.lstrip('/')}"
    headers = {"Authorization": f"Bearer {_token['access_token']}"}
    r = requests.get(url, params=params, headers=headers, timeout=30)
    if r.status_code == 401:
        # token expired or revoked -> refresh and retry once
        refresh_access_token()
        headers = {"Authorization": f"Bearer {_token['access_token']}"}
        r = requests.get(url, params=params, headers=headers, timeout=30)
    return r

@app.route("/")
def index():
    return render_template("index.html")

@app.get("/api/units")
def api_units():
    """Public endpoint: returns Unit__c list (cached)."""
    force_refresh = request.args.get("refresh") == "1"
    CACHE_KEY = "units:list:v1"

    if not force_refresh:
        cached = cache_get(CACHE_KEY)
        if cached:
            return jsonify(cached), 200

    soql = """
        SELECT Id, Name,Reference_Number__c,RecordType.Name,
          Unit_Type__c,Beds__c, Floor__c,Unit_No__c,
          Built_up_Area__c,Status__c, Price__c,
          Community__c,Building__r.Name
        FROM Unit__c
        ORDER BY LastModifiedDate DESC
        LIMIT 100
    """.strip().replace("\n", " ").replace("  ", " ")



    r = sf_get("/services/data/v61.0/query", params={"q": soql})
    if r.status_code != 200:
        # serve last good data if available
        fallback = cache_get(CACHE_KEY)
        if fallback:
            return jsonify(fallback), 200
        return jsonify(ok=False, error=r.text), r.status_code

    data = r.json()
    payload = {
        "ok": True,
        "fromCache": False,
        "cacheTtl": CACHE_TTL_SECONDS,
        "totalSize": data.get("totalSize", 0),
        "records": data.get("records", []),
    }
    # store a copy marked as fromCache for subsequent hits
    cache_set(CACHE_KEY, {**payload, "fromCache": True})
    return jsonify(payload), 200

@app.get("/api/units/describe")
def api_units_describe():
    """Public endpoint: returns Unit__c metadata (cached longer)."""
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
    payload = {"ok": True, "fromCache": False, "cacheTtl": max(CACHE_TTL_SECONDS, 300), "fields": fields}
    cache_set(CACHE_KEY, {**payload, "fromCache": True}, ttl=max(CACHE_TTL_SECONDS, 300))
    return jsonify(payload), 200

@app.get("/healthz")
def healthz():
    return "ok", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
