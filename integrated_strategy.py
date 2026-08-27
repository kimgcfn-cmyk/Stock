"""
통합 투자 전략 시스템 — 스캐너 + AI 분석 + 텔레그램
======================================================
[작동 흐름]
  1. 매일 장 마감 후 자동 실행 (cron)
  2. KIS API로 한국주식 / 미국주식 조건 탐지
  3. 탐지된 종목을 Claude API로 AI 심층 분석
  4. BUY / HOLD / SKIP 판단 + 근거
  5. 텔레그램으로 전체 결과 전송
  6. 사람이 최종 결정 후 수동 매수

[실전 전략 원칙]
  - 자동 주문 없음 (알림 전용)
  - AI 분석은 참고용, 최종 결정은 사람
  - 포지션당 계좌의 10% 이하
  - 동시 보유 최대 5종목

실행:
  python integrated_strategy.py --kr        # 한국주식만
  python integrated_strategy.py --us        # 미국주식만
  python integrated_strategy.py --all       # 전체
  python integrated_strategy.py --test      # 테스트 (API 호출 없이)
"""

import requests, time, json, sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# ══════════════════════════════════════════════════════
#  인증 정보
# ══════════════════════════════════════════════════════
try:
    from config import (
        KIS_APP_KEY as APP_KEY,
        KIS_APP_SECRET as APP_SECRET,
        KIS_IS_VIRTUAL as IS_VIRTUAL,
        TELEGRAM_TOKEN_KR as TELEGRAM_TOKEN, TELEGRAM_CHAT_ID_KR as TELEGRAM_CHAT_ID,
        TELEGRAM_TOKEN_US, TELEGRAM_CHAT_ID_US,
        telegram_configured, kis_configured,
    )
except ImportError:
    print("⚠️  config.py 없음 — .env 파일 확인하세요")
    sys.exit(1)

# 시장별 텔레그램 봇 매핑: 한국(--kr) → 봇3(KR) / 미국(--us) → 봇2(US)
_TELEGRAM_BY_MARKET = {
    "KR": (TELEGRAM_TOKEN, TELEGRAM_CHAT_ID),
    "US": (TELEGRAM_TOKEN_US, TELEGRAM_CHAT_ID_US),
}
_current_market = "KR"   # run() 시작 시 --kr/--us에 맞춰 갱신됨

# Anthropic API 키 (.env: ANTHROPIC_API_KEY=sk-ant-...)
try:
    import config as _cfg
    ANTHROPIC_API_KEY = _cfg._get("ANTHROPIC_API_KEY") if hasattr(_cfg, "_get") else \
                        getattr(_cfg, "ANTHROPIC_API_KEY", "")
except ImportError:
    ANTHROPIC_API_KEY = ""

BASE_URL = "https://openapivts.koreainvestment.com:29443" if IS_VIRTUAL \
           else "https://openapi.koreainvestment.com:9443"

