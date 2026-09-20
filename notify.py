"""Private Telegram alerts from completed encrypted collector output. No orders."""
import copy, hashlib, json, math, os, sys, time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from pipeline import encrypt, decrypt, load_holdings

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
        add('breakout',None,anchor is not None,'252거래일 고점 돌파')
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

def main(sender=send):
    token=os.getenv('TELEGRAM_BOT_TOKEN','').strip();chat=os.getenv('TELEGRAM_CHAT_ID','').strip()
    if not token or not chat:print('Telegram not configured; report remains available.');return
    password=os.getenv('REPORT_PASSWORD','');path=Path('state/alerts.enc')
    if len(password)<8:raise ValueError('Password missing')
    # Corrupt state must stop sending, never silently reset deduplication.
    state=json.loads(decrypt(json.loads(path.read_text()),password)) if path.exists() else {'rules':{},'sent':[]}
    patch=json.loads(decrypt(json.loads(Path('site/quotes-patch.enc').read_text()),password))
    holdings,_,_=load_holdings();now=datetime.now(timezone.utc)
    planned,events=evaluate(holdings,patch,state,now)
    sent=set(state.get('sent',[]));planned['sent']=list(sent)
    if not state.get('initialized'):
        sender(token,chat,'투자 데스크 알림 연결 완료\n현재 상태를 기준으로 등록했습니다. 이미 충족한 조건은 일괄 발송하지 않습니다. 이후 새 일봉에서 조건이 전환되면 알립니다.\n기준: GitHub에 저장된 보유내역·계획 (앱 변경 후 별도 갱신 필요)\n'+DESK)
        planned['initialized']=True;save(path,planned,password);print('Telegram baseline initialized.');return
    messages=[]
    for e in events:
        if e['urgent']:messages.append((e['key']+':'+e['date'],'[매매 기준 확인]\n'+e['text']))
    other=[e for e in events if not e['urgent']]
    day=now.astimezone(__import__('zoneinfo').ZoneInfo('Asia/Seoul')).date().isoformat()
    summary_key='summary:'+day
    if other and summary_key not in sent:
        text='[일봉 변화 요약]\n'+'\n\n'.join(e['text'] for e in other)
        if len(text)>2800:text=text[:2700]+'\n…나머지는 투자 데스크에서 확인하세요.'
        messages.append((summary_key,text))
    # Keep old rule states until all messages finish; successful sends are checkpointed independently.
    for key,text in messages:
        if key in sent:continue
        sender(token,chat,text+'\n수집된 종가 기준 · 주문 지시 아님\n'+DESK)
        sent.add(key);state['sent']=sorted(sent)[-5000:];save(path,state,password);time.sleep(1.1)
    planned['sent']=sorted(sent)[-5000:];save(path,planned,password)
    print('Telegram notification pass complete.')

if __name__=='__main__':
    try:main()
    except Exception as e:
        # URLs and response bodies may contain tokens or private holdings.
        print('Telegram notification failed:',type(e).__name__);sys.exit(1)
