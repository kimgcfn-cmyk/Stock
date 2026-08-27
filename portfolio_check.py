"""
통합 실잔고 조회 (portfolio_check.py)
================================================================
증권사/거래소의 "실제 계좌 잔고"를 직접 조회해서 한눈에 보여준다.
봇들의 JSON 기록이 아니라 진짜 계좌 데이터 기준.

[조회 대상]
  1. 한국주식: KIS 국내주식 잔고 (계좌: KIS_ACCOUNT_NO)
  2. 미국주식: KIS 해외주식 잔고 (같은 계좌, 미국 거래소)
  3. 코인: Bitget 잔고 (현물 + 선물) — .env에 API 키 필요

[.env 필요 항목]
  KIS_APP_KEY / KIS_APP_SECRET / KIS_ACCOUNT_NO (이미 있음)
  BITGET_API_KEY / BITGET_API_SECRET / BITGET_PASSPHRASE (코인 조회용)

실행:
  python portfolio_check.py           # 화면 출력
  python portfolio_check.py --send    # 텔레그램 발송 (한국주식 봇으로)

cron (선택 — 매일 아침 자산 리포트, UTC 23:00 = KST 08:00):
  0 23 * * * cd /home/ec2-user/trading-bot && venv/bin/python portfolio_check.py --send >> logs/portfolio.log 2>&1
"""

import sys, os
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

try:
    from config import (
        KIS_APP_KEY as APP_KEY,
        KIS_APP_SECRET as APP_SECRET,
        KIS_IS_VIRTUAL as IS_VIRTUAL,
    )
except ImportError:
    print("config.py 필요"); APP_KEY = APP_SECRET = ""; IS_VIRTUAL = True

try:
    from config import KIS_ACCOUNT_NO
except ImportError:
    KIS_ACCOUNT_NO = ""
_a = KIS_ACCOUNT_NO.split("-") if "-" in KIS_ACCOUNT_NO else [KIS_ACCOUNT_NO, "01"]
CANO, ACNT_PRDT = (_a[0], _a[1]) if len(_a) >= 2 else (KIS_ACCOUNT_NO, "01")

# Bitget 키 (.env → config._get 또는 환경변수)
def _env(name: str) -> str:
    try:
        import config as _c
        if hasattr(_c, "_get"):
            v = _c._get(name)
            if v: return v
    except Exception:
        pass
    return os.environ.get(name, "")

BITGET_KEY = _env("BITGET_API_KEY")
BITGET_SECRET = _env("BITGET_API_SECRET") or _env("BITGET_SECRET_KEY")
BITGET_PASS = _env("BITGET_PASSPHRASE") or _env("BITGET_PASSWORD")

# 텔레그램 (봇1=COIN 우선 사용 — 코인알림이 디스코드로 이전되며 유휴화됨)
TG_TOKEN = TG_CHAT = ""
try:
    import config as _cfg
    for tk, ch in [("TELEGRAM_TOKEN_COIN","TELEGRAM_CHAT_ID_COIN"),
                   ("TELEGRAM_TOKEN_KR","TELEGRAM_CHAT_ID_KR"),
                   ("TELEGRAM_TOKEN_US","TELEGRAM_CHAT_ID_US"),
                   ("TELEGRAM_TOKEN","TELEGRAM_CHAT_ID")]:
        if hasattr(_cfg, tk) and getattr(_cfg, tk):
            TG_TOKEN = getattr(_cfg, tk); TG_CHAT = getattr(_cfg, ch, ""); break
except ImportError:
    pass

BASE_URL = "https://openapivts.koreainvestment.com:29443" if IS_VIRTUAL \
           else "https://openapi.koreainvestment.com:9443"

_token = None
def get_token():
    """토큰 발급. KIS는 1분당 1회 발급 제한이 있어(5분 cron과 충돌 가능),
    거부되면 65초 대기 후 1회 자동 재시도한다."""
    global _token
    if _token: return _token
    import time as _t
    for attempt in range(2):
        try:
            r = requests.post(f"{BASE_URL}/oauth2/tokenP", json={
                "grant_type":"client_credentials","appkey":APP_KEY,"appsecret":APP_SECRET,
            }, timeout=15)
            d = r.json()
            _token = d.get("access_token")
            if _token:
                return _token
            err = d.get("error_description") or d.get("msg1") or str(d)
            print(f"  토큰 발급 거부: {err}")
            if attempt == 0:
                print("  → 1분당 1회 제한일 가능성 (5분 cron과 충돌). 65초 대기 후 재시도...")
                _t.sleep(65)
        except Exception as e:
            print(f"  토큰 오류: {e}")
            if attempt == 0:
                _t.sleep(65)
    return None

def _h(tr):
    return {"Content-Type":"application/json","Authorization":f"Bearer {get_token()}",
            "appkey":APP_KEY,"appsecret":APP_SECRET,"tr_id":tr}

