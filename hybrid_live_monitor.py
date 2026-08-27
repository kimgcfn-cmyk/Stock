"""
하이브리드 동적자산배분 — 실전 모니터링 (알림 전용)
======================================================
[중요] 신호 알림만 합니다. 자동 주문은 절대 넣지 않습니다.
       텔레그램으로 "오늘 목표 비중 / 리밸런싱 필요 여부 / 급락 경고"를
       받으면, 사용자가 직접 거래소에서 수동으로 조정합니다.

[하는 일]
  1) 매일 QQQ 지수로 카나리아 모멘텀 계산
  2) 오늘의 TQQQ 목표 비중 산출 (0~38%, 카나리아 연동)
  3) 실제 보유 비중(holdings.json)과 비교 → 리밸런싱 필요 시 알림
  4) TQQQ가 최근 고점 대비 급락(-15%)이면 즉시 방어 경고

[보유 상황 입력] — holdings.json (없으면 목표 비중만 안내)
  {
    "tqqq_value": 15000,     # 현재 TQQQ 평가액 (USD)
    "bond_value": 25000,     # 현재 채권/현금 평가액 (USD)
    "tqqq_peak":  18000      # TQQQ 최근 고점 (급락 감지용, 자동 갱신)
  }

실행:
  python hybrid_live_monitor.py               # 1회 점검 + 텔레그램
  python hybrid_live_monitor.py --check       # 텔레그램 없이 화면 출력만
"""

import requests, time, json, sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# ── 인증 (기존 config.py 재활용) ──────────────────────
try:
    from config import (
        KIS_APP_KEY as APP_KEY,
        KIS_APP_SECRET as APP_SECRET,
        KIS_IS_VIRTUAL as IS_VIRTUAL,
        TELEGRAM_TOKEN_US as TELEGRAM_TOKEN,
        TELEGRAM_CHAT_ID_US as TELEGRAM_CHAT_ID,
        telegram_configured,
    )
except ImportError:
    print("⚠️  config.py 없음 — .env 확인")
    APP_KEY = APP_SECRET = TELEGRAM_TOKEN = TELEGRAM_CHAT_ID = ""
    IS_VIRTUAL = True
    telegram_configured = lambda *a: False

BASE_URL = "https://openapivts.koreainvestment.com:29443" if IS_VIRTUAL \
           else "https://openapi.koreainvestment.com:9443"

# ── 전략 설정 (백테스트 최적값과 동일) ────────────────
CFG = {
    "attack_symbol"   : "TQQQ",   # 공격자산 (실전 매매 대상)
    "canary_index"    : "QQQ",    # 카나리아 판단 지수
    "tqqq_max_pct"    : 38.0,     # TQQQ 최대 비중
    "tqqq_min_pct"    : 0.0,
    "score_full_pct"  : 0.12,     # 모멘텀 12%면 최대 공격
    "canary_months"   : [21, 63, 126, 252],
    "rebalance_band"  : 5.0,      # 목표와 5%p 이상 벌어지면 리밸런싱 알림
    "crash_trigger_pct": 15.0,    # 고점 대비 -15%면 급락 경고
    "crash_derisk_to_pct": 12.0,
    "api_delay"       : 0.13,
    "holdings_file"   : "holdings.json",
}

HOLDINGS = Path(CFG["holdings_file"])

