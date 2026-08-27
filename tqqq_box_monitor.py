"""
TQQQ 박스전략 실전 모니터 (신호 감지 + 보유 자동 추적, 레버리지 없음)
================================================================
검증된 박스전략(PF 2.53, MDD -9.3%, 1배)을 TQQQ에서 실전 감시한다.
TQQQ 자체가 이미 3배 레버리지 ETF이므로 추가 레버리지 없이(1배) 운용.
실제 주문은 내지 않고, 매수/익절/손절/브레이크이븐 타이밍을
텔레그램으로 알린다. 보유 상태는 봇이 JSON으로 자동 추적한다.

[검증된 최적 파라미터]
  4시간봉 박스(스윙7봉) + 1시간봉 확정
  매수존: 박스 하단 20% / 최소손익비: 1.5
  손절: 신호봉 저점 / 목표: 박스 상단 / +1R 브레이크이븐

[미국장 시간 안내]
  정규장: 23:30~06:00 (한국시간, 서머타임 시 22:30~05:00)
  1시간봉 전략이라 장중에만 의미 있는 신호가 나온다.
  장 마감 후 실행되면 "장외 시간" 안내만 하고 스킵한다.

실행:
  python tqqq_box_monitor.py           # 1회 점검 (cron 매시간, 장중)
  python tqqq_box_monitor.py --check   # 상태만 출력 (발송 안 함)
  python tqqq_box_monitor.py --reset   # 상태 초기화
  python tqqq_box_monitor.py --force   # 장외 시간이어도 강제 점검(테스트용)

cron 등록 (미국 정규장 시간대, 한국시간 기준, 매시간):
  30 22-23,0-5 * * 1-5 cd ~/trading-bot && venv/bin/python tqqq_box_monitor.py >> logs/tqqq_box.log 2>&1
  (서머타임 여부에 따라 UTC 22:30~05:30 근처로 조정 필요)
"""

import sys, json, os, time
from datetime import datetime, timezone, timedelta
from typing import Optional

try:
    import yfinance as yf
    HAS_YF = True
except ImportError:
    HAS_YF = False

try:
    from config import (
        TELEGRAM_TOKEN_US as TG_TOKEN,
        TELEGRAM_CHAT_ID_US as TG_CHAT,
        telegram_configured,
    )
except ImportError:
    print("config.py 없음 — 텔레그램 설정 확인 필요")
    TG_TOKEN = TG_CHAT = ""
    telegram_configured = lambda *a: False

import requests

# ══════════════════════════════════════════════════════
#  설정 (백테스트 검증 최적값 그대로)
# ══════════════════════════════════════════════════════
CFG = {
    "symbol"         : "TQQQ",
    "swing_lookback" : 7,     # 최적화 결과
    "box_min_pct"    : 1.5,
    "box_max_pct"    : 25.0,
    "buy_zone_pct"   : 20.0,  # 최적화 결과: 박스 하단 20%
    "confirm_bars"   : 5,     # 반전 양봉 후 5시간 내 돌파해야
    "sl_buffer_pct"  : 0.2,
    "min_rr"         : 1.5,   # 최적화 결과
    "breakeven_at_r" : 1.0,
    "leverage"       : 1,     # TQQQ 자체가 3배 상품 → 추가 레버리지 없음
    "state_file"     : "tqqq_box_state.json",
    "h1_period"      : "60d", # 1시간봉 60일치면 4시간봉 박스 계산에 충분
}

# ══════════════════════════════════════════════════════
#  텔레그램 / 상태 저장
# ══════════════════════════════════════════════════════
def send_telegram(text: str) -> bool:
    try:
        if not telegram_configured("us"):
            print("  (텔레그램 미설정 — 콘솔 출력)")
            print(text)
            return False
    except TypeError:
        if not telegram_configured():
            print(text); return False
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": TG_CHAT, "text": text,
                                     "parse_mode": "HTML"}, timeout=10)
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
#  미국장 시간 체크 (뉴욕 시간 기준, 대략적 — 공휴일 미반영)
# ══════════════════════════════════════════════════════
def is_market_hours() -> bool:
    """
    뉴욕 시간 9:30~16:00 (정규장)을 UTC로 근사 계산.
    서머타임(3월~11월)은 UTC-4, 아니면 UTC-5로 근사.
    """
    now_utc = datetime.now(timezone.utc)
    month = now_utc.month
    # 대략적 서머타임 판정 (3~11월)
    ny_offset = -4 if 3 <= month <= 11 else -5
    ny_time = now_utc + timedelta(hours=ny_offset)
    if ny_time.weekday() >= 5:   # 토/일
        return False
    minutes = ny_time.hour * 60 + ny_time.minute
    return 9*60+30 <= minutes <= 16*60

