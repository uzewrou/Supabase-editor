"""
Filing alerts — subscription page + NSE/BSE filings browser (single file).

After login, a toggle switches between:
  • My subscriptions — pick/drop companies for the alert bot (Supabase, RLS-scoped)
  • Browse filings   — live NSE/BSE quarterly results & investor presentations

Security: identity + access token live in st.session_state (per browser).
Reads/writes use the user's JWT so RLS enforces per-user rows.
This app uses ONLY the anon key. The service_role key stays in bot.py.

Run:  streamlit run subscribe.py
Deps: streamlit, requests
Secrets: SUPABASE_URL, SUPABASE_ANON_KEY
"""
import re
import csv
import html
import base64
import hashlib
import secrets
import datetime as dt
from concurrent.futures import ThreadPoolExecutor

import requests
import streamlit as st

st.set_page_config(page_title="Filing Sentinel", page_icon="📊", layout="wide")

# ============================================================ shared constants
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

SUPABASE_URL = st.secrets["SUPABASE_URL"].rstrip("/")
ANON = st.secrets["SUPABASE_ANON_KEY"]
APP_URL = "https://filings-sentinel-uom9qn5uniaerksz5cjv3v.streamlit.app/"
MAX_PER_EMAIL = 60

NSE = "https://www.nseindia.com"
NSE_CSV = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"

BSE_H = {"User-Agent": UA, "Accept": "application/json",
         "Referer": "https://www.bseindia.com/", "Origin": "https://www.bseindia.com"}
BSE_SCRIP = "https://api.bseindia.com/BseIndiaAPI/api/ListofScripData_new/w"

START_YEAR = 2016

# ============================================================ filings CSS
st.markdown("""
<style>
  .block-container {max-width: 1050px; padding-top: 3.5rem;}
  .title {font-size: 26px; font-weight: 700; letter-spacing: -.4px; margin: 0;}
  .sub {font-size: 13px; opacity: .55; margin: 2px 0 14px;}
  .cobar {display:flex; align-items:baseline; gap:12px; flex-wrap:wrap;
          border-left:3px solid #3d8bfd; background:rgba(37,99,235,.07);
          border-radius:6px; padding:10px 15px; margin:8px 0 4px;}
  .cobar b {font-size:18px; letter-spacing:-.2px;}
  .cobar span {font-size:12.5px; opacity:.6;}
  .qh {font-size:13px; font-weight:700; letter-spacing:.5px; color:#3d8bfd;
       margin:20px 0 6px; text-transform:uppercase;}
  .card {border:1px solid rgba(128,128,128,.2); border-radius:9px;
         padding:11px 14px; margin:7px 0; background:rgba(128,128,128,.04);}
  .card.top {border-color:rgba(38,166,91,.5); background:rgba(38,166,91,.08);}
  .card.ann {border-color:rgba(147,51,234,.4); background:rgba(147,51,234,.05);}
  .card .m {font-size:11.5px; opacity:.65; font-family:ui-monospace,monospace; margin-bottom:4px;}
  .card .h {font-size:13.5px; line-height:1.45;}
  .card .d {font-size:12px; opacity:.6; line-height:1.4; margin:3px 0 5px;}
  .card a {font-size:12.5px; font-weight:600; color:#3d8bfd; text-decoration:none;}
  .card a:hover {text-decoration:underline;}
  .pill {font-size:9px; font-weight:700; letter-spacing:.5px; padding:2px 7px;
         border-radius:20px; margin-left:6px; text-transform:uppercase;}
  .pill.t {background:rgba(38,166,91,.2); color:#34c47c;}
  .pill.a {background:rgba(147,51,234,.18); color:#c08ae8;}
  div[role="radiogroup"] {gap:6px;}
</style>
""", unsafe_allow_html=True)


def qsort_key(q):
    if q == "Other":
        return (-1, 0)
    m = re.match(r"FY(\d+)\s+Q(\d)", q)
    return (int(m.group(1)), int(m.group(2)))


