"""
코스피 급등락 대응 실전 모니터 (양방향, 신호+보유 자동추적)
================================================================
매일 급등락(±3% 이상)을 거듭하는 장세 대응:
  - 매일 오후 3시(KST), 어제 장마감 대비 오늘 등락률을 확인
    -2% 이상 하락 → 레버리지 매수 (반등 베팅) 신호
    +2% 이상 상승 → 인버스 매수 (하락 베팅) 신호
  - 보유 중 +10% 도달 → 익절 신호 / -5% 도달 → 손절 신호
  - 자동매매 옵션: CFG["auto_trade"]=True 설정 시 신호와 동시에
    시장가 자동 주문 (기본값 False = 알림만)

[자동매매 활성화 방법 — 신중히!]
  1) config.py에 계좌번호 추가: KIS_ACCOUNT_NO = "12345678-01"
  2) 이 파일 CFG에서 "auto_trade": True 로 변경
  3) "order_budget"으로 1회 매수 금액 설정 (기본 100만원)
  ⚠️ 반드시 모의투자(KIS_IS_VIRTUAL=True)로 먼저 1~2주 테스트 후 실전 전환
  ⚠️ 이 파라미터 조합(±2%/+15%/-5% 양방향)은 백테스트 미검증 상태이며,
     과거 검증에서 숏(인버스)은 일관되게 손실이었음을 인지하고 사용

[추적 기준]
  신호 판단: 코스피200 지수(2001) 당일 등락률
  보유 추적: KODEX 레버리지(122630) / KODEX 200선물인버스2X(252670)
  ※ 실전에서 삼성전자/SK하이닉스 단일종목레버리지(롱)를 쓰실 경우,
    익절/손절은 그 종목 가격 기준으로 직접(예약주문 OCO) 관리 권장.
    이 모니터는 122630/252670 기준 참고 신호를 제공.

[운영 시간 — 한국장]
  정규장: KST 09:00~15:30
  진입 신호 체크: 매일 오후 3시(KST) 정각 1회 — 어제 종가 대비 오늘 등락률
  보유 중 익절/손절 체크: 장중 실행 시마다

실행:
  python kr_daily_swing_monitor.py           # 1회 점검 (cron용)
  python kr_daily_swing_monitor.py --check   # 상태만 (발송 안 함)
  python kr_daily_swing_monitor.py --force   # 장외 시간에도 강제 점검
  python kr_daily_swing_monitor.py --reset   # 보유 기록 초기화
  python kr_daily_swing_monitor.py --sold    # 수동 매도 완료 후 봇에게 알림 (idle로 전환)

cron 등록 (EC2, UTC 기준 — KST-9):
  # 기존 등록 그대로 사용 — 0,30 0-6 UTC는 KST 09:00~15:30을 30분 간격으로 커버하며
  # UTC 06:00 = KST 15:00 정각이 정확히 포함되어 있어 별도 등록 불필요.
  0,30 0-6 * * 1-5 cd /home/ec2-user/trading-bot && venv/bin/python kr_daily_swing_monitor.py >> logs/kr_daily_swing.log 2>&1
"""

import sys, json, os, time
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

try:
    from config import KIS_APP_KEY as APP_KEY, KIS_APP_SECRET as APP_SECRET, \
                       KIS_IS_VIRTUAL as IS_VIRTUAL
except ImportError:
    print("⚠️  config.py 없음 — EC2 trading-bot 폴더에서 실행하세요")
    APP_KEY = APP_SECRET = ""; IS_VIRTUAL = True

# 자동매매용 계좌번호 (config.py에 KIS_ACCOUNT_NO = "12345678-01" 형태로 추가)
try:
    from config import KIS_ACCOUNT_NO
except ImportError:
    KIS_ACCOUNT_NO = ""
_acct = KIS_ACCOUNT_NO.split("-") if "-" in KIS_ACCOUNT_NO else [KIS_ACCOUNT_NO, "01"]
CANO, ACNT_PRDT = (_acct[0], _acct[1]) if len(_acct) >= 2 else (KIS_ACCOUNT_NO, "01")

