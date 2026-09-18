#!/usr/bin/env python3
"""보유 종목 자동 점검 파이프라인 — GitHub Actions에서 매 거래일 한 번 실행된다.

  보유내역(HOLDINGS_JSON 비밀) → 일봉 수집 → 지표 계산 → 지난번 대비 변화
  → site/index.html (비밀번호로 잠근 리포트), site/quotes-patch.enc (앱용 시세, 잠금)
  → state/state.enc (다음 실행 비교용, 잠금)

원칙
  - 값을 못 받으면 비워 두고 실패로 표시한다. 추정값이나 임의 숫자를 만들지 않는다.
  - 표시는 확인 항목이지 매매 지시가 아니다. 판단은 사람이 한다.
  - 공개 저장소·공개 로그에 보유내역이 남지 않는다.
      · REPORT_PASSWORD 가 없으면 파일을 만들기 전에 중단한다. 평문 경로는 없다.
      · 로그에는 순번·성공/실패 건수·오류 종류만 찍는다. 종목명·코드는 찍지 않는다.
      · 공개할 파일은 site/ 에만 만든다. 비교용 상태는 state/ 에 잠가서 둔다.
  - 전 종목 수집 실패면 아무것도 만들지 않고 실패 코드로 끝난다(이전 결과 유지).

로컬 시험:  python3 -m unittest test_pipeline -v
"""
import base64, hashlib, json, math, os, re, sys, time, urllib.parse, urllib.request, urllib.error
from datetime import date, datetime, timezone, timedelta
from zoneinfo import ZoneInfo

UTC = timezone.utc
KST = ZoneInfo("Asia/Seoul")
NYT = ZoneInfo("America/New_York")
BARS_KEEP = 260
LOOKBACK = 252                       # '직전 252거래일' — 이 봉 수 + 오늘 봉이 있어야만 계산
CLOSE_TIME = {"KR": (15, 30), "US": (16, 0)}     # 정규장 마감 시각 (시세 파일의 quoteAsOf)
SETTLE_MIN = {"KR": 10, "US": 20}                # 마감 후 이 분이 지나야 당일 봉을 완결로 본다
PATCH_EXCLUDE_TODAY = False           # 앱은 당일 봉을 거부하므로 시세 파일에는 전일까지만 넣는다

# ============================================================
# 1. 지표 — 순수 함수. 같은 입력에는 항상 같은 결과.
# ============================================================
def sma(v, n): return sum(v[-n:])/n if len(v) >= n else None

def sma_series(v, n):
    return [sum(v[i-n+1:i+1])/n if i >= n-1 else None for i in range(len(v))]

def ema_series(v, n):
    out = [None]*len(v)
    if len(v) < n: return out
    k = 2/(n+1); out[n-1] = sum(v[:n])/n
    for i in range(n, len(v)): out[i] = v[i]*k + out[i-1]*(1-k)
    return out

def rsi(c, n=14):
    """와일더 RSI. 상승·하락이 모두 0(가격 불변)이면 중립 50. 하락만 0이면 100, 상승만 0이면 0."""
    if len(c) < n+1: return None
    g = l = 0.0
    for i in range(1, n+1):
        d = c[i]-c[i-1]; g += max(d, 0); l += max(-d, 0)
    ag, al = g/n, l/n
    for i in range(n+1, len(c)):
        d = c[i]-c[i-1]; ag = (ag*(n-1)+max(d, 0))/n; al = (al*(n-1)+max(-d, 0))/n
    if ag == 0 and al == 0: return 50.0
    if al == 0: return 100.0
    return 100 - 100/(1+ag/al)

def macd(c, fast=12, slow=26, sig=9):
    if len(c) < slow: return {"macd": None, "signal": None, "hist": None, "cross": None}
    ef, es = ema_series(c, fast), ema_series(c, slow)
    line = [ef[i]-es[i] for i in range(len(c)) if ef[i] is not None and es[i] is not None]
    sg = ema_series(line, sig)
    if sg[-1] is None: return {"macd": line[-1], "signal": None, "hist": None, "cross": None}
    cross = None
    if len(sg) >= 2 and sg[-2] is not None:
        was, now = line[-2]-sg[-2], line[-1]-sg[-1]
        cross = "golden" if was <= 0 < now else "dead" if was >= 0 > now else None
    return {"macd": line[-1], "signal": sg[-1], "hist": line[-1]-sg[-1], "cross": cross}

def atr(h, l, c, n=14):
    if len(c) < n+1: return None
    tr = [max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])) for i in range(1, len(c))]
    a = sum(tr[:n])/n
    for t in tr[n:]: a = (a*(n-1)+t)/n
    return a

def vol_ratio(v, n=20):
    """오늘 거래량 / 직전 n일 평균. 창 안에 누락(None)이 하나라도 있으면 계산하지 않는다(0으로 대체하지 않음)."""
    if len(v) < n+1: return None
    w = v[-n-1:]
    if any(x is None for x in w): return None
    base = sum(w[:-1])/n
    return w[-1]/base if base else None

def high_excl_today(h, n=LOOKBACK):
    """직전 n거래일(오늘 제외) 최고가. 봉이 n+1개 미만이면 None — 짧은 이력을 n일 돌파선으로 부르지 않는다."""
    if len(h) < n+1: return None
    return max(h[-(n+1):-1])

def high_avail_excl_today(h):
    """자료가 있는 구간의 오늘 제외 최고가와 그 봉 수. 252봉이 안 되는 신규 종목용 '참고값'."""
    w = h[:-1]
    return (max(w), len(w)) if w else (None, 0)

def sma_cross(c, n):
    s = sma_series(c, n)
    if len(c) < n+1 or s[-1] is None or s[-2] is None: return None
    was, now = c[-2]-s[-2], c[-1]-s[-1]
    return "golden" if was <= 0 < now else "dead" if was >= 0 > now else None

def compute(bars):
    c = [b["close"] for b in bars]; h = [b["high"] for b in bars]
    l = [b["low"] for b in bars]; v = [b["volume"] for b in bars]
    m = macd(c); full = len(bars) >= LOOKBACK
    ha, hn = high_avail_excl_today(h)
    return {"asOf": bars[-1]["date"], "bars": len(bars), "close": c[-1],
            "prevClose": c[-2] if len(c) > 1 else None,
            "sma20": sma(c, 20), "sma60": sma(c, 60), "sma120": sma(c, 120), "sma200": sma(c, 200),
            "rsi14": rsi(c), "macd": m["macd"], "macdSignal": m["signal"], "macdHist": m["hist"], "macdCross": m["cross"],
            "atr14": atr(h, l, c), "volRatio20": vol_ratio(v), "volumeMissing": any(x is None for x in v[-21:]),
            "high252ExclToday": high_excl_today(h, LOOKBACK),
            "highAvailExclToday": ha, "highAvailBars": hn,
            "high52w": max(h[-LOOKBACK:]) if full else None, "low52w": min(l[-LOOKBACK:]) if full else None,
            "cross20": sma_cross(c, 20), "cross60": sma_cross(c, 60)}

