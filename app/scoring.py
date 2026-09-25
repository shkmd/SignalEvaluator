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


STAGE_NAMES = {1: "Stage 1 (basing)", 2: "Stage 2 (advancing)", 3: "Stage 3 (topping)", 4: "Stage 4 (declining)"}


def market_context_factor(ctx: dict, direction: str):
    """(points out of 15, note, red_flags). Bullish calls want a Stage 2 stock with a high RS
    rank in a strong sector and a broad market; bearish calls want the mirror image.
    Breakdown: stage 6 + stock RS 4 + sector RS 3 + market breadth 2."""
    bull = direction == "bullish"
    stage, rs = ctx.get("stage"), ctx.get("rs")
    flags = []

    stage_pts = ({2: 6, 3: 3, 1: 2, 4: 0} if bull else {4: 6, 3: 3, 1: 2, 2: 0}).get(stage, 0)

    fav_rs = rs if bull else 100 - rs
    rs_pts = 4 if fav_rs >= 70 else 3 if fav_rs >= 50 else 1 if fav_rs >= 30 else 0

    sector_rs = ctx.get("sector_rs")
    if sector_rs is None:
        sector_pts, sector_note = 1, "sector n/a"
    else:
        fav_sec = sector_rs if bull else 100 - sector_rs
        sector_pts = 3 if fav_sec >= 60 else 2 if fav_sec >= 45 else 1 if fav_sec >= 30 else 0
        sector_note = f"{ctx.get('sector')} median RS {sector_rs}"

    regime = ctx.get("regime")
    aligned = (bull and regime == "bullish") or (not bull and regime == "bearish")
    opposed = (bull and regime == "bearish") or (not bull and regime == "bullish")
    breadth_pts = 2 if aligned else 0 if opposed else 1

    if bull and stage == 4:
        flags.append("Stock is in Weinstein Stage 4 (below a falling 30-week line) -- buying against a long-term downtrend.")
    if not bull and stage == 2:
        flags.append("Stock is in Weinstein Stage 2 (above a rising 30-week line) -- shorting/PE against a long-term uptrend.")
    if fav_rs < 30:
        flags.append(f"Relative strength rank is {rs}/99 -- the stock is among the {'laggards' if bull else 'leaders'} of the F&O universe.")
    if opposed:
        flags.append(f"Market breadth is {regime} ({ctx.get('breadth_pct')}% of F&O stocks above their 30-week line), against this call.")

    note = (
        f"{STAGE_NAMES.get(stage, 'stage n/a')}, RS {rs}/99, {sector_note}, "
        f"breadth {ctx.get('breadth_pct')}% above 30-wk line ({regime})"
    )
    return stage_pts + rs_pts + sector_pts + breadth_pts, note, flags


def evaluate_signal(
    signal: dict,
    technicals: dict,
    options_data: dict,
    news: dict,
    screener: dict = None,
    stock_context: dict = None,
    market_context: dict = None,
) -> dict:
    screener = screener or {"available": False}
    stock_context = stock_context or {"available": False}
    market_context = market_context or {"available": False}
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

    # --- Stock context: market-cap tier + sector strength (15 pts) ---
    max_score += 15
    if stock_context.get("available"):
        tier = stock_context.get("tier")
        tier_pts = {"large-cap": 8, "mid-cap": 5, "small-cap": 2, "unknown": 0}.get(tier, 0)
        score += tier_pts
        cap_note = (
            f"{tier} (~₹{stock_context['market_cap_cr']:,.0f} cr)" if stock_context.get("market_cap_cr") else tier
        )
        if tier == "small-cap":
            red_flags.append("Small-cap stock -- lower liquidity, wider spreads, higher volatility risk.")

        sector_pts = 0
        sector_note = "Sector strength not available."
        rel = stock_context.get("sector_rel_strength_10d")
        if rel is not None:
            favorable = (direction == "bullish" and rel > 0) or (direction == "bearish" and rel < 0)
            sector_pts = 7 if favorable else 0
            sector_note = (
                f"10d vs {stock_context.get('sector')} sector: {rel:+.2%} "
                f"({'leading peers' if favorable else 'lagging peers'})"
            )
            if not favorable:
                red_flags.append(
                    f"Stock is lagging its own sector ({stock_context.get('sector')}), which cuts against this call."
                )
        score += sector_pts

        breakdown.append(("Stock context", tier_pts + sector_pts, 15, f"{cap_note}. {sector_note}"))
    else:
        breakdown.append(("Stock context", 0, 15, stock_context.get("reason", "Not available.")))

    # --- Market context: stage / RS rank / sector / breadth (15 pts) ---
    # Only scored when the nightly job has data for this symbol -- for indices and non-F&O
    # names there is nothing to score, and adding an always-zero row would drag their score down.
    if market_context.get("available"):
        mc_pts, mc_note, mc_flags = market_context_factor(market_context, direction)
        max_score += 15
        score += mc_pts
        breakdown.append(("Market context", mc_pts, 15, mc_note))
        red_flags.extend(mc_flags)

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
