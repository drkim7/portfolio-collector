"""Official DART -> existing encrypted quote feed. Never log keys or holdings."""
import io, json, os, re, time, urllib.request, urllib.parse, urllib.error, zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta, date
from pathlib import Path
import pipeline as P

MAX_RESEARCH_CODES = 10

REPORTS = [('Q1','11013',3,31),('H1','11012',6,30),('Q3','11014',9,30),('FY','11011',12,31)]
DICTIONARY = json.loads((Path(__file__).parent/'financial-accounts.json').read_text())
IDS = {v for a in DICTIONARY['accounts'].values() for v in a['ids']}
NAMES = {re.sub(r'\s','',v) for a in DICTIONARY['accounts'].values() for v in a['names']}
FIELDS = {'rcept_no','reprt_code','bsns_year','corp_code','fs_div','sj_div','sj_nm','account_id','account_nm','ord','currency',
          'thstrm_nm','thstrm_amount','thstrm_add_amount','frmtrm_nm','frmtrm_amount','frmtrm_q_nm','frmtrm_q_amount','frmtrm_add_amount'}
HALT = {'010','011','012','020','901'}
class DartError(Exception):
    def __init__(self, code): self.code=code; super().__init__(code)
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args): return None

def request(endpoint,key,args,limit=2_000_000):
    # No caller-provided hosts, redirects, response messages or exception URLs.
    url='https://opendart.fss.or.kr/api/'+endpoint+'?'+urllib.parse.urlencode({'crtfc_key':key,**args})
    try:
        with urllib.request.build_opener(NoRedirect()).open(urllib.request.Request(url,headers={'Accept':'application/json, application/zip'}),timeout=20) as r:
            raw=r.read(limit+1)
            if len(raw)>limit: raise DartError('response-size')
            return raw
    except DartError: raise
    except urllib.error.HTTPError as e: raise DartError('http-'+str(e.code)) from None
    except Exception: raise DartError('transport') from None

def api(endpoint,key,args,fetch=request):
    for attempt in range(2):
        try: d=json.loads(fetch(endpoint+'.json',key,args))
        except DartError: raise
        except Exception: raise DartError('response-format') from None
        status=d.get('status') if isinstance(d,dict) else None
        if status in ('000','013'): return d
        if status in ('800','900') and attempt==0: continue
        raise DartError(status if isinstance(status,str) and re.fullmatch(r'\d{3}',status) else 'response-format')

def periods(today):
    out=[]
    for year in range(today.year-2,today.year+1):
        for kind,report,month,day in REPORTS:
            if date(year,month,day)+timedelta(days=91 if kind=='FY' else 46)<=today:
                out.append((str(year),report))
    return list(reversed(out))[:4]

def target_manifest(obj,override=''):
    holdings,_,_=P.parse_holdings(obj)
    active=[h for h in holdings if h.get('mkt')=='KR' and h.get('qty',0)>0 and h.get('assetClass')!='crypto']
    def excluded(h):return h.get('isETF') or re.search(r'ETF|ETN|KODEX|TIGER|KOACT|ACE |RISE |PLUS |SOL |HANARO|TIMEFOLIO',h.get('name',''),re.I)
    eligible=list(dict.fromkeys(h['code'] for h in active if not excluded(h)))
    explicit=override.split(',') if override.strip() else obj.get('researchCodes',obj.get('researchWorkroom',{}).get('codes')) if isinstance(obj,dict) else None
    if explicit is not None and (not isinstance(explicit,list) or len(explicit)>MAX_RESEARCH_CODES):raise DartError('target-settings')
    requested=list(dict.fromkeys(str(c).strip()[:100] for c in (explicit if explicit is not None else eligible[:MAX_RESEARCH_CODES])))
    selected=[];dropped=[]
    for code in requested:
        reason='invalid-code' if not re.fullmatch(r'[0-9A-Z]{6}',code) else 'excluded-etf' if any(h['code']==code and excluded(h) for h in active) and code not in eligible else 'not-in-holdings-snapshot' if code not in eligible else None
        if reason:dropped.append({'code':code,'reason':reason})
        else:selected.append(code)
    return {'requested':requested,'collected':selected,'dropped':dropped,'holdingsHash':P.holdings_hash(holdings)}

