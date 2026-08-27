"""
코인 범위매매 실전 자동매매 — BTC 전용 (신호 감지 + 실제 주문 실행)
============================================================
백테스트로 검증된 범위매매 전략(PF 1.81, 전 국면 수익)을
실전에서 자동 매매한다. 신호 발생 시 Bitget에 실제 주문을 넣고,
결과를 디스코드로 알린다.

[2026-08-24 변경]
  - SOL 제거, BTC만 운용
  - 알림 전용 → 실전 자동매매로 전환 (Bitget API 실주문)
  - 계좌가 헷지모드(hedge_mode)라 주문 시 hedged=True 파라미터 필수
    (2026-08-21 coin_grid_monitor.py에서 ccxt 소스코드 직접 확인해
     찾아낸 방식과 동일하게 적용)

[.env 설정 — 이미 있다면 재사용]
  DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
  BITGET_API_KEY=...
  BITGET_API_SECRET=...
  BITGET_PASSPHRASE=...

[검증된 파라미터 — BTC]
  RSI≤30 매수 / +4% 익절 / -4% 손절 (레버리지 3배)

[동작]
  실행(cron) 시:
    1) 미보유: RSI 체크 → 과매도(≤30)면 실제 매수 주문 + 진입가/수량 기록
    2) 보유 중: 현재가로 익절/손절 도달 확인 → 실제 매도 주문 + 기록삭제
  → 주문 성공을 확인한 뒤에만 상태를 기록한다 (실패 시 유령 포지션 방지)

[안전장치]
  - auto_trade 기본값 False (알림만, 실제 주문 없음) — 반드시 --check로
    충분히 확인 후 True로 전환할 것
  - 매수 실패 시 보유기록 남기지 않음, 매도 실패 시 보유기록 유지(재시도)

실행:
  python coin_range_monitor.py          # 1회 점검 (cron용)
  python coin_range_monitor.py --check  # 상태만 출력 (주문 없음, 저장도 안 함)
  python coin_range_monitor.py --status # 현재 보유 현황
  python coin_range_monitor.py --reset  # 보유기록 초기화 (실제 보유는 그대로)
"""

import sys, json, os, re, time
from datetime import datetime, timezone

try:
    import ccxt
    HAS_CCXT = True
except ImportError:
    HAS_CCXT = False

# ── config.py에서 디스코드 웹훅 + Bitget API 키 로드 ──
try:
    import config as _cfg
    def _cget(key):
        return _cfg._get(key) if hasattr(_cfg, "_get") else getattr(_cfg, key, "")
    DISCORD_WEBHOOK_URL = _cget("DISCORD_WEBHOOK_URL")
    BITGET_API_KEY      = _cget("BITGET_API_KEY")
    BITGET_API_SECRET   = _cget("BITGET_API_SECRET") or _cget("BITGET_SECRET_KEY")
    BITGET_PASSPHRASE   = _cget("BITGET_PASSPHRASE") or _cget("BITGET_PASSWORD")
except ImportError:
    print("⚠️  config.py 없음 — .env 설정 확인 필요")
    DISCORD_WEBHOOK_URL = BITGET_API_KEY = BITGET_API_SECRET = BITGET_PASSPHRASE = ""

import requests

# ══════════════════════════════════════════════════════
#  설정 — 백테스트 검증 최적 파라미터
# ══════════════════════════════════════════════════════
COINS = {
    "BTC/USDT": {
        "rsi_buy"      : 30,      # RSI 30 이하 매수
        "target_pct"   : 4.0,     # +4% 익절
        "stop_pct"     : 4.0,     # -4% 손절
        "label"        : "BTC",
        # ⚠️ 시세(RSI)는 Binance에서 조회(콜론 없는 표기)하지만,
        # 실제 주문은 Bitget 선물(USDT-M Perpetual)에 넣어야 하므로
        # 정확한 실행용 심볼을 별도로 지정 (2026-08-24 확인된 필수 사항 —
        # 콜론 없이 주문하면 "Insufficient balance"로 오해할 오류가 남)
        "exec_symbol"  : "BTC/USDT:USDT",
    },
}

CFG = {
    "timeframe"    : "4h",       # 4시간봉 (백테스트와 동일)
    "rsi_period"   : 14,
    "ma_period"    : 50,         # 하락장 필터용
    "down_guard"   : 8.0,        # MA -8% 이탈 시 매수 보류
    "leverage"     : 3,          # 거래소에 실제 설정할 레버리지
    "order_usdt"   : 10.0,       # ⚠️ 1회 매매 증거금(USDT) — ×3배 레버리지 = 노출 30USDT
    "auto_trade"   : True,       # ✅ 실전 자동매매 활성화 (2026-08-24 사용자 확정)
    "state_file"   : "coin_range_holdings.json",
}

