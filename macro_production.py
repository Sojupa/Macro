import os
import sys
import warnings
import datetime
import requests
import re
import numpy as np
import pandas as pd
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()
os.environ["PYTHONWARNINGS"] = "ignore"
warnings.filterwarnings("ignore")

import yfinance as yf
from fredapi import Fred
import FinanceDataReader as fdr

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID")
FRED_API_KEY = os.getenv("FRED_API_KEY")

# ==========================================================
# [2026-08-04 팩트체크 수정] 임계값을 한 곳에 모아 관리.
# 값을 바꿀 때 evaluate_conditions() 안의 매직넘버를 찾아다니지
# 않아도 되도록 하기 위한 리팩터링. 값 자체의 타당성은 주기적으로
# (최소 분기 1회) 재검토할 것 — "review" 주석에 마지막 검토일 기록.
# ==========================================================
THRESHOLDS = {
    # 미 10년물: 2026-08-04 기준 실제 4.69~4.75%로 이미 임계선 위에서 등락 중.
    # (review: 2026-08-04, 근거: FRED DGS10 / 로이터 보도)
    "US10Y_HARD": 4.5,
    "US10Y_HARD_CONSEC_DAYS": 3,
    "US10Y_CAUTION_LOW": 4.3,

    # DXY: 2026-08-04 기준 실제 99.9pt, 하드 임계치(105)와는 아직 거리가 있음.
    # 105 자체는 유지하되(2022년 이후 강달러 국면의 심리적 저항선), 접근 단계를
    # 조기 포착하기 위한 CAUTION 티어를 새로 추가함 (기존엔 하드 트리거 하나뿐).
    "DXY_HARD": 105.0,
    "DXY_HARD_CONSEC_DAYS": 2,
    "DXY_CAUTION_LOW": 102.0,   # [신규] review: 2026-08-04

    # 원/달러: 2026-08-04 기준 이미 1,420~1,440원대에서 수 주째 거래 중이라
    # "1400원 이상 2영업일 연속" 조건은 상시 충족 상태 -> 사실상 무의미해짐.
    # 그래서 (a) 레벨 단독 조건은 참고용 CAUTION으로 강등하고,
    #        (b) 실제 경보는 "급등 폭"과 "구조적 상단(1450원)" 조합으로 재설계.
    # 1450원은 최근 거래 레인지(약 1420~1440) 대비 상단 이탈을 보는 [추정] 값이며
    # 백테스트로 검증된 수치가 아님 — 주기적 재검토 필요.
    "USDKRW_CAUTION_LEVEL": 1400.0,
    "USDKRW_HARD_LEVEL": 1450.0,       # [추정, review: 2026-08-04]
    "USDKRW_HARD_LEVEL_CONSEC_DAYS": 2,
    "USDKRW_WEEK_SPIKE": 30.0,

    "SOX_DRAWDOWN_HARD_PCT": -15.0,

    "HY_SPREAD_HARD": 5.5,             # 2026-08-04 실제 약 2.8%(281bp)로 여유 있음. 유지.
    "RRP_HARD_LOW": 100.0,

    # ISM 제조업 PMI: 기존 기본값(46.8 / 47.4)은 침체 국면을 가정한 값이었으나
    # 2026-07 실제 발표치는 55.6 / 56.7으로 "7개월 연속 확장, 4년래 최고"에 해당.
    # 자동수집 실패시 쓰는 폴백 기본값을 실제 최근 확인치로 갱신하고,
    # "언제 마지막으로 사람이 확인했는지"를 별도로 기록해 자동 노후화 경고를 띄움.
    "ISM_PMI_HARD": 47.0,
    "ISM_NEW_ORDER_HARD": 48.0,
    "MONTHLY_FALLBACK_STALE_DAYS": 45,  # 이 일수보다 수동 확인이 오래되면 경고 표시
}

# [2026-08-04 갱신] 폴백 기본값 — 자동 수집(TradingEconomics 스크래핑)이 실패했을 때만 사용됨.
# 반드시 "최근 실제 발표치"로만 채울 것. 침체를 가정한 임의의 숫자를 넣지 말 것
# (과거 46.8/47.4 기본값이 실제로는 정반대 국면을 가리키고 있었던 사고가 있었음).
MONTHLY_DATA = {
    "ISM_PMI": 55.6, "ISM_NEW_ORDER": 56.7, "CAIXIN_PMI": 49.8, "CAIXIN_PMI_PREV": 48.9,
    "TAIWAN_EXPORTS": 2.8, "TSMC_GUIDANCE_QOQ": 5.0, "GERMAN_PMI": 43.5, "GERMAN_IFO": 85.5,
    "VIETNAM_FDI": -12.0, "PF_DELINQUENCY": 0.45, "CHINA_TSF_YOY": 7.8, "CHINA_M1_REBOUND": False
}
MONTHLY_DATA_LAST_VERIFIED = datetime.date(2026, 8, 4)  # 사람이 수치를 마지막으로 검증한 날짜


