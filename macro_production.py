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

MONTHLY_DATA = {
    "ISM_PMI": 46.8, "ISM_NEW_ORDER": 47.4, "CAIXIN_PMI": 49.8, "CAIXIN_PMI_PREV": 48.9,
    "TAIWAN_EXPORTS": 2.8, "TSMC_GUIDANCE_QOQ": 5.0, "GERMAN_PMI": 43.5, "GERMAN_IFO": 85.5,
    "VIETNAM_FDI": -12.0, "PF_DELINQUENCY": 0.45, "CHINA_TSF_YOY": 7.8, "CHINA_M1_REBOUND": False
}

# ==========================================
# 0. 핵심 위험 지표 (KRI) 엔진
# ==========================================
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

# ==========================================
# 1. 텔레그램 전송 함수
# ==========================================
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
    auto_data = default_monthly_data.copy()
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}

    def get_te_indicator(country, indicator):
        try:
            url = f"https://tradingeconomics.com/{country}/{indicator}"
            res = requests.get(url, headers=headers, timeout=5)
            if res.status_code == 200:
                soup = BeautifulSoup(res.text, 'html.parser')
                val_elem = soup.find('td', {'id': 'actual'}) or soup.find('span', {'id': 'last'})
                if val_elem:
                    return float(val_elem.text.strip().replace(',', ''))
        except Exception:
            pass
        return None

    pmi_val = get_te_indicator("united-states", "manufacturing-pmi")
    if pmi_val is not None: auto_data["ISM_PMI"] = pmi_val
    cx_val = get_te_indicator("china", "caixin-manufacturing-pmi")
    if cx_val is not None:
        auto_data["CAIXIN_PMI_PREV"] = auto_data.get("CAIXIN_PMI", 48.9)
        auto_data["CAIXIN_PMI"] = cx_val
    tw_val = get_te_indicator("taiwan", "export-orders-yoy")
    if tw_val is not None: auto_data["TAIWAN_EXPORTS"] = tw_val

    return auto_data

def get_consecutive_days_count(series: pd.Series, threshold: float, condition: str = ">=") -> int:
    if series.empty: return 0
    is_met = series >= threshold if condition == ">=" else series <= threshold
    count = 0
    for val in reversed(is_met.tolist()):
        if val: count += 1
        else: break
    return count

# ==========================================
# 2. 증시 캘린더 모듈
# ==========================================
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

# ==========================================
# 3. 매크로 데이터 수집
# ==========================================
def collect_macro_data():
    data = {}
    data["MONTHLY"] = fetch_monthly_data_auto(MONTHLY_DATA)

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
                if key == "USDKRW" and len(hist) >= 5:
                    data["USDKRW_WEEK_CHANGE"] = hist.iloc[-1] - hist.iloc[-5]
                if key == "SOX":
                    valid_closes = [x for x in hist if 0 < x < 10000]
                    if valid_closes:
                        data["SOX"] = valid_closes[-1]
                        data["SOX_20MA"] = sum(valid_closes[-20:]) / len(valid_closes[-20:])
                        data["SOX_HIGH_52W"] = max(valid_closes[-252:])
        except Exception:
            data[key] = 0.0

    if "US10Y_HIST" in data and not data["US10Y_HIST"].empty:
        data["US10Y_HIST"] = data["US10Y_HIST"].apply(lambda x: x if x < 10.0 else x / 10.0)
        data["US10Y"] = data["US10Y_HIST"].iloc[-1]

    try:
        if FRED_API_KEY:
            fred = Fred(api_key=FRED_API_KEY)
            hy_s = fred.get_series("BAMLH0A0HYM2").dropna()
            data["HY_SPREAD"] = hy_s.iloc[-1] if not hy_s.empty else 0.0
            rrp_s = fred.get_series("RRPONTSYD").dropna()
            raw_rrp = rrp_s.iloc[-1] if not rrp_s.empty else 120.0
            data["RRP_USD"] = raw_rrp if raw_rrp >= 0.1 else 120.0
        else:
            data["HY_SPREAD"], data["RRP_USD"] = 0.0, 120.0
    except Exception:
        data["HY_SPREAD"], data["RRP_USD"] = 0.0, 120.0

    try:
        if FRED_API_KEY:
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

    return data