# ══════════════════════════════════════════════════════
#  시세 수집 (yfinance)
# ══════════════════════════════════════════════════════
def fetch_h1(symbol: str, period: str) -> list:
    if not HAS_YF:
        return []
    try:
        df = yf.download(symbol, period=period, interval="1h",
                         progress=False, auto_adjust=True)
    except Exception as e:
        print(f"  {symbol} 조회 오류: {e}")
        return []
    if df is None or len(df) == 0:
        return []
    out = []
    for ts, row in df.iterrows():
        try:
            o, h, l, c = (float(row["Open"].iloc[0]), float(row["High"].iloc[0]),
                         float(row["Low"].iloc[0]), float(row["Close"].iloc[0]))
        except (TypeError, ValueError, AttributeError):
            try:
                o, h, l, c = float(row["Open"]), float(row["High"]), float(row["Low"]), float(row["Close"])
            except (TypeError, ValueError):
                continue
        if c <= 0:
            continue
        out.append([int(ts.timestamp()*1000), o, h, l, c])
    return out

def resample_4h(h1: list) -> list:
    """1시간봉 → 4시간봉 리샘플"""
    out, bucket = [], []
    for c in h1:
        ts = c[0]
        hstart = ts - (ts % 14_400_000)
        if bucket and (bucket[0][0] - (bucket[0][0] % 14_400_000)) != hstart:
            o = bucket[0][1]; h = max(x[2] for x in bucket)
            l = min(x[3] for x in bucket); cl = bucket[-1][4]
            out.append([bucket[0][0] - (bucket[0][0] % 14_400_000), o, h, l, cl])
            bucket = []
        bucket.append(c)
    if bucket:
        o = bucket[0][1]; h = max(x[2] for x in bucket)
        l = min(x[3] for x in bucket); cl = bucket[-1][4]
        out.append([bucket[0][0] - (bucket[0][0] % 14_400_000), o, h, l, cl])
    return out

# ══════════════════════════════════════════════════════
#  박스 계산 (백테스트와 동일 — 상승추세 대응 페어링)
# ══════════════════════════════════════════════════════
def current_box(h4: list) -> tuple:
    lb = CFG["swing_lookback"]
    n = len(h4)
    if n < lb*2 + 5:
        return (None, None)
    highs = [c[2] for c in h4]; lows = [c[3] for c in h4]
    swing_highs, swing_lows = [], []
    for i in range(lb, n-lb):
        if highs[i] == max(highs[i-lb:i+lb+1]):
            swing_highs.append(highs[i])
        if lows[i] == min(lows[i-lb:i+lb+1]):
            swing_lows.append(lows[i])
    if not swing_highs or not swing_lows:
        return (None, None)
    cur_low = swing_lows[-1]
    cur_high = None
    for hv in reversed(swing_highs):
        if hv > cur_low:
            cur_high = hv; break
    if cur_high:
        height = (cur_high - cur_low)/cur_low*100
        if CFG["box_min_pct"] <= height <= CFG["box_max_pct"]:
            return (cur_low, cur_high)
    return (None, None)

