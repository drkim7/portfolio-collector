"""Completed-bar transition helpers for the existing encrypted Telegram state."""
import math

BUFFER=.005

def positive(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) and value>0

def breakout_transition(bars, previous):
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

def condition_recovery(h,bars):
    plan=h.get('recovery') or {};stages=plan.get('tranches') or []
    if not isinstance(stages,list):return []
    last=bars[-1];close=last['close'];out=[]
    for j,t in enumerate(stages[:3]):
        if not isinstance(t,dict) or t.get('recorded') or t.get('execution'):continue
        checks=t.get('conditions') or []
        if not checks:continue
        states=[]
        for c in checks:
            kind=c.get('type');result=None
            if kind in ('ma20','ma60'):
                n=20 if kind=='ma20' else 60
                if len(bars)>=n:result=close>sum(x['close'] for x in bars[-n:])/n
            elif kind=='above-stop' and positive(h.get('stop') or h.get('stopAnchor')):result=close>(h.get('stop') or h.get('stopAnchor'))
            elif kind=='low-hold' and len(bars)>=10:result=min(x['low'] for x in bars[-5:])>=min(x['low'] for x in bars[-10:-5])
            elif kind=='volume' and len(bars)>=21:
                window=[x.get('volume') for x in bars[-21:]]
                if all(v is not None for v in window) and sum(window[:-1])>0:result=window[-1]/(sum(window[:-1])/20)>=c.get('threshold',1.5)
            elif kind=='thesis' and isinstance(h.get('tech'),dict):result=h['tech'].get('thesis')=='valid' if h['tech'].get('thesis') in ('valid','broken','uncertain') else None
            elif kind=='financing' and isinstance(h.get('tech'),dict):result=h['tech'].get('financing') in ('low','not_applicable') if h['tech'].get('financing') in ('low','high','not_applicable') else None
            # App-only risk room, RSI and MACD conditions are unknown here: fail closed.
            states.append(result)
        out.append((str(t.get('id') or j+1),len(states)>0 and all(v is True for v in states),f'{j+1}차 복구 조건 {sum(v is True for v in states)}/{len(states)} 충족'))
    return out
