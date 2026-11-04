"""코스피 시총 상위 300 -> 장초 스냅샷 -> 점수화 -> docs/index.html (GitHub Pages)
데이터: 네이버 금융(비공식 엔드포인트) + yfinance. 전부 실패해도 가능한 부분만 출력한다."""
import os, re, json, time, html, datetime as dt
from concurrent.futures import ThreadPoolExecutor as TP
import requests, pandas as pd

KST = dt.timezone(dt.timedelta(hours=9))
UA = {"User-Agent": "Mozilla/5.0"}
SLOT = os.getenv("SLOT", "now")            # 0910 / 0926 / now
TOP_N, SHOW = 300, 40
W = dict(liq=.25, hot=.20, mom=.20, dip=.15, hold=.15, trend=.05)   # 점수 가중치 (여기만 고치면 됨)
now = lambda: dt.datetime.now(KST)


def get(url, headers=None, **kw):
    for _ in range(3):
        try:
            r = requests.get(url, headers={**UA, **(headers or {})}, timeout=15, **kw)
            r.raise_for_status()
            return r
        except Exception:
            time.sleep(1.5)


def num(x):
    try:
        return float(re.sub(r"[^\d.\-]", "", str(x)))
    except ValueError:
        return 0.0


def universe():
    """시총 순 코스피 종목 (우선주 제외: 보통주 코드는 0으로 끝남)"""
    out = {}
    for p in range(1, 9):
        r = get("https://finance.naver.com/sise/sise_market_sum.naver", params={"sosok": 0, "page": p})
        if not r:
            continue
        r.encoding = "euc-kr"
        for c, n in re.findall(r'code=([0-9A-Z]{6})" class="tltle">([^<]+)<', r.text):
            if c.endswith("0"):
                out.setdefault(c, n)
        if len(out) >= TOP_N:
            break
    return list(out.items())[:TOP_N]


def quotes(codes):
