"""가짜 네트워크로 전체 파이프라인을 끝까지 돌리고 검증한다. 실패하면 종료 코드도 실패다.
    python3 -m unittest test_pipeline -v
"""
import contextlib, io, json, os, random, shutil, tempfile, unittest
from datetime import datetime, timezone, timedelta
import pipeline as P

NOW = datetime(2026, 9, 14, 7, 40, tzinfo=timezone.utc)        # 월요일 16:40 KST, 03:40 ET — 실제 예약 실행 시각
HOLD = [
    {"id": "h1", "name": "가종목", "code": "005930", "mkt": "KR", "acct": "ISA", "buy": 100, "qty": 10, "tags": ["국내 개별주", "반도체"]},
    {"id": "h2", "name": "가종목", "code": "005930", "mkt": "KR", "acct": "일반(국내)", "buy": 120, "qty": 5, "tags": ["국내 개별주", "반도체"]},   # 같은 종목, 다른 계좌
    {"id": "h3", "name": "새ETF", "code": "0153K0", "mkt": "KR", "acct": "ISA", "isETF": True, "buy": 10000, "qty": 100, "tags": ["국내 지수·배당"]},
    {"id": "h4", "name": "은ETF", "code": "SLV", "mkt": "US", "acct": "일반(해외)", "buy": 20, "qty": 10, "tags": ["미국주식", "은"]},
    {"id": "h5", "name": "큰종목", "code": "BIGX", "mkt": "US", "acct": "일반(해외)", "buy": 100, "qty": 100, "tags": ["미국주식", "빅테크"]},
    {"id": "h6", "name": "짧은이력", "code": "NEWX", "mkt": "US", "acct": "일반(해외)", "buy": 10, "qty": 1, "tags": ["미국주식"]},
]

def bars(n, empties=(), partial=(), end=None, volume_none=(), market="KR"):
    end = end or NOW.astimezone(P.KST).date()
    out = []
    for i in range(n):
        d = (end-timedelta(days=n-1-i)).isoformat()
        if i in empties: out.append({"date": d, "open": None, "high": None, "low": None, "close": None, "volume": None}); continue
        if i in partial: out.append({"date": d, "open": 100, "high": None, "low": 99, "close": 100, "volume": 1}); continue
        out.append({"date": d, "open": 100+i, "high": 102+i, "low": 99+i, "close": 101+i, "volume": None if i in volume_none else 1000})
    return out

class Indicators(unittest.TestCase):
    def test_sma(self): self.assertEqual(P.sma(list(map(float, range(1, 11))), 5), 8)
    def test_rsi_flat_is_neutral(self): self.assertEqual(P.rsi([100.0]*40), 50.0)          # 가격 불변 → 100 아님
    def test_rsi_all_up(self): self.assertEqual(P.rsi([float(i) for i in range(1, 40)]), 100.0)
    def test_rsi_all_down(self): self.assertEqual(P.rsi([float(40-i) for i in range(1, 40)]), 0.0)
    def test_high252_needs_253_bars(self):
        self.assertIsNone(P.high_excl_today([1.0, 2.0, 99.0]))                              # 3봉으로는 계산 안 함
        self.assertIsNone(P.high_excl_today([1.0]*252))
        self.assertEqual(P.high_excl_today([1.0]*252+[5.0, 99.0]), 5.0)
    def test_high_avail_is_separate(self): self.assertEqual(P.high_avail_excl_today([1.0, 2.0, 99.0]), (2.0, 2))
    def test_vol_missing_not_zero(self):
        v = [1000.0]*20+[3000.0]
        self.assertAlmostEqual(P.vol_ratio(v), 3.0)
        v[5] = None; self.assertIsNone(P.vol_ratio(v))                                      # 누락은 0이 아니라 None
    def test_sma_cross(self): self.assertEqual(P.sma_cross([100.0]*20+[60.0], 20), "dead")
    def test_atr(self):
        b = bars(40); self.assertAlmostEqual(P.atr([x["high"] for x in b], [x["low"] for x in b], [x["close"] for x in b]), 3.0, places=9)   # 고저폭 3, 갭 없음 → TR 3
    def test_compute_short_history(self):
        d = P.compute(bars(50))
        self.assertIsNone(d["high252ExclToday"]); self.assertIsNone(d["high52w"]); self.assertEqual(d["highAvailBars"], 49)

