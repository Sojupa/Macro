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
                    valid_closes =