# ==========================================
# 4. 수급 쏠림 및 레버리지 위험 평가
# ==========================================
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

    rrp = data.get("RRP_USD", 120)
    if rrp <= 100.0:
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

# ==========================================
# 5. 트리거 평가 엔진
# ==========================================
def evaluate_conditions(data):
    short_trig, long_trig, caution_list, explanations = [], [], [], []
    monthly = data.get("MONTHLY", MONTHLY_DATA)

    u10 = data.get("US10Y", 0)
    u10_hist = data.get("US10Y_HIST", pd.Series())
    u10_consec = get_consecutive_days_count(u10_hist, threshold=4.5, condition=">=")

    if u10_consec >= 3:
        short_trig.append(f"🔴 [Tier1-A] 미 10년물: {u10:.2f}% (≥4.5% 조건 {u10_consec}영업일 연속 안착 중)")
        explanations.append("💡 [미 금리 위험 구간 안착] 한미 금리차 역전 장기화 및 환차손 우려로 외국인 자금 이탈 압력 심화.")
    elif 4.3 <= u10 < 4.5:
        caution_list.append(f"⚠️ [유의-Tier1-A] 미 10년물: {u10:.2f}% (4.5% 안착 임계치 접근 중)")

    dxy = data.get("DXY", 0)
    dxy_hist = data.get("DXY_HIST", pd.Series())
    dxy_consec = get_consecutive_days_count(dxy_hist, threshold=105.0, condition=">=")

    if dxy_consec >= 2:
        short_trig.append(f"🔴 [Tier1-A] DXY 달러인덱스: {dxy:.2f}pt (≥105.0pt 조건 {dxy_consec}영업일 연속 안착 중)")
        explanations.append("💥 [강달러 지속] Risk-off 심리 강화로 신흥국 증시 전반 외국인 기계적 매도 확산.")

    usdkrw = data.get("USDKRW", 0)
    change_krw = data.get("USDKRW_WEEK_CHANGE", 0)
    krw_hist = data.get("USDKRW_HIST", pd.Series())
    krw_consec = get_consecutive_days_count(krw_hist, threshold=1400.0, condition=">=")

    if change_krw >= 30.0 and krw_consec >= 2:
        short_trig.append(f"🔴 [Tier1-A] 원/달러: {usdkrw:.1f}원 (주간 +{change_krw:.1f}원 급등 + ≥1400원 조건 {krw_consec}영업일 연속 안착)")

    sox = data.get("SOX", 0)
    sox_ma20 = data.get("SOX_20MA", 0)
    sox_high_52w = data.get("SOX_HIGH_52W", 0)
    if 0 < sox and 0 < sox_high_52w:
        drop_pct = ((sox - sox_high_52w) / sox_high_52w) * 100
        if drop_pct <= -15.0 and sox < sox_ma20:
            short_trig.append(f"🔴 [Tier1-C] SOX 반도체: {sox:.1f}pt (52주 고점대비 {drop_pct:.1f}% 폭락 + 20일선 이탈)")

    hy = data.get("HY_SPREAD", 0)
    if hy >= 5.5: short_trig.append(f"🔴 [Hidden-A1] HY 스프레드: {hy:.2f}% (≥5.5% 신용위험)")
    rrp = data.get("RRP_USD", 0)
    if 0 < rrp <= 100.0: short_trig.append(f"🔴 [Hidden-A2] 역레포 잔액: ${rrp:.1f}B (≤$1,000억 유동성 고갈)")

    pmi = monthly.get("ISM_PMI", 46.8)
    pmi_no = monthly.get("ISM_NEW_ORDER", 47.4)
    if pmi <= 47.0: short_trig.append(f"🔴 [E1] 미 ISM 제조업: {pmi} (≤47.0 침체)")
    if pmi_no <= 48.0: short_trig.append(f"🔴 [E2] 미 ISM 신규주문: {pmi_no} (≤48.0 선행 피크아웃)")

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

