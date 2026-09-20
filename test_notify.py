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
if __name__=='__main__':unittest.main()

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
