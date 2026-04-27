"""
홈페이지 (cyclehackers.kr) UTM 신청자수 수집 스크립트

인증 방식: Supabase Refresh Token 자동 갱신
실행: python collect_utm.py [--date YYYY-MM-DD]
"""

import os
import json
import base64
import argparse
import requests
from datetime import date, timedelta
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── 설정 ──────────────────────────────────────────────────────────────────
SUPABASE_URL   = os.getenv("SUPABASE_URL", "https://kiprvkudedkvynhblcqv.supabase.co")
SUPABASE_ANON  = os.getenv("SUPABASE_ANON_KEY", "sb_publishable_-X9zNgZoOuXv7TucsE9d3A_iG1QVSCk")  # 확인된 키
API_BASE       = os.getenv("CYCLEHACKERS_API", "https://www.cyclehackers.kr")
PROJECT_REF    = "kiprvkudedkvynhblcqv"
TOKEN_FILE     = Path(__file__).parent / ".refresh_token"   # 갱신된 토큰 자동 저장

# ── 저장용 DB (별도 Supabase 프로젝트) ────────────────────────────────────
DB_URL         = os.getenv("DB_SUPABASE_URL", "")
DB_SERVICE_KEY = os.getenv("DB_SUPABASE_SERVICE_KEY", "")


# ── 토큰 관리 ─────────────────────────────────────────────────────────────

def load_refresh_token() -> str:
    """저장된 refresh token 로드 (.refresh_token 파일 우선, 없으면 .env)"""
    if TOKEN_FILE.exists():
        token = TOKEN_FILE.read_text().strip()
        if token:
            return token
    token = os.getenv("REFRESH_TOKEN", "")
    if not token:
        raise ValueError(
            "Refresh token이 없습니다.\n"
            "1. .env 파일에 REFRESH_TOKEN=값 입력\n"
            "   (cURL 쿠키에서 refresh_token 값 추출: "
            "sb-kiprvkudedkvynhblcqv-auth-token.0 을 base64 디코딩 후 확인)\n"
            "2. 또는 .refresh_token 파일에 토큰 값만 입력"
        )
    return token


def save_refresh_token(token: str):
    """갱신된 refresh token 저장 (다음 실행에 사용)"""
    TOKEN_FILE.write_text(token)


def refresh_session(refresh_token: str) -> dict:
    """Supabase refresh token으로 새 세션 발급"""
    res = requests.post(
        f"{SUPABASE_URL}/auth/v1/token?grant_type=refresh_token",
        headers={
            "apikey": SUPABASE_ANON,
            "Content-Type": "application/json",
        },
        json={"refresh_token": refresh_token},
        timeout=10,
    )
    if res.status_code != 200:
        raise RuntimeError(f"토큰 갱신 실패 ({res.status_code}): {res.text}")

    session = res.json()
    # 갱신된 refresh token 즉시 저장 (한 번 쓰면 만료)
    save_refresh_token(session["refresh_token"])
    return session


def build_auth_cookies(session: dict) -> dict:
    """Supabase 세션 → Next.js 앱이 읽는 쿠키 형식으로 변환"""
    session_str  = json.dumps(session, separators=(",", ":"))
    session_b64  = base64.b64encode(session_str.encode()).decode()
    cookie_value = f"base64-{session_b64}"

    # 브라우저 쿠키 크기 제한으로 청크 분할 (3180자 단위)
    chunk_size = 3180
    chunks = [cookie_value[i:i + chunk_size] for i in range(0, len(cookie_value), chunk_size)]

    cookies = {}
    for i, chunk in enumerate(chunks):
        cookies[f"sb-{PROJECT_REF}-auth-token.{i}"] = chunk
    return cookies


# ── DB 저장 ───────────────────────────────────────────────────────────────

def save_to_db(rows: list[dict]) -> int:
    """utm_daily 테이블에 upsert (date + utm_id 기준 중복 방지)"""
    if not DB_URL or not DB_SERVICE_KEY:
        print("  [스킵] DB 설정 없음 (DB_SUPABASE_URL, DB_SUPABASE_SERVICE_KEY 필요)")
        return 0

    headers = {
        "apikey":        DB_SERVICE_KEY,
        "Authorization": f"Bearer {DB_SERVICE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "resolution=merge-duplicates,return=representation",
    }
    res = requests.post(
        f"{DB_URL}/rest/v1/utm_daily?on_conflict=date,utm_id",
        headers=headers,
        json=rows,
        timeout=30,
    )
    if res.status_code not in (200, 201):
        raise RuntimeError(f"DB 저장 실패 ({res.status_code}): {res.text[:300]}")
    return len(res.json())


# ── UTM 데이터 수집 ────────────────────────────────────────────────────────

