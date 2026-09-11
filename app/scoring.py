"""Combines parsed signal + technicals + options + news into a composite score,
verdict, red flags, and a risk/reward readout."""


def risk_reward(entry_low, entry_high, sl, targets):
    if entry_low is None or sl is None or not targets:
        return {"available": False}

    entry = entry_high if entry_high is not None else entry_low
    risk = entry - sl
    if risk <= 0:
        return {"available": False, "reason": "SL is not below entry -- check the signal."}

    first_target = min(targets)
    last_target = max(targets)
    rr_first = (first_target - entry) / risk
    rr_last = (last_target - entry) / risk

    return {
        "available": True,
        "entry_used": round(entry, 2),
        "risk_per_unit": round(risk, 2),
        "rr_first_target": round(rr_first, 2),
        "rr_last_target": round(rr_last, 2),
    }


def evaluate_signal(signal: dict, technicals: dict, options_data: dict, news: dict, screener: dict = None) -> dict:
    screener = screener or {"available": False}
    direction = "bearish" if (signal.get("instrument") == "PE" or signal.get("action") == "sell") else "bullish"

    score = 0
    max_score = 0
    breakdown = []
    red_flags = []

    # --- Trend alignment (25 pts) ---
    max_score += 25
    if technicals.get("available"):
        if technicals.get("aligned_with_direction"):
            score += 25
            breakdown.append(("Trend alignment", 25, 25, "Price/EMA structure agrees with the call's direction."))
        elif technicals.get("against_direction"):
            breakdown.append(("Trend alignment", 0, 25, "Price/EMA structure is AGAINST the call's direction."))
            red_flags.append("Signal direction is against the prevailing EMA trend.")
        else:
            score += 10
            breakdown.append(("Trend alignment", 10, 25, "Trend is mixed/sideways, not clearly aligned."))
    else:
        breakdown.append(("Trend alignment", 0, 25, "No price data available."))
        red_flags.append("Could not fetch price data to check trend.")

    # --- Breakout + volume confirmation (20 pts) ---
    max_score += 20
    if technicals.get("available"):
        breakout_relevant = technicals.get("breakout_up") if direction == "bullish" else technicals.get("breakdown")
        vol_ratio = technicals.get("vol_ratio")
        if breakout_relevant and vol_ratio and vol_ratio >= 1.5:
            score += 20
            breakdown.append(("Breakout + volume", 20, 20, f"Breakout confirmed with {vol_ratio}x avg volume."))
        elif breakout_relevant:
            score += 10
            breakdown.append(("Breakout + volume", 10, 20, "Breakout in price, but volume is not confirming it."))
            red_flags.append("Breakout without strong volume confirmation (weak conviction).")
        else:
            breakdown.append(("Breakout + volume", 0, 20, "No breakout of the 20-day range in this direction."))
    else:
        breakdown.append(("Breakout + volume", 0, 20, "No price data available."))

    # --- RSI sanity (10 pts) ---
    max_score += 10
    if technicals.get("available"):
        rsi = technicals.get("rsi14")
        if direction == "bullish":
            if 45 <= rsi <= 70:
                score += 10
                breakdown.append(("RSI", 10, 10, f"RSI {rsi} is in a healthy bullish zone."))
            elif rsi > 70:
                score += 3
                breakdown.append(("RSI", 3, 10, f"RSI {rsi} is overbought -- chasing extended momentum."))
                red_flags.append("RSI is overbought; entry may be chasing an extended move.")
            else:
                score += 3
                breakdown.append(("RSI", 3, 10, f"RSI {rsi} is weak/below 45 for a bullish call."))
        else:
            if 30 <= rsi <= 55:
                score += 10
                breakdown.append(("RSI", 10, 10, f"RSI {rsi} is in a healthy bearish zone."))
            elif rsi < 30:
                score += 3
                breakdown.append(("RSI", 3, 10, f"RSI {rsi} is oversold -- chasing extended downside."))
                red_flags.append("RSI is oversold; entry may be chasing an extended move.")
            else:
                score += 3
                breakdown.append(("RSI", 3, 10, f"RSI {rsi} is not confirming bearish momentum."))
    else:
        breakdown.append(("RSI", 0, 10, "No price data available."))

    # --- Relative strength vs Nifty (10 pts) ---
    max_score += 10
    if technicals.get("available") and technicals.get("rel_strength_10d") is not None:
        rel = technicals["rel_strength_10d"]
        favorable = (direction == "bullish" and rel > 0) or (direction == "bearish" and rel < 0)
        if favorable:
            score += 10
            breakdown.append(("Relative strength vs Nifty", 10, 10, f"10d relative strength {rel:+.2%} favors this direction."))
        else:
            breakdown.append(("Relative strength vs Nifty", 0, 10, f"10d relative strength {rel:+.2%} does not favor this direction."))
            red_flags.append("Stock is underperforming/outperforming the index against the call's direction.")
    else:
        breakdown.append(("Relative strength vs Nifty", 0, 10, "Not available."))

    # --- Options OI/IV context (10 pts) ---
    max_score += 10
    if options_data.get("available"):
        coi = options_data.get("change_in_oi") or 0
        if coi > 0:
            score += 10
            breakdown.append(("Options OI buildup", 10, 10, "Fresh OI buildup at this strike (conviction, not just a squeeze)."))
        else:
            score += 4
            breakdown.append(("Options OI buildup", 4, 10, "OI is flat/unwinding at this strike -- move may be short covering, not fresh conviction."))
    else:
        breakdown.append(("Options OI buildup", 0, 10, options_data.get("reason", "Not available.")))

    # --- News sentiment (15 pts) ---
    max_score += 15
    if news.get("available") and news.get("sentiment_label") != "no_data":
        label = news["sentiment_label"]
        if label == "positive":
            add = 15 if direction == "bullish" else 3
            score += add
            breakdown.append(("News sentiment", add, 15, "Recent headlines skew positive."))
            if direction == "bearish":
                red_flags.append("Recent news sentiment is positive, which cuts against a bearish/PE call.")
        elif label == "negative":
            add = 15 if direction == "bearish" else 3
            score += add
            breakdown.append(("News sentiment", add, 15, "Recent headlines skew negative."))
            if direction == "bullish":
                red_flags.append("Recent news sentiment is negative, which cuts against a bullish/CE call.")
        else:
            score += 8
            breakdown.append(("News sentiment", 8, 15, "Recent headlines are neutral/mixed."))
    else:
        breakdown.append(("News sentiment", 0, 15, "No recent headlines found."))

    # --- Screener confirmation (20 pts) ---
    max_score += 20
    if screener.get("available"):
        passed, total = screener["passed"], screener["total"]
        pts = round(20 * passed / total) if total else 0
        score += pts
        failed_names = [c["name"] for c in screener["checks"] if not c["passed"]]
        note = f"{passed}/{total} screener conditions met."
        if failed_names:
            note += " Missing: " + "; ".join(failed_names[:3]) + ("…" if len(failed_names) > 3 else "")
        breakdown.append(("Screener confirmation", pts, 20, note))
        if passed / total < 0.5:
            red_flags.append(f"Only {passed}/{total} screener conditions met (range expansion, MTF trend, volume, SMA, intraday follow-through).")
    else:
        breakdown.append(("Screener confirmation", 0, 20, screener.get("reason", "Not available.")))

    # --- Risk/Reward (10 pts) ---
    max_score += 10
    rr = risk_reward(signal.get("entry_low"), signal.get("entry_high"), signal.get("sl"), signal.get("targets"))
    if rr.get("available"):
        rr1 = rr["rr_first_target"]
        if rr1 >= 2:
            score += 10
            breakdown.append(("Risk/Reward", 10, 10, f"R:R to first target is {rr1}:1 or better."))
        elif rr1 >= 1:
            score += 5
            breakdown.append(("Risk/Reward", 5, 10, f"R:R to first target is only {rr1}:1."))
            red_flags.append("Risk/reward to the first target is thin (<2:1).")
        else:
            breakdown.append(("Risk/Reward", 0, 10, f"R:R to first target is poor ({rr1}:1)."))
            red_flags.append("Risk/reward to the first target is worse than 1:1.")
    else:
        breakdown.append(("Risk/Reward", 0, 10, rr.get("reason", "Entry/SL/targets incomplete -- cannot compute.")))
        red_flags.append("Cannot compute risk/reward -- signal is missing entry, SL, or targets.")

    pct = round(100 * score / max_score, 1) if max_score else 0

    if pct >= 70:
        verdict = "Strong setup"
    elif pct >= 50:
        verdict = "Moderate -- proceed with reduced size / tight risk control"
    else:
        verdict = "Weak -- consider skipping or waiting for confirmation"

    return {
        "direction": direction,
        "score": pct,
        "max_score": 100,
        "verdict": verdict,
        "breakdown": breakdown,
        "red_flags": red_flags,
        "risk_reward": rr,
    }
