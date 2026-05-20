import re
import asyncio
import aiohttp
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional

# -----------------------------
# Lightweight HTTP utilities
# -----------------------------

DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=15)
UA = "ApexMacroBot/1.0 (+https://t.me/)"

async def _fetch_text(session: aiohttp.ClientSession, url: str) -> str:
    async with session.get(url, headers={"User-Agent": UA}) as r:
        r.raise_for_status()
        return await r.text()

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)

def _safe_strip(s: str) -> str:
    return (s or "").strip()

def _dedupe_keep_order(items: List[Dict], key: str) -> List[Dict]:
    seen = set()
    out = []
    for it in items:
        k = it.get(key)
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out

# -----------------------------
# RSS parsing (minimal)
# -----------------------------

def _parse_rss_items(xml_text: str, limit: int = 20) -> List[Dict]:
    """
    Minimal RSS/Atom parser using BeautifulSoup (xml mode).
    Returns list of: {title, link, published, summary}
    """
    soup = BeautifulSoup(xml_text, "xml")
    items = []

    # RSS <item>
    for it in soup.find_all("item")[:limit]:
        title = _safe_strip(it.title.get_text() if it.title else "")
        link = _safe_strip(it.link.get_text() if it.link else "")
        pub = _safe_strip(it.pubDate.get_text() if it.pubDate else "")
        desc = _safe_strip(it.description.get_text() if it.description else "")
        items.append({"title": title, "link": link, "published": pub, "summary": desc})

    # Atom <entry>
    if not items:
        for it in soup.find_all("entry")[:limit]:
            title = _safe_strip(it.title.get_text() if it.title else "")
            link_tag = it.find("link")
            link = ""
            if link_tag:
                link = _safe_strip(link_tag.get("href") or link_tag.get_text() or "")
            pub = _safe_strip((it.published.get_text() if it.published else "") or (it.updated.get_text() if it.updated else ""))
            summ = _safe_strip(it.summary.get_text() if it.summary else "")
            items.append({"title": title, "link": link, "published": pub, "summary": summ})

    return _dedupe_keep_order(items, "link")

# -----------------------------
# Economic calendar (High-impact)
# -----------------------------

FOREXFACTORY_RSS = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
# NOTE: This is a public XML feed widely used by traders. If it ever changes, swap in another calendar feed.

_COUNTRY_TO_ASSETS = {
    "USD": {"XAUUSD", "NAS100", "US30", "GBPUSD", "USDJPY"},
    "JPY": {"USDJPY"},
    "GBP": {"GBPUSD"},
    # Gold often reacts to USD + yields; equities to USD+rates; keep mapping simple.
    "ALL": {"XAUUSD", "NAS100", "US30", "GBPUSD", "USDJPY"},
}

def _impact_is_high(impact_text: str) -> bool:
    t = (impact_text or "").lower()
    return ("high" in t) or ("red" in t) or ("impact=3" in t)

def _normalize_currency(cur: str) -> str:
    cur = (cur or "").upper().strip()
    return cur if cur else "ALL"

def _parse_ff_datetime(raw: str) -> Optional[datetime]:
    """
    ForexFactory XML commonly contains date strings like:
    "May 20, 2026 12:30pm" or similar. We'll parse best-effort.
    If timezone not provided, assume UTC to keep scheduling deterministic.
    """
    raw = _safe_strip(raw)
    if not raw:
        return None

    # Try multiple known formats
    fmts = [
        "%b %d, %Y %I:%M%p",
        "%B %d, %Y %I:%M%p",
        "%b %d, %Y %H:%M",
        "%B %d, %Y %H:%M",
    ]
    for fmt in fmts:
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue

    # Fallback: try to extract "YYYY-mm-dd HH:MM"
    m = re.search(r"(\d{4}-\d{2}-\d{2}).*?(\d{2}:\d{2})", raw)
    if m:
        try:
            dt = datetime.strptime(m.group(1) + " " + m.group(2), "%Y-%m-%d %H:%M")
            return dt.replace(tzinfo=timezone.utc)
        except Exception:
            pass

    return None