# ============================================================
# 2. 수집 — 야후(기본) + 공공데이터포털(키 있을 때, 국내 기준 데이터)
# ============================================================
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
KR_CODE = re.compile(r"^[0-9A-Z]{6}$")          # 숫자 6자리뿐 아니라 0153K0 같은 새 형식도 허용

_HOST_BLOCKS = {}
_HOST_TIMEOUTS = {}
class ProviderHTTPError(ValueError):
    def __init__(self, code, cached=False):
        self.code = code
        super().__init__(f"HTTP {code}" + (" (이번 실행 추가 요청 중지)" if cached else ""))

def _get(url, params=None, retries=2):
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname
    if host == "apis.data.go.kr": host += parsed.path.rsplit("/",1)[0]
    if _HOST_TIMEOUTS.get(host,0) >= 2: raise ValueError("제공자 연결 실패 반복: 이번 실행 추가 요청 중지")
    if host in _HOST_BLOCKS: raise ProviderHTTPError(_HOST_BLOCKS[host], True)
    if params: url += "?" + urllib.parse.urlencode(params, safe="%")
    for i in range(min(retries, 2)):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=10) as r:
                return r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            code = e.code
            e.close()
            if code in (401,403,429): _HOST_BLOCKS[host] = code
            if code < 500 or i == min(retries,2)-1: raise ProviderHTTPError(code) from None
        except (urllib.error.URLError, TimeoutError):
            _HOST_TIMEOUTS[host] = _HOST_TIMEOUTS.get(host,0)+1
            if i == min(retries,2)-1: raise ValueError("네트워크 연결 실패 또는 10초 시간 초과") from None
        if i < min(retries,2)-1: time.sleep(1)

def yahoo(symbol, market):
    raw = _get("https://query1.finance.yahoo.com/v8/finance/chart/"+urllib.parse.quote(symbol, safe=""),
               {"range": "2y", "interval": "1d"})
    res = json.loads(raw).get("chart", {}).get("result")
    if not res: raise ValueError("야후 응답 없음")
    r = res[0]; meta = r.get("meta", {})
    if meta.get("symbol", "").upper() != symbol.upper(): raise ValueError("응답 심볼 불일치")
    if meta.get("currency") != ("KRW" if market == "KR" else "USD"): raise ValueError("통화 불일치 "+str(meta.get("currency")))
    zone = ZoneInfo(meta.get("exchangeTimezoneName") or ("Asia/Seoul" if market == "KR" else "America/New_York"))
    q = r["indicators"]["quote"][0]
    bars = [{"date": datetime.fromtimestamp(t, zone).date().isoformat(),
             **{k: q[k][j] for k in ("open", "high", "low", "close", "volume")}}
            for j, t in enumerate(r.get("timestamp") or [])]
    return bars, {"name": meta.get("longName") or meta.get("shortName") or "", "exchange": meta.get("fullExchangeName") or ""}

_TD_LAST_REQUEST = 0.0

def twelvedata(symbol, key):
    """Authenticated US daily history, paced to at most eight calls/minute."""
    global _TD_LAST_REQUEST
    if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,14}", symbol): raise ValueError("미국 심볼 형식 오류")
    if "api.twelvedata.com" in _HOST_BLOCKS:
        raise ProviderHTTPError(_HOST_BLOCKS["api.twelvedata.com"], True)
    delay = 8.1 - (time.monotonic() - _TD_LAST_REQUEST)
    if delay > 0: time.sleep(delay)
    _TD_LAST_REQUEST = time.monotonic()
    raw = _get("https://api.twelvedata.com/time_series", {
        "symbol": symbol, "country": "United States", "interval": "1day",
        "outputsize": 300, "timezone": "Exchange", "apikey": key}, retries=1)
    d = json.loads(raw)
    if d.get("status") == "error":
        code = d.get("code")
        if code in (401,403,429): _HOST_BLOCKS["api.twelvedata.com"] = code
        raise ValueError("Twelve Data 오류 " + str(code if isinstance(code,int) else "응답"))
    meta = d.get("meta", {})
    if meta.get("symbol", "").upper() != symbol or meta.get("currency") != "USD":
        raise ValueError("Twelve Data 심볼·통화 불일치")
    rows = []
    try:
        for r in d.get("values", []):
            rows.append({"date": r["datetime"],
                **{k:float(r[k]) for k in ("open", "high", "low", "close")},
                "volume": float(r["volume"]) if r.get("volume") not in (None, "") else None})
    except (KeyError, TypeError, ValueError): raise ValueError("Twelve Data 일봉 형식 오류") from None
    return rows

GOV = "https://apis.data.go.kr/1160100/"
def gov(code, is_etf, key):
    path = "GetSecuritiesProductInfoService_V2/getETFPriceInfo_V2" if is_etf else "GetStockSecuritiesInfoService_V2/getStockPriceInfo_V2"
    rows = []
    for page in range(1, 11):
        raw = _get(GOV+path, {"serviceKey": urllib.parse.unquote(key), "resultType": "json", "numOfRows": 600, "pageNo": page,
                              "likeSrtnCd": code, "beginBasDt": (datetime.now(UTC)-timedelta(days=420)).strftime("%Y%m%d")})
        if raw.lstrip().startswith("<"): raise ValueError("공공데이터 인증 오류: "+re.sub(r"<[^>]+>", " ", raw)[:80].strip())
        d = json.loads(raw).get("response", {}); hd = d.get("header", {})
        if str(hd.get("resultCode")) not in ("00", "0"): raise ValueError("공공데이터 오류 코드 "+str(hd.get("resultCode")))
        body = d.get("body", {}); items = (body.get("items") or {}).get("item", [])
        if isinstance(items, dict): items = [items]
        for r in items:
            if str(r.get("srtnCd", "")).lstrip("A") != code: raise ValueError("공공데이터 코드 불일치")
            rows.append({"date": r["basDt"], **{k: float(r[v]) for k, v in
                        {"open": "mkp", "high": "hipr", "low": "lopr", "close": "clpr", "volume": "trqu"}.items()}})
        if page*600 >= int(body.get("totalCount", 0)): break
    return rows

