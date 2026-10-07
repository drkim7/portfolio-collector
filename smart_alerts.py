"""Completed-bar transition helpers for the existing encrypted Telegram state."""
import math

BUFFER=.005

def positive(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) and value>0

def breakout_transition(bars, previous):
    bars=technical_bars(bars)
    if len(bars)<253:return None,None
    last=bars[-1];date=last['date'];reference=max(x['high'] for x in bars[-253:-1]);close=last['close']
    if previous and previous.get('date','')>=date:return previous,None
    old=previous.get('state') if previous else 'below'
    anchor=previous.get('reference') if previous else None
    state=old
    if old=='below' and close>reference:state='first-break';anchor=reference
    elif old in ('first-break','holding','re-break') and positive(anchor):state='failed' if close<anchor*(1-BUFFER) else 'holding'
    elif old=='failed' and positive(anchor) and close>anchor*(1+BUFFER):state='re-break'
    nxt={'date':date,'state':state,'reference':anchor}
    if not previous or state==old:return nxt,None
    volume=last.get('volume');window=[x.get('volume') for x in bars[-21:-1]]
    avg=sum(window)/20 if len(window)==20 and all(x is not None and x>=0 for x in window) and sum(window)>0 else None
    ratio=volume/avg if avg and volume is not None else None
    labels={'first-break':'52주 고점 최초 돌파','holding':'돌파선 위 유지','failed':'돌파선 재이탈','re-break':'52주 고점 재돌파'}
    detail=labels[state]+f' · 기준 {anchor:,.2f}'
    if ratio is not None:detail+=f' · 거래량 {ratio:.2f}배'+(' 확인' if ratio>=1.5 else ' 미동반')
    return nxt,{'state':state,'label':detail,'date':date,'reference':anchor,'volumeRatio':ratio}

def technical_bars(bars):
    if not bars or not all(b.get('adjustment')=='total-return' and positive(b.get('adjustedClose')) for b in bars):return bars
    anchor=bars[-1]['close']/bars[-1]['adjustedClose']
    return [dict(b,**{k:b.get(k,b['close'])*b['adjustedClose']/b['close']*anchor for k in ('open','high','low','close')}) for b in bars]

def new_low_transition(bars,previous):
    bars=technical_bars(bars)
    if len(bars)<61:return None,None
    last=bars[-1];day=last['date'];reference=min(x['low'] for x in bars[-253:-1]);close=last['close']
    if previous and previous.get('date','')>=day:return previous,None
    if previous is None:
        state='new-low' if close<reference else 'normal'
        return {'date':day,'state':state,'anchor':close if state=='new-low' else None},None
    old=previous['state'];anchor=previous.get('anchor');state=old
    if old in ('normal','reclaim'):state='new-low' if close<reference else 'normal'
    elif positive(anchor) and close>anchor*1.01:state='reclaim'
    elif positive(anchor) and close<anchor:state='extending'
    if state in ('new-low','extending'):anchor=close
    nxt={'date':day,'state':state,'anchor':anchor}
    if state==old:return nxt,None
    label=('52주 신저가' if len(bars)>=253 else f'상장 후 저점 ({len(bars)}일)')+' · '+state
    return nxt,{'state':state,'date':day,'reference':reference,'label':label}

def condition_recovery(h,bars):
    plan=h.get('recovery') or {};stages=plan.get('tranches') or []
    if not isinstance(stages,list):return []
    last=bars[-1];close=last['close'];out=[]
    for j,t in enumerate(stages[:3]):
        if not isinstance(t,dict) or t.get('recorded') or t.get('execution'):continue
        checks=t.get('conditions') or []
        if not checks:continue
        states=[];details=[];missing=[]
        for c in checks:
            if not isinstance(c,dict):states.append(None);missing.append('알 수 없는 조건');continue
            kind=c.get('type');result=None;detail=None
            if kind in ('ma20','ma60'):
                n=20 if kind=='ma20' else 60
                if len(bars)>=n:
                    ma=sum(x['close'] for x in bars[-n:])/n;result=close>ma;detail=f'{n}일선 {close:,.2f}/{ma:,.2f}'
            elif kind=='above-stop' and positive(h.get('stop') or h.get('stopAnchor')):
                line=h.get('stop') or h.get('stopAnchor');result=close>line;detail=f'이탈선 {close:,.2f}/{line:,.2f}'
            elif kind=='low-hold' and len(bars)>=10:
                recent=min(x['low'] for x in bars[-5:]);prior=min(x['low'] for x in bars[-10:-5]);result=recent>=prior;detail=f'최근 저점 {recent:,.2f}/{prior:,.2f}'
            elif kind=='volume' and len(bars)>=21:
                window=[x.get('volume') for x in bars[-21:]]
                threshold=c.get('threshold',1.5)
                if all(v is not None for v in window) and sum(window[:-1])>0 and positive(threshold):
                    ratio=window[-1]/(sum(window[:-1])/20);result=ratio>=threshold;detail=f'거래량 {ratio:.2f}배/{threshold:.2f}배'
            elif kind=='thesis' and isinstance(h.get('tech'),dict):
                value=h['tech'].get('thesis');result=value=='valid' if value in ('valid','broken','uncertain') else None;detail='투자 논리 '+str(value or '미기록')
            elif kind=='financing' and isinstance(h.get('tech'),dict):
                value=h['tech'].get('financing');result=value in ('low','not_applicable') if value in ('low','high','not_applicable') else None;detail='자금조달 위험 '+str(value or '미기록')
            # App-only risk room, RSI and MACD conditions are unknown here: fail closed.
            description=detail or {'ma20':'20일선','ma60':'60일선','above-stop':'이탈선','low-hold':'최근 저점','volume':'거래량','thesis':'투자 논리','financing':'자금조달 위험'}.get(kind,str(kind or '알 수 없는 조건'))+' 자료 부족/앱에서 확인'
            states.append(result)
            if result is True:details.append(description)
            else:missing.append(description)
        active=len(states)>0 and all(v is True for v in states)
        summary=f'복구계획 {j+1}차 조건 {sum(v is True for v in states)}/{len(states)} '+('확인' if active else '대기')
        label=summary+' · 근거: '+(' / '.join(details[:3]) if details else '충족한 조건 없음')+' · 다음: '+('실제 실행·회수 계획 확인' if active else ' / '.join(missing[:3]))+' · 앱의 복구 코치 0~5단계와 별개'
        out.append((str(t.get('id') or j+1),active,label))
    return out