def targets(obj,override=''):
    return target_manifest(obj,override)['collected']

def filing_period(title):
    m=re.match(r'^(?:\[[^\]]+\]\s*)*(사업|반기|분기)보고서\s*\((\d{4})\.(\d{2})\)',title)
    if not m:return None
    kind={('사업','12'):'FY',('반기','06'):'H1',('분기','03'):'Q1',('분기','09'):'Q3'}.get((m[1],m[3]))
    return (m[2],dict((r[0],r[1]) for r in REPORTS)[kind],m[2]+kind) if kind else None

def periodic_filings(corp,key,today,fetch):
    d=api('list',key,{'corp_code':corp,'bgn_de':(today-timedelta(days=120)).strftime('%Y%m%d'),'end_de':today.strftime('%Y%m%d'),'pblntf_ty':'A','last_reprt_at':'Y','page_count':'100','page_no':'1'},fetch)
    result=[]
    for f in d.get('list',[]):
        title=str(f.get('report_nm',''));period=filing_period(title);receipt=f.get('rcept_no','')
        if period and re.fullmatch(r'\d{14}',receipt):result.append({'rceptNo':receipt,'title':title[:300],'date':f.get('rcept_dt',''),'periodKey':period[2]})
    return sorted(result,key=lambda f:f['rceptNo'],reverse=True)[:8]

def registry(key,fetch=request):
    raw=fetch('corpCode.xml',key,{},5_000_000)
    try:
        if not raw.startswith(b'PK'):
            status=json.loads(raw).get('status') if raw.lstrip().startswith(b'{') else ET.fromstring(raw).findtext('status')
            raise DartError(status if status in HALT else 'registry-format')
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            info=next(i for i in z.infolist() if i.filename.upper().endswith('CORPCODE.XML'))
            if info.file_size>40_000_000: raise ValueError()
            root=ET.fromstring(z.read(info))
        return {r.findtext('stock_code','').strip():r.findtext('corp_code','').strip() for r in root.findall('list')
                if re.fullmatch(r'[0-9A-Z]{6}',r.findtext('stock_code','').strip()) and re.fullmatch(r'\d{8}',r.findtext('corp_code','').strip())}
    except DartError: raise
    except Exception: raise DartError('registry-format') from None

def compact_response(d,corp,year,report,basis):
    rows=d.get('list',[])
    if not isinstance(rows,list) or not rows: raise DartError('empty-rows')
    receipts=set()
    for r in rows:
        if not isinstance(r,dict) or r.get('corp_code')!=corp or str(r.get('bsns_year'))!=year or r.get('reprt_code')!=report or r.get('fs_div',basis)!=basis: raise DartError('identity-mismatch')
        receipts.add(r.get('rcept_no'))
    if len(receipts)!=1 or not re.fullmatch(r'\d{14}',str(next(iter(receipts)))): raise DartError('receipt-mismatch')
    selected=[{k:v for k,v in r.items() if k in FIELDS and isinstance(v,str)} for r in rows if r.get('sj_div') in ('BS','IS','CIS','CF') and
        (r.get('account_id') in IDS or r.get('account_id','') in ('-표준계정코드 미사용-','-','') and re.sub(r'\s','',r.get('account_nm','')) in NAMES)]
    if len(selected)>64 or not selected: raise DartError('account-format')
    return {'status':'000','list':selected}