def _day(s):
    s = str(s); return datetime.strptime(s, "%Y%m%d" if re.fullmatch(r"\d{8}", s) else "%Y-%m-%d").date().isoformat()

def market_zone(market): return KST if market == "KR" else NYT

def market_closed(market, now):
    """정규장 마감 + 정산 여유가 지났는가."""
    local = now.astimezone(market_zone(market)); hh, mm = CLOSE_TIME[market]
    return (local.hour, local.minute) >= (hh, mm + SETTLE_MIN[market])

def completed_cutoff(market, now):
    """완결 일봉으로 인정할 마지막 날짜(현지). 마감 후면 당일, 아니면 전일."""
    local = now.astimezone(market_zone(market)).date()
    return local.isoformat() if market_closed(market, now) else (local-timedelta(days=1)).isoformat()

def clean(bars, market, now=None):
    """완결된 일봉만. 값이 전혀 없는 날은 건너뛰고 개수를 기록. 일부만 비었거나 고저가 어긋나면 거부.
    거래량만 없는 날은 volume=None 으로 남긴다(0과 구분)."""
    now = now or datetime.now(UTC)
    cutoff = completed_cutoff(market, now)
    rows = sorted([(_day(b["date"]), b) for b in bars if _day(b["date"]) <= cutoff], key=lambda x: x[0])
    out, dates, empty = [], set(), 0
    for d, b in reversed(rows):
        if len(out) >= BARS_KEEP: break
        if d in dates: raise ValueError("중복 날짜 "+d)
        ohlc = [b.get(k) for k in ("open", "high", "low", "close")]
        if all(x is None for x in ohlc): empty += 1; continue
        if not all(isinstance(x, (int, float)) and math.isfinite(x) for x in ohlc): raise ValueError("일부 값 누락 "+d)
        vol = b.get("volume")
        if vol is not None and not (isinstance(vol, (int, float)) and math.isfinite(vol) and vol >= 0): raise ValueError("거래량 값 이상 "+d)
        o, h, l, c = ohlc
        if min(o, h, l, c) <= 0 or h < max(o, l, c) or l > min(o, c): raise ValueError("고가·저가 불일치 "+d)
        dates.add(d); out.append({"date": d, "open": o, "high": h, "low": l, "close": c, "volume": vol})
    out.sort(key=lambda b: b["date"])
    return out, empty

def fetch_security(code, mkt, is_etf, gov_key, now=None):
    """한 종목(코드) 수집. 국내는 공공데이터(키 있으면)를 우선 사용하고 실패할 때 야후를 시도한다.
    오류 문구에 종목명·코드를 넣지 않는다(공개 로그에 찍힐 수 있다)."""
    code = (code or "").strip()
    rec = {"code": code, "mkt": mkt, "warnings": []}
    if not code: raise ValueError("종목코드 없음")
    if mkt not in ("KR", "US"): raise ValueError("시장 구분 이상")
    if mkt == "KR" and not KR_CODE.match(code): raise ValueError("국내 코드 형식 아님")
    bars, source, sym, empty = [], None, None, 0
    if mkt == "US":
        key = os.environ.get("TWELVE_DATA_API_KEY", "").strip()
        if key:
            raw = twelvedata(code, key)
            bars, empty = clean(raw, "US", now)
            source, sym = "twelvedata", code
            if bars and (now or datetime.now(UTC)).astimezone(NYT).date() - datetime.fromisoformat(bars[-1]["date"]).date() > timedelta(days=7):
                raise ValueError("Twelve Data 최신 일봉 지연")
        else:
            raw, meta = yahoo(code, "US")
            bars, empty = clean(raw, "US", now)
            source, sym = "yahoo", code
            rec["providerName"] = meta["name"]
    else:
        gov_bars = []
        gov_failure = "키 미설정" if not gov_key else "유효 일봉 없음"
        if gov_key:
            try:
                raw_gov = gov(code, is_etf, gov_key)
                try:
                    gov_bars, empty = clean(raw_gov, "KR", now)
                except ValueError as e:
                    # Never bridge a malformed historical candle: retain only the
                    # validated consecutive suffix after it, and disclose truncation.
                    bad = re.fullmatch(r"(?:고가·저가 불일치|일부 값 누락|거래량 값 이상) (\d{4}-\d{2}-\d{2})", str(e))
                    if not bad: raise
                    recent = [b for b in raw_gov if _day(b["date"]) > bad.group(1)]
                    gov_bars, empty = clean(recent, "KR", now)
                    if len(gov_bars) < 2: raise e
                    rec["warnings"].append("공공데이터 과거 일봉 오류: " + bad.group(1) + "까지 제외. 이후 " + str(len(gov_bars)) + "봉만으로 계산")
                source = "data.go.kr"
                if len(gov_bars) >= 2:
                    rec.update({"symbol":code,"source":source,"emptyBarsSkipped":empty,"indicators":compute(gov_bars),"bars":gov_bars[-BARS_KEEP:]})
                    return rec
            except Exception as e:
                if isinstance(e, ProviderHTTPError): gov_failure = str(e)
                elif isinstance(e, ValueError) and "공공데이터 인증 오류" in str(e):
                    gov_failure = "인증 응답 오류"
                    for known in ("SERVICE_KEY_IS_NOT_REGISTERED_ERROR", "SERVICE_ACCESS_DENIED_ERROR", "LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR", "SERVICE_KEY_IS_NOT_REGISTERED", "DEADLINE_HAS_EXPIRED_ERROR"):
                        if known in str(e): gov_failure += " " + known; break
                elif str(e) in ("네트워크 연결 실패 또는 10초 시간 초과", "제공자 연결 실패 반복: 이번 실행 추가 요청 중지"): gov_failure = str(e)
                elif re.fullmatch(r"(고가·저가 불일치|일부 값 누락|거래량 값 이상|중복 날짜) \d{4}-\d{2}-\d{2}", str(e)): gov_failure = str(e)
                else: gov_failure = type(e).__name__ + " (응답 형식 또는 일봉 검증 실패)"
                rec["warnings"].append("공공데이터 실패: " + gov_failure)
        y_bars, y_meta, err = [], {}, None
        for suf in (".KS", ".KQ"):
            try:
                raw, y_meta = yahoo(code+suf, "KR"); y_bars, y_empty = clean(raw, "KR", now); sym = code+suf; break
            except Exception as e:
                err = e
        if gov_bars:
            bars = gov_bars
            if y_bars and y_bars[-1]["date"] > gov_bars[-1]["date"]:     # 공공데이터는 T+1 지연 → 최근 봉을 야후로 보충
                rec["warnings"].append("야후에 더 최근 봉이 있으나 가격 조정 기준 혼합을 피하려고 공공데이터 이력을 유지합니다")
        elif y_bars:
            bars, source, empty = y_bars, "yahoo", y_empty
        else:
            raise ValueError("공공데이터: " + gov_failure + " / 야후: "+(str(err) if isinstance(err, ProviderHTTPError) else type(err).__name__ if err else "응답 없음"))
        rec["providerName"] = y_meta.get("name", "")
    if len(bars) < 2: raise ValueError("일봉 부족")
    rec.update({"symbol": sym or code, "source": source, "emptyBarsSkipped": empty,
                "indicators": compute(bars), "bars": bars[-BARS_KEEP:]})
    return rec

