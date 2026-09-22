/** 기존 Code.gs 전체를 이 파일로 교체. 토큰은 프로젝트 설정 > 스크립트 속성. */
const QUOTE_SHEET = '시트1';
const FIRST_ROW = 3;
const YAHOO_HOSTS = ['query1.finance.yahoo.com','query2.finance.yahoo.com'];
const LIVE_MAX_AGE_MS = 30 * 60 * 1000;
const CLOSED_MAX_AGE_MS = 96 * 60 * 60 * 1000;

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
  s.getRange(2, 1, 1, 12).setValues([['티커','종목명','알림조건(5=5%)','최근 시세','기준종가 대비%','마지막 알림','시세 기준 시각','조회 시각','조회 상태','통화','비교 기준 종가','시세 티커']]);
  s.getRange('G:H').setNumberFormat('yyyy-mm-dd hh:mm:ss');
  s.getRange('F:F').setNumberFormat('yyyy-mm-dd hh:mm:ss');
  s.getRange('D:D').setNumberFormat('0.00');
  s.getRange('E:E').setNumberFormat('0.00"%"');
  console.log('설정 완료. 프로젝트 설정의 스크립트 속성에서 FEED_KEY를 복사해 앱에 입력하세요.');
}

function validTicker_(ticker) {
  return /^[A-Z0-9^][A-Z0-9.^=\-]{0,24}$/.test(ticker);
}

function isCryptoTicker_(ticker) {
  return /^[A-Z0-9]{2,15}-USD$/.test(ticker);
}

function marketOpenGuess_(ticker, now) {
  if (isCryptoTicker_(ticker)) return true;
  if (/=X$/.test(ticker)) {
    const d = Utilities.formatDate(now, 'UTC', 'yyyy-MM-dd');
    const dow = new Date(d+'T12:00:00Z').getUTCDay();
    return dow !== 0 && dow !== 6;
  }
  const kr = /\.(KS|KQ)$/.test(ticker);
  const tz = kr ? 'Asia/Seoul' : 'America/New_York';
  const localDate = Utilities.formatDate(now, tz, 'yyyy-MM-dd');
  const dow = new Date(localDate+'T12:00:00Z').getUTCDay();
  if (dow === 0 || dow === 6) return false;
  const hm = Number(Utilities.formatDate(now, tz, 'HHmm'));
  return kr ? hm >= 900 && hm <= 1530 : hm >= 930 && hm <= 1600;
}

function yahooChart_(ticker, interval, range) {
  let lastCode = null;
  for (let i=0;i<YAHOO_HOSTS.length;i++) {
    const url = 'https://'+YAHOO_HOSTS[i]+'/v8/finance/chart/'+encodeURIComponent(ticker)
      +'?interval='+encodeURIComponent(interval)+'&range='+encodeURIComponent(range)
      +'&includePrePost=false&events=div%2Csplits';
    const r = UrlFetchApp.fetch(url, {muteHttpExceptions:true, followRedirects:true});
    const code = r.getResponseCode();
    if (code !== 200) {
      lastCode = code;
      if (code === 403 || code === 429 || code >= 500) continue;
      throw new Error('HTTP '+code);
    }
    let chart;
    try { chart = JSON.parse(r.getContentText()).chart; }
    catch (_) { throw new Error('Yahoo 응답 해석 오류'); }
    const result = chart && chart.result && chart.result[0];
    if (!result || !result.meta) throw new Error('Yahoo 응답 없음');
    if (String(result.meta.symbol||'').toUpperCase() !== ticker) throw new Error('Yahoo 종목 불일치');
    return result;
  }
  throw new Error('HTTP '+(lastCode||'Yahoo'));
}

function latestBar_(result) {
  const ts = result.timestamp || [];
  const q = result.indicators && result.indicators.quote && result.indicators.quote[0];
  const closes = q && q.close || [];
  for (let i=Math.min(ts.length, closes.length)-1;i>=0;i--) {
    const price = Number(closes[i]), at = Number(ts[i]);
    if (Number.isFinite(price) && price>0 && Number.isFinite(at) && at>0) return {price, at:at*1000};
  }
  return null;
}

