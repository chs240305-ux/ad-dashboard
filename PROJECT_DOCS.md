# 광고 성과 대시보드 — 프로젝트 문서

> 실행: `python3 -m streamlit run dashboard.py`
> 최종 정리일: 2026-04-24

---

## 1. 프로젝트 개요

cyclehackers.kr의 광고 성과를 통합 모니터링하는 Streamlit 대시보드.
Meta(Facebook/Instagram), Google Ads, 자체 홈페이지 UTM 데이터를 Supabase에 수집한 뒤 시각화한다.

### 기술 스택

| 구분 | 내용 |
|---|---|
| UI 프레임워크 | Streamlit 1.43.2 |
| 차트 라이브러리 | Plotly 5.24.1 (express + graph_objects) |
| 데이터 처리 | Pandas 2.2.3 |
| DB | Supabase (PostgreSQL REST API) |
| 광고 API | Meta Graph API v21.0, Google Ads API |
| 자동화 | GitHub Actions (매일 KST 05:00) |

---

## 2. 파일 구조

```
ad-dashboard/
├── dashboard.py          # 메인 대시보드 (Streamlit)
├── collect_meta.py       # Meta 광고 데이터 수집
├── collect_google.py     # Google 광고 데이터 수집
├── collect_utm.py        # 홈페이지 UTM 데이터 수집
├── get_google_token.py   # Google OAuth 토큰 발급 유틸
├── test_api.py           # API 연결 테스트
├── requirements.txt      # Python 의존성
├── .env                  # 실제 환경변수 (git 제외)
├── .env.example          # 환경변수 템플릿
├── .refresh_token        # UTM 수집용 Supabase refresh token (자동 갱신)
├── CYCLEHACKERS_API.md   # cyclehackers.kr 관리자 API 명세
└── .github/workflows/
    └── daily_collect.yml # 일일 자동 수집 GitHub Actions
```

---

## 3. 환경변수 (.env)

```env
# cyclehackers.kr 홈페이지 Supabase (UTM 수집용)
SUPABASE_URL=https://kiprvkudedkvynhblcqv.supabase.co
SUPABASE_ANON_KEY=sb_publishable_...
CYCLEHACKERS_API=https://www.cyclehackers.kr
REFRESH_TOKEN=                  # .refresh_token 파일로도 관리

# Analytics DB (데이터 저장용, 별도 Supabase 프로젝트)
DB_SUPABASE_URL=https://swlivceqsbyvcnfdxpmt.supabase.co
DB_SUPABASE_SERVICE_KEY=sb_secret_...

# Meta Ads
META_ACCESS_TOKEN_A=Bearer EAA...   # 계정A 전용 (약 60일마다 갱신)
META_ACCESS_TOKEN_B=Bearer EAA...   # 계정B/C 공용
META_AD_ACCOUNT_A=act_1292362411878252   # 로켓스파크(머니톡톡)
META_AD_ACCOUNT_B=act_3110366275814489   # ampm(러닝티)
META_AD_ACCOUNT_C=act_1579306933279906   # 카페24(러닝티)

# Google Ads
GOOGLE_DEVELOPER_TOKEN=
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
GOOGLE_REFRESH_TOKEN=               # get_google_token.py로 발급
GOOGLE_MCC_ID=4607905919
```

---

## 4. DB 스키마

### 4-1. `meta_ad_daily` — Meta 광고 일별 데이터

| 컬럼 | 타입 | 설명 |
|---|---|---|
| date | text | 날짜 (YYYY-MM-DD) |
| account_id | text | 광고 계정 ID |
| ad_id | text | 광고 ID (Unique Key) |
| ad_name | text | 광고명 |
| adset_id | text | 광고세트 ID |
| adset_name | text | 광고세트명 |
| campaign_id | text | 캠페인 ID |
| campaign_name | text | 캠페인명 |
| spend | numeric | 지출액 (KRW) |
| impressions | int | 노출수 |
| clicks | int | 클릭수 |
| link_clicks | int | 링크 클릭수 (inline_link_clicks) |
| ctr | numeric | CTR (inline_link_click_ctr) |
| cpc | numeric | CPC (cost_per_inline_link_click) |
| cpm | numeric | CPM |
| purchases | int | 픽셀 구매 전환수 |
| leads | int | 픽셀 리드 전환수 |
| results | int | 캠페인 목표 결과수 (선택적) |
| result_indicator | text | 결과 지표명 (선택적) |
| actions | jsonb | 전체 actions 배열 (원본) |