async def fetch_high_impact_events(session: aiohttp.ClientSession, limit: int = 30) -> List[Dict]:
    """
    Returns a list of upcoming high-impact events:
    {id, time_utc, currency, title, impact, link}
    """
    xml = await _fetch_text(session, FOREXFACTORY_RSS)
    soup = BeautifulSoup(xml, "xml")

    out = []
    # The FF feed structure can vary; attempt robust parsing.
    # Common tags: <event> with children <title>, <country>, <date>, <time>, <impact>, etc.
    for ev in soup.find_all(["event", "item"]):
        title = ""
        currency = ""
        impact = ""
        dt = None
        link = ""

        # Try known tags
        if ev.find("title"):
            title = _safe_strip(ev.find("title").get_text())
        if ev.find("country"):
            currency = _normalize_currency(ev.find("country").get_text())
        if ev.find("currency"):
            currency = _normalize_currency(ev.find("currency").get_text())
        if ev.find("impact"):
            impact = _safe_strip(ev.find("impact").get_text())
        if ev.find("link"):
            link = _safe_strip(ev.find("link").get_text())

        # Date+time fields
        # Some feeds store full datetime in one field; others split date/time.
        raw_dt = ""
        if ev.find("datetime"):
            raw_dt = _safe_strip(ev.find("datetime").get_text())
        else:
            d = _safe_strip(ev.find("date").get_text() if ev.find("date") else "")
            t = _safe_strip(ev.find("time").get_text() if ev.find("time") else "")
            raw_dt = (d + " " + t).strip()

        dt = _parse_ff_datetime(raw_dt)

        # High impact filter
        # If impact tag isn't explicit, infer from title/desc where possible
        if not _impact_is_high(impact):
            # Heuristic: red-folder keywords
            if not re.search(r"\b(cpi|nfp|fed|fomc|rate|ppi|gdp|employment|inflation)\b", (title or "").lower()):
                continue

        if not title or not dt:
            continue

        # Only keep future-ish events (including a small lookback window for post-release)
        now = _now_utc()
        if dt < now - timedelta(hours=6):
            continue

        event_id = f"{currency}:{title}:{int(dt.timestamp())}"
        out.append({
            "id": event_id,
            "time_utc": dt,
            "currency": currency,
            "title": title,
            "impact": impact or "HIGH",
            "link": link or FOREXFACTORY_RSS
        })

    # Sort by time
    out.sort(key=lambda x: x["time_utc"])
    return out[:limit]

def assets_affected_by_currency(currency: str) -> List[str]:
    currency = _normalize_currency(currency)
    assets = _COUNTRY_TO_ASSETS.get(currency, set())
    if not assets and currency != "ALL":
        assets = _COUNTRY_TO_ASSETS.get("ALL", set())
    return sorted(list(assets))

# -----------------------------
# Geopolitical / breaking news
# -----------------------------

RSS_SOURCES = {
    # Reuters RSS availability can vary; keep multiple sources and keywords.
    # If a feed fails, the bot will still function.
    "Reuters World": "https://feeds.reuters.com/Reuters/worldNews",
    "Reuters Business": "https://feeds.reuters.com/reuters/businessNews",
    "CNBC Top": "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "CNBC World": "https://www.cnbc.com/id/100727362/device/rss/rss.html",
    # Bloomberg often blocks/changes RSS; provide an alternative politics-like feed source if present.
    # Keep it optional; failures are handled.
    "Bloomberg Politics": "https://www.bloomberg.com/politics/feeds/site.xml",
}

GEO_KEYWORDS = [
    "war", "missile", "drone", "strike", "attack", "ceasefire", "nuclear",
    "sanction", "tariff", "trade war", "export ban", "embargo",
    "iran", "israel", "gaza", "ukraine", "russia", "china", "taiwan",
    "opec", "pipeline", "shipping", "red sea", "strait", "hormuz"
]

def _is_geopolitical(title: str, summary: str) -> bool:
    text = f"{title} {summary}".lower()
    return any(k in text for k in GEO_KEYWORDS)

async def fetch_geopolitical_headlines(session: aiohttp.ClientSession, limit: int = 12) -> List[Dict]:
    tasks = []
    for name, url in RSS_SOURCES.items():
        tasks.append(_fetch_text(session, url))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    headlines = []
    for (name, url), res in zip(RSS_SOURCES.items(), results):
        if isinstance(res, Exception):
            continue
        items = _parse_rss_items(res, limit=25)
        for it in items:
            title = it.get("title", "")
            summary = it.get("summary", "")
            if not title:
                continue
            if _is_geopolitical(title, summary):
                headlines.append({
                    "source": name,
                    "title": title,
                    "link": it.get("link", url),
                    "published": it.get("published", "")
                })

    # Keep newest-ish by presence of published string; otherwise keep order
    headlines = _dedupe_keep_order(headlines, "link")
    return headlines[:limit]

