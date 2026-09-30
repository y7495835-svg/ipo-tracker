"""2026 공모주 성과판 — 데이터 수집 + 사이트 생성.

GitHub Actions가 평일 18:30(KST)에 실행합니다.
  1) 38커뮤니케이션: 신규상장 목록, 공모 상세(밴드·확정가·경쟁률·확약), 수요예측/청약 일정
  2) KIND: 업종·주요제품
  3) 아이피오스탁: 상장주식수·유통가능물량·보호예수
  4) Daum 금융: 일별 종가, 코스닥·코스피 지수
결과: site/index.html (사이트), data/ipo.json·data/ipo.csv (원자료), data/cache.json (다음 실행용 캐시)
"""
import calendar, csv, datetime as dt, json, os, re, sys, time, traceback
import requests
from bs4 import BeautifulSoup

YEAR = int(os.environ.get("IPO_YEAR", "2026"))
KST = dt.timezone(dt.timedelta(hours=9))
NOW = dt.datetime.now(KST)
TODAY = NOW.date()
ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data"); SITE = os.path.join(ROOT, "site")
os.makedirs(DATA, exist_ok=True); os.makedirs(SITE, exist_ok=True)
EXCLUDE = re.compile(r"스팩|리츠|SPAC", re.I)
LOG = []          # 실행 요약(Actions 화면에 표시)
WARN = []

S = requests.Session()
S.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
                  "Accept-Language": "ko-KR,ko;q=0.9"})


def fetch(url, enc=None, method="GET", data=None, headers=None, tries=4, pause=0.4):
    for k in range(tries):
        try:
            r = S.request(method, url, data=data, headers=headers or {}, timeout=30)
            if r.status_code == 429:
                time.sleep(3 * (k + 1)); continue
            r.raise_for_status()
            if enc: r.encoding = enc
            time.sleep(pause)
            return r
        except Exception as e:
            if k == tries - 1: raise
            time.sleep(2 * (k + 1))
    raise RuntimeError(f"요청 한도 초과(429) 반복: {url}")


def soup(url, enc, **kw):
    return BeautifulSoup(fetch(url, enc, **kw).text, "lxml")


def txt(el): return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()
def num(s):
    if s is None: return None
    s = re.sub(r"[^\d.\-]", "", str(s))
    try: return float(s) if s not in ("", "-", ".") else None
    except ValueError: return None
def clean_name(n): return re.sub(r"\(구\..*?\)|\(유가\)", "", n).strip()
def norm(n): return re.sub(r"[\s()（）.·㈜]|주식회사", "", clean_name(n))
def leaf_rows(sp):
    for tr in sp.find_all("tr"):
        if tr.find("tr"): continue
        yield [txt(c) for c in tr.find_all(["td", "th"])], tr


def biggest_table(sp, must):
    best = None
    for t in sp.find_all("table"):
        if t.find("table"): continue
        if must not in t.get_text(): continue
        n = len(t.find_all("tr"))
        if not best or n > best[0]: best = (n, t)
    return best[1] if best else None


def no_of(tr):
    a = tr.find("a", href=re.compile(r"no=\d+"))
    return re.search(r"no=(\d+)", a["href"]).group(1) if a else None


def parse_range(s):
    """'2026.11.02~11.06' -> (date, date)"""
    m = re.search(r"(\d{4})\.(\d{1,2})\.(\d{1,2})\s*~\s*(?:(\d{4})\.)?(\d{1,2})\.(\d{1,2})", s or "")
    if not m:
        m1 = re.search(r"(\d{4})\.(\d{1,2})\.(\d{1,2})", s or "")
        if not m1: return None, None
        d = dt.date(*map(int, m1.groups())); return d, d
    y, mo, d, y2, mo2, d2 = m.groups()
    a = dt.date(int(y), int(mo), int(d))
    b = dt.date(int(y2) if y2 else int(y), int(mo2), int(d2))
    if b < a: b = dt.date(b.year + 1, b.month, b.day)
    return a, b


# ---------------------------------------------------------------- 38커뮤니케이션
def list_38():
    out = []
    for p in range(1, 15):
        sp = soup(f"http://www.38.co.kr/html/fund/index.htm?o=nw&page={p}", "euc-kr")
        t = biggest_table(sp, "신규상장일")
        if not t: break
        older = False
        for tr in t.find_all("tr"):
            c = [txt(x) for x in tr.find_all("td")]
            if len(c) < 9 or not re.match(r"\d{4}/\d{2}/\d{2}", c[1]): continue
            d = dt.date(*map(int, c[1].split("/")))
            if d.year < YEAR: older = True; continue
            if d.year > YEAR: continue
            out.append(dict(no=no_of(tr), raw=c[0], name=clean_name(c[0]), kospi="(유가)" in c[0], date=d,
                            open=num(c[6]), close1=num(c[8])))
        if older: break
    return out