def evaluate_liquidation_framework(
    total_assets: float = 10000000.0,
    initial_margin: float = 3000000.0,
    maint_margin: float = 1200000.0,
    current_price: float = 100.0,
    liq_price: float = 88.0,
    position_qty: float = 500000.0,
    adv: float = 1000000.0,
    daily_vol: float = 0.04,
    participation_rate: float = 0.10
):
    margin_cushion = ((initial_margin - maint_margin) / total_assets) * 100.0
    buffer_to_liq = (abs(current_price - liq_price) / current_price) * 100.0
    dtl = position_qty / (adv * participation_rate) if (adv * participation_rate) > 0 else 999.0

    tier = "1단계: 🟢 Green Tier (정상)"
    actions = []

    if margin_cushion < 15.0:
        tier = "4단계: 🔴 Red Tier (임계 - 비상 리스크 관리)"
        actions.append("비상 리스크 관리 작동 (포지션 축소 및 현금 담보 추가)")
    elif (15.0 <= margin_cushion < 30.0) or (buffer_to_liq < (2.0 * daily_vol * 100.0)):
        tier = "3단계: 🟠 Orange Tier (경고 - 선제적 디레버리징)"
        actions.append("선제적 디레버리징(De-leveraging) 실행 (포지션 일부 축소 또는 현금 담보 추가)")
    elif (30.0 <= margin_cushion <= 50.0) or (daily_vol >= 0.05):
        tier = "2단계: 🟡 Yellow Tier (주의 - 밀착 감시)"
        actions.append("신규 레버리지 진입 중단 및 담보 자산 실시간 밀착 감시 전환")
    else:
        tier = "1단계: 🟢 Green Tier (정상)"
        actions.append("기본 모니터링 주기 유지")

    slippage_factor = 0.02 if dtl > 2.0 else 0.005
    effective_liq_price = liq_price * (1.0 - slippage_factor) if current_price >= liq_price else liq_price * (1.0 + slippage_factor)

    return {
        "Margin_Cushion": round(margin_cushion, 2),
        "Buffer_to_Liq": round(buffer_to_liq, 2),
        "DTL": round(dtl, 2),
        "Effective_Liq_Price": round(effective_liq_price, 2),
        "Tier": tier,
        "Actions": actions
    }


def send_telegram(text):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("❌ TELEGRAM_TOKEN 또는 CHAT_ID가 미설정되었습니다.")
        return False

    token_str = str(TELEGRAM_TOKEN).strip()
    match_token = re.search(r'(\d+:[A-Za-z0-9_-]+)', token_str)
    clean_token = match_token.group(1) if match_token else token_str.replace('https://api.telegram.org/bot', '').replace('[', '').replace(']', '').strip()
    clean_chat_id = str(CHAT_ID).replace('[', '').replace(']', '').replace('"', '').replace("'", '').strip()

    url = f"https://api.telegram.org/bot{clean_token}/sendMessage"
    payload = {"chat_id": clean_chat_id, "text": text}

    try:
        res = requests.post(url, data=payload, timeout=10)
        return res.status_code == 200
    except Exception as e:
        print(f"텔레그램 전송 중 예외: {e}")
        return False


