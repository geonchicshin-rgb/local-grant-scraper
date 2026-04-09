import os
import requests
import html
import sys
import io

# Windows 터미널 한글/이모지 출력 지원 설정
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# 이 스크립트는 텔레그램 연동 설정을 테스트하기 위한 용도입니다.
# 실행 전 환경 변수가 설정되어 있어야 합니다.

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

def test_connection():
    if not BOT_TOKEN or not CHAT_ID:
        print("❌ 에러: TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID 환경 변수가 설정되지 않았습니다.")
        print("로컬에서 테스트하려면 다음 명령어를 입력하세요 (Windows PowerShell 기준):")
        print('$env:TELEGRAM_BOT_TOKEN="내_토큰"')
        print('$env:TELEGRAM_CHAT_ID="내_ID"')
        return

    print(f"🔄 텔레그램 연결 테스트 중... (Chat ID: {CHAT_ID})")
    
    test_name = "테스트 지원사업 & 공고" # 특수문자 포함 테스트
    escaped_name = html.escape(test_name)
    
    text = (
        f"✅ <b>연동 테스트 성공</b>\n\n"
        f"이 메시지가 보인다면 텔레그램 설정이 정상입니다.\n"
        f"테스트 항목: {escaped_name}"
    )
    
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML"
    }
    
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            print("✨ 텔레그램 메시지 전송 성공!")
        else:
            print(f"❌ 전송 실패 (Status: {resp.status_code})")
            print(f"응답 내용: {resp.text}")
            print("\n💡 팁: 토큰이나 Chat ID가 정확한지, 봇이 해당 채팅방에 초대되어 있는지 확인하세요.")
    except Exception as e:
        print(f"❗ 예외 발생: {e}")

if __name__ == "__main__":
    test_connection()
