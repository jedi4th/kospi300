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


LOG = []


def log(*a):
    s = " ".join(map(str, a))
    print(s)
    LOG.append(s)


def get(url, headers=None, tries=3, **kw):
    for _ in range(tries):
        try:
            r = requests.get(url, headers={**UA, **(headers or {})}, timeout=15, **kw)
            r.raise_for_status()
            return r
        except Exception as e:
            code = getattr(getattr(e, "response", None), "status_code", "-")
            if len(LOG) < 14:
                log("GET 실패", url[:80], type(e).__name__, code)
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
    log("유니버스(시총 페이지)", len(out))
    if len(out) < TOP_N:                               # 폴백: 모바일 시총 순위 API
        for p in range(1, 6):
            r = get("https://m.stock.naver.com/api/stocks/marketValue/KOSPI", params={"page": p, "pageSize": 100}, tries=1)
            try:
                for s in r.json()["stocks"]:
                    if s["itemCode"].endswith("0"):
                        out.setdefault(s["itemCode"], s["stockName"])
            except Exception:
                break
            if len(out) >= TOP_N:
                break
        log("유니버스(폴백 후)", len(out))
    return list(out.items())[:TOP_N]


def quotes(codes):
    def f(ch):
        r = get("https://polling.finance.naver.com/api/realtime/domestic/stock/" + ",".join(ch), tries=2)
        if r is None and len(ch) > 1:                  # 묶음 실패 -> 단건으로 재시도
            return [x for c in ch for x in f([c])]
        try:
            ds = r.json()["datas"]
        except Exception:
            if r is not None and len(LOG) < 14:
                log("시세 파싱 실패", r.text[:200].replace("\n", " "))
            return []
        out = []
        for i, d in enumerate(ds):   # 종목코드 필드명이 다를 수 있어 순서로도 보정
            c = d.get("itemCode") or d.get("symbolCode") or d.get("stockCode") or (ch[i] if len(ds) == len(ch) else None)
            if c:
                out.append((c, d))
        return out
    q = {}
    with TP(4) as ex:
        for ds in ex.map(f, [codes[i:i + 30] for i in range(0, len(codes), 30)]):
            q.update(ds)
    return q


def kospi_chg():
    r = get("https://polling.finance.naver.com/api/realtime/domestic/index/KOSPI")
    try:
        return num(r.json()["datas"][0]["fluctuationsRatio"])
    except Exception:
        return None


def daily(c):
    r = get(f"https://m.stock.naver.com/api/stock/{c}/price", params={"pageSize": 30, "page": 1})
    try:
        rows = sorted((x["localTradedAt"][:10].replace("-", ""), num(x["closePrice"]), num(x["accumulatedTradingVolume"]))
                      for x in r.json())
    except Exception:
        return c, None
    rows = [x for x in rows if x[0] < TODAY]          # 오늘 미완성 봉 제외
    if len(rows) < 6:
        return c, None
    cl, vo = [x[1] for x in rows], [x[2] for x in rows]
    n = min(20, len(rows))
    return c, dict(avgv=sum(vo[-n:]) / n, ret5=cl[-1] / cl[-6] - 1, ma20=sum(cl[-n:]) / n)


def minute(c):
    r = get("https://fchart.stock.naver.com/sise.nhn", params={"symbol": c, "timeframe": "minute", "count": 80, "requestType": 0})
    if not r:
        return c, None
    m = [x for x in re.findall(r'data="(\d{12})\|[\d.]+\|([\d.]+)\|[\d.]+\|([\d.]+)\|(\d+)"', r.text) if x[0].startswith(TODAY)]
    if len(m) < 5:
        return c, None
    hi, cl, vo = [float(x[1]) for x in m], [float(x[2]) for x in m], [float(x[3]) for x in m]
    t = m[hi.index(max(hi))][0]
    return c, dict(path=cl[4::5] + ([cl[-1]] if len(cl) % 5 else []), hi_t=f"{t[8:10]}:{t[10:12]}",
                   fade=(sum(vo[-3:]) / 3) / (sum(vo) / len(vo)))


def macro():
    try:
        import yfinance as yf
        T = {"S&P500": "^GSPC", "나스닥": "^IXIC", "반도체(SOX)": "^SOX", "S&P선물": "ES=F", "나스닥선물": "NQ=F",
             "원/달러": "KRW=X", "WTI": "CL=F", "美10년": "^TNX", "VIX": "^VIX"}
        d = yf.download(list(T.values()), period="7d", progress=False, auto_adjust=True)["Close"]
        return {k: round(float(d[v].dropna().pct_change().iloc[-1]) * 100, 2) for k, v in T.items() if v in d}
    except Exception as e:
        print("macro 실패:", e)
        return {}


