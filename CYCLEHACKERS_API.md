# cyclehackers.kr 관리자 API 문서

> 이 문서는 `collect_utm.py` 코드를 기반으로 정리된 API 명세입니다.
> 다른 대시보드나 자동화 스크립트 개발 시 참고용으로 사용합니다.

---

## 1. 기본 정보

| 항목 | 값 |
|---|---|
| API Base URL | `https://www.cyclehackers.kr` |
| Supabase URL (홈페이지) | `https://kiprvkudedkvynhblcqv.supabase.co` |
| Supabase Project Ref | `kiprvkudedkvynhblcqv` |
| 인증 방식 | Supabase 세션 쿠키 (Kakao OAuth) |

---

## 2. 인증 방식

### 2-1. 개요

cyclehackers.kr 는 **Next.js + Supabase Auth** 기반으로 구성되어 있으며,
관리자 API 호출 시 브라우저 세션 쿠키를 그대로 전송해야 합니다.

인증 흐름:
```
Refresh Token → POST /auth/v1/token → Access Token + 신규 Refresh Token
→ Session JSON → Base64 인코딩 → 쿠키 분할 세팅
```

### 2-2. 토큰 갱신 (Supabase)

```
POST https://kiprvkudedkvynhblcqv.supabase.co/auth/v1/token?grant_type=refresh_token
```

**헤더**

| 헤더 | 값 |
|---|---|
| `apikey` | `sb_publishable_-X9zNgZoOuXv7TucsE9d3A_iG1QVSCk` (Anon Key) |
| `Content-Type` | `application/json` |

**요청 바디**

```json
{
  "refresh_token": "저장된_리프레시_토큰"
}
```

**응답 주요 필드**

```json
{
  "access_token": "eyJ...",
  "token_type": "bearer",
  "expires_in": 3600,
  "expires_at": 1776145671,
  "refresh_token": "새로운_리프레시_토큰",
  "user": { ... }
}
```

> ⚠️ Refresh Token은 **1회 사용 후 즉시 만료**됩니다. 응답의 신규 `refresh_token`을 즉시 저장해야 합니다.

### 2-3. 쿠키 생성 방법

Supabase 세션 전체를 JSON → Base64 인코딩 → 브라우저 쿠키 형식으로 변환합니다.

```python
import json, base64

session_str = json.dumps(session, separators=(",", ":"))
session_b64 = base64.b64encode(session_str.encode()).decode()
cookie_value = f"base64-{session_b64}"

# 3180자 단위로 분할 (브라우저 쿠키 크기 제한)
chunk_size = 3180
chunks = [cookie_value[i:i + chunk_size] for i in range(0, len(cookie_value), chunk_size)]

cookies = {}
for i, chunk in enumerate(chunks):
    cookies[f"sb-kiprvkudedkvynhblcqv-auth-token.{i}"] = chunk
```

**생성되는 쿠키 키 예시**

- `sb-kiprvkudedkvynhblcqv-auth-token.0` → 청크 1
- `sb-kiprvkudedkvynhblcqv-auth-token.1` → 청크 2 (세션 크기에 따라 추가)

---

## 3. API 엔드포인트

### 3-1. UTM 링크 목록 + 지표 조회

```
GET https://www.cyclehackers.kr/api/admin/utm-links
```

#### 쿼리 파라미터

| 파라미터 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `page` | int | ✓ | 페이지 번호 (1부터 시작) |
| `limit` | int | ✓ | 페이지당 건수 (최대 100 확인됨) |
| `metricsStartDate` | string | ✓ | 지표 조회 시작일 (`YYYY-MM-DD`) |
| `metricsEndDate` | string | ✓ | 지표 조회 종료일 (`YYYY-MM-DD`) |
| `sortBy` | string | - | 정렬 기준 (예: `created_at`) |
| `sortOrder` | string | - | 정렬 방향 (`asc` / `desc`) |

#### 응답 구조