Unique Constraint: `(date, ad_id)`

### 4-2. `google_ad_daily` — Google 광고 일별 데이터

| 컬럼 | 타입 | 설명 |
|---|---|---|
| date | text | 날짜 (YYYY-MM-DD) |
| account_id | text | 광고 계정 ID |
| campaign_id | text | 캠페인 ID |
| campaign_name | text | 캠페인명 |
| adgroup_id | text | 광고그룹 ID |
| adgroup_name | text | 광고그룹명 |
| ad_id | text | 광고 ID (Unique Key) |
| ad_name | text | 광고명 |
| cost | numeric | 비용 (cost_micros / 1,000,000) |
| impressions | int | 노출수 |
| clicks | int | 클릭수 |
| conversions | numeric | 전환수 |

Unique Constraint: `(date, ad_id)`

### 4-3. `utm_daily` — UTM 링크 일별 전환 데이터

| 컬럼 | 타입 | 설명 |
|---|---|---|
| date | text | 날짜 (YYYY-MM-DD) |
| utm_id | text | UTM 링크 ID (Unique Key) |
| utm_name | text | UTM 링크 이름 |
| utm_source | text | UTM Source (meta / google / instagram 등) |
| utm_medium | text | UTM Medium (cpc / social 등) |
| utm_campaign | text | UTM Campaign명 |
| short_code | text | 단축 URL 코드 |
| target_url | text | 랜딩 URL |
| free_views | int | 무료강의 페이지 방문수 (신뢰도 미확인) |
| free_checkouts | int | 무료강의 신청 시도수 |
| free_purchases | int | 무료강의 등록 완료수 |
| paid_views | int | 유료강의 페이지 방문수 |
| paid_checkouts | int | 유료강의 결제 시도수 |
| paid_purchases | int | 유료강의 결제 완료수 |
| conversion_rate | numeric | API 제공 전환율 |

Unique Constraint: `(date, utm_id)`

---

## 5. UTM 명명 규칙

```
{YYMMDD}-{강사이름}-{채널}-{상품명}-{소재명}
```

예시: `260413-김성진-메타-워프(C)-스레드이미지2`

| 파트 | 의미 | 예시 |
|---|---|---|
| YYMMDD | 날짜 6자리 | 260413 |
| 강사이름 | 강사 이름 | 김성진 |
| 채널 | 광고 매체 | 메타 / 구글 |
| 상품명 | 강의명 (괄호 없음 = 자사) | 워프(C) |
| 소재명 | 광고 소재 구분 | 스레드이미지2 |

### 자사 광고 판별 기준

캠페인명 / UTM명이 `{6자리숫자}-{괄호없는이름}` 형식이면 자사로 분류.
상품명에 `()` 또는 `（）`가 포함되면 기타(타사 운영 광고)로 분류.

```python
# 상품 코드 추출 (앞 두 파트)
def _extract_product(name): return f"{parts[0]}-{parts[1]}"

# 미디어 그룹 추출 (앞 세 파트)
def _extract_media_group(name): return f"{parts[0]}-{parts[1]}-{parts[2]}"
```

---

## 6. 대시보드 UI 구조

### 사이드바

| 요소 | 기능 |
|---|---|
| 조회 기간 (date_input) | 데이터 조회 날짜 범위 선택. 기본값: 오늘 기준 최근 14일 |
| 새로고침 버튼 | `st.cache_data.clear()` 후 rerun |
| 수집 기간 (date_input) | 데이터 수집 날짜 범위 선택. 기본값: 어제 |
| UTM 수집 버튼 | `collect_utm.py` 실행 |
| Meta 수집 버튼 | `collect_meta.py` 실행 |
| Google 수집 버튼 | `collect_google.py` 실행 |
| 전체 수집 버튼 | 위 3개 순차 실행 |
| DB 캡션 | 연결된 DB URL 표시 |

### 메인 탭 구조

```
📊 통합 현황  |  🔗 UTM 신청자  |  📘 Meta 광고  |  🔍 Google 광고
```

---

## 7. 탭별 상세 UI

### TAB 1: 통합 현황

자사 데이터(`_is_own_product` 판별 통과)만 사용.

1. **채널별 지출 요약**
   - 도넛 파이차트: Meta vs Google 지출 비율
   - 요약 테이블: 채널 / 지출 / 결과 / 클릭

2. **UTM 전환 성과**
   - KPI 메트릭 5개: 무료시도 / 무료완료 / 유료시도 / 유료완료 / 전환율(무료→유료)
   - 계층 expander: 상품 → 미디어그룹 → UTM별 테이블