# ============================================================
# 3. 보유내역 — 형식·집계
# ============================================================
DEFAULT_LIMITS = {"은": 10, "비트코인": 10, "고변동성": 12, "바이오": 25, "초기 바이오": 6, "빅테크": 25}
CLASS_TAGS = {"국내 개별주", "미국주식", "국내 지수·배당", "국내 중소형", "배당"}

def sec_key(it):
    code = (it.get('code') or '').strip()
    return f"{it.get('mkt','KR')}:{code}" if code else f"missing:{it.get('id') or it.get('name')}"

def parse_holdings(obj):
    """HOLDINGS_JSON 은 (a) 종목 목록, 또는 (b) {"holdings":[...], "limits":{태그:한도%}, "usdkrw":환율}.
    앱의 {items, rate, limits, baseLimit} 내보내기도 지원하며 id를 유지한다."""
    if isinstance(obj, list): obj = {"holdings": obj}
    if isinstance(obj, dict) and "items" in obj:
        obj = {**obj, "holdings": obj["items"], "usdkrw": obj.get("rate")}
    if isinstance(obj, dict) and isinstance(obj.get("holdings"), list):
        obj = {**obj, "holdings": [dict(it, isETF=it.get("isETF", bool(re.match(r"^(KODEX|TIGER|ACE|PLUS|RISE|KoAct|SOL|HANARO|ARIRANG)\b", it.get("name", ""), re.I))), stop=it.get("stop") if it.get("stop") is not None else it.get("stopAnchor")) for it in obj["holdings"]]}
    if not isinstance(obj, dict) or not isinstance(obj.get("holdings"), list): raise ValueError("보유내역 형식 이상")
    limits = {t: float(obj.get("baseLimit", 15)) for it in obj["holdings"] for t in it.get("tags", [])} if "schemaVersion" in obj else dict(DEFAULT_LIMITS); limits.update({k: float(v) for k, v in (obj.get("limits") or {}).items()})
    return obj["holdings"], limits, obj.get("usdkrw")

def aggregate(holdings):
    """같은 종목을 여러 계좌에서 보유해도 시세는 한 번만 받고, 카드는 종목 단위로 합친다."""
    secs = {}
    for it in holdings:
        if not (it.get("qty") or 0) > 0: continue
        k = sec_key(it); s = secs.setdefault(k, {"key": k, "name": it["name"], "code": (it.get("code") or "").strip(),
                                                "mkt": it.get("mkt", "KR"), "isETF": bool(it.get("isETF")),
                                                "qty": 0, "cost": 0.0, "accts": [], "tags": [], "stop": None, "ids": []})
        s["qty"] += it["qty"]; s["cost"] += (it.get("buy") or 0)*it["qty"]
        if it.get("acct") and it["acct"] not in s["accts"]: s["accts"].append(it["acct"])
        for t in it.get("tags", []):
            if t not in s["tags"]: s["tags"].append(t)
        if it.get("stop"): s["stop"] = it["stop"] if s["stop"] is None else max(s["stop"], it["stop"])
        if it.get("id"): s["ids"].append(it["id"])
    for s in secs.values(): s["buy"] = s["cost"]/s["qty"] if s["qty"] else None
    return list(secs.values())

# ============================================================
# 4. 확인 항목 — 계산된 값으로만 만든다. 매매 지시가 아니다.
# ============================================================
def signals(rec, prev, sec, share_over):
    """(우선순위, 문구). 낮을수록 위. prev = 지난 실행의 같은 종목 요약."""
    d = rec["indicators"]; p = d["close"]; out = []; m = rec["mkt"]
    stop = sec.get("stop")
    if stop and p <= stop: out.append((1, f"이탈 기준가 {fmt(stop, m)} 아래로 마감"))
    if prev and prev.get("close") and prev["close"] > 0 and prev.get("asOf") != d["asOf"]:
        chg = (p/prev["close"]-1)*100
        if abs(chg) >= 5: out.append((3 if chg < 0 else 4, f"지난 확인({prev.get('asOf')}) 대비 {chg:+.1f}%"))
    hb = d["high252ExclToday"]
    if hb and p > hb and (not prev or not prev.get("aboveHigh")):
        vr = f" — 거래량 {d['volRatio20']:.1f}배" if d["volRatio20"] else ""
        out.append((2, f"직전 {LOOKBACK}거래일 최고가 {fmt(hb, m)} 위에서 마감"+vr))
    if hb and p <= hb and prev and prev.get("aboveHigh"): out.append((3, f"{LOOKBACK}거래일 최고가 아래로 되돌아옴"))
    if d["cross20"] == "dead": out.append((3, "20일선 아래로 마감 (전일은 위)"))
    if d["cross20"] == "golden": out.append((4, "20일선 위로 마감 (전일은 아래)"))
    if d["cross60"] == "dead": out.append((2, "60일선 아래로 마감"))
    if d["cross60"] == "golden": out.append((4, "60일선 위로 마감"))
    if d["macdCross"] == "dead": out.append((4, "MACD 데드크로스"))
    if d["macdCross"] == "golden": out.append((5, "MACD 골든크로스"))
    if d["volRatio20"] and d["volRatio20"] >= 2: out.append((4, f"거래량 20일 평균의 {d['volRatio20']:.1f}배"))
    r = d["rsi14"]
    if r is not None:
        if r <= 30 and (not prev or (prev.get("rsi") or 50) > 30): out.append((4, f"RSI {r:.0f} — 과매도 구간 진입 (바닥 신호는 아님)"))
        if r >= 70 and (not prev or (prev.get("rsi") or 50) < 70): out.append((5, f"RSI {r:.0f} — 과매수 구간 진입"))
    dd = (p/d["high52w"]-1)*100 if d["high52w"] else None
    if dd is not None and dd <= -20 and (not prev or (prev.get("dd") or 0) > -20): out.append((3, f"52주 고점 대비 {dd:.0f}% — 하락폭 20% 넘음"))
    for t in share_over: out.append((2, f"위험 태그 '{t}' 비중이 한도를 넘음"))
    return out