function yahooQuote_(ticker) {
  const now = new Date();
  const wantIntraday = marketOpenGuess_(ticker, now);
  let result, mode;
  try {
    result = yahooChart_(ticker, wantIntraday ? '5m' : '1d', wantIntraday ? '1d' : '5d');
    mode = wantIntraday ? 'intraday-5m' : 'daily-last';
  } catch (firstErr) {
    if (!wantIntraday) throw firstErr;
    result = yahooChart_(ticker, '1d', '5d');
    mode = 'daily-fallback';
  }

  const m = result.meta || {};
  const bar = latestBar_(result);
  const metaPrice = Number(m.regularMarketPrice), metaTime = Number(m.regularMarketTime);
  const metaCandidate = Number.isFinite(metaPrice) && metaPrice>0 && Number.isFinite(metaTime) && metaTime>0
    ? {price:metaPrice, at:metaTime*1000} : null;

  let chosen = null;
  if (bar && metaCandidate) chosen = bar.at >= metaCandidate.at ? bar : metaCandidate;
  else chosen = bar || metaCandidate;
  if (!chosen) throw new Error('가격·시각 미확인');
  if (chosen.at<=0 || chosen.at>Date.now()+60000) throw new Error('시세 시각 오류');

  const prev = Number.isFinite(Number(m.previousClose))&&Number(m.previousClose)>0
    ? Number(m.previousClose)
    : (Number.isFinite(Number(m.chartPreviousClose))&&Number(m.chartPreviousClose)>0 ? Number(m.chartPreviousClose) : null);

  const regular = m.currentTradingPeriod && m.currentTradingPeriod.regular || {};
  return {
    ticker,
    price: chosen.price,
    currency: m.currency || '',
    quoteAsOf: new Date(chosen.at).toISOString(),
    collectedAt: new Date().toISOString(),
    previousClose: prev,
    changePct: prev ? (chosen.price/prev-1)*100 : null,
    mode,
    regularStart: Number.isFinite(Number(regular.start)) ? Number(regular.start)*1000 : null,
    regularEnd: Number.isFinite(Number(regular.end)) ? Number(regular.end)*1000 : null
  };
}

function freshness_(ticker, quote) {
  const now = Date.now(), at = Date.parse(quote.quoteAsOf), age = now-at;
  const periodOpen = Number.isFinite(quote.regularStart) && Number.isFinite(quote.regularEnd)
    && now >= quote.regularStart && now <= quote.regularEnd;
  const expectedLive = isCryptoTicker_(ticker) || periodOpen || marketOpenGuess_(ticker, new Date(now));
  const maxAge = expectedLive ? LIVE_MAX_AGE_MS : CLOSED_MAX_AGE_MS;
  return {
    expectedLive,
    stale: !Number.isFinite(at) || age < -60000 || age > maxAge,
    ageMinutes: Number.isFinite(age) ? Math.max(0, Math.round(age/60000)) : null
  };
}

function statusText_(quote, freshness) {
  if (freshness.stale) {
    return freshness.expectedLive
      ? '조회 성공 · 장중 시세 지연 · 앱 미전달'
      : '조회 성공 · 오래된 마지막 시세 · 앱 미전달';
  }
  if (freshness.expectedLive) {
    return quote.mode === 'intraday-5m'
      ? '조회 성공 · 장중 5분봉 · 지연 가능'
      : '조회 성공 · 장중 메타 시세 · 지연 가능';
  }
  return '조회 성공 · 장외/휴장 · 마지막 시세';
}