DETAIL_KEYS = ["종목코드", "시장구분", "업종", "기업구분", "매출액", "순이익", "총공모주식수", "상장공모", "희망공모가액",
               "청약경쟁률", "확정공모가", "공모금액", "주간사", "수요예측일", "공모청약일", "기관경쟁률", "의무보유확약", "상장일"]


def detail_38(no):
    sp = soup(f"http://www.38.co.kr/html/fund/?o=v&no={no}", "euc-kr")
    o = {}
    for c, _ in leaf_rows(sp):
        for i in range(len(c) - 1):
            if c[i] in DETAIL_KEYS and c[i] not in o: o[c[i]] = c[i + 1]
    return o


def schedule_38(o, date_col, must):
    """일정 표는 먼 미래 → 과거 순. 공모가 몰리는 시기엔 가까운 일정이 2페이지 이후로 밀릴 수 있어 오늘 전까지 넘겨 봄"""
    rows, seen = [], set()
    for p in range(1, 6):
        sp = soup(f"http://www.38.co.kr/html/fund/index.htm?o={o}&page={p}", "euc-kr")
        t = biggest_table(sp, must)
        if not t: break
        oldest = None
        for tr in t.find_all("tr"):
            c = [txt(x) for x in tr.find_all("td")]
            if len(c) < 5: continue
            a, b = parse_range(c[date_col])
            if not a: continue
            k = (c[0], c[date_col])
            if k in seen: continue
            seen.add(k)
            rows.append(dict(name=clean_name(c[0]), no=no_of(tr), start=a, end=b, cells=c))
            oldest = b if oldest is None or b < oldest else oldest
        if oldest is None or oldest < TODAY - dt.timedelta(days=7): break
    return rows


# ---------------------------------------------------------------- KIND
def kind_map():
    body = {"method": "searchListingTypeSub", "currentPageSize": "300", "pageIndex": "1", "orderMode": "1", "orderStat": "D",
            "forward": "listingtype_sub", "listTypeArrStr": "01|", "choicTypeArrStr": "04|", "secuGrpArrStr": "0|ST|FS|DR|",
            "fromDate": f"{YEAR}-01-01", "toDate": f"{YEAR}-12-31"}
    r = fetch("https://kind.krx.co.kr/listinvstg/listingcompany.do", "utf-8", method="POST", data=body,
              headers={"Content-Type": "application/x-www-form-urlencoded"})
    m = {}
    for c, _ in leaf_rows(BeautifulSoup(r.text, "lxml")):
        if len(c) >= 7 and re.match(r"\d{4}-\d{2}-\d{2}", c[1] or ""):
            m[norm(c[0])] = dict(industry=c[4], product=c[7] if len(c) > 7 else "")
    return m


# ---------------------------------------------------------------- 아이피오스탁
def ipostock_codes():
    m = {}
    for p in range(1, 12):
        sp = soup(f"http://www.ipostock.co.kr/sub03/ipo05.asp?str1={YEAR}&str2=all&page={p}", "utf-8")
        got = 0
        for a in sp.find_all("a", href=re.compile(r"code=B\d+")):
            t = a.get_text(strip=True)
            if t: m[t] = re.search(r"code=(B\d+)", a["href"]).group(1); got += 1
        if not got: break
    return m


def ipostock_find(name, codes):
    for k, v in codes.items():
        if k == name or (k.endswith("..") and name.startswith(k[:-2])): return v
    sp = soup("http://www.ipostock.co.kr/sub05/company01.asp?str2=" + requests.utils.quote(name), "utf-8")
    for a in sp.find_all("a", href=re.compile(r"code=B\d+")):
        if a.get_text(strip=True) == name: return re.search(r"code=(B\d+)", a["href"]).group(1)
    return None


def horizon(ld, mth):
    """상장일 + 15일(mth=0.5) 또는 n개월 (월말 보정)"""
    if mth == 0.5: return ld + dt.timedelta(days=15)
    y, mo = ld.year + (ld.month - 1 + int(mth)) // 12, (ld.month - 1 + int(mth)) % 12 + 1
    return dt.date(y, mo, min(ld.day, calendar.monthrange(y, mo)[1]))


def ix_at(ix, d):
    ks = [k for k in ix if k <= d]
    return ix[max(ks)] if ks else None