```json
{
  "links": [
    {
      "id": "43846868-link-uuid",
      "name": "260413-김성진-메타-워프(C)-스레드이미지2",
      "utm_source": "meta",
      "utm_medium": "cpc",
      "utm_campaign": "워프(C)",
      "short_code": "abc123",
      "target_url": "https://www.cyclehackers.kr/..."
    }
  ],
  "metrics": {
    "43846868-link-uuid": {
      "free_views": 150,
      "free_checkouts": 30,
      "free_purchases": 12,
      "paid_views": 80,
      "paid_checkouts": 10,
      "paid_purchases": 3,
      "conversion_rate": 0.25
    }
  },
  "pagination": {
    "hasNext": true
  }
}
```

#### 페이지네이션

- `pagination.hasNext == false`가 될 때까지 `page`를 증가시키며 반복 호출
- 전체 UTM 링크 수가 100개 이하이면 단일 호출로 완료

---

## 4. 데이터 필드 정의

### links 객체

| 필드 | 타입 | 설명 |
|---|---|---|
| `id` | string | UTM 링크 고유 ID |
| `name` | string | UTM 링크 이름 (명명 규칙 참고) |
| `utm_source` | string | UTM Source (예: meta, google, instagram) |
| `utm_medium` | string | UTM Medium (예: cpc, social) |
| `utm_campaign` | string | UTM Campaign명 |
| `short_code` | string | 단축 URL 코드 |
| `target_url` | string | 실제 연결되는 랜딩 URL |

### metrics 객체

| 필드 | 타입 | 설명 |
|---|---|---|
| `free_views` | int | 무료강의 페이지 방문수 |
| `free_checkouts` | int | 무료강의 신청 시도수 (체크아웃 진입) |
| `free_purchases` | int | 무료강의 등록 완료수 |
| `paid_views` | int | 유료강의 페이지 방문수 |
| `paid_checkouts` | int | 유료강의 결제 시도수 (체크아웃 진입) |
| `paid_purchases` | int | 유료강의 결제 완료수 |
| `conversion_rate` | float | 전환율 (API 자체 제공) |

> **주의**: `free_views` / `paid_views`(조회수)는 현재 관리자페이지 기능 업데이트 예정 항목으로,
> 수치 신뢰도 확인 필요. 현재 대시보드에서는 표시 제외.

---

## 5. UTM 이름 명명 규칙

```
{YYMMDD}-{강사이름}-{채널}-{상품명}-{소재명}
```

**예시**

| UTM 이름 | YYMMDD | 강사 | 채널 | 상품 | 소재 |
|---|---|---|---|---|---|
| `260413-김성진-메타-워프(C)-스레드이미지2` | 260413 | 김성진 | 메타 | 워프(C) | 스레드이미지2 |
| `260413-김성진-메타-워프(C)-영상5` | 260413 | 김성진 | 메타 | 워프(C) | 영상5 |

**상품 그룹 추출 키**: `{YYMMDD}-{강사이름}` (앞 두 파트, `-` 기준 분리)

```python
def extract_product(utm_name: str) -> str:
    parts = utm_name.split("-")
    return f"{parts[0]}-{parts[1]}" if len(parts) >= 2 else utm_name
```

---

## 6. DB 스키마 (utm_daily 테이블)

저장용 Supabase DB (`swlivceqsbyvcnfdxpmt.supabase.co`)의 `utm_daily` 테이블 구조입니다.

| 컬럼 | 타입 | 설명 |
|---|---|---|
| `date` | text | 날짜 (`YYYY-MM-DD`) |
| `utm_id` | text | UTM 링크 ID (`links.id`) |
| `utm_name` | text | UTM 링크 이름 |
| `utm_source` | text | UTM Source |
| `utm_medium` | text | UTM Medium |
| `utm_campaign` | text | UTM Campaign |
| `short_code` | text | 단축 URL 코드 |
| `target_url` | text | 랜딩 URL |
| `free_views` | int | 무료강의 방문수 |
| `free_checkouts` | int | 무료강의 신청수 |
| `free_purchases` | int | 무료강의 등록완료수 |
| `paid_views` | int | 유료강의 방문수 |
| `paid_checkouts` | int | 유료강의 결제시도수 |
| `paid_purchases` | int | 유료강의 결제완료수 |
| `conversion_rate` | numeric | API 제공 전환율 |

**Unique Constraint**: `(date, utm_id)` — Upsert 기준키

---

## 7. 환경변수 정리