def fetch_utm_links(target_date: str, session: dict) -> list[dict]:
    """
    지정 날짜의 UTM별 방문수 / 신청수 수집

    반환 예시:
    [
      {
        "utm_name": "20250414_무료강의_소재1",
        "url": "https://...",
        "visits": 150,
        "signups": 12,
        "date": "2026-04-13"
      }, ...
    ]
    """
    cookies = build_auth_cookies(session)
    all_items = []
    page = 1

    while True:
        res = requests.get(
            f"{API_BASE}/api/admin/utm-links",
            params={
                "page": page,
                "limit": 100,
                "metricsStartDate": target_date,
                "metricsEndDate": target_date,
                "sortBy": "created_at",
                "sortOrder": "desc",
            },
            cookies=cookies,
            timeout=15,
        )

        if res.status_code in (401, 403):
            # Authorization 헤더 방식으로 재시도
            res2 = requests.get(
                f"{API_BASE}/api/admin/utm-links",
                params={
                    "page": page,
                    "limit": 100,
                    "metricsStartDate": target_date,
                    "metricsEndDate": target_date,
                    "sortBy": "created_at",
                    "sortOrder": "desc",
                },
                headers={"Authorization": f"Bearer {session['access_token']}"},
                timeout=15,
            )
            print(f"  [디버그] Cookie 방식: {res.status_code}, Bearer 방식: {res2.status_code}")
            print(f"  [디버그] 응답 본문: {res2.text[:300]}")
            if res2.status_code != 200:
                raise RuntimeError(
                    f"인증 실패 — Cookie({res.status_code}), Bearer({res2.status_code})\n"
                    f"응답: {res2.text[:200]}"
                )
            res = res2
        elif res.status_code != 200:
            raise RuntimeError(f"API 오류 ({res.status_code}): {res.text[:200]}")

        body = res.json()

        # 응답 구조: { links: [...], metrics: {id: {...}}, pagination: {...} }
        links   = body.get("links", [])
        metrics = body.get("metrics", {})
        pagination = body.get("pagination", {})

        if not links:
            break

        for link in links:
            m = metrics.get(link["id"], {})
            all_items.append(_normalize(link, m, target_date))

        # 페이지네이션 종료
        if not pagination.get("hasNext", False):
            break
        page += 1

    return all_items


def _normalize(link: dict, metrics: dict, target_date: str) -> dict:
    """
    links + metrics 를 결합하여 통일된 형식으로 반환

    필드 의미:
      free_views     : 무료강의 방문수
      free_checkouts : 무료강의 신청수 (체크아웃 진입)
      free_purchases : 무료강의 등록완료수
      paid_*         : 유료강의 동일 구조
    """
    return {
        "date":              target_date,
        "utm_id":            link.get("id", ""),
        "utm_name":          link.get("name", ""),
        "utm_source":        link.get("utm_source", ""),
        "utm_medium":        link.get("utm_medium", ""),
        "utm_campaign":      link.get("utm_campaign", ""),
        "short_code":        link.get("short_code", ""),
        "target_url":        link.get("target_url", ""),
        # 무료강의
        "free_views":        metrics.get("free_views", 0) or 0,
        "free_checkouts":    metrics.get("free_checkouts", 0) or 0,
        "free_purchases":    metrics.get("free_purchases", 0) or 0,
        # 유료강의
        "paid_views":        metrics.get("paid_views", 0) or 0,
        "paid_checkouts":    metrics.get("paid_checkouts", 0) or 0,
        "paid_purchases":    metrics.get("paid_purchases", 0) or 0,
        "conversion_rate":   metrics.get("conversion_rate", 0) or 0,
    }


# ── 메인 ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="UTM 신청자수 수집")
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    parser.add_argument("--date",       default=None,      help="단일 날짜 (YYYY-MM-DD)")
    parser.add_argument("--start-date", default=None,      help="시작 날짜 (YYYY-MM-DD)")
    parser.add_argument("--end-date",   default=yesterday, help="종료 날짜 (기본: 어제)")
    parser.add_argument("--debug", action="store_true", help="원본 응답 출력")
    args = parser.parse_args()

    # 날짜 범위 결정
    if args.date:
        dates = [args.date]
    else:
        start = date.fromisoformat(args.start_date) if args.start_date else date.today() - timedelta(days=1)
        end   = date.fromisoformat(args.end_date)
        if start > end:
            start, end = end, start
        days  = (end - start).days + 1
        dates = [(start + timedelta(days=i)).isoformat() for i in range(days)]

    print(f"[UTM 수집] 대상: {dates[0]} ~ {dates[-1]} ({len(dates)}일)")

    # 토큰은 처음 한 번만 갱신 (세션이 충분히 길어 여러 날짜 커버)
    print("  토큰 갱신 중...")
    refresh_token = load_refresh_token()
    session = refresh_session(refresh_token)
    print(f"  ✓ 토큰 갱신 완료 (만료: {session.get('expires_in', '?')}초 후)")

    all_rows = []
    for d in dates:
        print(f"\n── {d} ──")
        rows = fetch_utm_links(d, session)
        print(f"  ✓ {len(rows)}건 수집")
        if rows:
            saved = save_to_db(rows)
            print(f"  → DB 저장 {saved}건")
        all_rows.extend(rows)

    total_views     = sum(r["free_views"]     for r in all_rows)
    total_checkouts = sum(r["free_checkouts"] for r in all_rows)
    total_purchases = sum(r["free_purchases"] for r in all_rows)
    print(f"\n[최종 요약] {len(dates)}일 | 방문: {total_views:,} | 신청: {total_checkouts:,} | 등록: {total_purchases:,}")
    return all_rows


if __name__ == "__main__":
    main()