def lock_months(p):
    p = p.replace("상장후 ", "").strip()
    if p == "15일": return 0.5
    m = re.match(r"^(\d+)개월$", p)
    if m: return int(m.group(1))
    if "예탁후 12개월" in p or "후 1년" in p: return 12
    if p.startswith("3개월~6개월"): return 6
    return None


def ipostock_holders(code):
    sp = soup(f"http://www.ipostock.co.kr/view_pg/view_02.asp?code={code}", "utf-8")
    listed = flt = 0; locks = {}; in_lock = False
    for c, _ in leaf_rows(sp):
        c = [x for x in c if x]          # 빈 칸 제거
        if not c: continue
        k0 = c[0].replace(" ", "")       # '보호예수<br>매도금지'처럼 줄바꿈이 공백으로 읽히는 경우 대비
        if k0 == "공모후상장주식수": listed = int(num(c[1]) or 0)
        elif k0 == "유통가능주식합계": flt = int(num(c[1]) or 0)
        elif k0 == "보호예수물량합계": in_lock = False
        elif k0 == "보호예수매도금지":
            in_lock = True; m = lock_months(c[-1])
            if m is not None: locks[m] = locks.get(m, 0) + int(num(c[2]) or 0)
        elif in_lock and len(c) >= 5:
            m = lock_months(c[-1])
            if m is not None: locks[m] = locks.get(m, 0) + int(num(c[1]) or 0)
    return dict(listed=listed, float_sh=flt, locks={str(k): v for k, v in locks.items()})


# ---------------------------------------------------------------- Daum 금융
def daum(path, ref):
    return fetch("https://finance.daum.net" + path, headers={"Referer": ref}, pause=0.5).json()


def daum_series(code, adjusted=False):
    try:
        j = daum(f"/api/charts/A{code}/days?limit=400&adjusted={'true' if adjusted else 'false'}", f"https://finance.daum.net/quotes/A{code}")
    except Exception:
        return []          # 잘못된 코드(예: 38 표기 코드와 실제 코드가 다른 외국기업) → 이름 검색으로 재시도
    return [(d["date"][:10], d["tradePrice"], d["openingPrice"]) for d in j.get("data", []) if d.get("candleAccTradeVolume")]


def daum_search(name):
    j = daum("/api/search?q=" + requests.utils.quote(name), "https://finance.daum.net/")
    for it in j.get("suggestItems", []):
        return it["symbolCode"].lstrip("A")
    return None


def daum_index(market):
    rows = []
    for p in range(1, 6):
        j = daum(f"/api/market_index/days?market={market}&perPage=100&page={p}", "https://finance.daum.net/domestic")
        data = j.get("data", [])
        rows += [(d["date"][:10], d["tradePrice"]) for d in data]
        if not data or data[-1]["date"][:4] < str(YEAR): break
    return sorted({d: v for d, v in rows if d[:4] == str(YEAR)}.items())


