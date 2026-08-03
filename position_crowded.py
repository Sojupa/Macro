# ==========================================================
# position_crowded.py - 몰빵 지도 (포지션 과밀도)
# - CFTC COT(Commitment of Traders) 리포트: 헤지펀드(레버리지 펀드)의 S&P500 선물 순포지션
#   무료/무키 Socrata API 사용 (publicreporting.cftc.gov). 주 1회(금요일) 갱신.
# - 해석은 양면적: 헤지펀드 순숏 극단치는 "롱 입장에선 위험 + 숏 입장에선 숏커버 랠리 가능"
#   두 시나리오를 모두 제시 (친구 제안 그대로 반영)
# ⚠️ KOSPI200 선물 외국인 누적 포지션은 pykrx로 구현 불가 확인(파생상품 투자자 포지션 함수 없음).
#    TODO: KRX 정보데이터시스템 직접 스크레이핑 또는 유료 데이터 필요 — 이번 버전 미포함.
# ==========================================================
import os
import datetime
import requests
import pandas as pd
from dotenv import load_dotenv

load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# CFTC Socrata: Traders in Financial Futures (TFF) Futures Only 리포트
# CFTC_Contract_Market_Code: "13874A" = E-MINI S&P 500
TFF_DATASET_URL = "https://publicreporting.cftc.gov/resource/gpe5-46if.json"
SP500_CONTRACT_CODE = "13874A"
ZSCORE_WINDOW = 52  # 최근 52주(약 1년) 기준 극단치 판정


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
    """CFTC TFF 리포트에서 레버리지 펀드(Leveraged Funds)의 S&P500 선물 순포지션을 조회.
    ⚠️ 무료 공개 API지만 공식 API가 아닌 Socrata 오픈데이터 포털이라 필드명이 바뀔 수 있음."""
    data = {}
    try:
        params = {
            "$where": f"cftc_contract_market_code='{SP500_CONTRACT_CODE}'",
            "$order": "report_date_as_yyyy_mm_dd DESC",
            "$limit": ZSCORE_WINDOW,
        }
        res = requests.get(TFF_DATASET_URL, params=params, timeout=15)
        if res.status_code != 200:
            print(f"[position_crowded] CFTC API 응답 실패: status={res.status_code}")
            return data

        rows = res.json()
        if not rows:
            print("[position_crowded] CFTC API: 해당 계약코드로 데이터 없음 (계약코드 재확인 필요)")
            return data

        df = pd.DataFrame(rows)
        # Leveraged Funds(레버리지 펀드=헤지펀드 프록시) 롱/숏 컬럼명 확인 (TFF 리포트 표준 필드)
        long_col = "lev_money_positions_long"
        short_col = "lev_money_positions_short"
        if long_col not in df.columns or short_col not in df.columns:
            print(f"[position_crowded] 예상 컬럼 없음. 실제 컬럼: {list(df.columns)[:15]}")
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
        if z <= -1.5:
            alerts.append(
                f"🟡 [포지션 과밀-양면] 레버리지 펀드 S&P500 순숏 {abs(net):,}계약 (52주 대비 {z:+.1f}σ 극단)\n"
                f"   → 롱 관점: 추가 하락 압력 지속 가능 / 숏 관점: 과도한 순숏 누적 시 숏커버 랠리 리스크 유의"
            )
        elif z >= 1.5:
            alerts.append(
                f"🟡 [포지션 과밀-양면] 레버리지 펀드 S&P500 순롱 {net:,}계약 (52주 대비 {z:+.1f}σ 극단)\n"
                f"   → 롱 관점: 과열 시 되돌림 리스크 / 숏 관점: 롱 청산발 급락 트리거 가능성 유의"
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
        msg += "⚪ CFTC COT 데이터 미확보 (API 필드명 변경 가능성 — 콘솔 로그 확인 필요)\n\n"

    if alerts:
        msg += "\n".join(alerts) + "\n"
    else:
        msg += "특이사항 없음 (포지션 과밀 신호 없음)\n"

    msg += "\n※ KOSPI200 선물 외국인 누적 포지션은 무료 데이터 소스 미확보로 이번 버전엔 미포함\n"

    print(msg)
    if send_telegram(msg):
        print("텔레그램 전송 성공!")
    return msg


if __name__ == "__main__":
    run_position_crowded()