# 텔레그램: 봇1(COIN) 우선 사용 — 코인알림이 디스코드로 이전되며 유휴화됨
TG_TOKEN = TG_CHAT = ""
try:
    import config as _cfg
    for tk, ch in [("TELEGRAM_TOKEN_COIN","TELEGRAM_CHAT_ID_COIN"),
                   ("TELEGRAM_TOKEN_KR","TELEGRAM_CHAT_ID_KR"),
                   ("TELEGRAM_TOKEN_STOCK","TELEGRAM_CHAT_ID_STOCK"),
                   ("TELEGRAM_TOKEN_US","TELEGRAM_CHAT_ID_US"),
                   ("TELEGRAM_TOKEN","TELEGRAM_CHAT_ID")]:
        if hasattr(_cfg, tk) and getattr(_cfg, tk):
            TG_TOKEN = getattr(_cfg, tk); TG_CHAT = getattr(_cfg, ch, "")
            break
except ImportError:
    pass

BASE_URL = "https://openapivts.koreainvestment.com:29443" if IS_VIRTUAL \
           else "https://openapi.koreainvestment.com:9443"

# ══════════════════════════════════════════════════════
#  설정 — 사용자 지정값
# ══════════════════════════════════════════════════════
CFG = {
    "index_code"     : "2001",     # 코스피200 (신호 판단)
    "long_code"       : "122630",   # KODEX 레버리지 (급락일 매수)
    "long_name"       : "KODEX레버리지",
    "short_code"      : "252670",   # KODEX 200선물인버스2X (급등일 매수)
    "short_name"      : "KODEX인버스2X",
    "entry_pct"       : 2.0,        # 당일 ±2% 이상 등락 시 신호
    "target_pct"      : 10.0,       # +10% 익절 (손익비 2:1)
    "stop_pct"        : 5.0,        # -5% 손절
    "state_file"      : "kr_daily_swing_state.json",

    # ── 자동매매 (⚠️ 실제 주문이 나갑니다 — 신중히 켜세요) ──
    "auto_trade"      : False,       # ⚠️ 2026-07-31 16:30 임시 중단: 지수데이터 이상치(+20%)
                                      # + 봇상태-실계좌 불일치 발견, 원인 파악 전까지 매수 중단
    "auto_sell"       : False,       # ⚠️ 2026-07-31: 익절/손절은 수동 전환
                                      # (등락 심한 장에서 -5% 손절만 반복 체결되는
                                      #  문제로, 매도 시점은 사람이 직접 판단하도록 변경)
    "order_budget"    : 200_000,     # 1회 매수 금액 20만원 (레버리지 1주 이상 매수 가능)
}

# ══════════════════════════════════════════════════════
#  KIS 인증 / 시세
# ══════════════════════════════════════════════════════
_token = None
_token_at = None

def get_token():
    global _token, _token_at
    if _token and _token_at and (datetime.now()-_token_at).total_seconds() < 23*3600:
        return _token
    try:
        r = requests.post(f"{BASE_URL}/oauth2/tokenP", json={
            "grant_type":"client_credentials","appkey":APP_KEY,"appsecret":APP_SECRET,
        }, timeout=10)
        t = r.json().get("access_token")
        if t:
            _token, _token_at = t, datetime.now()
        return t
    except Exception as e:
        print(f"  토큰 오류: {e}")
        return None

def _headers(tr_id):
    return {"Content-Type":"application/json","Authorization":f"Bearer {get_token()}",
            "appkey":APP_KEY,"appsecret":APP_SECRET,"tr_id":tr_id}