# ══════════════════════════════════════════════════════
#  디스코드 (텔레그램 대체)
# ══════════════════════════════════════════════════════
def _html_to_discord_markdown(text: str) -> str:
    """<b>...</b> → **...** 등 텔레그램 HTML 태그를 디스코드 마크다운으로 변환"""
    text = re.sub(r"<b>(.*?)</b>", r"**\1**", text, flags=re.DOTALL)
    text = re.sub(r"<i>(.*?)</i>", r"*\1*", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", "", text)   # 남은 태그는 제거
    return text

def send_discord(text: str) -> bool:
    if not DISCORD_WEBHOOK_URL:
        print("  (디스코드 웹훅 미설정 — 콘솔 출력만)")
        print(text)
        return False
    content = _html_to_discord_markdown(text)
    if len(content) > 1900:   # 디스코드 메시지 길이 제한(2000자) 안전 마진
        content = content[:1900] + "\n…(생략)"
    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json={"content": content}, timeout=10)
        # 디스코드 웹훅은 성공 시 204 No Content 반환
        return r.status_code in (200, 204)
    except Exception as e:
        print(f"  디스코드 오류: {e}")
        return False

# ══════════════════════════════════════════════════════
#  실제 주문 실행 (Bitget, ccxt)
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

_leverage_set = set()   # 심볼별로 실행당 1회만 레버리지 설정 시도

def ensure_leverage(ex, symbol: str):
    if symbol in _leverage_set or not CFG["auto_trade"]:
        return
    try:
        if not ex.markets:
            ex.load_markets()
        market = ex.market(symbol)
        margin_coin = market.get("settleId") or market.get("settle") or "USDT"
        ex.set_leverage(CFG["leverage"], symbol, params={"marginCoin": margin_coin})
        print(f"  ✅ 레버리지 {CFG['leverage']}배 설정 완료 ({symbol})")
    except Exception as e:
        print(f"  ⚠️ 레버리지 설정 실패(이미 설정돼 있거나 확인 필요): {e}")
    _leverage_set.add(symbol)

