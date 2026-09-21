"""Private Telegram alerts from completed encrypted collector output. No orders."""
import copy, hashlib, json, math, os, sys, time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from pipeline import encrypt, decrypt, load_holdings, is_crypto

DESK='https://dh-portfolio-desk.drkim7.chatgpt.site'
def positive(x):
    return isinstance(x,(int,float)) and not isinstance(x,bool) and math.isfinite(x) and x>0

def conditions(h,b):
    last=b[-1]; p=last['close']; result=[]
    def add(kind,level,active,label,urgent=False):
        key=hashlib.sha256(json.dumps([h['id'],kind,level],sort_keys=True).encode()).hexdigest()
        result.append({'key':key,'active':active,'label':label,'urgent':urgent,'date':last['date']})
    stop=h.get('stop') or h.get('stopAnchor')
    if positive(stop):add('stop',stop,p<=stop,f'이탈선 {stop:,.2f} 이하',True)
    if positive(h.get('target')):add('target',h['target'],p>=h['target'],f'목표가 {h["target"]:,.2f} 도달',True)
    for s in (h.get('plan') or {}).get('stages',[]):
        if not s.get('done') and positive(s.get('price')):
            add('stage:'+str(s.get('id')),s['price'],p>=s['price'],f'미실행 매도 계획가 {s["price"]:,.2f} 도달',True)
    if len(b)>=20:
        avg=sum(x['close'] for x in b[-20:])/20
        add('ma20-up',None,p>avg,'20일선 위로 전환')
        add('ma20-down',None,p<avg,'20일선 아래로 전환')
    if len(b)>=253:
        anchor=None;event=''
        for j in range(252,len(b)):
            event=''
            if anchor is not None:
                if b[j]['close']<=anchor:anchor=None;event='fail'
            elif b[j]['close']>max(x['high'] for x in b[j-252:j]):
                anchor=max(x['high'] for x in b[j-252:j]);event='break'
        add('breakout',None,anchor is not None,'252일(UTC·주말 포함) 고점 돌파' if is_crypto(h.get('code')) else '252거래일 고점 돌파')
        add('breakout-fail',None,event=='fail','돌파 당시 기준선 아래 복귀')
    return result

def evaluate(holdings,patch,state,now):
    next_state=copy.deepcopy(state);next_state.setdefault('rules',{});events=[]
    by_id={h.get('id'):h for h in holdings if h.get('id') and positive(h.get('qty'))}
    for u in patch.get('updates',[]):
        h=by_id.get(u.get('id')); b=u.get('bars') or []
        if not h or len(b)<2 or u.get('name')!=h.get('name') or u.get('mkt')!=h.get('mkt','KR') or u.get('acct')!=h.get('acct'):continue
        age=(now-datetime.fromisoformat(u['quoteAsOf'])).total_seconds()
        if not 0<=age<=5*86400:continue
        for c in conditions(h,b):
            old=state.get('rules',{}).get(c['key'])
            if old and old['date']>=c['date']:continue
            next_state['rules'][c['key']]={'date':c['date'],'active':c['active']}
            if old and c['active'] and not old['active']:
                events.append({**c,'text':f'{h["name"][:100]}\n{c["label"]}\n{c["date"]} 종가 {b[-1]["close"]:,.2f} {"USD" if h.get("mkt")=="US" else "KRW"}'})
    return next_state,events

def save(path,state,password):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(encrypt(json.dumps(state,ensure_ascii=False),password)));tmp.replace(path)

def send(token,chat,text):
    payload=json.dumps({'chat_id':chat,'text':text,'link_preview_options':{'is_disabled':True}}).encode()
    req=Request('https://api.telegram.org/bot'+token+'/sendMessage',data=payload,headers={'Content-Type':'application/json'},method='POST')
    with urlopen(req,timeout=20) as r:reply=json.load(r)
    if not reply.get('ok'):raise RuntimeError('Telegram rejected message')

def data_status(patch, now):
    updates=patch.get('updates',[]); stale=0; dates={}
    for u in updates:
        try:
            age=(now-datetime.fromisoformat(u['quoteAsOf'])).total_seconds()
            if not 0<=age<=5*86400:stale+=1
        except (ValueError,KeyError,TypeError):stale+=1
        dates.setdefault('CRYPTO' if u.get('assetClass')=='crypto' or is_crypto(u.get('code')) else u.get('mkt','?'),set()).add(str(u.get('quoteDate','미확인')))
    failed=len(patch.get('failures',[]))
    label='partial' if failed or stale or not updates else 'ok'
    lines=[f'수집 성공 {len(updates)} / 실패 {failed} 보유항목',f'5일 초과·시각 미확인 {stale}항목']
    for market,ds in sorted(dates.items()):
        ordered=sorted(ds); span=ordered[0] if len(ordered)==1 else ordered[0]+' ~ '+ordered[-1]
        lines.append(('가상자산 UTC' if market=='CRYPTO' else '국내' if market=='KR' else '해외')+' 종가 기준일: '+span)
    return label,'\n'.join(lines)