# -----------------------------
# Intermarket drivers (DXY, US10Y)
# -----------------------------

YAHOO_QUOTES = {
    "DXY": "DX-Y.NYB",      # US Dollar Index (ICE)
    "US10Y": "^TNX",        # CBOE 10Y Treasury Yield index (TNX is yield*10)
}

async def fetch_intermarket(session: aiohttp.ClientSession) -> Dict:
    """
    Uses Yahoo quote API (public, lightweight) for quick snapshots.
    Returns:
      {
        "dxy": {"price": float|None},
        "us10y": {"yield": float|None},  # in %
        "risk_mode": "RISK-ON|RISK-OFF|MIXED",
      }
    """
    symbols = ",".join(YAHOO_QUOTES.values())
    url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={symbols}"

    try:
        data = await session.get(url, headers={"User-Agent": UA})
        data.raise_for_status()
        js = await data.json()
    except Exception:
        return {"dxy": {"price": None}, "us10y": {"yield": None}, "risk_mode": "MIXED"}

    quotes = {q.get("symbol"): q for q in (js.get("quoteResponse", {}).get("result", []) or [])}

    dxy_q = quotes.get(YAHOO_QUOTES["DXY"], {})
    tnx_q = quotes.get(YAHOO_QUOTES["US10Y"], {})

    dxy = dxy_q.get("regularMarketPrice", None)
    tnx = tnx_q.get("regularMarketPrice", None)  # TNX is yield*10
    us10y_yield = (tnx / 10.0) if isinstance(tnx, (int, float)) else None

    # Simple institutional flow heuristic
    # - DXY up + yields up = tighter USD liquidity => risk-off pressure
    # - DXY down + yields down = easing USD conditions => risk-on tailwind
    dxy_chg = dxy_q.get("regularMarketChangePercent", None)
    yld_chg = tnx_q.get("regularMarketChangePercent", None)

    risk_mode = "MIXED"
    if isinstance(dxy_chg, (int, float)) and isinstance(yld_chg, (int, float)):
        if dxy_chg > 0.15 and yld_chg > 0.15:
            risk_mode = "RISK-OFF"
        elif dxy_chg < -0.15 and yld_chg < -0.15:
            risk_mode = "RISK-ON"
        else:
            risk_mode = "MIXED"

    return {
        "dxy": {"price": dxy, "chg_pct": dxy_chg},
        "us10y": {"yield": us10y_yield, "chg_pct": yld_chg},
        "risk_mode": risk_mode
    }

# -----------------------------
# Central bank highlights (fast text parsing)
# -----------------------------

CB_SOURCES = {
    # These pages change; we keep broad URLs and extract text snippets.
    "FOMC": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
    "BoE": "https://www.bankofengland.co.uk/monetary-policy-summary-and-minutes",
    "BoJ": "https://www.boj.or.jp/en/mopo/mpmdeci/index.htm/",
}

_CB_KEYWORDS = [
    "inflation", "labor", "employment", "growth", "tight", "restrictive",
    "rates", "rate", "hike", "cut", "pause", "balance sheet",
    "hawkish", "dovish", "data dependent", "uncertainty", "risks",
    "financial conditions", "yield", "currency"
]

def _extract_key_sentences(text: str, max_sentences: int = 6) -> List[str]:
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return []
    # rough sentence split
    sents = re.split(r"(?<=[\.\!\?])\s+", text)
    picks = []
    for s in sents:
        sl = s.lower()
        if any(k in sl for k in _CB_KEYWORDS) and len(s) > 40:
            picks.append(s.strip())
        if len(picks) >= max_sentences:
            break
    # fallback: take first long sentences
    if not picks:
        for s in sents:
            if len(s) > 60:
                picks.append(s.strip())
            if len(picks) >= max_sentences:
                break
    return picks[:max_sentences]

async def fetch_central_bank_highlights(session: aiohttp.ClientSession) -> Dict[str, List[str]]:
    """
    Returns dict: {"FOMC":[...], "BoE":[...], "BoJ":[...]}
    Lightweight: grabs page HTML and pulls a few keyword-heavy sentences.
    """
    tasks = [ _fetch_text(session, url) for url in CB_SOURCES.values() ]
    htmls = await asyncio.gather(*tasks, return_exceptions=True)

    out = {}
    for (bank, url), html in zip(CB_SOURCES.items(), htmls):
        if isinstance(html, Exception):
            out[bank] = []
            continue
        soup = BeautifulSoup(html, "html.parser")
        # remove scripts/styles
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = soup.get_text(" ", strip=True)
        out[bank] = _extract_key_sentences(text, max_sentences=5)
    return out