# ══════════════════════════════════════════════════════
#  1) 한국주식 잔고 (KIS 국내)
# ══════════════════════════════════════════════════════
def get_kr_holdings() -> tuple:
    """반환: (보유리스트, 요약dict) — 실패 시 ([], None)"""
    tr = "VTTC8434R" if IS_VIRTUAL else "TTTC8434R"
    url = f"{BASE_URL}/uapi/domestic-stock/v1/trading/inquire-balance"
    try:
        r = requests.get(url, headers=_h(tr), params={
            "CANO": CANO, "ACNT_PRDT_CD": ACNT_PRDT,
            "AFHR_FLPR_YN":"N","OFL_YN":"","INQR_DVSN":"02","UNPR_DVSN":"01",
            "FUND_STTL_ICLD_YN":"N","FNCG_AMT_AUTO_RDPT_YN":"N","PRCS_DVSN":"00",
            "CTX_AREA_FK100":"","CTX_AREA_NK100":"",
        }, timeout=15)
        d = r.json()
        if d.get("rt_cd") != "0":
            print(f"  국내잔고 오류: {d.get('msg1','')}")
            return [], None
        items = []
        for x in d.get("output1", []):
            qty = float(x.get("hldg_qty", 0) or 0)
            if qty <= 0: continue
            items.append({
                "name": x.get("prdt_name","?"), "code": x.get("pdno",""),
                "qty": qty,
                "avg": float(x.get("pchs_avg_pric",0) or 0),
                "cur": float(x.get("prpr",0) or 0),
                "pnl_pct": float(x.get("evlu_pfls_rt",0) or 0),
                "eval": float(x.get("evlu_amt",0) or 0),
            })
        summ = (d.get("output2") or [{}])[0]
        summary = {
            "total_eval": float(summ.get("tot_evlu_amt",0) or 0),
            "cash": float(summ.get("dnca_tot_amt",0) or 0),
        }
        return items, summary
    except Exception as e:
        print(f"  국내잔고 예외: {e}")
        return [], None

# ══════════════════════════════════════════════════════
#  2) 미국주식 잔고 (KIS 해외)
# ══════════════════════════════════════════════════════
def get_us_holdings() -> list:
    tr = "VTTS3012R" if IS_VIRTUAL else "TTTS3012R"
    url = f"{BASE_URL}/uapi/overseas-stock/v1/trading/inquire-balance"
    items = []
    seen = set()   # 같은 종목이 여러 거래소 조회에서 중복 반환되는 것 방지
    for excg in ["NASD", "NYSE", "AMEX"]:
        try:
            r = requests.get(url, headers=_h(tr), params={
                "CANO": CANO, "ACNT_PRDT_CD": ACNT_PRDT,
                "OVRS_EXCG_CD": excg, "TR_CRCY_CD": "USD",
                "CTX_AREA_FK200":"", "CTX_AREA_NK200":"",
            }, timeout=15)
            d = r.json()
            if d.get("rt_cd") != "0":
                continue
            for x in d.get("output1", []):
                qty = float(x.get("ovrs_cblc_qty", 0) or 0)
                code = x.get("ovrs_pdno","")
                if qty <= 0 or code in seen:
                    continue
                seen.add(code)
                items.append({
                    "name": x.get("ovrs_item_name","?"),
                    "code": code,
                    "qty": qty,
                    "avg": float(x.get("pchs_avg_pric",0) or 0),
                    "cur": float(x.get("now_pric2",0) or 0),
                    "pnl_pct": float(x.get("evlu_pfls_rt",0) or 0),
                    "eval_usd": float(x.get("ovrs_stck_evlu_amt",0) or 0),
                })
        except Exception as e:
            print(f"  해외잔고({excg}) 예외: {e}")
    return items

