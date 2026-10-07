"""Private Telegram alerts from completed encrypted collector output. No orders."""
import copy, hashlib, json, math, os, sys, time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from pipeline import encrypt, decrypt, load_holdings, is_crypto
from smart_alerts import breakout_transition, condition_recovery, new_low_transition, technical_bars

DESK='https://dh-portfolio-desk.drkim7.chatgpt.site'
ALERT_SCHEMA=28
def positive(x):
    return isinstance(x,(int,float)) and not isinstance(x,bool) and math.isfinite(x) and x>0

def episode_step(previous, active, rearm, bar_date):
    """One completed local exchange bar per step; two buffered bars re-arm."""
    if previous and previous.get('date','')>=bar_date:return previous,False
    if previous is None:
        return {'date':bar_date,'active':active,'armed':not active,'episode':0,'rearmCounter':0,'lastTransitionBarDate':bar_date},False
    state=dict(previous)
    state.setdefault('armed',not state.get('active',False));state.setdefault('episode',0)
    state.setdefault('rearmCounter',0)
    fire=bool(active and state['armed'])
    if fire:
        state['armed']=False;state['rearmCounter']=0;state['lastTransitionBarDate']=bar_date
    elif not state['armed']:
        state['rearmCounter']=state['rearmCounter']+1 if rearm else 0
        if state['rearmCounter']>=2:
            state['armed']=True;state['episode']+=1;state['rearmCounter']=0
    state['date']=bar_date;state['active']=active
    return state,fire

def conditions(h,b):
    last=b[-1]; p=last['close']; result=[]
    def add(kind,level,active,label,urgent=False,rearm=None):
        key=hashlib.sha256(json.dumps([h['id'],kind,level],sort_keys=True).encode()).hexdigest()
        result.append({'key':key,'kind':kind,'active':active,'rearm':not active if rearm is None else rearm,'label':label,'urgent':urgent,'date':last['date']})
    stop=h.get('stop') or h.get('stopAnchor')
    if positive(stop):add('stop',stop,p<=stop,f'이탈선 {stop:,.2f} 이하',True,p>stop*1.01)
    if positive(h.get('target')):add('target',h['target'],p>=h['target'],f'목표가 {h["target"]:,.2f} 도달',True,p<h['target']*.99)
    if positive(h.get('target')):add('target-near',h['target'],h['target']*.98<=p<h['target'],f'목표가 {h["target"]:,.2f} 2% 이내',rearm=p<h['target']*.97 or p>h['target']*1.01)
    if positive(stop):add('stop-near',stop,stop<p<=stop*1.02,f'이탈선 {stop:,.2f} 2% 이내',True,p>stop*1.03 or p<stop*.99)
    if positive(h.get('buy')):add('avg-recovery',h['buy'],p>=h['buy'],f'단순 평단 {h["buy"]:,.2f} 회복',rearm=p<h['buy']*.99)
    if positive(h.get('economicBep')):add('bep-recovery',h['economicBep'],p>=h['economicBep'],f'입력한 경제적 BEP {h["economicBep"]:,.2f} 회복',rearm=p<h['economicBep']*.99)
    for s in (h.get('plan') or {}).get('stages',[]):
        if not s.get('done') and positive(s.get('price')):
            add('stage:'+str(s.get('id')),s['price'],p>=s['price'],f'미실행 매도 계획가 {s["price"]:,.2f} 도달',True,p<s['price']*.99)
    for n in (20,60,120):
        if len(b)>=n:
            window=b[-n:]
            adjusted=all(x.get('adjustment')=='total-return' and positive(x.get('adjustedClose')) for x in window)
            avg=sum(x['adjustedClose'] if adjusted else x['close'] for x in window)/n
            p=last['adjustedClose'] if adjusted else last['close']
            add(f'ma{n}-up',None,p>avg*1.005,f'{n}일선 위로 전환',rearm=p<avg*.995)
            add(f'ma{n}-down',None,p<avg*.995,f'{n}일선 아래로 전환',rearm=p>avg*1.005)
    if len(b)>=21:
        volumes=[x.get('volume') for x in b[-21:]]
        if all(v is not None for v in volumes) and sum(volumes[:-1])>0:
            ratio=volumes[-1]/(sum(volumes[:-1])/20)
            add('volume-high',1.5,ratio>=1.5,f'거래량 직전 20일 평균 대비 {ratio:.2f}배',rearm=ratio<1.35)
    for step,active,label in condition_recovery(h,b):add('recovery:'+step,None,active,label)
    return result