def fetch_monthly_data_auto(default_monthly_data):
    """
    [2026-08-04 수정] TradingEconomics 페이지 스크래핑은 로그인/유료월 처리로
    셀렉터가 조용히 비어있는 경우가 많아, 실패해도 예외 없이 None을 반환하고
    폴백 기본값이 쓰인다 — 문제는 그 사실이 최종 메시지에 전혀 드러나지 않았다는 것.
    아래는 (1) 값의 합리성 범위를 체크하고 (2) 어떤 필드가 "실시간 수집"이고
    어떤 필드가 "폴백(수동 확인치)"인지 상태를 함께 반환하도록 고친 버전.
    """
    auto_data = default_monthly_data.copy()
    status = {k: "FALLBACK" for k in default_monthly_data}
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}

    def get_te_indicator(country, indicator, sanity_range=(0.0, 100.0)):
        try:
            url = f"https://tradingeconomics.com/{country}/{indicator}"
            res = requests.get(url, headers=headers, timeout=5)
            if res.status_code == 200:
                soup = BeautifulSoup(res.text, 'html.parser')
                val_elem = soup.find('td', {'id': 'actual'}) or soup.find('span', {'id': 'last'})
                if val_elem:
                    val = float(val_elem.text.strip().replace(',', ''))
                    lo, hi = sanity_range
                    if lo <= val <= hi:
                        return val
                    print(f"[fetch_monthly_data_auto] {country}/{indicator} 값 {val} 이 합리적 범위({lo}~{hi}) 밖 — 폴백 유지")
        except Exception as e:
            print(f"[fetch_monthly_data_auto] {country}/{indicator} 수집 실패: {e}")
        return None

    pmi_val = get_te_indicator("united-states", "manufacturing-pmi")
    if pmi_val is not None:
        auto_data["ISM_PMI"] = pmi_val
        status["ISM_PMI"] = "LIVE"

    cx_val = get_te_indicator("china", "caixin-manufacturing-pmi")
    if cx_val is not None:
        auto_data["CAIXIN_PMI_PREV"] = auto_data.get("CAIXIN_PMI", 48.9)
        auto_data["CAIXIN_PMI"] = cx_val
        status["CAIXIN_PMI"] = "LIVE"

    tw_val = get_te_indicator("taiwan", "export-orders-yoy", sanity_range=(-100.0, 100.0))
    if tw_val is not None:
        auto_data["TAIWAN_EXPORTS"] = tw_val
        status["TAIWAN_EXPORTS"] = "LIVE"

    # ISM_NEW_ORDER는 원래 코드에서도 자동수집 대상이 아니었음 — 계속 폴백 사용.
    # 폴백을 쓰는 항목이 있다면, 그 폴백이 사람이 마지막으로 확인한 날로부터
    # 며칠이 지났는지 함께 경고해 신호의 신뢰도를 스스로 드러내게 함.
    stale_days = (datetime.date.today() - MONTHLY_DATA_LAST_VERIFIED).days
    is_stale = stale_days > THRESHOLDS["MONTHLY_FALLBACK_STALE_DAYS"]

    return auto_data, status, is_stale, stale_days


def get_consecutive_days_count(series: pd.Series, threshold: float, condition: str = ">=") -> int:
    if series.empty: return 0
    is_met = series >= threshold if condition == ">=" else series <= threshold
    count = 0
    for val in reversed(is_met.tolist()):
        if val: count += 1
        else: break
    return count


def fetch_market_calendar_events(date_obj):
    date_param = date_obj.strftime("%Y%m%d")
    events = []
    try:
        schedule_url = f"https://finance.naver.com/news/market_cal.naver?target_date={date_param}"
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        res = requests.get(schedule_url, headers=headers, timeout=5)

        if res.status_code == 200:
            soup = BeautifulSoup(res.text, 'html.parser')
            rows = soup.select("table.type_5 tr")
            for row in rows:
                cols = row.find_all("td")
                if len(cols) >= 2:
                    title = cols[0].get_text(strip=True)
                    desc = cols[1].get_text(strip=True)
                    if title:
                        events.append(f"{title} ({desc})" if desc else title)
    except Exception:
        pass
    return events


def generate_calendar_briefing():
    today = datetime.datetime.now()
    tomorrow = today + datetime.timedelta(days=1)

    today_str = today.strftime("%Y-%m-%d")
    tomorrow_str = tomorrow.strftime("%Y-%m-%d")

    today_evs = fetch_market_calendar_events(today)
    tomorrow_evs = fetch_market_calendar_events(tomorrow)

    msg = "📅 [오늘 & 내일 증시 캘린더]\n"
    msg += f"• 오늘({today_str}): "
    if today_evs:
        msg += ", ".join(today_evs[:3]) + "\n"
    else:
        msg += "특이 일정 없음 (또는 휴장일)\n"

    msg += f"• 내일({tomorrow_str}): "
    if tomorrow_evs:
        msg += ", ".join(tomorrow_evs[:3]) + "\n"
    else:
        msg += "특이 일정 없음\n"

    return msg


