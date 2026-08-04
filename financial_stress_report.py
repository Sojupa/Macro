# ==========================================================
# financial_stress_report.py - 금융 스트레스 & 포지션 통합 리포터 (버그 수정판)
# ==========================================================
import os
import datetime
import requests
from dotenv import load_dotenv

from credit_stress_detector import fetch_credit_stress_data, evaluate_credit_stress
from liquidity_blood_pressure import fetch_liquidity_pressure_data, evaluate_liquidity_pressure
from position_crowded import fetch_cot_sp500_position, evaluate_position_crowded

load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID")


def send_telegram(text):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("❌ [오류] 텔레그램 토큰 또는 CHAT_ID가 설정되지 않았습니다.")
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        res = requests.post(url, data={"chat_id": CHAT_ID, "text": text}, timeout=10)
        return res.status_code == 200
    except Exception as e:
        print(f"❌ 텔레그램 전송 예외: {e}")
        return False


def run_combined_financial_stress_report():
    today_str = datetime.date.today().strftime("%Y-%m-%d")

    # 1. 신용 경색 데이터
    c_data = fetch_credit_stress_data()
    c_alerts = evaluate_credit_stress(c_data)

    # 2. 유동성 혈압계 데이터
    l_data = fetch_liquidity_pressure_data()
    l_alerts = evaluate_liquidity_pressure(l_data)

    # 3. 몰빵 지도(포지션 과밀도) 데이터
    p_data = fetch_cot_sp500_position()
    p_alerts = evaluate_position_crowded(p_data)

    # 통합 메시지 조립
    msg = f"🏥 [금융 스트레스 & 포지션 정밀 진단 — {today_str}]\n"
    msg += "━━━━━━━━━━━━━━━━━━━━\n\n"

    # [1] 신용 & 채권 변동성
    msg += "📌 1. 신용 & 채권 변동성 (Credit & MOVE)\n"
    if "HYG_LQD_RATIO" in c_data:
        chg = c_data.get("HYG_LQD_RATIO_20D_CHANGE_PCT")
        chg_str = f" (20일: {chg:+.1f}%)" if chg is not None else ""
        msg += f"• HYG/LQD 비율: {c_data['HYG_LQD_RATIO']:.4f}{chg_str}\n"
    if "MOVE_INDEX" in c_data:
        z_str = f" ({c_data['MOVE_ZSCORE']:+.1f}σ)" if "MOVE_ZSCORE" in c_data else ""
        msg += f"• MOVE 지수: {c_data['MOVE_INDEX']:.1f}{z_str}\n"
    if not c_data:
        msg += "⚪ 데이터 미확보\n"
    msg += "\n"

    # [2] 단기 자금 & 은행 유동성
    msg += "📌 2. 단기 자금 & 은행 유동성 (Repo & Bank)\n"
    if "SOFR_IORB_SPREAD" in l_data:
        z_str = f" ({l_data['SOFR_IORB_ZSCORE']:+.1f}σ)" if "SOFR_IORB_ZSCORE" in l_data else ""
        msg += f"• SOFR-IORB 스프레드: {l_data['SOFR_IORB_SPREAD']:+.1f}bp{z_str}\n"
    if "PRIMARY_CREDIT_USD_B" in l_data:
        chg = l_data.get("PRIMARY_CREDIT_WOW_CHANGE_B")
        chg_str = f" (전주 대비 {chg:+.1f}B)" if chg is not None else ""
        msg += f"• Primary Credit 잔액: ${l_data['PRIMARY_CREDIT_USD_B']:,.1f}B{chg_str}\n"
    if not l_data:
        msg += "⚪ FRED_API_KEY 미설정 또는 데이터 미확보\n"
    msg += "\n"

    # [3] 포지션 과밀도 (COT)
    msg += "📌 3. 포지션 쏠림 (CFTC COT)\n"
    if "LEV_FUND_NET_SP500" in p_data:
        z_str = f" ({p_data['LEV_FUND_NET_ZSCORE']:+.1f}σ)" if "LEV_FUND_NET_ZSCORE" in p_data else ""
        msg += f"• S&P500 선물 순포지션: {p_data['LEV_FUND_NET_SP500']:+,}계약{z_str}\n"
    else:
        msg += "• CFTC COT 포지션: 데이터 수집 대기 중\n"
    msg += "\n"

    # [4] 경보 발생 종합
    all_alerts = c_alerts + l_alerts + p_alerts
    msg += "🚨 [위험 신호 감지 결과]\n"
    if all_alerts:
        msg += "\n".join(all_alerts) + "\n"
    else:
        msg += "✅ 특이사항 없음 (신용/유동성/포지션 전반 정상)\n"

    print(msg)
    if send_telegram(msg):
        print("✅ 금융 스트레스 통합 리포트 전송 성공!")
    else:
        print("❌ 텔레그램 전송 실패")


if __name__ == "__main__":
    run_combined_financial_stress_report()