# ============================================================
# 5. 리포트
# ============================================================
def fmt(v, mkt):
    if v is None: return "—"
    return f"${v:,.2f}" if mkt == "US" else f"{v:,.0f}"

def pct(v): return "—" if v is None else f"{v:+.1f}%"
def esc(s): return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

def spark(bars, w=120, h=32):
    c = [b["close"] for b in bars[-60:]]
    if len(c) < 2: return ""
    lo, hi = min(c), max(c); span = (hi-lo) or 1
    pts = " ".join(f"{i*w/(len(c)-1):.1f},{h-(v-lo)/span*(h-2)-1:.1f}" for i, v in enumerate(c))
    color = "#F2453D" if c[-1] >= c[0] else "#3D7BF2"
    return f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}"><polyline fill="none" stroke="{color}" stroke-width="1.5" points="{pts}"/></svg>'

CSS = """
:root{--bg:#0F1621;--card:#17202E;--chip:#1D2838;--line:#26324A;--ink:#E7ECF5;--mute:#8A97AE;--up:#F2453D;--down:#3D7BF2;--hit:#F5A524;--ok:#4FB286}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,"Apple SD Gothic Neo","Noto Sans KR","Malgun Gothic",sans-serif;-webkit-font-smoothing:antialiased}
.wrap{max-width:760px;margin:0 auto;padding:18px 14px 60px}h1{font-size:19px;margin:0 0 4px}h2{font-size:15px;margin:22px 0 10px;font-weight:700}
.stamp{color:var(--mute);font-size:12.5px;line-height:1.6}.num{font-variant-numeric:tabular-nums}
.card{background:var(--card);border-radius:12px;padding:13px 14px;margin-bottom:9px}
.sig{display:grid;grid-template-columns:8px 1fr;gap:9px;align-items:start;padding:8px 0;border-bottom:1px solid var(--line);font-size:13.5px}
.sig:last-child{border:0}.sig i{width:8px;height:8px;border-radius:50%;margin-top:6px;background:var(--mute)}
.p1 i,.p2 i{background:var(--down)}.p3 i{background:var(--hit)}.p4 i,.p5 i{background:var(--ok)}
.sig b{font-weight:650}.sig small{display:block;color:var(--mute);font-size:11.5px}
.row{display:grid;grid-template-columns:1fr auto;gap:10px;align-items:center}.name{font-weight:650}.sub{color:var(--mute);font-size:12px}
.price{font-size:19px;font-weight:700;text-align:right}.up{color:var(--up)}.down{color:var(--down)}
.chips{display:flex;flex-wrap:wrap;gap:5px;margin-top:9px}.chip{font-size:11.5px;color:var(--mute);background:var(--chip);padding:3px 7px;border-radius:5px}
.chip b{color:var(--ink);font-weight:600;margin-left:3px}.chip.warn{color:var(--hit)}
.bar{height:6px;background:var(--chip);border-radius:3px;overflow:hidden;margin-top:4px}.bar i{display:block;height:100%;border-radius:3px}
.tag{display:flex;justify-content:space-between;font-size:13px;margin-top:8px}.over{color:var(--hit)}.hold{color:var(--hit);font-size:12.5px;margin-bottom:6px}
details summary{cursor:pointer;color:var(--mute);font-size:13px;padding:6px 0}
.note{font-size:12px;color:var(--mute);line-height:1.65;border-top:1px solid var(--line);padding-top:12px;margin-top:22px}
.btn{display:inline-block;border:1px solid var(--line);background:var(--chip);color:var(--ink);padding:8px 13px;border-radius:8px;font-size:13.5px;text-decoration:none;cursor:pointer}
@media(max-width:400px){.price{font-size:17px}}
"""

def valuation(secs, records, prev_state, rate):
    """종목별 평가액. 이번에 실패한 종목은 지난 상태의 종가로 대체(표시), 그것도 없으면 미평가.
    분모는 '보유 전체'다. 수집 성공분만 분모로 쓰면 실패한 큰 종목 때문에 나머지 비중이 부풀려진다."""
    by_key = {r["key"]: r for r in records}
    prev = (prev_state or {}).get("securities", {})
    out = {}
    for s in secs:
        r = by_key.get(s["key"]); fx = rate if s["mkt"] == "US" else 1
        if r and "indicators" in r:
            out[s["key"]] = {"krw": r["indicators"]["close"]*s["qty"]*fx, "basis": "current", "asOf": r["indicators"]["asOf"]}
        elif prev.get(s["key"], {}).get("close"):
            p = prev[s["key"]]
            out[s["key"]] = {"krw": p["close"]*s["qty"]*fx, "basis": "stale", "asOf": p.get("asOf")}
        else:
            out[s["key"]] = {"krw": None, "basis": "none", "asOf": None}
    return out