# ==========================================
# 6. 메인 실행 (순서 최적화 반영)
# ==========================================
def run_macro_monitor(mode="MORNING"):
    data = collect_macro_data()
    short_trig, long_trig, caution_list, explanations = evaluate_conditions(data)
    leopold_score, leopold_lvl, leopold_msg = compute_leopold_risk(data)
    macro_score, alloc_recommendation = calculate_macro_composite_score(short_trig, long_trig, leopold_score)
    kri_res = evaluate_liquidation_framework()
    calendar_msg = generate_calendar_briefing()
    
    u10 = data.get("US10Y", 0)
    u10_hist = data.get("US10Y_HIST", pd.Series())
    u10_consec = get_consecutive_days_count(u10_hist, threshold=4.5, condition=">=")

    today_str = datetime.date.today().strftime('%Y-%m-%d')
    now_str = datetime.datetime.now().strftime('%H:%M')

    title = "🌅 [글로벌 매크로 & 강제청산 조기경보]" if mode == "MORNING" else "🌆 [글로벌 매크로 & 유동성 종합 리포트]"
    msg = f"{title} ({today_str} {now_str})\n\n"
    
    # [1] 증시 캘린더 최상단 배치
    msg += calendar_msg + "\n\n"

    # [2] 핵심 매크로 지표 요약
    msg += "📌 [핵심 매크로 수집 Summary]\n"
    msg += f"• 미 10년물 금리: {u10:.2f}% ({u10_consec}영업일 연속 ≥4.5% 안착)\n"
    msg += f"• 달러인덱스(DXY): {data.get('DXY', 0):.2f}pt\n"
    msg += f"• 원/달러 환율: {data.get('USDKRW', 0):.1f}원 (주간: {data.get('USDKRW_WEEK_CHANGE', 0):+.1f}원)\n"
    msg += f"• SOX 반도체지수: {data.get('SOX', 0):.1f}pt (52주 고점: {data.get('SOX_HIGH_52W', 0):.1f}pt)\n"
    net_liq = data.get("NET_LIQUIDITY_USD_B")
    net_liq_chg = data.get("NET_LIQUIDITY_4W_CHANGE_B")
    if net_liq is not None:
        chg_str = f" (4주 변화: {net_liq_chg:+.0f}B)" if net_liq_chg is not None else ""
        msg += f"• Net Liquidity: ${net_liq:,.0f}B{chg_str}\n"
    msg += "\n"

    # [3] 파급 효과 및 영향도
    if explanations:
        msg += "💡 [파급 효과 및 영향도]\n" + "\n".join(explanations) + "\n\n"

    # [4] 주의 필요 매크로 지표
    if short_trig:
        msg += "📉 [주의 필요 매크로 지표]\n" + "\n".join(short_trig) + "\n\n"
    if long_trig:
        msg += "🚀 [발동된 Master 롱 트리거]\n" + "\n".join(long_trig) + "\n\n"
    if caution_list:
        msg += "⚠️ [주의: 임계치 접근 유의 지표]\n" + "\n".join(caution_list) + "\n\n"

    # [5] 종합 매크로 스코어 및 자산배분
    msg += f"🎯 [종합 매크로 스코어]: {macro_score}pt / 100pt\n"
    msg += f"{alloc_recommendation}\n\n"

    # [6] 포트폴리오 KRI 조기경보 엔진
    msg += f"🚨 [0. 포트폴리오 KRI 조기경보 엔진]: {kri_res['Tier']}\n"
    msg += f"👉 실행 권고: {kri_res['Actions'][0]}\n\n"
    
    msg += f"📊 [포트폴리오 KRI 세부 정량 지표]\n"
    msg += f"• 증거금 여유율(MC): {kri_res['Margin_Cushion']}%\n"
    msg += f"• 청산 감내폭(B2L): {kri_res['Buffer_to_Liq']}%\n"
    msg += f"• 청산 소요 기간(DTL): {kri_res['DTL']}일 (실질 청산가: ${kri_res['Effective_Liq_Price']})\n\n"
    
    # [7] 수급 쏠림 및 레버리지 위험 평가
    msg += leopold_msg + "\n"

    print(msg)
    send_telegram(msg)

if __name__ == "__main__":
    mode_arg = sys.argv[1] if len(sys.argv) > 1 else "MORNING"
    run_macro_monitor(mode_arg)
