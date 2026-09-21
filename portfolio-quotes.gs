/** 기존 Code.gs 전체를 이 파일로 교체. 토큰은 프로젝트 설정 > 스크립트 속성. */
const QUOTE_SHEET = '시트1';
const FIRST_ROW = 3;

function quoteSheet_() {
  const id = PropertiesService.getScriptProperties().getProperty('SPREADSHEET_ID');
  if (!id) throw new Error('먼저 setupQuoteSheet를 실행하세요');
  const sheet = SpreadsheetApp.openById(id).getSheetByName(QUOTE_SHEET);
  if (!sheet) throw new Error('시트1 탭을 확인하세요');
  return sheet;
}

function setupQuoteSheet() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  if (!ss || !ss.getSheetByName(QUOTE_SHEET)) throw new Error('알람 시트의 확장 프로그램에서 실행하세요');
  const props = PropertiesService.getScriptProperties();
  props.setProperty('SPREADSHEET_ID', ss.getId());
  if (!props.getProperty('FEED_KEY')) props.setProperty('FEED_KEY', Utilities.getUuid()+Utilities.getUuid());
  const s = quoteSheet_();
  s.getRange(2, 1, 1, 12).setValues([['티커','종목명','알림조건(5=5%)','현재가','기준종가 대비%','마지막 알림','시세 기준 시각','조회 시각','조회 상태','통화','비교 기준 종가','시세 티커']]);
  s.getRange('G:H').setNumberFormat('yyyy-mm-dd hh:mm:ss');
  s.getRange('F:F').setNumberFormat('yyyy-mm-dd hh:mm:ss');
  s.getRange('D:D').setNumberFormat('0.00');
  s.getRange('E:E').setNumberFormat('0.00"%"');
  console.log('설정 완료. 프로젝트 설정의 스크립트 속성에서 FEED_KEY를 복사해 앱에 입력하세요.');
}

function yahooQuote_(ticker) {
  const r = UrlFetchApp.fetch('https://query1.finance.yahoo.com/v8/finance/chart/'+encodeURIComponent(ticker)+'?interval=1d&range=1d', {muteHttpExceptions:true});
  if (r.getResponseCode() !== 200) throw new Error('HTTP '+r.getResponseCode());
  const result = JSON.parse(r.getContentText()).chart;
  const m = result && result.result && result.result[0] && result.result[0].meta;
  if (!m || m.symbol.toUpperCase() !== ticker || !Number.isFinite(m.regularMarketPrice) || m.regularMarketPrice<=0 || !Number.isFinite(m.regularMarketTime)) throw new Error('가격·종목·시각 미확인');
  const at = m.regularMarketTime*1000;
  if (at<=0 || at>Date.now()+60000) throw new Error('시세 시각 오류');
  const prev = Number.isFinite(m.previousClose)&&m.previousClose>0 ? m.previousClose : m.chartPreviousClose;
  return {ticker,price:m.regularMarketPrice,currency:m.currency,quoteAsOf:new Date(at).toISOString(),collectedAt:new Date().toISOString(),previousClose:Number.isFinite(prev)&&prev>0?prev:null,changePct:Number.isFinite(prev)&&prev>0?(m.regularMarketPrice/prev-1)*100:null};
}

function alertTransition_(previous, quote, threshold) {
  const at = Date.parse(quote.quoteAsOf), age=Date.now()-at;
  if (!Number.isFinite(threshold)||threshold<=0||!Number.isFinite(quote.changePct)||age<0||age>90*60000) return null;
  const direction=quote.changePct>=threshold?'up':quote.changePct<=-threshold?'down':'none';
  if(previous && at<=previous.at) return null;
  const day=quote.quoteAsOf.slice(0,10);
  const sent=!previous&&direction!=='none'?[direction]:previous&&previous.day===day ? previous.sent||[] : [];
  const notify=!!previous && direction!=='none' && !sent.includes(direction);
  return {notify,direction,next:{at,day,sent:notify?sent.concat(direction):sent}};
}