def render(secs, records, prev_state, limits, rate, meta):
    ok = [r for r in records if "indicators" in r]
    fails = [(r["name"], r["error"]) for r in records if "error" in r]
    by_key = {s["key"]: s for s in secs}
    val = valuation(secs, records, prev_state, rate)
    valued = {k: v for k, v in val.items() if v["krw"] is not None}
    total = sum(v["krw"] for v in valued.values())
    unvalued = [by_key[k]["name"] for k, v in val.items() if v["krw"] is None]
    stale = [by_key[k]["name"] for k, v in val.items() if v["basis"] == "stale"]
    complete = not unvalued                     # 전 종목 평가액이 있어야 비중 판정을 한다
    tags = {}
    for k, v in valued.items():
        for t in by_key[k]["tags"]: tags[t] = tags.get(t, 0)+v["krw"]
    over = {t for t, v in tags.items() if complete and t not in CLASS_TAGS and total and v/total*100 > limits.get(t, 15)}
    prev_secs = (prev_state or {}).get("securities", {})

    sig_rows = []
    for r in ok:
        s = by_key[r["key"]]; prev = prev_secs.get(r["key"])
        for pr, text in signals(r, prev, s, [t for t in s["tags"] if t in over]):
            sig_rows.append((pr, s["name"], text, r["indicators"]["asOf"]))
    sig_rows.sort(key=lambda x: (x[0], x[1]))

    def sig_html(rows):
        return "".join(f'<div class="sig p{pr}"><i></i><div><b>{esc(n)}</b> {esc(t)}<small>{a} 마감 기준</small></div></div>' for pr, n, t, a in rows)
    head = sig_rows[:8]; rest = sig_rows[8:]

    cards = []
    for r in sorted(ok, key=lambda r: val[r["key"]]["krw"] or 0, reverse=True):
        d = r["indicators"]; s = by_key[r["key"]]; m = r["mkt"]
        chg = (d["close"]/d["prevClose"]-1)*100 if d["prevClose"] else None
        ret = (d["close"]/s["buy"]-1)*100 if s.get("buy") else None
        rel = lambda x: pct((d["close"]/x-1)*100) if x else "—"
        share = val[r["key"]]["krw"]/total*100 if total else 0
        atrp = d["atr14"]/d["close"]*100 if d["atr14"] else None
        warn = "".join(f'<span class="chip warn">{esc(w)}</span>' for w in r.get("warnings", []))
        if d["high252ExclToday"]:
            hi_chip = f'<span class="chip">{LOOKBACK}일 돌파선<b class="num">{rel(d["high252ExclToday"])}</b></span><span class="chip">52주 고점<b class="num">{rel(d["high52w"])}</b></span>'
        else:
            hi_chip = f'<span class="chip warn">이력 {d["highAvailBars"]}봉 최고가<b class="num">{rel(d["highAvailExclToday"])}</b> ({LOOKBACK}일 돌파선 아님)</span>'
        vol_chip = f"{d['volRatio20']:.1f}배" if d["volRatio20"] else ("거래량 누락" if d["volumeMissing"] else "—")
        cards.append(f"""<div class="card"><div class="row"><div><span class="name">{esc(s['name'])}</span>
          <div class="sub">{esc(' · '.join(s['accts']))} · {esc(r['symbol'])} · 비중 {share:.1f}%{' · 평단 대비 '+pct(ret) if ret is not None else ''}</div></div>
          <div><div class="price num {'up' if (chg or 0)>0 else 'down' if (chg or 0)<0 else ''}">{fmt(d['close'], m)}</div>
          <div class="sub num" style="text-align:right">{pct(chg)} · {d['asOf']}</div></div></div>
          <div style="margin-top:8px">{spark(r['bars'])}</div>
          <div class="chips">
            <span class="chip">20일선<b class="num">{rel(d['sma20'])}</b></span><span class="chip">60일선<b class="num">{rel(d['sma60'])}</b></span>
            <span class="chip">120일선<b class="num">{rel(d['sma120'])}</b></span>{hi_chip}
            <span class="chip">거래량<b class="num">{vol_chip}</b></span>
            <span class="chip">RSI<b class="num">{f"{d['rsi14']:.0f}" if d['rsi14'] is not None else '—'}</b></span>
            <span class="chip">MACD<b>{'골든' if d['macdCross']=='golden' else '데드' if d['macdCross']=='dead' else ('+' if (d['macdHist'] or 0)>0 else '−')}</b></span>
            <span class="chip">ATR<b class="num">{f"{atrp:.1f}%" if atrp else '—'}</b></span>
            {'<span class="chip">이탈가<b class="num">'+fmt(s['stop'], m)+'</b></span>' if s.get('stop') else ''}
            {warn}</div></div>""")

    if complete:
        hold_html = ""
    else:
        hold_html = f'<div class="hold">비중 판정 보류 — 평가액 없는 종목 {len(unvalued)}건({esc(", ".join(unvalued))}). 아래 비중은 일부 자료 기준이며 한도 초과 경고를 내지 않았습니다.</div>'
    if stale:
        hold_html += f'<div class="sub" style="margin-bottom:6px">지난 종가로 대체한 종목 {len(stale)}건: {esc(", ".join(stale))}</div>'
    tag_html = "".join(
        f'<div class="tag"><span>{esc(t)}<span class="sub"> · 한도 {limits.get(t,15):g}%</span></span><span class="num {"over" if t in over else ""}">{v/total*100:.1f}%</span></div>'
        f'<div class="bar"><i style="width:{min(v/total*100,100):.1f}%;background:{"var(--hit)" if t in over else "#5FA8D3"}"></i></div>'
        for t, v in sorted(tags.items(), key=lambda x: -x[1]) if t not in CLASS_TAGS and total)

    ids_missing = sum(1 for s in secs if not s["ids"])
    patch_js = """<script>
const b64=s=>Uint8Array.from(atob(s),c=>c.charCodeAt(0));
async function dl(){const b=document.getElementById('dl');b.textContent='받는 중…';
 try{const r=await fetch('quotes-patch.enc',{cache:'no-store'});if(!r.ok)throw new Error(r.status);
  const blob=await r.json();const pw=localStorage.getItem('pw')||sessionStorage.getItem('pw');if(!pw)throw new Error('비밀번호 없음');
  const key=await crypto.subtle.importKey('raw',new TextEncoder().encode(pw),'PBKDF2',false,['deriveKey']);
  const k=await crypto.subtle.deriveKey({name:'PBKDF2',salt:b64(blob.salt),iterations:blob.iter,hash:'SHA-256'},key,{name:'AES-GCM',length:256},false,['decrypt']);
  const txt=new TextDecoder().decode(await crypto.subtle.decrypt({name:'AES-GCM',iv:b64(blob.iv)},k,b64(blob.ct)));
  const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([txt],{type:'application/json'}));a.download='quotes-patch.json';a.click();b.textContent='시세 패치 내려받기';}
 catch(e){b.textContent='실패: '+e.message;}}
</script>"""
    st = lambda s: (s or "—")[:16].replace("T", " ")
    return f"""<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>보유 종목 점검</title><style>{CSS}</style></head><body><div class="wrap">
<h1>보유 종목 점검</h1>
<div class="stamp">시세 기준일 — 국내 {meta['asOf'].get('KR') or '—'} · 해외 {meta['asOf'].get('US') or '—'} (각 시장 마감)<br>
이번 실행 {st(meta['lastRunAt'])} UTC · 성공 {meta['ok']} / 실패 {meta['fail']} 종목 · 마지막 전종목 성공 {st(meta.get('lastFullSuccessAt'))} UTC<br>
환율 {rate:,.0f}원 가정{' · 새 봉 없음(휴장 또는 지연 가능)' if meta.get('noNewBars') else ''}</div>
<h2>오늘 확인할 것 <span class="sub">{len(sig_rows)}건</span></h2>
<div class="card">{sig_html(head) if head else '<div class="sub">계산 기준으로 새로 걸린 항목이 없습니다.</div>'}
{('<details><summary>나머지 '+str(len(rest))+'건</summary>'+sig_html(rest)+'</details>') if rest else ''}</div>
<h2>위험 태그 비중 <span class="sub">평가 {total/1e4:,.0f}만원 · {len(valued)}/{len(secs)}종목</span></h2><div class="card">{hold_html}{tag_html or '<div class="sub">태그 없음</div>'}</div>
<h2>종목 <span class="sub">평가액 순</span></h2>{''.join(cards)}
{('<h2>실패 '+str(len(fails))+'건</h2><div class="card">'+''.join(f'<div class="sig p3"><i></i><div><b>{esc(n)}</b> {esc(e)}</div></div>' for n,e in fails)+'</div>') if fails else ''}
<h2>다른 앱으로 보내기</h2><div class="card"><a class="btn" id="dl" onclick="dl()">시세 패치 내려받기</a>
<div class="sub" style="margin-top:8px">내 투자 데스크의 '시세 파일 가져오기'용. 전일까지의 완결 봉만 담고 종목별 id·collectedAt·마감 시각을 넣었습니다.{f' <span class="over">id 없는 종목 {ids_missing}건 — 앱에서 내보낸 보유내역(id 포함)으로 Secret을 갱신하세요.</span>' if ids_missing else ''}</div></div>
<p class="note">여기 표시되는 것은 계산된 값으로 만든 확인 항목이며 매매 지시가 아닙니다. 야후 데이터는 지연·누락이 있을 수 있고, 공공데이터포털은 영업일 하루 뒤 오후에 갱신됩니다.
값이 전혀 없는 날은 없는 날로 건너뛰었고, 일부만 빈 봉이 있는 종목은 실패로 남겼습니다. 보유 수량·평단은 증권사 API로 읽을 수 없어 저장된 보유내역을 기준으로 합니다. 매매 후에는 보유내역을 갱신하세요.
비중 분모는 보유 전체이며, 이번에 못 받은 종목은 지난 종가로 대체하거나(표시) 그것도 없으면 비중 판정을 보류합니다.</p>
</div>{patch_js}</body></html>"""