def fetch_index_recent(code: str, n: int = 5, retries: int = 2) -> list:
    """코스피200 최근 일봉 (당일 포함, 날짜/종가) — 장중엔 당일 행이 현재가.
    순간적 타임아웃 대비 재시도 포함 (신호를 놓치지 않기 위함)."""
    url = f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice"
    end = datetime.now(); start = end - timedelta(days=15)
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, headers=_headers("FHKUP03500100"), params={
                "FID_COND_MRKT_DIV_CODE":"U","FID_INPUT_ISCD":code,
                "FID_INPUT_DATE_1":start.strftime("%Y%m%d"),
                "FID_INPUT_DATE_2":end.strftime("%Y%m%d"),
                "FID_PERIOD_DIV_CODE":"D"}, timeout=15)
            rows = r.json().get("output2", [])
            rows = [x for x in rows if float(x.get("bstp_nmix_prpr",0)) > 0]
            rows.sort(key=lambda x: x.get("stck_bsop_date",""))
            return rows[-n:]
        except Exception as e:
            if attempt < retries:
                print(f"  지수 조회 재시도 {attempt+1}/{retries} ({e})")
                time.sleep(2)
            else:
                print(f"  지수 조회 오류(재시도 소진): {e}")
    return []

def fetch_price(code: str, retries: int = 2) -> Optional[float]:
    """종목(ETF) 현재가 — 타임아웃뿐 아니라 '빈 응답'도 재시도 대상으로 처리
    (장마감 후 KIS 실시간시세 API가 간헐적으로 빈 output을 주는 경우 대응)"""
    url = f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price"
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, headers=_headers("FHKST01010100"), params={
                "FID_COND_MRKT_DIV_CODE":"J","FID_INPUT_ISCD":code}, timeout=15)
            data = r.json()
            p = float(data.get("output",{}).get("stck_prpr",0))
            if p > 0:
                return p
            if attempt < retries:
                print(f"  ⚠️ {code} 시세 응답 비어있음 (원본: {data.get('output',{})}) "
                      f"— 재시도 {attempt+1}/{retries}")
                time.sleep(2)
            else:
                print(f"  ❌ {code} 시세 응답 계속 비어있음 (재시도 소진, 원본: {data.get('output',{})})")
        except Exception as e:
            if attempt < retries:
                print(f"  시세 조회 재시도 {attempt+1}/{retries} ({e})")
                time.sleep(2)
            else:
                print(f"  ❌ {code} 시세 조회 오류(재시도 소진): {e}")
    return None

# ══════════════════════════════════════════════════════
#  자동매매 주문 (⚠️ 실제 주문 발생 구간)
# ══════════════════════════════════════════════════════
def place_order(code: str, qty: int, is_buy: bool) -> tuple:
    """
    KIS 국내주식 현금 시장가 주문. 반환: (성공여부, 메시지)
    모의투자(VTTC...) / 실전(TTTC...) tr_id 자동 선택.
    ※ 주문 오류 시 KIS 설정에서 hashkey 필수 계정인지 확인 필요.
    """
    if not CANO:
        return False, "계좌번호 미설정 (config.py에 KIS_ACCOUNT_NO=\"12345678-01\" 추가 필요)"
    if is_buy:
        tr = "VTTC0802U" if IS_VIRTUAL else "TTTC0802U"
    else:
        tr = "VTTC0801U" if IS_VIRTUAL else "TTTC0801U"
    url = f"{BASE_URL}/uapi/domestic-stock/v1/trading/order-cash"
    body = {
        "CANO": CANO, "ACNT_PRDT_CD": ACNT_PRDT, "PDNO": code,
        "ORD_DVSN": "01",          # 01 = 시장가
        "ORD_QTY": str(qty), "ORD_UNPR": "0",
    }
    try:
        r = requests.post(url, headers=_headers(tr), json=body, timeout=15)
        d = r.json()
        ok = d.get("rt_cd") == "0"
        if ok:
            odno = d.get("output", {}).get("ODNO", "")
            return True, f"주문번호 {odno}"
        return False, d.get("msg1", "알 수 없는 오류")
    except Exception as e:
        return False, str(e)

