"""
코인 그리드매매 실전 자동매매 — "떨사오팔" 전략 (TQQQUSDT)
============================================================
기준가 대비 -5% 하락마다 10USDT 매수, +5% 상승마다 10USDT 매도를
반복하는 그리드매매 봇. coin_range_monitor.py의 구조(디스코드 알림,
상태파일 추적)를 그대로 따르되, 실제 주문 실행 기능을 추가했다.

[전략 성격 — 반드시 인지할 것]
  그리드매매는 박스권/횡보장에서 강하고, 한쪽 추세가 지속되면 약하다.
  TQQQ가 계속 하락만 하면 매수만 반복 누적되고 매도 기회가 안 와서
  (나스닥이 지속 하락하는 국면에서 특히 취약)
  자금이 무한정 물릴 수 있다. 그래서 총 투입 한도(max_total_usdt)를
  반드시 설정해 무제한 매수를 막는다.

[동작 방식]
  기준가(reference_price) 대비:
    -5% 하락 → 10USDT 매수, 기준가를 그 가격으로 갱신(더 아래로 래칫)
    +5% 상승 → 보유 중이면 10USDT어치 매도, 기준가를 그 가격으로 갱신(더 위로 래칫)
  총 매수 누적액이 max_total_usdt 도달 시 → 매수 중단(매도 감시는 계속)

[안전장치]
  - auto_trade 기본값 False (알림만, 실제 주문 없음) — 반드시 --check로
    충분히 확인 후 True로 전환할 것
  - max_total_usdt 로 무한 물타기 방지
  - 실제 주문은 Bitget API(ccxt) 사용, .env의 BITGET_API_KEY 등 필요

[.env 설정 — 이미 있다면 재사용]
  BITGET_API_KEY=...
  BITGET_API_SECRET=...
  BITGET_PASSPHRASE=...
  DISCORD_WEBHOOK_URL_GRID=... (그리드 전용 디스코드 채널 권장)

실행:
  python coin_grid_monitor.py --find-symbol   # 정확한 거래 심볼 형식 확인 (최초 1회 필수)
  python coin_grid_monitor.py --check          # 1회 점검 (주문 없음, 상태 저장도 안 함)
  python coin_grid_monitor.py                  # 1회 점검 + 신호 발생 시 실행 (cron용)
  python coin_grid_monitor.py --status          # 현재 보유/기준가 현황
  python coin_grid_monitor.py --reset           # 상태 초기화 (매매 기록 삭제, 실제 보유는 그대로)
"""

import sys, json, os, re, time
from datetime import datetime, timezone

try:
    import ccxt
    HAS_CCXT = True
except ImportError:
    HAS_CCXT = False

try:
    import config as _cfg
    def _cget(key):
        return _cfg._get(key) if hasattr(_cfg, "_get") else getattr(_cfg, key, "")
    DISCORD_WEBHOOK_URL   = _cget("DISCORD_WEBHOOK_URL_GRID") or _cget("DISCORD_WEBHOOK_URL")
    BITGET_API_KEY        = _cget("BITGET_API_KEY")
    BITGET_API_SECRET     = _cget("BITGET_API_SECRET") or _cget("BITGET_SECRET_KEY")
    BITGET_PASSPHRASE     = _cget("BITGET_PASSPHRASE") or _cget("BITGET_PASSWORD")
except ImportError:
    print("⚠️  config.py 없음 — .env 설정 확인 필요")
    DISCORD_WEBHOOK_URL = BITGET_API_KEY = BITGET_API_SECRET = BITGET_PASSPHRASE = ""

import requests

# ══════════════════════════════════════════════════════
#  설정 — "떨사오팔" 그리드 파라미터
# ══════════════════════════════════════════════════════
CFG = {
    "symbol"          : "TQQQ/USDT:USDT",  # ⚠️ --find-symbol 로 정확한 형식 먼저 확인할 것
    "step_pct"        : 5.0,               # 그리드 간격 5%
    "order_usdt"      : 10.0,              # 1회 매수/매도 금액
    "max_total_usdt"  : 200.0,             # ⚠️ 총 투입 한도 — 무제한 물타기 방지 (안전장치)
    "leverage"        : 1,                 # 1배(레버리지 없음) — TQQQ 자체가 이미 +3배 상품이라
                                            # 거래소 레버리지는 추가하지 않음(중첩 위험 방지)
    "auto_trade"      : True,             # ⚠️ True로 바꿔야만 실제 주문 실행
    "state_file"      : "coin_grid_state.json",
}