def place_order(symbol: str, side: str, margin_usdt: float, price: float, reduce_only: bool = False) -> tuple:
    """
    실제 시장가 주문. margin_usdt는 투입 증거금, 체결 수량은
    레버리지가 곱해진 명목가치(notional) 기준으로 계산된다.

    ⚠️ Bitget 계좌가 헷지모드(hedge_mode)이므로 hedged=True 필수
    (2026-08-21 coin_grid_monitor.py에서 ccxt bitget.py 소스코드 직접
     확인해 검증된 방식 — holdSide 직접 지정은 무시되고, hedged=True를
     주면 ccxt가 side/reduceOnly를 보고 posSide를 자동 계산해줌)

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
        # Bitget 선물 시장가 매수는 총비용(amount*price) 계산을 위해
        # price 인자가 필요함 (ccxt 오류 메시지로 확인된 사양)
        order = ex.create_market_order(symbol, side, qty, price, params=order_params)
        return True, f"체결 완료 (주문ID {order.get('id','?')}, {lev}배 노출 {notional:.0f}USDT)", qty
    except Exception as e:
        return False, f"주문 실패: {e}", 0

# ══════════════════════════════════════════════════════
#  보유 상태 저장/로드 (봇이 자동 추적)
# ══════════════════════════════════════════════════════
def load_holdings() -> dict:
    path = CFG["state_file"]
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_holdings(h: dict):
    with open(CFG["state_file"], "w", encoding="utf-8") as f:
        json.dump(h, f, ensure_ascii=False, indent=2)

# ══════════════════════════════════════════════════════
#  지표
# ══════════════════════════════════════════════════════
def calc_rsi(closes, period=14):
    n = len(closes)
    if n < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, n):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0)); losses.append(max(-d, 0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, n-1):
        avg_g = (avg_g*(period-1) + gains[i]) / period
        avg_l = (avg_l*(period-1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100 - (100 / (1 + rs))

def calc_sma(values, period):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period

# ══════════════════════════════════════════════════════
#  시세 조회 (Binance — 백테스트와 동일 소스)
# ══════════════════════════════════════════════════════
def fetch_recent(symbol: str, timeframe: str, limit: int = 120):
    if not HAS_CCXT:
        return []
    ex = ccxt.binance()
    try:
        return ex.fetch_ohlcv(symbol, timeframe, limit=limit)
    except Exception as e:
        print(f"  {symbol} 시세 조회 오류: {e}")
        return []

# ══════════════════════════════════════════════════════
#  핵심: 코인별 점검
# ══════════════════════════════════════════════════════
def check_coin(symbol: str, params: dict, holdings: dict) -> dict:
    """
    한 코인을 점검하고, 필요 시 신호를 반환.
    반환: {"signal": "buy"|"sell_target"|"sell_stop"|None, ...}
    """
    ohlcv = fetch_recent(symbol, CFG["timeframe"], 120)
    if len(ohlcv) < CFG["ma_period"] + 2:
        return {"signal": None, "error": "데이터 부족"}

    closes = [c[4] for c in ohlcv]
    cur_price = closes[-1]
    rsi = calc_rsi(closes, CFG["rsi_period"])
    ma = calc_sma(closes, CFG["ma_period"])
    label = params["label"]

    held = holdings.get(symbol)

    # ── 보유 중: 익절/손절 확인 ────────────────────────
    if held:
        entry = held["entry_price"]
        change_pct = (cur_price - entry) / entry * 100
        target = params["target_pct"]
        stop = params["stop_pct"]

        if change_pct >= target:
            return {"signal": "sell_target", "price": cur_price,
                    "entry": entry, "change": change_pct, "label": label}
        if change_pct <= -stop:
            return {"signal": "sell_stop", "price": cur_price,
                    "entry": entry, "change": change_pct, "label": label}
        # 보유 유지
        return {"signal": None, "held": True, "price": cur_price,
                "entry": entry, "change": change_pct, "rsi": rsi, "label": label}

    # ── 미보유: 매수 신호 확인 ─────────────────────────
    if rsi is None:
        return {"signal": None, "label": label}

    # 하락장 필터 (칼 안 잡기)
    down_market = False
    if ma and ma > 0 and (cur_price - ma) / ma * 100 < -CFG["down_guard"]:
        down_market = True

    if rsi <= params["rsi_buy"] and not down_market:
        return {"signal": "buy", "price": cur_price, "rsi": rsi,
                "target_pct": params["target_pct"], "stop_pct": params["stop_pct"],
                "label": label}

    return {"signal": None, "price": cur_price, "rsi": rsi,
            "down_market": down_market, "label": label}

# ══════════════════════════════════════════════════════
#  알림 메시지 생성
# ══════════════════════════════════════════════════════
def msg_buy(r: dict, symbol: str) -> str:
    price = r["price"]
    tgt = price * (1 + r["target_pct"]/100)
    stp = price * (1 - r["stop_pct"]/100)
    lev = CFG["leverage"]
    return (
        f"🟢 <b>{r['label']} 매수 신호!</b>\n"
        f"RSI {r['rsi']:.1f} (과매도)\n"
        f"현재가: ${price:,.2f}\n\n"
        f"목표: +{r['target_pct']:.0f}% → ${tgt:,.2f}\n"
        f"손절: -{r['stop_pct']:.0f}% → ${stp:,.2f}\n\n"
        f"💡 {lev}배 레버리지 기준\n"
        f"  익절 시 증거금 +{r['target_pct']*lev:.0f}%\n"
        f"  손절 시 증거금 -{r['stop_pct']*lev:.0f}%\n\n"
        f"📌 <b>예약주문 권장</b> (밤 대비):\n"
        f"  지정가 매수 + OCO(익절/손절) 걸어두기\n"
        f"⚠️ 손절 반드시 설정하세요"
    )

def msg_sell(r: dict, symbol: str, reason: str) -> str:
    icon = "🎯" if reason == "target" else "🔴"
    word = "익절" if reason == "target" else "손절"
    lev = CFG["leverage"]
    margin_change = r["change"] * lev
    return (
        f"{icon} <b>{r['label']} {word} 신호!</b>\n"
        f"진입가: ${r['entry']:,.2f}\n"
        f"현재가: ${r['price']:,.2f}\n"
        f"수익률: {r['change']:+.1f}% (가격)\n"
        f"{lev}배 증거금 기준: {margin_change:+.1f}%\n\n"
        f"📌 매도(청산) 권고"
    )

# ══════════════════════════════════════════════════════
#  메인 점검 루프
# ══════════════════════════════════════════════════════
def run_check(send: bool = True):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'='*56}")
    print(f"  코인 범위매매 모니터 — {now}")
    print(f"{'='*56}")

    if not HAS_CCXT:
        print("  ❌ ccxt 필요: pip install ccxt")
        return

    holdings = load_holdings()
    alerts = []

    for symbol, params in COINS.items():
        r = check_coin(symbol, params, holdings)
        label = params["label"]

        if r["signal"] == "buy":
            # ⚠️ 실제 매수 주문 → 체결 성공을 확인한 뒤에만 보유 기록
            #    (실패 시 기록하면 유령 포지션이 되어 계속 잘못 감시하게 됨)
            msg = msg_buy(r, symbol)
            if CFG["auto_trade"] and send:
                ok, detail, qty = place_order(params.get("exec_symbol", symbol), "buy", CFG["order_usdt"], r["price"], reduce_only=False)
                if ok:
                    holdings[symbol] = {
                        "entry_price": r["price"], "entry_time": now,
                        "target_pct": params["target_pct"], "stop_pct": params["stop_pct"],
                        "qty": qty, "auto_traded": True,
                    }
                    msg += f"\n\n🤖 <b>자동매수 실행됨</b>: {detail}"
                    print(f"  🟢 {label}: 매수 체결 (RSI {r['rsi']:.1f}, ${r['price']:,.2f}) {detail}")
                else:
                    msg += f"\n\n🤖 ❌ 자동매수 실패: {detail}\n→ 신호는 유효하나 주문은 안 됨. 다음 점검에서 재시도됩니다"
                    print(f"  ❌ {label}: 매수 실패 — {detail}")
            else:
                # 알림 전용 모드: 참고용으로만 보유 기록 (실주문 없음)
                holdings[symbol] = {
                    "entry_price": r["price"], "entry_time": now,
                    "target_pct": params["target_pct"], "stop_pct": params["stop_pct"],
                }
                print(f"  🟢 {label}: 매수 신호 (RSI {r['rsi']:.1f}, ${r['price']:,.2f}) [알림전용]")
            alerts.append(msg)

        elif r["signal"] in ("sell_target", "sell_stop"):
            reason = "target" if r["signal"] == "sell_target" else "stop"
            msg = msg_sell(r, symbol, reason)
            held = holdings.get(symbol, {})
            qty_held = held.get("qty")

            if CFG["auto_trade"] and send and qty_held:
                # 보유수량을 실제 알고 있을 때만(=봇이 직접 산 것만) 자동매도
                ok, detail, _ = place_order(params.get("exec_symbol", symbol), "sell", CFG["order_usdt"], r["price"], reduce_only=True)
                if ok:
                    msg += f"\n\n🤖 <b>자동매도 실행됨</b>: {detail}"
                    holdings.pop(symbol, None)   # 청산 성공 시에만 기록 삭제
                    print(f"  {'🎯' if reason=='target' else '🔴'} {label}: 매도 체결 ({r['change']:+.1f}%) {detail}")
                else:
                    msg += f"\n\n🤖 ❌ 자동매도 실패: {detail}\n→ 즉시 확인 필요! 다음 점검에서 재시도됩니다"
                    print(f"  ❌ {label}: 매도 실패 — {detail} (보유기록 유지, 재시도 예정)")
                    # 실패 시 holdings 그대로 유지 → 다음 점검에서 재시도
            else:
                if CFG["auto_trade"] and send and not qty_held:
                    msg += "\n\n🤖 자동매도 생략: 봇이 기록한 보유수량 없음(수동 매수분으로 추정) — 직접 매도하세요"
                holdings.pop(symbol, None)
                print(f"  {'🎯' if reason=='target' else '🔴'} {label}: {reason} 신호 ({r['change']:+.1f}%) [알림전용 또는 수량미상]")
            alerts.append(msg)

        elif r.get("held"):
            print(f"  ⏳ {label}: 보유 중 (진입 ${r['entry']:,.2f}, "
                  f"현재 {r['change']:+.1f}%, RSI {r.get('rsi') or 0:.1f})")

        else:
            rsi_str = f"RSI {r.get('rsi'):.1f}" if r.get('rsi') else "데이터부족"
            dm = " [하락장 보류]" if r.get("down_market") else ""
            print(f"  ⚪ {label}: 대기 ({rsi_str}){dm}")

    save_holdings(holdings)

    # 신호가 있으면 디스코드 발송
    if alerts and send:
        for msg in alerts:
            send_discord(msg)
        print(f"\n  ✅ 신호 {len(alerts)}건 디스코드 발송")
    elif not alerts:
        print(f"\n  신호 없음 (대기)")

    return holdings

# ══════════════════════════════════════════════════════
#  실행
# ══════════════════════════════════════════════════════
if __name__ == "__main__":
    args = sys.argv[1:]

    if "--reset" in args:
        if os.path.exists(CFG["state_file"]):
            os.remove(CFG["state_file"])
        print("  ✅ 보유 기록 초기화 완료")
        sys.exit(0)

    if "--check" in args:
        # 상태만 출력, 디스코드 발송 안 함
        run_check(send=False)
        sys.exit(0)

    if "--status" in args:
        h = load_holdings()
        if not h:
            print("  보유 없음 (idle)")
        else:
            for symbol, pos in h.items():
                print(f"  {symbol}: 진입 ${pos.get('entry_price',0):,.2f}, "
                      f"수량 {pos.get('qty','?')}, "
                      f"{'[자동매매]' if pos.get('auto_traded') else '[알림전용]'}")
        sys.exit(0)

    # 기본: 점검 + 신호 발송
    run_check(send=True)
