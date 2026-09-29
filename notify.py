"""Private Telegram alerts from completed encrypted collector output. No orders."""
import copy, hashlib, json, math, os, sys, time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from pipeline import encrypt, decrypt, load_holdings, is_crypto
from smart_alerts import breakout_transition, condition_recovery

DESK='https://dh-portfolio-desk.drkim7.chatgpt.site'
def positive(x):
    return isinstance(x,(int,float)) and not isinstance(x,bool) and math.isfinite(x) and x>0

def conditions(h,b):
    last=b[-1]; p=last['close']; result=[]
    def add(kind,level,active,label,urgent=False):
        key=hashlib.sha256(json.dumps([h['id'],kind,level],sort_keys=True).encode()).hexdigest()
        result.append({'key':key,'kind':kind,'active':active,'label':label,'urgent':urgent,'date':last['date']})
    stop=h.get('stop') or h.get('stopAnchor')
    if positive(stop):add('stop',stop,p<=stop,f'이탈선 {stop:,.2f} 이하',True)
    if positive(h.get('target')):add('target',h['target'],p>=h['target'],f'목표가 {h["target"]:,.2f} 도달',True)
    if positive(h.get('target')):add('target-near',h['target'],h['target']*.98<=p<h['target'],f'목표가 {h["target"]:,.2f} 2% 이내')
    if positive(stop):add('stop-near',stop,stop<p<=stop*1.02,f'이탈선 {stop:,.2f} 2% 이내',True)
    if positive(h.get('buy')):add('avg-recovery',h['buy'],p>=h['buy'],f'단순 평단 {h["buy"]:,.2f} 회복')
    if positive(h.get('economicBep')):add('bep-recovery',h['economicBep'],p>=h['economicBep'],f'입력한 경제적 BEP {h["economicBep"]:,.2f} 회복')
    for s in (h.get('plan') or {}).get('stages',[]):
        if not s.get('done') and positive(s.get('price')):
            add('stage:'+str(s.get('id')),s['price'],p>=s['price'],f'미실행 매도 계획가 {s["price"]:,.2f} 도달',True)
    for n in (20,60,120):
        if len(b)>=n:
            avg=sum(x['close'] for x in b[-n:])/n
            add(f'ma{n}-up',None,p>avg*1.005,f'{n}일선 위로 전환')
            add(f'ma{n}-down',None,p<avg*.995,f'{n}일선 아래로 전환')
    if len(b)>=21:
        volumes=[x.get('volume') for x in b[-21:]]
        if all(v is not None for v in volumes) and sum(volumes[:-1])>0:
            ratio=volumes[-1]/(sum(volumes[:-1])/20)
            add('volume-high',1.5,ratio>=1.5,f'거래량 직전 20일 평균 대비 {ratio:.2f}배')
    for step,active,label in condition_recovery(h,b):add('recovery:'+step,None,active,label)
    return result