def collect(obj,key,previous=None,now=None,fetch=request,override=''):
    injected=now is not None
    now=now or datetime.now(timezone.utc);stamp=now.isoformat(timespec='seconds');today=now.astimezone(P.KST).date();manifest=target_manifest(obj,override);selected=manifest['collected']
    packet={'kind':'portfolio-financials-v1','source':'official-dart-via-github','collectedAt':stamp,'status':'complete','reason':'ok','companies':[],'dictionaryVersion':DICTIONARY['version'],'targets':manifest}
    old={c['code']:c for c in (previous or {}).get('companies',[])}
    if not re.fullmatch(r'[a-zA-Z0-9]{40}',key or ''):
        return {**packet,'status':'setup-required','reason':'OPENDART_API_KEY-missing','companies':[old[c] for c in selected if c in old]}
    def timestamp():return (now if injected else datetime.now(timezone.utc)).isoformat(timespec='seconds')
    mapping=None;halt=None;transport_failures=0;deadline=time.monotonic()+600
    for code in selected:
        before=old.get(code,{});company={**before,'code':code,'checkedAt':stamp,'status':'error','reason':'not-collected','reports':list(before.get('reports',[])),'attempts':list(before.get('attempts',[]))}
        packet['companies'].append(company)
        if time.monotonic()>=deadline: halt='time-budget'
        if halt: company['reason']=halt; continue
        try:
            corp=before.get('corpCode')
            if not corp:
                if mapping is None: mapping=registry(key,fetch)
                corp=mapping.get(code)
            if not corp: raise DartError('company-not-found')
            # Refresh official identity daily. Never infer December year end.
            if not before.get('identityVerifiedAt') or datetime.fromisoformat(before['identityVerifiedAt']).astimezone(P.KST).date()!=today:
                d=api('company',key,{'corp_code':corp},fetch)
                if d.get('corp_code')!=corp or d.get('stock_code')!=code: raise DartError('identity-mismatch')
                month=int(d.get('acc_mt','0'))
                if not 1<=month<=12: raise DartError('fiscal-month')
                company.update(corpCode=corp,name=str(d.get('corp_name',''))[:150],fiscalMonth=month,industryCode=str(d.get('induty_code',''))[:6],identityVerifiedAt=stamp)
            if company.get('fiscalMonth')!=12 or re.match(r'^(64|65|66)',company.get('industryCode','')):
                company.update(status='unsupported',reason='period-or-industry');continue
            listed=[]
            try:listed=periodic_filings(corp,key,today,fetch);company['periodicFilings']=listed
            except DartError as e:
                company['reason']=e.code
                if e.code in HALT:raise
            candidates=set(periods(today))
            for f in listed:
                period=filing_period(f['title'])
                if period and int(period[0])>=today.year-2:candidates.add(period[:2])
            order={r[1]:i for i,r in enumerate(REPORTS)}
            wanted=sorted(candidates,key=lambda p:(p[0],order[p[1]]),reverse=True)[:4];reports={(r['meta']['bsnsYear'],r['meta']['reprtCode']):r for r in company['reports']}
            attempts={(a['year'],a['report']):a for a in company['attempts']}
            listing_failed=company.get('reason') not in ('not-collected','ok') and not listed
            company.update(status='partial' if listing_failed else 'complete',reason=company['reason'] if listing_failed else 'ok')
            full_due=not before.get('lastFullCheckOn') or (today-date.fromisoformat(before['lastFullCheckOn'])).days>=7
            full_ok=True
            for year,report in wanted:
                last=attempts.get((year,report),{})
                prior=reports.get((year,report));kind=next(r[0] for r in REPORTS if r[1]==report);receipts=[f['rceptNo'] for f in listed if f['periodKey']==year+kind];trigger=max(receipts) if receipts else None;old_receipt=prior['response']['list'][0]['rcept_no'] if prior else None
                needs_filing=trigger and (not old_receipt or trigger>old_receipt)
                if prior and not full_due and not needs_filing:continue
                if last.get('on')==str(today) and last.get('status') in ('000','013') and not needs_filing:continue
                try:
                    if time.monotonic()>=deadline: raise DartError('time-budget')
                    basis='CFS';d=api('fnlttSinglAcntAll',key,{'corp_code':corp,'bsns_year':year,'reprt_code':report,'fs_div':basis},fetch)
                    if d['status']=='013':
                        basis='OFS';d=api('fnlttSinglAcntAll',key,{'corp_code':corp,'bsns_year':year,'reprt_code':report,'fs_div':basis},fetch)
                    attempts[year,report]={'year':year,'report':report,'on':str(today),'status':d['status']}
                    if d['status']=='013':
                        if needs_filing:attempts[year,report]['status']='pending-api';company.update(status='partial',reason='pending-api');full_ok=False
                        continue
                    response=compact_response(d,corp,year,report,basis)
                    if trigger and response['list'][0]['rcept_no']<trigger:
                        attempts[year,report]['status']='pending-api';company.update(status='partial',reason='pending-api');full_ok=False
                    prior=reports.get((year,report))
                    if prior and prior['response']['list'][0]['rcept_no']>response['list'][0]['rcept_no']: raise DartError('older-receipt')
                    reports[year,report]={'meta':{'code':code,'corpCode':corp,'bsnsYear':year,'reprtCode':report,'fsDiv':basis,'fiscalMonth':company['fiscalMonth'],'fetchedAt':timestamp()},'response':response}
                except DartError as e:
                    full_ok=False
                    attempts[year,report]={'year':year,'report':report,'on':str(today),'status':e.code}
                    company.update(status='partial' if reports else 'error',reason=e.code)
                    if e.code in HALT or e.code=='time-budget': halt=e.code
                    if e.code.startswith(('http-','transport')):
                        transport_failures+=1
                        if transport_failures>=2: halt='transport'
                    if halt:break
                    continue
            if full_due and full_ok:company['lastFullCheckOn']=str(today)
            company['checkedAt']=timestamp()
            company['reports']=[reports[p] for p in wanted if p in reports]
            company['attempts']=list(attempts.values())[-16:]
            if company['reports'] and company['status']=='error':company['status']='partial'
            if len(company['reports'])<len(wanted) and company['status']=='complete': company.update(status='partial',reason='missing-periods')
        except (DartError,ValueError) as e:
            company['reason']=e.code if isinstance(e,DartError) else 'response-format'
            if company['reason'] in HALT: halt=company['reason']
            if company['reason'].startswith(('http-','transport')):
                transport_failures+=1
                if transport_failures>=2: halt='transport'
    packet['collectedAt']=timestamp()
    packet['targets']['collected']=[c['code'] for c in packet['companies'] if c.get('reports')]
    if packet['targets']['dropped']:packet.update(status='partial',reason='target-settings')
    if any(c['status'] not in ('complete','unsupported') for c in packet['companies']): packet.update(status='partial',reason=halt or 'company-errors')
    return packet