def collect_macro_data():
    data = {}
    data_status = {}  # [신규] 각 필드가 "LIVE" / "FALLBACK" / "MISSING" 인지 추적

    monthly, monthly_status, monthly_stale, stale_days = fetch_monthly_data_auto(MONTHLY_DATA)
    data["MONTHLY"] = monthly
    data_status["MONTHLY"] = monthly_status
    data_status["MONTHLY_STALE"] = monthly_stale
    data_status["MONTHLY_STALE_DAYS"] = stale_days

    yf_symbols = {
        "US10Y": "^TNX", "DXY": "DX-Y.NYB", "USDKRW": "KRW=X",
        "WTI": "CL=F", "USDJPY": "JPY=X", "USDCNY": "CNY=X",
        "SOX": "^SOX", "KOSPI200": "^KS200"
    }

    for key, symbol in yf_symbols.items():
        try:
            hist = yf.Ticker(symbol).history(period="1y")["Close"].dropna()
            if not hist.empty:
                data[f"{key}_HIST"] = hist
                data[key] = hist.iloc[-1]
                data_status[key] = "LIVE"
                if key == "USDKRW" and len(hist) >= 5:
                    data["USDKRW_WEEK_CHANGE"] = hist.iloc[-1] - hist.iloc[-5]
                if key == "SOX":
                    valid_closes = [x for x in hist if 0 < x < 10000]
                    if valid_closes:
                        data["SOX"] = valid_closes[-1]
                        data["SOX_20MA"] = sum(valid_closes[-20:]) / len(valid_closes[-20:])
                        data["SOX_HIGH_52W"] = max(valid_closes[-252:])
            else:
                data_status[key] = "MISSING"
        except Exception:
            data[key] = 0.0
            data_status[key] = "MISSING"

    # [2026-08-04 보강] ^TNX(야후 파이낸스)가 비어있을 때 FRED(DGS10)로 대체 수집.
    # (구 supply_sniper.py 패치 스니펫에 있던 로직을 본체로 정식 흡수함 — 자세한
    #  배경은 첨부 문서의 "버그 목록 3" 참고.)
    if data.get("US10Y", 0.0) <= 0.0 and FRED_API_KEY:
        try:
            fred = Fred(api_key=FRED_API_KEY)
            u10_fred = fred.get_series("DGS10").dropna()
            if not u10_fred.empty:
                data["US10Y"] = float(u10_fred.iloc[-1])
                data["US10Y_HIST"] = u10_fred.tail(252)
                data_status["US10Y"] = "LIVE(FRED 대체)"
        except Exception as e:
            print(f"[collect_macro_data] FRED 미 10년물 대체 수집 실패: {e}")

    if "US10Y_HIST" in data and not data["US10Y_HIST"].empty:
        data["US10Y_HIST"] = data["US10Y_HIST"].apply(lambda x: x if x < 10.0 else x / 10.0)
        data["US10Y"] = data["US10Y_HIST"].iloc[-1]

    # [2026-08-04 수정] HY_SPREAD / RRP_USD의 "안전해 보이는" 가짜 폴백값(0.0, 120.0)을
    # 제거. 리스크 트리거에 쓰이는 값을 조용히 지어내면, 실제로는 데이터가 없어서
    # "정상"으로 보이는 건지 진짜 정상인지 구분할 수 없다. 이제 실패시 None으로 두고
    # evaluate_conditions / compute_leopold_risk에서 "데이터 미확보"로 명시 처리한다.
    try:
        if FRED_API_KEY:
            fred = Fred(api_key=FRED_API_KEY)
            hy_s = fred.get_series("BAMLH0A0HYM2").dropna()
            data["HY_SPREAD"] = float(hy_s.iloc[-1]) if not hy_s.empty else None
            data_status["HY_SPREAD"] = "LIVE" if not hy_s.empty else "MISSING"

            rrp_s = fred.get_series("RRPONTSYD").dropna()
            data["RRP_USD"] = float(rrp_s.iloc[-1]) if not rrp_s.empty else None
            data_status["RRP_USD"] = "LIVE" if not rrp_s.empty else "MISSING"
        else:
            data["HY_SPREAD"], data["RRP_USD"] = None, None
            data_status["HY_SPREAD"] = data_status["RRP_USD"] = "MISSING(NO_API_KEY)"
    except Exception as e:
        print(f"[collect_macro_data] HY/RRP 수집 실패: {e}")
        data["HY_SPREAD"], data["RRP_USD"] = None, None
        data_status["HY_SPREAD"] = data_status["RRP_USD"] = "MISSING"

    try:
        if FRED_API_KEY and data.get("RRP_USD") is not None:
            walcl_s = fred.get_series("WALCL").dropna()
            wtregen_s = fred.get_series("WTREGEN").dropna()
            if not walcl_s.empty and not wtregen_s.empty:
                walcl_now = walcl_s.iloc[-1] / 1000.0
                wtregen_now = wtregen_s.iloc[-1] / 1000.0
                net_liq_now = walcl_now - wtregen_now - data["RRP_USD"]
                data["NET_LIQUIDITY_USD_B"] = round(net_liq_now, 1)

                cutoff = walcl_s.index[-1] - pd.Timedelta(days=28)
                walcl_prev = walcl_s[walcl_s.index <= cutoff]
                wtregen_prev = wtregen_s[wtregen_s.index <= cutoff]
                rrp_prev_s = fred.get_series("RRPONTSYD").dropna()
                rrp_prev_series = rrp_prev_s[rrp_prev_s.index <= cutoff]
                if not walcl_prev.empty and not wtregen_prev.empty and not rrp_prev_series.empty:
                    net_liq_prev = (
                        walcl_prev.iloc[-1] / 1000.0
                        - wtregen_prev.iloc[-1] / 1000.0
                        - rrp_prev_series.iloc[-1]
                    )
                    data["NET_LIQUIDITY_4W_CHANGE_B"] = round(net_liq_now - net_liq_prev, 1)
    except Exception:
        pass

    data["_DATA_STATUS"] = data_status
    return data


