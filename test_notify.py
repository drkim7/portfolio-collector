import unittest
from datetime import datetime, timezone
from notify import evaluate
NOW=datetime(2026,9,21,10,tzinfo=timezone.utc)
H=[dict(id='x',name='Fixture',acct='Test',mkt='KR',qty=1,stop=90,target=110)]
def patch(day,price):return {'updates':[dict(id='x',name='Fixture',acct='Test',mkt='KR',quoteAsOf=f'2026-09-{day:02d}T15:30:00+09:00',bars=[dict(date='2026-09-17',close=100,high=101),dict(date=f'2026-09-{day:02d}',close=price,high=price+1)])]}
class Alerts(unittest.TestCase):
 def test_baseline_no_flood(self):
  s,e=evaluate(H,patch(18,80),{},NOW);self.assertEqual(e,[])
 def test_cross_repeat_rearm(self):
  s,_=evaluate(H,patch(17,100),{},NOW)
  s,e=evaluate(H,patch(18,80),s,NOW);self.assertEqual(len(e),1)
  s,e=evaluate(H,patch(18,80),s,NOW);self.assertEqual(e,[])
  s,e=evaluate(H,patch(19,80),s,NOW);self.assertEqual(e,[])
  s,_=evaluate(H,patch(20,100),s,NOW)
  _,e=evaluate(H,patch(21,80),s,NOW);self.assertEqual(len(e),1)
 def test_stale_and_account_mismatch(self):
  s,_=evaluate(H,patch(17,100),{},NOW)
  _,e=evaluate(H,patch(1,80),s,NOW);self.assertEqual(e,[])
  p=patch(21,80);p['updates'][0]['acct']='Other'
  _,e=evaluate(H,p,s,NOW);self.assertEqual(e,[])
 def test_changed_rule_baseline(self):
  s,_=evaluate(H,patch(17,100),{},NOW)
  _,e=evaluate([dict(H[0],stop=105)],patch(21,100),s,NOW);self.assertEqual(e,[])

class Delivery(unittest.TestCase):
 def test_no_keys_no_send(self):
  from unittest.mock import patch, Mock
  from notify import main
  sender=Mock()
  with patch.dict('os.environ',{},clear=True):main(sender)
  sender.assert_not_called()
 def test_corrupt_state_stops(self):
  import tempfile,os
  from pathlib import Path
  from unittest.mock import patch, Mock
  from notify import main
  sender=Mock();cwd=os.getcwd()
  with tempfile.TemporaryDirectory() as tmp:
   try:
    os.chdir(tmp);Path('state').mkdir();Path('state/alerts.enc').write_text('broken')
    with patch.dict('os.environ',{'TELEGRAM_BOT_TOKEN':'fixture','TELEGRAM_CHAT_ID':'fixture','REPORT_PASSWORD':'fixture-password'},clear=True):
     with self.assertRaises(Exception):main(sender)
    sender.assert_not_called()
   finally:os.chdir(cwd)