class Clean(unittest.TestCase):
    def test_skip_empty_keep_260(self):
        b, empty = P.clean(bars(300, empties=(250, 280)), "KR", NOW)
        self.assertEqual((len(b), empty), (260, 2))
    def test_kr_after_close_includes_today(self):
        b, _ = P.clean(bars(300), "KR", NOW); self.assertEqual(b[-1]["date"], "2026-09-14")
    def test_kr_intraday_excludes_today(self):
        b, _ = P.clean(bars(300), "KR", datetime(2026, 9, 14, 2, 0, tzinfo=timezone.utc)); self.assertEqual(b[-1]["date"], "2026-09-13")
    def test_us_before_close_excludes_today(self):
        b, _ = P.clean(bars(300, end=NOW.astimezone(P.NYT).date()), "US", NOW); self.assertEqual(b[-1]["date"], "2026-09-13")
    def test_crypto_uses_previous_utc_day_and_keeps_weekends(self):
        sunday = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
        b, _ = P.clean(bars(10, end=sunday.date()), "US", datetime(2026, 9, 21, 7, 0, tzinfo=timezone.utc), continuous=True)
        self.assertEqual(b[-1]["date"], "2026-09-20")
    def test_partial_rejected(self):
        with self.assertRaises(ValueError): P.clean(bars(300, partial=(200,)), "KR", NOW)
    def test_volume_none_kept(self):
        b, _ = P.clean(bars(300, volume_none=(299,)), "KR", NOW); self.assertIsNone(b[-1]["volume"])
    def test_code_format(self):
        self.assertTrue(P.KR_CODE.match("0153K0") and P.KR_CODE.match("005930")); self.assertFalse(P.KR_CODE.match("PLTR1"))

class Holdings(unittest.TestCase):
    def test_aggregate_multi_account(self):
        secs = P.aggregate(HOLD); s = [x for x in secs if x["code"] == "005930"][0]
        self.assertEqual(s["qty"], 15); self.assertAlmostEqual(s["buy"], (100*10+120*5)/15); self.assertEqual(sorted(s["accts"]), ["ISA", "일반(국내)"])
        self.assertEqual(len(secs), 5)
    def test_parse_formats(self):
        h, lim, fx = P.parse_holdings(HOLD); self.assertEqual(lim["은"], 10); self.assertIsNone(fx)
        h, lim, fx = P.parse_holdings({"holdings": HOLD, "limits": {"은": 7}, "usdkrw": 1400}); self.assertEqual(lim["은"], 7); self.assertEqual(fx, 1400)

class Patch(unittest.TestCase):
    def test_close_stamp_dst(self):
        self.assertEqual(P.close_stamp("2026-01-15", "US"), "2026-01-15T16:00:00-05:00")
        self.assertEqual(P.close_stamp("2026-07-15", "US"), "2026-07-15T16:00:00-04:00")
        self.assertEqual(P.close_stamp("2026-09-14", "KR"), "2026-09-14T15:30:00+09:00")
    def test_quote_asof_not_in_future(self):
        rec = {"mkt": "KR", "bars": P.clean(bars(300), "KR", NOW)[0], "source": "yahoo", "indicators": {}}
        pb = P.patch_bars(rec, NOW); self.assertEqual(pb[-1]["date"], "2026-09-14")   # 마감 30분 이후 확정봉
        self.assertLess(datetime.fromisoformat(P.close_stamp(pb[-1]["date"], "KR")), NOW)
    def test_crypto_patch_keeps_weekend_and_uses_utc_day_end(self):
        now = datetime(2026, 9, 21, 7, 0, tzinfo=timezone.utc)
        rec = {"mkt": "US", "assetClass": "crypto", "bars": bars(10, end=datetime(2026,9,20,tzinfo=timezone.utc).date()), "source": "yahoo-crypto", "indicators": {}}
        pb = P.patch_bars(rec, now)
        self.assertEqual(pb[-1]["date"], "2026-09-20")
        self.assertEqual(P.close_stamp("2026-09-20", "US", "crypto"), "2026-09-20T23:59:59+00:00")