def compute_leopold_risk(data):
    signals = []
    score = 0

    try:
        sox_hist = yf.Ticker("^SOX").history(period="3mo")["Close"]
        qqq_hist = yf.Ticker("QQQ").history(period="3mo")["Close"]
        sox_curr = data.get("SOX", 0)
        sox_ma20 = data.get("SOX_20MA", 0)

        if len(sox_hist) > 20 and len(qqq_hist) > 20:
            corr = sox_hist.pct_change().rolling(20).corr(qqq_hist.pct_change()).iloc[-1]
            if corr >= 0.90:
                if sox_curr < sox_ma20:
                    signals.append(f"🔴 [포지션 쏠림] SOX-QQQ 상관계수 {corr:.2f} + SOX 20일선 하회 (단기 조정 유의)")
                    score += 25
                else:
                    signals.append(f"🟡 [포지션 쏠림] SOX-QQQ 상관계수 {corr:.2f} (대세 상승장 수급 쏠림 경계)")
                    score += 10
    except Exception:
        pass

    try:
        overheated = []
        for sym in ["SOXL", "NVDL", "TQQQ"]:
            h = yf.Ticker(sym).history(period="5d")
            if not h.empty:
                vol_usd = (h["Close"] * h["Volume"]).iloc[-1] / 1e9
                if vol_usd >= 3.0:
                    overheated.append(f"{sym}(${vol_usd:.1f}B)")
        if overheated:
            add_score = min(20, len(overheated) * 10 + 5)
            signals.append(f"⚠️ [레버리지 ETF] {', '.join(overheated)} 거래대금 과열 (변동성 확대 유의)")
            score += add_score
    except Exception:
        pass

    # [2026-08-04 수정] RRP_USD가 None(데이터 미확보)이면 "정상"으로 잘못 해석되지
    # 않도록 평가 자체를 건너뛰고 신호에 명시적으로 표기.
    rrp = data.get("RRP_USD")
    if rrp is None:
        signals.append("⚪ [단기 유동성] 역레포 잔액 데이터 미확보 — 이 항목은 평가에서 제외됨 (FRED_API_KEY 확인 필요)")
    elif rrp <= THRESHOLDS["RRP_HARD_LOW"]:
        signals.append(f"🔴 [단기 유동성] 역레포 잔액 ${rrp:.1f}B (유동성 임계 구간 진입)")
        score += 25
    elif rrp <= 150.0:
        signals.append(f"⚠️ [단기 유동성] 역레포 잔액 ${rrp:.1f}B (유동성 소진 유의)")
        score += 10

    try:
        sox = yf.Ticker("^SOX").history(period="2mo")
        if len(sox) > 20:
            ret = sox["Close"].pct_change().dropna()
            big_down = (ret.tail(5) <= -0.03).sum()
            if big_down >= 2:
                signals.append(f"🔴 [지수 변동성] SOX 지수 최근 5일 중 -3% 이상 조정 {big_down}회 발생")
                score += 15
    except Exception:
        pass

    score = min(100, score)
    level = "🟢 정상"
    if score >= 70:
        level = "🚨 리스크 높음 - 레버리지 포지션 관리 및 디레버리징 권고"
    elif score >= 45:
        level = "⚠️ 주의 필요 - 고레버리지 자산 비중 축소 구간"
    elif score >= 25:
        level = "🟡 관망 - 수급 쏠림 모니터링"

    msg = f"📊 [수급 쏠림 및 레버리지 과열 진단]: {score}pt / 100pt ({level})\n"
    if signals:
        msg += "\n".join(signals) + "\n"
    else:
        msg += "특이사항 없음 (수급 쏠림 및 레버리지 리스크 안정적)\n"

    return score, level, msg