class Health(unittest.TestCase):
 def test_status_counts_dates_and_empty(self):
  from notify import data_status
  p=patch(18,100);p['updates'][0]['quoteDate']='2026-09-18'
  status,detail=data_status(p,NOW)
  self.assertEqual(status,'ok');self.assertIn('2026-09-18',detail)
  p['failures']=[{'id':'other'}]
  self.assertEqual(data_status(p,NOW)[0],'partial')
  self.assertEqual(data_status({},NOW)[0],'partial')
  self.assertEqual(data_status(patch(1,100),NOW)[0],'partial')
 def test_transitions(self):
  from notify import health_message
  self.assertIsNone(health_message('partial','partial',''))
  self.assertIsNone(health_message(None,'ok',''))
  self.assertIn('복구',health_message('collection_failed','ok',''))
  self.assertIn('갱신 실패',health_message('ok','publish_failed',''))
 def test_failure_once_recovery_and_daily_summary(self):
  import tempfile,os,json
  from pathlib import Path
  from unittest.mock import patch as mockpatch, Mock
  from notify import main,save
  from pipeline import decrypt,encrypt
  sender=Mock();cwd=os.getcwd();password='fixture-password'
  env={'TELEGRAM_BOT_TOKEN':'fixture','TELEGRAM_CHAT_ID':'fixture','REPORT_PASSWORD':password,'COLLECTION_OUTCOME':'failure','DEPLOY_OUTCOME':'skipped'}
  with tempfile.TemporaryDirectory() as tmp:
   try:
    os.chdir(tmp)
    original={'initialized':True,'rules':{'keep':{'date':'2026-09-18','active':False}},'sent':[]}
    save(Path('state/alerts.enc'),original,password)
    with mockpatch.dict('os.environ',env,clear=True), mockpatch('notify.time.sleep'), mockpatch('notify.load_holdings',return_value=(H,{},None)), mockpatch('notify.datetime') as clock:
     clock.now.return_value=NOW;clock.fromisoformat.side_effect=datetime.fromisoformat
     main(sender);main(sender)
     self.assertEqual(sender.call_count,1)
     state=json.loads(decrypt(json.loads(Path('state/alerts.enc').read_text()),password))
     self.assertEqual(state['rules'],original['rules'])
     self.assertEqual(state['health'],'collection_failed')
     os.environ['COLLECTION_OUTCOME']='success';os.environ['DEPLOY_OUTCOME']='success'
     Path('site').mkdir();p=patch(18,100);p['updates'][0]['quoteDate']='2026-09-18'
     Path('site/quotes-patch.enc').write_text(json.dumps(encrypt(json.dumps(p),password)))
     main(sender);main(sender)
     self.assertEqual(sender.call_count,3)
     self.assertIn('복구',sender.call_args_list[1].args[2])
     self.assertIn('일일 시세 점검',sender.call_args_list[2].args[2])
   finally:os.chdir(cwd)




class DigestQueue(unittest.TestCase):
 def test_morning_afternoon_retry_and_next_day(self):
  import tempfile,os,json
  from pathlib import Path
  from unittest.mock import patch as mockpatch, Mock
  from notify import main,save
  from pipeline import encrypt,decrypt
  password='fixture-password';cwd=os.getcwd();sender=Mock()
  env={'TELEGRAM_BOT_TOKEN':'fixture','TELEGRAM_CHAT_ID':'fixture','REPORT_PASSWORD':password}
  def candles(day,price):
   p=patch(day,price);u=p['updates'][0];u['quoteDate']=f'2026-09-{day:02d}'
   u['bars']=[dict(date=f'2026-08-{j+1:02d}',close=100,high=101) for j in range(20)]+[u['bars'][-1]]
   return p
  with tempfile.TemporaryDirectory() as tmp:
   try:
    os.chdir(tmp);Path('site').mkdir()
    baseline,_=evaluate(H,candles(20,99),{},datetime(2026,9,22,tzinfo=timezone.utc));baseline['initialized']=True
    save(Path('state/alerts.enc'),baseline,password)
    def load():return json.loads(decrypt(json.loads(Path('state/alerts.enc').read_text()),password))
    with mockpatch.dict('os.environ',env,clear=True),mockpatch('notify.time.sleep'),mockpatch('notify.load_holdings',return_value=(H,{},None)),mockpatch('notify.datetime') as clock:
     clock.fromisoformat.side_effect=datetime.fromisoformat
     def run(at,day,price):
      clock.now.return_value=datetime.fromisoformat(at)
      Path('site/quotes-patch.enc').write_text(json.dumps(encrypt(json.dumps(candles(day,price)),password)))
      main(sender)
     run('2026-09-22T01:40:00+00:00',21,101)
     self.assertEqual(sender.call_count,0);self.assertTrue(load()['pending'])
     sender.side_effect=RuntimeError('offline')
     with self.assertRaises(RuntimeError):run('2026-09-22T07:40:00+00:00',22,99)
     self.assertEqual(len(load()['pending']),2)
     sender.side_effect=None;sender.reset_mock()
     run('2026-09-22T07:45:00+00:00',22,99)
     self.assertEqual(sender.call_count,1)
     text=sender.call_args.args[2];self.assertIn('위로 전환',text);self.assertIn('아래로 전환',text)
     self.assertEqual(load()['pending'],[])
     run('2026-09-22T08:00:00+00:00',22,99);self.assertEqual(sender.call_count,1)
     run('2026-09-23T01:40:00+00:00',22,101)
     # Same completed candle does not generate a second transition.
     self.assertEqual(sender.call_count,1)
     run('2026-09-23T07:40:00+00:00',23,101)
     self.assertEqual(sender.call_count,2);self.assertEqual(load()['pending'],[])
   finally:os.chdir(cwd)

if __name__=='__main__':unittest.main()
