"""
광고 성과 대시보드
실행: python3 -m streamlit run dashboard.py
"""

import os
import re
import sys
import subprocess
import requests
import pandas as pd
import streamlit as st
import plotly.express as px
import plotly.graph_objects as go
from datetime import date, timedelta
from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

load_dotenv()

DB_URL         = os.getenv("DB_SUPABASE_URL", "")
DB_SERVICE_KEY = os.getenv("DB_SUPABASE_SERVICE_KEY", "")

st.set_page_config(
    page_title="광고 성과 대시보드",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ── DB 조회 ────────────────────────────────────────────────────────────────

def _headers() -> dict:
    return {
        "apikey":        DB_SERVICE_KEY,
        "Authorization": f"Bearer {DB_SERVICE_KEY}",
        "Content-Type":  "application/json",
    }


_TABLE_ORDER = {
    "meta_ad_daily":   "date.desc,ad_id.asc",
    "utm_daily":       "date.desc,utm_id.asc",
    "google_ad_daily": "date.desc,ad_id.asc",
}

def _load_table(table: str, start: str, end: str):
    PAGE_SIZE = 1000
    all_data  = []
    offset    = 0
    order     = _TABLE_ORDER.get(table, "date.desc")

    while True:
        res = requests.get(
            f"{DB_URL}/rest/v1/{table}",
            headers=_headers(),
            params={
                "select": "*",
                "and":    f"(date.gte.{start},date.lte.{end})",
                "order":  order,
                "limit":  PAGE_SIZE,
                "offset": offset,
            },
            timeout=30,
        )
        if res.status_code != 200:
            return None
        page = res.json()
        all_data.extend(page)
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    return all_data


@st.cache_data(ttl=300)
def load_meta(start: str, end: str) -> pd.DataFrame:
    data = _load_table("meta_ad_daily", start, end)
    if data is None:
        st.error("Meta 데이터 로드 실패 – DB 설정을 확인하세요.")
        return pd.DataFrame()
    df = pd.DataFrame(data)
    if df.empty:
        return df
    df["date"]  = pd.to_datetime(df["date"])
    df["spend"] = pd.to_numeric(df["spend"], errors="coerce").fillna(0)
    for col in ["impressions", "link_clicks", "clicks", "purchases", "leads"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)
    for col in ["cpc", "cpm", "ctr"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    if "results" in df.columns:
        df["results"] = pd.to_numeric(df["results"], errors="coerce").fillna(0).astype(int)
        df["결과"] = df["results"]
    else:
        p = df["purchases"] if "purchases" in df.columns else 0
        l = df["leads"]     if "leads"     in df.columns else 0
        df["결과"] = p + l

    # 인스턴트 양식 판별 + 결과 재계산
    if "actions" in df.columns:
        df["onsite_leads"] = df["actions"].apply(_parse_onsite_leads)
        _has_pixel = df["actions"].apply(_has_pixel_conversion)
    else:
        df["onsite_leads"] = 0
        _has_pixel = pd.Series([False] * len(df), index=df.index)
    ri_series = df["result_indicator"] if "result_indicator" in df.columns else pd.Series([""] * len(df), index=df.index)
    df["is_instant_form"] = [
        _is_instant_form_meta(ri, ol, hp)
        for ri, ol, hp in zip(ri_series, df["onsite_leads"], _has_pixel)
    ]
    # 인스턴트 양식 캠페인: 결과값 = onsite_leads (픽셀 전환 대신 온사이트 리드 사용)
    mask_instant = df["is_instant_form"]
    if mask_instant.any():
        df.loc[mask_instant, "결과"] = df.loc[mask_instant, "onsite_leads"]

    return df


@st.cache_data(ttl=300)
def load_utm(start: str, end: str) -> pd.DataFrame:
    data = _load_table("utm_daily", start, end)
    if data is None:
        st.error("UTM 데이터 로드 실패 – DB 설정을 확인하세요.")
        return pd.DataFrame()
    df = pd.DataFrame(data)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    for col in ["free_views", "free_checkouts", "free_purchases",
                "paid_views",  "paid_checkouts",  "paid_purchases"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)
    return df


@st.cache_data(ttl=300)
def load_google(start: str, end: str) -> pd.DataFrame:
    data = _load_table("google_ad_daily", start, end)
    if data is None:
        return pd.DataFrame()
    df = pd.DataFrame(data)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    for col in ["cost", "impressions", "clicks", "conversions"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    return df


# ── 헬퍼 함수 ─────────────────────────────────────────────────────────────

def _extract_product(name: str) -> str:
    """이름에서 상품 코드 추출 (앞 두 파트: 6자리숫자-이름)"""
    parts = str(name).split("-")
    if len(parts) >= 2:
        return f"{parts[0]}-{parts[1]}"
    return str(name)


def _extract_media_group(name: str) -> str:
    """이름에서 미디어 그룹 추출 (앞 세 파트: 6자리숫자-이름-채널)"""
    parts = str(name).split("-")
    if len(parts) >= 3:
        return f"{parts[0]}-{parts[1]}-{parts[2]}"
    return _extract_product(name)


def _is_own_product(name: str) -> bool:
    """자사 광고 여부 판별: {6자리숫자}-{괄호없는이름} 형식"""
    parts = str(name).strip().split("-")
    if len(parts) < 2:
        return False
    if not re.match(r"^\d{6}$", parts[0]):
        return False
    if re.search(r"[()（）]", parts[1]):
        return False
    return True


def _parse_onsite_leads(actions_data) -> int:
    """actions 배열에서 lead action 수 추출"""
    if not isinstance(actions_data, list):
        return 0
    for a in actions_data:
        if isinstance(a, dict) and a.get("action_type") == "lead":
            try:
                return int(float(a.get("value", 0)))
            except (ValueError, TypeError):
                return 0
    return 0


def _has_pixel_conversion(actions_data) -> bool:
    """actions에 offsite_conversion.fb_pixel_* 이벤트가 있으면 True.
    픽셀 전환 = 홈페이지로 연결되는 UTM 캠페인 → 인스턴트 양식 아님.
    """
    if not isinstance(actions_data, list):
        return False
    return any(
        isinstance(a, dict)
        and str(a.get("action_type", "")).startswith("offsite_conversion.fb_pixel_")
        and float(a.get("value", 0)) > 0
        for a in actions_data
    )


def _is_instant_form_meta(result_indicator, onsite_leads: int, has_pixel: bool = False) -> bool:
    """인스턴트 양식 캠페인 판별.
    1) result_indicator 유효값: offsite_conversion 시작이면 일반, 그 외 → 인스턴트 양식
    2) 무효값('', \"''\")이면: lead > 0 이고 픽셀 전환(fb_pixel_*) 없을 때만 인스턴트 양식
    """
    ri = ""
    if result_indicator and not pd.isna(result_indicator):
        ri = str(result_indicator).strip().strip("'\"")
    if ri:
        return not ri.startswith("offsite_conversion")
    return onsite_leads > 0 and not has_pixel


def _split_own_other(df: pd.DataFrame, name_col: str):
    """자사 / 기타 분리"""
    if df.empty or name_col not in df.columns:
        return df.copy(), pd.DataFrame(columns=df.columns if not df.empty else [])
    mask = df[name_col].apply(_is_own_product)
    return df[mask].copy(), df[~mask].copy()


def _utm_product_map(utm_df: pd.DataFrame, sources: list = None) -> dict:
    """product_code → UTM 신청수(free_purchases) 합계"""
    if utm_df.empty or "utm_name" not in utm_df.columns or "free_purchases" not in utm_df.columns:
        return {}
    tmp = utm_df.copy()
    if sources and "utm_source" in tmp.columns:
        tmp = tmp[tmp["utm_source"].str.lower().isin([s.lower() for s in sources])]
    if tmp.empty:
        return {}
    tmp["__product__"] = tmp["utm_name"].apply(_extract_product)
    return tmp.groupby("__product__")["free_purchases"].sum().to_dict()


def _utm_daily_by_source(utm_df: pd.DataFrame, sources: list) -> pd.DataFrame:
    """특정 매체의 일별 UTM 신청수(free_purchases)"""
    if utm_df.empty or "utm_source" not in utm_df.columns or "free_purchases" not in utm_df.columns:
        return pd.DataFrame(columns=["date", "신청수"])
    filt = utm_df[utm_df["utm_source"].str.lower().isin([s.lower() for s in sources])]
    if filt.empty:
        return pd.DataFrame(columns=["date", "신청수"])
    return (
        filt.groupby("date")["free_purchases"]
        .sum().reset_index()
        .rename(columns={"free_purchases": "신청수"})
    )


def _utm_total_by_source(utm_df: pd.DataFrame, sources: list) -> int:
    d = _utm_daily_by_source(utm_df, sources)
    return int(d["신청수"].sum()) if not d.empty else 0


# ── 캠페인 집계 ─────────────────────────────────────────────────────────────

def campaign_summary_meta(df: pd.DataFrame, utm_product_map: dict = None) -> pd.DataFrame:
    lc = "link_clicks" if "link_clicks" in df.columns else "clicks"
    agg_kw: dict = {
        "지출":   ("spend",       "sum"),
        "결과":   ("결과",         "sum"),
        "노출":   ("impressions",  "sum"),
    }
    if lc in df.columns:
        agg_kw["링크클릭"] = (lc, "sum")
    agg = (
        df.groupby("campaign_name").agg(**agg_kw)
        .reset_index().rename(columns={"campaign_name": "캠페인명"})
    )
    if "링크클릭" not in agg.columns:
        agg["링크클릭"] = 0

    # 인스턴트 양식 캠페인 집합
    instant_camps = set()
    if "is_instant_form" in df.columns:
        instant_camps = set(df[df["is_instant_form"]]["campaign_name"].unique())

    # 신청수: 인스턴트 양식 → 결과값, 일반 → UTM 매핑
    def _signups(row):
        if row["캠페인명"] in instant_camps:
            return int(row["결과"])
        return int((utm_product_map or {}).get(_extract_product(row["캠페인명"]), 0))

    agg["신청수"] = agg.apply(_signups, axis=1)
    agg["유형"] = agg["캠페인명"].apply(
        lambda n: "인스턴트양식" if n in instant_camps else "UTM"
    )
    agg["CPC"] = (agg["지출"] / agg["링크클릭"].replace(0, float("nan"))).fillna(0).round(0)
    agg["CPM"] = (agg["지출"] / agg["노출"].replace(0, float("nan")) * 1000).fillna(0).round(0)
    agg["CTR"] = (agg["링크클릭"] / agg["노출"].replace(0, float("nan")) * 100).fillna(0).round(2)
    agg["CPA"] = (agg["지출"] / agg["신청수"].replace(0, float("nan"))).fillna(0).round(0)
    agg["결과"] = agg["결과"].astype(int)
    return agg[["캠페인명", "유형", "신청수", "지출", "결과", "노출", "링크클릭", "CPC", "CPM", "CTR", "CPA"]].sort_values("지출", ascending=False)


def campaign_summary_google(df: pd.DataFrame, utm_product_map: dict = None) -> pd.DataFrame:
    agg = (
        df.groupby("campaign_name")
        .agg(비용=("cost", "sum"), 전환수=("conversions", "sum"),
             노출=("impressions", "sum"), 클릭=("clicks", "sum"))
        .reset_index().rename(columns={"campaign_name": "캠페인명"})
    )
    if utm_product_map:
        agg["신청수(UTM)"] = agg["캠페인명"].apply(
            lambda n: int(utm_product_map.get(_extract_product(n), 0))
        )
    else:
        agg["신청수(UTM)"] = 0
    agg["CPC"] = (agg["비용"] / agg["클릭"].replace(0, float("nan"))).fillna(0).round(0)
    agg["CPM"] = (agg["비용"] / agg["노출"].replace(0, float("nan")) * 1000).fillna(0).round(0)
    agg["CTR"] = (agg["클릭"] / agg["노출"].replace(0, float("nan")) * 100).fillna(0).round(2)
    agg["CPA"] = (agg["비용"] / agg["신청수(UTM)"].replace(0, float("nan"))).fillna(0).round(0)
    agg["전환수"] = agg["전환수"].astype(int)
    return agg[["캠페인명", "신청수(UTM)", "비용", "전환수", "노출", "클릭", "CPC", "CPM", "CTR", "CPA"]].sort_values("비용", ascending=False)


def _fmt_campaign_meta(df: pd.DataFrame) -> pd.DataFrame:
    """Meta 캠페인 요약 테이블 — 표시용 문자열 포맷 적용"""
    d = df.copy()
    for c in ["신청수", "결과", "노출", "링크클릭"]:
        if c in d.columns:
            d[c] = d[c].apply(lambda x: f"{int(x):,}")
    for c in ["지출", "CPC", "CPM", "CPA"]:
        if c in d.columns:
            d[c] = d[c].apply(lambda x: f"₩{x:,.0f}")
    if "CTR" in d.columns:
        d["CTR"] = d["CTR"].apply(lambda x: f"{x:.2f}%")
    return d


def _fmt_campaign_google(df: pd.DataFrame) -> pd.DataFrame:
    """Google 캠페인 요약 테이블 — 표시용 문자열 포맷 적용"""
    d = df.copy()
    for c in ["신청수(UTM)", "전환수", "노출", "클릭"]:
        if c in d.columns:
            d[c] = d[c].apply(lambda x: f"{int(x):,}")
    for c in ["비용", "CPC", "CPM", "CPA"]:
        if c in d.columns:
            d[c] = d[c].apply(lambda x: f"₩{x:,.0f}")
    if "CTR" in d.columns:
        d["CTR"] = d["CTR"].apply(lambda x: f"{x:.2f}%")
    return d


# ── Meta 토큰 상태 확인 ────────────────────────────────────────────────────

def _meta_raw_token(env_key: str) -> str:
    val = os.getenv(env_key, "")
    return val.removeprefix("Bearer ").strip()


@st.cache_data(ttl=3600)
def _check_meta_token(account_id: str, token: str, label: str) -> dict:
    """Meta 계정 토큰 유효성 확인 (1시간 캐시)"""
    if not account_id or not token:
        return {"label": label, "account_id": account_id or "(미설정)", "valid": False,
                "reason": "계정 ID 또는 토큰 미설정 (.env 확인 필요)"}
    try:
        res = requests.get(
            f"https://graph.facebook.com/v21.0/{account_id}",
            params={"fields": "id,name", "access_token": token},
            timeout=10,
        )
        if res.status_code == 200:
            name = res.json().get("name", "")
            return {"label": label, "account_id": account_id, "valid": True,
                    "reason": f"정상 ({name})"}
        else:
            err = res.json().get("error", {})
            msg = err.get("message", f"HTTP {res.status_code}")
            code = err.get("code", "")
            hint = " — 토큰 만료 가능성" if code == 190 else ""
            return {"label": label, "account_id": account_id, "valid": False,
                    "reason": f"{msg}{hint}"}
    except Exception as e:
        return {"label": label, "account_id": account_id, "valid": False, "reason": str(e)}


# ── 상품별 계층 토글 ───────────────────────────────────────────────────────

def _show_ad_table(df: pd.DataFrame, spend_col: str, result_col: str, click_col: str):
    """광고별 집계 테이블"""
    if "ad_name" not in df.columns:
        return
    num_cols = [c for c in [spend_col, result_col, click_col, "impressions"] if c in df.columns]
    ad_agg = df.groupby("ad_name")[num_cols].sum().reset_index()
    if click_col in ad_agg.columns and "impressions" in ad_agg.columns:
        ad_agg["CTR(%)"] = (ad_agg[click_col] / ad_agg["impressions"].replace(0, float("nan")) * 100).fillna(0).round(2)
    if spend_col in ad_agg.columns and click_col in ad_agg.columns:
        ad_agg["CPC(₩)"] = (ad_agg[spend_col] / ad_agg[click_col].replace(0, float("nan"))).fillna(0).round(0)
    if spend_col in ad_agg.columns and result_col in ad_agg.columns:
        ad_agg["CPA(₩)"] = (ad_agg[spend_col] / ad_agg[result_col].replace(0, float("nan"))).fillna(0).round(0)
    st.dataframe(ad_agg.sort_values(spend_col, ascending=False), use_container_width=True, hide_index=True)


def _render_hierarchy(df: pd.DataFrame, spend_col: str, result_col: str,
                       click_col: str, adgroup_col: str,
                       utm_product_map: dict = None, source: str = "Meta",
                       key_prefix: str = ""):
    """상품별 → 캠페인별 → 광고세트/그룹별 → 광고별 계층 토글.
    product 레벨만 expander 사용, 하위는 markdown + dataframe.
    지출 0원 상품은 표시하지 않음.
    """
    if "campaign_name" not in df.columns:
        st.info("캠페인 데이터가 없습니다.")
        return
    if utm_product_map is None:
        utm_product_map = {}

    adgroup_label = "광고세트" if source == "Meta" else "광고그룹"

    df = df.copy()
    df["__product__"] = df["campaign_name"].apply(_extract_product)

    # 지출 0원 제품 제외
    product_spends = (
        df.groupby("__product__")[spend_col].sum().to_dict()
        if spend_col in df.columns else {}
    )
    valid_products = [p for p in sorted(df["__product__"].unique())
                      if product_spends.get(p, 0) > 0]

    if not valid_products:
        st.info("지출 데이터가 없습니다.")
        return

    for prod_idx, product in enumerate(valid_products):
        prod_df = df[df["__product__"] == product]
        prod_spend  = prod_df[spend_col].sum()  if spend_col  in prod_df.columns else 0
        prod_result = prod_df[result_col].sum() if result_col in prod_df.columns else 0
        prod_clicks = prod_df[click_col].sum()  if click_col  in prod_df.columns else 0
        prod_impr   = prod_df["impressions"].sum() if "impressions" in prod_df.columns else 0
        prod_ctr    = prod_clicks / prod_impr * 100 if prod_impr > 0 else 0
        utm_signups = int(utm_product_map.get(product, 0))
        prod_cpa    = prod_spend / utm_signups if utm_signups > 0 else 0

        # 운영중(지출>0) / 미운영(지출=0) 캠페인 수
        camp_spends = (
            prod_df.groupby("campaign_name")[spend_col].sum()
            if spend_col in prod_df.columns else pd.Series(dtype=float)
        )
        n_running = int((camp_spends > 0).sum())
        n_paused  = int((camp_spends == 0).sum())

        with st.expander(
            f"📦 {product}  |  신청수: {utm_signups:,}  |  지출: ₩{prod_spend:,.0f}  |  "
            f"결과: {int(prod_result):,}  |  CTR: {prod_ctr:.2f}%  |  CPA: ₩{prod_cpa:,.0f}  "
            f"|  운영중 {n_running}개 / 미운영 {n_paused}개"
        ):
            # CSV 다운로드
            dl_df = prod_df.drop(columns=["__product__"], errors="ignore").copy()
            dl_df["신청수(UTM)"] = utm_signups
            csv_bytes = dl_df.to_csv(index=False).encode("utf-8-sig")
            st.download_button(
                label="📥 CSV 다운로드",
                data=csv_bytes,
                file_name=f"{product}.csv",
                mime="text/csv",
                key=f"dl_{key_prefix}{source}_{prod_idx}",
            )

            for i, campaign in enumerate(sorted(prod_df["campaign_name"].unique())):
                camp_df = prod_df[prod_df["campaign_name"] == campaign]
                c_spend  = camp_df[spend_col].sum()  if spend_col  in camp_df.columns else 0
                c_result = camp_df[result_col].sum() if result_col in camp_df.columns else 0
                c_clicks = camp_df[click_col].sum()  if click_col  in camp_df.columns else 0

                if i > 0:
                    st.divider()
                st.markdown(
                    f"**📢 캠페인:** {campaign}  &nbsp;|&nbsp;  "
                    f"지출: ₩{c_spend:,.0f}  &nbsp;|&nbsp;  "
                    f"결과: {int(c_result):,}  &nbsp;|&nbsp;  클릭: {int(c_clicks):,}"
                )

                if adgroup_col and adgroup_col in camp_df.columns:
                    for adgroup in sorted(camp_df[adgroup_col].dropna().unique()):
                        ag_df = camp_df[camp_df[adgroup_col] == adgroup]
                        ag_spend  = ag_df[spend_col].sum()  if spend_col  in ag_df.columns else 0
                        ag_result = ag_df[result_col].sum() if result_col in ag_df.columns else 0
                        ag_clicks = ag_df[click_col].sum()  if click_col  in ag_df.columns else 0

                        st.markdown(
                            f"&emsp;📁 **{adgroup_label}:** {adgroup}  &nbsp;|&nbsp;  "
                            f"지출: ₩{ag_spend:,.0f}  &nbsp;|&nbsp;  "
                            f"결과: {int(ag_result):,}  &nbsp;|&nbsp;  클릭: {int(ag_clicks):,}"
                        )
                        _show_ad_table(ag_df, spend_col, result_col, click_col)
                else:
                    _show_ad_table(camp_df, spend_col, result_col, click_col)


# ── UTM ↔ 광고 매칭 ────────────────────────────────────────────────────────

def build_unified(meta_df: pd.DataFrame, google_df: pd.DataFrame, utm_df: pd.DataFrame):
    if utm_df.empty or "utm_name" not in utm_df.columns:
        return pd.DataFrame(), pd.DataFrame()

    utm_sum = [c for c in ["free_views", "free_checkouts", "free_purchases",
                             "paid_views",  "paid_checkouts",  "paid_purchases"]
               if c in utm_df.columns]
    utm_agg = utm_df.groupby("utm_name")[utm_sum].sum().reset_index()

    meta_agg = None
    if not meta_df.empty:
        hier = [c for c in ["campaign_name", "adset_name", "ad_name"] if c in meta_df.columns]
        num  = [c for c in ["spend", "impressions", "link_clicks", "결과"] if c in meta_df.columns]
        if hier and num:
            meta_agg = meta_df.groupby(hier)[num].sum().reset_index()

    matched, unmatched = [], []

    for _, row in utm_agg.iterrows():
        name     = str(row["utm_name"]).strip()
        utm_info = {
            "utm_이름":   name,
            "utm_방문":   int(row.get("free_views",     0)),
            "utm_신청":   int(row.get("free_checkouts", 0)),
            "utm_등록":   int(row.get("free_purchases", 0)),
            "유료결제":   int(row.get("paid_purchases",  0)),
        }
        found = False

        if meta_agg is not None:
            def _try_match(col, level, adset_col=None):
                if col not in meta_agg.columns:
                    return False
                hit = meta_agg[meta_agg[col] == name]
                if hit.empty:
                    return False
                spend = hit["spend"].sum() if "spend" in hit else 0
                reg   = int(row.get("free_purchases", 0))
                for _, r in hit.iterrows():
                    matched.append({
                        **utm_info,
                        "채널":     "Meta",
                        "매칭수준": level,
                        "캠페인명": r.get("campaign_name", ""),
                        "광고세트명": r.get(adset_col or "adset_name", "") if adset_col is not None else "(전체)",
                        "광고명":   r.get("ad_name", name) if col == "ad_name" else "(전체)",
                        "지출(₩)": round(spend if len(hit) > 1 else r.get("spend", 0), 0),
                        "결과":     int(r.get("결과", 0)) if col == "ad_name" else int(hit["결과"].sum() if "결과" in hit else 0),
                        "노출":     int(r.get("impressions", 0)) if col == "ad_name" else int(hit["impressions"].sum() if "impressions" in hit else 0),
                        "링크클릭": int(r.get("link_clicks", 0)) if col == "ad_name" else int(hit["link_clicks"].sum() if "link_clicks" in hit else 0),
                        "CPA(₩)":  round((spend if len(hit) > 1 else r.get("spend", 0)) / reg, 0) if reg > 0 else 0,
                    })
                    if col != "ad_name":
                        break
                return True

            for col, level, adset_col in [
                ("ad_name",       "광고",    "adset_name"),
                ("adset_name",    "광고세트", None),
                ("campaign_name", "캠페인",  None),
            ]:
                if _try_match(col, level, adset_col=adset_col if col == "ad_name" else None):
                    found = True
                    break

        if not found:
            unmatched.append(utm_info)

    return (
        pd.DataFrame(matched)   if matched   else pd.DataFrame(),
        pd.DataFrame(unmatched) if unmatched else pd.DataFrame(),
    )


# ── 인증 ──────────────────────────────────────────────────────────────────

def _get_secret(key: str, default: str = "") -> str:
    """st.secrets → 환경변수 순으로 값을 읽음 (Streamlit Cloud 호환)"""
    try:
        return st.secrets.get(key, os.getenv(key, default))
    except Exception:
        return os.getenv(key, default)


def _check_auth() -> bool:
    """로그인 여부 확인. 미인증 시 로그인 폼을 표시하고 False 반환.

    비밀번호 종류:
      ADMIN_PASSWORD     — 데이터 수집 버튼 포함 전체 접근
      DASHBOARD_PASSWORD — 조회·차트만 가능 (팀원 공유용)

    두 변수 모두 미설정이면 인증 없이 관리자 모드로 진입 (로컬 개발 편의).
    """
    if st.session_state.get("authenticated"):
        return True

    admin_pw  = _get_secret("ADMIN_PASSWORD")
    viewer_pw = _get_secret("DASHBOARD_PASSWORD")

    # 비밀번호 미설정 → 인증 없이 관리자 모드 (로컬 전용)
    if not admin_pw and not viewer_pw:
        st.session_state["authenticated"] = True
        st.session_state["is_admin"] = True
        return True

    # ── 로그인 UI ──────────────────────────────────────────────────────────
    _, col_form, _ = st.columns([1, 1.2, 1])
    with col_form:
        st.markdown("<br><br>", unsafe_allow_html=True)
        st.title("📊 광고 성과 대시보드")
        st.caption("비밀번호를 입력해 접속하세요.")
        with st.form("login_form", clear_on_submit=True):
            pw = st.text_input("비밀번호", type="password", placeholder="비밀번호 입력")
            submitted = st.form_submit_button("로그인", use_container_width=True)
        if submitted:
            if admin_pw and pw == admin_pw:
                st.session_state["authenticated"] = True
                st.session_state["is_admin"] = True
                st.rerun()
            elif viewer_pw and pw == viewer_pw:
                st.session_state["authenticated"] = True
                st.session_state["is_admin"] = False
                st.rerun()
            else:
                st.error("비밀번호가 올바르지 않습니다.")

    return False


# ── 사이드바 ──────────────────────────────────────────────────────────────

if not _check_auth():
    st.stop()

st.sidebar.title("📊 광고 대시보드")
st.sidebar.markdown("---")

today         = date.today()
default_start = today - timedelta(days=13)

date_range = st.sidebar.date_input(
    "조회 기간",
    value=(default_start, today),
    min_value=date(2025, 1, 1),
    max_value=today,
)

if isinstance(date_range, (list, tuple)) and len(date_range) == 2:
    start_date, end_date = date_range
else:
    start_date = end_date = date_range

start_str = start_date.isoformat()
end_str   = end_date.isoformat()

st.sidebar.markdown("---")
if st.sidebar.button("새로고침", use_container_width=True):
    st.cache_data.clear()
    st.rerun()

# ── 데이터 수집 버튼 (관리자 전용) ────────────────────────────────────────
if st.session_state.get("is_admin", False):
    st.sidebar.markdown("---")
    st.sidebar.markdown("**데이터 수집**")

    collect_range = st.sidebar.date_input(
        "수집 기간",
        value=(today - timedelta(days=1), today - timedelta(days=1)),
        min_value=date(2025, 1, 1),
        max_value=today,
        key="collect_range",
    )
    if isinstance(collect_range, (list, tuple)) and len(collect_range) == 2:
        c_start, c_end = collect_range
    else:
        c_start = c_end = collect_range
    c_start_str = c_start.isoformat()
    c_end_str   = c_end.isoformat()
    days_selected = (c_end - c_start).days + 1
    st.sidebar.caption(f"선택: {days_selected}일 ({c_start_str} ~ {c_end_str})")

    def _run_script(script_name: str, extra_args: list = None) -> tuple[bool, str]:
        cmd = [sys.executable, os.path.join(SCRIPT_DIR, script_name)]
        if extra_args:
            cmd.extend(extra_args)
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=SCRIPT_DIR,
            timeout=600,
        )
        output = (result.stdout + result.stderr).strip()
        return result.returncode == 0, output

    col_btn1, col_btn2, col_btn3 = st.sidebar.columns(3)

    with col_btn1:
        if st.button("UTM 수집", use_container_width=True):
            with st.sidebar:
                with st.spinner(f"UTM 수집 중... ({days_selected}일)"):
                    ok, out = _run_script("collect_utm.py",
                        ["--start-date", c_start_str, "--end-date", c_end_str])
                if ok:
                    st.success(f"UTM 완료 ({days_selected}일)")
                else:
                    st.error("UTM 실패")
                with st.expander("로그"):
                    st.code(out[-1500:] if len(out) > 1500 else out)
            st.cache_data.clear()
            st.rerun()

    with col_btn2:
        if st.button("Meta 수집", use_container_width=True):
            with st.sidebar:
                with st.spinner(f"Meta 수집 중... ({days_selected}일)"):
                    ok, out = _run_script("collect_meta.py",
                        ["--start-date", c_start_str, "--end-date", c_end_str])
                if ok:
                    st.success(f"Meta 완료 ({days_selected}일)")
                else:
                    st.error("Meta 실패")
                with st.expander("로그"):
                    st.code(out[-1500:] if len(out) > 1500 else out)
            st.cache_data.clear()
            st.rerun()

    with col_btn3:
        if st.button("Google 수집", use_container_width=True):
            with st.sidebar:
                with st.spinner(f"Google 수집 중... ({days_selected}일)"):
                    ok, out = _run_script("collect_google.py",
                        ["--start-date", c_start_str, "--end-date", c_end_str])
                if ok:
                    st.success(f"Google 완료 ({days_selected}일)")
                else:
                    st.error("Google 실패")
                with st.expander("로그"):
                    st.code(out[-1500:] if len(out) > 1500 else out)
            st.cache_data.clear()
            st.rerun()

    if st.sidebar.button("전체 수집 (UTM + Meta + Google)", use_container_width=True):
        with st.sidebar:
            with st.spinner(f"전체 수집 중... ({days_selected}일)"):
                ok1, out1 = _run_script("collect_utm.py",
                    ["--start-date", c_start_str, "--end-date", c_end_str])
                ok2, out2 = _run_script("collect_meta.py",
                    ["--start-date", c_start_str, "--end-date", c_end_str])
                ok3, out3 = _run_script("collect_google.py",
                    ["--start-date", c_start_str, "--end-date", c_end_str])
            status = "완료" if (ok1 and ok2 and ok3) else "일부 실패"
            (st.success if ok1 and ok2 and ok3 else st.warning)(f"전체 수집 {status} ({days_selected}일)")
            with st.expander("로그", expanded=not (ok1 and ok2 and ok3)):
                st.code(f"[UTM] {'✓' if ok1 else '✗'}\n{out1[-800:]}\n\n[Meta] {'✓' if ok2 else '✗'}\n{out2[-800:]}\n\n[Google] {'✓' if ok3 else '✗'}\n{out3[-800:]}")
        if ok1 and ok2 and ok3:
            st.cache_data.clear()
            st.rerun()

st.sidebar.markdown("---")
st.sidebar.caption(f"DB: {DB_URL.split('//')[1].split('.')[0] if DB_URL else '미설정'}")
_role = "관리자" if st.session_state.get("is_admin") else "팀원"
st.sidebar.caption(f"접속 권한: {_role}")
if st.sidebar.button("로그아웃", use_container_width=True):
    st.session_state.clear()
    st.rerun()

# ── 데이터 로드 ────────────────────────────────────────────────────────────

meta_df   = load_meta(start_str, end_str)
utm_df    = load_utm(start_str, end_str)
google_df = load_google(start_str, end_str)

# ── 자사 / 기타 분리 ──────────────────────────────────────────────────────

meta_own_df,   meta_other_df   = _split_own_other(meta_df,   "campaign_name")
google_own_df, google_other_df = _split_own_other(google_df, "campaign_name")
utm_own_df,    utm_other_df    = _split_own_other(utm_df,    "utm_name")

st.title("광고 성과 대시보드")
st.caption(f"조회 기간: {start_str} ~ {end_str}")

# 탭 순서: 통합현황 - UTM신청자 - Meta광고(자사) - Google광고(자사) - 기타
tab_combined, tab_utm, tab_meta, tab_google, tab_other = st.tabs([
    "📊 통합 현황", "🔗 UTM 신청자", "📘 Meta 광고", "🔍 Google 광고", "📋 기타"
])


# ════════════════════════════════════════════════════════════════════════════
# TAB 1: 통합 현황 (자사 데이터만 사용)
# ════════════════════════════════════════════════════════════════════════════

with tab_combined:

    # ── 채널별 지출 요약 ──────────────────────────────────────────────────
    st.subheader("채널별 지출 요약")

    _utm_map_meta_c   = _utm_product_map(utm_own_df, ["meta", "instagram"])
    _utm_map_google_c = _utm_product_map(utm_own_df, ["google"])
    _meta_signup_c    = sum(_utm_map_meta_c.values())   if _utm_map_meta_c   else 0
    _google_signup_c  = sum(_utm_map_google_c.values()) if _utm_map_google_c else 0
    # 인스턴트 양식 신청수 합산
    if not meta_own_df.empty and "is_instant_form" in meta_own_df.columns:
        _meta_signup_c += int(meta_own_df[meta_own_df["is_instant_form"]]["결과"].sum())

    _sum_rows = []
    _pie_rows = []

    # Meta 계정 레이블 (자사/기타 공통)
    _albl_c = {k: v for k, v in {
        os.getenv("META_AD_ACCOUNT_A", ""): "로켓스파크(머니톡톡)",
        os.getenv("META_AD_ACCOUNT_B", ""): "ampm(러닝티)",
        os.getenv("META_AD_ACCOUNT_C", "act_1579306933279906"): "카페24(러닝티)",
    }.items() if k}

    # Google 계정 레이블 (.env GOOGLE_ACCOUNT_{id} 키로 관리)
    def _google_acct_label(account_id: str) -> str:
        return os.getenv(f"GOOGLE_ACCOUNT_{account_id}", account_id)

    # Meta 자사
    if not meta_own_df.empty and "spend" in meta_own_df.columns:
        _lc_c = "link_clicks" if "link_clicks" in meta_own_df.columns else "clicks"
        _ms = meta_own_df["spend"].sum()
        _mr = int(meta_own_df["결과"].sum())
        _mc = int(meta_own_df[_lc_c].sum()) if _lc_c in meta_own_df.columns else 0
        _mi = int(meta_own_df["impressions"].sum()) if "impressions" in meta_own_df.columns else 0
        _sum_rows.append({
            "채널":          "📘 Meta 광고",
            "신청수":        f"{_meta_signup_c:,}",
            "지출":          f"₩{_ms:,.0f}",
            "결과":          f"{_mr:,}",
            "CTR":           f"{_mc/_mi*100:.2f}%" if _mi > 0 else "-",
            "CPA":           f"₩{_ms/_meta_signup_c:,.0f}" if _meta_signup_c > 0 else "-",
            "클릭대비전환율": f"{_meta_signup_c/_mc*100:.2f}%" if _mc > 0 else "-",
        })
        _pie_rows.append({"채널": "Meta (자사)", "지출": _ms})

        if "account_id" in meta_own_df.columns:
            _by_acct_m = (
                meta_own_df.groupby("account_id")["spend"].sum()
                .reset_index().sort_values("spend", ascending=False)
            )
            for _, _ar in _by_acct_m.iterrows():
                _sum_rows.append({
                    "채널":          f"  └ {_albl_c.get(_ar['account_id'], _ar['account_id'])}",
                    "신청수":        "",
                    "지출":          f"₩{_ar['spend']:,.0f}",
                    "결과":          "", "CTR": "", "CPA": "", "클릭대비전환율": "",
                })

    # Google 자사
    if not google_own_df.empty and "cost" in google_own_df.columns:
        _gs = google_own_df["cost"].sum()
        _gresult = int(google_own_df["conversions"].sum()) if "conversions" in google_own_df.columns else 0
        _gc = int(google_own_df["clicks"].sum()) if "clicks" in google_own_df.columns else 0
        _gi = int(google_own_df["impressions"].sum()) if "impressions" in google_own_df.columns else 0
        _sum_rows.append({
            "채널":          "🔍 Google 광고",
            "신청수":        f"{_google_signup_c:,}",
            "지출":          f"₩{_gs:,.0f}",
            "결과":          f"{_gresult:,}",
            "CTR":           f"{_gc/_gi*100:.2f}%" if _gi > 0 else "-",
            "CPA":           f"₩{_gs/_google_signup_c:,.0f}" if _google_signup_c > 0 else "-",
            "클릭대비전환율": f"{_google_signup_c/_gc*100:.2f}%" if _gc > 0 else "-",
        })
        _pie_rows.append({"채널": "Google (자사)", "지출": _gs})

        if "account_id" in google_own_df.columns:
            _by_acct_g = (
                google_own_df.groupby("account_id")["cost"].sum()
                .reset_index().sort_values("cost", ascending=False)
            )
            for _, _gr_row in _by_acct_g.iterrows():
                _sum_rows.append({
                    "채널":          f"  └ {_google_acct_label(_gr_row['account_id'])}",
                    "신청수":        "",
                    "지출":          f"₩{_gr_row['cost']:,.0f}",
                    "결과":          "", "CTR": "", "CPA": "", "클릭대비전환율": "",
                })

    # 기타 광고 합산 + 계정별 하위 행
    _other_meta_spend   = meta_other_df["spend"].sum()  if not meta_other_df.empty  and "spend" in meta_other_df.columns  else 0
    _other_google_spend = google_other_df["cost"].sum() if not google_other_df.empty and "cost"  in google_other_df.columns else 0
    _other_spend = _other_meta_spend + _other_google_spend

    if _other_spend > 0:
        _sum_rows.append({
            "채널":          "📋 기타 광고",
            "신청수":        "",
            "지출":          f"₩{_other_spend:,.0f}",
            "결과":          "", "CTR": "", "CPA": "", "클릭대비전환율": "",
        })
        _pie_rows.append({"채널": "기타 광고", "지출": _other_spend})

        # Meta 기타 계정별
        if _other_meta_spend > 0 and "account_id" in meta_other_df.columns:
            _by_acct_mo = (
                meta_other_df.groupby("account_id")["spend"].sum()
                .reset_index().sort_values("spend", ascending=False)
            )
            for _, _row in _by_acct_mo.iterrows():
                _sum_rows.append({
                    "채널":          f"  └ [Meta] {_albl_c.get(_row['account_id'], _row['account_id'])}",
                    "신청수":        "",
                    "지출":          f"₩{_row['spend']:,.0f}",
                    "결과":          "", "CTR": "", "CPA": "", "클릭대비전환율": "",
                })

        # Google 기타 계정별
        if _other_google_spend > 0 and "account_id" in google_other_df.columns:
            _by_acct_go = (
                google_other_df.groupby("account_id")["cost"].sum()
                .reset_index().sort_values("cost", ascending=False)
            )
            for _, _row in _by_acct_go.iterrows():
                _sum_rows.append({
                    "채널":          f"  └ [Google] {_google_acct_label(_row['account_id'])}",
                    "신청수":        "",
                    "지출":          f"₩{_row['cost']:,.0f}",
                    "결과":          "", "CTR": "", "CPA": "", "클릭대비전환율": "",
                })

    if _sum_rows:
        _sum_df = pd.DataFrame(_sum_rows)
        _pie_df = pd.DataFrame(_pie_rows)
        col_s1, col_s2 = st.columns([1, 2])
        with col_s1:
            _fig_pie = px.pie(_pie_df, values="지출", names="채널", title="채널별 지출 비율", hole=0.4)
            _fig_pie.update_layout(height=320, margin=dict(t=30, b=10))
            st.plotly_chart(_fig_pie, use_container_width=True)
        with col_s2:
            _tbl_height = min(35 * (len(_sum_df) + 1) + 12, 420)
            st.dataframe(_sum_df, use_container_width=True, hide_index=True, height=_tbl_height)
    else:
        st.info("광고 지출 데이터가 없습니다.")

    st.markdown("---")

    # ── UTM 전환 성과 (자사 UTM만, 계층 토글) ────────────────────────────
    st.subheader("UTM 전환 성과")

    if not utm_own_df.empty:
        total_fc = int(utm_own_df["free_checkouts"].sum()) if "free_checkouts" in utm_own_df.columns else 0
        total_fp = int(utm_own_df["free_purchases"].sum()) if "free_purchases" in utm_own_df.columns else 0
        total_pc = int(utm_own_df["paid_checkouts"].sum()) if "paid_checkouts" in utm_own_df.columns else 0
        total_pp = int(utm_own_df["paid_purchases"].sum()) if "paid_purchases" in utm_own_df.columns else 0
        conv_rate = (total_pp / total_fp * 100) if total_fp > 0 else 0

        col_u1, col_u2, col_u3, col_u4, col_u5 = st.columns(5)
        col_u1.metric("무료시도", f"{total_fc:,}")
        col_u2.metric("무료완료", f"{total_fp:,}")
        col_u3.metric("유료시도", f"{total_pc:,}")
        col_u4.metric("유료완료", f"{total_pp:,}")
        col_u5.metric("전환율(무료→유료)", f"{conv_rate:.1f}%")

        if "utm_name" in utm_own_df.columns:
            utm_hier = utm_own_df.copy()
            utm_hier["__product__"]     = utm_hier["utm_name"].apply(_extract_product)
            utm_hier["__media_group__"] = utm_hier["utm_name"].apply(_extract_media_group)

            products = sorted(utm_hier["__product__"].unique())

            for prod_idx, product in enumerate(products):
                prod_sub = utm_hier[utm_hier["__product__"] == product]
                p_fp = int(prod_sub["free_purchases"].sum()) if "free_purchases" in prod_sub.columns else 0
                p_pp = int(prod_sub["paid_purchases"].sum()) if "paid_purchases" in prod_sub.columns else 0
                p_conv = (p_pp / p_fp * 100) if p_fp > 0 else 0

                with st.expander(
                    f"📦 {product}  |  무료완료: {p_fp:,}건  |  유료완료: {p_pp:,}건  |  전환율: {p_conv:.1f}%"
                ):
                    media_groups = sorted(prod_sub["__media_group__"].unique())
                    for mg_idx, media_group in enumerate(media_groups):
                        mg_sub = prod_sub[prod_sub["__media_group__"] == media_group]
                        mg_fp = int(mg_sub["free_purchases"].sum()) if "free_purchases" in mg_sub.columns else 0
                        mg_pp = int(mg_sub["paid_purchases"].sum()) if "paid_purchases" in mg_sub.columns else 0
                        mg_conv = (mg_pp / mg_fp * 100) if mg_fp > 0 else 0

                        if mg_idx > 0:
                            st.divider()
                        if media_group != product:
                            st.markdown(
                                f"**📢 {media_group}**  &nbsp;|&nbsp;  "
                                f"무료완료: {mg_fp:,}건  &nbsp;|&nbsp;  "
                                f"유료완료: {mg_pp:,}건  &nbsp;|&nbsp;  "
                                f"전환율: {mg_conv:.1f}%"
                            )

                        # 개별 UTM 테이블
                        group_keys = [c for c in ["utm_name", "utm_source"] if c in mg_sub.columns]
                        sum_keys   = [c for c in ["free_checkouts", "free_purchases",
                                                   "paid_checkouts", "paid_purchases"]
                                      if c in mg_sub.columns]
                        if group_keys and sum_keys:
                            utm_tbl = (
                                mg_sub.groupby(group_keys)[sum_keys].sum()
                                .reset_index()
                                .rename(columns={
                                    "utm_name":       "UTM 이름",
                                    "utm_source":     "채널",
                                    "free_checkouts": "무료시도",
                                    "free_purchases": "무료완료",
                                    "paid_checkouts": "유료시도",
                                    "paid_purchases": "유료완료",
                                })
                            )
                            sort_col = "무료완료" if "무료완료" in utm_tbl.columns else utm_tbl.columns[-1]
                            utm_tbl = utm_tbl.sort_values(sort_col, ascending=False)
                            st.dataframe(utm_tbl, use_container_width=True, hide_index=True)
    else:
        st.info("UTM 데이터가 없습니다.")

    st.markdown("---")

    # ── UTM ↔ 광고 매칭 ──────────────────────────────────────────────────
    st.subheader("UTM ↔ 광고 매칭 현황")
    st.caption("UTM 이름이 광고명/광고세트명/캠페인명과 일치할 경우 자동 연결됩니다.")

    matched_df, unmatched_df = build_unified(meta_own_df, google_own_df, utm_own_df)

    if matched_df.empty and unmatched_df.empty:
        st.info("UTM 데이터가 없습니다.")
    else:
        if not matched_df.empty:
            with st.expander(f"✅ 매칭 완료: {len(matched_df)}건"):
                disp_m = matched_df.copy()
                disp_m["지출(₩)"] = disp_m["지출(₩)"].map("₩{:,.0f}".format)
                disp_m["CPA(₩)"]  = disp_m["CPA(₩)"].map("₩{:,.0f}".format)
                col_order = [c for c in [
                    "매칭수준", "채널", "캠페인명", "광고세트명", "광고명",
                    "지출(₩)", "결과", "노출", "링크클릭", "CPA(₩)",
                    "utm_이름", "utm_방문", "utm_신청", "utm_등록", "유료결제",
                ] if c in disp_m.columns]
                st.dataframe(disp_m[col_order], use_container_width=True, hide_index=True)
        else:
            st.info("매칭된 UTM이 없습니다.")

        if not unmatched_df.empty:
            with st.expander(f"❌ 미매칭 UTM {len(unmatched_df)}건"):
                st.dataframe(unmatched_df, use_container_width=True, hide_index=True)

    st.markdown("---")

    # ── 상품별 광고 성과 비교 ─────────────────────────────────────────────
    st.subheader("상품별 광고 성과 비교")
    st.caption("상품명을 선택하면 해당 상품의 일별 데이터가 그래프에 추가됩니다.")

    if not utm_own_df.empty and "utm_name" in utm_own_df.columns:
        utm_prod_work = utm_own_df.copy()
        utm_prod_work["상품"] = utm_prod_work["utm_name"].apply(_extract_product)
        all_products = sorted(utm_prod_work["상품"].dropna().unique().tolist())

        if "free_purchases" in utm_prod_work.columns:
            top_products = (
                utm_prod_work.groupby("상품")["free_purchases"].sum()
                .sort_values(ascending=False)
                .head(3).index.tolist()
            )
        else:
            top_products = all_products[:3]

        col_sel, col_metric = st.columns([3, 1])
        with col_sel:
            sel_products_chart = st.multiselect(
                "비교할 상품 선택",
                options=all_products,
                default=top_products,
                key="product_chart_sel",
            )
        with col_metric:
            metric_choice = st.selectbox(
                "지표",
                options=["신청수(무료완료)", "유료결제"],
                key="product_chart_metric",
            )

        if sel_products_chart:
            metric_col = "free_purchases" if metric_choice == "신청수(무료완료)" else "paid_purchases"
            fig = go.Figure()
            for product in sel_products_chart:
                prod_sub = utm_prod_work[utm_prod_work["상품"] == product]
                if metric_col not in prod_sub.columns:
                    continue
                daily = prod_sub.groupby("date")[metric_col].sum().reset_index().sort_values("date")
                fig.add_trace(go.Scatter(
                    x=daily["date"], y=daily[metric_col],
                    name=product, mode="lines+markers",
                ))
            fig.update_layout(
                xaxis_title="날짜",
                yaxis_title=metric_choice,
                height=380, margin=dict(t=10, b=10),
                legend=dict(orientation="h", x=0.5, xanchor="center", y=1.08),
            )
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("비교할 상품을 선택하세요.")
    else:
        st.info("UTM 데이터가 없습니다.")


# ════════════════════════════════════════════════════════════════════════════
# TAB 2: UTM 신청자
# ════════════════════════════════════════════════════════════════════════════

with tab_utm:
    if utm_df.empty:
        st.info("해당 기간 UTM 데이터가 없습니다.")
    else:
        utm_work = utm_df.copy()
        if "utm_name" in utm_work.columns:
            utm_work["상품"] = utm_work["utm_name"].apply(_extract_product)
            all_products = sorted(utm_work["상품"].dropna().unique().tolist())
            sel_products = st.multiselect(
                "상품별 필터 (6자리숫자-이름)",
                options=all_products,
                default=[],
                placeholder="전체 보기 (미선택 시 전체)",
            )
            if sel_products:
                utm_work = utm_work[utm_work["상품"].isin(sel_products)]

        fc  = int(utm_work["free_checkouts"].sum()) if "free_checkouts" in utm_work.columns else 0
        fp  = int(utm_work["free_purchases"].sum()) if "free_purchases" in utm_work.columns else 0
        pc  = int(utm_work["paid_checkouts"].sum()) if "paid_checkouts" in utm_work.columns else 0
        pp  = int(utm_work["paid_purchases"].sum()) if "paid_purchases" in utm_work.columns else 0
        conv_rate = (pp / fp * 100) if fp > 0 else 0

        col1, col2, col3, col4, col5 = st.columns(5)
        col1.metric("무료시도",          f"{fc:,}")
        col2.metric("무료완료",          f"{fp:,}")
        col3.metric("유료시도",          f"{pc:,}")
        col4.metric("유료완료",          f"{pp:,}")
        col5.metric("전환율(무료→유료)", f"{conv_rate:.1f}%")

        st.markdown("---")

        agg_dict = {col: (col, "sum") for col in
                    ["free_checkouts", "free_purchases", "paid_checkouts", "paid_purchases"]
                    if col in utm_work.columns}
        daily_utm = (
            utm_work.groupby("date")
            .agg(**agg_dict)
            .reset_index().sort_values("date")
        )

        col_c1, col_c2 = st.columns(2)
        with col_c1:
            st.subheader("일별 무료/유료 전환")
            fig = go.Figure()
            for trace_col, trace_name in [
                ("free_checkouts", "무료시도"),
                ("free_purchases", "무료완료"),
                ("paid_checkouts", "유료시도"),
                ("paid_purchases", "유료완료"),
            ]:
                if trace_col in daily_utm.columns:
                    fig.add_trace(go.Bar(x=daily_utm["date"], y=daily_utm[trace_col],
                                         name=trace_name))
            fig.update_layout(barmode="group", height=300, margin=dict(t=10, b=10))
            st.plotly_chart(fig, use_container_width=True)

        with col_c2:
            st.subheader("일별 전환율 (무료완료→유료완료)")
            if "free_purchases" in daily_utm.columns and "paid_purchases" in daily_utm.columns:
                daily_utm["conv"] = (
                    daily_utm["paid_purchases"] /
                    daily_utm["free_purchases"].replace(0, float("nan")) * 100
                ).fillna(0)
                fig2 = px.line(daily_utm, x="date", y="conv", markers=True,
                               labels={"conv": "전환율(%)", "date": "날짜"})
                fig2.update_layout(height=300, margin=dict(t=10, b=10))
                st.plotly_chart(fig2, use_container_width=True)
            else:
                st.info("전환율 계산에 필요한 데이터가 없습니다.")

        st.subheader("UTM별 성과 요약")
        group_keys = [c for c in ["utm_name", "utm_source", "utm_medium", "utm_campaign"]
                      if c in utm_work.columns]
        sum_keys   = [c for c in ["free_checkouts", "free_purchases",
                                   "paid_checkouts", "paid_purchases"]
                      if c in utm_work.columns]
        if group_keys and sum_keys:
            utm_summary = (
                utm_work.groupby(group_keys)[sum_keys]
                .sum()
                .sort_values("free_purchases" if "free_purchases" in sum_keys else sum_keys[0],
                             ascending=False)
                .reset_index()
                .rename(columns={
                    "free_checkouts": "무료시도",
                    "free_purchases": "무료완료",
                    "paid_checkouts": "유료시도",
                    "paid_purchases": "유료완료",
                })
            )
            st.dataframe(utm_summary, use_container_width=True, hide_index=True)


# ════════════════════════════════════════════════════════════════════════════
# TAB 3: Meta 광고 (자사 전용)
# ════════════════════════════════════════════════════════════════════════════

with tab_meta:
    if meta_own_df.empty:
        st.info("해당 기간 자사 Meta 광고 데이터가 없습니다.")
    else:
        lc = "link_clicks" if "link_clicks" in meta_own_df.columns else "clicks"
        total_spend  = meta_own_df["spend"].sum()
        total_impr   = int(meta_own_df["impressions"].sum()) if "impressions" in meta_own_df.columns else 0
        total_clicks = int(meta_own_df[lc].sum()) if lc in meta_own_df.columns else 0
        total_result = int(meta_own_df["결과"].sum())
        avg_ctr      = total_clicks / total_impr * 100 if total_impr > 0 else 0

        meta_utm_map   = _utm_product_map(utm_own_df, ["meta", "instagram"])
        meta_signup    = sum(meta_utm_map.values()) if meta_utm_map else 0
        # 인스턴트 양식 캠페인의 신청수(결과값) 합산
        if "is_instant_form" in meta_own_df.columns:
            meta_signup += int(meta_own_df[meta_own_df["is_instant_form"]]["결과"].sum())
        avg_cpa      = total_spend / meta_signup if meta_signup > 0 else 0

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("신청수",  f"{meta_signup:,}")
        c2.metric("총 지출",      f"₩{total_spend:,.0f}")
        c3.metric("결과(전환)",   f"{total_result:,}")
        c4.metric("CTR(%)",      f"{avg_ctr:.2f}%")
        c5.metric("CPA(₩)",      f"₩{avg_cpa:,.0f}")

        if "result_indicator" in meta_own_df.columns:
            indicators = [i for i in meta_own_df["result_indicator"].dropna().unique() if i]
            if indicators:
                st.caption(f"결과 지표: {', '.join(set(indicators))}")

        # 계정 토큰 상태
        _acct_token_checks = [
            (os.getenv("META_AD_ACCOUNT_A", ""), _meta_raw_token("META_ACCESS_TOKEN_A"), "로켓스파크(머니톡톡)"),
            (os.getenv("META_AD_ACCOUNT_B", ""), _meta_raw_token("META_ACCESS_TOKEN_B"), "ampm(러닝티)"),
            (os.getenv("META_AD_ACCOUNT_C", "act_1579306933279906"), _meta_raw_token("META_ACCESS_TOKEN_B"), "카페24(러닝티)"),
        ]
        _acct_ids_own   = set(meta_own_df["account_id"].unique())   if "account_id" in meta_own_df.columns   else set()
        _acct_ids_other = set(meta_other_df["account_id"].unique()) if "account_id" in meta_other_df.columns else set()
        with st.expander("🔑 Meta 계정 토큰 상태 확인"):
            for _aid, _tok, _lbl in _acct_token_checks:
                _s = _check_meta_token(_aid, _tok, _lbl)
                _icon = "✅" if _s["valid"] else "❌"
                if not _aid:
                    _data_flag = ""
                elif _aid in _acct_ids_own:
                    _data_flag = " · 📊 데이터 있음 (자사)"
                elif _aid in _acct_ids_other:
                    _data_flag = " · 📊 데이터 있음 (기타) — 캠페인명이 기타 분류 조건에 해당"
                else:
                    _data_flag = " · ⚠️ 조회 기간 내 데이터 없음 (수집 미실행 또는 해당 기간 광고 없음)"
                st.markdown(f"{_icon} **{_s['label']}** (`{_s['account_id']}`) — {_s['reason']}{_data_flag}")

        st.markdown("---")

        # 일별 데이터 준비
        daily_agg_kw: dict = {
            "spend":       ("spend",       "sum"),
            "결과":        ("결과",         "sum"),
            "impressions": ("impressions",  "sum"),
        }
        if lc in meta_own_df.columns:
            daily_agg_kw["클릭"] = (lc, "sum")
        daily_m = meta_own_df.groupby("date").agg(**daily_agg_kw).reset_index().sort_values("date")
        if "클릭" not in daily_m.columns:
            daily_m["클릭"] = 0

        utm_daily_meta = _utm_daily_by_source(utm_own_df, ["meta", "instagram"])
        if not utm_daily_meta.empty:
            daily_m = daily_m.merge(utm_daily_meta, on="date", how="left")
            daily_m["신청수"] = daily_m["신청수"].fillna(0).astype(int)
        else:
            daily_m["신청수"] = 0

        daily_m["CTR"] = (daily_m["클릭"] / daily_m["impressions"].replace(0, float("nan")) * 100).fillna(0).round(2)
        daily_m["CPA"] = (daily_m["spend"] / daily_m["신청수"].replace(0, float("nan"))).fillna(0).round(0)

        col_c1, col_c2 = st.columns(2)
        with col_c1:
            st.subheader("일별 지출 / 신청수 / 결과")
            fig = go.Figure()
            fig.add_trace(go.Bar(x=daily_m["date"], y=daily_m["spend"],
                                  name="지출(₩)", marker_color="#636EFA"))
            fig.add_trace(go.Scatter(x=daily_m["date"], y=daily_m["신청수"],
                                      name="신청수", mode="lines+markers",
                                      yaxis="y2", line=dict(color="#EF553B")))
            fig.add_trace(go.Scatter(x=daily_m["date"], y=daily_m["결과"],
                                      name="결과", mode="lines+markers",
                                      yaxis="y2", line=dict(color="#00CC96")))
            fig.update_layout(
                yaxis=dict(title="지출(₩)"),
                yaxis2=dict(title="건수", overlaying="y", side="right"),
                height=320, margin=dict(t=10, b=10),
                legend=dict(orientation="h", x=0.5, xanchor="center", y=1.05),
            )
            st.plotly_chart(fig, use_container_width=True)

        with col_c2:
            st.subheader("일별 광고지표 (CTR, CPA)")
            fig2 = go.Figure()
            fig2.add_trace(go.Scatter(x=daily_m["date"], y=daily_m["CTR"],
                                       name="CTR(%)", mode="lines+markers",
                                       line=dict(color="#AB63FA")))
            fig2.add_trace(go.Scatter(x=daily_m["date"], y=daily_m["CPA"],
                                       name="CPA(₩)", mode="lines+markers",
                                       yaxis="y2", line=dict(color="#FFA15A")))
            fig2.update_layout(
                yaxis=dict(title="CTR(%)"),
                yaxis2=dict(title="CPA(₩)", overlaying="y", side="right"),
                height=320, margin=dict(t=10, b=10),
                legend=dict(orientation="h", x=0.5, xanchor="center", y=1.05),
            )
            st.plotly_chart(fig2, use_container_width=True)

        # 계정별 파이 + 캠페인별 성과
        col_pie, col_camp = st.columns([1, 3])

        if "account_id" in meta_own_df.columns:
            _acct_env = [
                ("META_AD_ACCOUNT_A", "로켓스파크(머니톡톡)"),
                ("META_AD_ACCOUNT_B", "ampm(러닝티)"),
                ("META_AD_ACCOUNT_C", "카페24(러닝티)"),
            ]
            account_labels = {
                os.getenv(env_key, ""): label
                for env_key, label in _acct_env
                if os.getenv(env_key, "")
            }
            by_acct = meta_own_df.groupby("account_id")["spend"].sum().reset_index()
            by_acct["name"] = by_acct["account_id"].map(account_labels).fillna(by_acct["account_id"])
            total_acct_spend = by_acct["spend"].sum()
            pull_vals = [
                0.12 if total_acct_spend > 0 and (row["spend"] / total_acct_spend) < 0.05 else 0
                for _, row in by_acct.iterrows()
            ]
            with col_pie:
                st.subheader("계정별 지출")
                fig3 = go.Figure(go.Pie(
                    labels=by_acct["name"],
                    values=by_acct["spend"],
                    hole=0.4,
                    pull=pull_vals,
                    textinfo="label+percent",
                    customdata=by_acct["spend"],
                    hovertemplate="%{label}<br>₩%{customdata:,.0f}<br>%{percent}<extra></extra>",
                ))
                fig3.update_layout(
                    height=380,
                    margin=dict(t=10, b=90, l=0, r=0),
                    showlegend=True,
                    legend=dict(orientation="h", x=0.5, xanchor="center", y=-0.15),
                )
                st.plotly_chart(fig3, use_container_width=True)

        if "campaign_name" in meta_own_df.columns:
            with col_camp:
                st.subheader("캠페인별 성과")
                st.dataframe(
                    _fmt_campaign_meta(campaign_summary_meta(meta_own_df, meta_utm_map)),
                    use_container_width=True,
                    hide_index=True,
                )

        # 상품별 계층 토글
        st.markdown("---")
        st.subheader("상품별 성과 (캠페인 → 광고세트 → 광고)")
        adgroup_col_m = "adset_name" if "adset_name" in meta_own_df.columns else None
        _render_hierarchy(
            meta_own_df, "spend", "결과", lc, adgroup_col_m,
            utm_product_map=meta_utm_map, source="Meta", key_prefix="own_",
        )

        with st.expander("광고 상세 테이블"):
            show_cols = [c for c in ["date", "account_id", "campaign_name", "adset_name",
                                      "ad_name", "spend", "결과", "result_indicator",
                                      "impressions", "link_clicks", "cpc", "cpm",
                                      "purchases", "leads"] if c in meta_own_df.columns]
            st.dataframe(meta_own_df[show_cols].sort_values("date", ascending=False),
                         use_container_width=True, hide_index=True)


# ════════════════════════════════════════════════════════════════════════════
# TAB 4: Google 광고 (자사 전용)
# ════════════════════════════════════════════════════════════════════════════

with tab_google:
    if google_own_df.empty:
        st.info("해당 기간 자사 Google 광고 데이터가 없습니다.")
    else:
        total_cost  = google_own_df["cost"].sum()        if "cost"        in google_own_df.columns else 0
        total_gimpr = int(google_own_df["impressions"].sum()) if "impressions" in google_own_df.columns else 0
        total_gclk  = int(google_own_df["clicks"].sum())     if "clicks"      in google_own_df.columns else 0
        total_conv  = google_own_df["conversions"].sum()  if "conversions" in google_own_df.columns else 0
        avg_gctr    = total_gclk / total_gimpr * 100 if total_gimpr > 0 else 0

        google_utm_map = _utm_product_map(utm_own_df, ["google"])
        google_signup  = sum(google_utm_map.values()) if google_utm_map else 0
        avg_gcpa       = total_cost / google_signup if google_signup > 0 else 0

        g1, g2, g3, g4, g5 = st.columns(5)
        g1.metric("신청수(UTM)",  f"{google_signup:,}")
        g2.metric("총 비용",      f"₩{total_cost:,.0f}")
        g3.metric("결과(전환)",   f"{int(total_conv):,}")
        g4.metric("CTR(%)",      f"{avg_gctr:.2f}%")
        g5.metric("CPA(₩)",      f"₩{avg_gcpa:,.0f}")

        st.markdown("---")

        # 일별 데이터 준비
        daily_g = (
            google_own_df.groupby("date")
            .agg(cost=("cost", "sum"), conversions=("conversions", "sum"),
                 impressions=("impressions", "sum"), clicks=("clicks", "sum"))
            .reset_index().sort_values("date")
        )

        utm_daily_google = _utm_daily_by_source(utm_own_df, ["google"])
        if not utm_daily_google.empty:
            daily_g = daily_g.merge(utm_daily_google, on="date", how="left")
            daily_g["신청수"] = daily_g["신청수"].fillna(0).astype(int)
        else:
            daily_g["신청수"] = 0

        daily_g["CTR"] = (daily_g["clicks"] / daily_g["impressions"].replace(0, float("nan")) * 100).fillna(0).round(2)
        daily_g["CPA"] = (daily_g["cost"] / daily_g["신청수"].replace(0, float("nan"))).fillna(0).round(0)

        col_c1, col_c2 = st.columns(2)
        with col_c1:
            st.subheader("일별 비용 / 신청수 / 결과")
            fig = go.Figure()
            fig.add_trace(go.Bar(x=daily_g["date"], y=daily_g["cost"],
                                  name="비용(₩)", marker_color="#636EFA"))
            fig.add_trace(go.Scatter(x=daily_g["date"], y=daily_g["신청수"],
                                      name="신청수", mode="lines+markers",
                                      yaxis="y2", line=dict(color="#EF553B")))
            fig.add_trace(go.Scatter(x=daily_g["date"], y=daily_g["conversions"],
                                      name="결과(전환)", mode="lines+markers",
                                      yaxis="y2", line=dict(color="#00CC96")))
            fig.update_layout(
                yaxis=dict(title="비용(₩)"),
                yaxis2=dict(title="건수", overlaying="y", side="right"),
                height=320, margin=dict(t=10, b=10),
                legend=dict(orientation="h", x=0.5, xanchor="center", y=1.05),
            )
            st.plotly_chart(fig, use_container_width=True)

        with col_c2:
            st.subheader("일별 광고지표 (CTR, CPA)")
            fig2 = go.Figure()
            fig2.add_trace(go.Scatter(x=daily_g["date"], y=daily_g["CTR"],
                                       name="CTR(%)", mode="lines+markers",
                                       line=dict(color="#AB63FA")))
            fig2.add_trace(go.Scatter(x=daily_g["date"], y=daily_g["CPA"],
                                       name="CPA(₩)", mode="lines+markers",
                                       yaxis="y2", line=dict(color="#FFA15A")))
            fig2.update_layout(
                yaxis=dict(title="CTR(%)"),
                yaxis2=dict(title="CPA(₩)", overlaying="y", side="right"),
                height=320, margin=dict(t=10, b=10),
                legend=dict(orientation="h", x=0.5, xanchor="center", y=1.05),
            )
            st.plotly_chart(fig2, use_container_width=True)

        if "campaign_name" in google_own_df.columns:
            col_pie_g, col_camp_g = st.columns([1, 3])
            by_camp = google_own_df.groupby("campaign_name")["cost"].sum().reset_index()
            by_camp["label"] = by_camp.apply(
                lambda r: f"{r['campaign_name']}<br>₩{r['cost']:,.0f}", axis=1)
            with col_pie_g:
                st.subheader("캠페인별 지출")
                fig_gp = px.pie(by_camp, values="cost", names="label", hole=0.4)
                fig_gp.update_layout(height=380, margin=dict(t=10, b=90, l=0, r=0),
                                     showlegend=True, legend=dict(orientation="h", x=0.5, xanchor="center", y=-0.15))
                st.plotly_chart(fig_gp, use_container_width=True)
            with col_camp_g:
                st.subheader("캠페인별 성과")
                st.dataframe(
                    _fmt_campaign_google(campaign_summary_google(google_own_df, google_utm_map)),
                    use_container_width=True,
                    hide_index=True,
                )

        st.markdown("---")
        st.subheader("상품별 성과 (캠페인 → 광고그룹 → 광고)")
        adgroup_col_g = "adgroup_name" if "adgroup_name" in google_own_df.columns else None
        _render_hierarchy(
            google_own_df, "cost", "conversions", "clicks", adgroup_col_g,
            utm_product_map=google_utm_map, source="Google", key_prefix="own_",
        )

        with st.expander("광고 상세 테이블"):
            st.dataframe(google_own_df.sort_values("date", ascending=False),
                         use_container_width=True, hide_index=True)


# ════════════════════════════════════════════════════════════════════════════
# TAB 5: 기타 광고 (Meta 기타 + Google 기타)
# ════════════════════════════════════════════════════════════════════════════

with tab_other:
    sub_other_meta, sub_other_google = st.tabs(["📘 Meta 기타", "🔍 Google 기타"])

    # ── Meta 기타 ──────────────────────────────────────────────────────────
    with sub_other_meta:
        if meta_other_df.empty:
            st.info("기타 Meta 광고 데이터가 없습니다.")
        else:
            lc_o = "link_clicks" if "link_clicks" in meta_other_df.columns else "clicks"
            meta_other_utm_map = _utm_product_map(utm_other_df, ["meta", "instagram"])

            st.subheader("캠페인별 성과")
            st.dataframe(
                _fmt_campaign_meta(campaign_summary_meta(meta_other_df, meta_other_utm_map)),
                use_container_width=True,
                hide_index=True,
            )

            st.markdown("---")
            st.subheader("상품별 성과 (캠페인 → 광고세트 → 광고)")
            adgroup_col_mo = "adset_name" if "adset_name" in meta_other_df.columns else None
            _render_hierarchy(
                meta_other_df, "spend", "결과", lc_o, adgroup_col_mo,
                utm_product_map=meta_other_utm_map, source="Meta", key_prefix="other_",
            )

            with st.expander("광고 상세 테이블"):
                show_cols_o = [c for c in ["date", "account_id", "campaign_name", "adset_name",
                                            "ad_name", "spend", "결과", "result_indicator",
                                            "impressions", "link_clicks", "cpc", "cpm",
                                            "purchases", "leads"] if c in meta_other_df.columns]
                st.dataframe(meta_other_df[show_cols_o].sort_values("date", ascending=False),
                             use_container_width=True, hide_index=True)

    # ── Google 기타 ────────────────────────────────────────────────────────
    with sub_other_google:
        if google_other_df.empty:
            st.info("기타 Google 광고 데이터가 없습니다.")
        else:
            google_other_utm_map = _utm_product_map(utm_other_df, ["google"])

            st.subheader("캠페인별 성과")
            st.dataframe(
                _fmt_campaign_google(campaign_summary_google(google_other_df, google_other_utm_map)),
                use_container_width=True,
                hide_index=True,
            )

            st.markdown("---")
            st.subheader("상품별 성과 (캠페인 → 광고그룹 → 광고)")
            adgroup_col_go = "adgroup_name" if "adgroup_name" in google_other_df.columns else None
            _render_hierarchy(
                google_other_df, "cost", "conversions", "clicks", adgroup_col_go,
                utm_product_map=google_other_utm_map, source="Google", key_prefix="other_",
            )

            with st.expander("광고 상세 테이블"):
                st.dataframe(google_other_df.sort_values("date", ascending=False),
                             use_container_width=True, hide_index=True)
