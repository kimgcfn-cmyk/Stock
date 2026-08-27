"""
kis_us_live_scanner.py — 미국주식 실전 운영 스캐너
====================================================
백테스트에서 검증된 전략을 실전에 적용합니다.
자동 주문은 없고, 모든 매수/매도는 사용자가 직접 실행합니다.

[기능 3가지]
  1. 신규 탐지 스캔  — 오늘 조건 맞는 종목 → 텔레그램 진입 추천 알림
  2. 보유 종목 모니터링 — 매일 MA/스윙저점 이탈 체크 → 청산 알림
  3. 포지션 관리    — positions.json 에 보유 종목 저장/관리

[전략]
  진입: 직전 20거래일 내 20%↑ 탐지
  청산: 강제손절 -10% | 스윙저점(20일) 이탈 | MA20 이탈 | MA50 이탈
  보유: 최대 60거래일

[사용법]
  python kis_us_live_scanner.py --scan       # 오늘 신규 탐지 스캔
  python kis_us_live_scanner.py --monitor    # 보유 종목 청산 체크
  python kis_us_live_scanner.py --add AAPL 182.50   # 보유 종목 직접 추가
  python kis_us_live_scanner.py --list       # 보유 종목 현황 출력
  python kis_us_live_scanner.py --remove AAPL       # 보유 종목 제거 (청산 완료 시)
  python kis_us_live_scanner.py --all        # 스캔 + 모니터링 한 번에 (매일 실행용)
"""

import requests, time, json, sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# ── 인증 정보 ────────────────────────────────────────
try:
    from config import (
        KIS_APP_KEY as APP_KEY, KIS_APP_SECRET as APP_SECRET,
        KIS_IS_VIRTUAL as IS_VIRTUAL,
        TELEGRAM_TOKEN_US as TELEGRAM_TOKEN, TELEGRAM_CHAT_ID_US as TELEGRAM_CHAT_ID,
        telegram_configured, kis_configured,
    )
except ImportError:
    print("⚠️  config.py 를 찾을 수 없습니다.")
    sys.exit(1)

BASE_URL = "https://openapivts.koreainvestment.com:29443" if IS_VIRTUAL \
           else "https://openapi.koreainvestment.com:9443"

# ══════════════════════════════════════════════════════
#  전략 설정 (백테스트와 동일하게 유지)
# ══════════════════════════════════════════════════════
CFG = {
    "lookback_days"     : 20,
    "price_surge_pct"   : 20.0,
    "min_price_usd"     : 5.0,
    "ma_short"          : 20,
    "ma_long"           : 50,
    "swing_lookback"    : 20,
    "hard_stop_pct"     : 10.0,
    "max_hold_days"     : 60,
    "commission_pct"    : 0.25,
    "slippage_pct"      : 0.10,
    "api_delay"         : 0.15,
}

POSITIONS_FILE = Path("positions.json")

SEP = "─" * 60

# ══════════════════════════════════════════════════════
#  텔레그램
# ══════════════════════════════════════════════════════
def send_telegram(text: str) -> bool:
    if not telegram_configured("us"):
        print(f"\n[텔레그램 미설정 — 콘솔 출력]\n{text}\n")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text, "parse_mode": "HTML",
        }, timeout=10)
        return r.json().get("ok", False)
    except Exception as e:
        print(f"  ❌ 텔레그램 오류: {e}")
        return False

# ══════════════════════════════════════════════════════
#  KIS API
# ══════════════════════════════════════════════════════
_token: Optional[str] = None
_token_at: Optional[datetime] = None

def get_token() -> Optional[str]:
    global _token, _token_at
    if _token and _token_at and (datetime.now() - _token_at).total_seconds() < 23*3600:
        return _token
    url = f"{BASE_URL}/oauth2/tokenP"
    try:
        r = requests.post(url, json={
            "grant_type": "client_credentials",
            "appkey": APP_KEY, "appsecret": APP_SECRET,
        }, timeout=10)
        t = r.json().get("access_token")
        if t:
            _token, _token_at = t, datetime.now()
            return t
    except Exception as e:
        print(f"  ❌ 토큰 오류: {e}")
    return None

def _headers(tr_id: str = "") -> Optional[dict]:
    t = get_token()
    if not t:
        return None
    h = {"Content-Type": "application/json",
         "Authorization": f"Bearer {t}",
         "appkey": APP_KEY, "appsecret": APP_SECRET}
    if tr_id:
        h["tr_id"] = tr_id
    return h