# ==================== SUBSCRIPTIONS: company matching ====================
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


# ---------- per-user auth header (RLS enforced by user's JWT) ----------
def user_headers():
    return {"apikey": ANON, "Authorization": f"Bearer {st.session_state['token']}"}


def get_subscriptions(email):
    url = f"{SUPABASE_URL}/rest/v1/subscriptions"
    params = {"select": "company_key,bse_code,nse_symbol",
              "email": f"eq.{email}", "order": "company_key.asc"}
    try:
        r = requests.get(url, headers=user_headers(), params=params, timeout=20)
        r.raise_for_status()
        rows = r.json()
        return rows if isinstance(rows, list) else []
    except Exception as e:
        st.error(f"Could not load subscriptions: {e}")
        return []


def insert_subscription(email, c):
    url = f"{SUPABASE_URL}/rest/v1/subscriptions"
    headers = {**user_headers(), "Content-Type": "application/json", "Prefer": "return=minimal"}
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


def delete_subscription(email, company_key):
    url = f"{SUPABASE_URL}/rest/v1/subscriptions"
    params = {"email": f"eq.{email}", "company_key": f"eq.{company_key}"}
    try:
        r = requests.delete(url, headers=user_headers(), params=params, timeout=20)
        return "deleted" if r.status_code in (200, 204) else f"error {r.status_code}: {r.text[:120]}"
    except Exception as e:
        return f"error: {e}"


# ==================== AUTH (manual PKCE, per-session identity) ====================
@st.cache_resource
def _pkce_store():
    return {}


def _pkce_pair():
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    return verifier, challenge


def oauth_url(provider):
    verifier, challenge = _pkce_pair()
    _pkce_store()[provider] = verifier
    return (f"{SUPABASE_URL}/auth/v1/authorize"
            f"?provider={provider}&redirect_to={APP_URL}"
            f"&code_challenge={challenge}&code_challenge_method=s256")


def exchange_code(code):
    store = _pkce_store()
    for provider, verifier in list(store.items()):
        try:
            r = requests.post(
                f"{SUPABASE_URL}/auth/v1/token?grant_type=pkce",
                headers={"apikey": ANON, "Content-Type": "application/json"},
                json={"auth_code": code, "code_verifier": verifier},
                timeout=20,
            )
        except Exception:
            continue
        if r.status_code == 200:
            store.pop(provider, None)
            return r.json()
    return None


# ==================== SUBSCRIPTIONS VIEW ====================
def subscriptions_run(email):
    companies = matched_companies()
    by_label = {f"{c['symbol']} — {c['name']}": c for c in companies}

    current = get_subscriptions(email)
    n = len(current)
    st.subheader(f"Your subscriptions — {n} / {MAX_PER_EMAIL}")
    if current:
        name_by_key = {c["symbol"]: c["name"] for c in companies}
        h = st.columns([3, 4, 2, 2, 1])
        h[0].caption("**Ticker**")
        h[1].caption("**Company**")
        h[2].caption("**BSE**")
        h[3].caption("**NSE**")
        h[4].caption("")
        for row in current:
            c = st.columns([3, 4, 2, 2, 1])
            c[0].write(row["company_key"])
            c[1].write(name_by_key.get(row["company_key"], "—"))
            c[2].write(str(row["bse_code"]))
            c[3].write(row["nse_symbol"])
            if c[4].button("✕", key=f"del_{row['company_key']}"):
                res = delete_subscription(email, row["company_key"])
                if res == "deleted":
                    st.rerun()
                else:
                    st.error(res)
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


# ============================================================ FILINGS: NSE
NSE_MONTH_Q = {7: ("Q1", 1), 8: ("Q1", 1), 10: ("Q2", 1), 11: ("Q2", 1),
               1: ("Q3", 0), 2: ("Q3", 0), 4: ("Q4", 0), 5: ("Q4", 0), 6: ("Q4", 0)}
NSE_KW = ("financial results", "financial statement", "quarter ended", "year ended",
          "audited", "unaudited", "standalone", "consolidated")