새 대시보드 구축 시 필요한 `.env` 항목입니다.

```env
# cyclehackers.kr 홈페이지 Supabase
SUPABASE_URL=https://kiprvkudedkvynhblcqv.supabase.co
SUPABASE_ANON_KEY=sb_publishable_-X9zNgZoOuXv7TucsE9d3A_iG1QVSCk
CYCLEHACKERS_API=https://www.cyclehackers.kr
REFRESH_TOKEN=                  # .refresh_token 파일로 관리 가능

# 데이터 저장용 Supabase (Analytics DB)
DB_SUPABASE_URL=https://swlivceqsbyvcnfdxpmt.supabase.co
DB_SUPABASE_SERVICE_KEY=sb_secret_...
```

---

## 8. 호출 예시 (Python)

```python
import json, base64, requests

SUPABASE_URL  = "https://kiprvkudedkvynhblcqv.supabase.co"
SUPABASE_ANON = "sb_publishable_-X9zNgZoOuXv7TucsE9d3A_iG1QVSCk"
PROJECT_REF   = "kiprvkudedkvynhblcqv"
API_BASE      = "https://www.cyclehackers.kr"

# 1. 토큰 갱신
def get_session(refresh_token: str) -> dict:
    res = requests.post(
        f"{SUPABASE_URL}/auth/v1/token?grant_type=refresh_token",
        headers={"apikey": SUPABASE_ANON, "Content-Type": "application/json"},
        json={"refresh_token": refresh_token},
        timeout=10,
    )
    res.raise_for_status()
    return res.json()

# 2. 쿠키 생성
def make_cookies(session: dict) -> dict:
    val    = "base64-" + base64.b64encode(json.dumps(session, separators=(",", ":")).encode()).decode()
    chunks = [val[i:i+3180] for i in range(0, len(val), 3180)]
    return {f"sb-{PROJECT_REF}-auth-token.{i}": c for i, c in enumerate(chunks)}

# 3. UTM 데이터 수집 (특정 날짜)
def fetch_utm(target_date: str, session: dict) -> list[dict]:
    cookies  = make_cookies(session)
    all_rows = []
    page     = 1

    while True:
        res = requests.get(
            f"{API_BASE}/api/admin/utm-links",
            params={
                "page": page, "limit": 100,
                "metricsStartDate": target_date,
                "metricsEndDate":   target_date,
                "sortBy": "created_at", "sortOrder": "desc",
            },
            cookies=cookies,
            timeout=15,
        )
        res.raise_for_status()
        body = res.json()

        for link in body.get("links", []):
            m = body.get("metrics", {}).get(link["id"], {})
            all_rows.append({
                "date":           target_date,
                "utm_id":         link["id"],
                "utm_name":       link["name"],
                "utm_source":     link.get("utm_source", ""),
                "utm_medium":     link.get("utm_medium", ""),
                "utm_campaign":   link.get("utm_campaign", ""),
                "free_checkouts": m.get("free_checkouts", 0) or 0,
                "free_purchases": m.get("free_purchases", 0) or 0,
                "paid_checkouts": m.get("paid_checkouts", 0) or 0,
                "paid_purchases": m.get("paid_purchases", 0) or 0,
            })

        if not body.get("pagination", {}).get("hasNext", False):
            break
        page += 1

    return all_rows

# 사용
session  = get_session("저장된_refresh_token")
utm_data = fetch_utm("2026-04-19", session)
```

---

## 9. 주요 제약사항 및 참고사항

| 항목 | 내용 |
|---|---|
| Refresh Token 수명 | **1회 사용 후 만료** — 사용 즉시 새 토큰 저장 필수 |
| Access Token 수명 | `expires_in: 3600` (1시간) |
| limit 최대값 | 100 (실험적으로 확인된 값, 공식 문서 없음) |
| 관리자 권한 필요 | `app_metadata.role: "admin"` 계정만 API 접근 가능 |
| 인증 대안 | Cookie 방식 실패 시 `Authorization: Bearer {access_token}` 헤더 방식으로 재시도 가능 |
| 조회수(free_views) | 현재 신뢰도 미확인, 관리자페이지 기능 업데이트 예정 |