def evaluate(holdings,patch,state,now,limits=None,fx=None,enabled_types=None,trusted_holdings=True,tiny_pct=.3):
    next_state=copy.deepcopy(state);next_state.setdefault('rules',{});next_state.setdefault('breakout',{});next_state.setdefault('newLow',{});events=[]
    by_id={h.get('id'):h for h in holdings if h.get('id') and positive(h.get('qty'))}
    values={}
    if trusted_holdings and (positive(fx) or all(h.get('mkt')!='US' for h in by_id.values())):
        for u in patch.get('updates',[]):
            h=by_id.get(u.get('id'))
            if not h or not positive(u.get('price')):continue
            try:age=(now-datetime.fromisoformat(u['quoteAsOf'])).total_seconds()
            except (ValueError,KeyError,TypeError):continue
            if 0<=age<=5*86400:values[h['id']]=u['price']*h['qty']*(fx if h.get('mkt')=='US' else 1)
    nav=sum(values.values()) if len(values)==len(by_id) else 0
    for u in patch.get('updates',[]):
        h=by_id.get(u.get('id')); b=u.get('bars') or []
        if not h or len(b)<2 or u.get('name')!=h.get('name') or u.get('mkt')!=h.get('mkt','KR') or u.get('acct')!=h.get('acct'):continue
        try:age=(now-datetime.fromisoformat(u['quoteAsOf'])).total_seconds()
        except (ValueError,KeyError,TypeError):continue
        if not 0<=age<=5*86400:continue
        if u.get('quoteDate') and u['quoteDate']!=b[-1].get('date'):continue
        if nav>0 and values.get(h['id'],nav)/nav*100<tiny_pct:continue
        window=b[-120:]
        adjusted=all(x.get('adjustment')=='total-return' and positive(x.get('adjustedClose')) for x in window)
        prices=[x['adjustedClose'] if adjusted else x['close'] for x in window]
        if any(prices[j]/prices[j-1]>1.5 or prices[j]/prices[j-1]<.5 for j in range(1,len(prices))):
            next_state.setdefault('dataHealth',{})[h['id']]={'date':b[-1]['date'],'reason':'price-discontinuity'}
            continue
        if h.get('isETF') and b[-1]['date'] in (h.get('distributionDates') or []) and not adjusted:
            next_state.setdefault('dataHealth',{})[h['id']]={'date':b[-1]['date'],'reason':'unverified-ex-date'}
            continue
        basis='total-return' if adjusted else 'raw'
        previous_basis=state.get('basis',{}).get(h['id'])
        reset_basis=previous_basis is not None and previous_basis!=basis
        next_state.setdefault('basis',{})[h['id']]=basis
        if h.get('isETF') and not adjusted and not h.get('distributionDates') and not all(x.get('distributionVerified') is True for x in window):
            next_state.setdefault('dataHealth',{})[h['id']]={'date':b[-1]['date'],'reason':'unverified-distribution-history'}
            continue
        old_break=None if reset_basis else state.get('breakout',{}).get(h['id'])
        if old_break and (datetime.fromisoformat(b[-1]['date'])-datetime.fromisoformat(old_break['date'])).days>20:old_break=None
        nxt,change=breakout_transition(b,old_break)
        if nxt:next_state['breakout'][h['id']]=nxt
        if change:
            key=hashlib.sha256(json.dumps([h['id'],'breakout',change['state']]).encode()).hexdigest()
            if enabled_types is None or 'breakout' in enabled_types:events.append({'key':key,'kind':'breakout','itemId':h['id'],'active':True,'label':change['label'],'urgent':change['state'] in ('first-break','failed','re-break'),'date':change['date'],'text':f'{h["name"][:100]}\n{change["label"]}\n{change["date"]} 확정 종가 {b[-1]["close"]:,.2f} {"USD" if h.get("mkt")=="US" else "KRW"}'})
        old_low=None if reset_basis else state.get('newLow',{}).get(h['id'])
        if old_low and (datetime.fromisoformat(b[-1]['date'])-datetime.fromisoformat(old_low['date'])).days>20:old_low=None
        next_low,low_change=new_low_transition(b,old_low)
        if next_low:next_state['newLow'][h['id']]=next_low
        if low_change and enabled_types is not None and 'new-low' in enabled_types:
            key=hashlib.sha256(json.dumps([h['id'],'new-low',low_change['state'],low_change['date']]).encode()).hexdigest()
            events.append({'key':key,'kind':'new-low','itemId':h['id'],'active':True,'label':low_change['label'],'urgent':False,'date':low_change['date'],'text':f'{h["name"][:100]}\n{low_change["label"]}\n{low_change["date"]} 확정 일봉 · 보유 재검토용'})
        for c in conditions(h,technical_bars(b)):
            old=None if reset_basis else state.get('rules',{}).get(c['key'])
            if old and (datetime.fromisoformat(c['date'])-datetime.fromisoformat(old['date'])).days>20:old=None
            nxt,fire=episode_step(old,c['active'],c['rearm'],c['date'])
            next_state['rules'][c['key']]=nxt
            category='ma' if c['kind'].startswith('ma') else 'volume' if c['kind'].startswith('volume') else 'recovery' if c['kind'].startswith('recovery:') else 'avg' if 'recovery' in c['kind'] else 'level'
            if not trusted_holdings and category not in ('ma','volume'):continue
            regression=category=='recovery' and old and old.get('date','')<c['date'] and old.get('active') and not c['active']
            if (fire or regression) and (enabled_types is None or category in enabled_types):
                events.append({**c,'key':c['key']+':'+str(nxt['episode'])+(':waiting' if regression else ''),'itemId':h['id'],'text':f'{h["name"][:100]}\n{c["label"]}\n{c["date"]} 확정 종가 {b[-1]["close"]:,.2f} {"USD" if h.get("mkt")=="US" else "KRW"}'})
    # Concentration is comparable only when every held row has a fresh close.
    if trusted_holdings and isinstance(limits,dict) and positive(fx):
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
                nxt,fire=episode_step(old,active,amount/total*100<limit-.2,day)
                next_state['rules'][key]=nxt
                if fire:
                    label=f'{tag} 한도 '+('초과' if active else '이내 복귀')+f' · {amount/total*100:.1f}% / {limit:.1f}%'
                    if enabled_types is None or 'limit' in enabled_types:events.append({'key':key+':'+str(nxt['episode']),'kind':'limit','itemId':'portfolio:'+tag,'active':active,'label':label,'urgent':active,'date':day,'text':f'{tag}\n{label}\n{day} 확정 일봉'})
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