def news(name):
    i, s = os.getenv("NAVER_ID"), os.getenv("NAVER_SECRET")
    if not i:
        return ""
    r = get("https://openapi.naver.com/v1/search/news.json", headers={"X-Naver-Client-Id": i, "X-Naver-Client-Secret": s},
            params={"query": name, "display": 3, "sort": "date"})
    try:
        return " | ".join(re.sub(r"<[^>]+>|&quot;|&amp;", "", x["title"]) for x in r.json()["items"][:2])
    except Exception:
        return ""


def main():
    uni = universe()
    names, mc = dict(uni), {c: i + 1 for i, (c, _) in enumerate(uni)}
    rows = []
    qs = quotes([c for c, _ in uni])
    log("시세 응답 종목수", len(qs))
    if qs:
        log("샘플", json.dumps(next(iter(qs.values())), ensure_ascii=False)[:500])
    for c, d in qs.items():
        try:
            cur, op, hi, vol, amt, chv, fr = (num(d[k]) for k in (
                "closePrice", "openPrice", "highPrice", "accumulatedTradingVolume", "accumulatedTradingValue",
                "compareToPreviousClosePrice", "fluctuationsRatio"))
        except Exception:
            continue
        if fr * chv < 0:                               # 변동폭 부호 보정
            chv = -chv
        prev = cur - chv
        if vol <= 0 or prev <= 0 or op <= 0 or hi <= 0:
            continue
        u = min((1e6, 1e3, 1), key=lambda u: abs(amt * u / vol / cur - 1))   # 거래대금 단위 자동 보정
        vw = amt * u / vol
        rows.append(dict(code=c, name=names.get(c, c), mc=mc.get(c, 0), prev=prev, cur=cur, open=op, high=hi, vol=vol,
                         amt=round(amt * u / 1e8, 1), chg=cur / prev - 1, gap=op / prev - 1, hi_chg=hi / prev - 1,
                         pull=1 - cur / hi, vwap=vw, vs_vwap=cur / vw - 1))
    if len(rows) < 30:
        log("수집 종목 부족(휴장/장 시작 전/차단) -> 종료:", len(rows))
        os.makedirs("docs", exist_ok=True)
        open("docs/debug.txt", "w", encoding="utf-8").write(now().strftime("%Y-%m-%d %H:%M:%S KST") + "\n" + "\n".join(LOG))
        return
    full = pd.DataFrame(rows).set_index("code")
    full["liq"] = full.amt.rank(pct=True)

    # 1차 필터: 거래대금 상위 80 + 등락률 상위 40
    pick = set(full.amt.nlargest(80).index) | set(full.chg.nlargest(40).index)
    with TP(8) as ex:
        dd = dict(ex.map(daily, pick))
    df = full.loc[list(pick)].copy()
    for k in ("avgv", "ret5", "ma20"):
        df[k] = [(dd.get(c) or {}).get(k) for c in df.index]
    df["vmult"] = df.vol / df.avgv
    df["hot"] = df.vmult.rank(pct=True).fillna(.5)
    df["mom"] = df.hi_chg.clip(0, .15) / .15
    df["dip"] = df.pull.map(lambda p: .5 if p < .01 else 1 if p <= .05 else .6 if p <= .08 else .2)
    df["hold"] = ((df.cur >= df.vwap).astype(float) + (df.cur >= df.open).astype(float)) / 2
    df["trend"] = (df.prev > df.ma20).astype(float) * .5 + .5
    pen = (df.ret5.fillna(0) > .25) * 15 + (df.chg >= .25) * 100        # 5일 과열 감점, 상한가권 제외
    df["score"] = (100 * sum(W[k] * df[k] for k in W) - pen).round(1)

    # 직전 스냅샷(9:10) 대비 변화 = 최근 매수세 대용
    os.makedirs("docs", exist_ok=True)
    try:
        s = json.load(open("docs/snap_0910.json"))
        if s["date"] == TODAY and SLOT != "0910":
            b = pd.DataFrame(s["rows"]).set_index("code")
            df["d_amt"] = (df.amt - b.amt.reindex(df.index)).round(1)
            df["d_chg"] = ((df.chg - b.chg.reindex(df.index)) * 100).round(2)
    except Exception:
        pass
    json.dump({"date": TODAY, "time": now().strftime("%H:%M"),
               "rows": [dict(code=c, amt=r.amt, chg=r.chg) for c, r in full.iterrows()]},
              open(f"docs/snap_{SLOT}.json", "w"))

    top = df.sort_values("score", ascending=False).head(SHOW)
    with TP(8) as ex:
        mm = dict(ex.map(minute, list(top.index[:30])))
    nw = {c: news(top.loc[c, "name"]) for c in top.index[:15]}
    mac, kc = macro(), kospi_chg()

    # ---- 출력 ----
    P = lambda x: "" if pd.isna(x) else f"{x * 100:+.1f}"
    cols = ["#", "종목", "시총", "현재가", "등락%", "갭%", "장초高%", "고점比%", "VWAP比%", "대금억", "Δ대금억",
            "Δ등락%p", "거래량%", "5일%", "점수", "진입참고", "손절", "익절", "5분경로(전일比%)", "고점시각", "뉴스"]
    out = []
    for i, (c, r) in enumerate(top.iterrows(), 1):
        m = mm.get(c) or {}
        entry = round(min(r.cur, r.high * .97))
        out.append([i, f"{r['name']}({c})", int(r.mc), f"{r.cur:,.0f}", P(r.chg), P(r.gap), P(r.hi_chg), P(-r.pull),
                    P(r.vs_vwap), f"{r.amt:,.0f}", "" if pd.isna(r.get("d_amt")) else f"{r.d_amt:+,.0f}",
                    "" if pd.isna(r.get("d_chg")) else f"{r.d_chg:+.1f}",
                    "" if pd.isna(r.vmult) else f"{r.vmult * 100:.0f}", P(r.ret5), r.score, f"{entry:,}",
                    f"{entry * .97:,.0f}", f"{entry * 1.06:,.0f}",
                    " ".join(f"{(x / r.prev - 1) * 100:+.0f}" for x in m.get("path", [])), m.get("hi_t", ""),
                    nw.get(c, "")])
    stamp = now().strftime("%Y-%m-%d %H:%M:%S KST")
    mline = " | ".join(f"{k} {v:+.2f}%" for k, v in mac.items()) or "매크로 수집 실패"
    mline = (f"코스피 {kc:+.2f}% | " if kc is not None else "") + mline
    tsv = "\n".join("\t".join(map(str, x)) for x in [cols] + out)
    th = "".join(f"<th>{html.escape(x)}</th>" for x in cols)
    tb = "".join("<tr>" + "".join(f"<td>{html.escape(str(v))}</td>" for v in r) + "</tr>" for r in out)
    open("docs/index.html", "w", encoding="utf-8").write(f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>코스피300 장초 스냅샷 {SLOT}</title>
<style>:root{{color-scheme:light dark;--bg:#fff;--fg:#1a1a1a;--line:#d8d8d8;--mut:#666}}
@media(prefers-color-scheme:dark){{:root{{--bg:#16181c;--fg:#e8e8e8;--line:#3a3d44;--mut:#9aa}}}}
body{{margin:0;padding:12px;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,-apple-system,"Noto Sans KR",sans-serif}}
h1{{font-size:18px;margin:0 0 4px}}p{{margin:4px 0;color:var(--mut)}}.w{{overflow-x:auto}}
table{{border-collapse:collapse;font-size:12px;white-space:nowrap}}th,td{{border:1px solid var(--line);padding:3px 6px;text-align:right}}
td:nth-child(2),td:nth-child(21){{text-align:left}}th{{position:sticky;top:0;background:var(--bg)}}
pre{{font-size:11px;overflow-x:auto;border:1px solid var(--line);padding:8px}}button{{padding:8px 14px;font:inherit}}</style></head><body>
<h1>코스피 시총 {TOP_N} 장초 스냅샷 ({SLOT})</h1>
<p>생성 {stamp} | 수집 {len(full)}종목 | 1차필터 {len(df)}종목 중 점수 상위 {SHOW}</p><p>{html.escape(mline)}</p>
<p>점수 = 유동성25·거래량20·장초강도20·눌림적합15·지지(VWAP/시가)15·추세5, 5일 +25% 이상 -15. 진입참고 = min(현재가, 장초고점×0.97), 손절 -3%, 익절 +6%.</p>
<div class="w"><table><thead><tr>{th}</tr></thead><tbody>{tb}</tbody></table></div>
<p><button onclick="navigator.clipboard.writeText(document.getElementById('t').textContent)">표 복사</button></p>
<pre id="t">{html.escape(tsv)}</pre></body></html>""")
    print("완료", SLOT, stamp, len(full), len(top))


if __name__ == "__main__":
    T = {"0910": (9, 10), "0926": (9, 26)}
    if SLOT in T:                                  # 크론은 일찍 깨우고, 목표 시각까지 대기
        t = now().replace(hour=T[SLOT][0], minute=T[SLOT][1], second=0, microsecond=0)
        if 0 < (t - now()).total_seconds() < 3000:
            time.sleep((t - now()).total_seconds())
    TODAY = now().strftime("%Y%m%d")
    main()
