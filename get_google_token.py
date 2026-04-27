"""
Google Ads OAuth2 Refresh Token 발급 스크립트
실행: python3 get_google_token.py
"""

import webbrowser
import threading
import urllib.parse
import http.server
import requests

CLIENT_ID     = "1019764226436-sg1jpnnrr0mfas1o486qlb0hdrlv7nqd.apps.googleusercontent.com"
CLIENT_SECRET = "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"
REDIRECT_URI  = "http://localhost:8080"
SCOPE         = "https://www.googleapis.com/auth/adwords"

auth_code = None


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        global auth_code
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        auth_code = qs.get("code", [None])[0]
        self.send_response(200)
        self.end_headers()
        self.wfile.write("<h2>인증 완료! 터미널로 돌아가세요.</h2>".encode())

    def log_message(self, *args):
        pass  # 로그 숨김


def main():
    # 1. 로컬 서버 시작
    server = http.server.HTTPServer(("", 8080), _Handler)
    t = threading.Thread(target=server.serve_forever)
    t.daemon = True
    t.start()

    # 2. 인증 URL 생성 및 브라우저 오픈
    auth_url = "https://accounts.google.com/o/oauth2/auth?" + urllib.parse.urlencode({
        "client_id":     CLIENT_ID,
        "redirect_uri":  REDIRECT_URI,
        "response_type": "code",
        "scope":         SCOPE,
        "access_type":   "offline",
        "prompt":        "consent",
    })
    print("브라우저에서 Google 계정 인증 페이지를 엽니다...")
    webbrowser.open(auth_url)
    print("Google 계정으로 로그인 → 권한 허용을 클릭해주세요.\n")

    # 3. 코드 수신 대기
    while auth_code is None:
        pass
    server.shutdown()

    # 4. Refresh Token 교환
    res = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id":     CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "code":          auth_code,
            "redirect_uri":  REDIRECT_URI,
            "grant_type":    "authorization_code",
        },
    )
    tokens = res.json()

    if "refresh_token" not in tokens:
        print(f"오류: {tokens}")
        return

    print(f"\n✓ Refresh Token 발급 완료:\n{tokens['refresh_token']}\n")
    print(".env 파일의 GOOGLE_REFRESH_TOKEN 에 위 값을 입력하세요.")


if __name__ == "__main__":
    main()