def valid_state(state):
    if not isinstance(state,dict) or state.get('schemaVersion')!=ALERT_SCHEMA:return False
    rules=state.get('rules',{})
    if not isinstance(rules,dict) or len(rules)>30000:return False
    for value in rules.values():
        if not isinstance(value,dict) or not isinstance(value.get('date'),str) or not isinstance(value.get('active'),bool):return False
        if not isinstance(value.get('armed'),bool) or not isinstance(value.get('episode'),int) or value['episode']<0:return False
        if not isinstance(value.get('rearmCounter'),int) or not 0<=value['rearmCounter']<2:return False
    for key in ('breakout','newLow'):
        values=state.get(key,{})
        if not isinstance(values,dict):return False
        for value in values.values():
            if not isinstance(value,dict) or not isinstance(value.get('date'),str) or value.get('state') not in ('below','first-break','holding','failed','re-break','normal','new-low','extending','reclaim'):return False
    if any(not isinstance(x,str) for x in state.get('sent',[])):return False
    if any(not isinstance(x,dict) or not all(isinstance(x.get(k),str) for k in ('key','kind','itemId','date','label','text')) or not isinstance(x.get('urgent'),bool) for x in state.get('pending',[])):return False
    return all(isinstance(state.get(k,{}),dict) for k in ('breakout','newLow','daily_count')) and all(isinstance(state.get(k,[]),list) for k in ('sent','pending'))

def cluster_events(events,clusters,holdings,trusted):
    if not trusted:return events
    taken=set();result=[];names={h['id']:h.get('name',h['id']) for h in holdings}
    for c in clusters or []:
        if not isinstance(c,dict) or not c.get('confirmedAt'):continue
        groups={}
        for j,e in enumerate(events):
            if j in taken or e.get('itemId') not in c.get('memberIds',[]) or not (e['kind'].startswith('ma') or e['kind'] in ('volume-high','breakout','new-low')):continue
            groups.setdefault((e['date'],e['kind'],e.get('active',True)),[]).append((j,e))
        for (date,kind,_),members in groups.items():
            label=members[0][1]['label']
            if len({e['itemId'] for _,e in members})<3:continue
            taken.update(j for j,_ in members)
            identity=hashlib.sha256(json.dumps([c['id'],date,kind,sorted(e['key'] for _,e in members)]).encode()).hexdigest()
            compact=' · '.join(names[e['itemId']][:40] for _,e in members)
            result.append({'key':identity,'kind':kind,'itemId':'cluster:'+c['id'],'date':date,'active':True,'urgent':any(e['urgent'] for _,e in members),'label':f'{label} · {len(members)}종목 ({compact})','text':f'{c.get("name","위험 묶음")} 묶음\n{label}\n{compact}\n{date} 확정 일봉'})
    return result+[e for j,e in enumerate(events) if j not in taken]

