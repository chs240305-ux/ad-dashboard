"""
구글 광고 인사이트 수집 스크립트

MCC 하위 계정 전체 → google_ad_daily 테이블 저장
실행: python3 collect_google.py [--date YYYY-MM-DD]
"""

import os
import argparse
import time
import requests
from datetime import date, timedelta
from dotenv import load_dotenv
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

load_dotenv()

# ── 설정 ──────────────────────────────────────────────────────────────────
DEVELOPER_TOKEN  = os.getenv("GOOGLE_DEVELOPER_TOKEN", "")
CLIENT_ID        = os.getenv("GOOGLE_CLIENT_ID", "")
CLIENT_SECRET    = os.getenv("GOOGLE_CLIENT_SECRET", "")
REFRESH_TOKEN    = os.getenv("GOOGLE_REFRESH_TOKEN", "")
MCC_ID           = os.getenv("GOOGLE_MCC_ID", "").replace("-", "")   # 대시 제거

DB_URL           = os.getenv("DB_SUPABASE_URL", "")
DB_SERVICE_KEY   = os.getenv("DB_SUPABASE_SERVICE_KEY", "")

GAQL = """
SELECT
  customer.id,
  campaign.id,
  campaign.name,
  ad_group.id,
  ad_group.name,
  ad_group_ad.ad.id,
  ad_group_ad.ad.name,
  metrics.cost_micros,
  metrics.impressions,
  metrics.clicks,
  metrics.conversions,
  segments.date
FROM ad_group_ad
WHERE segments.date = '{date}'
  AND campaign.status != 'REMOVED'
  AND ad_group.status != 'REMOVED'
  AND ad_group_ad.status != 'REMOVED'
"""


# ── Google Ads API ─────────────────────────────────────────────────────────

def build_client() -> GoogleAdsClient:
    config = {
        "developer_token":    DEVELOPER_TOKEN,
        "client_id":          CLIENT_ID,
        "client_secret":      CLIENT_SECRET,
        "refresh_token":      REFRESH_TOKEN,
        "login_customer_id":  MCC_ID,
        "use_proto_plus":     True,
    }
    return GoogleAdsClient.load_from_dict(config)


def list_customer_ids(client: GoogleAdsClient) -> list[str]:
    """MCC 하위의 활성 광고 계정 ID 목록 반환"""
    svc = client.get_service("CustomerService")
    accessible = svc.list_accessible_customers()
    # MCC 자신 제외, 실제 광고 계정만
    return [r.split("/")[-1] for r in accessible.resource_names
            if r.split("/")[-1] != MCC_ID]


def fetch_customer_data(client: GoogleAdsClient, customer_id: str, target_date: str) -> list[dict]:
    """단일 계정의 하루 광고 데이터 수집"""
    svc = client.get_service("GoogleAdsService")
    query = GAQL.format(date=target_date)

    rows = []
    try:
        response = svc.search(customer_id=customer_id, query=query)
        for row in response:
            m = row.metrics
            rows.append({
                "date":          target_date,
                "account_id":    str(row.customer.id),
                "campaign_id":   str(row.campaign.id),
                "campaign_name": row.campaign.name,
                "adgroup_id":    str(row.ad_group.id),
                "adgroup_name":  row.ad_group.name,
                "ad_id":         str(row.ad_group_ad.ad.id),
                "ad_name":       row.ad_group_ad.ad.name or "",
                "cost":          round(m.cost_micros / 1_000_000, 2),
                "impressions":   int(m.impressions),
                "clicks":        int(m.clicks),
                "conversions":   float(m.conversions),
            })
    except GoogleAdsException as e:
        print(f"    ✗ 계정 {customer_id} 오류: {e.error.code().name}")
    return rows


# ── DB 저장 ───────────────────────────────────────────────────────────────

def save_to_db(rows: list[dict], batch_size: int = 100) -> int:
    if not DB_URL or not DB_SERVICE_KEY:
        print("  [스킵] DB 설정 없음")
        return 0

    headers = {
        "apikey":        DB_SERVICE_KEY,
        "Authorization": f"Bearer {DB_SERVICE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "resolution=merge-duplicates,return=representation",
    }
    total_saved = 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        for attempt in range(3):
            try:
                res = requests.post(
                    f"{DB_URL}/rest/v1/google_ad_daily?on_conflict=date,ad_id",
                    headers=headers,
                    json=batch,
                    timeout=30,
                )
                if res.status_code not in (200, 201):
                    raise RuntimeError(f"DB 저장 실패 ({res.status_code}): {res.text[:300]}")
                total_saved += len(res.json())
                break
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                if attempt == 2:
                    raise
                wait = 5 * (attempt + 1)
                print(f"  [재시도 {attempt + 1}/3] 연결 오류, {wait}초 후 재시도...")
                time.sleep(wait)
    return total_saved


# ── 메인 ──────────────────────────────────────────────────────────────────

def main():
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    parser = argparse.ArgumentParser(description="구글 광고 인사이트 수집")
    parser.add_argument("--date",       default=None,      help="단일 날짜 (YYYY-MM-DD)")
    parser.add_argument("--start-date", default=None,      help="시작 날짜 (YYYY-MM-DD)")
    parser.add_argument("--end-date",   default=yesterday, help="종료 날짜 (기본: 어제)")
    parser.add_argument("--accounts",   default=None,      help="수집할 계정 ID (쉼표 구분, 예: 8859050033,2660472982)")
    args = parser.parse_args()

    # 날짜 범위 결정
    if args.date:
        dates = [args.date]
    else:
        start = date.fromisoformat(args.start_date) if args.start_date else date.today() - timedelta(days=1)
        end   = date.fromisoformat(args.end_date)
        if start > end:
            start, end = end, start
        dates = [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]

    print(f"[구글 광고 수집] 대상: {dates[0]} ~ {dates[-1]} ({len(dates)}일)")

    client = build_client()

    # 계정 목록 결정
    if args.accounts:
        customer_ids = [a.strip() for a in args.accounts.split(",") if a.strip()]
        print(f"  ✓ 지정 계정 {len(customer_ids)}개: {customer_ids}")
    else:
        print("  MCC 하위 계정 목록 조회 중...")
        try:
            customer_ids = list_customer_ids(client)
            print(f"  ✓ 계정 {len(customer_ids)}개 발견: {customer_ids}")
        except Exception as e:
            print(f"  ✗ 계정 목록 조회 실패: {e}")
            return

    grand_total = []
    for target_date in dates:
        print(f"\n── {target_date} ──")
        all_rows = []
        for cid in customer_ids:
            rows = fetch_customer_data(client, cid, target_date)
            all_rows.extend(rows)
            if rows:
                print(f"  계정 {cid}: {len(rows)}건")

        print(f"  소계: {len(all_rows)}건")
        if all_rows:
            saved = save_to_db(all_rows)
            print(f"  → DB 저장 {saved}건")
        grand_total.extend(all_rows)

    total_cost  = sum(r["cost"] for r in grand_total)
    total_impr  = sum(r["impressions"] for r in grand_total)
    total_click = sum(r["clicks"] for r in grand_total)
    total_conv  = sum(r["conversions"] for r in grand_total)
    print(f"\n[최종 요약] {len(dates)}일 | 비용: ₩{total_cost:,.0f} | 노출: {total_impr:,} | 클릭: {total_click:,} | 전환: {total_conv:.1f}")


if __name__ == "__main__":
    main()
