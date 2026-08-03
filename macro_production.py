# ==========================================
# 4. 수급 쏠림 및 레버리지 위험 평가 (순화/개선판)
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