# ---- 가짜 네트워크 ----
def fake_get_factory(fail_codes=(), gov_ok=False):
    random.seed(5)
    def fake_get(url, params=None, retries=3):
        if "data.go.kr" in url:
            if not gov_ok: return "<OpenAPI_ServiceResponse><cmmMsgHeader><returnAuthMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR</returnAuthMsg></cmmMsgHeader></OpenAPI_ServiceResponse>"
        sym = url.split("/chart/")[1]
        if sym.endswith(".KQ"): raise ValueError("404")
        if any(c in sym for c in fail_codes): raise ValueError("404")
        kr = sym.endswith(".KS"); n = 60 if "NEWX" in sym else 300
        px = 10000.0 if kr else 100.0; ts = []; q = {k: [] for k in ("open", "high", "low", "close", "volume")}
        zone = P.KST if kr else P.NYT
        end = NOW.astimezone(zone).replace(hour=12, minute=0, second=0, microsecond=0)
        for i in range(n):
            t = end - timedelta(days=n-1-i); px *= 1+random.uniform(-0.02, 0.021); ts.append(int(t.timestamp()))
            if i == 150: [q[k].append(None) for k in q]; continue
            q["open"].append(px*0.99); q["high"].append(px*1.02); q["low"].append(px*0.97); q["close"].append(px); q["volume"].append(1e5*(3 if i == n-1 else 1))
        return json.dumps({"chart": {"result": [{"meta": {"symbol": sym, "currency": "KRW" if kr else "USD", "exchangeTimezoneName": "Asia/Seoul" if kr else "America/New_York", "longName": "Fake"},
                                                 "timestamp": ts, "indicators": {"quote": [q]}}]}})
    return fake_get

def run(tmp, fail_codes=(), password="test-1234", holdings=HOLD):
    P._get = fake_get_factory(fail_codes); P.time.sleep = lambda *_: None
    os.environ["REPORT_PASSWORD"] = password; os.environ["DATA_GO_KR_KEY"] = "dummy"; os.environ["HOLDINGS_JSON"] = json.dumps(holdings)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        recs, meta = P.main(os.path.join(tmp, "site"), os.path.join(tmp, "state"), NOW)
    return recs, meta, buf.getvalue()

def read_text(path):
    with open(path, encoding="utf-8") as f: return f.read()
def read_enc(path, pw): return P.decrypt(json.loads(read_text(path)), pw)
def read_html(tmp, pw): return P.decrypt(json.loads(read_text(os.path.join(tmp, "site/index.html")).split("const BLOB=")[1].split(";\n")[0]), pw)

