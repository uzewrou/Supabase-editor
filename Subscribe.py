"""
Subscribe page — Supabase-owned OAuth (Google + Microsoft, any account).
PKCE code flow: Supabase returns ?code=..., exchanged for a session.
Verified email drives subscriptions. Cap 60/email.

Login: only the clicked provider generates a PKCE verifier, so the two
providers no longer clobber each other's verifier in the shared client.

Run:  streamlit run subscribe.py
Deps: streamlit, requests, supabase
Secrets: SUPABASE_URL, SUPABASE_KEY
"""
import csv
import requests
import streamlit as st
from supabase import create_client

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

SUPABASE_URL = st.secrets["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = st.secrets["SUPABASE_KEY"]
SB_HEADERS = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
APP_URL = "https://supabase-editor-xb3xzprepaltfjzplqo7ej.streamlit.app"
MAX_PER_EMAIL = 60


@st.cache_resource
def get_supabase():
    # One shared client so the PKCE code_verifier persists between the
    # login redirect and the code exchange.
    return create_client(SUPABASE_URL, SUPABASE_KEY)


supabase = get_supabase()

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


# ==================== AUTH ====================
def start_login(provider):
    res = supabase.auth.sign_in_with_oauth({
        "provider": provider,
        "options": {"redirect_to": APP_URL},
    })
    st.markdown(
        f'<script>window.top.location.href = "{res.url}";</script>',
        unsafe_allow_html=True,
    )
    st.stop()


def logged_in_email():
    try:
        session = supabase.auth.get_session()
        if session and session.user:
            return (session.user.email or "").strip().lower()
    except Exception:
        pass
    return None


st.title("Subscribe to filing alerts")

# --- Handle the ?code=... redirect from Supabase (PKCE flow) ---
qp = st.query_params
if "code" in qp:
    try:
        supabase.auth.exchange_code_for_session({"auth_code": qp["code"]})
        st.query_params.clear()
        st.rerun()
    except Exception as e:
        st.error(f"Login failed during code exchange: {e}")
        st.query_params.clear()

email = logged_in_email()

# --- Not logged in -> buttons ---
if not email:
    st.write("Sign in to manage your filing-alert subscriptions.")
    col1, col2 = st.columns(2)
    if col1.button("Sign in with Microsoft", type="primary"):
        start_login("azure")
    if col2.button("Sign in with Google"):
        start_login("google")
    st.stop()

# --- Logged in ---
c1, c2 = st.columns([4, 1])
c1.caption(f"Signed in as **{email}**")
if c2.button("Log out"):
    try:
        supabase.auth.sign_out()
    except Exception:
        pass
    st.rerun()

# ==================== SUBSCRIBE UI ====================
companies = matched_companies()
by_label = {f"{c['symbol']} — {c['name']}": c for c in companies}

current = get_subscriptions(email)
n = len(current)
st.subheader(f"Your subscriptions — {n} / {MAX_PER_EMAIL}")
if current:
    st.dataframe(current, width="stretch", hide_index=True)
else:
    st.caption("No subscriptions yet.")

remaining = MAX_PER_EMAIL - n
picks = st.multiselect("Add companies", list(by_label),
                       placeholder="Type a name or ticker…")

over = len(picks) > remaining
if over:
    st.warning(f"You have {remaining} slot(s) left but picked {len(picks)}. "
               f"Remove {len(picks) - remaining} to stay under {MAX_PER_EMAIL}.")

if st.button("Subscribe", disabled=not picks or over, type="primary"):
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
    st.rerun()

st.caption(f"{len(companies)} companies available (listed on both BSE & NSE).")