def evaluate_conditions(data):
    short_trig, long_trig, caution_list, explanations = [], [], [], []
    monthly = data.get("MONTHLY", MONTHLY_DATA)
    monthly_status = data.get("_DATA_STATUS", {}).get("MONTHLY", {})
    monthly_stale = data.get("_DATA_STATUS", {}).get("MONTHLY_STALE", False)
    stale_days = data.get("_DATA_STATUS", {}).get("MONTHLY_STALE_DAYS", 0)

    u10 = data.get("US10Y", 0)
    u10_hist = data.get("US10Y_HIST", pd.Series())
    u10_consec = get_consecutive_days_count(u10_hist, threshold=THRESHOLDS["US10Y_HARD"], condition=">=")

    if u10_consec >= THRESHOLDS["US10Y_HARD_CONSEC_DAYS"]:
        short_trig.append(f"🔴 [Tier1-A] 미 10년물: {u10:.2f}% (≥{THRESHOLDS['US10Y_HARD']}% 조건 {u10_consec}영업일 연속 안착 중)")
        explanations.append("💡 [미 금리 위험 구간 안착] 한미 금리차 역전 장기화 및 환차손 우려로 외국인 자금 이탈 압력 심화.")
    elif THRESHOLDS["US10Y_CAUTION_LOW"] <= u10 < THRESHOLDS["US10Y_HARD"]:
        caution_list.append(f"⚠️ [유의-Tier1-A] 미 10년물: {u10:.2f}% ({THRESHOLDS['US10Y_HARD']}% 안착 임계치 접근 중)")

    dxy = data.get("DXY", 0)
    dxy_hist = data.get("DXY_HIST", pd.Series())
    dxy_consec = get_consecutive_days_count(dxy_hist, threshold=THRESHOLDS["DXY_HARD"], condition=">=")

    if dxy_consec >= THRESHOLDS["DXY_HARD_CONSEC_DAYS"]:
        short_trig.append(f"🔴 [Tier1-A] DXY 달러인덱스: {dxy:.2f}pt (≥{THRESHOLDS['DXY_HARD']}pt 조건 {dxy_consec}영업일 연속 안착 중)")
        explanations.append("💥 [강달러 지속] Risk-off 심리 강화로 신흥국 증시 전반 외국인 기계적 매도 확산.")
    elif THRESHOLDS["DXY_CAUTION_LOW"] <= dxy < THRESHOLDS["DXY_HARD"]:
        # [신규] 기존엔 하드 트리거(105) 하나뿐이라 접근 국면을 놓쳤음. 조기 경보 티어 추가.
        caution_list.append(f"⚠️ [유의-Tier1-A] DXY 달러인덱스: {dxy:.2f}pt ({THRESHOLDS['DXY_HARD']}pt 안착 임계치 접근 중)")

    # [2026-08-04 재설계] 원/달러 트리거 로직
    # 기존: "1400원 이상 2영업일 연속" + "주간 +30원" 동시 충족 → 하드 트리거.
    # 문제: 환율이 이미 1420~1440원대에서 상시 거래되고 있어 "1400원 이상"
    #      조건이 사실상 항상 참이 되어 게이트 역할을 못 함.
    # 수정: 레벨 단독 안착은 참고용 CAUTION으로 분리하고, 하드 트리거는
    #      "구조적 상단(1450원) 안착" 또는 "주간 급등(+30원)" 중 하나로 재정의.
    usdkrw = data.get("USDKRW", 0)
    change_krw = data.get("USDKRW_WEEK_CHANGE", 0)
    krw_hist = data.get("USDKRW_HIST", pd.Series())
    krw_caution_consec = get_consecutive_days_count(krw_hist, threshold=THRESHOLDS["USDKRW_CAUTION_LEVEL"], condition=">=")
    krw_hard_consec = get_consecutive_days_count(krw_hist, threshold=THRESHOLDS["USDKRW_HARD_LEVEL"], condition=">=")

    if krw_hard_consec >= THRESHOLDS["USDKRW_HARD_LEVEL_CONSEC_DAYS"]:
        short_trig.append(f"🔴 [Tier1-A] 원/달러: {usdkrw:.1f}원 (≥{THRESHOLDS['USDKRW_HARD_LEVEL']:.0f}원 구조적 상단 {krw_hard_consec}영업일 연속 안착)")
    elif change_krw >= THRESHOLDS["USDKRW_WEEK_SPIKE"]:
        short_trig.append(f"🔴 [Tier1-A] 원/달러: {usdkrw:.1f}원 (주간 +{change_krw:.1f}원 급등)")
    elif krw_caution_consec >= 2:
        caution_list.append(f"⚠️ [참고] 원/달러: {usdkrw:.1f}원 (심리적 저항선 {THRESHOLDS['USDKRW_CAUTION_LEVEL']:.0f}원 위 {krw_caution_consec}영업일 연속, 단독으로는 경보 아님)")

    sox = data.get("SOX", 0)
    sox_ma20 = data.get("SOX_20MA", 0)
    sox_high_52w = data.get("SOX_HIGH_52W", 0)
    if 0 < sox and 0 < sox_high_52w:
        drop_pct = ((sox - sox_high_52w) / sox_high_52w) * 100
        if drop_pct <= THRESHOLDS["SOX_DRAWDOWN_HARD_PCT"] and sox < sox_ma20:
            short_trig.append(f"🔴 [Tier1-C] SOX 반도체: {sox:.1f}pt (52주 고점대비 {drop_pct:.1f}% 폭락 + 20일선 이탈)")

    # [2026-08-04 수정] HY_SPREAD가 None(데이터 미확보)이면 "0.0 = 안전"으로
    # 잘못 읽히지 않도록 명시적으로 분리.
    hy = data.get("HY_SPREAD")
    if hy is None:
        caution_list.append("⚪ [데이터 미확보] HY 스프레드 — FRED_API_KEY 확인 필요, 이 항목 평가 제외")
    elif hy >= THRESHOLDS["HY_SPREAD_HARD"]:
        short_trig.append(f"🔴 [Hidden-A1] HY 스프레드: {hy:.2f}% (≥{THRESHOLDS['HY_SPREAD_HARD']}% 신용위험)")

    rrp = data.get("RRP_USD")
    if rrp is None:
        caution_list.append("⚪ [데이터 미확보] 역레포 잔액(RRP) — FRED_API_KEY 확인 필요, 이 항목 평가 제외")
    elif 0 < rrp <= THRESHOLDS["RRP_HARD_LOW"]:
        short_trig.append(f"🔴 [Hidden-A2] 역레포 잔액: ${rrp:.1f}B (≤${THRESHOLDS['RRP_HARD_LOW']:.0f}억 유동성 고갈)")

    # [2026-08-04 수정] ISM 트리거 — 폴백(수동 확인치)을 쓰고 있을 때는
    # 라벨에 그 사실을 명시해 "실시간 침체 신호"와 구분되게 함.
    pmi = monthly.get("ISM_PMI", MONTHLY_DATA["ISM_PMI"])
    pmi_no = monthly.get("ISM_NEW_ORDER", MONTHLY_DATA["ISM_NEW_ORDER"])
    pmi_src_tag = "" if monthly_status.get("ISM_PMI") == "LIVE" else " (⚠️ 자동수집 실패, 수동확인 폴백값 사용 중"
    pmi_src_tag += f", {stale_days}일 전 확인)" if pmi_src_tag and monthly_stale else (")" if pmi_src_tag else "")

    if pmi <= THRESHOLDS["ISM_PMI_HARD"]:
        short_trig.append(f"🔴 [E1] 미 ISM 제조업: {pmi}{pmi_src_tag} (≤{THRESHOLDS['ISM_PMI_HARD']} 침체)")
    if pmi_no <= THRESHOLDS["ISM_NEW_ORDER_HARD"]:
        short_trig.append(f"🔴 [E2] 미 ISM 신규주문: {pmi_no}{pmi_src_tag} (≤{THRESHOLDS['ISM_NEW_ORDER_HARD']} 선행 피크아웃)")

    net_liq = data.get("NET_LIQUIDITY_USD_B")
    net_liq_chg = data.get("NET_LIQUIDITY_4W_CHANGE_B")
    if net_liq is not None and net_liq_chg is not None:
        if net_liq_chg <= -100.0:
            short_trig.append(f"🔴 [Hidden-A3] Net Liquidity: ${net_liq:,.0f}B (4주간 {net_liq_chg:+.0f}B 급감 — 유동성 축소)")
            explanations.append("💥 [유동성 축소] 연준 B/S 감소 + TGA 재충전 + RRP 소진 겹치며 시중 실질 유동성이 마르는 국면.")
        elif net_liq_chg >= 100.0:
            long_trig.append(f"🟢 [Hidden-A3] Net Liquidity: ${net_liq:,.0f}B (4주간 {net_liq_chg:+.0f}B 급증 — 유동성 확장)")

    return short_trig, long_trig, caution_list, explanations