class EndToEnd(unittest.TestCase):
    def setUp(self): self.tmp = tempfile.mkdtemp(); self.saved = dict(os.environ)
    def tearDown(self): shutil.rmtree(self.tmp, ignore_errors=True); os.environ.clear(); os.environ.update(self.saved)

    def test_no_password_creates_nothing(self):
        P._get = fake_get_factory(); os.environ["HOLDINGS_JSON"] = json.dumps(HOLD)
        for pw in ("", "short"):
            os.environ["REPORT_PASSWORD"] = pw
            with self.assertRaises(SystemExit): P.main(os.path.join(self.tmp, "site"), os.path.join(self.tmp, "state"), NOW)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "site")))

    def test_all_fail_creates_nothing(self):
        with self.assertRaises(SystemExit): run(self.tmp, fail_codes=("005930", "0153K0", "SLV", "BIGX", "NEWX"))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "site/index.html")))

    def test_log_has_no_names_or_codes(self):
        recs, meta, log = run(self.tmp, fail_codes=("0153K0",))
        for word in ("가종목", "새ETF", "은ETF", "005930", "0153K0", "SLV", "BIGX"): self.assertNotIn(word, log)
        self.assertIn("실패:", log); self.assertIn("4종목 성공, 1종목 실패", log)

    def test_report_and_state(self):
        recs, meta, _ = run(self.tmp, fail_codes=("0153K0",))
        raw = read_text(os.path.join(self.tmp, "site/index.html"))
        self.assertNotIn("가종목", raw)
        html = read_html(self.tmp, "test-1234")
        self.assertIn("가종목", html); self.assertIn("거래량 20일 평균의", html)
        self.assertIn("공공데이터 실패", html)                                   # 공공데이터 실패는 경고로만
        self.assertIn("252일 돌파선 아님", html)                                # 짧은 이력 종목은 돌파선 아님을 표시
        self.assertEqual((meta["ok"], meta["fail"]), (4, 1)); self.assertIsNone(meta["lastFullSuccessAt"])
        self.assertIn("비중 판정 보류", html)                                     # 평가액 없는 종목 있음 → 보류
        self.assertEqual(set(os.listdir(os.path.join(self.tmp, "site"))), {"index.html", "quotes-patch.enc", ".nojekyll"})
        state = json.loads(read_enc(os.path.join(self.tmp, "state/state.enc"), "test-1234"))
        self.assertIn("KR:005930", state["securities"]); self.assertNotIn("KR:0153K0", state["securities"])

    def test_weights_use_full_denominator_with_stale_fallback(self):
        run(self.tmp)                                                           # 1회차: 전 종목 성공
        recs, meta, _ = run(self.tmp, fail_codes=("BIGX",))                     # 2회차: 큰 종목 실패
        html = read_html(self.tmp, "test-1234")
        self.assertIn("지난 종가로 대체한 종목 1건: 큰종목", html)
        self.assertNotIn("비중 판정 보류", html)                                 # 지난 값이 있으니 분모 유지, 보류 아님
        self.assertIsNotNone(meta["lastFullSuccessAt"])                          # 1회차의 전종목 성공 시각 유지
        state = json.loads(read_enc(os.path.join(self.tmp, "state/state.enc"), "test-1234"))
        self.assertIn("US:BIGX", state["securities"])                            # 실패 종목의 지난 값 보존

    def test_patch_matches_app_rules(self):
        run(self.tmp)
        patch = json.loads(read_enc(os.path.join(self.tmp, "site/quotes-patch.enc"), "test-1234"))
        self.assertEqual(len(patch["updates"]), 6)                               # 보유 항목(계좌별)마다 한 줄
        for u in patch["updates"]:
            self.assertTrue(u["id"]); self.assertEqual(u["matchBy"], "id"); self.assertTrue(u["collectedAt"])
            self.assertLess(datetime.fromisoformat(u["quoteAsOf"]), NOW)         # 미래 시각 없음
            self.assertEqual(u["quoteDate"], u["bars"][-1]["date"])
            today = NOW.astimezone(P.market_zone(u["mkt"])).date().isoformat()
            self.assertLessEqual(u["bars"][-1]["date"], today)
            if u["mkt"] == "US": self.assertLess(u["bars"][-1]["date"], today)
            if u["mkt"] == "US": self.assertTrue(u["quoteAsOf"].endswith("-04:00"))   # 9월 = 서머타임
            else: self.assertTrue(u["quoteAsOf"].endswith("T15:30:00+09:00"))
        kr = [u for u in patch["updates"] if u["code"] == "005930"]; self.assertEqual({u["acct"] for u in kr}, {"ISA", "일반(국내)"})

    def test_legacy_plaintext_removed(self):
        os.makedirs(os.path.join(self.tmp, "site")); 
        with open(os.path.join(self.tmp, "site/quotes-patch.json"), "w") as f: f.write("x")
        run(self.tmp); self.assertFalse(os.path.exists(os.path.join(self.tmp, "site/quotes-patch.json")))


class NetworkAndIdentity(unittest.TestCase):
    def test_missing_codes_stay_separate(self):
        h=[dict(HOLD[0],id='a',code='',name='A'),dict(HOLD[0],id='b',code='',name='B')]
        self.assertEqual(len(P.aggregate(h)),2)
    def test_denied_host_stops_without_leaking_url(self):
        from unittest.mock import patch
        import urllib.error, importlib.util
        spec=importlib.util.spec_from_file_location('isolated_pipeline',P.__file__)
        mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
        for code in (401,403,429):
            mod._HOST_BLOCKS.clear()
            with patch.object(mod.urllib.request,'urlopen',side_effect=urllib.error.HTTPError('https://example.test/private-code?key=secret',code,'fail',{},None)) as call:
                for _ in range(2):
                    with self.assertRaises(mod.ProviderHTTPError) as e: mod._get('https://example.test/private-code',{'key':'secret'})
                    self.assertIn(str(code),str(e.exception));self.assertNotIn('secret',str(e.exception));self.assertNotIn('private-code',str(e.exception))
                self.assertEqual(call.call_count,1)

