"""
하이브리드 동적자산배분 (한국판) — KODEX 레버리지 실전 모니터링
================================================================
[중요] 신호 알림 전용입니다. 자동 주문은 넣지 않습니다.

[미국판과의 차이]
  - 공격자산: KODEX 레버리지(122630) = 코스피200 2배 (TQQQ는 3배)
    → 2배라 변동성이 낮으므로 공격 비중을 더 높게(최대 55%) 가져감
  - 카나리아: KODEX 200(069500) = 코스피200 지수
  - 방어자산: KODEX 국고채(153130) 또는 현금/MMF
  - 국내 API 사용 (해외 API 아님)

[자산 구성]
  공격: KODEX 레버리지 122630 (코스피200 x2)
  방어: 채권/현금 (나머지)

[전략 로직 — 미국판과 동일]
  카나리아 모멘텀(1,3,6,12개월 평균) 강도에 따라
  레버리지 목표 비중을 0~55% 사이에서 조절.
  급락(-15%) 감지 시 즉시 축소.

[보유 상황] — holdings_kr.json (없으면 목표 비중만 안내)
  {"lev_value": 20000000, "bond_value": 30000000, "lev_peak": 24000000}
  (단위: 원)

실행:
  python hybrid_kr_monitor.py          # 1회 점검 + 텔레그램
  python hybrid_kr_monitor.py --check  # 화면 출력만
"""

import requests, time, json, sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# ── 인증 (한국주식 봇 = KR 채널) ──────────────────────
try:
    from config import (
        KIS_APP_KEY as APP_KEY,
        KIS_APP_SECRET as APP_SECRET,
        KIS_IS_VIRTUAL as IS_VIRTUAL,
        TELEGRAM_TOKEN_KR as TELEGRAM_TOKEN,
        TELEGRAM_CHAT_ID_KR as TELEGRAM_CHAT_ID,
        telegram_configured,
    )
except ImportError:
    print("⚠️  config.py 없음 — .env 확인")
    APP_KEY = APP_SECRET = TELEGRAM_TOKEN = TELEGRAM_CHAT_ID = ""
    IS_VIRTUAL = True
    telegram_configured = lambda *a: False

BASE_URL = "https://openapivts.koreainvestment.com:29443" if IS_VIRTUAL \
           else "https://openapi.koreainvestment.com:9443"

# ── 전략 설정 ─────────────────────────────────────────
CFG = {
    "attack_symbol"   : "122630",  # KODEX 레버리지 (코스피200 2배)
    "attack_name"     : "KODEX 레버리지",
    "canary_index"    : "2001",    # 코스피200 지수 (업종코드, ETF 아님 → 왜곡 없음)
    "bond_symbol"     : "153130",  # KODEX 국고채 (참고용)
    # 2배 레버리지라 3배(TQQQ)보다 변동성 낮음 → 공격 비중 상향
    "lev_max_pct"     : 55.0,      # KODEX 레버리지 최대 비중
    "lev_min_pct"     : 0.0,
    "score_full_pct"  : 0.20,      # 레버리지 기준 모멘텀 20%면 최대 공격 (2배 증폭 반영)
    "canary_months"   : [21, 63, 126, 252],
    "rebalance_band"  : 5.0,
    "crash_trigger_pct": 15.0,     # 고점 대비 -15%면 급락 경고
    "crash_derisk_to_pct": 15.0,
    "api_delay"       : 0.13,
    "holdings_file"   : "holdings_kr.json",
}

HOLDINGS = Path(CFG["holdings_file"])

# ══════════════════════════════════════════════════════
#  텔레그램
# ══════════════════════════════════════════════════════
def send_telegram(text: str) -> bool:
    try:
        if not telegram_configured("kr"):
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
#  KIS API (국내 주식)
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