# ══════════════════════════════════════════════════════
#  전략 파라미터 (백테스트로 검증된 값)
# ══════════════════════════════════════════════════════
STRATEGY = {
    # ── 한국주식 탐지 조건 ──────────────────────────
    "kr": {
        "lookback_days"  : 20,
        "surge_pct"      : 35.0,  # 20거래일 내 35%↑ (2026-08-08: 20%→35%, 추천 종목 수 축소)
        "min_price"      : 1000,
        "market"         : "J",   # J: 주식
    },
    # ── 미국주식 탐지 조건 ──────────────────────────
    "us": {
        "lookback_days"  : 20,
        "surge_pct"      : 40.0,  # 20거래일 내 40%↑ (2026-08-08: 25%→40%, 추천 종목 수 축소)
        "min_price"      : 5.0,
    },
    # ── 공통 청산 기준 ──────────────────────────────
    "exit": {
        "hard_stop_pct"  : 10.0,  # 강제손절 -10%
        "ma_short"       : 20,    # MA20 이탈 → 손절
        "ma_long"        : 50,    # MA50 이탈 → 추세청산
        "swing_lookback" : 20,    # 트레일링 스윙저점
        "max_hold_days"  : 60,
    },
    # ── 정량 1차 필터 (KIS 재무데이터, 무료) ──────────
    #   여기를 통과한 종목만 Claude AI 분석(2차, 유료)으로 넘어간다.
    "quant_filter": {
        "exclude_loss": True,      # 적자기업(EPS<=0 또는 PER<=0) 제외
        "max_per": 25.0,          # PER 25 초과는 제외 (2026-08-08: 100→25, 고평가 종목 제외 강화)
    },
    # ── AI 비용 절감 설정 (2025-07-29 추가) ──────────
    "ai": {
        "model"              : "claude-haiku-4-5-20251001",  # Sonnet→Haiku (가장 큰 절감 요인)
        "max_tokens"         : 1200,   # 800→1200 (답변 끝부분 잘림 방지)
        "web_search_max_uses": 2,      # 검색 횟수 제한 (컨텍스트 토큰이 비용의 큰 축)
        "max_ai_calls_per_run": 2,     # 하루 실행당 AI 분석 최대 건수 (급등률 높은 순)
    },
    "position": {
        "size_pct"       : 10.0,  # 계좌의 10%
        "max_positions"  : 5,     # 최대 동시 보유 5종목
    },
}

POSITIONS_FILE = Path("positions.json")
LOG_FILE       = Path("strategy_log.json")

# ══════════════════════════════════════════════════════
#  텔레그램
# ══════════════════════════════════════════════════════
def send_telegram(text: str, parse_mode: str = "HTML") -> bool:
    """_current_market에 따라 봇을 자동 선택 (KR→봇3, US→봇2)"""
    token, chat_id = _TELEGRAM_BY_MARKET.get(_current_market, (TELEGRAM_TOKEN, TELEGRAM_CHAT_ID))
    if not token or not chat_id:
        print(f"\n[텔레그램 미설정: {_current_market}]\n{text}\n")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
        }, timeout=15)
        return r.json().get("ok", False)
    except Exception as e:
        print(f"  ❌ 텔레그램 오류: {e}")
        return False

# ══════════════════════════════════════════════════════
#  KIS API 인증
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

def _hdr(tr_id: str = "") -> Optional[dict]:
    t = get_token()
    if not t:
        return None
    h = {"Content-Type": "application/json",
         "Authorization": f"Bearer {t}",
         "appkey": APP_KEY, "appsecret": APP_SECRET}
    if tr_id:
        h["tr_id"] = tr_id
    return h

# ══════════════════════════════════════════════════════
#  포지션 관리
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
        json.dumps(positions, ensure_ascii=False, indent=2), encoding="utf-8")

def is_already_holding(symbol: str) -> bool:
    return any(p["symbol"] == symbol for p in load_positions())

def holding_count() -> int:
    return len(load_positions())

