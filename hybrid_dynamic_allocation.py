"""
하이브리드 동적자산배분 전략 — TQQQ 무한매수법 + 채권 대피
==============================================================
[목표]
  MDD -20% 이내, 연평균 수익률 10% 이상

[기존 전략의 한계]
  무한매수법+카나리아는 MDD -41%, 연평균 ~7%.
  카나리아 방어 시 "현금 대기"라 방어 기간 수익이 0이고,
  3배 레버리지 100% 노출이라 급락 시작~카나리아 전환 사이
  손실이 크다.

[하이브리드 개선안]
  1) 방어 시 현금이 아니라 "채권(TLT/IEF 근사)"으로 대피
     → 하락장에서 채권은 오히려 오르거나 이자 수익 발생
  2) 3배 레버리지 노출 상한 (equity의 일정 비율만 TQQQ)
     → 나머지는 항상 안전자산에 두어 MDD 억제
  3) 카나리아 강도에 따라 공격/방어 비중을 단계적으로 조절
     → 모멘텀 강하면 공격 늘리고, 약하면 채권 늘림

[자산 구성]
  공격자산: TQQQ (합성 3배 레버리지)
  방어자산: 채권 (연 4% 수익 가정, 하락장 소폭 상승 근사)

실행:
  python hybrid_dynamic_allocation.py              # 30년 기본
  python hybrid_dynamic_allocation.py --years 15
  python hybrid_dynamic_allocation.py --tqqq-max 50  # TQQQ 최대 비중 조정
"""

import requests, time, json, sys
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional

# ══════════════════════════════════════════════════════
#  인증 정보
# ══════════════════════════════════════════════════════
try:
    from config import (
        KIS_APP_KEY as APP_KEY,
        KIS_APP_SECRET as APP_SECRET,
        KIS_IS_VIRTUAL as IS_VIRTUAL,
        TELEGRAM_TOKEN_US as TELEGRAM_TOKEN,
        TELEGRAM_CHAT_ID_US as TELEGRAM_CHAT_ID,
        telegram_configured, kis_configured,
    )
except ImportError:
    print("⚠️  config.py 를 찾을 수 없습니다.")
    APP_KEY = APP_SECRET = TELEGRAM_TOKEN = TELEGRAM_CHAT_ID = ""
    IS_VIRTUAL = True
    telegram_configured = kis_configured = lambda *a: False

BASE_URL = "https://openapivts.koreainvestment.com:29443" if IS_VIRTUAL \
           else "https://openapi.koreainvestment.com:9443"

def send_telegram(text: str) -> bool:
    try:
        if not telegram_configured("us"):
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
    except Exception:
        return False

# ══════════════════════════════════════════════════════
#  전략 설정 (v2.2 공개 로직 기반)
# ══════════════════════════════════════════════════════
CFG = {
    "symbol"          : "TQQQ",
    "starting_cash"   : 40_000.0,
    "commission_pct"  : 0.07,
    "years"           : 30,
    "api_delay"       : 0.13,

    # ── 동적 자산배분 핵심 파라미터 ────────────────────
    #   전체 자산을 TQQQ(공격) + 채권(방어)로 나눠 관리.
    #   카나리아 모멘텀에 따라 TQQQ 목표비중을 단계적으로 조절.
    "tqqq_max_pct"    : 38.0,   # TQQQ 최대 비중 (MDD 20% 이내 목표로 하향 조정)
    "tqqq_min_pct"    : 0.0,    # TQQQ 최소 비중 (완전 방어 시 0 = 채권 100%)
    "rebalance_days"  : 21,     # 월 1회(약 21거래일) 리밸런싱
    "rebalance_band"  : 5.0,    # 목표비중과 5%p 이상 벌어지면 리밸런싱

    # ── 카나리아 (스노우72 방식) ───────────────────────
    "canary_months"   : [21, 63, 126, 252],  # 1,3,6,12개월
    "score_full_pct"  : 0.12,   # 모멘텀 0.12(12%)면 최대 공격

    # ── 방어자산(채권) 수익 가정 ───────────────────────
    #   실제 TLT/IEF 데이터 대신 근사: 역사적 장기채 강세 반영
    "bond_annual_pct"     : 5.5,    # 채권 연 수익률 근사 (2007~2020 장기채 강세)
    "bond_crash_bonus"    : 0.3,    # 주식 일간 -3%↓일 때 채권 소폭 상승 계수

    # ── 급락 긴급 방어 (월 1회 리밸런싱의 느린 반응 보완) ──
    #   TQQQ가 고점 대비 crash_trigger% 이상 빠지면 즉시 채권으로 대피
    #   (카나리아가 다음 리밸런싱까지 기다리지 않고 즉각 손실 차단)
    "crash_guard_enabled" : True,
    "crash_trigger_pct"   : 15.0,   # TQQQ 부분이 최근 고점 대비 -15%면 발동 (민감하게)
    "crash_derisk_to_pct" : 12.0,   # 발동 시 TQQQ 비중을 12%로 즉시 축소

    # ── 3배 레버리지 합성 ──────────────────────────────
    "synthetic_base"      : "QQQ",
    "synthetic_er_pct"    : 0.95,
    "synthetic_borrow_pct": 3.0,
}