def health_message(previous,current,detail):
    if previous==current:return None
    if current=='collection_failed':return '[수집 중단]\n이번 실행에서 수집을 완료하지 못했습니다. 기존 리포트를 유지하며 이번 시세 알림은 보류합니다.'
    if current=='publish_failed':return '[리포트 갱신 실패]\n수집 후 게시를 완료하지 못했습니다. 이번 시세 알림은 보류합니다.'
    if current=='partial':return '[시세 자료 확인 필요]\n'+detail
    if current=='ok' and previous and previous!='ok':return '[수집·리포트 복구]\n'+detail
    return None

def main(sender=send):
    token=os.getenv('TELEGRAM_BOT_TOKEN','').strip();chat=os.getenv('TELEGRAM_CHAT_ID','').strip()
    if not token or not chat:print('Telegram not configured; report remains available.');return
    password=os.getenv('REPORT_PASSWORD','');path=Path('state/alerts.enc')
    if len(password)<8:raise ValueError('Password missing')
    # Corrupt state must stop sending, never silently reset deduplication.
    state=json.loads(decrypt(json.loads(path.read_text()),password)) if path.exists() else {'rules':{},'sent':[]}
    outcome=os.getenv('COLLECTION_OUTCOME','success')
    deployed=os.getenv('DEPLOY_OUTCOME','success')
    if outcome!='success' or deployed!='success':
        health='collection_failed' if outcome!='success' else 'publish_failed'
        message=health_message(state.get('health'),health,'')
        if message:
            sender(token,chat,message+'\n실행 내역: https://github.com/drkim7/portfolio-collector/actions')
            state['health']=health;save(path,state,password)
        print('Notification health check complete.');return
    patch=json.loads(decrypt(json.loads(Path('site/quotes-patch.enc').read_text()),password))
    holdings,_,_=load_holdings();now=datetime.now(timezone.utc)
    health,detail=data_status(patch,now)
    message=health_message(state.get('health'),health,detail)
    if message:
        sender(token,chat,message+'\n'+DESK)
        state['health']=health;save(path,state,password);time.sleep(1.1)
    else:state['health']=health
    planned,events=evaluate(holdings,patch,state,now)
    sent=set(state.get('sent',[]));planned['sent']=list(sent)
    if not state.get('initialized'):
        sender(token,chat,'투자 데스크 알림 연결 완료\n현재 상태를 기준으로 등록했습니다. 이미 충족한 조건은 일괄 발송하지 않습니다. 이후 새 일봉에서 조건이 전환되면 알립니다.\n기준: GitHub에 저장된 보유내역·계획 (앱 변경 후 별도 갱신 필요)\n'+DESK)
        planned['initialized']=True;save(path,planned,password);print('Telegram baseline initialized.');return
    # Queue technical transitions before moving rule state forward. Even a second
    # same-day collection or a failed delivery must not discard an event.
    pending={e['key']+':'+e['date']:e for e in state.get('pending',[])}
    for e in events:
        if not e['urgent']:pending[e['key']+':'+e['date']]=e
    state['pending']=list(pending.values());save(path,state,password)
    def deliver(key,text,event_keys=()):
        if key in sent:return
        sender(token,chat,text+'\n수집된 종가 기준 · 주문 지시 아님\n'+DESK)
        sent.add(key);sent.update('event:'+k for k in event_keys)
        state['sent']=sorted(sent)[-5000:];save(path,state,password);time.sleep(1.1)
    for e in events:
        if e['urgent']:deliver(e['key']+':'+e['date'],'[매매 기준 확인]\n'+e['text'])
    local=now.astimezone(__import__('zoneinfo').ZoneInfo('Asia/Seoul'))
    day=local.date().isoformat()
    # Weekdays: one afternoon digest. Weekend schedule has only a morning run.
    due=local.weekday()>=5 or (local.hour,local.minute)>=(16,30)
    summary_key='summary:v2:'+day
    remaining={k:e for k,e in pending.items() if 'event:'+k not in sent}
    if due and summary_key not in sent:
        # Split messages instead of truncating and losing the remaining signals.
        chunks=[];chunk=[];size=0
        for key,e in remaining.items():
            if chunk and size+len(e['text'])>2200:chunks.append(chunk);chunk=[];size=0
            chunk.append((key,e));size+=len(e['text'])+2
        if chunk:chunks.append(chunk)
        if not chunks:chunks=[[]]
        for chunk in chunks:
            keys=[k for k,_ in chunk]
            batch=hashlib.sha256(json.dumps(keys).encode()).hexdigest()[:20]
            text='[일일 시세 점검] '+day+'\n'+detail+'\n\n'
            text+=('\n\n'.join(e['text'] for _,e in chunk) if chunk else '새 기술 조건 전환 없음. 미확인·실패 항목의 안전을 의미하지 않습니다.')
            text+='\n기술 신호는 기록된 기준일의 전환입니다. 현재 지속 여부는 앱에서 확인하세요.'
            deliver(summary_key+':'+batch,text,keys)
        sent.add(summary_key)
    planned['pending']=[e for k,e in pending.items() if 'event:'+k not in sent]
    planned['sent']=sorted(sent)[-5000:];save(path,planned,password)
    print('Telegram notification pass complete.')

if __name__=='__main__':
    try:main()
    except Exception as e:
        # URLs and response bodies may contain tokens or private holdings.
        print('Telegram notification failed:',type(e).__name__);sys.exit(1)