def nse_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "application/json", "Accept-Language": "en-US,en;q=0.9",
    })
    for u in (NSE + "/get-quotes/equity?symbol=RELIANCE",
              NSE + "/api/equity-stock-indices?index=NIFTY%20500"):
        try:
            s.get(u, timeout=8)
        except Exception:
            pass
    return s


@st.cache_data(ttl=86400, show_spinner=False)
def nse_names():
    try:
        r = nse_session().get(
            "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv", timeout=25)
        return {row["Symbol"].strip(): row["Company Name"].strip()
                for row in csv.DictReader(r.text.splitlines())
                if row.get("Symbol") and row.get("Company Name")}
    except Exception:
        return {}


@st.cache_data(ttl=86400, show_spinner=False)
def nse_companies():
    try:
        s = nse_session()
        s.headers["Referer"] = NSE + "/market-data/live-equity-market"
        r = s.get(NSE + "/api/equity-stock-indices?index=NIFTY%20500", timeout=25)
        names = nse_names()
        out = [{"symbol": row["symbol"], "name": names.get(row["symbol"]) or row["symbol"]}
               for row in r.json()["data"] if not row["symbol"].upper().startswith("NIFTY")]
        out.sort(key=lambda c: c["symbol"])
        return out
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {e}"}


def nse_quarter(an_dt):
    d = dt.datetime.strptime(an_dt.split()[0], "%d-%b-%Y").date()
    if d.month not in NSE_MONTH_Q:
        return "Other"
    q, add = NSE_MONTH_Q[d.month]
    return f"FY{str(d.year + add)[2:]} {q}"


def nse_strength(text, url):
    if any(k in (text or "").lower() for k in NSE_KW):
        return 2
    fn = (url or "").lower().rsplit("/", 1)[-1]
    if any(x in fn for x in ("fundraise", "appoint", "gdr", "qip", "director")):
        return 0
    return 1 if ("result" in fn or "financial" in fn) else 0


@st.cache_data(ttl=86400, show_spinner=False)
def nse_lookup(symbol, n_years):
    s = nse_session()
    to, windows = dt.date.today(), []
    for _ in range(n_years):
        frm = to.replace(year=to.year - 1)
        windows.append((frm, to))
        to = frm

    def one(w):
        url = (NSE + "/api/corporate-announcements?index=equities&symbol=" + symbol
               + "&from_date=" + w[0].strftime("%d-%m-%Y") + "&to_date=" + w[1].strftime("%d-%m-%Y"))
        h = dict(s.headers, Referer=NSE + "/get-quotes/equity?symbol=" + symbol)
        try:
            return s.get(url, headers=h, timeout=25).json()
        except Exception:
            return []

    rows = []
    with ThreadPoolExecutor(max_workers=min(3, len(windows))) as ex:
        for chunk in ex.map(one, windows):
            rows.extend(chunk)

    def bucketed(kind):
        want = "Outcome of Board Meeting" if kind == "results" else "Investor Presentation"
        buckets, seen = {}, set()
        for row in rows:
            if row.get("desc") != want:
                continue
            uid = row.get("attchmntFile") or (row.get("an_dt", "") + (row.get("attchmntText") or "")[:40])
            if uid in seen:
                continue
            seen.add(uid)
            strength = nse_strength(row.get("attchmntText"), row.get("attchmntFile")) if kind == "results" else 0
            buckets.setdefault(nse_quarter(row["an_dt"]), []).append({
                "date": row["an_dt"], "desc": row.get("attchmntText", ""),
                "url": row.get("attchmntFile", ""), "strength": strength,
                "size": row.get("attFileSize") or "",
            })
        for q in buckets:
            buckets[q].sort(key=lambda x: x["date"], reverse=True)
            if kind == "results":
                buckets[q].sort(key=lambda x: -x["strength"])
        return [{"quarter": q, "items": buckets[q]} for q in sorted(buckets, key=qsort_key, reverse=True)]

    return {"n_rows": len(rows), "results": bucketed("results"),
            "presentations": bucketed("presentations")}