def main(site_dir='site',state_dir='state'):
    password=os.environ.get('REPORT_PASSWORD','').strip()
    if len(password)<8: raise SystemExit('REPORT_PASSWORD 설정 필요. 출력하지 않고 중단합니다.')
    obj=json.loads(os.environ.get('HOLDINGS_JSON','{}'));path=Path(state_dir)/'research-financials.enc';previous=None
    if path.exists():
        try: previous=json.loads(P.decrypt(json.loads(path.read_text()),password))
        except Exception: pass
    packet=collect(obj,os.environ.get('OPENDART_API_KEY','').strip(),previous,override=os.environ.get('DART_RESEARCH_CODES',''))
    site=Path(site_dir);quote=site/'quotes-patch.enc';available=any(c.get('reports') for c in packet['companies'])
    if quote.exists(): patch=json.loads(P.decrypt(json.loads(quote.read_text()),password))
    elif available: patch={'kind':'portfolio-quotes-v1','collectedAt':packet['collectedAt'],'updates':[],'failures':[]}
    else: patch=None
    if patch is not None:
        patch['financials']=packet
        raw=json.dumps(patch,ensure_ascii=False)
        if len(raw.encode())>2_500_000: raise SystemExit('암호화 수집 파일 크기 한도 초과. 이전 자료를 유지합니다.')
        site.mkdir(exist_ok=True,parents=True);quote.write_text(json.dumps(P.encrypt(raw,password)))
        if not (site/'index.html').exists(): (site/'index.html').write_text('<!doctype html><html lang="ko"><meta charset="utf-8"><title>수집 결과</title><p>이번 시세 수집은 완료되지 않았습니다. 암호화된 실적 결과는 투자 데스크의 수집 결과 연결에서 확인하세요.</p></html>')
        (site/'.nojekyll').touch()
    path.parent.mkdir(exist_ok=True,parents=True)
    # Missing key must not destroy the last successful encrypted collector state.
    if packet['status']!='setup-required': path.write_text(json.dumps(P.encrypt(json.dumps(packet,ensure_ascii=False),password)))
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'],'a') as f: f.write('available='+str(available and patch is not None).lower()+'\n')
    print('DART:',packet['status'],'설정 확인 필요' if packet['status']=='setup-required' else '암호화 결과 작성 완료',flush=True)

if __name__=='__main__': main()
