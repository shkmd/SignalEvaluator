"""Fetches recent headlines for a stock via Google News RSS and scores sentiment with a
lightweight keyword lexicon. No API key required; good enough for a directional read, not
a substitute for actually reading the news."""
import feedparser
import requests
from urllib.parse import quote_plus

POSITIVE_WORDS = {
    "upgrade", "upgraded", "beats", "beat", "surge", "surges", "rally", "rallies",
    "bullish", "record", "profit", "profits", "growth", "outperform", "strong",
    "wins", "win", "order", "orders", "jump", "jumps", "gains", "gain", "buy",
    "positive", "expands", "expansion", "raises", "raised", "boost", "boosts",
    "top", "soar", "soars", "rebound", "rebounds", "robust", "milestone",
}
NEGATIVE_WORDS = {
    "downgrade", "downgraded", "misses", "miss", "plunge", "plunges", "crash",
    "crashes", "bearish", "loss", "losses", "weak", "underperform", "probe",
    "fraud", "decline", "declines", "cut", "cuts", "sell", "negative", "fall",
    "falls", "falling", "slump", "slumps", "penalty", "fine", "default", "scam",
    "lawsuit", "layoffs", "resigns", "resignation", "concern", "concerns",
    "warning", "warns", "drop", "drops",
}


def fetch_news(company_query: str, max_items: int = 8) -> dict:
    query = quote_plus(f"{company_query} share")
    url = f"https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"

    try:
        resp = requests.get(url, timeout=6, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
        entries = feed.entries[:max_items]

        headlines = []
        pos_hits, neg_hits = 0, 0
        for e in entries:
            title = e.get("title", "")
            lower = title.lower()
            p = sum(1 for w in POSITIVE_WORDS if w in lower)
            n = sum(1 for w in NEGATIVE_WORDS if w in lower)
            pos_hits += p
            neg_hits += n
            headlines.append(
                {
                    "title": title,
                    "published": e.get("published", ""),
                    "link": e.get("link", ""),
                    "tone": "positive" if p > n else ("negative" if n > p else "neutral"),
                }
            )

        net = pos_hits - neg_hits
        if not headlines:
            label = "no_data"
        elif net >= 2:
            label = "positive"
        elif net <= -2:
            label = "negative"
        else:
            label = "neutral"

        return {
            "available": True,
            "headline_count": len(headlines),
            "headlines": headlines,
            "positive_hits": pos_hits,
            "negative_hits": neg_hits,
            "sentiment_label": label,
        }
    except Exception as e:
        return {"available": False, "reason": f"News fetch failed: {e}"}
