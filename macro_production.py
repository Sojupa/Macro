# ==========================================================
# credit_stress_detector.py - 신용 경색 감지기 (순환 참조 수정판)
# - HYG/LQD 비율: 하이일드 대 투자등급 상대강도
# - MOVE 지수: 채권시장 변동성(채권판 VIX)
# ==========================================================
import os
import datetime
import requests
import pandas as pd
from dotenv import load_dotenv
import yfinance as yf

load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID")

MOVE_ZSCORE_WINDOW = 60
RATIO_LOOKBACK_DAYS = 20

def send_telegram(text):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        res = requests.post(url, data={"chat_id": CHAT_ID, "text": text}, timeout=10)
        return res.status_code == 200
    except Exception:
        return False

def fetch_credit_stress_data():
    data = {}
    try:
        hyg = yf.Ticker("HYG").history(period="3mo")["Close"].dropna()
        lqd = yf.Ticker("LQD").history(period="3mo")["Close"].dropna()
        if not hyg.empty and not lqd.empty:
            ratio = (hyg / lqd).dropna()
            if len(ratio) >= RATIO_LOOKBACK_DAYS:
                data["HYG_LQD_RATIO"] = round(float(ratio.iloc[-1]), 4)
                past = ratio.iloc[-RATIO_LOOKBACK_DAYS]
                data["HYG_LQD_RATIO_20D_CHANGE_PCT"] = round((ratio.iloc[-1] - past) / past * 100, 2)
    except Exception as e:
        print(f"[credit_stress_detector] HYG/LQD 수집 실패: {e}")

    try:
        move = yf.Ticker("^MOVE").history(period="6mo")["Close"].dropna()
        if not move.empty:
            data["MOVE_INDEX"] = round(float(move.iloc[-1]), 1)
            recent = move.tail(MOVE_ZSCORE_WINDOW)
            if len(recent) >= 20:
                mean, std = recent.mean(), recent.std()
                if std > 0:
                    data["MOVE_ZSCORE"] = round(float((move.iloc[-1] - mean) / std), 2)
    except Exception as e:
        print(f"[credit_stress_detector] MOVE 지수 수집 실패: {e}")

    return data

def evaluate_credit_stress(data):
    alerts = []
    ratio_chg = data.get("HYG_LQD_RATIO_20D_CHANGE_PCT")
    if ratio_chg is not None and ratio_chg <= -3.0:
        alerts.append(f"🔴 [신용 경색] HYG/LQD 비율 20일간 {ratio_chg:+.1f}% (하이일드 상대적 열위 — 신용 위험 증가)")

    move_z = data.get("MOVE_ZSCORE")
    move_val = data.get("MOVE_INDEX")
    if move_z is not None and move_val is not None and move_z >= 2.0:
        alerts.append(f"🚨 [채권 변동성] MOVE 지수 {move_val:.1f} ({MOVE_ZSCORE_WINDOW}일 평균 대비 {move_z:+.1f}σ 돌파 — 금리 발작 위험)")

    return alerts

def run_credit_stress_detector():
    data = fetch_credit_stress_data()
    alerts = evaluate_credit_stress(data)

    today_str = datetime.date.today().strftime("%Y-%m-%d")
    msg = f"🏥 [신용 경색 감지기 — {today_str}]\n\n"
    if "HYG_LQD_RATIO" in data:
        chg = data.get("HYG_LQD_RATIO_20D_CHANGE_PCT")
        chg_str = f" (20일: {chg:+.1f}%)" if chg is not None else ""
        msg += f"• HYG/LQD 비율: {data['HYG_LQD_RATIO']:.4f}{chg_str}\n"
    if "MOVE_INDEX" in data:
        z_str = f" ({data['MOVE_ZSCORE']:+.1f}σ)" if "MOVE_ZSCORE" in data else ""
        msg += f"• MOVE 지수: {data['MOVE_INDEX']:.1f}{z_str}\n"
    msg += "\n"

    if alerts:
        msg += "\n".join(alerts) + "\n"
    else:
        msg += "특이사항 없음 (신용/채권시장 정상)\n"

    print(msg)
    send_telegram(msg)
    return msg

if __name__ == "__main__":
    run_credit_stress_detector()