# ══════════════════════════════════════════════════════
#  디스코드
# ══════════════════════════════════════════════════════
def _html_to_discord_markdown(text: str) -> str:
    text = re.sub(r"<b>(.*?)</b>", r"**\1**", text, flags=re.DOTALL)
    text = re.sub(r"<i>(.*?)</i>", r"*\1*", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", "", text)
    return text

def send_discord(text: str) -> bool:
    if not DISCORD_WEBHOOK_URL:
        print("  (디스코드 웹훅 미설정 — 콘솔 출력만)")
        print(text)
        return False
    content = _html_to_discord_markdown(text)
    if len(content) > 1900:
        content = content[:1900] + "\n…(생략)"
    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json={"content": content}, timeout=10)
        return r.status_code in (200, 204)
    except Exception as e:
        print(f"  디스코드 오류: {e}")
        return False

# ══════════════════════════════════════════════════════
#  상태 저장/로드
# ══════════════════════════════════════════════════════
def load_state() -> dict:
    path = CFG["state_file"]
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_state(s: dict):
    with open(CFG["state_file"], "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)

# ══════════════════════════════════════════════════════
#  거래소 (Bitget, ccxt)
# ══════════════════════════════════════════════════════
def get_exchange():
    if not HAS_CCXT:
        print("  ❌ ccxt 필요: pip install ccxt")
        return None
    if not (BITGET_API_KEY and BITGET_API_SECRET and BITGET_PASSPHRASE):
        print("  ⚠️  Bitget API 키 미설정 — 시세 조회는 되지만 주문은 불가")
    return ccxt.bitget({
        "apiKey": BITGET_API_KEY, "secret": BITGET_API_SECRET,
        "password": BITGET_PASSPHRASE, "enableRateLimit": True,
    })

_leverage_set = False   # 실행당 1회만 설정 시도 (매번 API 호출 방지)

def ensure_leverage(ex, symbol: str):
    global _leverage_set
    if _leverage_set or not CFG["auto_trade"]:
        return
    try:
        ex.set_leverage(CFG["leverage"], symbol)
        print(f"  ✅ 레버리지 {CFG['leverage']}배 설정 완료 ({symbol})")
    except Exception as e:
        print(f"  ⚠️ 레버리지 설정 실패(이미 설정돼 있거나 마켓타입 확인 필요): {e}")
    _leverage_set = True

def find_symbol(keyword: str = "TQQQ"):
    """정확한 거래 심볼 형식을 찾는 도구. 최초 1회 반드시 실행 권장."""
    ex = get_exchange()
    if not ex:
        return
    try:
        markets = ex.load_markets()
    except Exception as e:
        print(f"  ❌ 마켓 목록 조회 실패: {e}")
        return
    matches = [m for m in markets if keyword.upper() in m.upper()]
    if not matches:
        print(f"  ⚠️  '{keyword}' 포함 심볼을 찾지 못했습니다. "
              f"Bitget에 해당 자산이 없거나 다른 이름일 수 있습니다.")
        return
    print(f"  '{keyword}' 관련 심볼 {len(matches)}개:")
    for m in matches:
        info = markets[m]
        kind = "선물(swap)" if info.get("swap") else ("현물(spot)" if info.get("spot") else "?")
        print(f"    {m}   [{kind}]")
    print(f"\n  이 중 정확한 심볼을 CFG['symbol']에 넣으세요 (기본값: TQQQ/USDT:USDT 는 추정치입니다).")

def fetch_price(symbol: str) -> float:
    ex = get_exchange()
    if not ex:
        return 0.0
    try:
        ticker = ex.fetch_ticker(symbol)
        return float(ticker.get("last") or 0)
    except Exception as e:
        print(f"  시세 조회 오류: {e}")
        return 0.0

def place_order(symbol: str, side: str, margin_usdt: float, price: float, reduce_only: bool = False) -> tuple:
    """
    실제 시장가 주문. margin_usdt는 투입 증거금이고, 실제 체결 수량은
    레버리지가 곱해진 명목가치(notional) 기준으로 계산된다.

    ⚠️ 2026-08-21 최종 수정: ccxt bitget.py 소스코드 직접 확인 결과,
    holdSide를 직접 넣는 건 무시되고 ccxt가 'hedged' bool 값을 보고
    자동으로 holdSide/posSide를 계산해서 넣는 구조였다.
    (ccxt==4.5.64 기준, venv/lib/python3.9/site-packages/ccxt/bitget.py
     5113~5235줄 참고 — hedged=True면 holdSide/posSide를 hedge_mode
     방식으로 자동 세팅해줌)
    계좌가 hedge_mode이므로 hedged=True 고정.

    반환: (성공여부, 메시지, 체결수량)
    """
    lev = CFG["leverage"]
    notional = margin_usdt * lev
    qty = notional / price if price > 0 else 0
    if not CFG["auto_trade"]:
        return True, f"auto_trade 꺼짐 — 실제 주문 없음(알림만, {lev}배 노출 {notional:.0f}USDT 가정)", qty
    ex = get_exchange()
    if not ex:
        return False, "거래소 연결 실패", 0
    if qty <= 0:
        return False, "수량 계산 오류(가격 0)", 0
    ensure_leverage(ex, symbol)
    try:
        order_params = {"hedged": True, "reduceOnly": reduce_only}
        order = ex.create_market_order(symbol, side, qty, params=order_params)
        return True, f"체결 완료 (주문ID {order.get('id','?')}, {lev}배 노출 {notional:.0f}USDT)", qty
    except Exception as e:
        return False, f"주문 실패: {e}", 0

# ══════════════════════════════════════════════════════
#  그리드 로직
# ══════════════════════════════════════════════════════
def check_grid(state: dict, price: float) -> dict:
    """
    현재가를 기준가와 비교해 매수/매도 신호 판단.
    반환: {"action": "buy"|"sell"|None, "reason": str}
    """
    ref = state.get("reference_price")
    if ref is None or ref <= 0:
        return {"action": "init", "reason": "최초 실행 — 기준가 설정"}

    step = CFG["step_pct"] / 100
    drop_trigger = ref * (1 - step)
    rise_trigger = ref * (1 + step)

    if price <= drop_trigger:
        invested = state.get("holdings_usdt", 0.0)
        if invested >= CFG["max_total_usdt"]:
            return {"action": "buy_blocked", "reason":
                    f"총 투입한도({CFG['max_total_usdt']:.0f}USDT) 도달 — 매수 중단(안전장치)"}
        return {"action": "buy", "reason": f"기준가 {ref:,.4f} 대비 -{CFG['step_pct']:.0f}% 도달"}

    if price >= rise_trigger:
        qty_needed = (CFG["order_usdt"] * CFG["leverage"]) / price
        if state.get("qty", 0.0) < qty_needed:
            return {"action": "sell_blocked", "reason": "보유 수량 부족 — 매도할 물량 없음"}
        return {"action": "sell", "reason": f"기준가 {ref:,.4f} 대비 +{CFG['step_pct']:.0f}% 도달"}

    return {"action": None, "reason": f"대기 (기준가 {ref:,.4f}, "
            f"매수트리거 {drop_trigger:,.4f} / 매도트리거 {rise_trigger:,.4f})"}

# ══════════════════════════════════════════════════════
#  메인 점검
# ══════════════════════════════════════════════════════
def run_check(send: bool = True):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'='*56}")
    print(f"  코인 그리드매매 (떨사오팔) — {CFG['symbol']} — {now}")
    print(f"{'='*56}")

    if not HAS_CCXT:
        print("  ❌ ccxt 필요: pip install ccxt")
        return

    state = load_state()
    price = fetch_price(CFG["symbol"])
    if price <= 0:
        print(f"  ❌ {CFG['symbol']} 시세 조회 실패 — --find-symbol 로 정확한 심볼 확인 필요")
        return
    print(f"  현재가: {price:,.4f} USDT")

    result = check_grid(state, price)
    action = result["action"]
    print(f"  {result['reason']}")

    msg = None

    if action == "init":
        state["reference_price"] = price
        state.setdefault("holdings_usdt", 0.0)
        state.setdefault("qty", 0.0)
        print(f"  ✅ 기준가 {price:,.4f} 설정 완료")

    elif action == "buy":
        ok, detail, qty = place_order(CFG["symbol"], "buy", CFG["order_usdt"], price, reduce_only=False)
        if ok:
            state["qty"] = state.get("qty", 0.0) + qty
            state["holdings_usdt"] = state.get("holdings_usdt", 0.0) + CFG["order_usdt"]
            state["reference_price"] = price   # 래칫: 기준가를 더 아래로
            notional = CFG["order_usdt"] * CFG["leverage"]
            msg = (f"🟢 <b>{CFG['symbol']} 매수</b> ({CFG['step_pct']:.0f}% 하락)\n"
                   f"체결가: {price:,.4f} USDT\n"
                   f"증거금: {CFG['order_usdt']:.0f} USDT × {CFG['leverage']}배 "
                   f"= 노출 {notional:.0f} USDT ({qty:.4f}개)\n"
                   f"누적 증거금: {state['holdings_usdt']:.0f} / {CFG['max_total_usdt']:.0f} USDT\n"
                   f"{detail}")
            print(f"  🟢 매수 체결: {detail}")
        else:
            msg = f"❌ <b>{CFG['symbol']} 매수 실패</b>\n{detail}\n다음 점검에서 재시도됩니다"
            print(f"  ❌ 매수 실패: {detail}")

    elif action == "sell":
        qty_to_sell = (CFG["order_usdt"] * CFG["leverage"]) / price
        ok, detail, qty = place_order(CFG["symbol"], "sell", CFG["order_usdt"], price, reduce_only=True)
        if ok:
            state["qty"] = max(0.0, state.get("qty", 0.0) - qty_to_sell)
            state["holdings_usdt"] = max(0.0, state.get("holdings_usdt", 0.0) - CFG["order_usdt"])
            state["reference_price"] = price   # 래칫: 기준가를 더 위로
            notional = CFG["order_usdt"] * CFG["leverage"]
            msg = (f"🔴 <b>{CFG['symbol']} 매도</b> ({CFG['step_pct']:.0f}% 상승)\n"
                   f"체결가: {price:,.4f} USDT\n"
                   f"증거금: {CFG['order_usdt']:.0f} USDT × {CFG['leverage']}배 "
                   f"= 노출 {notional:.0f} USDT ({qty_to_sell:.4f}개)\n"
                   f"잔여 증거금: {state['holdings_usdt']:.0f} USDT\n"
                   f"{detail}")
            print(f"  🔴 매도 체결: {detail}")
        else:
            msg = f"❌ <b>{CFG['symbol']} 매도 실패</b>\n{detail}\n다음 점검에서 재시도됩니다"
            print(f"  ❌ 매도 실패: {detail}")

    elif action == "buy_blocked":
        print(f"  ⚠️ {result['reason']}")
        # 매수 막힘 알림은 반복 스팸 방지를 위해 상태에 1회만 기록
        if not state.get("notified_budget_full"):
            msg = f"⚠️ <b>{CFG['symbol']} 매수 한도 도달</b>\n{result['reason']}\n(매도는 계속 감시 중)"
            state["notified_budget_full"] = True

    elif action == "sell_blocked":
        print(f"  ⚠️ {result['reason']}")

    if send:
        save_state(state)
    else:
        print("  (--check 모드: 상태 저장 생략)")

    if msg:
        if send:
            send_discord(msg)
            print("\n  ✅ 알림 발송")
        else:
            print("\n  [--check, 발송 생략]")
            print("  " + msg.replace("<b>","").replace("</b>",""))
    else:
        print("\n  액션 없음 (대기)")