# ============================================================
# 6. 앱용 시세 파일 — 앱의 검증 규칙에 맞춘다
#    · 항목마다 앱 id · collectedAt
#    · quoteAsOf 는 그 날의 실제 마감 시각(국내 15:30+09:00, 미국 16:00 현지, 서머타임 자동)
#    · 당일 봉은 넣지 않는다(앱은 당일 봉 거부) → 전일 완결 봉 기준
# ============================================================
def close_stamp(date_iso, mkt):
    y, mo, d = map(int, date_iso.split("-")); hh, mm = CLOSE_TIME[mkt]
    return datetime(y, mo, d, hh, mm, tzinfo=market_zone(mkt)).isoformat()

def patch_bars(rec, now):
    local = now.astimezone(market_zone(rec["mkt"]))
    # Conservative regular close plus 30 minutes, matching the app validator.
    close_minute = 16*60 if rec["mkt"] == "KR" else 16*60+30
    ready = local.hour*60+local.minute >= close_minute
    cutoff = local.date() if ready else local.date()-timedelta(days=1)
    return [b for b in rec["bars"] if b["date"] <= cutoff.isoformat() and date.fromisoformat(b["date"]).weekday() < 5]

def build_patch(holdings, records, generated, now):
    by_key = {r["key"]: r for r in records}
    ups, fails = [], []
    for it in holdings:
        if not (it.get("qty") or 0) > 0: continue
        r = by_key.get(sec_key(it))
        if not r or "indicators" not in r:
            fails.append({"id": it.get("id"), "name": it["name"], "code": it.get("code"), "error": (r or {}).get("error", "미수집")}); continue
        bars = patch_bars(r, now)
        if len(bars)<2: fails.append({"id": it.get("id"), "name": it["name"], "code": it.get("code"), "error": "완결 봉 없음"}); continue
        last = bars[-1]
        ups.append({"id": it.get("id"), "matchBy": "id" if it.get("id") else "code", "name": it["name"], "code": it.get("code"), "mkt": it["mkt"],
                    "acct": it.get("acct"), "price": last["close"], "quoteDate": last["date"], "quoteAsOf": close_stamp(last["date"], it["mkt"]),
                    "quoteTimePrecision": "day", "quoteSource": r["source"]+" completed daily bar", "collectedAt": generated,
                    "bars": bars})
    return {"kind": "portfolio-quotes-v1", "collectedAt": generated, "barRule": "completed bars up to previous local trading day",
            "updates": ups, "failures": fails}

# ============================================================
# 7. 암호화 — 공개 페이지에 보유내역이 노출되지 않게
# ============================================================
def encrypt(plain: str, password: str) -> dict:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, iv = os.urandom(16), os.urandom(12)
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310_000, 32)
    ct = AESGCM(key).encrypt(iv, plain.encode("utf-8"), None)
    return {"v": 1, "iter": 310_000, "salt": base64.b64encode(salt).decode(), "iv": base64.b64encode(iv).decode(), "ct": base64.b64encode(ct).decode()}

def decrypt(blob: dict, password: str) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(blob["salt"]), blob["iter"], 32)
    return AESGCM(key).decrypt(base64.b64decode(blob["iv"]), base64.b64decode(blob["ct"]), None).decode("utf-8")

SHELL = """<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>보유 종목 점검</title><style>body{margin:0;background:#0F1621;color:#E7ECF5;font:15px -apple-system,"Apple SD Gothic Neo","Noto Sans KR",sans-serif;display:flex;min-height:100vh;align-items:center;justify-content:center}
.box{background:#17202E;padding:26px 22px;border-radius:14px;width:min(360px,92vw)}h1{font-size:17px;margin:0 0 6px}p{color:#8A97AE;font-size:13px;margin:0 0 16px}
input{width:100%;padding:11px;border-radius:8px;border:1px solid #26324A;background:#0F1621;color:#E7ECF5;font-size:16px}button{margin-top:10px;width:100%;padding:11px;border:0;border-radius:8px;background:#2B4B7E;color:#fff;font-size:15px;font-weight:600}
label{display:flex;gap:7px;align-items:center;color:#8A97AE;font-size:12.5px;margin-top:10px}.err{color:#F5A524;font-size:13px;min-height:18px;margin-top:8px}</style></head>
<body><div class="box"><h1>보유 종목 점검</h1><p>__STAMP__ 갱신. 비밀번호를 입력하면 이 기기에서만 풀립니다.</p>
<input id="pw" type="password" placeholder="비밀번호" autocomplete="current-password"><button id="go">열기</button>
<label><input type="checkbox" id="keep" style="width:auto"> 이 기기에서 기억</label><div class="err" id="err"></div></div>
<script>
const BLOB=__BLOB__;
const b64=s=>Uint8Array.from(atob(s),c=>c.charCodeAt(0));
async function open(pw){
  const key=await crypto.subtle.importKey("raw",new TextEncoder().encode(pw),"PBKDF2",false,["deriveKey"]);
  const k=await crypto.subtle.deriveKey({name:"PBKDF2",salt:b64(BLOB.salt),iterations:BLOB.iter,hash:"SHA-256"},key,{name:"AES-GCM",length:256},false,["decrypt"]);
  const pt=await crypto.subtle.decrypt({name:"AES-GCM",iv:b64(BLOB.iv)},k,b64(BLOB.ct));
  document.open();document.write(new TextDecoder().decode(pt));document.close();
}
async function go(){const pw=document.getElementById("pw").value;if(!pw)return;
  try{await open(pw);if(document.getElementById("keep")&&document.getElementById("keep").checked)localStorage.setItem("pw",pw);else sessionStorage.setItem("pw",pw);}
  catch(e){document.getElementById("err").textContent="비밀번호가 맞지 않습니다.";}}
document.getElementById("go").onclick=go;document.getElementById("pw").onkeydown=e=>{if(e.key==="Enter")go();};
const saved=localStorage.getItem("pw")||sessionStorage.getItem("pw");if(saved){open(saved).catch(()=>{localStorage.removeItem("pw");sessionStorage.removeItem("pw");});}
</script></body></html>"""

