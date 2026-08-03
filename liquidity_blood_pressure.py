# ==========================================================
# liquidity_blood_pressure.py - 유동성 혈압계
# - SOFR - IORB 스프레드: 레포시장 경색 조기 감지 (2-시그마 돌파 알람)
# - Primary Credit(재할인창구) 사용량: 은행 비상 자금조달 게이지
#   ※ BTFP(비상대출기구)는 2024.03 신규대출 중단, FRED에서 DISCONTINUED 처리됨.
#     따라서 살아있는 대체 지표인 Primary Credit(WLCFLPCL)을 사용.
# ==========================================================
import os
import datetime
import requests
import numpy as np
import pandas as pd
from dotenv import load_dotenv
from fredapi import Fred

load_dotenv()
FRED_API_KEY = os.getenv("FRED_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

SOFR_IORB_ZSCORE_WINDOW = 60  # 최근 60영업일 평균/표준편차 기준 2-시그마 판정


def send_telegram(text):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        res = requests.post(url, data={"chat_id": CHAT_ID, "text": text}, timeout=10)
        return res.status_code == 200
    except Exception:
        return False


def fetch_liquidity_pressure_data():
    data = {}
    if not FRED_API_KEY:
        return data
    try:
        fred = Fred(api_key=FRED_API_KEY)

        sofr = fred.get_series("SOFR").dropna()
        iorb = fred.get_series("IORB").dropna()
        if not sofr.empty and not iorb.empty:
            spread = (sofr - iorb).dropna()
            if not spread.empty:
                data["SOFR_IORB_SPREAD"] = round(float(spread.iloc[-1]) * 100, 2)  # bp 단위로 보기 쉽게 *100
                recent = spread.tail(SOFR_IORB_ZSCORE_WINDOW)
                if len(recent) >= 20:
                    mean, std = recent.mean(), recent.std()
                    if std > 0:
                        z = (spread.iloc[-1] - mean) / std
                        data["SOFR_IORB_ZSCORE"] = round(float(z), 2)

        pc = fred.get_series("WLCFLPCL").dropna()  # Primary Credit, Millions USD
        if not pc.empty:
            data["PRIMARY_CREDIT_USD_B"] = round(float(pc.iloc[-1]) / 1000.0, 2)
            if len(pc) >= 2:
                data["PRIMARY_CREDIT_WOW_CHANGE_B"] = round((pc.iloc[-1] - pc.iloc[-2]) / 1000.0, 2)
    except Exception as e:
        print(f"[liquidity_blood_pressure] 데이터 수집 실패: {e}")
    return data


def evaluate_liquidity_pressure(data):
    alerts = []

    z = data.get("SOFR_IORB_ZSCORE")
    spread = data.get("SOFR_IORB_SPREAD")
    if z is not None and spread is not None:
        if abs(z) >= 2.0:
            alerts.append(
                f"🚨 [레포 시장 경색] SOFR-IORB 스프레드 {spread:+.1f}bp (최근 {SOFR_IORB_ZSCORE_WINDOW}일 평균 대비 {z:+.1f}σ 돌파)"
            )
        elif abs(z) >= 1.5:
            alerts.append(f"⚠️ [유의] SOFR-IORB 스프레드 {spread:+.1f}bp ({z:+.1f}σ, 경계 구간 근접)")

    pc = data.get("PRIMARY_CREDIT_USD_B")
    pc_chg = data.get("PRIMARY_CREDIT_WOW_CHANGE_B")
    if pc is not None and pc_chg is not None:
        # [추정] 임계치는 2023 SVB 사태 당시 규모($150B) 대비 잠정 설정, 백테스트 미검증
        if pc_chg >= 10.0:
            alerts.append(f"🚨 [은행 비상창구] Primary Credit 잔액 ${pc:,.1f}B (전주 대비 {pc_chg:+.1f}B 급증 — 은행권 자금 스트레스 신호)")

    return alerts


def run_liquidity_blood_pressure():
    data = fetch_liquidity_pressure_data()
    alerts = evaluate_liquidity_pressure(data)

    today_str = datetime.date.today().strftime("%Y-%m-%d")
    msg = f"🩺 [유동성 혈압계 — {today_str}]\n\n"
    if "SOFR_IORB_SPREAD" in data:
        z_str = f" ({data['SOFR_IORB_ZSCORE']:+.1f}σ)" if "SOFR_IORB_ZSCORE" in data else ""
        msg += f"• SOFR-IORB 스프레드: {data['SOFR_IORB_SPREAD']:+.1f}bp{z_str}\n"
    if "PRIMARY_CREDIT_USD_B" in data:
        chg = data.get("PRIMARY_CREDIT_WOW_CHANGE_B")
        chg_str = f" (전주 대비 {chg:+.1f}B)" if chg is not None else ""
        msg += f"• Primary Credit(재할인창구) 잔액: ${data['PRIMARY_CREDIT_USD_B']:,.1f}B{chg_str}\n"
    msg += "\n"

    if alerts:
        msg += "\n".join(alerts) + "\n"
    else:
        msg += "특이사항 없음 (레포시장/은행 유동성 정상)\n"

    print(msg)
    if send_telegram(msg):
        print("텔레그램 전송 성공!")
    return msg


if __name__ == "__main__":
    run_liquidity_blood_pressure()