class GovernmentV2Contract(unittest.TestCase):
    def test_official_stock_and_etf_contract_without_yahoo(self):
        from unittest.mock import patch
        import importlib.util
        spec = importlib.util.spec_from_file_location('gov_contract', P.__file__)
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        for is_etf, code, endpoint in (
            (False, '005930', 'GetStockSecuritiesInfoService_V2/getStockPriceInfo_V2'),
            (True, '0000D0', 'GetSecuritiesProductInfoService_V2/getETFPriceInfo_V2'),
        ):
            for key in ('dummy+key/=', 'dummy%2Bkey%2F%3D'):
                rows = [dict(basDt=d, srtnCd=code, mkp='100', hipr='110', lopr='90', clpr='105', trqu='123')
                        for d in ('20260713', '20260714')]
                response = json.dumps({'response': {'header': {'resultCode': '00'},
                    'body': {'totalCount': 2, 'items': {'item': rows}}}})
                with patch.object(mod, '_get', return_value=response) as request, patch.object(mod, 'yahoo') as yahoo:
                    rec = mod.fetch_security(code, 'KR', is_etf, key, NOW)
                    self.assertEqual(rec['source'], 'data.go.kr')
                    self.assertEqual(rec['bars'][-1]['close'], 105)
                    yahoo.assert_not_called()
                    url, params = request.call_args.args
                    self.assertEqual(url, 'https://apis.data.go.kr/1160100/' + endpoint)
                    self.assertEqual(params['serviceKey'], 'dummy+key/=')
                    self.assertEqual(params['likeSrtnCd'], code)
                    self.assertEqual(params['resultType'], 'json')

class GovernmentHistoricalGap(unittest.TestCase):
    def test_bad_historical_bar_truncates_without_bridging_or_hiding_latest_error(self):
        from unittest.mock import patch
        rows = [dict(date=d, open=100, high=110, low=90, close=105, volume=10)
                for d in ('20260710', '20260713', '20260714', '20260715')]
        rows[1]['high'] = 1
        with patch.object(P, 'gov', return_value=rows), patch.object(P, 'yahoo') as y:
            rec = P.fetch_security('005930', 'KR', False, 'test-key', NOW)
            self.assertEqual([b['date'] for b in rec['bars']], ['2026-07-14', '2026-07-15'])
            self.assertTrue(rec['warnings'])
            self.assertIsNone(rec['indicators']['high252ExclToday'])
            y.assert_not_called()
        rows[-1]['high'] = 1
        with patch.object(P, 'gov', return_value=rows), patch.object(P, 'yahoo', side_effect=ValueError('unavailable')):
            with self.assertRaises(ValueError):
                P.fetch_security('005930', 'KR', False, 'test-key', NOW)

class CoinbaseCrypto(unittest.TestCase):
    def test_public_candles_parse(self):
        from unittest.mock import patch
        now = datetime(2026, 9, 21, 7, 0, tzinfo=timezone.utc)
        payload = json.dumps([
            [int(datetime(2026,9,20,tzinfo=timezone.utc).timestamp()), 100, 120, 105, 115, 1234],
            [int(datetime(2026,9,19,tzinfo=timezone.utc).timestamp()), 95, 110, 100, 105, 1111],
        ])
        with patch.object(P, "_get", return_value=payload) as req:
            rows = P.coinbase_crypto("BTC-USD", now)
        self.assertEqual(rows[0]["date"], "2026-09-20")
        self.assertEqual(rows[0]["open"], 105.0); self.assertEqual(rows[0]["close"], 115.0)
        self.assertIn("/products/BTC-USD/candles", req.call_args.args[0])
        self.assertEqual(req.call_args.args[1]["granularity"], 86400)

    def test_crypto_prefers_coinbase_without_yahoo(self):
        from unittest.mock import patch
        now = datetime(2026, 9, 21, 7, 0, tzinfo=timezone.utc)
        raw = bars(20, end=datetime(2026,9,20,tzinfo=timezone.utc).date())
        with patch.object(P, "coinbase_crypto", return_value=raw) as cb, patch.object(P, "yahoo") as y:
            r = P.fetch_security("BTC-USD", "US", False, "", now)
        self.assertEqual(r["source"], "coinbase"); self.assertEqual(r["assetClass"], "crypto")
        self.assertEqual(r["bars"][-1]["date"], "2026-09-20"); cb.assert_called_once(); y.assert_not_called()

    def test_crypto_falls_back_to_yahoo(self):
        from unittest.mock import patch
        now = datetime(2026, 9, 21, 7, 0, tzinfo=timezone.utc)
        raw = bars(20, end=datetime(2026,9,20,tzinfo=timezone.utc).date())
        with patch.object(P, "coinbase_crypto", side_effect=ValueError("down")), patch.object(P, "yahoo", return_value=(raw, {"name":"Bitcoin","exchange":"CCC"})) as y:
            r = P.fetch_security("BTC-USD", "US", False, "", now)
        self.assertEqual(r["source"], "yahoo-crypto"); self.assertTrue(r["warnings"]); y.assert_called_once()

