"""
config.py — 공통 인증 정보 로더
================================
.env 파일 하나에 봇 3개를 구분해서 보관합니다.
각 스크립트는 용도에 맞는 봇을 선택해서 사용합니다.

.env 파일 형식:
  # 디스코드봇: 코인
  DISCORD_WEBHOOK_URL =채팅ID

  # 봇1: 통합잔고조회
  TELEGRAM_TOKEN_COIN=토큰
  TELEGRAM_CHAT_ID_COIN=채팅ID

  # 봇2: 미국주식
  TELEGRAM_TOKEN_US=토큰
  TELEGRAM_CHAT_ID_US=채팅ID

  # 봇3: 한국주식
  TELEGRAM_TOKEN_KR=토큰
  TELEGRAM_CHAT_ID_KR=채팅ID

  # KIS API
  KIS_APP_KEY=키
  KIS_APP_SECRET=시크릿
  KIS_IS_VIRTUAL=false

  # Bitget API (향후 자동매매용)
  BITGET_API_KEY=
  BITGET_SECRET_KEY=
  BITGET_PASSPHRASE=

폴더 구조:
  trading-bot/      (EC2 서버)
  ├── .env          ← 여기에만 실제 값 입력
  ├── config.py     ← 이 파일
  ├── telegram_alert_scanner.py   (코인 봇 사용)
  ├── kis_us_live_scanner.py      (미국주식 봇 사용)
  ├── kis_kr_stock_backtest.py    (한국주식 봇 사용)
  └── integrated_strategy.py     (한국주식 봇 사용)
"""

import os
from pathlib import Path
from typing import Optional

# ──────────────────────────────────────────────────────
#  .env 파일 파싱
# ──────────────────────────────────────────────────────
def _load_env(env_path: Optional[Path] = None) -> dict[str, str]:
    if env_path is None:
        env_path = Path(__file__).parent / ".env"

    env: dict[str, str] = {}
    if not env_path.exists():
        return env

    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if "  #" in val:
                val = val[:val.index("  #")].strip()
            env[key] = val

    return env

_ENV = _load_env()

def _get(key: str, default: str = "") -> str:
    return os.environ.get(key) or _ENV.get(key, default)

# ══════════════════════════════════════════════════════
#  텔레그램 봇 3개 + 디스코드 봇 2개 — 용도별로 분리
# ══════════════════════════════════════════════════════

# 디스코드봇: 코인박스스캔
DISCORD_WEBHOOK_URL = _get("DISCORD_WEBHOOK_URL")

# 디스코드봇: 코인급등스캔
DISCORD_WEBHOOK_URL_ALERT = _get("DISCORD_WEBHOOK_URL_ALERT")

# 봇1: 통합잔고조회 전용
TELEGRAM_TOKEN_COIN   = _get("TELEGRAM_TOKEN_COIN")
TELEGRAM_CHAT_ID_COIN = _get("TELEGRAM_CHAT_ID_COIN")

# 봇2: 미국주식 스캐너 전용
TELEGRAM_TOKEN_US     = _get("TELEGRAM_TOKEN_US")
TELEGRAM_CHAT_ID_US   = _get("TELEGRAM_CHAT_ID_US")

# 봇3: 한국주식 스캐저 전용
TELEGRAM_TOKEN_KR     = _get("TELEGRAM_TOKEN_KR")
TELEGRAM_CHAT_ID_KR   = _get("TELEGRAM_CHAT_ID_KR")

# 하위 호환성 — 기존 코드에서 TELEGRAM_TOKEN으로 import하는 경우
# 각 스크립트에서 직접 TOKEN_COIN / TOKEN_US / TOKEN_KR 을 import하는 게 권장
TELEGRAM_TOKEN   = TELEGRAM_TOKEN_COIN    # 기본값: 코인봇
TELEGRAM_CHAT_ID = TELEGRAM_CHAT_ID_COIN

def _is_valid(token: str, chat_id: str) -> bool:
    return bool(token and chat_id
                and "여기에" not in token
                and "여기에" not in chat_id
                and token != ""
                and chat_id != "")