# ══════════════════════════════════════════════════════
#  KIS API (일봉 수집 — 기존 코드 재활용)
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
            print("  ✅ 토큰 발급 성공")
            return t
    except Exception as e:
        print(f"  ❌ 토큰 오류: {e}")
    return None

def fetch_page(symbol: str, excd: str, bymd: str) -> list:
    t = get_token()
    if not t:
        return []
    h = {"Content-Type": "application/json", "Authorization": f"Bearer {t}",
         "appkey": APP_KEY, "appsecret": APP_SECRET, "tr_id": "HHDFS76240000"}
    url = f"{BASE_URL}/uapi/overseas-price/v1/quotations/dailyprice"
    try:
        r = requests.get(url, headers=h, params={
            "AUTH": "", "EXCD": excd, "SYMB": symbol,
            "GUBN": "0", "BYMD": bymd, "MODP": "1",
        }, timeout=10)
        d = r.json()
        if d.get("rt_cd") == "0":
            return d.get("output2", [])
    except Exception:
        pass
    return []

def fetch_candles(symbol: str, years: int) -> list:
    """일봉 페이징 수집 (주말 빈응답 재시도 포함)"""
    excd = ""
    for e in ["NAS", "NYS", "AMS"]:
        if fetch_page(symbol, e, ""):
            excd = e
            break
        time.sleep(CFG["api_delay"])
    if not excd:
        print("  ❌ 거래소 판별 실패")
        return []

    target = years * 252 + 30
    all_c, seen, base = [], set(), ""
    empty_retry = 0
    max_calls = (target // 100) + 10

    for _ in range(max_calls):
        page = fetch_page(symbol, excd, base)
        if not page:
            empty_retry += 1
            if empty_retry > 5:
                break
            if base:
                base = (datetime.strptime(base, "%Y%m%d") - timedelta(days=7)).strftime("%Y%m%d")
            time.sleep(CFG["api_delay"])
            continue
        empty_retry = 0
        added = 0
        for c in page:
            d = c.get("xymd", "")
            if d and d not in seen:
                seen.add(d)
                all_c.append(c)
                added += 1
        if added == 0:
            break
        oldest = min(c.get("xymd", "99999999") for c in page)
        base = (datetime.strptime(oldest, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
        time.sleep(CFG["api_delay"])
        if len(all_c) >= target:
            break

    all_c.sort(key=lambda c: c.get("xymd", ""))
    print(f"  {symbol} 일봉 {len(all_c)}개 수집 ({all_c[0].get('xymd','')} ~ {all_c[-1].get('xymd','')})")
    return all_c

# ══════════════════════════════════════════════════════
#  카나리아 모멘텀 스코어 (스노우72 HAA 방식)
# ══════════════════════════════════════════════════════
def compute_canary_scores(base_closes: list[float]) -> list[Optional[float]]:
    """
    각 시점 t에 대해 (1,3,6,12개월 수익률의 평균)을 계산한다.
    거래일 근사: 21/63/126/252일.
    워밍업 기간(252일) 이전은 None을 반환한다.

    스코어 >= 0 → 카나리아 안전 (공격 모드 허용)
    스코어 <  0 → 카나리아 위험 (방어 모드 — 신규 매수 중단)
    """
    n = len(base_closes)
    scores: list[Optional[float]] = [None] * n
    periods = CFG["canary_months"]
    warmup = max(periods)

    for i in range(warmup, n):
        rets = []
        for p in periods:
            past = base_closes[i - p]
            now  = base_closes[i]
            if past > 0:
                rets.append((now - past) / past)
        if rets:
            scores[i] = sum(rets) / len(rets)

    return scores

# ══════════════════════════════════════════════════════
#  3배 레버리지 합성 (장기 백테스트용)
# ══════════════════════════════════════════════════════
def synthesize_3x(base_candles: list, start_price: float = 100.0) -> list:
    """
    QQQ 일봉에서 3배 레버리지 ETF 가격 시계열을 합성한다.

    합성 공식 (일일 리셋 레버리지 ETF의 표준 근사):
      r_3x(t) = 3 × r_base(t) − daily_cost
      daily_cost = (운용보수 + 차입비용 × 2) / 252

    - 운용보수: TQQQ 실제 ER 0.95%/년
    - 차입비용: 3배 노출을 만들기 위한 2배분 차입에 연 3% 가정
    - 일일 리셋이므로 복리 왜곡(volatility decay)이 자연히 반영됨
    """
    er     = CFG["synthetic_er_pct"] / 100
    borrow = CFG["synthetic_borrow_pct"] / 100 * 2  # 2배 차입분
    daily_cost = (er + borrow) / 252

    out = []
    prev_close: Optional[float] = None
    price = start_price

    for c in base_candles:
        date = c.get("xymd", "")
        try:
            close = float(c.get("clos", 0))
        except (TypeError, ValueError):
            continue
        if close <= 0:
            continue

        if prev_close is not None and prev_close > 0:
            r_base = close / prev_close - 1
            r_3x   = 3 * r_base - daily_cost
            price  = price * (1 + r_3x)
            if price < 0.01:   # 사실상 청산 수준 방어
                price = 0.01
        prev_close = close
        out.append({"xymd": date, "clos": f"{price:.4f}"})

    return out


# ══════════════════════════════════════════════════════
#  하이브리드 동적자산배분 백테스트 엔진
# ══════════════════════════════════════════════════════
def target_tqqq_pct(score: Optional[float]) -> float:
    """
    카나리아 모멘텀 스코어 → TQQQ 목표 비중(%).
      score <= 0            → tqqq_min_pct (채권 대피)
      score >= score_full   → tqqq_max_pct (최대 공격)
      그 사이는 선형 비례.
    """
    lo, hi = CFG["tqqq_min_pct"], CFG["tqqq_max_pct"]
    if score is None:
        return lo   # 데이터 부족하면 보수적으로 방어
    if score <= 0:
        return lo
    if score >= CFG["score_full_pct"]:
        return hi
    ratio = score / CFG["score_full_pct"]
    return lo + (hi - lo) * ratio

def run_backtest(candles: list, canary_scores: Optional[list] = None) -> dict:
    """
    동적자산배분 백테스트:
      매일 TQQQ와 채권 두 자산의 가치를 각각 갱신하고,
      리밸런싱 주기마다 카나리아 모멘텀에 따른 목표비중으로 조정한다.

    - TQQQ 가치: 합성 3배 레버리지 일수익률로 매일 변동
    - 채권 가치: 연 4% 수익 + 주식 급락 시 소폭 상승(안전자산 선호)
    - 리밸런싱: rebalance_days 마다, 목표비중과 rebalance_band 이상
      벌어졌을 때만 실행 (거래비용 절약)
    """
    fee = CFG["commission_pct"] / 100
    bond_daily = (1 + CFG["bond_annual_pct"] / 100) ** (1/252) - 1

    # 초기: 전액 채권에서 시작 (첫 리밸런싱 때 카나리아 보고 편입)
    tqqq_val = 0.0
    bond_val = CFG["starting_cash"]

    equity_curve = []
    peak = CFG["starting_cash"]
    mdd = 0.0
    rebalance_count = 0
    crash_guard_count = 0
    tqqq_peak = 0.0          # TQQQ 부분의 고점 추적 (급락 감지용)
    days_since_reb = CFG["rebalance_days"]   # 첫날 바로 리밸런싱 허용
    prev_close: Optional[float] = None

    for idx, c in enumerate(candles):
        date = c.get("xymd", "")
        try:
            close = float(c.get("clos", 0))
        except (TypeError, ValueError):
            continue
        if close <= 0:
            continue

        # ── 1. 자산 가치 일일 갱신 ─────────────────────
        # TQQQ: 전일 대비 수익률만큼 변동
        if prev_close is not None and prev_close > 0 and tqqq_val > 0:
            r = close / prev_close - 1
            tqqq_val *= (1 + r)

        # 채권: 매일 이자 + 주식 급락일엔 안전자산 선호로 소폭 상승
        stock_daily_ret = (close / prev_close - 1) if (prev_close and prev_close > 0) else 0.0
        bond_move = bond_daily
        if stock_daily_ret < -0.03:   # 주식 하루 -3%↓
            bond_move += abs(stock_daily_ret) * CFG["bond_crash_bonus"] * 0.1
        bond_val *= (1 + bond_move)

        prev_close = close
        days_since_reb += 1

        # TQQQ 고점 추적
        if tqqq_val > tqqq_peak:
            tqqq_peak = tqqq_val

        # ── 1.5. 급락 긴급 방어 (리밸런싱 주기 무시하고 즉시) ──
        if CFG["crash_guard_enabled"] and tqqq_val > 0 and tqqq_peak > 0:
            tqqq_dd = (tqqq_peak - tqqq_val) / tqqq_peak * 100
            total = tqqq_val + bond_val
            cur_pct = tqqq_val / total * 100 if total > 0 else 0
            if tqqq_dd >= CFG["crash_trigger_pct"] and cur_pct > CFG["crash_derisk_to_pct"]:
                # TQQQ를 즉시 축소 → 채권으로 대피
                tgt_val = total * CFG["crash_derisk_to_pct"] / 100
                cost = abs(tqqq_val - tgt_val) * fee
                tqqq_val = tgt_val
                bond_val = total - tgt_val - cost
                crash_guard_count += 1
                tqqq_peak = tqqq_val   # 고점 리셋 (재진입 후 새로 추적)

        # ── 2. 리밸런싱 (월 1회, 밴드 초과 시만) ────────
        if days_since_reb >= CFG["rebalance_days"]:
            score = None
            if canary_scores is not None and idx < len(canary_scores):
                score = canary_scores[idx]

            total = tqqq_val + bond_val
            if total > 0:
                cur_tqqq_pct = tqqq_val / total * 100
                tgt_tqqq_pct = target_tqqq_pct(score)

                # 목표와 현재 비중이 band 이상 벌어졌을 때만 거래
                if abs(cur_tqqq_pct - tgt_tqqq_pct) >= CFG["rebalance_band"]:
                    tgt_tqqq_val = total * tgt_tqqq_pct / 100
                    trade_amount = abs(tgt_tqqq_val - tqqq_val)
                    cost = trade_amount * fee   # 리밸런싱 거래비용
                    tqqq_val = tgt_tqqq_val
                    bond_val = total - tgt_tqqq_val - cost
                    rebalance_count += 1

            days_since_reb = 0

        # ── 3. 자산곡선 / MDD ──────────────────────────
        total = tqqq_val + bond_val
        equity_curve.append((date, round(total, 2)))
        if total > peak:
            peak = total
        dd = (peak - total) / peak * 100 if peak > 0 else 0
        mdd = max(mdd, dd)

    final_total = equity_curve[-1][1] if equity_curve else CFG["starting_cash"]
    n_years = len(equity_curve) / 252 if equity_curve else 1
    cagr = ((final_total / CFG["starting_cash"]) ** (1/n_years) - 1) * 100 if n_years > 0 else 0

    return {
        "equity_curve": equity_curve,
        "final_total": final_total,
        "total_return_pct": round((final_total / CFG["starting_cash"] - 1) * 100, 2),
        "cagr_pct": round(cagr, 2),
        "mdd_pct": round(mdd, 2),
        "rebalance_count": rebalance_count,
        "crash_guard_count": crash_guard_count,
        "n_years": round(n_years, 1),
    }

# ══════════════════════════════════════════════════════
#  결과 출력
# ══════════════════════════════════════════════════════
SEP2 = "═" * 72

def print_summary(res: dict, symbol: str, years: int):
    print(f"\n{SEP2}")
    print(f"  📊  하이브리드 동적자산배분 — {symbol} / {years}년")
    print(f"  TQQQ 비중 {CFG['tqqq_min_pct']:.0f}~{CFG['tqqq_max_pct']:.0f}% "
          f"(카나리아 연동) + 나머지 채권")
    print(f"  리밸런싱: {CFG['rebalance_days']}거래일마다 "
          f"(밴드 {CFG['rebalance_band']:.0f}%p)")
    print(SEP2)

    print(f"\n  리밸런싱 횟수: {res['rebalance_count']}회  |  급락방어 발동: {res['crash_guard_count']}회  |  기간: {res['n_years']}년")

    print(f"\n  [최종 결과]")
    print(f"  최종 자산: ${res['final_total']:,.0f}  ({res['total_return_pct']:+.1f}%)")
    print(f"  📈 연평균 수익률(CAGR): {res['cagr_pct']:+.1f}%")
    print(f"  📉 MDD: -{res['mdd_pct']:.1f}%")

    # 목표 달성 여부
    goal_cagr = res['cagr_pct'] >= 10.0
    goal_mdd  = res['mdd_pct'] <= 20.0
    print(f"\n  [목표 달성 여부]")
    print(f"  연평균 10%+ : {'✅ 달성' if goal_cagr else '❌ 미달'} ({res['cagr_pct']:+.1f}%)")
    print(f"  MDD -20% 이내: {'✅ 달성' if goal_mdd else '❌ 미달'} (-{res['mdd_pct']:.1f}%)")

    print(f"\n  📅  연도별 자산 추이 (연말 기준)")
    year_end = {}
    for date, total in res["equity_curve"]:
        year_end[date[:4]] = total
    prev = CFG["starting_cash"]
    for yr in sorted(year_end.keys()):
        total = year_end[yr]
        yr_ret = (total / prev - 1) * 100
        print(f"  {yr}: ${total:>12,.0f}  ({yr_ret:+6.1f}%)")
        prev = total

    print(f"\n  ⚠️  채권 수익은 연 {CFG['bond_annual_pct']}% 근사이며, 실제 TLT/IEF와 다를 수 있습니다.")
    print(f"     3배 레버리지 합성이므로 실제와 오차가 있습니다.")
    print(SEP2)

def save(res: dict, symbol: str, years: int):
    out = {"config": CFG, "symbol": symbol, "years": years,
           "final_total": res["final_total"],
           "total_return_pct": res["total_return_pct"],
           "cagr_pct": res["cagr_pct"], "mdd_pct": res["mdd_pct"]}
    path = f"hybrid_backtest_{years}y.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n  💾  저장: {path}")

# ══════════════════════════════════════════════════════
#  실행
# ══════════════════════════════════════════════════════
if __name__ == "__main__":
    args = sys.argv[1:]
    years = CFG["years"]
    if "--years" in args:
        try: years = int(args[args.index("--years")+1])
        except: pass
    if "--tqqq-max" in args:
        try: CFG["tqqq_max_pct"] = float(args[args.index("--tqqq-max")+1])
        except: pass

    print(f"\n{SEP2}")
    print(f"  📡  하이브리드 동적자산배분 백테스트 — {years}년")
    print(SEP2)

    if not get_token():
        print("  ❌ 토큰 발급 실패 — .env 확인")
        sys.exit(1)

    symbol = CFG["symbol"]
    use_synthetic = (symbol == "TQQQ" and years > 15) or ("--synthetic" in args)

    if use_synthetic:
        base = CFG["synthetic_base"]
        print(f"  ℹ️  {years}년 요청 — {base} 일봉으로 3배 레버리지 합성")
        base_candles = fetch_candles(base, years)
        if len(base_candles) < 100:
            print("  ❌ 데이터 부족"); sys.exit(1)
        candles = synthesize_3x(base_candles)
        base_closes = [float(c.get("clos", 0)) for c in base_candles]
        symbol_label = f"{symbol}(합성/{base}기반)"
    else:
        candles = fetch_candles(symbol, years)
        if len(candles) < 100:
            print("  ❌ 데이터 부족"); sys.exit(1)
        qqq = fetch_candles("QQQ", years)
        base_closes = [float(c.get("clos", 0)) for c in qqq] if qqq else \
                      [float(c.get("clos", 0)) for c in candles]
        symbol_label = symbol

    canary_scores = compute_canary_scores(base_closes)
    if len(canary_scores) != len(candles):
        n = min(len(canary_scores), len(candles))
        canary_scores, candles = canary_scores[:n], candles[:n]

    res = run_backtest(candles, canary_scores)
    print_summary(res, symbol_label, years)
    save(res, symbol, years)

    send_telegram(
        f"📊 <b>하이브리드 동적배분 백테스트</b>\n"
        f"{symbol_label} / {years}년\n"
        f"연평균(CAGR): {res['cagr_pct']:+.1f}%\n"
        f"MDD: -{res['mdd_pct']:.1f}%\n"
        f"최종: ${res['final_total']:,.0f} ({res['total_return_pct']:+.1f}%)"
    )