def nse_render(quarters, kind):
    if not quarters:
        st.info("No filings found.")
        return
    links = []
    for q in quarters:
        st.markdown(f"<div class='qh'>{html.escape(q['quarter'])}</div>", unsafe_allow_html=True)
        for i, it in enumerate(q["items"]):
            top = kind == "results" and i == 0 and it["strength"] > 0
            annual = top and q["quarter"].endswith("Q4")
            if top or kind != "results":
                links.append(it["url"])
            pill = ("<span class='pill t'>likely</span>" if top else "") + \
                   ("<span class='pill a'>annual</span>" if annual else "")
            cls = "top" if top else ""
            st.markdown(
                f"<div class='card {cls}'><div class='m'>{html.escape(it['date'])} · "
                f"{html.escape(it['size'])}{pill}</div><div class='d'>{html.escape(it['desc'])}</div>"
                f"<a href='{html.escape(it['url'])}' target='_blank'>Open PDF ↗</a></div>",
                unsafe_allow_html=True)
    if links:
        with st.expander(f"📋 Copy {len(links)} links"):
            st.code("\n".join(links), language=None)


def nse_run():
    st.markdown('<div class="title">📊 NSE Quarterly Results &amp; Presentations</div>'
                '<div class="sub">NIFTY 500 · links only · live from NSE</div>', unsafe_allow_html=True)
    companies = nse_companies()
    if isinstance(companies, dict):
        st.error(f"Couldn't load NSE company list. {companies['_error']}")
        return
    lab = {(c["symbol"] if c["name"] == c["symbol"] else f"{c['symbol']} — {c['name']}"): c
           for c in companies}

st.markdown("""
<style>
  /* make the radio look like a tab strip */
  div[role="radiogroup"] { flex-direction: row; gap: 4px; border-bottom: 1px solid rgba(128,128,128,.25); }
  div[role="radiogroup"] > label { margin: 0; padding: 8px 16px; cursor: pointer;
                                    border-bottom: 2px solid transparent; }
  div[role="radiogroup"] > label > div:first-child { display: none; }   /* hide the dot */
  div[role="radiogroup"] > label:has(input:checked) { border-bottom: 2px solid #ff4b4b; }
  div[role="radiogroup"] > label:has(input:checked) p { color: #ff4b4b; font-weight: 600; }
</style>
""", unsafe_allow_html=True)

    choice = st.selectbox("Company", list(lab), index=None,
                          placeholder="Search a company…", label_visibility="collapsed", key="nse_co")
    latest = st.radio("Range", ["Latest quarter", f"Full history (since {START_YEAR})"],
                      horizontal=True, label_visibility="collapsed", key="nse_scope").startswith("Latest")
    if not choice:
        st.caption("Pick a company to load its filings.")
        return
    sym = lab[choice]["symbol"]
    n_years = 1 if latest else dt.date.today().year - START_YEAR
    with st.spinner(f"Fetching {sym} from NSE…"):
        data = nse_lookup(sym, n_years)
    if data["n_rows"] == 0:
        nse_lookup.clear()
        st.error(f"NSE returned nothing for {sym} (blocked or timed out). Retry in a moment.")
        return
    res = data["results"][:1] if latest else data["results"]
    ppt = data["presentations"][:1] if latest else data["presentations"]
    n_res = sum(len(q["items"]) for q in res)
    n_ppt = sum(len(q["items"]) for q in ppt)
    st.markdown(f'<div class="cobar"><b>{html.escape(sym)}</b>'
                f'<span>{html.escape(lab[choice]["name"])}</span></div>', unsafe_allow_html=True)
    t1, t2 = st.tabs([f"Results ({n_res})", f"Presentations ({n_ppt})"])
    with t1:
        nse_render(res, "results")
    with t2:
        nse_render(ppt, "presentations")