def telegram_configured(bot: str = "coin") -> bool:
    """
    bot: "coin" | "us" | "kr"
    해당 봇이 설정되어 있는지 확인합니다.
    """
    if bot == "coin":
        return _is_valid(TELEGRAM_TOKEN_COIN, TELEGRAM_CHAT_ID_COIN)
    elif bot == "us":
        return _is_valid(TELEGRAM_TOKEN_US, TELEGRAM_CHAT_ID_US)
    elif bot == "kr":
        return _is_valid(TELEGRAM_TOKEN_KR, TELEGRAM_CHAT_ID_KR)
    return False

def get_telegram_bot(bot: str = "coin") -> tuple[str, str]:
    """
    bot: "coin" | "us" | "kr"
    (token, chat_id) 튜플 반환
    """
    if bot == "coin":
        return TELEGRAM_TOKEN_COIN, TELEGRAM_CHAT_ID_COIN
    elif bot == "us":
        return TELEGRAM_TOKEN_US, TELEGRAM_CHAT_ID_US
    elif bot == "kr":
        return TELEGRAM_TOKEN_KR, TELEGRAM_CHAT_ID_KR
    return "", ""

# ══════════════════════════════════════════════════════
#  엔스로픽 Claude Open API
# ══════════════════════════════════════════════════════
ANTHROPIC_API_KEY = _get("ANTHROPIC_API_KEY")

# ══════════════════════════════════════════════════════
#  한국투자증권 Open API
# ══════════════════════════════════════════════════════
KIS_APP_KEY    = _get("KIS_APP_KEY")
KIS_APP_SECRET = _get("KIS_APP_SECRET")
KIS_IS_VIRTUAL = _get("KIS_IS_VIRTUAL", "true").lower() == "true"

# ══════════════════════════════════════════════════════
# 한국투자증권 실 계좌번호
# ══════════════════════════════════════════════════════
KIS_ACCOUNT_NO = _get("KIS_ACCOUNT_NO")

def kis_configured() -> bool:
    return bool(KIS_APP_KEY and KIS_APP_SECRET
                and "여기에" not in KIS_APP_KEY
                and "여기에" not in KIS_APP_SECRET)

# ══════════════════════════════════════════════════════
#  Bitget API (향후 자동매매용)
# ══════════════════════════════════════════════════════
BITGET_API_KEY    = _get("BITGET_API_KEY")
BITGET_SECRET_KEY = _get("BITGET_SECRET_KEY")
BITGET_PASSPHRASE = _get("BITGET_PASSPHRASE")

# ══════════════════════════════════════════════════════
#  설정 확인 출력 (python3 config.py 로 직접 실행 시)
# ══════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=== 인증 정보 설정 확인 ===\n")
    print(f"텔레그램 봇1 (코인)    : {'✅ 설정됨' if telegram_configured('coin') else '❌ 미설정'}")
    print(f"텔레그램 봇2 (미국주식): {'✅ 설정됨' if telegram_configured('us') else '❌ 미설정'}")
    print(f"텔레그램 봇3 (한국주식): {'✅ 설정됨' if telegram_configured('kr') else '❌ 미설정'}")
    print(f"KIS API               : {'✅ 설정됨' if kis_configured() else '❌ 미설정'}")
    print(f"KIS 환경              : {'모의투자' if KIS_IS_VIRTUAL else '실전투자'}")
    print(f"Bitget API            : {'✅ 설정됨' if BITGET_API_KEY else '❌ 미설정 (현재 불필요)'}")
    print()
    if not telegram_configured("coin"):
        print("→ .env: TELEGRAM_TOKEN_COIN, TELEGRAM_CHAT_ID_COIN 입력 필요")
    if not telegram_configured("us"):
        print("→ .env: TELEGRAM_TOKEN_US, TELEGRAM_CHAT_ID_US 입력 필요")
    if not telegram_configured("kr"):
        print("→ .env: TELEGRAM_TOKEN_KR, TELEGRAM_CHAT_ID_KR 입력 필요")
    if not kis_configured():
        print("→ .env: KIS_APP_KEY, KIS_APP_SECRET 입력 필요")