def calculate_macro_composite_score(short_trig, long_trig, leopold_score):
    base_score = (len(long_trig) * 20) - (len(short_trig) * 15) - (leopold_score * 0.4)
    macro_score = max(-100, min(100, int(base_score)))

    if macro_score <= -40:
        alloc = "🛡️ [자산배분 : 보수적 운용 (비중 축소)] 주식(KOSPI) 10% | 달러/인버스 50% | 현금 40%"
    elif macro_score >= 40:
        alloc = "🚀 [자산배분 : 적극적 운용 (비중 확대)] KOSPI200/반도체 ETF 80% | 현금 20%"
    else:
        alloc = "⚖️ [자산배분 : 중립/관망] 주식(KOSPI) 40% | 달러 20% | 현금 40%"

    return macro_score, alloc


def run_macro_monitor(mode="MORNING"):
    data = collect_macro_data()
    short_trig, long_trig, caution_list, explanations = evaluate_conditions(data)
    leopold_score, leopold_lvl, leopold_msg = compute_leopold_risk(data)
    macro_score, alloc_recommendation = calculate_macro_composite_score(short_trig, long_trig, leopold_score)
    kri_res = evaluate_liquidation_framework()
    calendar_msg = generate_calendar_briefing()

    u10 = data.get("US10Y", 0)
    u10_hist = data.get("US10Y_HIST", pd.Series())
    u10_consec = get_consecutive_days_count(u10_hist, threshold=THRESHOLDS["US10Y_HARD"], condition=">=")

    today_str = datetime.date.today().strftime('%Y-%m-%d')
    now_str = datetime.datetime.now().strftime('%H:%M')

    title = "🌅 [글로벌 매크로 & 강제청산 조기경보]" if mode == "MORNING" else "🌆 [글로벌 매크로 & 유동성 종합 리포트]"
    msg = f"{title} ({today_str} {now_str})\n\n"

    # 1. 증시 캘린더 최상단
    msg += calendar_msg + "\n\n"

    # 2. 핵심 매크로 지표 요약
    msg += "📌 [핵심 매크로 수집 Summary]\n"
    msg += f"• 미 10년물 금리: {u10:.2f}% ({u10_consec}영업일 연속 ≥{THRESHOLDS['US10Y_HARD']}% 안착)\n"
    msg += f"• 달러인덱스(DXY): {data.get('DXY', 0):.2f}pt\n"
    msg += f"• 원/달러 환율: {data.get('USDKRW', 0):.1f}원 (주간: {data.get('USDKRW_WEEK_CHANGE', 0):+.1f}원)\n"
    msg += f"• SOX 반도체지수: {data.get('SOX', 0):.1f}pt (52주 고점: {data.get('SOX_HIGH_52W', 0):.1f}pt)\n"
    net_liq = data.get("NET_LIQUIDITY_USD_B")
    net_liq_chg = data.get("NET_LIQUIDITY_4W_CHANGE_B")
    if net_liq is not None:
        chg_str = f" (4주 변화: {net_liq_chg:+.0f}B)" if net_liq_chg is not None else ""
        msg += f"• Net Liquidity: ${net_liq:,.0f}B{chg_str}\n"
    msg += "\n"

    # 3. 파급 효과 및 영향도
    if explanations:
        msg += "💡 [파급 효과 및 영향도]\n" + "\n".join(explanations) + "\n\n"

    # 4. 주의 필요 매크로 지표
    if short_trig:
        msg += "📉 [주의 필요 매크로 지표]\n" + "\n".join(short_trig) + "\n\n"
    if long_trig:
        msg += "🚀 [발동된 Master 롱 트리거]\n" + "\n".join(long_trig) + "\n\n"
    if caution_list:
        msg += "⚠️ [주의: 임계치 접근/참고 지표]\n" + "\n".join(caution_list) + "\n\n"

    # 5. 종합 매크로 스코어 및 자산배분
    msg += f"🎯 [종합 매크로 스코어]: {macro_score}pt / 100pt\n"
    msg += f"{alloc_recommendation}\n\n"

    # 6. 포트폴리오 KRI 조기경보 엔진
    msg += f"🚨 [0. 포트폴리오 KRI 조기경보 엔진]: {kri_res['Tier']}\n"
    msg += f"👉 실행 권고: {kri_res['Actions'][0]}\n\n"

    msg += f"📊 [포트폴리오 KRI 세부 정량 지표]\n"
    msg += f"• 증거금 여유율(MC): {kri_res['Margin_Cushion']}%\n"
    msg += f"• 청산 감내폭(B2L): {kri_res['Buffer_to_Liq']}%\n"
    msg += f"• 청산 소요 기간(DTL): {kri_res['DTL']}일 (실질 청산가: ${kri_res['Effective_Liq_Price']})\n\n"

    # 7. 수급 쏠림 및 레버리지 위험 평가
    msg += leopold_msg + "\n"

    # 8. [신규] 데이터 신뢰도 각주 — 어떤 값이 실시간이고 어떤 값이 폴백/미확보인지 항상 표기.
    status = data.get("_DATA_STATUS", {})
    fallback_fields = [k for k, v in status.items() if isinstance(v, str) and "FALLBACK" in v or v == "MISSING"]
    monthly_status = status.get("MONTHLY", {})
    monthly_fallback = [k for k, v in monthly_status.items() if v == "FALLBACK"]
    if monthly_fallback or fallback_fields:
        msg += "🧾 [데이터 신뢰도 각주]\n"
        if monthly_fallback:
            stale_note = f" (수동 확인 {status.get('MONTHLY_STALE_DAYS', 0)}일 경과)" if status.get("MONTHLY_STALE") else ""
            msg += f"• 월간 지표 폴백 사용: {', '.join(monthly_fallback)}{stale_note}\n"
        if fallback_fields:
            msg += f"• 실시간 미확보/폴백 필드: {', '.join(fallback_fields)}\n"

    print(msg)
    send_telegram(msg)


if __name__ == "__main__":
    mode_arg = sys.argv[1] if len(sys.argv) > 1 else "MORNING"
    run_macro_monitor(mode_arg)
