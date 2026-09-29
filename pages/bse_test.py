import os
import requests
import streamlit as st

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
HOME = "https://www.bseindia.com/"
TARGET = ("https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
          "?pageno=1&strCat=Result&subcategory=Financial%20Results&strPrevDate=20250101"
          "&strToDate=20261231&strScrip=500325&strSearch=P&strType=C")
BASIC = {"User-Agent": UA, "Accept": "application/json", "Referer": HOME, "Origin": HOME.rstrip("/")}
FULL = {**BASIC,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site"}


def test(name, fn):
    try:
        code, text = fn()
        ok = code == 200 and text.lstrip()[:1] in "{["
        st.write(f"{'✅' if ok else '❌'} **{name}** → {code}")
        st.code(text[:150])
    except Exception as e:
        st.write(f"❌ **{name}** → {type(e).__name__}: {e}")


def get(url, headers=None, session=None):
    r = (session or requests).get(url, headers=headers, timeout=20)
    return r.status_code, r.text


def m1_tls():
    from curl_cffi import requests as creq
    r = creq.get(TARGET, headers={"Referer": HOME, "Origin": HOME.rstrip("/")},
                 impersonate="chrome", timeout=20)
    return r.status_code, r.text


def m2_cookies():
    s = requests.Session()
    s.get(HOME, headers={"User-Agent": UA}, timeout=20)
    return get(TARGET, BASIC, s)


def m123_combined():
    from curl_cffi import requests as creq
    s = creq.Session(impersonate="chrome")
    s.get(HOME, timeout=20)
    r = s.get(TARGET, headers={"Referer": HOME, "Origin": HOME.rstrip("/")}, timeout=20)
    return r.status_code, r.text


def m4_browser():
    from playwright.sync_api import sync_playwright
    exe = "/usr/bin/chromium" if os.path.exists("/usr/bin/chromium") else None
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, executable_path=exe)
        page = b.new_page(user_agent=UA)
        page.goto(HOME, timeout=40000)
        r = page.goto(TARGET, timeout=40000)
        out = r.status, page.inner_text("body")
        b.close()
        return out


st.title("BSE bypass test")
test("Outbound IP", lambda: get("https://ipinfo.io/json"))
test("0. Baseline (current code)", lambda: get(TARGET, BASIC))
test("1. Chrome TLS (curl_cffi)", m1_tls)
test("2. Homepage cookies first", m2_cookies)
test("3. Full Chrome headers", lambda: get(TARGET, FULL))
test("1+2+3 combined", m123_combined)
test("4. Real headless browser", m4_browser)
test("5a. Other door: www homepage", lambda: get(HOME, {"User-Agent": UA}))
test("5b. Other door: announcements page",
      lambda: get(HOME + "corporates/ann.html", {"User-Agent": UA}))
