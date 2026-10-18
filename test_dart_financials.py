import contextlib, io, json, os, tempfile, unittest, zipfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch
import dart_financials as D
import pipeline as P
NOW=datetime(2026,10,17,21,40,tzinfo=timezone.utc)
KEY='k'*40
OBJ={'holdings':[{'id':'h','name':'test','code':'000660','mkt':'KR','qty':1,'buy':1,'acct':'test','isETF':False}]}
def fake(status=None):
    calls=[]
    def get(endpoint,key,args,limit=None):
        calls.append((endpoint,dict(args)))
        if status: return json.dumps({'status':status}).encode()
        if endpoint=='corpCode.xml':
            b=io.BytesIO()
            with zipfile.ZipFile(b,'w') as z:z.writestr('CORPCODE.xml','<result><list><corp_code>00164779</corp_code><stock_code>000660</stock_code></list></result>')
            return b.getvalue()
        if endpoint=='list.json':return b'{"status":"013"}'
        if endpoint=='company.json':return json.dumps({'status':'000','corp_code':'00164779','stock_code':'000660','corp_name':'test','acc_mt':'12','induty_code':'2612'}).encode()
        rows=[{'corp_code':args['corp_code'],'bsns_year':args['bsns_year'],'reprt_code':args['reprt_code'],'rcept_no':'20260814000001','sj_div':'CIS','account_id':'ifrs-full_Revenue','account_nm':'매출액','currency':'KRW','thstrm_amount':'123','thstrm_add_amount':'234','frmtrm_q_amount':'100'}]
        return json.dumps({'status':'000','list':rows}).encode()
    return get,calls