def try_auto_buy(code: str, name: str, price: float, qty_out: dict) -> tuple:
    """자동매수 시도 → (성공여부, 메시지, 실체결수량). 상태 확정은 호출부에서 성공시에만."""
    qty = int(CFG["order_budget"] // price)
    if qty < 1:
        return False, f"\n\n🤖 자동매수 불가: 예산 {CFG['order_budget']:,}원 < 1주 가격", 0
    ok, detail = place_order(code, qty, is_buy=True)
    if ok:
        return (True,
                f"\n\n🤖 <b>자동매수 실행됨</b>: {qty}주 시장가 ({detail})\n"
                f"{'[모의투자]' if IS_VIRTUAL else '[⚠️실전계좌]'}",
                qty)
    return (False,
            f"\n\n🤖 ❌ 자동매수 실패: {detail}\n→ 신호는 유효하나 자동매수는 안 됐습니다. 수동으로 매수하거나 다음 신호를 기다리세요",
            0)

def try_auto_sell(code: str, name: str, state: dict) -> tuple:
    """자동매도 시도 (보유수량 전량) → (성공여부, 메시지)"""
    qty = int(state.get("qty", 0))
    if qty < 1:
        return True, "\n\n🤖 자동매도 생략: 기록된 보유수량 없음 (수동 매수분은 직접 매도하세요)"
    ok, detail = place_order(code, qty, is_buy=False)
    if ok:
        return True, (f"\n\n🤖 <b>자동매도 실행됨</b>: {qty}주 시장가 ({detail})\n"
                      f"{'[모의투자]' if IS_VIRTUAL else '[⚠️실전계좌]'}")
    return False, (f"\n\n🤖 ❌ 자동매도 실패: {detail}\n"
                   f"→ 즉시 수동으로 매도하세요! (봇은 다음 점검에서 재시도합니다)")

# ══════════════════════════════════════════════════════
#  점검
# ══════════════════════════════════════════════════════
def run_check(send: bool = True, force: bool = False):
    now = kst_now().strftime("%Y-%m-%d %H:%M KST")
    print(f"\n{'='*56}")
    print(f"  코스피 급등락 대응 모니터 (양방향) — {now}")
    print(f"{'='*56}")

    if not force and not is_market_hours():
        print("  한국 정규장 시간 아님 (09:00~15:30 KST). --force로 강제 점검 가능.")
        return
    if not get_token():
        print("  ❌ 토큰 실패")
        return

    state = load_state()
    positions = state.get("positions", [])
    msgs = []
    any_notify_suppressed = False

    # ── 보유 중인 모든 포지션을 각각 점검 (2026-07-31: 다중 포지션 지원) ──
    # 실계좌에서 레버리지/인버스를 동시에 들고 있는 상태가 확인되어,
    # 기존 "한 번에 하나만" 구조를 리스트 기반으로 변경.
    if positions:
        remaining = []
        for pos in positions:
            side = pos["side"]
            code = CFG["long_code"] if side == "long" else CFG["short_code"]
            name = CFG["long_name"] if side == "long" else CFG["short_name"]
            cur = fetch_price(code)
            if cur is None:
                print(f"  {name} 시세 조회 실패 — 이번 점검 보류")
                remaining.append(pos)
                continue
            entry = pos["entry"]
            chg = (cur-entry)/entry*100
            target = entry*(1+CFG["target_pct"]/100)
            stop = entry*(1-CFG["stop_pct"]/100)

            exit_side = None
            if cur >= target:
                exit_side = "target"
                base_msg = (f"🎯 <b>{name} 익절 신호!</b> (+{CFG['target_pct']:.0f}% 도달)\n"
                            f"진입 {entry:,.0f}원 ({pos.get('entry_date','')}, {pos.get('qty','?')}주)\n"
                            f"현재 {cur:,.0f}원 ({chg:+.1f}%)\n"
                            f"📌 매도하세요 — 큰 한 방이 왔습니다")
            elif cur <= stop:
                exit_side = "stop"
                base_msg = (f"🔴 <b>{name} 손절 신호!</b> (-{CFG['stop_pct']:.0f}% 도달)\n"
                            f"진입 {entry:,.0f}원 ({pos.get('entry_date','')}, {pos.get('qty','?')}주)\n"
                            f"현재 {cur:,.0f}원 ({chg:+.1f}%)\n"
                            f"📌 손절하세요 — 작게 지고 다음 기회를 기다립니다")

            if exit_side:
                if CFG["auto_trade"] and CFG["auto_sell"] and send:
                    sold, sell_msg = try_auto_sell(code, name, pos)
                    msgs.append(base_msg + sell_msg)
                    if not sold:
                        remaining.append(pos)   # 매도 실패 시 계속 추적
                else:
                    if not pos.get("notified_exit"):
                        pos["notified_exit"] = True
                        msgs.append(base_msg +
                            "\n\n🖐️ <b>수동 매도 모드</b> — 직접 거래소에서 매도 후\n"
                            "다음 명령으로 봇에게 알려주세요:\n"
                            f"<code>venv/bin/python kr_daily_swing_monitor.py --sold {side}</code>")
                    else:
                        any_notify_suppressed = True
                        print(f"  🎯🔴 {name}({side}): {exit_side} 조건 유지 중 (이미 알림 발송됨, "
                              f"매도 후 --sold {side} 로 알려주세요) 현재 {cur:,.0f}원 ({chg:+.1f}%)")
                    remaining.append(pos)   # 수동모드는 --sold 전까지 계속 추적
            else:
                print(f"  ⏳ 보유 중({side}/{name}, {pos.get('qty','?')}주): "
                      f"진입 {entry:,.0f} → 현재 {cur:,.0f} ({chg:+.1f}%)")
                print(f"     익절 {target:,.0f} / 손절 {stop:,.0f}")
                remaining.append(pos)
        state["positions"] = remaining

    # ── 신규 진입 신호 (3시 창 또는 재시도 대기 중이면) ──
    # 기존 포지션이 있어도 신규 신호는 계속 체크한다(다만 여기서는 관찰만 —
    # auto_trade가 켜져 있을 때만 실제로 새로 매수됨. 무분별한 중첩 방지를
    # 원하면 len(state["positions"])>0일 때 진입 자체를 막는 것도 가능).
    if force or is_entry_window(state):
        rows = fetch_index_recent(CFG["index_code"], 5)
        if len(rows) < 2:
            print("  지수 데이터 부족 — 잠시 후(다음 실행 때) 자동 재시도됩니다")
            state["pending_entry_check"] = True
        else:
            prev_close = float(rows[-2]["bstp_nmix_prpr"])   # 어제 장마감 종가
            today = float(rows[-1]["bstp_nmix_prpr"])         # 오늘 체크 시점 지수
            day_chg = (today/prev_close - 1) * 100
            print(f"  코스피200: 어제 종가 {prev_close:.2f} → 오늘 {today:.2f} ({day_chg:+.2f}%)")

            # 이상치 방어: ±25% 초과는 데이터 오류로 간주해 차단.
            # (2026-07-31: ±15%로 뒀다가 실제 극단적 폭락/반등 장세를
            #  오작동으로 오인해 정상 신호를 막아버린 사고 발생 — 완화함)
            is_anomaly = abs(day_chg) > 25.0
            if is_anomaly:
                print(f"  🚨 지수 변동률({day_chg:+.2f}%)이 비정상적으로 큽니다 — "
                      f"API 데이터 이상치로 판단, 신호 생성 안 함. "
                      f"(전일종가 {prev_close}, 금일 {today} 값 직접 확인 필요)")
                side = None
            else:
                side = None
                if day_chg <= -CFG["entry_pct"]:
                    side = "long"    # 급락 → 레버리지 매수 (반등 베팅)
                elif day_chg >= CFG["entry_pct"]:
                    side = "short"   # 급등 → 인버스 매수 (하락 베팅)

            if side:
                code = CFG["long_code"] if side == "long" else CFG["short_code"]
                name = CFG["long_name"] if side == "long" else CFG["short_name"]
                price = fetch_price(code)
                if price:
                    target = price*(1+CFG["target_pct"]/100)
                    stop = price*(1-CFG["stop_pct"]/100)
                    arrow = "📉→🟢매수(반등베팅)" if side=="long" else "📈→🔴매수(하락베팅)"
                    entry_msg = (f"{'🟢' if side=='long' else '🔴'} <b>{name} 매수 신호!</b> {arrow}\n"
                           f"코스피200 당일 {day_chg:+.2f}% (기준 ±{CFG['entry_pct']:.0f}% 충족)\n\n"
                           f"{name} 현재 {price:,.0f}원\n"
                           f"장마감 전 종가 부근 매수\n\n"
                           f"🎯 익절 +{CFG['target_pct']:.0f}% → {target:,.0f}원\n"
                           f"📍 손절 -{CFG['stop_pct']:.0f}% → {stop:,.0f}원\n\n"
                           f"💡 예약주문(OCO) 걸어두면 이후 자동 관리\n"
                           f"※ 삼성전자/하이닉스 단일종목레버리지 사용 시(롱)\n"
                           f"  해당 종목 가격 기준 +{CFG['target_pct']:.0f}%/-{CFG['stop_pct']:.0f}%로 설정")

                    if CFG["auto_trade"] and send:
                        # ⚠️ 자동매매 켜진 경우: 실제 체결 성공을 확인한 뒤에만 포지션 추가.
                        bought, buy_msg, qty = try_auto_buy(code, name, price, {})
                        entry_msg += buy_msg
                        if bought:
                            state.setdefault("positions", []).append({
                                "side": side, "entry": price, "qty": qty,
                                "entry_date": kst_now().strftime("%Y-%m-%d"),
                                "index_chg_at_entry": round(day_chg,2),
                            })
                        state["pending_entry_check"] = False
                    else:
                        # 자동매매 미사용: 알림만 (참고용, 포지션엔 추가 안 함)
                        state["pending_entry_check"] = False
                    msgs.append(entry_msg)
                else:
                    print(f"  🚨 진입 조건({day_chg:+.2f}%)은 충족했으나 {name}({code}) "
                          f"시세 조회 실패 — 다음 실행 때 자동 재시도됩니다")
                    state["pending_entry_check"] = True
            else:
                if is_anomaly:
                    state["pending_entry_check"] = True   # 이상치는 다음 실행에서 재확인
                else:
                    print(f"  ⚪ 진입 조건 미충족 (당일 {day_chg:+.2f}%, 기준 ±{CFG['entry_pct']:.0f}%)")
                    state["pending_entry_check"] = False
    elif not positions:
        print("  진입 신호 창(14:55~15:05 KST, 또는 재시도 대기중이면 15:30까지) 아님 — 보유도 없어 점검만 종료")

    if send:
        save_state(state)   # 실제(cron) 실행일 때만 보유 상태 반영
    else:
        print("  (--check 모드: 상태 저장 생략 — 실제 보유 기록에 영향 없음)")

    if msgs:
        for msg in msgs:
            if send:
                send_telegram(msg)
            else:
                print("\n  신호 (--check, 발송 생략):")
                print("  " + msg.replace("<b>","").replace("</b>",""))
        if send:
            print(f"\n  ✅ 신호 {len(msgs)}건 발송")
    elif not any_notify_suppressed:
        print("\n  신호 없음")

# ══════════════════════════════════════════════════════
#  텔레그램 / 상태
# ══════════════════════════════════════════════════════
def send_telegram(text: str) -> bool:
    if not TG_TOKEN or not TG_CHAT:
        print("  (텔레그램 미설정 — 콘솔 출력)")
        print(text)
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          json={"chat_id":TG_CHAT,"text":text,"parse_mode":"HTML"},
                          timeout=10)
        return r.json().get("ok", False)
    except Exception as e:
        print(f"  텔레그램 오류: {e}")
        return False