# ══════════════════════════════════════════════════════
#  AI 심층 분석 (Claude API)
# ══════════════════════════════════════════════════════
def analyze_with_claude(symbol: str, name: str, change_pct: float,
                         market: str = "KR") -> dict:
    """
    Claude API로 탐지된 종목을 심층 분석.
    웹 검색을 활용해 최신 정보를 기반으로 BUY/HOLD/SKIP 판단.

    ⚠️ 이전 버전은 API가 에러를 반환해도(예: 크레딧 부족, 모델명 오류,
    레이트리밋) 예외 없이 조용히 넘어가서 "분석 없음 + 기본값 HOLD"로
    처리되는 문제가 있었다. 이번 버전은 에러를 명시적으로 감지해서
    사유를 그대로 노출한다.
    """
    market_label = "한국주식(KRX)" if market == "KR" else "미국주식(NYSE/NASDAQ)"
    prompt = f"""당신은 증권사 수석 애널리스트입니다.

다음 종목이 쿨라매기 전략 스캐너에 탐지되었습니다.

종목: {name} ({symbol})
시장: {market_label}
탐지 사유: 직전 20거래일 내 {change_pct:.1f}% 상승

지금부터 이 종목에 대해 웹 검색을 통해 최신 정보(최근 증시 뉴스,
종목 관련 뉴스, 그리고 네이버 종목토론방/증권 커뮤니티/투자 카페 등에서
개인투자자들이 실제로 보이는 반응과 여론 분위기)를 수집하고,
다음 6가지를 간결하게 분석해주세요.

1. 급등 이유 (최근 뉴스, 실적, 이슈)
2. 사업 경쟁력 (핵심 사업, 시장 지위)
3. 재무 건전성 (매출 성장, 수익성)
4. 모멘텀 지속 가능성
5. 개인투자자 여론 (종목토론방/커뮤니티 분위기 — 기대감 과열인지,
   신중론이 우세한지, 최근 논쟁거리가 있는지 구체적으로)
6. 주요 리스크

마지막에 반드시 다음 형식으로 결론을 내려주세요:

투자의견: BUY / HOLD / SKIP (하나만 선택)
근거: (검색한 증시/종목 뉴스를 반영해 20~50자 내외로 핵심만 간결하게)
목표가: (현재가 대비 상승여력 %)
리스크: (가장 중요한 리스크 1개)

분석은 한국어로 작성하고, 전체 400자 이내로 최대한 간결하게 (항목별 1줄씩)."""

    if not ANTHROPIC_API_KEY:
        return {"opinion": "HOLD",
               "analysis": "⚠️ AI 분석 불가: .env에 ANTHROPIC_API_KEY 없음",
               "success": False}

    try:
        ai_cfg = STRATEGY["ai"]
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": ai_cfg["model"],
                "max_tokens": ai_cfg["max_tokens"],
                "tools": [{"type": "web_search_20250305", "name": "web_search",
                          "max_uses": ai_cfg["web_search_max_uses"]}],
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=90,
        )
        data = r.json()

        # ── API 에러를 명시적으로 감지 (기존 버그: 여기서 조용히 넘어감) ──
        if "error" in data:
            err = data["error"]
            print(f"    ❌ Claude API 오류 [{err.get('type','?')}]: {err.get('message','')}")
            return {"opinion": "HOLD",
                   "analysis": f"⚠️ AI 분석 실패(API 오류): {err.get('message','알 수 없음')}",
                   "success": False}

        content = data.get("content", [])
        text = " ".join(
            block["text"] for block in content if block.get("type") == "text"
        ).strip()

        if not text:
            # content는 있는데 text 블록이 비어있는 이례적 케이스 — 원인 진단용 로그
            print(f"    ⚠️ {symbol} 응답에 텍스트 없음. "
                  f"stop_reason={data.get('stop_reason')}, "
                  f"블록타입={[b.get('type') for b in content]}")
            return {"opinion": "HOLD",
                   "analysis": "⚠️ AI 분석 실패: 응답에 텍스트 없음 (로그 확인 필요)",
                   "success": False}

        # 투자의견 파싱
        opinion = "HOLD"
        for line in text.split("\n"):
            if "투자의견" in line:
                if "BUY" in line.upper():
                    opinion = "BUY"
                elif "SKIP" in line.upper():
                    opinion = "SKIP"
                break

        return {"opinion": opinion, "analysis": text, "success": True}

    except Exception as e:
        print(f"    ❌ {symbol} 분석 중 예외: {e}")
        return {"opinion": "HOLD", "analysis": f"⚠️ AI 분석 오류(예외): {e}", "success": False}