# ══════════════════════════════════════════════════════
#  점검 로직 (코인 박스 모니터와 동일 상태 머신)
# ══════════════════════════════════════════════════════
def check_symbol(symbol: str, state: dict) -> Optional[str]:
    st = state.get(symbol, {"phase": "idle"})
    phase = st.get("phase", "idle")

    h1 = fetch_h1(symbol, CFG["h1_period"])
    if len(h1) < 10:
        print(f"  {symbol}: 데이터 부족")
        return None
    last_closed = h1[-2]   # 마지막은 진행 중일 수 있어 직전 확정봉 사용
    cur_price = h1[-1][4]
    lo, lc = last_closed[3], last_closed[4]

    # ── 보유 중 ──
    if phase == "holding":
        entry, sl, target = st["entry"], st["sl"], st["target"]
        be_done = st.get("be_done", False)
        risk = entry - st["orig_sl"]

        if cur_price <= sl:
            word = "본전청산" if be_done else "손절"
            state[symbol] = {"phase": "idle"}
            return (f"🔴 <b>{symbol} {word} 신호!</b>\n"
                    f"진입 ${entry:,.2f} → 현재 ${cur_price:,.2f}\n"
                    f"손절선 ${sl:,.2f} 도달 — 매도(청산)하세요")
        if cur_price >= target:
            pct = (target-entry)/entry*100
            state[symbol] = {"phase": "idle"}
            return (f"🎯 <b>{symbol} 목표 도달!</b> (박스 상단)\n"
                    f"진입 ${entry:,.2f} → 목표 ${target:,.2f} ({pct:+.1f}%)\n"
                    f"매도(익절)하세요")
        if not be_done and cur_price >= entry + risk*CFG["breakeven_at_r"]:
            st["sl"] = entry; st["be_done"] = True
            state[symbol] = st
            return (f"🛡 <b>{symbol} 브레이크이븐!</b> (+1R 도달)\n"
                    f"손절 주문을 진입가 ${entry:,.2f}로 올리세요\n"
                    f"→ 이제 이 거래는 잃지 않습니다")
        chg = (cur_price-entry)/entry*100
        print(f"  ⏳ {symbol}: 보유 중 (진입 ${entry:,.2f}, {chg:+.1f}%, "
              f"목표 ${target:,.2f}, 손절 ${st['sl']:,.2f})")
        state[symbol] = st
        return None

    h4 = resample_4h(h1)
    box_low, box_high = current_box(h4)
    if not box_low:
        print(f"  ⚪ {symbol}: 유효한 박스 없음")
        state[symbol] = {"phase": "idle"}
        return None

    # ── 돌파 대기 ──
    if phase == "waiting":
        sig_high, sig_low = st["sig_high"], st["sig_low"]
        st["bars_waited"] = st.get("bars_waited", 0) + 1
        if cur_price < sig_low:
            state[symbol] = {"phase": "idle"}
            print(f"  ⚪ {symbol}: 돌파 대기 무효화 (신호봉 저점 이탈)")
            return None
        if st["bars_waited"] > CFG["confirm_bars"]:
            state[symbol] = {"phase": "idle"}
            print(f"  ⚪ {symbol}: 돌파 대기 시간 초과 (리셋)")
            return None
        if cur_price >= sig_high:
            entry = sig_high
            sl = sig_low * (1 - CFG["sl_buffer_pct"]/100)
            target = box_high
            risk_d = entry - sl
            rr = (target-entry)/risk_d if risk_d > 0 else 0
            if rr < CFG["min_rr"]:
                state[symbol] = {"phase": "idle"}
                print(f"  ⚪ {symbol}: 돌파했으나 손익비 {rr:.1f} 미달 — 포기")
                return None
            state[symbol] = {"phase": "holding", "entry": entry, "sl": sl,
                             "orig_sl": sl, "target": target, "be_done": False}
            risk_pct = risk_d/entry*100
            return (f"🟢 <b>{symbol} 매수 신호!</b> (박스전략 확정)\n"
                    f"신호봉 고점 ${sig_high:,.2f} 돌파\n"
                    f"진입가: ${entry:,.2f}\n\n"
                    f"🎯 목표(박스상단): ${target:,.2f} (+{(target-entry)/entry*100:.1f}%)\n"
                    f"📍 손절(신호봉저점): ${sl:,.2f} (-{risk_pct:.1f}%)\n"
                    f"손익비 1:{rr:.1f}\n\n"
                    f"💡 레버리지 추가 없이(1배) — TQQQ 자체가 이미 3배 상품\n"
                    f"📌 +1R 도달 시 브레이크이븐 알림 예정")
        print(f"  ⏳ {symbol}: 돌파 대기 중 ({st['bars_waited']}/{CFG['confirm_bars']}봉, "
              f"돌파선 ${sig_high:,.2f}, 현재 ${cur_price:,.2f})")
        state[symbol] = st
        return None

    # ── 대기: 매수존 + 반전 양봉 ──
    box_h = box_high - box_low
    buy_zone_top = box_low + box_h*CFG["buy_zone_pct"]/100
    in_zone = lo <= buy_zone_top
    is_bull = lc > last_closed[1]

    if in_zone and is_bull:
        state[symbol] = {"phase": "waiting", "sig_high": last_closed[2],
                         "sig_low": last_closed[3], "bars_waited": 0}
        return (f"👀 <b>{symbol} 박스 하단 반전 캔들 감지</b>\n"
                f"박스: ${box_low:,.2f} ~ ${box_high:,.2f}\n"
                f"신호봉 고점 ${last_closed[2]:,.2f} 돌파 시 매수 신호 발송")
    zone_pos = (cur_price-box_low)/box_h*100 if box_h > 0 else 0
    print(f"  ⚪ {symbol}: 대기 (박스 ${box_low:,.2f}~${box_high:,.2f}, "
          f"현재 위치 {zone_pos:.0f}%)")
    state[symbol] = {"phase": "idle"}
    return None

# ══════════════════════════════════════════════════════
#  메인
# ══════════════════════════════════════════════════════
def run_check(send: bool = True, force: bool = False):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'='*56}")
    print(f"  TQQQ 박스전략 모니터 — {now}")
    print(f"{'='*56}")

    if not force and not is_market_hours():
        print("  미국 정규장 시간이 아닙니다 (스킵). --force로 강제 점검 가능.")
        return
    if not HAS_YF:
        print("  yfinance 필요: pip install yfinance")
        return

    state = load_state()
    msg = check_symbol(CFG["symbol"], state)
    save_state(state)

    if msg and send:
        send_telegram(msg)
        print("\n  신호 발송 완료")
    elif msg:
        print("\n  신호 (발송 생략 --check):")
        print("  " + msg.replace("<b>","").replace("</b>",""))
    else:
        print("\n  신호 없음")

if __name__ == "__main__":
    args = sys.argv[1:]
    if "--reset" in args:
        if os.path.exists(CFG["state_file"]):
            os.remove(CFG["state_file"])
        print("  상태 초기화 완료")
        sys.exit(0)
    run_check(send=("--check" not in args), force=("--force" in args))
