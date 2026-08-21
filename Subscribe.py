"""
Minimal BSE company search bar. Master list only — subscribe logic comes later.
Run:  streamlit run subscribe.py
Deps: pip install streamlit requests
"""
import requests
import streamlit as st

BSE_H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
         "Accept": "application/json", "Referer": "https://www.bseindia.com/",
         "Origin": "https://www.bseindia.com"}
BSE_SCRIP = "https://api.bseindia.com/BseIndiaAPI/api/ListofScripData_new/w"


@st.cache_data(ttl=86400, show_spinner="Loading BSE company list…")
def bse_companies():
    params = {"Group": "", "Scripcode": "", "segment": "Equity", "status": "Active", "scripName": ""}
    rows = requests.get(BSE_SCRIP, headers=BSE_H, params=params, timeout=25).json()
    out = [{"code": str(r["SCRIP_CD"]), "symbol": r.get("scrip_id") or "",
            "name": r.get("Scrip_Name") or r.get("Issuer_Name") or ""} for r in rows]
    out.sort(key=lambda c: c["name"].lower())
    return out


st.title("Subscribe to BSE filing alerts")

companies = bse_companies()
lab = {f"{c['symbol'] or c['code']} — {c['name']} ({c['code']})": c for c in companies}
choice = st.selectbox("Company", list(lab), index=None,
                      placeholder="Type a name, ticker, or scrip code…",
                      label_visibility="collapsed")

if choice:
    c = lab[choice]
    st.write(f"**{c['name']}** — symbol `{c['symbol']}`, scrip code `{c['code']}`")
else:
    st.caption(f"{len(companies)} BSE companies available.")