# ══════════════════════════════════════════════════════
#  미국주식 탐지
# ══════════════════════════════════════════════════════
def get_us_financial_snapshot(symbol: str, excd: str = "NAS") -> Optional[dict]:
    """
    KIS 해외주식 현재가상세에서 PER/PBR/EPS를 가져온다 (정량 1차 필터용).
    ⚠️ 이 API의 정확한 필드명은 국내주식보다 확신도가 낮음 —
      파싱 실패 시 원본 키를 로그로 남기니, 실제 실행 결과를 보고
      필드명이 다르면 바로잡을 것 (KR 버전과 동일한 안전 원칙).
    """
    hdr = _hdr("HHDFS76200200")
    if not hdr:
        return None
    url = f"{BASE_URL}/uapi/overseas-price/v1/quotations/price-detail"
    try:
        r = requests.get(url, headers=hdr, params={
            "AUTH": "", "EXCD": excd, "SYMB": symbol,
        }, timeout=10)
        out = r.json().get("output", {})
    except Exception as e:
        print(f"    ⚠️ {symbol} 미국 재무스냅샷 조회 예외: {e}")
        return None

    if not out:
        return None

    def _f(*keys):
        for k in keys:
            v = out.get(k, "")
            if v not in ("", None):
                try:
                    return float(v)
                except (TypeError, ValueError):
                    continue
        return None

    # 필드명 후보를 여러 개 시도 (KIS 스펙 확신도가 낮아 안전하게)
    per = _f("perx", "per")
    pbr = _f("pbrx", "pbr")
    eps = _f("epsx", "eps")

    if per is None and pbr is None and eps is None:
        print(f"    ⚠️ {symbol} 미국 재무 필드 파싱 실패 — "
              f"원본 키: {list(out.keys())[:20]} (정량필터는 건너뛰고 AI 분석 진행)")
        return None

    return {"per": per, "pbr": pbr, "eps": eps}

def scan_us_stock(symbol: str, name: str) -> Optional[dict]:
    """미국주식 20거래일 내 20%↑ 조건 체크"""
    cfg = STRATEGY["us"]
    hdr = _hdr("HHDFS76240000")
    if not hdr:
        return None

    end_dt   = datetime.now()
    start_dt = end_dt.replace(year=end_dt.year - 1)

    url = f"{BASE_URL}/uapi/overseas-price/v1/quotations/dailyprice"
    params = {
        "AUTH": "",
        "EXCD": "NAS",
        "SYMB": symbol,
        "GUBN": "0",
        "BYMD": "",
        "MODP": "1",
    }
    try:
        r = requests.get(url, headers=hdr, params=params, timeout=10)
        candles = r.json().get("output2", [])
    except Exception:
        return None

    if not candles or len(candles) < cfg["lookback_days"] + 1:
        # NAS 실패 시 NYS 시도
        try:
            params["EXCD"] = "NYS"
            r2 = requests.get(url, headers=hdr, params=params, timeout=10)
            candles = r2.json().get("output2", [])
        except Exception:
            return None

    if len(candles) < cfg["lookback_days"] + 1:
        return None

    candles.sort(key=lambda c: c.get("xymd", ""))
    window = candles[-cfg["lookback_days"]:]
    try:
        p_ago = float(window[0].get("clos", 0))
        p_now = float(window[-1].get("clos", 0))
    except (TypeError, ValueError):
        return None

    if p_ago <= 0 or p_now < cfg["min_price"]:
        return None

    chg = (p_now - p_ago) / p_ago * 100
    if chg < cfg["surge_pct"]:
        return None

    return {"symbol": symbol, "name": name, "market": "US",
            "price": p_now, "change_pct": round(chg, 2)}

# ══════════════════════════════════════════════════════
#  한국주식 탐지
# ══════════════════════════════════════════════════════
def get_kr_financial_snapshot(symbol: str) -> Optional[dict]:
    """
    KIS 국내주식 현재가 조회에서 PER/PBR/EPS/BPS를 가져온다 (무료, 정량 1차 필터용).
    ※ 필드명(per/pbr/eps/bps)은 KIS 통상 스펙 기준 — 실제 실행 결과가 다르면
      아래 print로 원본 구조가 로그에 남으니 그걸 보고 바로잡을 것.
    """
    hdr = _hdr("FHKST01010100")
    if not hdr:
        return None
    url = f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price"
    try:
        r = requests.get(url, headers=hdr, params={
            "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol,
        }, timeout=10)
        out = r.json().get("output", {})
    except Exception as e:
        print(f"    ⚠️ {symbol} 재무스냅샷 조회 예외: {e}")
        return None

    if not out:
        print(f"    ⚠️ {symbol} 재무스냅샷 응답 비어있음")
        return None

    def _f(key):
        try:
            v = out.get(key, "")
            return float(v) if v not in ("", None) else None
        except (TypeError, ValueError):
            return None

    per, pbr, eps, bps = _f("per"), _f("pbr"), _f("eps"), _f("bps")
    if per is None and pbr is None and eps is None:
        print(f"    ⚠️ {symbol} 재무 필드(per/pbr/eps) 전부 파싱 실패 — "
              f"원본 키: {list(out.keys())[:15]}")
        return None

    return {"per": per, "pbr": pbr, "eps": eps, "bps": bps}