# -----------------------------
# Asset bias helper (simple rule engine)
# -----------------------------

ASSETS = ["XAUUSD", "NAS100", "US30", "GBPUSD", "USDJPY"]

def build_asset_bias(asset: str, intermarket: Dict, upcoming_events: List[Dict], geo: List[Dict]) -> Dict:
    """
    Produces a compact, explainable bias block.
    Returns: {"asset":..., "bias":"BULLISH|BEARISH|NEUTRAL", "reasons":[...], "key_risks":[...]}
    """
    a = (asset or "").upper().strip()
    if a not in ASSETS:
        a = "XAUUSD"

    risk_mode = (intermarket or {}).get("risk_mode", "MIXED")
    dxy = (intermarket or {}).get("dxy", {}).get("chg_pct", None)
    yld = (intermarket or {}).get("us10y", {}).get("chg_pct", None)

    reasons = []
    risks = []

    # Macro impulses
    if risk_mode == "RISK-OFF":
        reasons.append("USD liquidity tightening bias (DXY↑ + yields↑) → defensive positioning.")
    elif risk_mode == "RISK-ON":
        reasons.append("USD conditions easing bias (DXY↓ + yields↓) → pro-cyclical positioning.")
    else:
        reasons.append("Intermarket signals mixed → position sizing discipline recommended.")

    # Geo
    if geo:
        reasons.append("Geopolitical risk premium elevated (headline flow flagged).")

    # Event risk in next 24h
    now = _now_utc()
    next_24h = [e for e in upcoming_events if now <= e["time_utc"] <= now + timedelta(hours=24)]
    if next_24h:
        risks.append(f"High-impact event risk in next 24h: {next_24h[0]['currency']} {next_24h[0]['title']}.")

    # Asset-specific mapping
    bias = "NEUTRAL"
    if a == "XAUUSD":
        # Gold often inverse to real yields / USD; geo supports gold
        if risk_mode == "RISK-OFF" or geo:
            bias = "BULLISH"
            reasons.append("Risk-off + geopolitical bid typically supports gold demand.")
        if isinstance(dxy, (int, float)) and dxy > 0.2 and isinstance(yld, (int, float)) and yld > 0.2:
            bias = "NEUTRAL"  # strong USD+yields can cap gold
            risks.append("Strong USD + rising yields can cap upside in XAUUSD.")
    elif a in ("NAS100", "US30"):
        if risk_mode == "RISK-ON":
            bias = "BULLISH"
            reasons.append("Easing USD conditions typically support equities (beta bid).")
        elif risk_mode == "RISK-OFF":
            bias = "BEARISH"
            reasons.append("Tighter USD conditions typically pressure equity multiples.")
    elif a == "GBPUSD":
        # Simplified: USD regime dominates short-term
        if isinstance(dxy, (int, float)) and dxy < -0.15:
            bias = "BULLISH"
            reasons.append("Softening USD impulse supports GBPUSD upside.")
        elif isinstance(dxy, (int, float)) and dxy > 0.15:
            bias = "BEARISH"
            reasons.append("Strengthening USD impulse pressures GBPUSD.")
        # BoE event risk mention
        for e in next_24h[:3]:
            if e["currency"] == "GBP":
                risks.append("BoE/UK data volatility window active → widen stops or reduce size.")
                break
    elif a == "USDJPY":
        # USDJPY is heavily yield differential driven; rising yields + USD often lifts USDJPY
        if isinstance(yld, (int, float)) and yld > 0.15 and isinstance(dxy, (int, float)) and dxy > 0.1:
            bias = "BULLISH"
            reasons.append("US yields + USD bid supports USDJPY upside (carry tailwind).")
        elif geo:
            bias = "NEUTRAL"
            risks.append("Risk-off/geo spikes can trigger JPY safe-haven flows (two-way risk).")
        elif isinstance(yld, (int, float)) and yld < -0.15:
            bias = "BEARISH"
            reasons.append("Falling yields reduce USDJPY carry support.")

    return {"asset": a, "bias": bias, "reasons": reasons[:5], "key_risks": risks[:5]}