def fetch_daily(symbol: str, excd: str, bymd: str = "", limit: int = 100) -> list:
    h = _headers("HHDFS76240000")
    if not h:
        return []
    url = f"{BASE_URL}/uapi/overseas-price/v1/quotations/dailyprice"
    try:
        r = requests.get(url, headers=h, params={
            "AUTH": "", "EXCD": excd, "SYMB": symbol,
            "GUBN": "0", "BYMD": bymd, "MODP": "1",
        }, timeout=10)
        data = r.json()
        if data.get("rt_cd") == "0":
            return data.get("output2", [])
    except Exception:
        pass
    return []

def detect_excd(symbol: str) -> str:
    for excd in ["NAS", "NYS", "AMS"]:
        if fetch_daily(symbol, excd, limit=1):
            return excd
        time.sleep(CFG["api_delay"])
    return ""

def fetch_candles(symbol: str, excd: str, n_days: int = 120) -> list:
    """최근 n_days 거래일치 일봉 수집 (페이징)"""
    target = n_days + 10
    all_c, seen, base = [], set(), ""
    max_calls = (target // 100) + 5
    empty_retry = 0

    for _ in range(max_calls):
        page = fetch_daily(symbol, excd, base)
        if not page:
            empty_retry += 1
            if empty_retry > 5:
                break
            if base:
                dt = datetime.strptime(base, "%Y%m%d") - timedelta(days=7)
                base = dt.strftime("%Y%m%d")
            time.sleep(CFG["api_delay"])
            continue
        empty_retry = 0
        added = 0
        for c in page:
            d = c.get("xymd", "")
            if d and d not in seen:
                seen.add(d); all_c.append(c); added += 1
        if added == 0:
            break
        oldest = min(c.get("xymd", "99999999") for c in page)
        try:
            base = (datetime.strptime(oldest, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
        except ValueError:
            break
        time.sleep(CFG["api_delay"])
        if len(all_c) >= target:
            break

    return sorted(all_c, key=lambda c: c.get("xymd", ""))

# ══════════════════════════════════════════════════════
#  기술적 지표
# ══════════════════════════════════════════════════════
def calc_ma(candles: list, period: int) -> Optional[float]:
    closes = []
    for c in candles[-period:]:
        try:
            closes.append(float(c.get("clos", 0)))
        except (TypeError, ValueError):
            pass
    if len(closes) < period:
        return None
    return sum(closes) / len(closes)

def calc_swing_low(candles: list, period: int) -> Optional[float]:
    lows = []
    for c in candles[-period:]:
        try:
            lows.append(float(c.get("low", 0)))
        except (TypeError, ValueError):
            pass
    return min(lows) if lows else None

def get_latest(candles: list) -> dict:
    if not candles:
        return {}
    c = candles[-1]
    try:
        return {
            "date":  c.get("xymd", ""),
            "close": float(c.get("clos", 0)),
            "high":  float(c.get("high", 0)),
            "low":   float(c.get("low", 0)),
        }
    except (TypeError, ValueError):
        return {}

# ══════════════════════════════════════════════════════
#  포지션 관리 (positions.json)
# ══════════════════════════════════════════════════════
def load_positions() -> list[dict]:
    if not POSITIONS_FILE.exists():
        return []
    try:
        return json.loads(POSITIONS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []

def save_positions(positions: list[dict]):
    POSITIONS_FILE.write_text(
        json.dumps(positions, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

def add_position(symbol: str, entry_price: float, excd: str = ""):
    positions = load_positions()
    for p in positions:
        if p["symbol"] == symbol:
            print(f"  ⚠️  {symbol} 이미 보유 중입니다.")
            return
    if not excd:
        excd = detect_excd(symbol)
    pos = {
        "symbol": symbol,
        "excd": excd,
        "entry_price": entry_price,
        "entry_date": datetime.now().strftime("%Y%m%d"),
        "added_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    positions.append(pos)
    save_positions(positions)
    print(f"  ✅  {symbol} 추가됨 (진입가: ${entry_price:.2f})")

def remove_position(symbol: str):
    positions = load_positions()
    before = len(positions)
    positions = [p for p in positions if p["symbol"] != symbol]
    if len(positions) == before:
        print(f"  ⚠️  {symbol} 을 보유 목록에서 찾을 수 없습니다.")
        return
    save_positions(positions)
    print(f"  ✅  {symbol} 제거됨")

def list_positions():
    positions = load_positions()
    if not positions:
        print("  보유 종목 없음")
        return
    print(f"\n  {'종목':<8} | {'진입가':>10} | {'진입일':<10} | {'추가시각'}")
    print(f"  {'─'*8}-+-{'─'*10}-+-{'─'*10}-+-{'─'*16}")
    for p in positions:
        print(f"  {p['symbol']:<8} | ${p['entry_price']:>9.2f} | {p['entry_date']:<10} | {p['added_at']}")

# ══════════════════════════════════════════════════════
#  1. 신규 탐지 스캔
# ══════════════════════════════════════════════════════
def fetch_sp500_symbols() -> list[dict]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
    }
    # GitHub CSV 폴백 (캐시 우선)
    cache = Path("sp500_symbols_cache.json")
    try:
        import pandas as pd
        url = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
        import io
        df = pd.read_csv(io.StringIO(r.text))
        sym_col = next((c for c in df.columns if "symbol" in c.lower()), df.columns[0])
        name_col = next((c for c in df.columns if "name" in c.lower()), None)
        symbols = [{"symbol": str(row[sym_col]).strip().replace(".", "-"),
                    "name": str(row[name_col]).strip() if name_col else str(row[sym_col]).strip()}
                   for _, row in df.iterrows()]
        cache.write_text(json.dumps(symbols, ensure_ascii=False), encoding="utf-8")
        return symbols
    except Exception:
        if cache.exists():
            return json.loads(cache.read_text(encoding="utf-8"))
        return []

def scan_new_signals(limit: Optional[int] = None) -> list[dict]:
    """오늘 조건에 맞는 종목 탐지"""
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    print(f"\n{SEP}")
    print(f"  📡  신규 탐지 스캔 — {now}")
    print(SEP)

    symbols = fetch_sp500_symbols()
    if limit:
        symbols = symbols[:limit]
    print(f"  S&P500 {len(symbols)}개 종목 스캔 중...")

    send_telegram(
        f"📡 <b>미국주식 신규 탐지 스캔 시작</b>\n"
        f"{now}\n"
        f"S&P500 {len(symbols)}개 종목\n"
        f"탐지조건: {CFG['lookback_days']}거래일 내 {CFG['price_surge_pct']}%↑"
    )

    existing = {p["symbol"] for p in load_positions()}
    W = CFG["lookback_days"]
    signals = []

    for i, s in enumerate(symbols, 1):
        sym = s["symbol"]
        if sym in existing:
            continue  # 이미 보유 중이면 스킵

        try:
            excd = detect_excd(sym)
            if not excd:
                continue
            candles = fetch_candles(sym, excd, n_days=W + 5)
            if len(candles) < W + 1:
                continue

            window = candles[-W-1:-1]   # 직전 20일 (오늘 제외)
            today  = candles[-1]

            try:
                p_ago   = float(window[0].get("clos", 0))
                p_today = float(today.get("clos", 0))
            except (TypeError, ValueError):
                continue
            if p_ago <= 0 or p_today < CFG["min_price_usd"]:
                continue

            chg = (p_today - p_ago) / p_ago * 100
            if chg < CFG["price_surge_pct"]:
                continue

            # MA 확인 (추세 방향 체크)
            ma20 = calc_ma(candles[:-1], CFG["ma_short"])
            ma50 = calc_ma(candles[:-1], CFG["ma_long"])

            sig = {
                "symbol": sym, "name": s["name"], "excd": excd,
                "price": round(p_today, 2), "change_pct": round(chg, 2),
                "ma20": round(ma20, 2) if ma20 else None,
                "ma50": round(ma50, 2) if ma50 else None,
                "above_ma20": (p_today > ma20) if ma20 else None,
                "above_ma50": (p_today > ma50) if ma50 else None,
            }
            signals.append(sig)
            print(f"  🚨 탐지: {sym:<6} +{chg:.1f}%  현재가 ${p_today:.2f}")

        except Exception:
            pass

        if i % 100 == 0:
            print(f"  진행 {i}/{len(symbols)}")

    print(f"\n  스캔 완료: {len(signals)}건 탐지")

    # ── 텔레그램 전송 ──────────────────────────────────
    if signals:
        for sig in signals:
            ma_str = ""
            if sig["ma20"]:
                ma_str += f"MA20: ${sig['ma20']:.2f} ({'위 ✅' if sig['above_ma20'] else '아래 ⚠️'})\n"
            if sig["ma50"]:
                ma_str += f"MA50: ${sig['ma50']:.2f} ({'위 ✅' if sig['above_ma50'] else '아래 ⚠️'})"

            # 두 MA 모두 아래면 추가 주의 경고
            caution = ""
            if sig.get("above_ma20") is False and sig.get("above_ma50") is False:
                caution = "\n⚠️ MA20·MA50 모두 하회 중 — 진입 신중히 검토하세요"

            msg = (
                f"🚨 <b>진입 추천 — {sig['symbol']}</b>\n"
                f"{sig['name'][:30]}\n"
                f"{datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"현재가: <b>${sig['price']:.2f}</b>\n"
                f"20거래일 상승: <b>+{sig['change_pct']:.1f}%</b>\n"
                f"{ma_str}"
                f"{caution}\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"강제손절: ${sig['price'] * (1 - CFG['hard_stop_pct']/100):.2f} (-{CFG['hard_stop_pct']:.0f}%)\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"⚠️ 자동 주문 아님 — 직접 매수 후 아래 명령어로 등록:\n"
                f"python kis_us_live_scanner.py --add {sig['symbol']} 매수가"
            )
            send_telegram(msg)
    else:
        send_telegram(
            f"🔍 신규 탐지 없음\n"
            f"{datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
            f"S&P500 {len(symbols)}개 종목 스캔 완료"
        )

    return signals

# ══════════════════════════════════════════════════════
#  2. 보유 종목 모니터링 (청산 신호 체크)
# ══════════════════════════════════════════════════════
def monitor_positions():
    """보유 종목별 청산 조건 체크 후 텔레그램 알림"""
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    positions = load_positions()

    print(f"\n{SEP}")
    print(f"  🔍  보유 종목 청산 체크 — {now}")

    if not positions:
        print("  보유 종목 없음 — 모니터링 스킵")
        send_telegram(
            f"🔍 <b>미국주식 보유종목 점검 시작</b>\n"
            f"{now}\n"
            f"현재 보유 종목 없음"
        )
        return

    send_telegram(
        f"🔍 <b>미국주식 보유종목 점검 시작</b>\n"
        f"{now}\n"
        f"보유 종목 {len(positions)}개 청산 조건 체크 중\n"
        f"(MA20/MA50/스윙저점/강제손절)"
    )

    print(f"  {len(positions)}개 종목 모니터링 중...")
    print(SEP)

    hold_ok, exit_signals = [], []

    for p in positions:
        sym          = p["symbol"]
        excd         = p.get("excd", "")
        entry_price  = p["entry_price"]
        entry_date   = p.get("entry_date", "")

        # 보유 기간 계산
        hold_days = 0
        try:
            hold_days = (datetime.now() - datetime.strptime(entry_date, "%Y%m%d")).days
        except (ValueError, TypeError):
            pass

        try:
            if not excd:
                excd = detect_excd(sym)
                p["excd"] = excd
            candles = fetch_candles(sym, excd, n_days=CFG["ma_long"] + CFG["swing_lookback"] + 10)
            if not candles:
                print(f"  ⚠️  {sym}: 데이터 없음")
                hold_ok.append(p)
                continue

            latest = get_latest(candles)
            close  = latest.get("close", 0)
            if close <= 0:
                hold_ok.append(p)
                continue

            # 수익률 계산 (수수료 포함)
            gross_ret = (close - entry_price) / entry_price * 100
            cost      = (CFG["commission_pct"] + CFG["slippage_pct"]) * 2
            net_ret   = gross_ret - cost

            # ── 청산 조건 체크 ────────────────────────────
            exit_reason = None

            # ① 강제 손절 -10%
            if gross_ret <= -CFG["hard_stop_pct"]:
                exit_reason = "hard_sl"

            # ② 스윙 저점 이탈 (직전 20일 최저가, 매일 갱신)
            elif (sw := calc_swing_low(candles[:-1], CFG["swing_lookback"])) and close < sw:
                exit_reason = "swing_sl"

            # ③ MA20 이탈
            elif (ma20 := calc_ma(candles[:-1], CFG["ma_short"])) and close < ma20:
                exit_reason = "ma_sl"

            # ④ MA50 이탈
            elif (ma50 := calc_ma(candles[:-1], CFG["ma_long"])) and close < ma50:
                exit_reason = "ma_exit"

            # ⑤ 타임아웃 (60거래일 ≈ 84일)
            elif hold_days >= CFG["max_hold_days"] * 1.4:
                exit_reason = "timeout"

            reason_label = {
                "hard_sl":  "🔴 강제손절 (-10% 도달)",
                "swing_sl": "🟠 스윙저점 이탈",
                "ma_sl":    "🟡 MA20 이탈 (손절)",
                "ma_exit":  "🟢 MA50 이탈 (추세청산)",
                "timeout":  "⏰ 최대보유일 초과",
            }

            if exit_reason:
                exit_signals.append({**p, "close": close, "net_ret": net_ret,
                                       "exit_reason": exit_reason, "hold_days": hold_days})
                label = reason_label.get(exit_reason, exit_reason)
                print(f"  {label}: {sym}  현재가 ${close:.2f}  수익 {net_ret:+.1f}%")

                msg = (
                    f"{'🔴' if 'sl' in exit_reason else '🟢'} <b>청산 신호 — {sym}</b>\n"
                    f"{datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
                    f"━━━━━━━━━━━━━━━━━━━\n"
                    f"사유: <b>{label}</b>\n"
                    f"진입가: ${entry_price:.2f}  →  현재가: ${close:.2f}\n"
                    f"수익률(수수료포함): <b>{net_ret:+.1f}%</b>\n"
                    f"보유기간: {hold_days}일\n"
                    f"━━━━━━━━━━━━━━━━━━━\n"
                    f"✅ 매도 후 아래 명령어로 포지션 제거:\n"
                    f"python kis_us_live_scanner.py --remove {sym}"
                )
                send_telegram(msg)
            else:
                # 청산 조건 미해당 — 현황만 출력
                ma20 = calc_ma(candles[:-1], CFG["ma_short"])
                ma50 = calc_ma(candles[:-1], CFG["ma_long"])
                sw   = calc_swing_low(candles[:-1], CFG["swing_lookback"])
                print(f"  ✅  {sym:<6} 보유중  현재 ${close:.2f}  "
                      f"수익 {net_ret:+.1f}%  보유 {hold_days}일  "
                      f"MA20 ${ma20:.2f if ma20 else '?'}  "
                      f"스윙저점 ${sw:.2f if sw else '?'}")
                hold_ok.append(p)

        except Exception as e:
            print(f"  ⚠️  {sym} 오류: {e}")
            hold_ok.append(p)

        time.sleep(CFG["api_delay"] * 2)

    # 청산 신호 없으면 요약 전송
    if not exit_signals and positions:
        lines = []
        for p in positions:
            lines.append(f"  {p['symbol']} — 보유중")
        send_telegram(
            f"✅ <b>청산 신호 없음</b>\n"
            f"{datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
            f"보유 {len(positions)}개 종목 모두 청산 조건 미해당"
        )

    save_positions(hold_ok + [p for p in positions if p not in hold_ok
                               and p not in [e for e in exit_signals]])

# ══════════════════════════════════════════════════════
#  실행
# ══════════════════════════════════════════════════════
if __name__ == "__main__":
    args = sys.argv[1:]

    if not kis_configured():
        print("❌ KIS API 미설정 — .env 파일의 KIS_APP_KEY, KIS_APP_SECRET 을 입력하세요")
        sys.exit(1)

    if not get_token():
        print("❌ 토큰 발급 실패")
        sys.exit(1)

    if "--add" in args:
        idx = args.index("--add")
        try:
            sym   = args[idx+1].upper()
            price = float(args[idx+2])
            add_position(sym, price)
        except (IndexError, ValueError):
            print("사용법: --add 종목코드 진입가   예) --add AAPL 182.50")

    elif "--remove" in args:
        idx = args.index("--remove")
        try:
            remove_position(args[idx+1].upper())
        except IndexError:
            print("사용법: --remove 종목코드   예) --remove AAPL")

    elif "--list" in args:
        list_positions()

    elif "--monitor" in args:
        monitor_positions()

    elif "--scan" in args:
        limit = None
        if "--limit" in args:
            try: limit = int(args[args.index("--limit")+1])
            except: pass
        scan_new_signals(limit=limit)

    elif "--all" in args:
        # 매일 실행용 — 스캔 + 모니터링
        print(f"📅 {datetime.now().strftime('%Y-%m-%d')} 일일 점검 시작")
        monitor_positions()
        scan_new_signals()

    else:
        print(__doc__)