def passes_quant_filter(snapshot: Optional[dict]) -> tuple:
    """정량 1차 필터 통과 여부. 반환: (통과여부, 사유문자열)"""
    qf = STRATEGY["quant_filter"]
    if snapshot is None:
        return True, "재무데이터 조회 실패 — 필터 건너뛰고 AI 분석 진행"

    per, eps = snapshot.get("per"), snapshot.get("eps")

    if qf.get("exclude_loss"):
        if (eps is not None and eps <= 0) or (per is not None and per <= 0):
            return False, f"적자기업 (EPS {eps}, PER {per}) — AI 분석 없이 자동 SKIP"

    max_per = qf.get("max_per", 0)
    if max_per > 0 and per is not None and per > max_per:
        return False, f"PER {per:.1f} > {max_per:.0f} (과도한 고평가) — AI 분석 없이 자동 SKIP"

    return True, "정량 필터 통과"

def scan_kr_stock(symbol: str, name: str) -> Optional[dict]:
    """한국주식 20거래일 내 15%↑ 조건 체크"""
    cfg = STRATEGY["kr"]
    hdr = _hdr("FHKST03010100")
    if not hdr:
        return None

    end_dt   = datetime.now()
    start_dt = end_dt.replace(year=end_dt.year - 1)   # 1년치면 충분

    url = f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
    params = {
        "FID_COND_MRKT_DIV_CODE": cfg["market"],
        "FID_INPUT_ISCD": symbol,
        "FID_INPUT_DATE_1": start_dt.strftime("%Y%m%d"),
        "FID_INPUT_DATE_2": end_dt.strftime("%Y%m%d"),
        "FID_PERIOD_DIV_CODE": "D",
        "FID_ORG_ADJ_PRC": "1",
    }
    try:
        r = requests.get(url, headers=hdr, params=params, timeout=10)
        candles = r.json().get("output2", [])
    except Exception:
        return None

    if len(candles) < cfg["lookback_days"] + 1:
        return None

    candles.sort(key=lambda c: c.get("stck_bsop_date", ""))
    window = candles[-cfg["lookback_days"]:]
    try:
        p_ago = float(window[0].get("stck_clpr", 0))
        p_now = float(window[-1].get("stck_clpr", 0))
    except (TypeError, ValueError):
        return None

    if p_ago <= 0 or p_now < cfg["min_price"]:
        return None

    chg = (p_now - p_ago) / p_ago * 100
    if chg < cfg["surge_pct"]:
        return None

    return {"symbol": symbol, "name": name, "market": "KR",
            "price": p_now, "change_pct": round(chg, 2)}