# ══════════════════════════════════════════════════════
#  실행
# ══════════════════════════════════════════════════════
if __name__ == "__main__":
    args = sys.argv[1:]

    if "--find-symbol" in args:
        idx = args.index("--find-symbol")
        kw = args[idx+1] if idx+1 < len(args) else "TQQQ"
        find_symbol(kw)
        sys.exit(0)

    if "--reset" in args:
        if os.path.exists(CFG["state_file"]):
            os.remove(CFG["state_file"])
        print("  ✅ 그리드 상태 초기화 완료 (실제 보유 코인은 그대로입니다)")
        sys.exit(0)

    if "--status" in args:
        st = load_state()
        if not st or st.get("reference_price") is None:
            print("  아직 시작 안 함 (기준가 미설정)")
        else:
            print(f"  기준가: {st['reference_price']:,.4f}")
            print(f"  레버리지: {CFG['leverage']}배")
            print(f"  보유 수량: {st.get('qty',0):.4f}")
            print(f"  누적 증거금: {st.get('holdings_usdt',0):.0f} / {CFG['max_total_usdt']:.0f} USDT")
            print(f"  누적 노출액(레버리지 반영): {st.get('holdings_usdt',0)*CFG['leverage']:.0f} USDT")
        sys.exit(0)

    run_check(send=("--check" not in args))