# ============================================================ FILINGS: BSE
BSE_ANN = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
BSE_ASPX = "https://www.bseindia.com/stockinfo/AnnPdfOpen.aspx?Pname="
BSE_CORP = "https://www.bseindia.com/xml-data/corpfiling/CorpAttachment/"
BSE_MONTH_Q = {7: ("Q1", 1), 8: ("Q1", 1), 9: ("Q1", 1), 10: ("Q2", 1), 11: ("Q2", 1),
               12: ("Q2", 1), 1: ("Q3", 0), 2: ("Q3", 0), 3: ("Q3", 0),
               4: ("Q4", 0), 5: ("Q4", 0), 6: ("Q4", 0)}


@st.cache_data(ttl=86400, show_spinner="Loading BSE company list…")
def bse_companies():
    params = {"Group": "", "Scripcode": "", "segment": "Equity", "status": "Active", "scripName": ""}
    rows = requests.get(BSE_SCRIP, headers=BSE_H, params=params, timeout=25).json()
    out = [{"code": str(r["SCRIP_CD"]), "symbol": r.get("scrip_id") or "",
            "name": r.get("Scrip_Name") or r.get("Issuer_Name") or ""} for r in rows]
    out.sort(key=lambda c: c["name"].lower())
    return out


def bse_dt(row):
    raw = (row.get("DissemDT") or row.get("News_submission_dt") or row.get("NEWS_DT") or "").split(".")[0]
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(raw, fmt)
        except ValueError:
            pass
    return None


def bse_quarter(d):
    if not d or d.month not in BSE_MONTH_Q:
        return "Other"
    q, add = BSE_MONTH_Q[d.month]
    return f"FY{str(d.year + add)[2:]} {q}"


def bse_pdf(row):
    fn = row.get("ATTACHMENTNAME") or ""
    if not fn:
        return ""
    if row.get("PDFFLAG") == 2:
        p = (row.get("DissemDT") or row.get("NEWS_DT") or "")[:10].split("-")
        return f"{BSE_CORP}{p[0]}/{int(p[1])}/{fn}" if len(p) == 3 else ""
    return f"{BSE_ASPX}{fn}"


@st.cache_data(ttl=86400, show_spinner=False)
def bse_fetch(code, n_years, cat, subcat):
    to = dt.date.today()
    frm = to.replace(year=to.year - n_years)
    params = {"pageno": 1, "strCat": cat, "subcategory": subcat,
              "strPrevDate": frm.strftime("%Y%m%d"), "strToDate": to.strftime("%Y%m%d"),
              "strScrip": code, "strSearch": "P", "strType": "C"}
    data = requests.get(BSE_ANN, headers=BSE_H, params=params, timeout=30).json()
    table = data.get("Table", []) if isinstance(data, dict) else []
    buckets = {}
    for r in table:
        d = bse_dt(r)
        buckets.setdefault(bse_quarter(d), []).append({
            "date": d.strftime("%d %b %Y") if d else "", "raw": d or dt.datetime.min,
            "title": r.get("NEWSSUB") or "", "desc": r.get("HEADLINE") or "",
            "url": bse_pdf(r), "size": r.get("Fld_Attachsize") or 0,
        })
    for q in buckets:
        buckets[q].sort(key=lambda x: x["raw"], reverse=True)
    return [{"quarter": q, "items": buckets[q]} for q in sorted(buckets, key=qsort_key, reverse=True)]


def bse_render(quarters, kind):
    if not quarters:
        st.info("No filings found.")
        return
    for q in quarters:
        st.markdown(f'<div class="qh">{q["quarter"]}</div>', unsafe_allow_html=True)
        for it in q["items"]:
            ann = kind == "results" and q["quarter"].endswith("Q4")
            pill = '<span class="pill a">annual</span>' if ann else ""
            kb = f'{int(it["size"]) / 1024:.0f} KB' if it["size"] else ""
            link = f'<a href="{it["url"]}" target="_blank">Open PDF ↗</a>' if it["url"] else ""
            st.markdown(f'<div class="card {"ann" if ann else ""}"><div class="m">{it["date"]} · '
                        f'{kb}{pill}</div><div class="h">{it["title"]}</div>'
                        f'<div class="d">{it["desc"]}</div>{link}</div>',
                        unsafe_allow_html=True)


