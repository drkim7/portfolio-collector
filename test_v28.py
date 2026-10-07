"""Synthetic-only v28 release gates; network/Telegram are never called."""
import json, os, tempfile, unittest
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import Mock, patch
from notify import episode_step, conditions, evaluate, main, save, cluster_events, valid_state
from pipeline import encrypt, decrypt, holdings_hash, clean
from smart_alerts import new_low_transition
V=json.loads(Path('fixtures/event-vectors.json').read_text())
NOW=datetime(2026,10,6,10,tzinfo=timezone.utc)
H=[dict(id='synthetic',name='Synthetic',code='TEST',mkt='US',acct='Fixture',qty=10,buy=150,stop=80,tags=[])]
def bars(n=121):
 from datetime import timedelta
 start=datetime(2026,3,1)
 return [dict(date=(start+timedelta(days=j)).date().isoformat(),open=100,high=101,low=99,close=100,volume=1000) for j in range(n)]
def update(b,h=None):
 h=h or H[0]
 return dict(id=h['id'],name=h['name'],code=h['code'],mkt=h['mkt'],acct=h['acct'],quoteDate=b[-1]['date'],quoteAsOf=NOW.isoformat(),price=b[-1]['close'],bars=b)
class SharedVectors(unittest.TestCase):
 def test_episode_vectors(self):
  for v in V['episodeVectors']:
   state=None
   for x in v['steps']:
    state,fire=episode_step(state,x['active'],x['rearm'],x['date'])
    self.assertEqual((fire,state['episode'],state['armed']),(x['fire'],x['episode'],x['armed']),v['name'])
 def test_market_vectors(self):
  for v in V['marketVectors']:
   c=next(c for c in conditions(H[0],v['bars']) if c['kind']=='volume-high')
   self.assertEqual(c['label'],v['expectedVolumeLabel']);self.assertEqual(c['active'],v['expectedVolumeActive'])
 def test_hash_vectors(self):
  for v in V['hashVectors']:self.assertEqual(holdings_hash(v['holdings']),v['expected'])
class Safety(unittest.TestCase):
 def test_ex_date_and_adjusted(self):
  b=bars();h=dict(H[0],isETF=True,distributionDates=[b[-1]['date']]);p={'updates':[update(b,h)]}
  s,e=evaluate([h],p,{},NOW);self.assertEqual(e,[]);self.assertEqual(s['dataHealth'][h['id']]['reason'],'unverified-ex-date')
  p['updates'][0]['bars']=[dict(x,adjustedClose=100,adjustment='total-return') for x in b]
  s,e=evaluate([h],p,{},NOW);self.assertNotIn('dataHealth',s);self.assertEqual(e,[])
 def test_split_guard(self):
  b=bars();b[-1]=dict(b[-1],open=40,high=41,low=39,close=40)
  s,e=evaluate(H,{'updates':[update(b)]},{},NOW);self.assertEqual(e,[]);self.assertEqual(s['dataHealth'][H[0]['id']]['reason'],'price-discontinuity')
 def test_state_schema_and_corruption_validation(self):
  self.assertFalse(valid_state({'schemaVersion':28,'rules':{'bad':{'active':True}}}));self.assertFalse(valid_state({'schemaVersion':27}));self.assertTrue(valid_state({'schemaVersion':28,'rules':{}}))
 def test_new_item_and_long_gap_zero(self):
  b=bars();s,_=evaluate(H,{'updates':[update(b)]},{},NOW)
  for value in s['rules'].values():value['date']='2025-01-01'
  b[-1]=dict(b[-1],close=110,high=111)
  _,e=evaluate(H,{'updates':[update(b)]},s,NOW);self.assertEqual(e,[])
  _,e=evaluate([dict(H[0],id='new')],{'updates':[update(b,dict(H[0],id='new'))]},s,NOW);self.assertEqual(e,[])
 def test_tiny_excluded_only_when_complete(self):
  hs=[dict(H[0],qty=.001),dict(H[0],id='large',qty=10000)];b=bars();s,_=evaluate(hs,{'updates':[update(b,h) for h in hs]},{},NOW,fx=1400)
  b.append(dict(b[-1],date='2026-07-01',close=110,high=111))
  _,e=evaluate(hs,{'updates':[update(b,h) for h in hs]},s,NOW,fx=1400)
  self.assertTrue(all(e['itemId']=='large' for e in e));self.assertTrue(e)
 def test_drift_suppresses_cost_plan_alerts_but_not_market(self):
  b=bars();s,_=evaluate(H,{'updates':[update(b)]},{},NOW)
  b.append(dict(b[-1],date='2026-07-01',close=160,open=150,low=149,high=161))
  # Avoid an artificial split warning while crossing average.
  b[-2]=dict(b[-2],close=140,open=140,low=139,high=141)
  _,e=evaluate(H,{'updates':[update(b)]},s,NOW,trusted_holdings=False)
  self.assertTrue(all(x['kind'].startswith('ma') or x['kind']=='volume-high' for x in e))
 def test_cluster_compact_and_company_specific_exception(self):
  events=[dict(key=str(j),kind='ma20-down',itemId=str(j),date='2026-09-01',label='20일선 아래',active=True,urgent=False,text='Fixture') for j in range(3)]
  events.append(dict(events[0],key='company',kind='research',label='기업 공시'))
  c=[dict(id='r',name='Synthetic risk',memberIds=['0','1','2'],confirmedAt='2026-09-01T00:00:00Z')]
  out=cluster_events(events,c,[dict(id=str(j),name='Synthetic '+str(j)) for j in range(3)],True)
  self.assertEqual(len(out),2);self.assertTrue(any(e['kind']=='research' for e in out));self.assertTrue(any(e['itemId']=='cluster:r' for e in out));self.assertEqual(cluster_events(events,c,[],False),events)
 def test_metadata_not_guessed(self):
  b=bars(2);out,_=clean(b,'US',NOW,continuous=True);self.assertNotIn('adjustment',out[0]);b=[dict(x,adjustment='total-return',adjustedClose=100,distributionVerified=True,cashDistribution=1) for x in b];out,_=clean(b,'US',NOW,continuous=True);self.assertEqual(out[0]['cashDistribution'],1);self.assertEqual(out[0]['adjustment'],'total-return')
 def test_new_low_minimum_and_reclaim(self):
  self.assertEqual(new_low_transition(bars(59),None),(None,None));b=bars(60);state=None
  b.append(dict(b[-1],date='2026-05-01',close=98,open=98,high=99,low=97));state,event=new_low_transition(b,None);self.assertEqual(state['state'],'new-low');self.assertIsNone(event)
  b.append(dict(b[-1],date='2026-05-02',close=97,open=97,high=98,low=96));state,event=new_low_transition(b,state);self.assertEqual(event['state'],'extending')
  b.append(dict(b[-1],date='2026-05-03',close=99,open=99,high=100,low=98));_,event=new_low_transition(b,state);self.assertEqual(event['state'],'reclaim')