def evaluate(holdings,patch,state,now,limits=None,fx=None,enabled_types=None):
    next_state=copy.deepcopy(state);next_state.setdefault('rules',{});next_state.setdefault('breakout',{});events=[]
    by_id={h.get('id'):h for h in holdings if h.get('id') and positive(h.get('qty'))}
    for u in patch.get('updates',[]):
        h=by_id.get(u.get('id')); b=u.get('bars') or []
        if not h or len(b)<2 or u.get('name')!=h.get('name') or u.get('mkt')!=h.get('mkt','KR') or u.get('acct')!=h.get('acct'):continue
        try:age=(now-datetime.fromisoformat(u['quoteAsOf'])).total_seconds()
        except (ValueError,KeyError,TypeError):continue
        if not 0<=age<=5*86400:continue
        if u.get('quoteDate') and u['quoteDate']!=b[-1].get('date'):continue
        old_break=state.get('breakout',{}).get(h['id'])
        nxt,change=breakout_transition(b,old_break)
        if nxt:next_state['breakout'][h['id']]=nxt
        if change:
            key=hashlib.sha256(json.dumps([h['id'],'breakout',change['state']]).encode()).hexdigest()
            if enabled_types is None or 'breakout' in enabled_types:events.append({'key':key,'kind':'breakout','itemId':h['id'],'active':True,'label':change['label'],'urgent':change['state'] in ('first-break','failed','re-break'),'date':change['date'],'text':f'{h["name"][:100]}\n{change["label"]}\n{change["date"]} 확정 종가 {b[-1]["close"]:,.2f} {"USD" if h.get("mkt")=="US" else "KRW"}'})
        for c in conditions(h,b):
            old=state.get('rules',{}).get(c['key'])
            if old and old['date']>=c['date']:continue
            next_state['rules'][c['key']]={'date':c['date'],'active':c['active']}
            category='ma' if c['kind'].startswith('ma') else 'volume' if c['kind'].startswith('volume') else 'recovery' if c['kind'].startswith('recovery:') else 'avg' if 'recovery' in c['kind'] else 'level'
            if old and c['active'] and not old['active'] and (enabled_types is None or category in enabled_types):
                events.append({**c,'itemId':h['id'],'text':f'{h["name"][:100]}\n{c["label"]}\n{c["date"]} 확정 종가 {b[-1]["close"]:,.2f} {"USD" if h.get("mkt")=="US" else "KRW"}'})
    # Concentration is comparable only when every held row has a fresh close.
    if isinstance(limits,dict) and positive(fx):
        updates={u.get('id'):u for u in patch.get('updates',[]) if u.get('id') in by_id}
        valid=len(updates)==len(by_id)
        if valid:
            for h in by_id.values():
                u=updates[h['id']]
                try:age=(now-datetime.fromisoformat(u['quoteAsOf'])).total_seconds()
                except (ValueError,KeyError,TypeError):valid=False;break
                if not 0<=age<=5*86400 or not positive(u.get('price')):valid=False;break
        if valid:
            amounts={};total=0
            ignored={'국내 개별주','미국주식','국내 지수·배당','국내 중소형','국내 기타','배당'}
            for h in by_id.values():
                value=updates[h['id']]['price']*h['qty']*(fx if h.get('mkt')=='US' else 1);total+=value
                for tag in h.get('tags',[]):
                    if tag not in ignored:amounts[tag]=amounts.get(tag,0)+value
            day=max((u['bars'][-1]['date'] for u in updates.values() if u.get('bars')),default='')
            for tag,amount in amounts.items():
                limit=limits.get(tag)
                if not positive(total) or not isinstance(limit,(int,float)) or not math.isfinite(limit) or not 0<=limit<=100:continue
                active=amount/total*100>limit+.2
                key=hashlib.sha256(json.dumps(['portfolio','tag-limit',tag,limit],ensure_ascii=False).encode()).hexdigest()
                old=state.get('rules',{}).get(key)
                if old and old['date']>=day:continue
                next_state['rules'][key]={'date':day,'active':active}
                if old and active!=old['active']:
                    label=f'{tag} 한도 '+('초과' if active else '이내 복귀')+f' · {amount/total*100:.1f}% / {limit:.1f}%'
                    if enabled_types is None or 'limit' in enabled_types:events.append({'key':key,'kind':'limit','itemId':'portfolio:'+tag,'active':active,'label':label,'urgent':active,'date':day,'text':f'{tag}\n{label}\n{day} 확정 일봉'})
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
    holdings,limits,fx=load_holdings();now=datetime.now(timezone.utc)
    health,detail=data_status(patch,now)
    message=health_message(state.get('health'),health,detail) if os.getenv('SMART_ALERT_HEALTH','1').strip()!='0' else None
    if message:
        sender(token,chat,message+'\n'+DESK)
        state['health']=health;save(path,state,password);time.sleep(1.1)
    else:state['health']=health
    types=os.getenv('SMART_ALERT_TYPES','').strip()
    enabled=set(x.strip() for x in types.split(',') if x.strip()) if types else None
    planned,events=evaluate(holdings,patch,state,now,limits,fx,enabled)
    sent=set(state.get('sent',[]));planned['sent']=list(sent)
    if not state.get('initialized'):
        sender(token,chat,'투자 데스크 알림 연결 완료\n현재 상태를 기준으로 등록했습니다. 이미 충족한 조건은 일괄 발송하지 않습니다. 이후 새 일봉에서 조건이 전환되면 알립니다.\n기준: GitHub에 저장된 보유내역·계획 (앱 변경 후 별도 갱신 필요)\n'+DESK)
        planned['initialized']=True;save(path,planned,password);print('Telegram baseline initialized.');return
    # Queue every transition before moving state forward. Failed sends retain events.
    pending={e['key']+':'+e['date']:e for e in state.get('pending',[])}
    for e in events:
        pending[e['key']+':'+e['date']]=e
    state['pending']=list(pending.values());save(path,state,password)
    local=now.astimezone(__import__('zoneinfo').ZoneInfo('Asia/Seoul'))
    day=local.date().isoformat()
    cap=max(1,min(30,int(os.getenv('SMART_ALERT_DAILY_CAP','').strip() or '8')))
    counts=state.get('daily_count') or {}
    used=counts.get(day,0)
    def deliver(key,text,event_keys=()):
        nonlocal used
        if key in sent:return
        sender(token,chat,text+'\n수집된 종가 기준 · 주문 지시 아님\n'+DESK)
        sent.add(key);sent.update('event:'+k for k in event_keys)
        if event_keys:used+=1;counts[day]=used;state['daily_count']={day:used}
        state['sent']=sorted(sent)[-5000:];save(path,state,password);time.sleep(1.1)
    def groups(rows):
        grouped={}
        for key,e in rows:
            group=e.get('itemId') or key;grouped.setdefault(group,[]).append((key,e))
        return list(grouped.values())
    remaining={k:e for k,e in pending.items() if 'event:'+k not in sent}
    for group in groups(remaining.items()):
        if not any(e['urgent'] for _,e in group) or used>=cap:continue
        keys=[k for k,_ in group];identity=hashlib.sha256(json.dumps(keys,sort_keys=True).encode()).hexdigest()[:20]
        labels='\n'.join('• '+e['label'] for _,e in group)
        first=group[0][1]
        deliver('smart:'+identity,f'[중요 변화 {len(group)}개]\n{first["text"].splitlines()[0]}\n{labels}\n{first["date"]} 확정 일봉',keys)
    # Weekdays: one afternoon digest. Weekend schedule has only a morning run.
    due=local.weekday()>=5 or (local.hour,local.minute)>=(16,30)
    summary_key='summary:v2:'+day
    remaining={k:e for k,e in pending.items() if 'event:'+k not in sent}
    if due and summary_key not in sent:
        # One block per ticker; cap limits ticker groups, not individual reasons.
        chunks=groups(remaining.items())[:max(0,cap-used)]
        if not chunks:chunks=[[]]
        for chunk in chunks:
            keys=[k for k,_ in chunk]
            batch=hashlib.sha256(json.dumps(keys,sort_keys=True).encode()).hexdigest()[:20]
            text='[일일 시세 점검] '+day+'\n'+detail+'\n\n'
            text+=(chunk[0][1]['text'].splitlines()[0]+'\n'+'\n'.join('• '+e['label'] for _,e in chunk)+'\n'+chunk[0][1]['date']+' 확정 일봉' if chunk else f'일일 알림 상한 도달 · 다음 실행 대기 {len(remaining)}건' if remaining else '새 기술 조건 전환 없음. 미확인·실패 항목의 안전을 의미하지 않습니다.')
            text+='\n기술 신호는 기록된 기준일의 전환입니다. 현재 지속 여부는 앱에서 확인하세요.'
            deliver(summary_key+':'+batch,text,keys)
        sent.add(summary_key)
    planned['pending']=[e for k,e in pending.items() if 'event:'+k not in sent]
    planned['daily_count']=state.get('daily_count',{})
    planned['sent']=sorted(sent)[-5000:];save(path,planned,password)
    print('Telegram notification pass complete.')

if __name__=='__main__':
    try:main()
    except Exception as e:
        # URLs and response bodies may contain tokens or private holdings.
        print('Telegram notification failed:',type(e).__name__);sys.exit(1)