def fetch_index_daily_kr(index_code: str = "2001", days_target: int = 300) -> list:
    """
    코스피200 지수(업종코드 2001) 일봉 수집.
    ETF(069500)와 달리 순수 지수라 액면분할 왜곡이 없다.
    업종 지수 일봉 API 사용 (tr_id: FHKUP03500100).

    지수 API는 요청 날짜 구간 내 데이터를 반환하므로,
    구간을 넓게(약 100거래일=150일씩) 잡고 과거로 밀며 페이징한다.
    """
    t = get_token()
    if not t:
        return []
    h = {"Content-Type": "application/json", "Authorization": f"Bearer {t}",
         "appkey": APP_KEY, "appsecret": APP_SECRET, "tr_id": "FHKUP03500100"}
    url = f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice"

    all_c, seen = [], set()
    end_dt = datetime.now()

    # days_target 거래일 확보까지 과거로 밀며 반복 (거래일≈달력일*0.7)
    max_pages = (days_target // 50) + 8
    for _ in range(max_pages):
        # 한 번에 넉넉히 210일(약 100거래일 이상) 요청
        start_dt = end_dt - timedelta(days=210)
        try:
            r = requests.get(url, headers=h, params={
                "FID_COND_MRKT_DIV_CODE": "U",
                "FID_INPUT_ISCD": index_code,
                "FID_INPUT_DATE_1": start_dt.strftime("%Y%m%d"),
                "FID_INPUT_DATE_2": end_dt.strftime("%Y%m%d"),
                "FID_PERIOD_DIV_CODE": "D",
            }, timeout=10)
            page = r.json().get("output2", [])
        except Exception:
            break
        if not page:
            break
        added = 0
        for c in page:
            d = c.get("stck_bsop_date", "")
            if d and d not in seen:
                seen.add(d); all_c.append(c); added += 1
        # 이번 페이지에서 가장 오래된 날짜를 다음 조회의 끝점으로
        oldest = min((c.get("stck_bsop_date", "99999999") for c in page
                      if c.get("stck_bsop_date")), default="")
        if added == 0 or not oldest:
            break
        try:
            new_end = datetime.strptime(oldest, "%Y%m%d") - timedelta(days=1)
        except ValueError:
            break
        # 진전이 없으면(같은 구간 반복) 중단
        if new_end >= end_dt:
            break
        end_dt = new_end
        time.sleep(CFG["api_delay"])
        if len(all_c) >= days_target:
            break

    all_c.sort(key=lambda c: c.get("stck_bsop_date", ""))
    return all_c

def fetch_daily_kr(symbol: str, days_target: int = 300) -> list:
    """국내 일봉 수집 (페이징) — 최신순 응답을 오름차순 정렬"""
    t = get_token()
    if not t:
        return []
    h = {"Content-Type": "application/json", "Authorization": f"Bearer {t}",
         "appkey": APP_KEY, "appsecret": APP_SECRET, "tr_id": "FHKST03010100"}
    url = f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"

    all_c, seen = [], set()
    end_dt = datetime.now()

    for _ in range((days_target // 100) + 6):
        start_dt = end_dt - timedelta(days=150)
        try:
            r = requests.get(url, headers=h, params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": symbol,
                "FID_INPUT_DATE_1": start_dt.strftime("%Y%m%d"),
                "FID_INPUT_DATE_2": end_dt.strftime("%Y%m%d"),
                "FID_PERIOD_DIV_CODE": "D",
                "FID_ORG_ADJ_PRC": "1",
            }, timeout=10)
            page = r.json().get("output2", [])
        except Exception:
            break
        if not page:
            break
        added = 0
        for c in page:
            d = c.get("stck_bsop_date", "")
            if d and d not in seen:
                seen.add(d); all_c.append(c); added += 1
        if added == 0:
            break
        oldest = min(c.get("stck_bsop_date", "99999999") for c in page)
        try:
            end_dt = datetime.strptime(oldest, "%Y%m%d") - timedelta(days=1)
        except ValueError:
            break
        time.sleep(CFG["api_delay"])
        if len(all_c) >= days_target:
            break

    all_c.sort(key=lambda c: c.get("stck_bsop_date", ""))
    return all_c

# ══════════════════════════════════════════════════════
#  카나리아 & 목표 비중
# ══════════════════════════════════════════════════════
def momentum_score(closes: list[float], debug: bool = False) -> Optional[float]:
    periods = CFG["canary_months"]
    if len(closes) < max(periods) + 1:
        avail = [p for p in periods if len(closes) > p]
        if not avail:
            return None
        periods = avail
    now = closes[-1]
    rets = []
    for p in periods:
        past = closes[-1 - p]
        if past > 0:
            r = (now - past) / past
            rets.append(r)
            if debug:
                print(f"    {p}거래일 전: {past:,.0f} → 현재 {now:,.0f} = {r*100:+.1f}%")
    if not rets:
        return None
    score = sum(rets) / len(rets)
    # 이상치 방어: 개별 수익률이 비정상적으로 크면(±100%↑) 데이터 오류 의심
    if debug and any(abs(r) > 1.0 for r in rets):
        print(f"    ⚠️ 비정상 수익률 감지 — 데이터 확인 필요")
    return score

def target_lev_pct(score: Optional[float]) -> float:
    lo, hi = CFG["lev_min_pct"], CFG["lev_max_pct"]
    if score is None or score <= 0:
        return lo
    if score >= CFG["score_full_pct"]:
        return hi
    return lo + (hi - lo) * (score / CFG["score_full_pct"])

# ══════════════════════════════════════════════════════
#  보유 상황
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
#  메인 점검
# ══════════════════════════════════════════════════════
def run_check(send: bool = True):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"\n{'='*60}")
    print(f"  하이브리드 동적배분 (한국) 실전 점검 — {now}")
    print(f"{'='*60}")

    if not get_token():
        print("  ❌ 토큰 발급 실패")
        return

    # 1) 카나리아 — KODEX 레버리지 최근 데이터로 단기 추세 판단.
    #    [중요] 122630은 액면분할 이력이 있어 KIS 수정주가가 과거를 왜곡한다.
    #    분할은 며칠에 걸쳐 완만히 나타나기도 해서 일간 급변 감지로는 부족.
    #    → 가장 확실한 방법: 현재가의 60% 미만인 과거 가격은 분할 왜곡으로 보고,
    #      그런 값이 나타난 지점 "이후"의 연속 구간만 사용한다.
    lev_for_canary = fetch_daily_kr(CFG["attack_symbol"], 120)
    if len(lev_for_canary) < 30:
        print("  ❌ 카나리아 데이터 부족")
        return
    raw = [float(c.get("stck_clpr", 0)) for c in lev_for_canary
           if float(c.get("stck_clpr", 0)) > 0]

    cur_price = raw[-1]
    floor = cur_price * 0.60   # 현재가의 60% 미만이면 왜곡으로 간주

    # 뒤(최신)에서부터 거슬러 올라가다, floor 미만 값을 만나면 거기서 끊음
    start_i = 0
    for i in range(len(raw) - 1, -1, -1):
        if raw[i] < floor:
            start_i = i + 1
            break
    clean = raw[start_i:]
    if start_i > 0:
        print(f"  ⚠️ 분할 왜곡 제거 → 최근 {len(clean)}일만 사용 "
              f"(현재가 {cur_price:,.0f}의 60%={floor:,.0f}원 미만 컷)")
    idx_closes = clean

    if len(idx_closes) < 22:
        print(f"  ⚠️ 깨끗한 데이터가 {len(idx_closes)}일뿐 — 최소 단기(1개월) 모멘텀만 사용")

    print(f"\n  [카나리아 계산 상세] (KODEX 레버리지 최근구간)")
    score = momentum_score(idx_closes, debug=True)
    tgt_pct = target_lev_pct(score)

    defensive = (score is not None and score <= 0)
    mode = "🔴 방어 (채권/현금 100%)" if defensive else (
           "🟢 공격" if tgt_pct >= CFG["lev_max_pct"] - 1 else "🟡 중립 (부분 공격)")

    print(f"\n  [카나리아] KODEX레버리지 모멘텀: {score:+.3f}" if score is not None else "  [카나리아] 데이터부족")
    print(f"  [모드] {mode}")
    print(f"  [목표] KODEX레버리지 {tgt_pct:.0f}% / 채권·현금 {100-tgt_pct:.0f}%")

    # 2) KODEX 레버리지 현재가 + 급락 감지 (분할 왜곡 없는 최근 구간 사용)
    lev_now = idx_closes[-1] if idx_closes else 0
    recent_high = max(idx_closes[-60:]) if len(idx_closes) >= 60 else (max(idx_closes) if idx_closes else 0)
    lev_dd = (recent_high - lev_now) / recent_high * 100 if recent_high > 0 else 0
    crash = lev_dd >= CFG["crash_trigger_pct"]

    print(f"\n  [KODEX레버리지] 현재가 {lev_now:,.0f}원  (최근고점 대비 -{lev_dd:.1f}%)")
    if crash:
        print(f"  🚨 급락 경고! 고점 대비 -{lev_dd:.1f}% → {CFG['crash_derisk_to_pct']:.0f}%로 축소 권고")

    # 3) 보유 비교
    holdings = load_holdings()
    rebalance_needed = False
    cur_pct = None
    rebalance_msg = ""
    if holdings:
        lv = holdings.get("lev_value", 0)
        bv = holdings.get("bond_value", 0)
        total = lv + bv
        if total > 0:
            cur_pct = lv / total * 100
            gap = abs(cur_pct - tgt_pct)
            rebalance_needed = gap >= CFG["rebalance_band"]
            print(f"\n  [보유] 레버리지 {cur_pct:.1f}% ({lv:,.0f}원) / 채권 {100-cur_pct:.1f}% ({bv:,.0f}원)")
            print(f"  [비교] 목표 {tgt_pct:.0f}% vs 현재 {cur_pct:.1f}% (차이 {gap:.1f}%p)")
            if rebalance_needed:
                direction = "매수" if tgt_pct > cur_pct else "매도"
                trade_val = abs(tgt_pct - cur_pct) / 100 * total
                rebalance_msg = (f"⚖️ 리밸런싱 필요: 레버리지 {direction} "
                                 f"약 {trade_val:,.0f}원 (목표 {tgt_pct:.0f}%로 조정)")
                print(f"  {rebalance_msg}")
            else:
                print(f"  ✅ 리밸런싱 불필요 (밴드 {CFG['rebalance_band']:.0f}%p 이내)")

        prev_peak = holdings.get("lev_peak", 0)
        if lev_now > prev_peak:
            holdings["lev_peak"] = lev_now
            save_holdings(holdings)
    else:
        print(f"\n  [보유] holdings_kr.json 없음 — 목표 비중만 안내")

    # ── 텔레그램 ──────────────────────────────────────
    if send:
        msg = f"📊 <b>하이브리드 동적배분 (한국) 일일 점검</b>\n{now}\n\n"
        if score is not None:
            msg += f"카나리아(코스피200 모멘텀): {score:+.3f}\n"
        msg += f"<b>{mode}</b>\n"
        msg += f"🎯 목표: KODEX레버리지 {tgt_pct:.0f}% / 채권·현금 {100-tgt_pct:.0f}%\n"
        msg += f"KODEX레버리지 현재가: {lev_now:,.0f}원 (고점대비 -{lev_dd:.1f}%)\n"

        if crash:
            msg += (f"\n🚨 <b>급락 경고</b>\n"
                    f"KODEX레버리지가 최근 고점 대비 -{lev_dd:.1f}% 하락.\n"
                    f"→ 비중을 {CFG['crash_derisk_to_pct']:.0f}%로 즉시 축소 권고\n")

        if holdings and cur_pct is not None:
            msg += f"\n현재 보유: 레버리지 {cur_pct:.1f}% / 채권 {100-cur_pct:.1f}%\n"
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
    send = "--check" not in args
    run_check(send=send)