# ---------------------------------------------------------------- main
def main():
    cache_path = os.path.join(DATA, "cache.json")
    cache = json.load(open(cache_path, encoding="utf-8")) if os.path.exists(cache_path) else {}
    cache.setdefault("detail", {}); cache.setdefault("holders", {})
    allrows = list_38()
    LOG.append(f"38 신규상장 목록 {YEAR}년: {len(allrows)}건")
    stocks = [r for r in allrows if not EXCLUDE.search(r["raw"]) and r["date"] <= TODAY]
    upcoming_list = [r for r in allrows if not EXCLUDE.search(r["raw"]) and r["date"] > TODAY]
    if not stocks: raise SystemExit("상장 종목을 하나도 못 찾았습니다 — 38커뮤니케이션 구조가 바뀌었을 수 있습니다.")

    try: kind = kind_map(); LOG.append(f"KIND 업종 매칭 대상: {len(kind)}건")
    except Exception as e: kind = {}; WARN.append(f"KIND 실패: {e}")
    try: ipc = ipostock_codes(); LOG.append(f"아이피오스탁 코드 목록: {len(ipc)}건")
    except Exception as e: ipc = {}; WARN.append(f"아이피오스탁 목록 실패: {e}")

    kq = daum_index("KOSDAQ"); kp = daum_index("KOSPI")
    if not kq: raise SystemExit("코스닥 지수를 못 가져왔습니다.")
    cal = [d for d, _ in kq]; kqd = dict(kq); kpd = dict(kp)
    LOG.append(f"지수 {len(cal)}거래일 ({cal[0]} ~ {cal[-1]})")

    rows, unlocks = [], []
    for s in stocks:
        n = s["name"]
        try:
            key = s["no"]
            d = cache["detail"].get(key)
            if not d or (TODAY - s["date"]).days <= 7:
                d = detail_38(key); cache["detail"][key] = d
            code = re.sub(r"\s", "", d.get("종목코드", ""))
            band = re.findall(r"[\d,]+", d.get("희망공모가액", ""))
            lo, hi = (num(band[0]), num(band[1])) if len(band) >= 2 else (None, None)
            offer = num(d.get("확정공모가"))
            if not offer: WARN.append(f"{n}: 확정공모가 없음 → 제외"); continue
            shares = num(d.get("총공모주식수"))
            m = re.search(r"구주매출\s*:\s*([\d,]+)", d.get("상장공모", "")); old = num(m.group(1)) if m else 0
            # holders
            h = cache["holders"].get(n)
            if not h or not h.get("listed") or not h.get("locks"):
                ic = ipostock_find(n, ipc)
                h = ipostock_holders(ic) if ic else {}
                if h.get("listed") and h.get("locks"): cache["holders"][n] = h
                else: WARN.append(f"{n}: 아이피오스탁 주주구성 못 찾음")
            # prices
            ser = daum_series(code) if code else []
            if not ser:
                c2 = daum_search(n)
                if c2 and c2 != code: code = c2; ser = daum_series(code)
            ser = [x for x in ser if x[0] >= s["date"].isoformat()]
            # 무상증자·액면분할 대비: 수정주가 시계열을 쓰고 공모가도 같은 비율로 보정
            fac = 1.0
            if ser:
                adj = dict((x[0], x[1]) for x in daum_series(code, adjusted=True))
                if adj.get(ser[0][0]):
                    fac = adj[ser[0][0]] / ser[0][1]
                    ser = [(dd_, adj.get(dd_, v * fac), o_) for dd_, v, o_ in ser]
            px = [x[1] for x in ser]
            market = "코스피" if (s["kospi"] or d.get("시장구분") == "거래소") else "코스닥"
            ix = kpd if market == "코스피" else kqd
            k = kind.get(norm(n), {})
            lk = d.get("의무보유확약", "")
            listed = h.get("listed") or 0
            r = dict(name=n, code=code, market=market, industry=k.get("industry") or d.get("업종", ""),
                     product=k.get("product", ""), lead=(d.get("주간사", "").split(",")[0]).strip(), underwriters=d.get("주간사", ""),
                     listdate=s["date"].isoformat(), fcst=(parse_range(d.get("수요예측일", ""))[0] or "") and parse_range(d.get("수요예측일", ""))[0].isoformat(),
                     band_lo=lo, band_hi=hi, offer=offer, inst=num(d.get("기관경쟁률", "").split(":")[0]),
                     lockup=(num(lk) / 100) if num(lk) is not None else None,
                     retail=num(d.get("청약경쟁률", "").split(":")[0]), shares=shares,
                     old_ratio=(old / shares) if shares else 0, listed=listed,
                     float_pct=(h.get("float_sh", 0) / listed) if listed else None,
                     sales=num(d.get("매출액")), ni=num(d.get("순이익")),
                     amount=(num(d.get("공모금액")) or 0) / 100)
            r["mcap_ipo"] = offer * listed / 1e8 if listed else None
            r["pricing"] = ("상단초과" if offer > hi else "상단" if offer == hi else "밴드내" if offer > lo else "하단" if offer == lo else "하단미만") if lo and hi else "-"
            r["bandpos"] = offer / hi - 1 if hi else None
            opn = ser[0][2] if ser else s.get("open")          # 시초가(원래 가격)
            r["open"] = opn; r["r_open"] = opn / offer - 1 if opn else None
            offer_adj = offer * fac                            # 수정주가 기준 공모가
            r["offer_adj"] = offer_adj
            r["r0"] = px[0] / offer_adj - 1 if px else None
            # 확약 해제 시점과 같은 달력 기준: 15일·1개월·3개월·6개월 (그날이 휴일이면 직전 거래일 종가)
            for key_, mth in (("r15", 0.5), ("r1m", 1), ("r3m", 3), ("r6m", 6)):
                hd = horizon(s["date"], mth).isoformat()
                if not px or hd > max(cal[-1], ser[-1][0]): r[key_] = None; continue
                upto = [x[1] for x in ser if x[0] <= hd]
                r[key_] = upto[-1] / offer_adj - 1 if upto else None
            if px:
                mx = max(px); last_d = ser[-1][0]
                r.update(cur=px[-1], r_cur=px[-1] / offer_adj - 1, maxc=mx, dd=px[-1] / mx - 1, days=len(px),
                         s_ret=px[-1] / px[0] - 1, mcap_cur=px[-1] * listed / 1e8 if listed else None)
                # 지수가 그날 아직 안 올라왔으면 직전 값 사용
                i0, i1 = ix_at(ix, ser[0][0]), ix_at(ix, last_d)
                r["i_ret"] = i1 / i0 - 1 if i0 and i1 else None
                r["excess"] = r["s_ret"] - r["i_ret"] if r["i_ret"] is not None else None
            else:
                r.update(cur=None, r_cur=None, maxc=None, dd=None, days=0, s_ret=None, i_ret=None, excess=None, mcap_cur=None)
            r["px"] = px
            for mth, sh in (h.get("locks") or {}).items():
                mth = float(mth)
                if mth > 12: continue
                ud = horizon(s["date"], mth)
                unlocks.append(dict(name=n, period="15일" if mth == 0.5 else f"{int(mth)}개월", date=ud.isoformat(), shares=sh,
                                    pct=sh / listed if listed else None, amt=(r["cur"] * sh / 1e8) if r["cur"] else None,
                                    ratio=(r["cur"] / offer_adj) if r["cur"] else None))
            rows.append(r)
        except Exception as e:
            WARN.append(f"{n}: 처리 실패 — {type(e).__name__}: {e}")
            traceback.print_exc()
    rows.sort(key=lambda r: r["listdate"])
    unlocks.sort(key=lambda u: u["date"])

    # 일정 (수요예측 · 청약 · 상장 예정)
    sched = []
    try:
        for x in schedule_38("r", 1, "수요예측일"):
            if EXCLUDE.search(x["name"]) or x["end"] < TODAY - dt.timedelta(days=3): continue
            c = x["cells"]; sched.append(dict(kind="수요예측", name=x["name"], start=x["start"].isoformat(), end=x["end"].isoformat(),
                                             band=c[2], offer=c[3], amount=c[4], underwriter=c[5] if len(c) > 5 else ""))
        for x in schedule_38("k", 1, "공모주일정"):
            if EXCLUDE.search(x["name"]) or x["end"] < TODAY - dt.timedelta(days=3): continue
            c = x["cells"]; sched.append(dict(kind="청약", name=x["name"], start=x["start"].isoformat(), end=x["end"].isoformat(),
                                             band=c[3], offer=c[2], comp=c[4], underwriter=c[5] if len(c) > 5 else ""))
    except Exception as e:
        WARN.append(f"일정 수집 실패: {e}")
    for u in upcoming_list:
        sched.append(dict(kind="상장", name=u["name"], start=u["date"].isoformat(), end=u["date"].isoformat()))
    sched.sort(key=lambda x: (x["start"], x["kind"]))
    LOG.append(f"분석 종목 {len(rows)}개, 보호예수 {len(unlocks)}건, 일정 {len(sched)}건")

    out = dict(rows=rows, unlocks=unlocks, schedule=sched, base=cal[-1], cal=cal, kq=[v for _, v in kq],
               kp=[kpd.get(d) for d in cal], updated=NOW.strftime("%Y-%m-%d %H:%M"))
    json.dump(out, open(os.path.join(DATA, "ipo.json"), "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    json.dump(cache, open(cache_path, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    cols = ["name", "code", "market", "industry", "lead", "fcst", "listdate", "band_lo", "band_hi", "offer", "pricing", "inst",
            "lockup", "retail", "amount", "listed", "float_pct", "open", "r_open", "r0", "r15", "r1m", "r3m", "r6m", "cur", "r_cur", "maxc", "dd", "excess"]
    with open(os.path.join(DATA, "ipo.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f); w.writerow(cols)
        for r in rows: w.writerow([r.get(c) for c in cols])
    tpl = open(os.path.join(ROOT, "template.html"), encoding="utf-8").read()
    html = tpl.replace("__DATA__", json.dumps(out, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/"))
    open(os.path.join(SITE, "index.html"), "w", encoding="utf-8").write(html)
    import shutil; shutil.copy(os.path.join(DATA, "ipo.csv"), os.path.join(SITE, "ipo.csv"))

    md = ["## 수집 결과", ""] + [f"- {x}" for x in LOG] + ["", f"### 경고 {len(WARN)}건", ""] + [f"- {x}" for x in WARN]
    print("\n".join(md))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8").write("\n".join(md) + "\n")
    if len(rows) < max(1, len(stocks) // 2):
        raise SystemExit("절반 이상 종목 처리 실패 — 사이트를 갱신하지 않습니다.")


if __name__ == "__main__":
    main()