function alertTransition_(previous, quote, threshold, freshness) {
  const at = Date.parse(quote.quoteAsOf);
  if (freshness && freshness.stale) return null;
  if (!Number.isFinite(threshold)||threshold<=0||!Number.isFinite(quote.changePct)||!Number.isFinite(at)) return null;
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
        if(!validTicker_(ticker))throw new Error('티커 형식 확인');
        if(!cache[ticker])cache[ticker]=yahooQuote_(ticker);
        const q=cache[ticker], fr=freshness_(ticker,q), status=statusText_(q,fr);

        s.getRange(FIRST_ROW+j,4,1,2).setValues([[q.price,q.changePct===null?'':q.changePct]]);
        s.getRange(FIRST_ROW+j,7,1,6).setValues([[new Date(q.quoteAsOf),new Date(q.collectedAt),status,q.currency,q.previousClose===null?'':q.previousClose,ticker]]);

        const threshold=Number(row[2]), key='alert:'+ticker+':'+threshold;
        let old=null;const stored=props.getProperty(key);if(stored)old=JSON.parse(stored);
        const event=alertTransition_(old,q,threshold,fr);
        if(event) {
          if(event.notify) {
            const text='[가격 변동 확인] '+String(row[1]||ticker).slice(0,100)+' ('+ticker+')\n기준 종가 대비 '+q.changePct.toFixed(2)+'%\n가격 '+q.price+' '+q.currency+'\n시세 기준 '+Utilities.formatDate(new Date(q.quoteAsOf),'Asia/Seoul','MM-dd HH:mm:ss')+' KST\nYahoo 장중 시세 · 지연 가능 · 주문 지시 아님';
            if(sendTelegram(text)){s.getRange(FIRST_ROW+j,6).setValue(new Date());props.setProperty(key,JSON.stringify(event.next));}
          } else props.setProperty(key,JSON.stringify(event.next));
        }
      } catch(e) {
        const msg=/^HTTP \d+$/.test(e.message)?e.message:'조회 또는 알림 오류';
        s.getRange(FIRST_ROW+j,8).setValue(new Date());
        s.getRange(FIRST_ROW+j,9).setValue(msg+' · 기존값 유지');
      }
    }
    props.setProperty('LAST_COLLECTION',new Date().toISOString());
    console.log('시세 점검 완료. 장중 시세가 30분 넘게 오래되면 앱에는 전달하지 않습니다.');
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

function testFirstQuote() {
  const s=quoteSheet_(), ticker=String(s.getRange(FIRST_ROW,1).getValue()).trim().toUpperCase();
  if(!ticker||!validTicker_(ticker))throw new Error('첫 데이터 행의 티커를 확인하세요');
  const q=yahooQuote_(ticker), fr=freshness_(ticker,q);
  console.log(JSON.stringify({ok:true,mode:q.mode,price:q.price,quoteAsOf:q.quoteAsOf,status:statusText_(q,fr),ageMinutes:fr.ageMinutes}));
}

function installQuoteTrigger(){
  quoteSheet_();
  const existing=ScriptApp.getProjectTriggers().filter(t=>t.getHandlerFunction()==='checkStockAlerts');
  ScriptApp.newTrigger('checkStockAlerts').timeBased().everyMinutes(15).create();
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
        const status=String(r[8]||'');
        const valid=status.startsWith('조회 성공')&&!status.includes('앱 미전달')
          &&r[11]===ticker&&r[6] instanceof Date&&r[7] instanceof Date&&typeof r[3]==='number'&&r[3]>0;
        if(valid) quotes.push({
          ticker,price:r[3],currency:r[9],
          quoteAsOf:r[6].toISOString(),collectedAt:r[7].toISOString(),
          source:'Yahoo · Apps Script',status
        });
        else failures.push({ticker,reason:status.includes('앱 미전달')?'stale':'unavailable'});
      });
      return jsonOutput_({ok:true,kind:'portfolio-spot-v1',collectedAt:p.getProperty('LAST_COLLECTION'),quotes,failures});
    } finally {lock.releaseLock();}
  }catch(_){return jsonOutput_({ok:false,error:'시트 설정 또는 수집 상태를 확인하세요'});}
}
