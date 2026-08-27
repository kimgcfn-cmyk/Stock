"""
텔레그램 봇 작동 테스트
실행 전: TOKEN과 CHAT_ID를 본인 값으로 교체하세요
"""


import ssl
import urllib.request

# SSL 검증 무시 (맥OS 인증서 문제 임시 해결)
ssl_context = ssl.create_default_context()
ssl_context.check_hostname = False
ssl_context.verify_mode = ssl.CERT_NONE

https_handler = urllib.request.HTTPSHandler(context=ssl_context)
opener = urllib.request.build_opener(https_handler)
urllib.request.install_opener(opener)


import urllib.request
import urllib.parse
import json

# ─────────────────────────────────────────
# ✏️ 여기만 수정하세요
# atman미국주식
# ─────────────────────────────────────────
TOKEN   = "8927029898:AAGqlhdxGnxMBE3ysFgW9IukqFenK46koBs"
CHAT_ID = "6070609895"        # 예: 738291045
# ─────────────────────────────────────────

def send_message(text):
    url  = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id":    CHAT_ID,
        "text":       text,
        "parse_mode": "Markdown",
    }).encode()
    req  = urllib.request.Request(url, data=data)
    resp = urllib.request.urlopen(req, timeout=10)
    return json.loads(resp.read())


def check_bot_info():
    url  = f"https://api.telegram.org/bot{TOKEN}/getMe"
    resp = urllib.request.urlopen(url, timeout=10)
    return json.loads(resp.read())


def run_test():
    print("=" * 45)
    print("  텔레그램 봇 작동 테스트")
    print("=" * 45)

    # 1. 토큰 유효성 확인
    print("\n[1단계] 봇 토큰 확인 중...")
    try:
        info = check_bot_info()
        bot  = info["result"]
        print(f"  ✅ 봇 이름    : {bot['first_name']}")
        print(f"  ✅ 봇 username: @{bot['username']}")
        print(f"  ✅ 봇 ID      : {bot['id']}")
    except Exception as e:
        print(f"  ❌ 토큰 오류: {e}")
        print("  → TOKEN 값을 다시 확인하세요")
        return

    # 2. 메시지 전송 테스트
    print("\n[2단계] 메시지 전송 테스트...")
    try:
        msg = (
            "✅ *텔레그램 봇 연동 성공!*\n"
            "━━━━━━━━━━━━━━━━\n"
            "🤖 봇이 정상 작동합니다\n"
            "📊 쿨라매기 백테스팅 알림 준비 완료\n"
            "━━━━━━━━━━━━━━━━\n"
            "_Kularagi Strategy | BTC/USDT | Bitget_"
        )
        result = send_message(msg)
        if result["ok"]:
            print(f"  ✅ 메시지 전송 성공!")
            print(f"  ✅ Chat ID     : {CHAT_ID}")
            print(f"  ✅ Message ID  : {result['result']['message_id']}")
        else:
            print(f"  ❌ 전송 실패: {result}")
    except Exception as e:
        print(f"  ❌ 전송 오류: {e}")
        print("  → CHAT_ID 값을 다시 확인하세요")
        return

    print("\n" + "=" * 45)
    print("  🎉 모든 테스트 통과! 텔레그램 연동 완료")
    print("=" * 45)


if __name__ == "__main__":
    run_test()