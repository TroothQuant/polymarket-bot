"""Gamma API integration for market discovery and filtering."""

import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import requests

from config import BotConfig
from models import MarketInfo

log = logging.getLogger("bot.scanner")

# Keywords for rough category classification from event slugs/titles
CATEGORY_KEYWORDS = {
    "politics": ["president", "election", "congress", "senate", "governor", "vote", "party",
                  "democrat", "republican", "trump", "biden", "political", "inaugur",
                  "legislation", "supreme court", "cabinet", "impeach", "primary"],
    "geopolitics": ["iran", "israel", "strike", "invade", "invasion", "war", "military",
                    "nato", "sanction", "nuclear", "missile", "ceasefire", "peace deal",
                    "china", "taiwan", "russia", "ukraine", "north korea", "tariff"],
    "sports": ["nfl", "nba", "mlb", "nhl", "soccer", "football", "basketball", "baseball",
               "tennis", "ufc", "fight", "championship", "super bowl", "world series",
               "premier league", "match", "game", "serie a", "ncaa", "ligue 1",
               "olympics", "medal", "la liga", "bundesliga", "win on 202", "rio open",
               "open:", "grand slam"],
    "crypto": ["bitcoin", "btc", "ethereum", "eth", "crypto", "solana", "sol", "token",
               "defi", "blockchain", "coin", "memecoin", "fdv", "airdrop"],
    "tech": ["ai model", "claude", "gpt", "openai", "anthropic", "google ai", "apple",
             "microsoft", "tesla", "spacex", "launch", "release", "chip", "semiconductor"],
    "social_media": ["tweet", "post", "elon musk", "follower", "subscriber", "tiktok",
                     "youtube", "instagram", "x.com"],
    "weather": ["weather", "temperature", "hurricane", "storm", "rainfall", "snow", "climate"],
    "entertainment": ["oscar", "grammy", "emmy", "movie", "film", "tv", "show", "album",
                      "music", "celebrity", "award", "box office"],
    "finance": ["fed", "interest rate", "inflation", "gdp", "stock", "market", "s&p",
                "nasdaq", "dow", "recession", "unemployment", "spx", "treasury"],
}


