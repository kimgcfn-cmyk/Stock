"""
코스피 일일 역추세 스윙 백테스트 — "떨어지면 롱, 오르면 숏, +5% 익절"
================================================================
[사용자 전략 명세]
  - 당일 코스피 하락 마감 → 장마감 전 2배 레버리지 매수 (반등 노림)
  - 당일 코스피 상승 마감 → 장마감 전 인버스 2배 매수 (되돌림 노림)
  - 보유 중 +5% 익절 / -5% 손절 (손절 없는 버전은 계좌소멸 확인됨)
  - 동시보유 불가 (한 포지션 청산 후 다음 진입)

[백테스트 데이터]
  코스피200 지수(2001)의 일봉 O/H/L/C에서 2배 레버리지/인버스를 합성.
  합성식: 당일 각 가격의 전일종가 대비 수익률 × (+2 또는 -2) − 일일비용
  (실전은 삼성전자/SK하이닉스 단일종목레버리지 + KODEX인버스2X 사용 예정
   — 단일종목은 지수와 움직임이 달라 실전 성과는 백테스트와 차이날 수 있음)

[손절 없음의 리스크를 드러내는 지표]
  - 최대 물림 기간 (거래일)
  - 보유 중 최대 평가손실 (%)
  - 기간 종료 시 미청산 포지션 여부

실행:
  python kr_daily_swing_backtest.py                       # 명세 그대로 (손절 없음)
  python kr_daily_swing_backtest.py --optimize            # 진입임계/목표 탐색
  python kr_daily_swing_backtest.py --since 20260101 --min-trades 3
  python kr_daily_swing_backtest.py --stop 10             # 손절 -10% 추가 비교
"""

import sys, time, json
from datetime import datetime, timedelta
from typing import Optional

import requests

try:
    from config import (
        KIS_APP_KEY as APP_KEY,
        KIS_APP_SECRET as APP_SECRET,
        KIS_IS_VIRTUAL as IS_VIRTUAL,
    )
except ImportError:
    print("⚠️  config.py 를 찾을 수 없습니다.")
    APP_KEY = APP_SECRET = ""
    IS_VIRTUAL = True

BASE_URL = "https://openapivts.koreainvestment.com:29443" if IS_VIRTUAL \
           else "https://openapi.koreainvestment.com:9443"

# ══════════════════════════════════════════════════════
#  설정
# ══════════════════════════════════════════════════════
CFG = {
    "index_code"      : "2001",
    "years"           : 3.0,
    "since_date"      : None,
    "warmup_days"     : 10,
    "starting_cash"   : 10_000_000.0,

    # ── 사용자 명세 파라미터 ────────────────────────────
    "entry_drop_pct"  : 1.0,   # 당일 -1.0% 이상 하락 마감 → 롱 진입
    "entry_rise_pct"  : 1.0,   # 당일 +1.0% 이상 상승 마감 → 숏 진입
    "target_pct"      : 15.0,  # +15% 익절 (손익비 5:1)
    "stop_pct"        : 3.0,   # 손절 -3% (타이트) — 손익비 5:1
    "max_hold_days"   : 0,     # 0 = 무제한 보유 (명세 기본)
    "trade_long"      : True,  # 하락일 → 레버리지 롱
    "trade_short"     : True,  # 상승일 → 인버스 숏 (--long-only로 끔)

    # ── 2배 합성 비용 ───────────────────────────────────
    "synth_annual_cost_pct": 1.0,   # 레버리지 ETF 보수+비용 연 1% 근사
    "fee_pct"         : 0.015,

    "risk_per_trade"  : 1.0,   # 계좌 100% 투입 (단순 명세 — 한 포지션에 전액)
}

# ══════════════════════════════════════════════════════
#  KIS 인증 / 지수 일봉 수집 (O/H/L/C 포함)
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