def bse_run():
    st.markdown('<div class="title">📊 BSE Quarterly Results</div>'
                '<div class="sub">Any listed company · links only · live from BSE</div>',
                unsafe_allow_html=True)
    companies = bse_companies()
    lab = {f"{c['symbol'] or c['code']} — {c['name']} ({c['code']})": c for c in companies}
    choice = st.selectbox("Company", list(lab), index=None,
                          placeholder="Type a name, ticker, or code…",
                          label_visibility="collapsed", key="bse_co")
    scope = st.radio("Range", ["Latest", "5Y", "10Y"], horizontal=True,
                     label_visibility="collapsed", key="bse_scope")
    n_years = {"Latest": 1, "5Y": 5, "10Y": 10}[scope]
    if not choice:
        st.caption(f"{len(companies)} BSE companies available.")
        return
    c = lab[choice]
    with st.spinner(f"Fetching {c['name']} from BSE…"):
        res = bse_fetch(c["code"], n_years, "Result", "Financial Results")
        ppt = bse_fetch(c["code"], n_years, "Company Update", "Investor Presentation")
    if scope == "Latest":
        res, ppt = res[:1], ppt[:1]
    n_res = sum(len(q["items"]) for q in res)
    n_ppt = sum(len(q["items"]) for q in ppt)
    st.markdown(f'<div class="cobar"><b>{c["symbol"] or c["code"]}</b>'
                f'<span>{c["name"]}</span></div>', unsafe_allow_html=True)
    t1, t2 = st.tabs([f"Results ({n_res})", f"Presentations ({n_ppt})"])
    with t1:
        bse_render(res, "results")
    with t2:
        bse_render(ppt, "presentations")


# ============================================================ MAIN
st.title("Filings Sentinel")

# --- Handle the ?code=... redirect (Filing Alerts login) ---
qp = st.query_params
if "code" in qp:
    session = exchange_code(qp["code"])
    if session and session.get("user") and session.get("access_token"):
        st.session_state["email"] = (session["user"].get("email") or "").strip().lower()
        st.session_state["token"] = session["access_token"]
        st.session_state["goto_alerts"] = True
    else:
        st.error("Login failed during code exchange. Please try signing in again.")
    st.query_params.clear()
    st.rerun()

email = st.session_state.get("email")

sections = ["About", "NSE", "BSE", "Filing Alerts"]

# Land on Filing Alerts right after a fresh login; About by default otherwise.
if st.session_state.pop("goto_alerts", False):
    st.session_state["section"] = "Filing Alerts"

default = "Filing Alerts" if email else "About"
if "section" not in st.session_state:
    st.session_state["section"] = default

choice = st.radio("Section", sections,
                  index=sections.index(st.session_state["section"]),
                  horizontal=True, label_visibility="collapsed", key="section")

if choice == "About":
    st.subheader("About Filings Sentinel")
    st.markdown(
        "- **Filing Alerts** — sign in to pick companies and get emailed when they file with BSE/NSE.\n"
        "- **NSE** — live NIFTY 500 quarterly results & investor presentations, straight from NSE.\n"
        "- **BSE** — quarterly results & presentations for any BSE-listed company."
    )
elif choice == "NSE":
    nse_run()
elif choice == "BSE":
    bse_run()
else:  # Filing Alerts
    if not email:
        st.write("Sign in to manage your filing-alert subscriptions.")
        col1, col2 = st.columns(2)
        col1.link_button("Sign in with Microsoft", oauth_url("azure"), type="primary")
        col2.link_button("Sign in with Google", oauth_url("google"))
    else:
        c1, c2 = st.columns([4, 1])
        c1.caption(f"Signed in as **{email}**")
        if c2.button("Log out"):
            for k in ("email", "token", "section"):
                st.session_state.pop(k, None)
            st.rerun()
        subscriptions_run(email)