3. **UTM ↔ 광고 매칭 현황**
   - UTM 이름과 Meta 광고명/광고세트명/캠페인명을 자동 매칭
   - 매칭 완료 / 미매칭 건수 expander로 표시

4. **상품별 광고 성과 비교**
   - 멀티셀렉트로 상품 선택 (기본: 신청수 상위 3개)
   - 지표 선택: 신청수(무료완료) / 유료결제
   - 일별 추이 라인차트

---

### TAB 2: UTM 신청자

전체 UTM 데이터(자사 + 기타) 사용.

1. **상품별 필터** (멀티셀렉트)
2. **KPI 메트릭 5개**: 무료시도 / 무료완료 / 유료시도 / 유료완료 / 전환율
3. **일별 무료/유료 전환** (그룹 막대차트)
4. **일별 전환율** (라인차트)
5. **UTM별 성과 요약** 테이블 (utm_name / utm_source / utm_medium / utm_campaign 기준 집계)

---

### TAB 3: Meta 광고

서브탭: `🏠 자사 광고` / `📋 기타`

#### 자사 광고

1. **KPI 메트릭 5개**: 신청수(UTM) / 총 지출 / 결과(전환) / CTR / CPA
2. **일별 지출 / 신청수 / 결과** (이중 Y축: 막대 + 라인)
3. **일별 CTR / CPA** (이중 Y축 라인차트)
4. **계정별 지출** 파이차트 (3개 계정: 로켓스파크, ampm, 카페24)
5. **캠페인별 성과** 테이블: 캠페인명 / 신청수(UTM) / 지출 / 결과 / 노출 / 링크클릭 / CPC / CPM / CTR / CPA
6. **상품별 계층 성과** (expander 토글)
   - 상품 → 캠페인 → 광고세트 → 광고별 테이블
   - 지출 0원 상품 자동 제외
   - 상품 단위 CSV 다운로드 버튼
7. **광고 상세 테이블** (expander, 전체 raw 데이터)

#### 기타 광고

1. **캠페인별 성과** 테이블
2. **상품별 계층 성과** (expander 토글)
3. **광고 상세 테이블** (expander)

---

### TAB 4: Google 광고

서브탭: `🏠 자사 광고` / `📋 기타`

#### 자사 광고

1. **KPI 메트릭 5개**: 신청수(UTM) / 총 비용 / 결과(전환) / CTR / CPA
2. **일별 비용 / 신청수 / 결과** (이중 Y축: 막대 + 라인)
3. **일별 CTR / CPA** (이중 Y축 라인차트)
4. **캠페인별 지출** 파이차트
5. **캠페인별 성과** 테이블: 캠페인명 / 신청수(UTM) / 비용 / 전환수 / 노출 / 클릭 / CPC / CPM / CTR / CPA
6. **상품별 계층 성과** (expander 토글)
   - 상품 → 캠페인 → 광고그룹 → 광고별 테이블
7. **광고 상세 테이블** (expander)

#### 기타 광고

1. **캠페인별 성과** 테이블
2. **상품별 계층 성과** (expander 토글)
3. **광고 상세 테이블** (expander)

---

## 8. 핵심 함수 목록

### dashboard.py

| 함수 | 역할 |
|---|---|
| `_headers()` | Supabase REST API 인증 헤더 생성 |
| `_load_table(table, start, end)` | Supabase 테이블 페이지네이션 조회 (1000건 단위) |
| `load_meta(start, end)` | meta_ad_daily 로드 + 타입 변환, 캐시 5분 |
| `load_utm(start, end)` | utm_daily 로드 + 타입 변환, 캐시 5분 |
| `load_google(start, end)` | google_ad_daily 로드 + 타입 변환, 캐시 5분 |
| `_extract_product(name)` | 이름에서 상품 코드 추출 (앞 2파트) |
| `_extract_media_group(name)` | 이름에서 미디어 그룹 추출 (앞 3파트) |
| `_is_own_product(name)` | 자사 광고 여부 판별 |
| `_split_own_other(df, name_col)` | 자사 / 기타 데이터프레임 분리 |
| `_utm_product_map(utm_df, sources)` | product_code → UTM 신청수 합계 딕셔너리 |
| `_utm_daily_by_source(utm_df, sources)` | 특정 매체의 일별 UTM 신청수 |
| `_utm_total_by_source(utm_df, sources)` | 특정 매체의 기간 합계 신청수 |
| `campaign_summary_meta(df, utm_product_map)` | Meta 캠페인별 집계 테이블 생성 |
| `campaign_summary_google(df, utm_product_map)` | Google 캠페인별 집계 테이블 생성 |
| `_meta_col_config()` | Meta 테이블 컬럼 포맷 설정 |
| `_google_col_config()` | Google 테이블 컬럼 포맷 설정 |
| `_show_ad_table(df, spend_col, result_col, click_col)` | 광고별 집계 테이블 렌더링 |
| `_render_hierarchy(df, ...)` | 상품→캠페인→광고세트→광고 계층 expander 렌더링 |
| `build_unified(meta_df, google_df, utm_df)` | UTM ↔ 광고 이름 매칭 (광고명→광고세트명→캠페인명 순) |
| `_run_script(script_name, extra_args)` | 사이드바 수집 버튼용 subprocess 실행 |

