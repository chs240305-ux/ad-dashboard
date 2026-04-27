"""
메타 광고 인사이트 수집 스크립트

광고계정 3개 (A, B, C) → meta_ad_daily 테이블 저장
실행: python3 collect_meta.py [--date YYYY-MM-DD]
"""

import os
import json
import argparse
import requests
from datetime import date, timedelta
from dotenv import load_dotenv

load_dotenv()

# ── 설정 ──────────────────────────────────────────────────────────────────
META_API_BASE = "https://graph.facebook.com/v21.0"

# 광고계정 목록 (계정C는 계정B 토큰 공유)
def _token(env_key: str) -> str:
    """Bearer 접두어 제거 후 순수 토큰 반환"""
    val = os.getenv(env_key, "")
    return val.removeprefix("Bearer ").strip()

ACCOUNTS = [
    {
        "label":      "로켓스파크(머니톡톡)",
        "account_id": os.getenv("META_AD_ACCOUNT_A", ""),
        "token":      _token("META_ACCESS_TOKEN_A"),
    },
    {
        "label":      "ampm(러닝티)",
        "account_id": os.getenv("META_AD_ACCOUNT_B", ""),
        "token":      _token("META_ACCESS_TOKEN_B"),
    },
    {
        "label":      "카페24(러닝티)",
        "account_id": os.getenv("META_AD_ACCOUNT_C", "act_1579306933279906"),
        "token":      _token("META_ACCESS_TOKEN_B"),   # B와 동일 토큰
    },
]

FIELDS = ",".join([
    "campaign_id", "campaign_name",
    "adset_id", "adset_name",
    "ad_id", "ad_name",
    "date_start",
    "spend", "impressions",
    "inline_link_clicks", "inline_link_click_ctr", "cost_per_inline_link_click",
    "cpm", "actions",
])

DB_URL         = os.getenv("DB_SUPABASE_URL", "")
DB_SERVICE_KEY = os.getenv("DB_SUPABASE_SERVICE_KEY", "")


# ── Meta Insights API ──────────────────────────────────────────────────────

def fetch_account_insights(account_id: str, token: str, target_date: str) -> list[dict]:
    """단일 계정의 광고 인사이트 수집 (페이지네이션 포함)"""
    params = {
        "level":          "ad",
        "fields":         FIELDS,
        "time_increment": 1,
        "limit":          500,
        "time_range":     json.dumps({"since": target_date, "until": target_date}),
        "access_token":   token,
    }

    all_rows = []
    url = f"{META_API_BASE}/{account_id}/insights"
    next_url = None  # Meta가 제공하는 next URL 사용 (커서 재구성 불필요)

    while True:
        if next_url:
            res = requests.get(next_url, timeout=60)
        else:
            res = requests.get(url, params=params, timeout=60)

        if res.status_code != 200:
            raise RuntimeError(
                f"Meta API 오류 [{account_id}] ({res.status_code}): {res.text[:300]}"
            )

        body = res.json()
        all_rows.extend(body.get("data", []))

        # Meta가 제공하는 next URL을 그대로 사용 (모든 파라미터 포함)
        next_url = body.get("paging", {}).get("next")
        if not next_url:
            break

    return all_rows


def _parse_actions(actions: list[dict]) -> dict:
    """actions 배열 → {action_type: value} 딕셔너리"""
    return {a["action_type"]: float(a.get("value", 0)) for a in (actions or [])}


def normalize(row: dict, account_id: str) -> dict:
    """Meta API 응답 → DB 저장 형식"""
    acts         = _parse_actions(row.get("actions"))
    purchases    = int(acts.get("offsite_conversion.fb_pixel_purchase", 0))
    leads        = int(acts.get("offsite_conversion.fb_pixel_lead", 0))
    onsite_leads = int(acts.get("lead", 0) or acts.get("onsite_conversion.lead_grouped", 0))
    return {
        "date":             row.get("date_start", ""),
        "account_id":       account_id,
        "ad_id":            row.get("ad_id", ""),
        "ad_name":          row.get("ad_name", ""),
        "adset_id":         row.get("adset_id", ""),
        "adset_name":       row.get("adset_name", ""),
        "campaign_id":      row.get("campaign_id", ""),
        "campaign_name":    row.get("campaign_name", ""),
        "spend":            float(row.get("spend", 0) or 0),
        "impressions":      int(row.get("impressions", 0) or 0),
        "clicks":           int(row.get("clicks", 0) or 0),
        "ctr":              float(row.get("inline_link_click_ctr", 0) or 0),
        "cpc":              float(row.get("cost_per_inline_link_click", 0) or 0),
        "cpm":              float(row.get("cpm", 0) or 0),
        "link_clicks":      int(row.get("inline_link_clicks", 0) or 0),
        "purchases":        purchases,
        "leads":            leads,
        "onsite_leads":     onsite_leads,
        "result_indicator": row.get("result_indicator", ""),
        "actions":          row.get("actions", []),
    }


# ── DB 저장 ───────────────────────────────────────────────────────────────

def save_to_db(rows: list[dict]) -> int:
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
        f"{DB_URL}/rest/v1/meta_ad_daily?on_conflict=date,ad_id",
        headers=headers,
        json=rows,
        timeout=30,
    )
    if res.status_code not in (200, 201):
        raise RuntimeError(f"DB 저장 실패 ({res.status_code}): {res.text[:300]}")
    return len(res.json())


# ── 메인 ──────────────────────────────────────────────────────────────────

def collect_date(target_date: str) -> list[dict]:
    """단일 날짜 수집 후 DB 저장, 수집된 rows 반환"""
    rows_all = []
    for acct in ACCOUNTS:
        if not acct["account_id"] or not acct["token"]:
            print(f"  [스킵] {acct['label']} — 계정 정보 없음 (.env 확인)")
            continue
        print(f"  {acct['label']} ({acct['account_id']}) 수집 중...")
        try:
            raw  = fetch_account_insights(acct["account_id"], acct["token"], target_date)
            rows = [normalize(r, acct["account_id"]) for r in raw]
            rows_all.extend(rows)
            print(f"    ✓ {len(rows)}건")
        except RuntimeError as e:
            print(f"    ✗ 오류: {e}")

    if rows_all:
        saved = save_to_db(rows_all)
        print(f"  → DB 저장 {saved}건")
    else:
        print("  → 수집 데이터 없음")
    return rows_all


def main():
    parser = argparse.ArgumentParser(description="메타 광고 인사이트 수집")
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    parser.add_argument("--date",       default=None,      help="단일 날짜 (YYYY-MM-DD)")
    parser.add_argument("--start-date", default=None,      help="시작 날짜 (YYYY-MM-DD)")
    parser.add_argument("--end-date",   default=yesterday, help="종료 날짜 (기본: 어제)")
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

    print(f"[메타 광고 수집] 대상: {dates[0]} ~ {dates[-1]} ({len(dates)}일)")

    all_rows = []
    for d in dates:
        print(f"\n── {d} ──")
        all_rows.extend(collect_date(d))

    total_spend  = sum(r["spend"]       for r in all_rows)
    total_impr   = sum(r["impressions"] for r in all_rows)
    total_clicks = sum(r["link_clicks"] for r in all_rows)
    print(f"\n[최종 요약] {len(dates)}일 | 지출: ₩{total_spend:,.0f} | 노출: {total_impr:,} | 클릭: {total_clicks:,}")
    return all_rows


if __name__ == "__main__":
    main()