def main(sender=send):
    token=os.getenv('TELEGRAM_BOT_TOKEN','').strip();chat=os.getenv('TELEGRAM_CHAT_ID','').strip()
    if not token or not chat:print('Telegram not configured; report remains available.');return
    password=os.getenv('REPORT_PASSWORD','');path=Path('state/alerts.enc')
    if len(password)<8:raise ValueError('Password missing')
    mode=os.getenv('V28_ALERT_MODE','').strip().lower() or 'shadow'
    if mode not in ('shadow','live','off'):raise ValueError('Invalid alert mode')
    # A missing/corrupt/old state is a baseline, never a replay of live alerts.
    try:
        state=json.loads(decrypt(json.loads(path.read_text()),password)) if path.exists() else None
        if not valid_state(state):state=None
    except Exception:state=None  # Decode scope only: includes AES-GCM InvalidTag; never replay on state loss.
    baseline=state is None or not state.get('initialized')
    if baseline:state={'schemaVersion':ALERT_SCHEMA,'rules':{},'breakout':{},'sent':[],'pending':[]}
    outcome=os.getenv('COLLECTION_OUTCOME','success')
    deployed=os.getenv('DEPLOY_OUTCOME','success')
    if outcome!='success' or deployed!='success':
        health='collection_failed' if outcome!='success' else 'publish_failed'
        message=health_message(state.get('health'),health,'')
        if message:
            if mode!='off':sender(token,chat,message+'\n실행 내역: https://github.com/drkim7/portfolio-collector/actions')
            state['health']=health;save(path,state,password)
        print('Notification health check complete.');return
    patch=json.loads(decrypt(json.loads(Path('site/quotes-patch.enc').read_text()),password))
    holdings,limits,fx=load_holdings();now=datetime.now(timezone.utc)
    if state.get('lastEvaluatedAt'):
        try:restart=(now-datetime.fromisoformat(state['lastEvaluatedAt'])).total_seconds()>20*86400
        except (ValueError,TypeError):restart=True
        if restart:state={'schemaVersion':ALERT_SCHEMA,'rules':{},'breakout':{},'sent':[],'pending':[]};baseline=True
    health,detail=data_status(patch,now)
    message=health_message(state.get('health'),health,detail) if mode!='off' and not baseline and os.getenv('SMART_ALERT_HEALTH','1').strip()!='0' else None
    if message:
        sender(token,chat,message+'\n'+DESK)
        state['health']=health;save(path,state,password);time.sleep(1.1)
    else:state['health']=health
    types=os.getenv('SMART_ALERT_TYPES','').strip()
    enabled=set(x.strip() for x in types.split(',') if x.strip()) if types else None
    from pipeline import holdings_hash
    match=os.getenv('APP_HOLDINGS_HASH','').strip()
    snapshot_time=patch.get('holdingsAsOf')
    try:snapshot_age=(now-datetime.fromisoformat(snapshot_time)).total_seconds()
    except (ValueError,TypeError):snapshot_age=float('inf')
    trusted=bool(match and match==patch.get('holdingsHash')==holdings_hash(holdings) and 0<=snapshot_age<=7*86400)
    source=json.loads(os.getenv('HOLDINGS_JSON','{}') or '{}')
    if not isinstance(source,dict):source={}
    tiny=source.get('tinyHoldingPct',.3)
    if not isinstance(tiny,(int,float)) or not 0<=tiny<=5:tiny=.3
    planned,events=evaluate(holdings,patch,state,now,limits,fx,enabled,trusted_holdings=trusted,tiny_pct=tiny)
    events=cluster_events(events,source.get('riskClusters',[]),holdings,trusted)
    planned['schemaVersion']=ALERT_SCHEMA
    planned['lastEvaluatedAt']=now.isoformat()
    planned['shadowEvents']=events[-1000:] if mode=='shadow' and not baseline else []
    sent=set(state.get('sent',[]));planned['sent']=list(sent)
    if baseline or not state.get('initialized'):
        if mode!='off':sender(token,chat,'투자 데스크 알림 기준 상태 등록 완료\n새 상태·종목은 과거 조건을 투자 알림으로 재생하지 않습니다. 현재 모드: '+mode+'\n'+DESK)
        planned['initialized']=True;planned['pending']=[];save(path,planned,password);print('Telegram baseline initialized; investment alerts 0.');return
    if mode!='live':
        planned['pending']=[];save(path,planned,password)
        print('Telegram shadow review complete.' if mode=='shadow' else 'Telegram alerts off.')
        return
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