class Modes(unittest.TestCase):
 def run_synthetic(self,mode=None,state=None,corrupt=False,partial=False):
  sender=Mock();cwd=os.getcwd();password='synthetic-password';b=bars();b[-1]['date']='2026-10-05';blob={'updates':[update(b)],'holdingsAsOf':NOW.isoformat(),'holdingsHash':holdings_hash(H)}
  if partial:blob['failures']=[{'id':'synthetic-failure'}]
  env={'TELEGRAM_BOT_TOKEN':'synthetic','TELEGRAM_CHAT_ID':'synthetic','REPORT_PASSWORD':password,'APP_HOLDINGS_HASH':holdings_hash(H)}
  if mode is not None:env['V28_ALERT_MODE']=mode
  with tempfile.TemporaryDirectory() as tmp:
   try:
    os.chdir(tmp);Path('site').mkdir();Path('site/quotes-patch.enc').write_text(json.dumps(encrypt(json.dumps(blob),password)))
    if corrupt:
     Path('state').mkdir();Path('state/alerts.enc').write_text(json.dumps(encrypt('{}','wrong-password')))
    elif state is not None:save(Path('state/alerts.enc'),state,password)
    with patch.dict(os.environ,env,clear=True),patch('notify.load_holdings',return_value=(H,{},1400)),patch('notify.datetime') as clock,patch('notify.time.sleep'):
     clock.now.return_value=NOW;clock.fromisoformat.side_effect=datetime.fromisoformat
     main(sender)
    final=json.loads(decrypt(json.loads(Path('state/alerts.enc').read_text()),password))
    return sender,final
   finally:os.chdir(cwd)
 def test_missing_corrupt_schema_baseline_one_operational_message(self):
  for kwargs in ({},{'corrupt':True},{'state':{'schemaVersion':27}},{'state':{'schemaVersion':28,'rules':{'bad':{}}}}):
   sender,state=self.run_synthetic(**kwargs);self.assertEqual(sender.call_count,1);self.assertEqual(state['pending'],[]);self.assertEqual(state['shadowEvents'],[]);self.assertIn('기준 상태 등록',sender.call_args.args[2])
 def test_long_gap_partial_has_only_one_operational_message(self):
  sender,_=self.run_synthetic(state={'schemaVersion':28,'rules':{},'initialized':True,'lastEvaluatedAt':'2026-01-01T00:00:00Z','health':'ok'},partial=True);self.assertEqual(sender.call_count,1)
 def test_default_shadow_existing_state_never_sends_investment(self):
  sender,state=self.run_synthetic(state={'schemaVersion':28,'rules':{},'initialized':True,'health':'ok'});sender.assert_not_called();self.assertEqual(state['pending'],[])
 def test_off_baseline_no_message(self):
  sender,_=self.run_synthetic(mode='off');sender.assert_not_called()
if __name__=='__main__':unittest.main()
