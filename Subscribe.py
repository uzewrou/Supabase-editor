"""
Subscribe page (TEST BUILD — no auth, RLS off, keep local/private).
Enter an email -> auto-loads that person's current subscriptions from Supabase
into a table with a count. Pick companies from the live matched BSE+NSE list and
Subscribe -> writes rows into `subscriptions`. Cap: 60 companies per email.
DB unique constraint on (email, company_key) rejects duplicates.

Run:  streamlit run subscribe.py
Deps: pip install streamlit requests
Secrets (.streamlit/secrets.toml or Streamlit Cloud secrets):
    SUPABASE_URL = "https://xxxx.supabase.co"
    SUPABASE_KEY = "sb_publishable_..."
"""
import csv
import requests
import streamlit as st

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

SUPABASE_URL = st.secrets["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = st.secrets["SUPABASE_KEY"]
SB_HEADERS = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
MAX_PER_EMAIL = 3

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


def get_subscriptions(email):
    """Return list of this email's rows from Supabase (empty list on none/error)."""
    url = f"{SUPABASE_URL}/rest/v1/subscriptions"
    params = {"select": "company_key,bse_code,nse_symbol",
              "email": f"eq.{email}", "order": "company_key.asc"}
    try:
        r = requests.get(url, headers=SB_HEADERS, params=params, timeout=20)
        r.raise_for_status()
        rows = r.json()
        return rows if isinstance(rows, list) else []
    except Exception as e:
        st.error(f"Could not load subscriptions: {e}")
        return []


def insert_subscription(email, c):
    """Insert one row. Returns 'added', 'duplicate', or an error string."""
    url = f"{SUPABASE_URL}/rest/v1/subscriptions"
    headers = {**SB_HEADERS, "Content-Type": "application/json", "Prefer": "return=minimal"}
    payload = {"email": email, "company_key": c["symbol"],
               "bse_code": c["bse_code"], "nse_symbol": c["nse_symbol"]}
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=20)
        if r.status_code in (200, 201, 204):
            return "added"
        if r.status_code == 409:
            return "duplicate"
        return f"error {r.status_code}: {r.text[:120]}"
    except Exception as e:
        return f"error: {e}"


st.title("Subscribe to filing alerts")
st.caption("Test build — no login yet.")

companies = matched_companies()
by_label = {f"{c['symbol']} — {c['name']}": c for c in companies}

email = st.text_input("Email", placeholder="name@ashikagroup.com").strip()

if email:
    current = get_subscriptions(email)
    n = len(current)
    st.subheader(f"Current subscriptions — {n} / {MAX_PER_EMAIL}")
    if current:
        st.dataframe(current, use_container_width=True, hide_index=True)
    else:
        st.caption("No subscriptions yet for this email.")

    remaining = MAX_PER_EMAIL - n
    picks = st.multiselect("Add companies", list(by_label),
                           placeholder="Type a name or ticker…")

    over = len(picks) > remaining
    if over:
        st.warning(f"You have {remaining} slot(s) left but picked {len(picks)}. "
                   f"Remove {len(picks) - remaining} to stay under {MAX_PER_EMAIL}.")

    if st.button("Subscribe", disabled=not picks or over):
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
        st.rerun()   # refresh the table + count

st.caption(f"{len(companies)} companies available (listed on both BSE & NSE).")
