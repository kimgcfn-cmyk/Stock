"""
Bitget 단기 급등 알림 봇 — 디스코드 신호 전용 (자동매매 없음)
================================================================
[중요] 이 스크립트는 신호 알림만 합니다. 실제 주문은 절대 자동으로 넣지 않습니다.
       디스코드로 "OOO 진입 추천, 손절가 X, 목표가 Y"를 받으면
       사용자가 직접 거래소에서 수동으로 주문을 넣어야 합니다.

[2026-07-30 변경] 알림 채널: 텔레그램 → 디스코드로 이전
  이유: coin_range_monitor.py(RSI 범위매매)와 알림 채널을 구분하기 위해
       이 급등스캐너는 별도 디스코드 웹훅(전용 채널)을 사용한다.

[전략 근거] — 로직은 변경 없음, 알림 채널만 교체
  - 4시간봉 기준 12시간 내 30%+ 상승, 거래량 2배+ 폭증 탐지
  - 진입 시점 직전 5분봉(30분치)으로 가격/거래량 가속도 계산
    → 가속 중이면 롱, 아니면 숏 (사후 정보 없이 진입 시점에 즉시 결정)
  - 손절: 탐지구간 고점/저점 ± 5%, 손익비 1:5

[설정 방법]
  1. 디스코드에서 급등스캐너 전용 채널 생성
  2. 채널 설정 → 연동(Integrations) → 웹후크 → 새 웹후크 생성 → URL 복사
  3. .env에 DISCORD_WEBHOOK_URL_ALERT=복사한URL 추가
  4. config.py에 DISCORD_WEBHOOK_URL_ALERT = _get("DISCORD_WEBHOOK_URL_ALERT") 추가

실행:
  python telegram_alert_scanner.py              # 반복 스캔 (기본 5분 간격)
  python telegram_alert_scanner.py --once        # 1회만 스캔
  python telegram_alert_scanner.py --test TACUSDT  # 단일 종목 테스트 (디스코드 전송 포함)
  python telegram_alert_scanner.py --notify-test   # 디스코드 연결 테스트 메시지만 전송
"""

import requests, time, sys, re
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from typing import Optional

# ══════════════════════════════════════════════════════
#  인증 정보 — .env / config.py 에서 자동으로 불러옵니다
#  수정이 필요하면 이 파일이 아닌 .env 파일을 수정하세요
# ══════════════════════════════════════════════════════
try:
    import config as _cfg
    DISCORD_WEBHOOK_URL_ALERT = _cfg._get("DISCORD_WEBHOOK_URL_ALERT") if hasattr(_cfg, "_get") else \
                               getattr(_cfg, "DISCORD_WEBHOOK_URL_ALERT", "")
except ImportError:
    print("⚠️  config.py 를 찾을 수 없습니다. 같은 폴더에 config.py 와 .env 파일이 있는지 확인하세요.")
    DISCORD_WEBHOOK_URL_ALERT = ""

# ══════════════════════════════════════════════════════
#  전략 설정 (백테스트로 검증된 값 그대로 사용 — 변경 없음)
# ══════════════════════════════════════════════════════
H4 = 4 * 3_600_000
M5 = 5 * 60_000
H1 = 3_600_000

CFG = {
    "price_surge_pct"  : 30.0,
    "volume_mult_min"  : 2.0,
    "surge_periods"    : 3,
    "baseline_periods" : 6,
    "min_vol_usdt"     : 5_000,

    "accel_lookback_candles": 3,
    "accel_price_weight"    : 0.5,
    "accel_vol_weight"      : 0.5,
    "accel_score_threshold" : 1.0,

    "sl_buffer_pct" : 5.0,
    "rr_ratio"      : 5,
    "use_wick_sl"   : True,

    "dedup_minutes"     : 240,
    "scan_interval_sec" : 300,
    "api_delay"         : 0.08,

    "notify_on_bot_start" : True,
    "notify_on_scan_start": True,
    "notify_scan_start_interval_sec": 43200,
    "notify_on_scan_end"  : False,
}

BASE    = "https://api.bitget.com"
HEADERS = {"Content-Type": "application/json"}
_seen: dict[str, datetime] = {}
_last_scan_start_notify: Optional[datetime] = None

