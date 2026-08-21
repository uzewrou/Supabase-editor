"""
Subscribe page (TEST BUILD — no auth, RLS off, keep local).
Enter an email, pick companies from the live matched BSE+NSE list, write rows
into Supabase `subscriptions`. DB unique constraint on (email, company_key)
rejects duplicates; we report those separately.

Run:  streamlit run subscribe.py
Deps: pip install streamlit requests
Secrets (.streamlit/secrets.toml):
    SUPABASE_URL = "https://xxxx.supabase.co"
    SUPABASE_KEY = "your-key"
"""
import csv
import requests
import streamlit as st

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

SUPABASE_URL = st.secrets["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = st.secrets["SUPABASE_KEY"]

# ---------- BSE ----------
BSE_H = {"User-Agent": UA, "Accept": "application/json",
         "Referer": "https://www.bseindia.com/", "Origin": "https://www.bseindia.com"}
BSE_SCRIP = "https://api.bseindia.com/BseIndiaAPI/api/ListofScripData_new/w"

# ---------- NSE ----------
NSE = "https://www.nseindia.com"
NSE_CSV = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"


@st.cache_data(ttl=86400, show_spinner=False)
def bse_by_symbol():
    params = {"Group": "", "Scripcode": "", "segment": "Equity", "status": "Active", "scripName": ""}
    rows = requests.get(BSE_SCRIP, headers=BSE_H, params=params, timeout=25).json()
    out = {}
    for x in rows:
        sym = (x.get("scrip_id") or "").strip().upper()
        if sym:
            out[sym] = {"code": str(x["SCRIP_CD"]),
                        "name": x.get("Scrip_Name") or x.get("Issuer_Name") or ""}
    return out


@st.cache_data(ttl=86400, show_spinner=False)
def nse_by_symbol():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json",
                      "Accept-Language": "en-US,en;q=0.9"})
    for u in (NSE + "/get-quotes/equity?symbol=RELIANCE",
              NSE + "/market-data/securities-available-for-trading"):
        try:
            s.get(u, timeout=8)
        except Exception:
            pass
    r = s.get(NSE_CSV, timeout=25)
    out = {}
    for row in csv.DictReader(r.text.splitlines()):
        sym = (row.get("SYMBOL") or "").strip().upper()
        series = (row.get(" SERIES") or row.get("SERIES") or "").strip()
        name = (row.get("NAME OF COMPANY") or row.get(" NAME OF COMPANY") or "").strip()
        if sym and series == "EQ":
            out[sym] = name
    return out


@st.cache_data(ttl=86400, show_spinner="Loading company list…")
def matched_companies():
    bse, nse = bse_by_symbol(), nse_by_symbol()
    out = []
    for sym in set(bse) & set(nse):
        out.append({"symbol": sym, "name": bse[sym]["name"],
                    "bse_code": bse[sym]["code"], "nse_symbol": sym})
    out.sort(key=lambda c: c["name"].lower())
    return out


def insert_subscription(email, c):
    """Insert one row. Returns 'added', 'duplicate', or an error string."""
    url = f"{SUPABASE_URL}/rest/v1/subscriptions"
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
               "Content-Type": "application/json", "Prefer": "return=minimal"}
    payload = {"email": email, "company_key": c["symbol"],
               "bse_code": c["bse_code"], "nse_symbol": c["nse_symbol"]}
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=20)
        if r.status_code in (200, 201, 204):
            return "added"
        if r.status_code == 409:          # unique constraint -> already subscribed
            return "duplicate"
        return f"error {r.status_code}: {r.text[:120]}"
    except Exception as e:
        return f"error: {e}"


st.title("Subscribe to filing alerts")
st.caption("Test build — no login yet.")

companies = matched_companies()
by_label = {f"{c['symbol']} — {c['name']}": c for c in companies}

email = st.text_input("Email", placeholder="name@ashikagroup.com").strip()
picks = st.multiselect("Companies", list(by_label),
                       placeholder="Type a name or ticker…")

if st.button("Subscribe", disabled=not (email and picks)):
    added, dupes, errors = [], [], []
    for label in picks:
        c = by_label[label]
        res = insert_subscription(email, c)
        if res == "added":
            added.append(c["symbol"])
        elif res == "duplicate":
            dupes.append(c["symbol"])
        else:
            errors.append(f"{c['symbol']}: {res}")

    if added:
        st.success(f"Added: {', '.join(added)}")
    if dupes:
        st.info(f"Already subscribed (skipped): {', '.join(dupes)}")
    if errors:
        st.error("Errors:\n" + "\n".join(errors))

st.caption(f"{len(companies)} companies available (listed on both BSE & NSE).")