class YahooFallbackAndCrypto(unittest.TestCase):
    def test_yahoo_uses_query2_after_query1_429(self):
        from unittest.mock import patch
        sample = fake_get_factory()("https://query2.finance.yahoo.com/v8/finance/chart/AAPL", {"range":"2y","interval":"1d"})
        calls=[]
        def get(url, params=None, retries=2):
            calls.append(url)
            if "query1.finance.yahoo.com" in url: raise P.ProviderHTTPError(429)
            return sample
        with patch.object(P, "_get", side_effect=get), patch.object(P.time, "sleep"):
            b, meta = P.yahoo("AAPL", "US")
        self.assertTrue(b); self.assertEqual(len(calls), 2); self.assertIn("query2.finance.yahoo.com", calls[-1])

    def test_crypto_bypasses_twelve_data_and_marks_asset_class(self):
        from unittest.mock import patch
        raw = bars(20, end=datetime(2026,9,20,tzinfo=timezone.utc).date())
        with patch.dict(os.environ, {"TWELVE_DATA_API_KEY":"fake-key"}), patch.object(P, "coinbase_crypto", side_effect=ValueError("down")), patch.object(P, "yahoo", return_value=(raw, {"name":"Bitcoin","exchange":"CCC"})) as y, patch.object(P, "twelvedata") as td:
            r = P.fetch_security("BTC-USD", "US", False, "", datetime(2026,9,21,7,0,tzinfo=timezone.utc))
        self.assertEqual(r["source"], "yahoo-crypto"); self.assertEqual(r["assetClass"], "crypto")
        self.assertEqual(r["bars"][-1]["date"], "2026-09-20"); y.assert_called_once(); td.assert_not_called()

    def test_failure_kind(self):
        self.assertEqual(P.failure_kind("야후: HTTP 429"), "요청 제한")
        self.assertEqual(P.failure_kind("네트워크 연결 실패 또는 10초 시간 초과"), "연결 실패")
        self.assertEqual(P.failure_kind("HTTP 404"), "티커·제공자")

class AuthenticatedUSData(unittest.TestCase):
    def response(self):
        return {'meta': {'symbol': 'AAPL', 'currency': 'USD'}, 'values': [
            {'datetime': (NOW-timedelta(days=n)).date().isoformat(), 'open':'100', 'high':'110', 'low':'90', 'close':'105', 'volume':'123'} for n in (1,2,3)]}
    def test_authenticated_route_and_missing_volume(self):
        from unittest.mock import patch
        d=self.response(); d['values'][0].pop('volume')
        with patch.dict(os.environ, {'TWELVE_DATA_API_KEY':'fake-key'}), patch.object(P,'_get',return_value=json.dumps(d)) as req, patch.object(P,'yahoo') as y, patch.object(P.time,'sleep'):
            r=P.fetch_security('AAPL','US',False,'',NOW)
            self.assertEqual(r['source'],'twelvedata'); self.assertIsNone(r['bars'][-1]['volume']); y.assert_not_called()
            self.assertEqual(req.call_args.args[1]['country'],'United States')
    def test_rejects_wrong_identity_and_does_not_expose_error_body(self):
        from unittest.mock import patch
        for d in (dict(self.response(),meta={'symbol':'OTHER','currency':'USD'}), {'status':'error','code':401,'message':'secret-key and private symbol'}):
            P._HOST_BLOCKS.clear()
            with patch.object(P,'_get',return_value=json.dumps(d)), patch.object(P.time,'sleep'):
                with self.assertRaises(ValueError) as e: P.twelvedata('AAPL','fake-key')
                self.assertNotIn('secret-key',str(e.exception)); self.assertNotIn('private',str(e.exception))
        P._HOST_BLOCKS.clear()
    def test_quota_error_stops_further_requests(self):
        from unittest.mock import patch
        P._HOST_BLOCKS.clear()
        with patch.object(P,'_get',return_value=json.dumps({'status':'error','code':429})), patch.object(P.time,'sleep'):
            with self.assertRaises(ValueError): P.twelvedata('AAPL','fake-key')
        with patch.object(P,'_get') as req:
            with self.assertRaises(P.ProviderHTTPError): P.twelvedata('AAPL','fake-key')
            req.assert_not_called()
        P._HOST_BLOCKS.clear()

if __name__ == '__main__':
    unittest.main()