def load_state() -> dict:
    if os.path.exists(CFG["state_file"]):
        try:
            with open(CFG["state_file"], encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_state(s: dict):
    with open(CFG["state_file"], "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)

# ══════════════════════════════════════════════════════
#  한국장 시간 (KST = UTC+9)
# ══════════════════════════════════════════════════════
def kst_now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=9)

def is_market_hours() -> bool:
    t = kst_now()
    if t.weekday() >= 5:
        return False
    m = t.hour*60 + t.minute
    return 9*60 <= m <= 15*60+30

def is_entry_window(state: Optional[dict] = None) -> bool:
    """진입 신호 체크 시점 판단:
    1) 기본: 오후 3시(KST) ±5분 — '3시 기준' 취지 유지
    2) 예외: 3시 체크가 실패(API 오류 등)해서 pending_entry_check 플래그가
       남아있으면, 장마감(15:30)까지는 이후 실행 때마다 재시도 허용
       (그래야 3시 API 장애로 그날 하루 전체를 놓치지 않음)"""
    t = kst_now()
    if t.weekday() >= 5:
        return False
    m = t.hour*60 + t.minute
    if 14*60+55 <= m <= 15*60+5:
        return True
    if state and state.get("pending_entry_check") and m <= 15*60+30:
        return True
    return False

if __name__ == "__main__":
    args = sys.argv[1:]
    if "--reset" in args:
        if os.path.exists(CFG["state_file"]):
            os.remove(CFG["state_file"])
        print("  보유 기록 초기화 완료 (모든 포지션 기록 삭제)")
        sys.exit(0)

    if "--sold" in args:
        idx = args.index("--sold")
        side_arg = args[idx+1] if idx+1 < len(args) and args[idx+1] in ("long","short") else None
        st = load_state()
        positions = st.get("positions", [])
        if not positions:
            print("  ⚪ 현재 보유 중인 포지션이 없습니다")
            sys.exit(0)
        if side_arg is None:
            if len(positions) == 1:
                side_arg = positions[0]["side"]
            else:
                sides = [p["side"] for p in positions]
                print(f"  ⚠️ 보유 포지션이 {len(positions)}개입니다({sides}). "
                      f"어느 것을 매도완료 처리할지 지정하세요:")
                print(f"     venv/bin/python kr_daily_swing_monitor.py --sold long")
                print(f"     venv/bin/python kr_daily_swing_monitor.py --sold short")
                sys.exit(1)
        matched = [p for p in positions if p["side"] == side_arg]
        if not matched:
            print(f"  ⚪ {side_arg} 포지션이 없습니다")
            sys.exit(0)
        p = matched[0]
        print(f"  ✅ 매도 완료 처리: {side_arg} 진입 {p['entry']:,.0f}원 "
              f"({p.get('qty','?')}주) 포지션 종료")
        st["positions"] = [x for x in positions if x["side"] != side_arg]
        save_state(st)
        sys.exit(0)

    if "--add-position" in args:
        # 실제 계좌와 봇 기록이 어긋났을 때 수동 동기화용
        # 사용법: --add-position long 84977 1   (side, 평단가, 수량)
        idx = args.index("--add-position")
        try:
            side_a, entry_a, qty_a = args[idx+1], float(args[idx+2]), int(args[idx+3])
            assert side_a in ("long", "short")
        except (IndexError, ValueError, AssertionError):
            print("  사용법: --add-position <long|short> <평단가> <수량>")
            print("  예:    --add-position long 84977 1")
            sys.exit(1)
        st = load_state()
        st.setdefault("positions", []).append({
            "side": side_a, "entry": entry_a, "qty": qty_a,
            "entry_date": kst_now().strftime("%Y-%m-%d"),
            "note": "manual_sync",
        })
        save_state(st)
        print(f"  ✅ 포지션 추가(동기화): {side_a} 평단 {entry_a:,.0f}원 {qty_a}주")
        sys.exit(0)

    if "--status" in args:
        st = load_state()
        positions = st.get("positions", [])
        if not positions:
            print("  보유 포지션 없음 (idle)")
        else:
            print(f"  보유 포지션 {len(positions)}건:")
            for p in positions:
                print(f"    {p['side']}: 평단 {p['entry']:,.0f}원, {p.get('qty','?')}주, "
                      f"진입일 {p.get('entry_date','?')}")
        sys.exit(0)

    run_check(send=("--check" not in args), force=("--force" in args))