# ══════════════════════════════════════════════════════
#  디스코드 전송 (텔레그램 대체)
# ══════════════════════════════════════════════════════
def _html_to_discord_markdown(text: str) -> str:
    text = re.sub(r"<b>(.*?)</b>", r"**\1**", text, flags=re.DOTALL)
    text = re.sub(r"<i>(.*?)</i>", r"*\1*", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", "", text)
    return text

def send_telegram(text: str) -> bool:
    """이름은 하위호환을 위해 유지하지만 실제로는 디스코드로 전송한다."""
    if not DISCORD_WEBHOOK_URL_ALERT:
        print("  ⚠️  디스코드 웹훅 미설정 — .env 파일의 DISCORD_WEBHOOK_URL_ALERT 를 확인하세요.")
        print(f"\n[디스코드 전송 예정 내용]\n{text}\n")
        return False
    content = _html_to_discord_markdown(text)
    if len(content) > 1900:
        content = content[:1900] + "\n…(생략)"
    try:
        r = requests.post(DISCORD_WEBHOOK_URL_ALERT, json={"content": content}, timeout=10)
        ok = r.status_code in (200, 204)
        if not ok:
            print(f"  ❌ 디스코드 전송 실패: {r.status_code} {r.text[:200]}")
        return ok
    except Exception as e:
        print(f"  ❌ 디스코드 전송 예외: {e}")
        return False

def test_telegram_connection():
    msg = (
        "🔔 <b>디스코드 연동 테스트</b>\n\n"
        "이 메시지가 보이면 봇 연결이 정상입니다.\n"
        f"테스트 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    ok = send_telegram(msg)
    print("✅ 전송 성공" if ok else "❌ 전송 실패 — 웹훅 URL을 다시 확인하세요")

# ══════════════════════════════════════════════════════
#  데이터 구조
# ══════════════════════════════════════════════════════
@dataclass
class Signal:
    symbol      : str
    price_entry : float
    change_pct  : float
    vol_mult    : float
    high_period : float
    low_period  : float
    direction   : str
    accel_score : float
    accel_available: bool
    sl_price    : float = 0.0
    tp_price    : float = 0.0
    ts          : str = ""

    def direction_label(self) -> str:
        return "롱 (상승 베팅)" if self.direction == "long" else "숏 (하락 베팅)"

# ══════════════════════════════════════════════════════
#  API
# ══════════════════════════════════════════════════════
def _get(url, params=None):
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=15)
        d = r.json()
        if d.get("code") == "00000":
            return d.get("data") or []
    except Exception:
        pass
    return []

def fetch_futures_symbols() -> list[str]:
    data = _get(f"{BASE}/api/v2/mix/market/contracts", {"productType": "usdt-futures"})
    return [d["symbol"] for d in data
            if d.get("symbol") and d.get("symbolStatus") == "normal"]

def fetch_candles_4h(symbol: str, limit: int = 25) -> list:
    raw = _get(f"{BASE}/api/v2/mix/market/candles", {
        "symbol": symbol, "granularity": "4H",
        "productType": "usdt-futures", "limit": str(limit),
    })
    if not raw:
        return []
    try:
        return sorted(raw, key=lambda c: int(c[0]))
    except (IndexError, ValueError):
        return raw

def fetch_candles_5m(symbol: str, limit: int = 10) -> list:
    raw = _get(f"{BASE}/api/v2/mix/market/candles", {
        "symbol": symbol, "granularity": "5m",
        "productType": "usdt-futures", "limit": str(limit),
    })
    if not raw:
        return []
    try:
        return sorted(raw, key=lambda c: int(c[0]))
    except (IndexError, ValueError):
        return raw

# ══════════════════════════════════════════════════════
#  5분봉 선행 가속도
# ══════════════════════════════════════════════════════
def compute_lead_accel(symbol: str) -> dict:
    n = CFG["accel_lookback_candles"]
    candles = fetch_candles_5m(symbol, limit=2*n + 2)
    if len(candles) < 2 * n:
        return {"available": False, "score": 0.0}

    older = candles[-2*n:-n]
    newer = candles[-n:]

    try:
        older_open, older_close = float(older[0][1]), float(older[-1][4])
        newer_open, newer_close = float(newer[0][1]), float(newer[-1][4])
        older_chg = (older_close - older_open) / older_open * 100 if older_open > 0 else 0
        newer_chg = (newer_close - newer_open) / newer_open * 100 if newer_open > 0 else 0
        price_accel = newer_chg - older_chg

        older_vol = sum(float(c[6]) for c in older if len(c) > 6)
        newer_vol = sum(float(c[6]) for c in newer if len(c) > 6)
        vol_ratio = (newer_vol / older_vol) if older_vol > 0 else 0
        vol_accel = (vol_ratio - 1.0) * 10

        score = CFG["accel_price_weight"] * (price_accel/5.0) + CFG["accel_vol_weight"] * (vol_accel/10.0)
        return {"available": True, "score": round(score, 2)}
    except (IndexError, ValueError, ZeroDivisionError):
        return {"available": False, "score": 0.0}

# ══════════════════════════════════════════════════════
#  손절/익절 계산
# ══════════════════════════════════════════════════════
def compute_sl_tp(price_entry: float, high_p: float, low_p: float, direction: str) -> tuple[float, float]:
    buf, rr = CFG["sl_buffer_pct"] / 100, CFG["rr_ratio"]
    if direction == "short":
        sl = (high_p * (1 + buf)) if (CFG["use_wick_sl"] and high_p > price_entry) else price_entry * (1 + buf)
        risk = sl - price_entry
        return round(sl, 8), round(price_entry - risk * rr, 8)
    else:
        sl = (low_p * (1 - buf)) if (CFG["use_wick_sl"] and 0 < low_p < price_entry) else price_entry * (1 - buf)
        risk = price_entry - sl
        return round(sl, 8), round(price_entry + risk * rr, 8)

# ══════════════════════════════════════════════════════
#  탐지 분석
# ══════════════════════════════════════════════════════
def analyze_symbol(symbol: str) -> Optional[Signal]:
    W, B = CFG["surge_periods"], CFG["baseline_periods"]
    candles = fetch_candles_4h(symbol, limit=W + B + 3)
    if len(candles) < W + B:
        return None

    recent, baseline = candles[-W:], candles[-(W+B):-W]
    try:
        p_open = float(recent[0][1])
        p_now  = float(recent[-1][4])
        high_p = max(float(c[2]) for c in recent)
        low_p  = min(float(c[3]) for c in recent)
    except Exception:
        return None
    if p_open <= 0 or p_now <= 0:
        return None

    chg = (p_now - p_open) / p_open * 100
    if chg < CFG["price_surge_pct"]:
        return None

    try:
        vol_r = sum(float(c[6]) for c in recent   if len(c) > 6)
        vol_b = [float(c[6])    for c in baseline if len(c) > 6]
    except Exception:
        return None
    if not vol_b or vol_r < CFG["min_vol_usdt"]:
        return None

    vol_avg = (sum(vol_b) / len(vol_b)) * W
    if vol_avg <= 0:
        return None
    vol_mult = vol_r / vol_avg
    if vol_mult < CFG["volume_mult_min"]:
        return None

    accel = compute_lead_accel(symbol)
    direction = "long" if (accel["available"] and accel["score"] >= CFG["accel_score_threshold"]) else "short"

    sl, tp = compute_sl_tp(p_now, high_p, low_p, direction)

    return Signal(
        symbol=symbol, price_entry=round(p_now, 8), change_pct=round(chg, 2),
        vol_mult=round(vol_mult, 2), high_period=round(high_p, 8), low_period=round(low_p, 8),
        direction=direction, accel_score=accel["score"], accel_available=accel["available"],
        sl_price=sl, tp_price=tp,
        ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )

# ══════════════════════════════════════════════════════
#  중복 알림 방지
# ══════════════════════════════════════════════════════
def dedup_check(symbol: str) -> bool:
    last = _seen.get(symbol)
    if last is None:
        return False
    return (datetime.now() - last).total_seconds() / 60 < CFG["dedup_minutes"]

def mark_seen(symbol: str):
    _seen[symbol] = datetime.now()

# ══════════════════════════════════════════════════════
#  메시지 포맷
# ══════════════════════════════════════════════════════
def format_entry_message(s: Signal) -> str:
    risk_pct = abs(s.sl_price - s.price_entry) / s.price_entry * 100
    accel_note = f"{s.accel_score:+.2f}" if s.accel_available else "데이터 부족"

    return (
        f"🚨 <b>급등 탐지 — {s.symbol}</b>\n"
        f"{s.ts}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"방향: <b>{s.direction_label()}</b>\n"
        f"진입가: {s.price_entry:,.8f}\n"
        f"12h 상승률: +{s.change_pct:.1f}%\n"
        f"거래량 배수: {s.vol_mult:.1f}x\n"
        f"선행 가속도 점수: {accel_note}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📍 손절가: {s.sl_price:,.8f}\n"
        f"🎯 목표가: {s.tp_price:,.8f}\n"
        f"리스크: {risk_pct:.2f}%  (손익비 1:{CFG['rr_ratio']})\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ 자동 주문 아님 — 직접 거래소에서 진입하세요\n"
        f"⚠️ 표본 부족 전략, 반드시 소액으로 검증하세요"
    )

# ══════════════════════════════════════════════════════
#  스캔 루프
# ══════════════════════════════════════════════════════
def should_notify_scan_start() -> bool:
    global _last_scan_start_notify
    if not CFG["notify_on_scan_start"]:
        return False
    now = datetime.now()
    if _last_scan_start_notify is None:
        return True
    elapsed = (now - _last_scan_start_notify).total_seconds()
    return elapsed >= CFG["notify_scan_start_interval_sec"]

def scan_once() -> list[Signal]:
    global _last_scan_start_notify
    start_time = datetime.now()
    print(f"\n[{start_time.strftime('%H:%M:%S')}] 스캔 시작...")

    if should_notify_scan_start():
        send_telegram(
            f"🔍 <b>스캔 진행중</b>\n"
            f"{start_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"조건: 12h {CFG['price_surge_pct']}%↑ & 거래량 {CFG['volume_mult_min']}x↑\n"
            f"(12시간 간격으로 알립니다)"
        )
        _last_scan_start_notify = start_time

    symbols = fetch_futures_symbols()
    print(f"  선물 거래 가능 심볼 {len(symbols)}개 스캔 중...")

    signals = []
    for i, sym in enumerate(symbols, 1):
        if dedup_check(sym):
            continue
        try:
            sig = analyze_symbol(sym)
            if sig:
                signals.append(sig)
                mark_seen(sym)
                print(f"  🚨 탐지: {sym} → {sig.direction_label()}")
                msg = format_entry_message(sig)
                send_telegram(msg)
        except Exception as e:
            pass
        time.sleep(CFG["api_delay"])
        if i % 200 == 0:
            print(f"  진행 {i}/{len(symbols)}")

    elapsed = (datetime.now() - start_time).total_seconds()
    print(f"  스캔 완료: 탐지 {len(signals)}건  ({elapsed:.0f}초 소요)")

    if CFG["notify_on_scan_end"]:
        send_telegram(
            f"✅ <b>스캔 완료</b>\n"
            f"심볼 {len(symbols)}개 확인  |  탐지 {len(signals)}건  |  {elapsed:.0f}초 소요\n"
            f"다음 스캔까지 {CFG['scan_interval_sec']}초"
        )

    return signals

def run(once: bool = False):
    print("="*60)
    print("  Bitget 급등 알림 봇 (디스코드 전용, 자동매매 없음)")
    print(f"  조건: 12h {CFG['price_surge_pct']}%↑ & 거래량 {CFG['volume_mult_min']}x↑")
    print("="*60)

    if CFG["notify_on_bot_start"]:
        mode = "1회 스캔 모드" if once else f"반복 모니터링 모드 ({CFG['scan_interval_sec']}초 간격)"
        send_telegram(
            f"🟢 <b>봇 가동 시작</b>\n"
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"모드: {mode}\n"
            f"탐지조건: 12h {CFG['price_surge_pct']}%↑ & 거래량 {CFG['volume_mult_min']}x↑\n"
            f"손익비: 1:{CFG['rr_ratio']}\n\n"
            f"⚠️ 신호 알림 전용입니다. 자동 주문은 실행되지 않습니다."
        )

    while True:
        try:
            scan_once()
        except Exception as e:
            print(f"  ⚠️  스캔 중 오류: {e}")
            send_telegram(f"⚠️ <b>스캔 오류 발생</b>\n{str(e)[:200]}")

        if once:
            break
        print(f"  {CFG['scan_interval_sec']}초 후 재스캔...")
        time.sleep(CFG["scan_interval_sec"])

# ══════════════════════════════════════════════════════
#  단일 종목 테스트
# ══════════════════════════════════════════════════════
def test_symbol(symbol: str):
    print(f"단일 종목 테스트: {symbol}")
    sig = analyze_symbol(symbol)
    if sig:
        print(f"✅ 탐지됨 → {sig.direction_label()}")
        msg = format_entry_message(sig)
        print(msg)
        send_telegram(msg)
    else:
        print("❌ 조건 미달 — 탐지되지 않음")

if __name__ == "__main__":
    args = sys.argv[1:]
    if "--notify-test" in args:
        test_telegram_connection()
    elif "--test" in args:
        idx = args.index("--test")
        sym = args[idx+1].upper() if idx+1 < len(args) else "BTCUSDT"
        test_symbol(sym)
    elif "--once" in args:
        run(once=True)
    else:
        try:
            run(once=False)
        except KeyboardInterrupt:
            print("\n[종료]")
            sys.exit(0)