class MarketScanner:
    def __init__(self, config: BotConfig):
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        self.base_url = config.gamma_api_host

    def scan(self) -> list[MarketInfo]:
        """Fetch all active markets, filter, and return eligible MarketInfo list."""
        raw_events = self._fetch_all_events()
        markets = []

        for event in raw_events:
            event_title = event.get("title", "")
            event_slug = event.get("slug", "")
            description = event.get("description", "")
            category = self._categorize(event_title, event_slug)

            for mkt in event.get("markets", []):
                parsed = self._parse_market(mkt, event_title, description, category)
                if parsed is not None:
                    markets.append(parsed)

        # Sort by 24h volume descending (highest activity first)
        markets.sort(key=lambda m: m.volume_24hr, reverse=True)
        log.info(f"Scan complete: {len(raw_events)} events, {len(markets)} eligible markets")
        return markets

    def _fetch_all_events(self) -> list[dict]:
        """Fetch all active events with pagination."""
        all_events = []
        offset = 0
        limit = 100

        while True:
            page = self._fetch_events_page(offset, limit)
            if not page:
                break
            all_events.extend(page)
            if len(page) < limit:
                break
            offset += limit

        return all_events

    def _fetch_events_page(self, offset: int, limit: int = 100) -> list[dict]:
        """Single page fetch with retry logic."""
        url = f"{self.base_url}/events"
        params = {
            "active": "true",
            "closed": "false",
            "limit": limit,
            "offset": offset,
        }

        for attempt in range(3):
            try:
                resp = self.session.get(url, params=params, timeout=30)
                if resp.status_code == 429:
                    wait = 2 ** attempt
                    log.warning(f"Rate limited on /events, retrying in {wait}s")
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp.json()
            except requests.RequestException as e:
                if attempt < 2:
                    wait = 2 ** attempt
                    log.warning(f"Error fetching events (attempt {attempt + 1}): {e}, retrying in {wait}s")
                    time.sleep(wait)
                else:
                    log.error(f"Failed to fetch events after 3 attempts: {e}")
                    return []

        return []

    def _parse_market(self, mkt: dict, event_title: str, description: str,
                      category: str) -> Optional[MarketInfo]:
        """Parse a single market dict into MarketInfo. Returns None if filtered out."""
        try:
            # Must be active and accepting orders
            if not mkt.get("active", False) or mkt.get("closed", False):
                return None

            # Parse outcome prices (JSON-encoded string)
            outcomes_raw = mkt.get("outcomes", "[]")
            if isinstance(outcomes_raw, str):
                outcomes = json.loads(outcomes_raw)
            else:
                outcomes = outcomes_raw

            # Only binary markets
            if len(outcomes) != 2:
                return None

            prices_raw = mkt.get("outcomePrices", "[]")
            if isinstance(prices_raw, str):
                prices = json.loads(prices_raw)
            else:
                prices = prices_raw

            if len(prices) != 2:
                return None

            yes_price = float(prices[0])
            no_price = float(prices[1])

            # Parse token IDs (JSON-encoded string)
            tokens_raw = mkt.get("clobTokenIds", "[]")
            if isinstance(tokens_raw, str):
                tokens = json.loads(tokens_raw)
            else:
                tokens = tokens_raw

            if len(tokens) != 2:
                return None

            token_yes = tokens[0]
            token_no = tokens[1]

            # Liquidity and volume filters
            liquidity = float(mkt.get("liquidity", 0) or 0)
            volume = float(mkt.get("volume", 0) or 0)
            volume_24hr = float(mkt.get("volume24hr", 0) or 0)

            if liquidity < self.config.min_liquidity:
                return None
            if volume_24hr < self.config.min_volume_24hr:
                return None

            # Price filter — skip markets where neither side is in the tradeable range
            # Markets at extreme prices (e.g. YES=0.001, NO=0.999) have no FOK liquidity
            min_p = self.config.min_market_price
            max_p = 1.0 - min_p
            if not (min_p <= yes_price <= max_p or min_p <= no_price <= max_p):
                return None

            # Time to resolution filter
            end_date_str = mkt.get("endDate", "")
            if end_date_str:
                try:
                    end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                    now = datetime.now(timezone.utc)
                    hours_left = (end_date - now).total_seconds() / 3600
                    if hours_left < self.config.min_time_to_resolution_hours:
                        return None
                except (ValueError, TypeError):
                    pass  # If we can't parse the date, don't filter on it

            # Spread
            best_bid = float(mkt.get("bestBid", 0) or 0)
            best_ask = float(mkt.get("bestAsk", 0) or 0)
            spread = best_ask - best_bid if best_ask > best_bid else 0.0

            # Filter wide spreads — indicates thin liquidity and poor fill quality
            if best_bid > 0 and best_ask > 0 and spread > self.config.max_spread:
                return None

            question = mkt.get("question", event_title)
            slug = mkt.get("slug", "")
            mkt_description = mkt.get("description", description)

            return MarketInfo(
                condition_id=mkt.get("conditionId", ""),
                question=question,
                slug=slug,
                outcome_yes_price=yes_price,
                outcome_no_price=no_price,
                token_id_yes=token_yes,
                token_id_no=token_no,
                liquidity=liquidity,
                volume=volume,
                volume_24hr=volume_24hr,
                best_bid=best_bid,
                best_ask=best_ask,
                spread=spread,
                end_date=end_date_str,
                category=category,
                event_title=event_title,
                description=mkt_description or "",
            )

        except (KeyError, ValueError, TypeError) as e:
            log.debug(f"Failed to parse market: {e}")
            return None

    def _categorize(self, title: str, slug: str) -> str:
        """Classify market category from event title/slug using keyword matching."""
        text = f"{title} {slug}".lower()
        for category, keywords in CATEGORY_KEYWORDS.items():
            if any(kw in text for kw in keywords):
                return category
        return "other"

    def check_market_resolution(self, condition_id: str) -> Optional[dict]:
        """Check if a market has resolved.
        Returns:
          {"winning_side": "YES"|"NO"} if resolved decisively,
          {"status": "void"}           if 50-50 cash settlement,
          {"status": "unknown_delisted"} if CLOB 404 and Gamma also can't confirm,
          None                          if still open / inconclusive."""
        try:
            url = f"{self.config.clob_host}/markets/{condition_id}"
            resp = self.session.get(url, timeout=10)
            if resp.status_code == 404:
                # CLOB has dropped the market — could be resolved+pruned or
                # UMA-resolved on Gamma but never propagated to CLOB. Fall back.
                return self._resolve_via_gamma(condition_id)
            resp.raise_for_status()
            data = resp.json()

            if not data.get("closed", False):
                return None

            # CLOB returns tokens array with winner flag
            tokens = data.get("tokens", [])
            for token in tokens:
                if token.get("winner", False):
                    outcome = token.get("outcome", "").upper()
                    if outcome == "YES":
                        return {"winning_side": "YES"}
                    elif outcome == "NO":
                        return {"winning_side": "NO"}

            # Market closed but no winner flag matched. Two known shapes land
            # here: (a) 50-50 void settlements (no token carries winner=True),
            # (b) sports/esports markets whose token outcomes are team names,
            # never YES/NO. CLOB alone cannot disambiguate either — reuse the
            # gamma fallback (handles void, decisive, and unknown_delisted).
            return self._resolve_via_gamma(condition_id)
        except Exception as e:
            log.debug(f"Resolution check failed for {condition_id[:20]}...: {e}")
            return None

    def _resolve_via_gamma(self, condition_id: str) -> dict:
        """Gamma fallback when CLOB 404s. 3 attempts with exponential backoff.

        Returns one of:
          {"winning_side": "YES"|"NO"} — Gamma shows decisive resolution (>=0.99)
          {"status": "void"}           — Gamma shows 50-50 / UMA void/cancel
          {"status": "unknown_delisted"} — all 3 attempts inconclusive
        """
        base_url = self.config.gamma_api_host
        last_err = None
        for attempt in range(3):
            if attempt > 0:
                time.sleep(2 ** (attempt - 1))  # 1s, 2s between retries
            try:
                r = self.session.get(
                    f"{base_url}/markets",
                    params={"condition_ids": condition_id, "closed": "true", "limit": 1},
                    timeout=15,
                )
                r.raise_for_status()
                data = r.json()

                market = None
                if isinstance(data, list) and data:
                    market = data[0]
                elif isinstance(data, dict) and data.get("data"):
                    market = data["data"][0]

                if not market:
                    last_err = "empty gamma response"
                    continue

                uma_status = (market.get("umaResolutionStatus") or "").lower()
                if "void" in uma_status or "cancel" in uma_status:
                    log.info(f"Gamma fallback: VOID via uma_status='{uma_status}' for {condition_id[:20]}...")
                    return {"status": "void"}

                outcome_prices_raw = market.get("outcomePrices", "[]")
                outcomes_raw = market.get("outcomes", "[]")
                outcome_prices = (
                    json.loads(outcome_prices_raw)
                    if isinstance(outcome_prices_raw, str) else outcome_prices_raw
                )
                outcomes = (
                    json.loads(outcomes_raw)
                    if isinstance(outcomes_raw, str) else outcomes_raw
                )

                if len(outcome_prices) < 2:
                    last_err = f"short outcomePrices: {outcome_prices}"
                    continue

                try:
                    p0 = float(outcome_prices[0])
                    p1 = float(outcome_prices[1])
                except (TypeError, ValueError):
                    last_err = f"non-numeric outcomePrices: {outcome_prices}"
                    continue

                # 50-50 cash settlement
                if abs(p0 - 0.5) < 0.05 and abs(p1 - 0.5) < 0.05:
                    log.info(f"Gamma fallback: VOID via ~0.5/0.5 prices for {condition_id[:20]}...")
                    return {"status": "void"}

                # Map indices via outcomes labels if available, else assume [YES, NO]
                yes_idx, no_idx = 0, 1
                if outcomes and len(outcomes) >= 2:
                    labels = [str(o).upper() for o in outcomes[:2]]
                    if "YES" in labels and "NO" in labels:
                        yes_idx = labels.index("YES")
                        no_idx = labels.index("NO")

                if float(outcome_prices[yes_idx]) >= 0.99:
                    log.info(f"Gamma fallback: YES wins for {condition_id[:20]}...")
                    return {"winning_side": "YES"}
                if float(outcome_prices[no_idx]) >= 0.99:
                    log.info(f"Gamma fallback: NO wins for {condition_id[:20]}...")
                    return {"winning_side": "NO"}

                last_err = f"closed but no decisive winner: prices={outcome_prices}"
            except requests.RequestException as e:
                last_err = f"http error: {e}"
            except (json.JSONDecodeError, ValueError, KeyError) as e:
                last_err = f"parse error: {e}"

        log.warning(
            f"Gamma fallback exhausted for {condition_id[:20]}... "
            f"({last_err}) — flagging unknown_delisted for manual review"
        )
        return {"status": "unknown_delisted"}

    def get_market_prices(self, token_ids: list[str]) -> dict[str, float]:
        """Fetch current prices for multiple tokens. Returns dict of token_id -> midpoint price."""
        prices = {}
        for tid in token_ids:
            p = self.get_market_price(tid)
            if p is not None and p > 0:
                prices[tid] = p
        return prices

    def get_market_price(self, token_id: str) -> Optional[float]:
        """Fetch current price for a single token from the CLOB API.

        Log levels are tiered so a CLOB outage is distinguishable from a
        legitimately-resolved market on the operator's end:
          - 404                  -> INFO  (resolved/void; expected after settle)
          - 5xx                  -> WARN  (transient CLOB error)
          - network/parse errors -> ERROR (something genuinely broken)
        """
        try:
            url = f"{self.config.clob_host}/midpoint"
            params = {"token_id": token_id}
            resp = self.session.get(url, params=params, timeout=10)
            if resp.status_code == 404:
                log.info(f"Price 404 for {token_id[:20]}... (resolved/void)")
                return None
            if 500 <= resp.status_code < 600:
                log.warning(
                    f"Price {resp.status_code} for {token_id[:20]}... (transient CLOB error)"
                )
                return None
            resp.raise_for_status()
            data = resp.json()
            return float(data.get("mid", 0))
        except requests.RequestException as e:
            log.error(f"Network error fetching price for {token_id[:20]}...: {e}")
            return None
        except (ValueError, KeyError, TypeError) as e:
            log.error(f"Parse error for price {token_id[:20]}...: {e}")
            return None