def fetch_index_daily(code: str, days_target: int) -> list:
    """코스피200 지수 일봉 (시/고/저/종 전부)"""
    t = get_token()
    if not t:
        return []
    h = {"Content-Type": "application/json", "Authorization": f"Bearer {t}",
         "appkey": APP_KEY, "appsecret": APP_SECRET, "tr_id": "FHKUP03500100"}
    url = f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice"
    all_c, seen = [], set()
    end_dt = datetime.now()
    for _ in range((days_target // 50) + 8):
        start_dt = end_dt - timedelta(days=210)
        try:
            r = requests.get(url, headers=h, params={
                "FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": code,
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
        oldest = min((c.get("stck_bsop_date","99999999") for c in page
                      if c.get("stck_bsop_date")), default="")
        if added == 0 or not oldest:
            break
        try:
            new_end = datetime.strptime(oldest, "%Y%m%d") - timedelta(days=1)
        except ValueError:
            break
        if new_end >= end_dt:
            break
        end_dt = new_end
        time.sleep(0.1)
        if len(all_c) >= days_target:
            break
    all_c.sort(key=lambda c: c.get("stck_bsop_date",""))
    return all_c

# ══════════════════════════════════════════════════════
#  2배 레버리지 / 인버스 2배 합성 (일봉 OHLC)
# ══════════════════════════════════════════════════════
def synthesize(idx_ohlc: list, mult: float, start_price: float = 10000.0) -> dict:
    """
    지수 일봉에서 배수(mult=+2 레버리지, -2 인버스) ETF OHLC 합성.
    각 가격 = 전일 합성종가 × (1 + mult × (지수각가격/지수전일종가 - 1) - 일일비용)
    인버스(mult<0)는 지수 고가가 합성 저가가 되므로 high/low 스왑 처리.
    반환: {date: (o,h,l,c)}
    """
    daily_cost = CFG["synth_annual_cost_pct"] / 100 / 252
    out = {}
    prev_idx_close = None
    prev_synth_close = start_price
    for row in idx_ohlc:
        d = row["date"]
        io, ih, il, ic = row["o"], row["h"], row["l"], row["c"]
        if prev_idx_close is None or prev_idx_close <= 0:
            out[d] = (prev_synth_close,)*4
            prev_idx_close = ic
            continue
        def conv(p):
            r = p/prev_idx_close - 1
            return prev_synth_close * (1 + mult*r - daily_cost)
        so, sh_, sl_, sc = conv(io), conv(ih), conv(il), conv(ic)
        if mult < 0:
            sh_, sl_ = sl_, sh_   # 인버스: 지수 고가=합성 저가
        # 안전: high/low 정합
        hi = max(so, sh_, sl_, sc)
        lo = min(so, sh_, sl_, sc)
        out[d] = (so, hi, lo, sc)
        prev_idx_close = ic
        prev_synth_close = sc
    return out

# ══════════════════════════════════════════════════════
#  백테스트 엔진 (명세: 종가 진입, +5% 익절, 손절 없음)
# ══════════════════════════════════════════════════════
def run_backtest(dates, idx_close, lev_ohlc, inv_ohlc,
                 start_date: Optional[str] = None) -> dict:
    fee = CFG["fee_pct"]/100
    cash = CFG["starting_cash"]
    trades = []
    peak, mdd = cash, 0.0
    position = None
    # 물림 추적
    worst_underwater_pct = 0.0
    longest_hold = 0

    n = len(dates)
    for i in range(1, n):
        date = dates[i]
        day_ret = (idx_close[i]/idx_close[i-1] - 1) * 100 if idx_close[i-1] > 0 else 0

        # ── 보유 중: 익절/손절/시간 판정 (당일 고가 기준) ──
        if position is not None:
            pm = lev_ohlc if position["side"] == "long" else inv_ohlc
            if date in pm:
                o, h, l, c = pm[date]
                entry = position["entry"]
                position["days"] += 1
                longest_hold = max(longest_hold, position["days"])
                # 보유 중 평가손 최저점 추적
                uw = (l - entry)/entry*100
                worst_underwater_pct = min(worst_underwater_pct, uw)

                target = entry * (1 + CFG["target_pct"]/100)
                exit_now, reason, exit_price = False, "", None
                if CFG["stop_pct"] > 0 and l <= entry*(1-CFG["stop_pct"]/100):
                    exit_now, reason = True, "stop"
                    exit_price = entry*(1-CFG["stop_pct"]/100)
                elif h >= target:
                    exit_now, reason, exit_price = True, "target", target
                elif CFG["max_hold_days"] > 0 and position["days"] >= CFG["max_hold_days"]:
                    exit_now, reason, exit_price = True, "timeout", c

                if exit_now:
                    change = (exit_price-entry)/entry*100
                    pv = cash * CFG["risk_per_trade"]
                    net = pv*(change/100) - pv*fee*2
                    cash += net
                    trades.append({"date": date, "side": position["side"],
                                  "entry": round(entry,0), "exit": round(exit_price,0),
                                  "change_pct": round(change,2), "pnl": round(net,0),
                                  "reason": reason, "held_days": position["days"],
                                  "entry_date": position["entry_date"]})
                    position = None
                    if cash > peak: peak = cash
                    dd = (peak-cash)/peak*100 if peak > 0 else 0
                    mdd = max(mdd, dd)
            if position is not None:
                continue   # 보유 유지 중이면 신규 진입 없음

        # ── 미보유: 종가 기준 당일 등락으로 진입 판정 ──
        if start_date and date < start_date:
            continue
        if CFG["trade_long"] and day_ret <= -CFG["entry_drop_pct"] and date in lev_ohlc:
            entry = lev_ohlc[date][3]   # 장마감 전 = 종가 근사
            position = {"side":"long", "entry": entry, "days": 0,
                       "entry_date": date}
        elif CFG["trade_short"] and day_ret >= CFG["entry_rise_pct"] and date in inv_ohlc:
            entry = inv_ohlc[date][3]
            position = {"side":"short", "entry": entry, "days": 0,
                       "entry_date": date}

    # 기간 종료 시 미청산 포지션
    open_pos = None
    if position is not None:
        pm = lev_ohlc if position["side"] == "long" else inv_ohlc
        last_c = pm[dates[-1]][3] if dates[-1] in pm else position["entry"]
        open_pos = {"side": position["side"], "entry_date": position["entry_date"],
                   "days": position["days"],
                   "unrealized_pct": round((last_c-position["entry"])/position["entry"]*100,2)}

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    pf = abs(sum(t["pnl"] for t in wins)/sum(t["pnl"] for t in losses)) \
         if losses and sum(t["pnl"] for t in losses) != 0 else (99.0 if wins else 0)

    yearly = {}
    for t in trades:
        yr = t["date"][:4]
        if yr not in yearly: yearly[yr] = {"trades":0,"wins":0,"pnl":0}
        yearly[yr]["trades"] += 1
        if t["pnl"]>0: yearly[yr]["wins"] += 1
        yearly[yr]["pnl"] += t["pnl"]

    avg_hold = sum(t["held_days"] for t in trades)/len(trades) if trades else 0

    # 실질 수익률: 미청산 포지션 평가손익 반영 (최적화 정렬 기준)
    true_equity = cash
    if open_pos:
        true_equity = cash * (1 + CFG["risk_per_trade"]*open_pos["unrealized_pct"]/100)
    return {
        "final_cash": round(cash,0),
        "total_return_pct": round((cash/CFG["starting_cash"]-1)*100,2),
        "true_return_pct": round((true_equity/CFG["starting_cash"]-1)*100,2),
        "mdd_pct": round(mdd,2),
        "n_trades": len(trades),
        "win_rate": round(len(wins)/len(trades)*100,1) if trades else 0,
        "profit_factor": round(pf,2),
        "long_n": len([t for t in trades if t["side"]=="long"]),
        "short_n": len([t for t in trades if t["side"]=="short"]),
        "avg_hold_days": round(avg_hold,1),
        "longest_hold_days": longest_hold,
        "worst_underwater_pct": round(worst_underwater_pct,1),
        "open_position": open_pos,
        "yearly": {yr:{"trades":d["trades"],
                       "win_rate": round(d["wins"]/d["trades"]*100,1),
                       "pnl": round(d["pnl"],0)} for yr,d in yearly.items()},
        "trades": trades,
    }

def print_result(res: dict):
    print(f"\n{'='*62}")
    print(f"  일일 역추세 스윙 백테스트 (합성 2배 레버리지/인버스)")
    dir_txt = []
    if CFG["trade_long"]: dir_txt.append(f"하락 {CFG['entry_drop_pct']}%↓→롱")
    if CFG["trade_short"]: dir_txt.append(f"상승 {CFG['entry_rise_pct']}%↑→숏")
    print(f"  진입: {' / '.join(dir_txt)} (종가)")
    stop_txt = f"-{CFG['stop_pct']}%" if CFG['stop_pct']>0 else "없음(위험)"
    hold_txt = f"{CFG['max_hold_days']}일" if CFG['max_hold_days']>0 else "무제한(명세)"
    print(f"  익절: +{CFG['target_pct']}% / 손절: {stop_txt} / 최대보유: {hold_txt}")
    print(f"{'='*62}")
    print(f"  거래: {res['n_trades']}회 (롱 {res['long_n']} / 숏 {res['short_n']})")
    print(f"  승률: {res['win_rate']:.1f}%  |  PF: {res['profit_factor']:.2f}")
    print(f"  평균 보유: {res['avg_hold_days']}일  |  최장 물림: {res['longest_hold_days']}일")
    print(f"  보유 중 최대 평가손실: {res['worst_underwater_pct']:.1f}%  ← 손절 없음의 실제 고통")
    print(f"\n  [최종 결과]")
    print(f"  최종 자본: {res['final_cash']:,.0f}원 ({res['total_return_pct']:+.1f}%)")
    print(f"  MDD: -{res['mdd_pct']:.1f}%")
    if res.get("open_position"):
        op = res["open_position"]
        true_equity = res["final_cash"] * (1 + CFG["risk_per_trade"]*op["unrealized_pct"]/100)
        true_ret = (true_equity/CFG["starting_cash"]-1)*100
        print(f"  ⚠️ 기간말 미청산: {op['side']} {op['entry_date']} 진입, "
              f"{op['days']}일째, 평가 {op['unrealized_pct']:+.1f}%")
        print(f"  🚨 실질 최종 자산(미청산 반영): {true_equity:,.0f}원 ({true_ret:+.1f}%)"
              f"  ← 이게 진짜 성과")
    if res.get("yearly"):
        print(f"\n  [연도별 성과]")
        for yr in sorted(res["yearly"].keys()):
            d = res["yearly"][yr]
            mark = "[+]" if d["pnl"]>0 else "[-]"
            print(f"  {yr}: {d['trades']:>3}회  승률 {d['win_rate']:>5.1f}%  "
                  f"{mark}{d['pnl']:>+12,.0f}원")
    print(f"\n  [평가]")
    if res['profit_factor'] >= 1.3 and res.get('true_return_pct', res['total_return_pct']) > 0:
        print(f"  [OK] 손익비 양호 (PF {res['profit_factor']:.2f})")
    elif res['profit_factor'] >= 1.0:
        print(f"  [~] 손익비 보통 (PF {res['profit_factor']:.2f})")
    else:
        print(f"  [X] 손익비 미달 (PF {res['profit_factor']:.2f})")
    print(f"{'='*62}")

def optimize(dates, idx_close, lev_ohlc, inv_ohlc, start_date=None, min_trades=10):
    print(f"\n  파라미터 최적화 탐색 (진입임계/목표/손절)\n")
    results = []
    for th in [0.5, 1.0, 1.5, 2.0, 3.0]:
        for tg in [3.0, 5.0, 7.0]:
            for st in [0.0, 5.0, 10.0]:
                CFG["entry_drop_pct"] = CFG["entry_rise_pct"] = th
                CFG["target_pct"], CFG["stop_pct"] = tg, st
                r = run_backtest(dates, idx_close, lev_ohlc, inv_ohlc, start_date)
                if r["n_trades"] < min_trades:
                    continue
                results.append((th, tg, st, r))
    results.sort(key=lambda x: (x[3]["true_return_pct"]), reverse=True)
    if not results:
        print(f"  ⚠️ 최소 거래 {min_trades}회 만족 조합 없음")
        return results
    print(f"  {'임계%':<7}{'목표%':<7}{'손절%':<8}{'거래':<6}{'승률':<8}{'PF':<7}{'실질수익':<10}{'최장물림'}")
    for th, tg, st, r in results[:10]:
        st_txt = f"{st:.0f}" if st>0 else "없음"
        print(f"  {th:<7}{tg:<7}{st_txt:<8}{r['n_trades']:<6}{r['win_rate']:<7.1f}%"
              f"{r['profit_factor']:<7.2f}{r['true_return_pct']:+9.1f}% {r['longest_hold_days']}일")
    best = results[0]
    CFG["entry_drop_pct"] = CFG["entry_rise_pct"] = best[0]
    CFG["target_pct"], CFG["stop_pct"] = best[1], best[2]
    print(f"\n  최적: 임계 ±{best[0]}% / 목표 +{best[1]}% / "
          f"손절 {'-'+str(best[2])+'%' if best[2]>0 else '없음'}")
    return results

# ══════════════════════════════════════════════════════
#  실행
# ══════════════════════════════════════════════════════
if __name__ == "__main__":
    args = sys.argv[1:]
    if "--years" in args:
        try: CFG["years"] = float(args[args.index("--years")+1])
        except: pass
    if "--since" in args:
        try: CFG["since_date"] = args[args.index("--since")+1]
        except: pass
    if "--stop" in args:
        try: CFG["stop_pct"] = float(args[args.index("--stop")+1])
        except: pass
    if "--target" in args:
        try: CFG["target_pct"] = float(args[args.index("--target")+1])
        except: pass
    if "--long-only" in args:
        CFG["trade_short"] = False
    if "--short-only" in args:
        CFG["trade_long"] = False
    min_trades = 10
    if "--min-trades" in args:
        try: min_trades = int(args[args.index("--min-trades")+1])
        except: pass
    do_optimize = "--optimize" in args

    if CFG["since_date"]:
        since_dt = datetime.strptime(CFG["since_date"], "%Y%m%d")
        days_target = (datetime.now() - (since_dt - timedelta(days=CFG["warmup_days"]))).days
        period_label = f"{CFG['since_date']} 이후"
    else:
        days_target = int(CFG["years"]*252)
        period_label = f"최근 {CFG['years']:.0f}년"

    print(f"\n{'='*62}")
    print(f"  [일일 역추세 스윙] 코스피200 지수 수집 → 2배 합성")
    print(f"  기간: {period_label}")
    print(f"{'='*62}")

    if not get_token():
        print("  ❌ 토큰 발급 실패")
        sys.exit(1)

    raw = fetch_index_daily(CFG["index_code"], days_target)
    idx_ohlc = []
    for c in raw:
        try:
            idx_ohlc.append({
                "date": c["stck_bsop_date"],
                "o": float(c.get("bstp_nmix_oprc", 0)),
                "h": float(c.get("bstp_nmix_hgpr", 0)),
                "l": float(c.get("bstp_nmix_lwpr", 0)),
                "c": float(c.get("bstp_nmix_prpr", 0)),
            })
        except (TypeError, ValueError):
            continue
    idx_ohlc = [r for r in idx_ohlc if r["c"] > 0]
    dates = [r["date"] for r in idx_ohlc]
    idx_close = [r["c"] for r in idx_ohlc]
    print(f"  지수 {len(dates)}일 수집 ({dates[0] if dates else '?'} ~ {dates[-1] if dates else '?'})")
    if len(dates) < 30:
        print("  ❌ 데이터 부족")
        sys.exit(1)

    print("  2배 레버리지 합성 / 인버스 2배 합성...")
    lev_ohlc = synthesize(idx_ohlc, +2.0)
    inv_ohlc = synthesize(idx_ohlc, -2.0)

    if do_optimize:
        optimize(dates, idx_close, lev_ohlc, inv_ohlc, CFG["since_date"], min_trades)
        print(f"\n  === 최적 파라미터로 최종 백테스트 ===")

    res = run_backtest(dates, idx_close, lev_ohlc, inv_ohlc, CFG["since_date"])
    print_result(res)

    if res["trades"]:
        print(f"\n  [최근 거래 10건]")
        for t in res["trades"][-10:]:
            mark = "🟢" if t["pnl"] > 0 else "🔴"
            print(f"  {mark} {t['entry_date']}→{t['date']} {t['side']:5s} "
                  f"보유{t['held_days']:>3}일 {t['change_pct']:+5.1f}% "
                  f"{t['pnl']:>+10,.0f}원 ({t['reason']})")

    with open("kr_daily_swing_backtest.json", "w", encoding="utf-8") as f:
        json.dump({"config": {k:v for k,v in CFG.items()},
                   "result": {k:v for k,v in res.items() if k!="trades"}},
                  f, ensure_ascii=False, indent=2)
    print(f"\n  💾 결과 저장: kr_daily_swing_backtest.json")