---

## 9. 데이터 수집 스크립트

### collect_meta.py

- **대상**: 광고 계정 3개 (A: 로켓스파크, B: ampm, C: 카페24)
- **API**: Meta Graph API v21.0 `/{account_id}/insights`
- **수집 레벨**: ad (광고 단위)
- **수집 필드**: campaign / adset / ad 계층명 + spend / impressions / link_clicks / ctr / cpc / cpm / actions
- **결과 파싱**: `actions` 배열에서 `offsite_conversion.fb_pixel_purchase` (purchases), `offsite_conversion.fb_pixel_lead` (leads) 추출
- **DB 저장**: `meta_ad_daily`, Upsert on `(date, ad_id)`

```bash
python3 collect_meta.py --start-date 2026-04-01 --end-date 2026-04-23
```

### collect_google.py

- **대상**: MCC(`GOOGLE_MCC_ID`) 하위 전체 활성 계정 자동 탐색
- **API**: Google Ads API (`GoogleAdsService.search`)
- **GAQL 쿼리**: ad_group_ad 레벨, REMOVED 상태 제외
- **수집 필드**: customer / campaign / ad_group / ad + cost_micros / impressions / clicks / conversions
- **DB 저장**: `google_ad_daily`, Upsert on `(date, ad_id)`, 100건 배치, 3회 재시도

```bash
python3 collect_google.py --start-date 2026-04-01 --end-date 2026-04-23
python3 collect_google.py --accounts 8859050033,2660472982  # 특정 계정만
```

### collect_utm.py

- **인증**: cyclehackers.kr Supabase Refresh Token (1회용 → 자동 갱신, `.refresh_token` 파일 저장)
- **API**: `GET /api/admin/utm-links` (페이지당 100건, hasNext로 페이지네이션)
- **인증 우선순위**: Cookie 방식 → 실패 시 Bearer 헤더 방식 재시도
- **DB 저장**: `utm_daily`, Upsert on `(date, utm_id)`

```bash
python3 collect_utm.py --start-date 2026-04-01 --end-date 2026-04-23
```

---

## 10. 자동화 (GitHub Actions)

파일: `.github/workflows/daily_collect.yml`

- **스케줄**: 매일 UTC 20:00 (= KST 05:00)
- **수동 실행**: `workflow_dispatch` 지원
- **실행 순서**: UTM → Meta → Google (각각 `continue-on-error: true`)
- **환경변수**: GitHub Secrets에서 주입
- **타임아웃**: 30분

---

## 11. 지표 정의

| 지표 | 계산식 | 비고 |
|---|---|---|
| CPC | 지출 ÷ 링크클릭 | Meta: link_clicks 기준 |
| CPM | 지출 ÷ 노출 × 1,000 | |
| CTR | 클릭 ÷ 노출 × 100 | % 단위 |
| CPA | 지출 ÷ 신청수(UTM) | UTM free_purchases 기준 |
| 결과 | purchases + leads | results 컬럼 없을 때 |
| 전환율 | 유료완료 ÷ 무료완료 × 100 | UTM 무료→유료 전환 |

---

## 12. 알려진 제약사항

- `free_views` / `paid_views` (조회수)는 수치 신뢰도 미확인 — 관리자페이지 업데이트 예정, 현재 대시보드에서 미표시
- Meta Access Token은 약 60일마다 수동 갱신 필요
- cyclehackers Refresh Token은 1회 사용 후 만료 → `.refresh_token` 파일 자동 업데이트
- Google Ads API는 Basic Access 승인 전까지 테스트 계정만 사용 가능