# ══════════════════════════════════════════════════════
#  3) 코인 잔고 (Bitget — ccxt)
# ══════════════════════════════════════════════════════
def get_coin_holdings() -> tuple:
    """반환: (현물리스트, 선물잔고리스트, 선물포지션리스트, 오류메시지 or None)
    ※ 선물은 fetch_balance만으로는 증거금(현금)만 보이고 실제 포지션(BTC/ETH 등
    레버리지 진입분)은 안 잡히므로 fetch_positions를 별도로 조회한다."""
    if not BITGET_KEY or not BITGET_SECRET:
        return [], [], [], ".env에 BITGET_API_KEY / BITGET_API_SECRET / BITGET_PASSPHRASE 필요"
    try:
        import ccxt
    except ImportError:
        return [], [], [], "ccxt 미설치 (venv/bin/pip install ccxt)"
    spot_items, swap_bal_items, positions = [], [], []
    try:
        ex = ccxt.bitget({"apiKey": BITGET_KEY, "secret": BITGET_SECRET,
                          "password": BITGET_PASS, "enableRateLimit": True})
        bal = ex.fetch_balance()
        for coin, amt in (bal.get("total") or {}).items():
            if amt and amt > 0:
                spot_items.append({"coin": coin, "amount": amt})
        try:
            bal2 = ex.fetch_balance({"type": "swap"})
            for coin, amt in (bal2.get("total") or {}).items():
                if amt and amt > 0:
                    swap_bal_items.append({"coin": coin, "amount": amt})
        except Exception:
            pass
        # 실제 진입한 선물 포지션 (증거금이 아니라 방향/수량/손익)
        try:
            poss = ex.fetch_positions()
            for p in poss:
                contracts = p.get("contracts") or 0
                if contracts and contracts > 0:
                    positions.append({
                        "symbol": p.get("symbol", "?"),
                        "side": p.get("side", "?"),
                        "contracts": contracts,
                        "entry": p.get("entryPrice"),
                        "mark": p.get("markPrice"),
                        "pnl_pct": p.get("percentage"),
                        "unrealized": p.get("unrealizedPnl"),
                    })
        except Exception as e:
            print(f"  선물 포지션 조회 예외: {e}")
        return spot_items, swap_bal_items, positions, None
    except Exception as e:
        return [], [], [], f"Bitget 조회 오류: {e}"

# ══════════════════════════════════════════════════════
#  출력 / 텔레그램
# ══════════════════════════════════════════════════════
def _fmt_qty(q: float) -> str:
    """수량 표시: 정수면 그대로, 소수점 있으면 불필요한 0은 제거하고 최대 4자리까지"""
    if q == int(q):
        return f"{int(q):,}"
    s = f"{q:,.4f}".rstrip("0").rstrip(".")
    return s

def send_telegram(text: str):
    if not TG_TOKEN:
        print("(텔레그램 미설정)"); return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id":TG_CHAT,"text":text,"parse_mode":"HTML"}, timeout=10)
    except Exception as e:
        print(f"텔레그램 오류: {e}")

def build_report() -> str:
    kst = (datetime.now(timezone.utc)+timedelta(hours=9)).strftime("%Y-%m-%d %H:%M")
    lines = [f"📊 <b>통합 실잔고 리포트</b> ({kst} KST)", ""]

    # 한국주식
    kr, kr_sum = get_kr_holdings()
    lines.append("🇰🇷 <b>한국주식</b>")
    if kr:
        for x in kr:
            lines.append(f"  {x['name']}: {_fmt_qty(x['qty'])}주 "
                         f"(평단 {x['avg']:,.0f} → 현재 {x['cur']:,.0f}, {x['pnl_pct']:+.1f}%)")
        if kr_sum:
            lines.append(f"  💰 평가총액 {kr_sum['total_eval']:,.0f}원 / 예수금 {kr_sum['cash']:,.0f}원")
    else:
        lines.append("  보유 없음 (또는 조회 실패)")
    lines.append("")

    # 미국주식
    us = get_us_holdings()
    lines.append("🇺🇸 <b>미국주식</b>")
    if us:
        for x in us:
            lines.append(f"  {x['code']} {x['name']}: {_fmt_qty(x['qty'])}주 "
                         f"(평단 ${x['avg']:,.2f} → ${x['cur']:,.2f}, {x['pnl_pct']:+.1f}%)")
    else:
        lines.append("  보유 없음 (또는 조회 실패)")
    lines.append("")

    # 코인
    spot, swap, positions, err = get_coin_holdings()
    lines.append("🪙 <b>코인 (Bitget)</b>")
    if err:
        lines.append(f"  ⚠️ {err}")
    else:
        if spot:
            lines.append("  [현물] " + ", ".join(f"{x['coin']} {_fmt_qty(x['amount'])}" for x in spot))
        if positions:
            lines.append("  [선물 포지션]")
            for p in positions:
                side_kr = "롱" if p["side"] == "long" else "숏" if p["side"] == "short" else p["side"]
                entry = f"{p['entry']:,.2f}" if p['entry'] else "?"
                mark = f"{p['mark']:,.2f}" if p['mark'] else "?"
                pnl = f"{p['pnl_pct']:+.1f}%" if p['pnl_pct'] is not None else "?"
                lines.append(f"    {p['symbol']} {side_kr} {_fmt_qty(p['contracts'])} "
                             f"(진입 {entry} → 현재 {mark}, {pnl})")
        elif swap:
            lines.append("  [선물 증거금] " + ", ".join(f"{x['coin']} {_fmt_qty(x['amount'])}" for x in swap)
                         + " (오픈 포지션 없음)")
        if not spot and not positions and not swap:
            lines.append("  보유 없음")

    return "\n".join(lines)

if __name__ == "__main__":
    if not get_token():
        print("❌ KIS 토큰 실패"); sys.exit(1)
    report = build_report()
    print(report.replace("<b>","").replace("</b>",""))
    if "--send" in sys.argv:
        send_telegram(report)
        print("\n✅ 텔레그램 발송 완료")