class Collection(unittest.TestCase):
    def test_official_backfill_daily_cache_and_encrypted_compatible_packet(self):
        f,calls=fake();a=D.collect(OBJ,KEY,now=NOW,fetch=f)
        self.assertEqual(len(a['companies'][0]['reports']),4)
        self.assertEqual(len(calls),7)
        b=D.collect(OBJ,KEY,a,now=NOW,fetch=f)
        self.assertEqual(len(calls),8)
        self.assertEqual(b['companies'][0]['reports'],a['companies'][0]['reports'])
        self.assertNotIn(KEY,json.dumps(a));self.assertEqual(json.loads(P.decrypt(P.encrypt(json.dumps(a),'test-1234'),'test-1234')),a)
    def test_key_missing_is_visible_and_keeps_previous_reports(self):
        f,_=fake();a=D.collect(OBJ,KEY,now=NOW,fetch=f)
        b=D.collect(OBJ,'',a,now=NOW,fetch=lambda *_:self.fail('no key must not fetch'))
        self.assertEqual(b['status'],'setup-required');self.assertEqual(b['companies'],a['companies'])
    def test_authentication_halts_other_companies_without_relays_or_key_leak(self):
        f,calls=fake('010');before={'companies':[{'code':'000660','corpCode':'00164779','reports':[],'attempts':[]}]}
        a=D.collect(OBJ,KEY,before,now=NOW,fetch=f)
        self.assertEqual(a['reason'],'010');self.assertEqual(len(calls),1);self.assertNotIn(KEY,json.dumps(a))
    def test_failure_preserves_good_reports_and_rejects_other_company_rows(self):
        f,_=fake();a=D.collect(OBJ,KEY,now=NOW,fetch=f)
        def bad(*_):raise D.DartError('http-302')
        b=D.collect(OBJ,KEY,a,now=NOW.replace(day=18),fetch=bad)
        self.assertEqual(b['companies'][0]['reports'],a['companies'][0]['reports'])
        self.assertEqual(b['companies'][0]['reason'],'http-302')
        with self.assertRaises(D.DartError):D.compact_response({'list':[{'corp_code':'wrong'}]},'00164779','2026','11012','CFS')
    def test_ten_target_limit_keeps_every_selected_company(self):
        codes=[str(100000+i) for i in range(10)]
        obj={'holdings':[dict(OBJ['holdings'][0],id='h'+str(i),code=code,name='test') for i,code in enumerate(codes)]}
        self.assertEqual(D.targets(obj,','.join(codes)),codes)
        self.assertEqual(D.targets(obj),codes)
        base,_=fake()
        def many(endpoint,key,args,limit=None):
            if endpoint=='corpCode.xml':
                xml='<result>'+''.join('<list><stock_code>'+c+'</stock_code><corp_code>'+str(10000000+i)+'</corp_code></list>' for i,c in enumerate(codes))+'</result>'
                buf=io.BytesIO()
                with zipfile.ZipFile(buf,'w') as z:z.writestr('CORPCODE.xml',xml)
                return buf.getvalue()
            if endpoint=='company.json':
                c=codes[int(args['corp_code'])-10000000]
                return json.dumps({'status':'000','corp_code':args['corp_code'],'stock_code':c,'corp_name':'test','acc_mt':'12','induty_code':'2612'}).encode()
            return base(endpoint,key,args,limit)
        for count in (9,10):
            chosen=codes[:count]
            a=D.collect(obj,KEY,now=NOW,override=','.join(chosen),fetch=many)
            self.assertEqual([c['code'] for c in a['companies']],chosen)
            self.assertTrue(all(len(c['reports'])==4 for c in a['companies']))
        with self.assertRaises(D.DartError) as e:D.targets(obj,','.join(codes+['199999']))
        self.assertEqual(e.exception.code,'target-settings')
    def test_targets_keep_app_preference_and_exclude_etfs_and_sold_holdings(self):
        obj={'holdings':OBJ['holdings']+[dict(OBJ['holdings'][0],id='e',code='123456',isETF=True),dict(OBJ['holdings'][0],id='z',code='654321',qty=0)]}
        self.assertEqual(D.targets(obj),['000660']);self.assertEqual(D.targets(obj,'000660'),['000660'])
        self.assertEqual(D.target_manifest(obj,'not-a-code')['dropped'],[{'code':'not-a-code','reason':'invalid-code'}])
        self.assertEqual(D.periods(NOW.date())[0],('2026','11012'))
    def test_cfs_no_data_falls_back_only_on_013(self):
        f,calls=fake()
        def separate(endpoint,key,args,limit=None):
            if endpoint.startswith('fnltt') and args['fs_div']=='CFS':return b'{"status":"013"}'
            return f(endpoint,key,args,limit)
        a=D.collect(OBJ,KEY,now=NOW,fetch=separate)
        self.assertTrue(all(r['meta']['fsDiv']=='OFS' for r in a['companies'][0]['reports']))
    def test_financials_can_publish_without_successful_quotes_and_setup_never_destroys_cache(self):
        f,_=fake();packet=D.collect(OBJ,KEY,now=NOW,fetch=f)
        with tempfile.TemporaryDirectory() as t,patch.dict(os.environ,{'REPORT_PASSWORD':'test-1234','HOLDINGS_JSON':json.dumps(OBJ),'OPENDART_API_KEY':KEY,'GITHUB_OUTPUT':str(Path(t)/'out')},clear=True):
            site=Path(t)/'site';state=Path(t)/'state'
            with patch.object(D,'collect',return_value=packet),contextlib.redirect_stdout(io.StringIO()) as log:D.main(site,state)
            raw=(site/'quotes-patch.enc').read_text();self.assertNotIn('000660',raw);self.assertNotIn(KEY,raw+log.getvalue());data=json.loads(P.decrypt(json.loads(raw),'test-1234'))
            self.assertEqual(data['updates'],[]);self.assertEqual(data['financials'],packet);self.assertIn('available=true',(Path(t)/'out').read_text())
            old=(state/'research-financials.enc').read_text()
            with patch.object(D,'collect',return_value={**packet,'status':'setup-required','reason':'OPENDART_API_KEY-missing'}),contextlib.redirect_stdout(io.StringIO()):D.main(site,state)
            self.assertEqual((state/'research-financials.enc').read_text(),old)
    def test_target_manifest_keeps_missing_invalid_and_etf_reasons(self):
        obj={'holdings':OBJ['holdings']+[dict(OBJ['holdings'][0],id='etf',code='123456',isETF=True)]}
        m=D.target_manifest(obj,'005930,123456,invalid')
        self.assertEqual(m['collected'],[])
        self.assertEqual([d['reason'] for d in m['dropped']],['not-in-holdings-snapshot','excluded-etf','invalid-code'])
        self.assertEqual(m['holdingsHash'],P.holdings_hash(P.parse_holdings(obj)[0]))
    def test_early_filing_collects_before_date_estimate_and_weekly_sweep_limits_calls(self):
        base,calls=fake()
        def early(endpoint,key,args,limit=None):
            if endpoint=='list.json':
                calls.append((endpoint,dict(args)))
                return json.dumps({'status':'000','list':[{'report_nm':'[기재정정]분기보고서 (2026.09)','rcept_no':'20261015000001','rcept_dt':'20261015'}]}).encode()
            raw=base(endpoint,key,args,limit)
            if endpoint=='fnlttSinglAcntAll.json' and args['bsns_year']=='2026' and args['reprt_code']=='11014':
                d=json.loads(raw)
                for r in d['list']:r['rcept_no']='20261015000001'
                return json.dumps(d).encode()
            return raw
        a=D.collect(OBJ,KEY,now=NOW,fetch=early)
        self.assertIn(('2026','11014'),[(r['meta']['bsnsYear'],r['meta']['reprtCode']) for r in a['companies'][0]['reports']])
        calls.clear();b=D.collect(OBJ,KEY,a,now=NOW+timedelta(days=1),fetch=early)
        self.assertEqual(len([e for e,args in calls if e=='fnlttSinglAcntAll.json']),0)
        self.assertEqual(len([e for e,args in calls if e=='list.json']),1)
        calls.clear();D.collect(OBJ,KEY,b,now=NOW+timedelta(days=7),fetch=early)
        self.assertEqual(len([e for e,args in calls if e=='fnlttSinglAcntAll.json']),4)
    def test_pending_api_retries_same_day_without_refetching_cached_other_periods(self):
        base,calls=fake()
        def pending(endpoint,key,args,limit=None):
            if endpoint=='list.json':return json.dumps({'status':'000','list':[{'report_nm':'분기보고서 (2026.09)','rcept_no':'20261015000001','rcept_dt':'20261015'}]}).encode()
            if endpoint=='fnlttSinglAcntAll.json' and args['reprt_code']=='11014' and args['bsns_year']=='2026':
                calls.append((endpoint,dict(args)));return b'{"status":"013"}'
            return base(endpoint,key,args,limit)
        a=D.collect(OBJ,KEY,now=NOW,fetch=pending)
        self.assertIn('pending-api',[x['status'] for x in a['companies'][0]['attempts']])
        calls.clear();D.collect(OBJ,KEY,a,now=NOW,fetch=pending)
        fin=[args for e,args in calls if e=='fnlttSinglAcntAll.json']
        self.assertEqual(len(fin),2);self.assertTrue(all(a['reprt_code']=='11014' for a in fin))
    def test_one_period_failure_continues_other_reports(self):
        base,_=fake()
        def broken(endpoint,key,args,limit=None):
            if endpoint=='fnlttSinglAcntAll.json' and args['reprt_code']=='11012':raise D.DartError('response-format')
            return base(endpoint,key,args,limit)
        a=D.collect(OBJ,KEY,now=NOW,fetch=broken)
        self.assertEqual(a['companies'][0]['status'],'partial');self.assertEqual(len(a['companies'][0]['reports']),3)

    def test_http_redirect_is_never_followed(self):
        self.assertIsNone(D.NoRedirect().redirect_request(None,None,302,None,None,None))

if __name__=='__main__':unittest.main()
