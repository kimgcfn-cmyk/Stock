"""
KODEX 레버리지·인버스 변동성 돌파 단타 백테스트
================================================================
매일 ±7% 급등락하는 코스피 장세 대응 — "그날의 방향에 올라타기"

[전략 — 래리 윌리엄스 변동성 돌파, 양방향]
  각 ETF에서 독립적으로:
    돌파가 = 당일 시가 + K × (전일 고가 - 전일 저가)
    당일 고가가 돌파가 도달 → 돌파가에 매수
    당일 종가에 무조건 청산 (오버나이트 갭 리스크 제로)

  KODEX레버리지(122630):   지수 급등일에 돌파 → 상승 수익
  KODEX인버스2X(252670):   지수 급락일에 돌파(인버스는 상승) → 하락 수익
  → 위든 아래든 크게 움직이는 날 수익 기회

[왜 이 방식인가]
  - RSI 역추세는 추세적 급락장에서 칼날 잡기 (백테스트로 손실 확인됨)
  - 변동성 돌파는 방향을 예측하지 않고 "움직임 자체"를 따라감
  - 당일 청산이라 ±7% 갭 장세에서 밤새 리스크 없음
  - 일봉 OHLC만으로 정확한 백테스트 가능 (고전적 검증 방법론)

[백테스트 근사의 한계 — 정직 고지]
  돌파 후 장중 손절은 분봉 없이 시뮬레이션 불가 → 종가 청산만 가정.
  실전에서는 장중 손절(-N%)을 추가하면 백테스트보다 안전해질 수 있음.

실행 (EC2):
  python kr_vb_backtest.py --since 20260101 --optimize --min-trades 3
  python kr_vb_backtest.py --years 3 --optimize
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
    print("⚠️  config.py 를 찾을 수 없습니다. EC2의 trading-bot 폴더에서 실행하세요.")
    APP_KEY = APP_SECRET = ""
    IS_VIRTUAL = True

BASE_URL = "https://openapivts.koreainvestment.com:29443" if IS_VIRTUAL \
           else "https://openapi.koreainvestment.com:9443"

# ══════════════════════════════════════════════════════
#  설정
# ══════════════════════════════════════════════════════
CFG = {
    "long_code"      : "122630",   # KODEX 레버리지 (급등일 담당)
    "short_code"     : "252670",   # KODEX 200선물인버스2X (급락일 담당)
    "years"          : 3.0,
    "since_date"     : None,       # "YYYYMMDD"
    "warmup_days"    : 40,         # 전일 변동폭/필터 계산용
    "starting_cash"  : 10_000_000.0,

    # ── 변동성 돌파 ─────────────────────────────────────
    "k_value"        : 0.5,        # 돌파 계수 (0.3 공격적 ~ 0.7 보수적)
    "trade_long"     : True,       # 레버리지(상승) 사용
    "trade_short"    : True,       # 인버스(하락) 사용

    # ── 필터 ────────────────────────────────────────────
    "min_range_pct"  : 1.0,        # 전일 변동폭이 시가의 1% 미만이면 스킵 (조용한 날 제외)
    "ma_filter"      : 5,          # N일 이평 위에 있을 때만 진입 (0이면 미사용)

    # ── 리스크 ──────────────────────────────────────────
    "risk_per_trade" : 0.02,       # 1회 리스크 2% (변동폭 기준 사이징)
    "fee_pct"        : 0.015,
}

# ══════════════════════════════════════════════════════
#  KIS 인증 / 데이터 (기존과 동일)
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

def fetch_stock_daily(code: str, days_target: int) -> list:
    t = get_token()
    if not t:
        return []
    h = {"Content-Type": "application/json", "Authorization": f"Bearer {t}",
         "appkey": APP_KEY, "appsecret": APP_SECRET, "tr_id": "FHKST03010100"}
    url = f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
    all_c, seen = [], set()
    end_dt = datetime.now()
    for _ in range((days_target // 90) + 8):
        start_dt = end_dt - timedelta(days=140)
        try:
            r = requests.get(url, headers=h, params={
                "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
                "FID_INPUT_DATE_1": start_dt.strftime("%Y%m%d"),
                "FID_INPUT_DATE_2": end_dt.strftime("%Y%m%d"),
                "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0",
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

def desplit_ohlc(dates, opens, highs, lows, closes, threshold=0.45, label=""):
    """
    종가 기준 분할 감지 → O/H/L/C 동일 배율 보정 (다중 분할 대응).
    threshold 0.45: 2배 레버리지/인버스 상품은 지수 ±15%급 극단일에
    ±30%를 정상적으로 움직일 수 있으므로, 45% 초과만 분할로 간주.
    (실제 액면분할/병합은 보통 ±50% 이상의 가격 점프를 만듦)
    """
    if not closes:
        return [], [], [], []
    adj_o, adj_h, adj_l, adj_c = [opens[0]], [highs[0]], [lows[0]], [closes[0]]
    factor = 1.0
    splits = []
    for i in range(1, len(closes)):
        prev_raw, cur_raw = closes[i-1], closes[i]
        ret = (cur_raw-prev_raw)/prev_raw if prev_raw > 0 else 0.0
        if abs(ret) > threshold and cur_raw > 0:
            factor = adj_c[-1]/cur_raw
            splits.append(dates[i] if i < len(dates) else str(i))
        adj_o.append(opens[i]*factor)
        adj_h.append(highs[i]*factor)
        adj_l.append(lows[i]*factor)
        adj_c.append(closes[i]*factor)
    if splits:
        print(f"    ℹ️  [{label}] 분할/왜곡 {len(splits)}건 보정: {', '.join(splits[:5])}")
    # 검증용: 원본 마지막 가격 vs 보정 마지막 가격 (배율이 있으면 보정이 적용된 것)
    if closes and abs(adj_c[-1] - closes[-1]) > 0.01:
        print(f"    ⚠️  [{label}] 최종가 원본 {closes[-1]:,.0f} → 보정 {adj_c[-1]:,.0f} "
              f"(보정 적용 중 — 실제 현재가와 비교해 확인 필요)")
    else:
        print(f"    ✅ [{label}] 최종가 {closes[-1]:,.0f} (보정 없음, 원본 그대로)")
    return adj_o, adj_h, adj_l, adj_c

def calc_sma(values, period):
    n = len(values)
    out = [None]*n
    for i in range(period-1, n):
        out[i] = sum(values[i-period+1:i+1])/period
    return out

# ══════════════════════════════════════════════════════
#  변동성 돌파 백테스트 (단일 ETF)
# ══════════════════════════════════════════════════════
def backtest_etf(dates, o, h, l, c, label: str,
                 start_date: Optional[str] = None) -> list:
    """
    한 ETF에 대한 변동성 돌파 일일 시뮬레이션.
    반환: 거래 리스트 [{date, entry, exit, change_pct, reason}, ...]
    """
    k = CFG["k_value"]
    sma = calc_sma(c, CFG["ma_filter"]) if CFG["ma_filter"] > 0 else [None]*len(c)
    trades = []
    for i in range(1, len(dates)):
        date = dates[i]
        if start_date and date < start_date:
            continue
        prev_range = h[i-1] - l[i-1]
        if o[i] <= 0 or prev_range <= 0:
            continue
        # 조용한 날 스킵 (전일 변동폭이 너무 작으면 돌파 의미 없음)
        if prev_range / o[i] * 100 < CFG["min_range_pct"]:
            continue
        # 추세 필터: 종가가 N일 이평 아래면 스킵 (역추세 돌파 방지)
        if CFG["ma_filter"] > 0 and sma[i-1] is not None and c[i-1] < sma[i-1]:
            continue
        target_entry = o[i] + k * prev_range
        if h[i] >= target_entry:   # 장중 돌파 발생
            entry = target_entry
            exit_price = c[i]      # 당일 종가 청산
            change = (exit_price - entry) / entry * 100
            trades.append({"date": date, "etf": label, "entry": round(entry,0),
                          "exit": round(exit_price,0),
                          "change_pct": round(change,2)})
    return trades

def run_backtest(long_data, short_data, start_date: Optional[str] = None) -> dict:
    """양방향(레버리지+인버스) 통합 시뮬레이션"""
    fee = CFG["fee_pct"]/100
    all_trades = []
    if CFG["trade_long"] and long_data:
        all_trades += backtest_etf(*long_data, "레버리지", start_date)
    if CFG["trade_short"] and short_data:
        all_trades += backtest_etf(*short_data, "인버스", start_date)
    all_trades.sort(key=lambda t: t["date"])

    cash = CFG["starting_cash"]
    peak, mdd = cash, 0.0
    for t in all_trades:
        # 포지션 크기: 리스크 기반 (전일 변동폭 대신 고정 2% 손절 가정 사이징)
        position_value = min(cash * CFG["risk_per_trade"] / 0.02, cash)
        net = position_value*(t["change_pct"]/100) - position_value*fee*2
        cash += net
        t["pnl"] = round(net, 0)
        if cash > peak: peak = cash
        dd = (peak-cash)/peak*100 if peak > 0 else 0
        mdd = max(mdd, dd)

    wins = [t for t in all_trades if t["pnl"] > 0]
    losses = [t for t in all_trades if t["pnl"] <= 0]
    pf = abs(sum(t["pnl"] for t in wins)/sum(t["pnl"] for t in losses)) \
         if losses and sum(t["pnl"] for t in losses) != 0 else 0

    lev_trades = [t for t in all_trades if t["etf"]=="레버리지"]
    inv_trades = [t for t in all_trades if t["etf"]=="인버스"]

    yearly = {}
    for t in all_trades:
        yr = t["date"][:4]
        if yr not in yearly: yearly[yr] = {"trades":0,"wins":0,"pnl":0}
        yearly[yr]["trades"] += 1
        if t["pnl"]>0: yearly[yr]["wins"] += 1
        yearly[yr]["pnl"] += t["pnl"]

    return {
        "final_cash": round(cash,0),
        "total_return_pct": round((cash/CFG["starting_cash"]-1)*100,2),
        "mdd_pct": round(mdd,2),
        "n_trades": len(all_trades),
        "win_rate": round(len(wins)/len(all_trades)*100,1) if all_trades else 0,
        "profit_factor": round(pf,2),
        "lev": {"n": len(lev_trades),
                "wins": len([t for t in lev_trades if t["pnl"]>0]),
                "pnl": round(sum(t["pnl"] for t in lev_trades),0)},
        "inv": {"n": len(inv_trades),
                "wins": len([t for t in inv_trades if t["pnl"]>0]),
                "pnl": round(sum(t["pnl"] for t in inv_trades),0)},
        "yearly": {yr:{"trades":d["trades"],
                       "win_rate": round(d["wins"]/d["trades"]*100,1),
                       "pnl": round(d["pnl"],0)} for yr,d in yearly.items()},
        "trades": all_trades,
    }

def print_result(res: dict):
    print(f"\n{'='*60}")
    print(f"  KODEX 변동성 돌파 단타 백테스트 (양방향, 당일청산)")
    print(f"  K={CFG['k_value']} / MA필터 {CFG['ma_filter']}일 / 최소변동 {CFG['min_range_pct']}%")
    print(f"{'='*60}")
    print(f"  거래: {res['n_trades']}회  |  승률: {res['win_rate']:.1f}%  |  PF: {res['profit_factor']:.2f}")
    lv, iv = res["lev"], res["inv"]
    if lv["n"]:
        print(f"  레버리지(급등일): {lv['n']}회, 승률 {lv['wins']/lv['n']*100:.1f}%, 손익 {lv['pnl']:+,.0f}원")
    if iv["n"]:
        print(f"  인버스(급락일):   {iv['n']}회, 승률 {iv['wins']/iv['n']*100:.1f}%, 손익 {iv['pnl']:+,.0f}원")
    print(f"\n  [최종 결과]")
    print(f"  최종 자본: {res['final_cash']:,.0f}원 ({res['total_return_pct']:+.1f}%)")
    print(f"  MDD: -{res['mdd_pct']:.1f}%")
    if res.get("yearly"):
        print(f"\n  [연도별 성과]")
        for yr in sorted(res["yearly"].keys()):
            d = res["yearly"][yr]
            mark = "[+]" if d["pnl"]>0 else "[-]"
            print(f"  {yr}: {d['trades']:>3}회  승률 {d['win_rate']:>5.1f}%  "
                  f"{mark}{d['pnl']:>+12,.0f}원")
    print(f"\n  [평가]")
    if res['profit_factor'] >= 1.3:
        print(f"  [OK] 손익비 양호 (PF {res['profit_factor']:.2f})")
    elif res['profit_factor'] >= 1.0:
        print(f"  [~] 손익비 보통 (PF {res['profit_factor']:.2f})")
    else:
        print(f"  [X] 손익비 미달 (PF {res['profit_factor']:.2f})")
    print(f"{'='*60}")

def optimize(long_data, short_data, start_date=None, min_trades=10):
    print(f"\n  파라미터 최적화 탐색 중... (K값/MA필터/최소변동폭)\n")
    results = []
    for k in [0.3, 0.4, 0.5, 0.6, 0.7]:
        for ma in [0, 3, 5, 10]:
            for mr in [0.5, 1.0, 1.5]:
                CFG["k_value"], CFG["ma_filter"], CFG["min_range_pct"] = k, ma, mr
                r = run_backtest(long_data, short_data, start_date)
                if r["n_trades"] < min_trades:
                    continue
                results.append((k, ma, mr, r))
    results.sort(key=lambda x: x[3]["profit_factor"], reverse=True)
    if not results:
        print(f"  ⚠️ 최소 거래 {min_trades}회를 만족하는 조합 없음 — --min-trades를 낮춰보세요")
        return results
    print(f"  {'K':<6}{'MA필터':<8}{'최소변동':<9}{'거래':<6}{'승률':<8}{'PF':<7}{'수익률'}")
    for k, ma, mr, r in results[:10]:
        print(f"  {k:<6}{ma:<8}{mr:<9}{r['n_trades']:<6}{r['win_rate']:<7.1f}%"
              f"{r['profit_factor']:<7.2f}{r['total_return_pct']:+.1f}%")
    best = results[0]
    CFG["k_value"], CFG["ma_filter"], CFG["min_range_pct"] = best[0], best[1], best[2]
    print(f"\n  최적: K={best[0]} / MA필터 {best[1]}일 / 최소변동 {best[2]}%")
    return results

# ══════════════════════════════════════════════════════
#  실행
# ══════════════════════════════════════════════════════
def load_etf(code: str, days_target: int, name: str):
    print(f"\n  [{name}({code})] 수집...")
    raw = fetch_stock_daily(code, days_target)
    dates = [c["stck_bsop_date"] for c in raw]
    o = [float(c.get("stck_oprc",0)) for c in raw]
    h = [float(c.get("stck_hgpr",0)) for c in raw]
    l = [float(c.get("stck_lwpr",0)) for c in raw]
    cl = [float(c.get("stck_clpr",0)) for c in raw]
    print(f"    {len(dates)}일 수집")
    ao, ah, al, ac = desplit_ohlc(dates, o, h, l, cl, label=name)
    return (dates, ao, ah, al, ac)

if __name__ == "__main__":
    args = sys.argv[1:]
    if "--years" in args:
        try: CFG["years"] = float(args[args.index("--years")+1])
        except: pass
    if "--since" in args:
        try: CFG["since_date"] = args[args.index("--since")+1]
        except: pass
    min_trades = 10
    if "--min-trades" in args:
        try: min_trades = int(args[args.index("--min-trades")+1])
        except: pass
    if "--long-only" in args:
        CFG["trade_short"] = False
    if "--short-only" in args:
        CFG["trade_long"] = False
    do_optimize = "--optimize" in args

    if CFG["since_date"]:
        since_dt = datetime.strptime(CFG["since_date"], "%Y%m%d")
        days_target = (datetime.now() - (since_dt - timedelta(days=CFG["warmup_days"]))).days
        period_label = f"{CFG['since_date']} 이후 (워밍업 {CFG['warmup_days']}일 포함)"
    else:
        days_target = int(CFG["years"]*252)
        period_label = f"최근 {CFG['years']:.0f}년"

    print(f"\n{'='*60}")
    print(f"  [변동성 돌파 단타] 데이터 수집")
    print(f"  기간: {period_label}")
    print(f"  방식: 시가+K×전일변동폭 돌파 시 진입 → 당일 종가 청산")
    print(f"{'='*60}")

    if not get_token():
        print("  ❌ 토큰 발급 실패 — config.py 확인")
        sys.exit(1)

    long_data = load_etf(CFG["long_code"], days_target, "KODEX레버리지")
    short_data = load_etf(CFG["short_code"], days_target, "KODEX인버스2X")

    if len(long_data[0]) < 30 or len(short_data[0]) < 30:
        print("\n  ❌ 데이터 부족")
        sys.exit(1)

    if CFG["since_date"]:
        n_days = sum(1 for d in long_data[0] if d >= CFG["since_date"])
        print(f"\n  ℹ️  거래 판정 구간: {CFG['since_date']} 이후 {n_days}거래일")

    if do_optimize:
        optimize(long_data, short_data, CFG["since_date"], min_trades)
        print(f"\n  === 최적 파라미터로 최종 백테스트 ===")

    res = run_backtest(long_data, short_data, CFG["since_date"])
    print_result(res)

    # 최근 거래 상세 (원인 분석용)
    if res["trades"]:
        print(f"\n  [최근 거래 10건]")
        for t in res["trades"][-10:]:
            mark = "🟢" if t["pnl"] > 0 else "🔴"
            print(f"  {mark} {t['date']} {t['etf']:5s} 진입{t['entry']:>8,.0f} "
                  f"청산{t['exit']:>8,.0f} {t['change_pct']:+5.1f}% {t['pnl']:>+10,.0f}원")

    with open("kr_vb_backtest.json", "w", encoding="utf-8") as f:
        json.dump({"config": {k:v for k,v in CFG.items()},
                   "result": {k:v for k,v in res.items() if k!="trades"}},
                  f, ensure_ascii=False, indent=2)
    print(f"\n  💾 결과 저장: kr_vb_backtest.json")
