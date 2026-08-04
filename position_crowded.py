# ==========================================================
# position_crowded.py - 몰빵 지도 (포지션 과밀도 - 문구 교정판)
#
# [2026-08-04 팩트체크] SP500_CONTRACT_CODE = "13874A"는 CME E-mini S&P 500
# Stock Index의 CFTC 코드가 맞음 (CFTC TFF 리포트/tradingster 등 교차 확인 완료).
# 코드 값 자체는 문제 없음 — 수정 없이 유지.
# ==========================================================
import os
import datetime
import requests
import pandas as pd
from dotenv import load_dotenv

load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID")

TFF_DATASET_URL = "https://publicreporting.cftc.gov/resource/gpe5-46if.json"
SP500_CONTRACT_CODE = "13874A"
ZSCORE_WINDOW = 52

THRESHOLDS = {
    "LEV_FUND_NET_ZSCORE_HARD": 1.5,
}


def send_telegram(text):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        res = requests.post(url, data={"chat_id": CHAT_ID, "text": text}, timeout=10)
        return res.status_code == 200
    except Exception:
        return False


def fetch_cot_sp500_position():
    data = {}
    try:
        params = {
            "$where": f"cftc_contract_market_code='{SP500_CONTRACT_CODE}'",
            "$order": "report_date_as_yyyy_mm_dd DESC",
            "$limit": ZSCORE_WINDOW,
        }
        res = requests.get(TFF_DATASET_URL, params=params, timeout=15)
        if res.status_code != 200:
            print(f"[position_crowded] CFTC API 응답 오류: {res.status_code}")
            return data

        rows = res.json()
        if not rows:
            return data

        df = pd.DataFrame(rows)
        long_col = "lev_money_positions_long"
        short_col = "lev_money_positions_short"
        if long_col not in df.columns or short_col not in df.columns:
            return data

        df[long_col] = pd.to_numeric(df[long_col], errors="coerce")
        df[short_col] = pd.to_numeric(df[short_col], errors="coerce")
        df["net"] = df[long_col] - df[short_col]
        df = df.sort_values("report_date_as_yyyy_mm_dd")

        net_now = df["net"].iloc[-1]
        data["LEV_FUND_NET_SP500"] = int(net_now)
        data["LEV_FUND_NET_DATE"] = df["report_date_as_yyyy_mm_dd"].iloc[-1][:10]

        if len(df) >= 20:
            mean, std = df["net"].mean(), df["net"].std()
            if std > 0:
                data["LEV_FUND_NET_ZSCORE"] = round(float((net_now - mean) / std), 2)
    except Exception as e:
        print(f"[position_crowded] COT 데이터 수집 실패: {e}")
    return data


def evaluate_position_crowded(data):
    alerts = []
    net = data.get("LEV_FUND_NET_SP500")
    z = data.get("LEV_FUND_NET_ZSCORE")

    if net is not None and z is not None:
        pos_type = "순롱" if net >= 0 else "순숏"
        abs_net_str = f"{net:+,}계약" if net >= 0 else f"{net:,}계약"

        if abs(z) >= THRESHOLDS["LEV_FUND_NET_ZSCORE_HARD"]:
            if net < 0:
                alerts.append(
                    f"🟡 [포지션 과밀-양면] 레버리지 펀드 S&P500 {pos_type} {abs_net_str} (52주 대비 {z:+.1f}σ 변화)\n"
                    f"   → 롱 관점: 숏 포지션 부담 지속 / 숏 관점: 숏커버링 유입 시 단기 반등 리스크 유의"
                )
            else:
                alerts.append(
                    f"🟡 [포지션 과밀-양면] 레버리지 펀드 S&P500 {pos_type} {abs_net_str} (52주 대비 {z:+.1f}σ 극단)\n"
                    f"   → 롱 관점: 매수 과열에 따른 되돌림 유의 / 숏 관점: 롱 청산발 급락 가능성 유의"
                )

    return alerts


def run_position_crowded():
    data = fetch_cot_sp500_position()
    alerts = evaluate_position_crowded(data)

    today_str = datetime.date.today().strftime("%Y-%m-%d")
    msg = f"🗺️ [몰빵 지도 (포지션 과밀도) — {today_str}]\n\n"
    if "LEV_FUND_NET_SP500" in data:
        z_str = f" ({data['LEV_FUND_NET_ZSCORE']:+.1f}σ)" if "LEV_FUND_NET_ZSCORE" in data else ""
        msg += f"• 레버리지 펀드 S&P500 순포지션: {data['LEV_FUND_NET_SP500']:+,}계약{z_str}\n"
        msg += f"  (CFTC 리포트 기준일: {data.get('LEV_FUND_NET_DATE', 'N/A')}, 매주 금요일 갱신)\n\n"
    else:
        msg += "⚪ CFTC COT 데이터 미확보\n\n"

    if alerts:
        msg += "\n".join(alerts) + "\n"
    else:
        msg += "특이사항 없음 (포지션 과밀 신호 없음)\n"

    print(msg)
    send_telegram(msg)
    return msg


if __name__ == "__main__":
    run_position_crowded()