# ══════════════════════════════════════════════════════
#  메인 실행
# ══════════════════════════════════════════════════════
def run(mode: str = "all", test: bool = False):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"\n{'='*60}")
    print(f"  통합 투자 전략 시스템 — {now}")
    print(f"{'='*60}")

    if test:
        print("  [테스트 모드] — 실제 API 호출 없이 흐름만 확인")
        send_telegram(
            f"🧪 <b>테스트 실행</b>\n{now}\n통합 전략 시스템 정상 작동 중"
        )
        return

    if not get_token():
        print("  ❌ KIS 토큰 발급 실패")
        return

    positions = load_positions()
    max_pos = STRATEGY["position"]["max_positions"]

    # 텔레그램: 일일 점검 시작
    send_telegram(
        f"📊 <b>일일 투자 점검 시작</b>\n"
        f"{now}\n"
        f"보유 종목: {len(positions)}/{max_pos}개\n"
        f"스캔 대상: {'한국+미국' if mode=='all' else ('한국' if mode=='kr' else '미국')}"
    )

    signals = []
    global _current_market

    # ── 한국주식 스캔 ──────────────────────────────────
    if mode in ("all", "kr"):
        _current_market = "KR"   # 봇3(KR)로 발송
        print("\n  [한국주식 스캔]")
        cache_file = Path("kr_symbols_cache.json")
        if cache_file.exists():
            kr_symbols = json.loads(cache_file.read_text(encoding="utf-8"))
        else:
            kr_symbols = [
                {"symbol": "005930", "name": "삼성전자"},
                {"symbol": "000660", "name": "SK하이닉스"},
                {"symbol": "035420", "name": "NAVER"},
                {"symbol": "051910", "name": "LG화학"},
                {"symbol": "006400", "name": "삼성SDI"},
            ]

        send_telegram(
            f"🇰🇷 <b>한국주식 탐지 스캔 시작</b>\n"
            f"{now}\n"
            f"{len(kr_symbols)}개 종목\n"
            f"탐지조건: {STRATEGY['kr']['lookback_days']}거래일 내 {STRATEGY['kr']['surge_pct']}%↑"
        )

        for s in kr_symbols:
            if is_already_holding(s["symbol"]):
                continue
            sig = scan_kr_stock(s["symbol"], s["name"])
            if sig:
                signals.append(sig)
                print(f"  🚨 탐지: {s['symbol']} {s['name']} +{sig['change_pct']:.1f}%")
            time.sleep(0.15)

    # ── 미국주식 스캔 ──────────────────────────────────
    if mode in ("all", "us"):
        _current_market = "US"   # 봇2(US)로 발송
        print("\n  [미국주식 스캔]")
        cache_file_us = Path("sp500_symbols_cache.json")
        if cache_file_us.exists():
            us_symbols = json.loads(cache_file_us.read_text(encoding="utf-8"))
        else:
            us_symbols = [
                {"symbol": "AAPL",  "name": "Apple"},
                {"symbol": "MSFT",  "name": "Microsoft"},
                {"symbol": "NVDA",  "name": "NVIDIA"},
                {"symbol": "AMZN",  "name": "Amazon"},
                {"symbol": "GOOGL", "name": "Alphabet"},
                {"symbol": "META",  "name": "Meta"},
                {"symbol": "TSLA",  "name": "Tesla"},
                {"symbol": "AMD",   "name": "AMD"},
            ]

        send_telegram(
            f"🇺🇸 <b>미국주식 탐지 스캔 시작</b>\n"
            f"{now}\n"
            f"{len(us_symbols)}개 종목\n"
            f"탐지조건: {STRATEGY['us']['lookback_days']}거래일 내 {STRATEGY['us']['surge_pct']}%↑"
        )

        for s in us_symbols:
            if is_already_holding(s["symbol"]):
                continue
            sig = scan_us_stock(s["symbol"], s["name"])
            if sig:
                signals.append(sig)
                print(f"  🚨 탐지: {s['symbol']} {s['name']} +{sig['change_pct']:.1f}%")
            time.sleep(0.15)

    # ── 시그널 AI 분석 ─────────────────────────────────
    # 급등률 높은 순으로 정렬 후, 하루 AI 분석 건수를 상한(max_ai_calls_per_run)으로
    # 제한한다. 상한 초과분은 AI 호출 없이 탐지 사실+정량스냅샷만 알린다(비용 절감).
    signals.sort(key=lambda s: s["change_pct"], reverse=True)
    max_ai_calls = STRATEGY["ai"]["max_ai_calls_per_run"]
    ai_call_count = 0

    results = []
    for sig in signals:
        _current_market = sig["market"]   # --all 모드에서 종목별 정확한 봇 선택
        if holding_count() >= max_pos:
            print(f"  ⚠️  최대 보유 종목 수({max_pos}개) 도달 — 추가 탐지 중단")
            break

        # ── 1차: 정량 필터 (KIS 데이터 기반, 무료) ──
        # 한국: PER/PBR/EPS 확신도 높음. 미국: 필드명 확신도 낮아
        # 파싱 실패 시 자동으로 필터를 건너뛰고 AI 분석으로 넘어간다(안전).
        snapshot = None
        if sig["market"] == "KR":
            print(f"\n  📊 정량 필터 확인: {sig['symbol']} {sig['name']}...")
            snapshot = get_kr_financial_snapshot(sig["symbol"])
        elif sig["market"] == "US":
            print(f"\n  📊 정량 필터 확인(미국): {sig['symbol']} {sig['name']}...")
            snapshot = get_us_financial_snapshot(sig["symbol"])

        if sig["market"] in ("KR", "US"):
            ok, reason = passes_quant_filter(snapshot)
            per_txt = f"PER {snapshot['per']:.1f}" if snapshot and snapshot.get('per') is not None else "PER ?"
            pbr_txt = f"PBR {snapshot['pbr']:.2f}" if snapshot and snapshot.get('pbr') is not None else "PBR ?"
            print(f"     {per_txt} / {pbr_txt} → {reason}")

            if not ok:
                # 정량 필터 탈락 — AI 호출 없이 즉시 SKIP 처리 (비용 절감)
                result = {**sig, "opinion": "SKIP", "analysis": reason,
                          "success": True, "quant_only": True}
                results.append(result)

                # 2026-08-27: SKIP은 사유 불문 전부 텔레그램 알림 생략
                # (콘솔 로그와 결과 기록에는 남기되, 의미없는 알림만 줄임)
                print(f"  🔇 {sig['name']}: 정량 필터 SKIP({reason})이라 텔레그램 알림 생략")
                continue

        # ── 2차: AI 심층 분석 (정량 필터 통과분 + 미국주식 전체) ──
        # 하루 AI 호출 상한 체크 — 초과분은 비용 없이 정량정보만 알림
        if ai_call_count >= max_ai_calls:
            print(f"  💰 AI 분석 일일 한도({max_ai_calls}건) 도달 — "
                  f"{sig['symbol']} {sig['name']}는 정량정보만 알림 (비용 절감)")
            result = {**sig, "opinion": "HOLD",
                      "analysis": f"AI 분석 일일 한도({max_ai_calls}건) 초과로 생략",
                      "success": True, "quant_only": True}
            results.append(result)
            cur_symbol_sym = "원" if sig["market"] == "KR" else "$"
            fin_line = ""
            if snapshot:
                per_txt = f"{snapshot['per']:.1f}" if snapshot.get('per') is not None else "?"
                pbr_txt = f"{snapshot['pbr']:.2f}" if snapshot.get('pbr') is not None else "?"
                fin_line = f"PER {per_txt} / PBR {pbr_txt}\n"
            msg = (
                f"🟡 <b>{sig['name']} ({sig['symbol']})</b>\n"
                f"탐지: +{sig['change_pct']:.1f}% (20거래일)\n"
                f"현재가: {sig['price']:,.0f}{cur_symbol_sym}\n"
                f"{fin_line}"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"💰 AI 분석 일일 한도({max_ai_calls}건) 초과 — 정량정보만 참고하세요"
            )
            send_telegram(msg)
            continue

        print(f"\n  🤖 AI 분석 중: {sig['symbol']} {sig['name']}...")
        ai = analyze_with_claude(
            sig["symbol"], sig["name"],
            sig["change_pct"], sig["market"]
        )
        ai_call_count += 1

        result = {**sig, **ai}
        results.append(result)

        # ── 텔레그램 개별 종목 알림 ────────────────────
        opinion = ai["opinion"]
        emoji = "🟢" if opinion == "BUY" else ("🟡" if opinion == "HOLD" else "🔴")

        fin_line = ""
        if snapshot:
            per_txt = f"{snapshot['per']:.1f}" if snapshot.get('per') is not None else "?"
            pbr_txt = f"{snapshot['pbr']:.2f}" if snapshot.get('pbr') is not None else "?"
            fin_line = f"PER {per_txt} / PBR {pbr_txt} (정량필터 통과)\n"

        msg = (
            f"{emoji} <b>{sig['name']} ({sig['symbol']})</b>\n"
            f"탐지: +{sig['change_pct']:.1f}% (20거래일)\n"
            f"현재가: {sig['price']:,.0f}{'원' if sig['market']=='KR' else '$'}\n"
            f"{fin_line}"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"<b>AI 투자의견: {opinion}</b>\n\n"
            f"{ai['analysis']}\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
        )
        if opinion == "BUY":
            msg += (
                f"💡 매수 고려 시:\n"
                f"  - 포지션 크기: 계좌의 {STRATEGY['position']['size_pct']:.0f}%\n"
                f"  - 손절: 매수가 -10%\n"
                f"  - MA20 이탈 시 즉시 청산\n"
                f"⚠️ 자동 주문 아님 — 직접 매수 후 등록 필요"
            )
        elif opinion == "SKIP":
            msg += "❌ AI 판단: 현재 진입 비추천"

        if opinion == "SKIP":
            # 2026-08-25: AI가 SKIP 판정한 종목은 텔레그램 알림 생략
            # (BUY/HOLD 후보만 받아보고 싶다는 요청 — 알림이 너무 많아 혼란)
            # 결과 기록(results)에는 남겨서 일일 요약 통계엔 계속 반영됨
            print(f"  🔇 {sig['name']}: AI SKIP 판정이라 텔레그램 알림 생략")
        else:
            send_telegram(msg)
        time.sleep(2)   # API 과부하 방지

    # ── 일일 요약 ──────────────────────────────────────
    buy_count  = sum(1 for r in results if r["opinion"] == "BUY")
    hold_count = sum(1 for r in results if r["opinion"] == "HOLD")
    skip_count = sum(1 for r in results if r["opinion"] == "SKIP")

    summary = (
        f"📋 <b>일일 점검 완료</b>\n"
        f"{now}\n\n"
        f"탐지: {len(signals)}건\n"
        f"AI 분석: BUY {buy_count} / HOLD {hold_count} / SKIP {skip_count}\n\n"
        f"현재 보유: {len(positions)}/{max_pos}종목\n\n"
    )
    if not signals:
        summary += "오늘은 탐지된 종목이 없습니다."
    else:
        summary += "상세 분석은 위 메시지를 확인하세요."

    if mode == "all":
        # 한국+미국 둘 다 돌았으면 요약을 양쪽 봇 모두에 발송
        for m in ("KR", "US"):
            _current_market = m
            send_telegram(summary)
    else:
        _current_market = mode.upper()
        send_telegram(summary)

    # 결과 로그 저장
    log = {
        "date": now, "signals": len(signals),
        "results": [{
            "symbol": r["symbol"], "name": r["name"],
            "change_pct": r["change_pct"], "opinion": r["opinion"],
        } for r in results]
    }
    existing = []
    if LOG_FILE.exists():
        try: existing = json.loads(LOG_FILE.read_text())
        except: pass
    existing.append(log)
    LOG_FILE.write_text(json.dumps(existing[-30:], ensure_ascii=False, indent=2))

    print(f"\n  완료: 탐지 {len(signals)}건, BUY {buy_count}건")

if __name__ == "__main__":
    args = sys.argv[1:]
    test = "--test" in args
    if "--kr" in args:
        mode = "kr"
    elif "--us" in args:
        mode = "us"
    else:
        mode = "all"

    try:
        run(mode=mode, test=test)
    except KeyboardInterrupt:
        print("\n[중단]")
        sys.exit(0)