function checkStockAlerts() {
  const lock=LockService.getScriptLock(); if(!lock.tryLock(1000))return;
  try {
    const s=quoteSheet_(), n=s.getLastRow()-FIRST_ROW+1;if(n<=0)return;
    if(n>100)throw new Error('종목은 최대 100행까지 지원합니다');
    const rows=s.getRange(FIRST_ROW,1,n,12).getValues(), props=PropertiesService.getScriptProperties(), cache={};
    for(let j=0;j<rows.length;j++) {
      const row=rows[j], ticker=String(row[0]).trim().toUpperCase();if(!ticker)continue;
      try {
        if(!/^[A-Z0-9^][A-Z0-9.^=\-]{0,24}$/.test(ticker))throw new Error('티커 형식 확인');
        if(!cache[ticker])cache[ticker]=yahooQuote_(ticker);
        const q=cache[ticker];
        const stale=Date.now()-Date.parse(q.quoteAsOf)>90*60000;
        s.getRange(FIRST_ROW+j,4,1,2).setValues([[q.price,q.changePct===null?'':q.changePct]]);
        s.getRange(FIRST_ROW+j,7,1,6).setValues([[new Date(q.quoteAsOf),new Date(q.collectedAt),stale?'조회 성공 · 이전 시세/휴장 가능':'조회 성공 · 지연 가능',q.currency,q.previousClose===null?'':q.previousClose,ticker]]);
        const threshold=Number(row[2]), key='alert:'+ticker+':'+threshold;
        let old=null;const stored=props.getProperty(key);if(stored)old=JSON.parse(stored);
        const event=alertTransition_(old,q,threshold);
        if(event) {
          if(event.notify) {
            const text='[가격 변동 확인] '+String(row[1]||ticker).slice(0,100)+' ('+ticker+')\n기준 종가 대비 '+q.changePct.toFixed(2)+'%\n가격 '+q.price+' '+q.currency+'\n시세 기준 '+Utilities.formatDate(new Date(q.quoteAsOf),'Asia/Seoul','MM-dd HH:mm:ss')+' KST\nYahoo 지연 가능 · 주문 지시 아님';
            if(sendTelegram(text)){s.getRange(FIRST_ROW+j,6).setValue(new Date());props.setProperty(key,JSON.stringify(event.next));}
          } else props.setProperty(key,JSON.stringify(event.next));
        }
      } catch(e) {
        // Never log response URLs: they may contain credentials.
        const msg=/^HTTP \d+$/.test(e.message)?e.message:'조회 또는 알림 오류';
        s.getRange(FIRST_ROW+j,9).setValue(msg+' · 기존값 유지');
      }
    }
    props.setProperty('LAST_COLLECTION',new Date().toISOString());
    console.log('시세 점검 완료. 시트의 조회 상태·시세 기준 시각을 확인하세요.');
  } finally {lock.releaseLock();}
}

function sendTelegram(message) {
  const p=PropertiesService.getScriptProperties(), token=p.getProperty('TELEGRAM_BOT_TOKEN'),chat=p.getProperty('TELEGRAM_CHAT_ID');
  if(!token||!chat)return false;
  const r=UrlFetchApp.fetch('https://api.telegram.org/bot'+token+'/sendMessage',{method:'post',contentType:'application/json',muteHttpExceptions:true,payload:JSON.stringify({chat_id:chat,text:message,link_preview_options:{is_disabled:true}})});
  if(r.getResponseCode()!==200||!JSON.parse(r.getContentText()).ok)throw new Error('알림 전송 실패');
  return true;
}
function testAlert(){if(!sendTelegram('Apps Script 시세 알림 연결 완료'))throw new Error('스크립트 속성의 텔레그램 설정을 확인하세요');}
function installQuoteTrigger(){
  quoteSheet_();
  const existing=ScriptApp.getProjectTriggers().filter(t=>t.getHandlerFunction()==='checkStockAlerts');
  const next=ScriptApp.newTrigger('checkStockAlerts').timeBased().everyMinutes(15).create();
  existing.forEach(t=>ScriptApp.deleteTrigger(t));
  console.log('15분 간격 자동 갱신 설정 완료');
}
function doGet(){return jsonOutput_({ok:false,error:'앱에서 연결키를 입력해 연결하세요'});}
function jsonOutput_(v){return ContentService.createTextOutput(JSON.stringify(v)).setMimeType(ContentService.MimeType.JSON);}
function doPost(e){
  try {
    const p=PropertiesService.getScriptProperties(),key=p.getProperty('FEED_KEY');
    if(!key||key.length<32||!e||!e.postData||e.postData.contents.length>4096)return jsonOutput_({ok:false,error:'연결키 확인'});
    const body=JSON.parse(e.postData.contents);
    if(body.key!==key)return jsonOutput_({ok:false,error:'연결키 확인'});
    const lock=LockService.getScriptLock();if(!lock.tryLock(1000))return jsonOutput_({ok:false,error:'수집 중입니다. 잠시 후 다시 확인하세요'});
    try {
      const s=quoteSheet_(),n=s.getLastRow()-FIRST_ROW+1;
      if(n>100)return jsonOutput_({ok:false,error:'시트 행 수 확인'});
      const rows=n>0?s.getRange(FIRST_ROW,1,n,12).getValues():[],quotes=[],failures=[],seen={};
      rows.forEach(r=>{
        const ticker=String(r[0]).trim().toUpperCase();if(!ticker||seen[ticker])return;seen[ticker]=true;
        if(String(r[8]).startsWith('조회 성공')&&r[11]===ticker&&r[6] instanceof Date&&r[7] instanceof Date&&typeof r[3]==='number'&&r[3]>0)quotes.push({ticker,price:r[3],currency:r[9],quoteAsOf:r[6].toISOString(),collectedAt:r[7].toISOString()});
        else failures.push({ticker});
      });
      return jsonOutput_({ok:true,kind:'portfolio-spot-v1',collectedAt:p.getProperty('LAST_COLLECTION'),quotes,failures});
    } finally {lock.releaseLock();}
  }catch(_){return jsonOutput_({ok:false,error:'시트 설정 또는 수집 상태를 확인하세요'});}
}