# ============================================================
# 8. 실행
# ============================================================
def load_holdings():
    raw = os.environ.get("HOLDINGS_JSON", "").strip()
    if raw: return parse_holdings(json.loads(raw))
    if os.path.exists("holdings.json"):
        with open("holdings.json", encoding="utf-8") as f: return parse_holdings(json.load(f))
    raise SystemExit("보유내역이 없습니다. HOLDINGS_JSON 비밀이 필요합니다.")

def load_state(path, password):
    if not os.path.exists(path): return None
    try:
        with open(path, encoding="utf-8") as f: return json.loads(decrypt(json.loads(f.read()), password))
    except Exception as e:
        print("이전 상태를 읽지 못해 비교를 건너뜁니다:", type(e).__name__); return None

def main(site_dir="site", state_dir="state", now=None):
    _HOST_BLOCKS.clear()
    _HOST_TIMEOUTS.clear()
    password = os.environ.get("REPORT_PASSWORD", "").strip()
    if len(password) < 8:
        raise SystemExit("REPORT_PASSWORD 비밀이 없거나 8자 미만입니다. 평문 게시는 지원하지 않으므로 아무 파일도 만들지 않고 중단합니다.")
    holdings, limits, usdkrw = load_holdings()
    gov_key = os.environ.get("DATA_GO_KR_KEY", "").strip()
    rate = float(os.environ.get("USDKRW") or usdkrw or 1380)
    now = now or datetime.now(UTC)
    generated = now.isoformat(timespec="seconds")
    os.makedirs(site_dir, exist_ok=True); os.makedirs(state_dir, exist_ok=True)
    # 예전 버전이 남겼을 수 있는 평문 파일은 제거한다(공개 폴더에 남지 않게)
    for legacy in ("state.json", "quotes-patch.json"):
        for d in (site_dir, state_dir, "docs"):
            p = os.path.join(d, legacy)
            if os.path.exists(p): os.remove(p); print("평문 잔재 삭제:", p)

    state_path = os.path.join(state_dir, "state.enc")
    prev_state = load_state(state_path, password)

    secs = aggregate(holdings)
    records = []
    for i, s in enumerate(secs, 1):
        try:
            rec = fetch_security(s["code"], s["mkt"], s["isETF"], gov_key, now); rec["key"] = s["key"]; rec["name"] = s["name"]; records.append(rec)
            print(f"[{i:2d}/{len(secs)}] OK {rec['indicators']['asOf']} {rec['source']}" + (f" 빈봉{rec['emptyBarsSkipped']}" if rec["emptyBarsSkipped"] else ""), flush=True)
        except Exception as e:
            msg = str(e) if isinstance(e, ValueError) else type(e).__name__
            records.append({"key": s["key"], "name": s["name"], "code": s["code"], "mkt": s["mkt"], "error": msg})
            print(f"[{i:2d}/{len(secs)}] 실패: {msg}", flush=True)
        time.sleep(0.4)

    ok = [r for r in records if "indicators" in r]; nfail = len(records)-len(ok)
    if not ok:
        raise SystemExit(f"전 종목 수집 실패({nfail}건). 파일을 만들지 않고 이전 결과를 유지합니다.")

    asof = {m: max([r["indicators"]["asOf"] for r in ok if r["mkt"] == m], default=None) for m in ("KR", "US")}
    prev_asof = ((prev_state or {}).get("meta") or {}).get("asOf") or {}
    meta = {"lastRunAt": generated, "ok": len(ok), "fail": nfail, "asOf": asof,
            "lastFullSuccessAt": generated if nfail == 0 else ((prev_state or {}).get("meta") or {}).get("lastFullSuccessAt"),
            "noNewBars": bool(prev_asof) and all(asof.get(m) == prev_asof.get(m) for m in ("KR", "US") if asof.get(m))}
    html = render(secs, records, prev_state, limits, rate, meta)

    state_secs = dict((prev_state or {}).get("securities", {}))     # 실패 종목은 지난 값을 그대로 보존
    for r in ok:
        d = r["indicators"]
        state_secs[r["key"]] = {"close": d["close"], "asOf": d["asOf"], "rsi": d["rsi14"],
                                "aboveHigh": bool(d["high252ExclToday"] and d["close"] > d["high252ExclToday"]),
                                "dd": (d["close"]/d["high52w"]-1)*100 if d["high52w"] else None}
    state = {"meta": meta, "securities": state_secs}
    patch = json.dumps(build_patch(holdings, records, generated, now), ensure_ascii=False)

    def put(path, text):
        with open(path, "w", encoding="utf-8") as f: f.write(text)
    put(os.path.join(site_dir, "index.html"), SHELL.replace("__BLOB__", json.dumps(encrypt(html, password))).replace("__STAMP__", generated[:10]))
    put(os.path.join(site_dir, "quotes-patch.enc"), json.dumps(encrypt(patch, password)))
    put(os.path.join(site_dir, ".nojekyll"), "")
    put(state_path, json.dumps(encrypt(json.dumps(state, ensure_ascii=False), password)))
    print(f"\n{len(ok)}종목 성공, {nfail}종목 실패 → {site_dir}/ (암호화), 상태 {state_path}")
    return records, meta

if __name__ == "__main__":
    main()