# ══════════════════════════════════════════════════════
#  텔레그램
# ══════════════════════════════════════════════════════
def send_telegram(text: str) -> bool:
    try:
        if not telegram_configured("us"):
            print("  ⚠️  텔레그램 미설정")
            print(f"\n[전송 예정 내용]\n{text}\n")
            return False
    except TypeError:
        if not telegram_configured():
            return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML",
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
    if _token and _token_at and (datetime.now()-_token_at).total_seconds() < 23*3600:
        return _token
    try:
        r = requests.post(f"{BASE_URL}/oauth2/tokenP", json={
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

def fetch_daily(symbol: str, days_target: int = 300) -> list:
    """해외 일봉 수집 (페이징)"""
    t = get_token()
    if not t:
        return []
    h = {"Content-Type": "application/json", "Authorization": f"Bearer {t}",
         "appkey": APP_KEY, "appsecret": APP_SECRET, "tr_id": "HHDFS76240000"}
    url = f"{BASE_URL}/uapi/overseas-price/v1/quotations/dailyprice"

    excd = ""
    for e in ["NAS", "NYS", "AMS"]:
        try:
            r = requests.get(url, headers=h, params={
                "AUTH": "", "EXCD": e, "SYMB": symbol,
                "GUBN": "0", "BYMD": "", "MODP": "1"}, timeout=10)
            if r.json().get("output2"):
                excd = e
                break
        except Exception:
            pass
        time.sleep(CFG["api_delay"])
    if not excd:
        return []

    all_c, seen, base = [], set(), ""
    for _ in range((days_target // 100) + 8):
        try:
            r = requests.get(url, headers=h, params={
                "AUTH": "", "EXCD": excd, "SYMB": symbol,
                "GUBN": "0", "BYMD": base, "MODP": "1"}, timeout=10)
            page = r.json().get("output2", [])
        except Exception:
            page = []
        if not page:
            break
        added = 0
        for c in page:
            d = c.get("xymd", "")
            if d and d not in seen:
                seen.add(d); all_c.append(c); added += 1
        if added == 0:
            break
        oldest = min(c.get("xymd", "99999999") for c in page)
        base = (datetime.strptime(oldest, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
        time.sleep(CFG["api_delay"])
        if len(all_c) >= days_target:
            break

    all_c.sort(key=lambda c: c.get("xymd", ""))
    return all_c

# ══════════════════════════════════════════════════════
#  카나리아 모멘텀 & 목표 비중
# ══════════════════════════════════════════════════════
def momentum_score(closes: list[float]) -> Optional[float]:
    """1,3,6,12개월 평균 수익률"""
    periods = CFG["canary_months"]
    if len(closes) < max(periods) + 1:
        # 데이터 부족 시 축소 모멘텀
        avail = [p for p in periods if len(closes) > p]
        if not avail:
            return None
        periods = avail
    now = closes[-1]
    rets = []
    for p in periods:
        past = closes[-1 - p]
        if past > 0:
            rets.append((now - past) / past)
    return sum(rets) / len(rets) if rets else None

def target_tqqq_pct(score: Optional[float]) -> float:
    """모멘텀 → TQQQ 목표 비중"""
    lo, hi = CFG["tqqq_min_pct"], CFG["tqqq_max_pct"]
    if score is None or score <= 0:
        return lo
    if score >= CFG["score_full_pct"]:
        return hi
    return lo + (hi - lo) * (score / CFG["score_full_pct"])

# ══════════════════════════════════════════════════════
#  보유 상황 로드/저장
# ══════════════════════════════════════════════════════
def load_holdings() -> Optional[dict]:
    if HOLDINGS.exists():
        try:
            return json.loads(HOLDINGS.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None

def save_holdings(h: dict):
    HOLDINGS.write_text(json.dumps(h, ensure_ascii=False, indent=2), encoding="utf-8")

# ══════════════════════════════════════════════════════
#  메인 점검 로직
# ══════════════════════════════════════════════════════
def run_check(send: bool = True):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"\n{'='*60}")
    print(f"  하이브리드 동적배분 실전 점검 — {now}")
    print(f"{'='*60}")

    if not get_token():
        print("  ❌ 토큰 발급 실패")
        return

    # 1) 카나리아 (QQQ 지수)
    qqq = fetch_daily(CFG["canary_index"], 300)
    if len(qqq) < 63:
        print("  ❌ QQQ 데이터 부족")
        return
    qqq_closes = [float(c.get("clos", 0)) for c in qqq if float(c.get("clos", 0)) > 0]
    score = momentum_score(qqq_closes)
    tgt_pct = target_tqqq_pct(score)

    defensive = (score is not None and score <= 0)
    mode = "🔴 방어 (채권 100%)" if defensive else (
           "🟢 공격" if tgt_pct >= CFG["tqqq_max_pct"] - 1 else "🟡 중립 (부분 공격)")

    print(f"\n  [카나리아] QQQ 모멘텀: {score:+.3f}" if score is not None else "  [카나리아] 데이터부족")
    print(f"  [모드] {mode}")
    print(f"  [목표] TQQQ {tgt_pct:.0f}% / 채권 {100-tgt_pct:.0f}%")

    # 2) TQQQ 현재가 + 급락 감지
    tqqq = fetch_daily(CFG["attack_symbol"], 300)
    tqqq_closes = [float(c.get("clos", 0)) for c in tqqq if float(c.get("clos", 0)) > 0]
    tqqq_now = tqqq_closes[-1] if tqqq_closes else 0
    recent_high = max(tqqq_closes[-60:]) if len(tqqq_closes) >= 60 else (max(tqqq_closes) if tqqq_closes else 0)
    tqqq_dd = (recent_high - tqqq_now) / recent_high * 100 if recent_high > 0 else 0
    crash = tqqq_dd >= CFG["crash_trigger_pct"]

    print(f"\n  [TQQQ] 현재가 ${tqqq_now:.2f}  (최근60일 고점 대비 -{tqqq_dd:.1f}%)")
    if crash:
        print(f"  🚨 급락 경고! 고점 대비 -{tqqq_dd:.1f}% → TQQQ {CFG['crash_derisk_to_pct']:.0f}%로 축소 권고")

    # 3) 보유 상황과 비교 (holdings.json 있으면)
    holdings = load_holdings()
    rebalance_needed = False
    cur_pct = None
    rebalance_msg = ""
    if holdings:
        tv = holdings.get("tqqq_value", 0)
        bv = holdings.get("bond_value", 0)
        total = tv + bv
        if total > 0:
            cur_pct = tv / total * 100
            gap = abs(cur_pct - tgt_pct)
            rebalance_needed = gap >= CFG["rebalance_band"]
            print(f"\n  [보유] TQQQ {cur_pct:.1f}% (${tv:,.0f}) / 채권 {100-cur_pct:.1f}% (${bv:,.0f})")
            print(f"  [비교] 목표 {tgt_pct:.0f}% vs 현재 {cur_pct:.1f}% (차이 {gap:.1f}%p)")
            if rebalance_needed:
                direction = "매수" if tgt_pct > cur_pct else "매도"
                trade_val = abs(tgt_pct - cur_pct) / 100 * total
                rebalance_msg = (f"⚖️ 리밸런싱 필요: TQQQ {direction} "
                                 f"약 ${trade_val:,.0f} (목표 {tgt_pct:.0f}%로 조정)")
                print(f"  {rebalance_msg}")
            else:
                print(f"  ✅ 리밸런싱 불필요 (밴드 {CFG['rebalance_band']:.0f}%p 이내)")

        # TQQQ 고점 자동 갱신
        prev_peak = holdings.get("tqqq_peak", 0)
        if tqqq_now > prev_peak:
            holdings["tqqq_peak"] = tqqq_now
            save_holdings(holdings)
    else:
        print(f"\n  [보유] holdings.json 없음 — 목표 비중만 안내")
        print(f"         (보유 추적하려면 holdings.json 생성)")

    # ── 텔레그램 메시지 구성 ──────────────────────────
    if send:
        msg = f"📊 <b>하이브리드 동적배분 일일 점검</b>\n{now}\n\n"
        if score is not None:
            msg += f"카나리아(QQQ 모멘텀): {score:+.3f}\n"
        msg += f"<b>{mode}</b>\n"
        msg += f"🎯 목표 비중: TQQQ {tgt_pct:.0f}% / 채권 {100-tgt_pct:.0f}%\n"
        msg += f"TQQQ 현재가: ${tqqq_now:.2f} (고점대비 -{tqqq_dd:.1f}%)\n"

        if crash:
            msg += (f"\n🚨 <b>급락 경고</b>\n"
                    f"TQQQ가 최근 고점 대비 -{tqqq_dd:.1f}% 하락.\n"
                    f"→ TQQQ 비중을 {CFG['crash_derisk_to_pct']:.0f}%로 즉시 축소 권고\n")

        if holdings and cur_pct is not None:
            msg += f"\n현재 보유: TQQQ {cur_pct:.1f}% / 채권 {100-cur_pct:.1f}%\n"
            if rebalance_needed:
                msg += f"\n{rebalance_msg}\n"
            else:
                msg += "✅ 리밸런싱 불필요\n"

        msg += "\n⚠️ 신호 알림 전용입니다. 자동 주문은 실행되지 않습니다."
        send_telegram(msg)
        print(f"\n  ✅ 텔레그램 전송 완료")

    print(f"{'='*60}")

if __name__ == "__main__":
    args = sys.argv[1:]
    send = "--check" not in args   # --check면 화면만
    run_check(send=send)
