# Kalshi API Field Reference
> Living document — updated as new discoveries are made.
> These are hard-won findings from live trading, not just API docs.

---

## Market Object Fields

Fetched via: `GET /trade-api/v2/markets?series_ticker=KXNBAPTS&limit=200&status=open`

| Field | Type | Example | Notes |
|-------|------|---------|-------|
| `ticker` | string | `KXNBAPTS-26MAR31PHXORL-PHXDBOOKER1-25` | Format: SERIES-DATEAWAYTEAMHOMETEAM-TEAMPLAYERCODE-THRESHOLD |
| `series_ticker` | string | `KXNBAPTS` | Series: KXNBAPTS/REB/AST/3PT/STL/BLK/GAME |
| `event_ticker` | string | `KXNBAPTS-26MAR31PHXORL` | Game-level grouping, needed for RFQ leg submission |
| `title` | string | `Devin Booker: 25+ points` | Human readable |
| `yes_sub_title` | string | `Devin Booker: 25+` | Same content as title usually |
| `no_sub_title` | string | `Devin Booker: 25+` | ⚠️ SAME as yes_sub_title on combo markets — NO does NOT mean opposite legs |
| `status` | string | `active` | active / closed / finalized |
| `result` | string | `yes` / `no` / `` | Empty until settled |
| `market_type` | string | `binary` | Always binary for props |

### Pricing Fields

| Field | Type | Example | Notes |
|-------|------|---------|-------|
| `yes_bid_dollars` | float | `0.39` | What buyers will pay for YES. Market's implied probability. |
| `yes_ask_dollars` | float | `0.63` | What market maker charges for YES |
| `no_bid_dollars` | float | `0.37` | What buyers will pay for NO |
| `no_ask_dollars` | float | `0.61` | What market maker charges for NO |
| `last_price_dollars` | float | `0.63` | Last traded price |
| `previous_yes_bid_dollars` | float | `0.00` | Previous session bid |

### Liquidity Fields ⭐ KEY FOR RFQ SUCCESS

| Field | Type | Example | Notes |
|-------|------|---------|-------|
| `yes_ask_size_fp` | float | `1098` | **Most important.** Contracts market maker will sell at ask. High = active MM = RFQ likely to be quoted. Median tonight ~132. Top markets >2000. |
| `yes_bid_size_fp` | float | `35` | Contracts available to sell at bid. Usually lower than ask side. |
| `open_interest_fp` | float | `199` | Total contracts held by all traders. High OI = real money in market. |
| `volume_24h_fp` | float | `199` | Contracts traded today. High volume confirms active market. |
| `volume_fp` | float | `199` | All-time volume |
| `liquidity_dollars` | float | `0.0` | ⚠️ Often misleading/zero even on liquid markets. Use ask_size instead. |

### Strike / Threshold Fields

| Field | Type | Example | Notes |
|-------|------|---------|-------|
| `floor_strike` | float | `24.5` | **Actual threshold.** Ticker shows 25 but real strike is 24.5. Player needs 25+ to settle YES. |
| `strike_type` | string | `structured` | structured = uses custom_strike player data |
| `custom_strike` | dict | `{basketball_player: uuid, basketball_team: uuid}` | Player and team UUIDs in Kalshi's system |
| `price_level_structure` | string | `linear_cent` | Prices in cents |
| `tick_size` | int | `1` | Minimum price increment (1 cent) |

### Timing Fields

| Field | Type | Example | Notes |
|-------|------|---------|-------|
| `open_time` | datetime | `2026-03-31T06:06:00Z` | When market opened for trading |
| `close_time` | datetime | `2026-04-14T23:00:00Z` | Final expiration (safety net) |
| `expected_expiration_time` | datetime | `2026-04-01T02:00:00Z` | **When game ends / market settles** |
| `can_close_early` | bool | `True` | Market closes when prop hits threshold during game |
| `early_close_condition` | string | see rules | Closes early if event occurs |
| `settlement_timer_seconds` | int | `300` | 5 min delay after game before settlement |

### Rules Fields ⭐ CRITICAL

| Field | Example | Notes |
|-------|---------|-------|
| `rules_primary` | `If Devin Booker records 25+ Points...resolves to Yes` | Main settlement condition |
| `rules_secondary` | `If a player is active but never takes the court, the market settles to the last fair market price before game start` | **DNP RULE** — Player DNP does NOT resolve to 0/1. Settles at last traded price. Critical for combo strategy. |

---

## Series Tickers

| Series | Stat | Notes |
|--------|------|-------|
| `KXNBAPTS` | Points | Highest volume, most liquid |
| `KXNBAREB` | Rebounds | |
| `KXNBAAST` | Assists | |
| `KXNBA3PT` | 3-Pointers | |
| `KXNBASTL` | Steals | Lower volume |
| `KXNBABLK` | Blocks | Lower volume |
| `KXNBAGAME` | Game lines | Win/loss — rarely listed |
| `KXMVESPORTSMULTIGAMEEXTENDED-R` | Combo template | Used for RFQ submission |

---

## RFQ / Combo System

### How Combos Work
- YES on combo = ALL legs must hit → you win
- NO on combo = ANY leg misses → you win
- `yes_sub_title` and `no_sub_title` show the SAME legs — NO is just the inverse outcome

### RFQ Flow
1. POST `/trade-api/v2/multivariate_event_collections/KXMVESPORTSMULTIGAMEEXTENDED-R` → creates dynamic market ticker
2. POST `/trade-api/v2/communications/rfqs` with `target_cost_dollars` → sends request to market makers
3. GET `/trade-api/v2/communications/quotes?rfq_id=X` → poll for responses
4. PUT `/trade-api/v2/communications/quotes/{id}/accept` with `{"accepted_side": "yes"|"no"}` → accept

### Quote Fields

| Field | Notes |
|-------|-------|
| `yes_bid_dollars` | Price market maker charges for YES (rare — usually NO-only) |
| `yes_contracts_fp` | Contracts offered YES side — **MUST BE PRESENT to accept either side** |
| `no_bid_dollars` | Price market maker will pay for NO (most common) |
| `no_contracts_fp` | Contracts offered NO side |
| `rfq_target_cost_dollars` | Dollar amount we requested to spend |

### Critical Discoveries
- **Market makers quote NO-only** on most combos. `yes_bid_dollars` = 0, only `no_bid_dollars` populated.
- **`yes_contracts_fp` must be non-null** to accept either side. NO-only quotes (`yes_contracts=None`) fail with `invalid_parameters` on accept.
- **Market makers stop quoting above ~12-14 legs.**
- **RFQs only get quoted pre-game** (~1-2 hours before tip). Mid-day RFQs usually timeout.
- **`target_cost_dollars`** = what you want to spend (not what you want to win).
- **`effective_yes`** = `1 - no_bid` when market maker quotes NO side only.

### Sell NO Mechanic
When market maker bids NO at 0.90:
- You SELL NO = receive 0.90 per contract upfront
- If YES wins (all legs hit): NO worthless, you keep 0.90
- If NO wins (any leg misses): you owe 0.10 per contract
- This is equivalent to buying YES at 10¢

### Combo Settlement — DNP Edge Case
If a player in a combo DNPs:
- Their leg settles at last traded price (e.g. 0.53)
- Combo payout multiplied by 0.53 instead of 1.0
- NOT voided like traditional sportsbooks
- Monitor injury reports to avoid combo legs with questionable players

---

## Authentication

- **Method:** RSA-PSS with SHA-256
- **Signature input:** `timestamp_ms + HTTP_METHOD + path` (NO query string in path)
- **Headers:** `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-SIGNATURE`, `KALSHI-ACCESS-TIMESTAMP`
- **Key file:** RSA private key PEM
- ⚠️ Query params must be passed separately — including them in the signed path causes 401

---

## Known Quirks

| Issue | Detail |
|-------|--------|
| SDK `get_positions()` broken | Returns empty. Use raw API: `GET /trade-api/v2/portfolio/positions` → `market_positions` field |
| SDK fill fields null | `count`, `remaining_count` always None. Use `GET /portfolio/fills?order_id=X` instead |
| `liquidity_dollars` misleading | Often 0 even on liquid markets. Use `yes_ask_size_fp` instead |
| `no_sub_title` same as `yes_sub_title` | On combo markets both show YES legs. NO = inverse outcome, not different legs |
| 429 rate limits | Hitting `/markets` endpoints for 7+ series per cycle causes frequent 429s. Normal, retry next cycle |
| NBA.com blocked | nba_api library blocked on VPS IPs (DigitalOcean). Use ESPN public APIs instead |
| `.git-credentials` auto-staged | Git tracks root directory. Always `git rm --cached` before committing |

---

## RFQ Quote Mechanics — Confirmed via Live Testing

### yes_contracts_fp Population
- **yes_c > 0 is rare** — appears in <10% of quotes
- Appears unpredictably, likely time-of-day dependent (closer to tip = more likely)
- Cannot be forced by leg selection or leg count
- When yes_c=0, `accept YES` returns 400 invalid_parameters
- **NOT a blocker** — NO-only quotes are still profitable via accept NO

### Accept Side → Position Side (CONFIRMED)
| Accept Side | yes_c required | position_fp | Holding | Win when |
|-------------|---------------|-------------|---------|----------|
| `no` | No | Positive | YES | All legs hit |
| `yes` | YES (yes_c > 0) | Negative | NO | Any leg misses |

### Leg Count → Quote Type
- 2-4 legs: usually NO-only (yes_c=0), sometimes no quote at all
- 6+ legs: reliable NO-only quote in ~1s if legs have ask_size >= 500
- yes_c populated: unpredictable, not correlated with leg count

### ask_size Patterns
- High ask_size + low bid_size = market maker selling YES aggressively (one-sided)
- Balanced ask/bid (75/75) = Kalshi placeholder minimum, not real liquidity
- Real two-sided activity = high OI + high volume (e.g. Banchero 20+ bid_size=2101)
- **For reliable RFQ quotes: use ask_size >= 500**

### Optimal RFQ Strategy (Confirmed Working)
1. Fetch all tonight markets, filter ask_size >= 500
2. Score with model, pick 6-10 legs with best edge
3. Submit RFQ with `target_cost_dollars: 5.00`
4. Accept NO → hold YES position
5. Win if all legs hit (positive EV if model confidence > market implied prob)

### Time of Day
- Daytime (6+ hrs before tip): quotes available but NO-only, yes_c=0
- Pre-game (T-2hr): test whether yes_c starts appearing
- Live game: markets close as props trigger

---

## BUYING NO — Complete Research Log (Will Not Fail)

### Status: IN PROGRESS — pathway confirmed, timing is the blocker

### What We Know For Certain
1. **Combo markets ARE full binary markets** — each has its own orderbook, YES and NO sides
2. **Our 38 confirmed NO fills are real** — `taker=no` in trades history, `position_fp` negative
3. **The fills happened at 01:04-05:17 UTC** — ~6-10 hours before tip (8pm ET)
4. **Mechanism: market maker posted YES limit orders, we hit from NO side**
5. **Trade data confirms:** `taker=no, yes_price=0.513, no_price=0.487, count=5`
6. **active_quoters field** in multivariate_event_collections shows when MMs are live

### The active_quoters Signal
- `GET /trade-api/v2/multivariate_event_collections` returns collections with `active_quoters` per event
- When `active_quoters: []` → market makers offline → orderbooks empty → NO fills impossible
- When `active_quoters: [uuid]` → market maker online → submit RFQ → YES orders appear → hit NO
- Active window appears to be ~T-2hr to tip time for NBA props

### The Exact Flow That Produced NO Fills
1. combo_scheduler submitted RFQs every cycle
2. Market maker received RFQ, posted YES limit order on combo market orderbook
3. Bot placed NO buy limit order at YES ask price (1 - yes_bid)
4. Orders matched → we hold NO (position_fp negative) → win if any leg misses
5. Orderbook clears after fill — looks empty in hindsight

### Why It Doesn't Work Right Now
- active_quoters = [] on all NBA collections (games 6+ hours away)
- RFQ gets a bilateral quote response but no orderbook posting
- Direct limit NO orders rest unfilled (no counterparty)

### The Path Forward
- Monitor `active_quoters` field on NBA collections
- When non-empty: submit RFQ → immediately check orderbook for YES orders → place NO buy
- Target: 2-3 leg combos where YES is 50-70¢ (NO costs 30-50¢, pays 2-3x)
- Window: T-120min to T-30min before tip (~midnight-2am UTC for 8pm ET games)
- Optimal legs: high yes_ask_size (500+) + model confidence + smart money YES buying

### Why More Legs ≠ Better NO Payout
- Market maker prices combo NO at fair value regardless of leg count
- 10-leg NO at 98¢ costs 2¢ → win 2¢ profit = bad
- 2-leg NO at 50¢ costs 50¢ → win 100¢ = 2x = decent
- Sweet spot: 2-4 legs where YES probability is 40-70% per leg
- Combined YES prob = 0.55^2 = 30% → NO = 70% → NO costs ~30¢ → win $1 = 3.3x

### Scheduled Implementation
- Add active_quoters polling to combo_scheduler
- When quoters detected: run NO buyer scan
- Place NO limit orders at YES ask price (from orderbook after RFQ activation)
- Cancel unfilled orders after 60 seconds
- Target: 2-3 NBA same-game combos per slate, $1-2 each

---

## Multivariate Collection Types (Confirmed March 31, 2026)

Three collection series available for combo markets:

### 1. KXMVENBASINGLEGAME-{date}{GAME}
- Single game only — e.g. KXMVENBASINGLEGAME-26MAR31CLELAL
- Per-game market makers, potentially tighter quotes
- Collection ticker format: KXMVENBASINGLEGAME-26MAR31CLELAL
- Best for: same-game combos, higher liquidity per game

### 2. KXMVESPORTSMULTIGAMEEXTENDED-R
- Cross-game, cross-sport extended
- What we've been using for all combos
- Supports legs from multiple games in same slate
- Best for: multi-game parlays, bigger leg pools

### 3. KXMVECROSSCATEGORY-R
- Cross-category (sports + non-sports)
- Untested — may support combining NBA with other markets

### active_quoters Signal
- Check via: GET /trade-api/v2/multivariate_event_collections?limit=100
- Filter for game date in associated_event_tickers
- active_quoters populated = MM online = RFQ will get yes_c
- Always 0 pre-game (>2hr before tip)
- Populates ~T-60min to T-15min before tip
- Returns to 0 once game goes live

### Single Game Collection Flow
POST /trade-api/v2/multivariate_event_collections/KXMVENBASINGLEGAME-26MAR31CLELAL
with selected_markets from that game only

---

## Strategy Performance (Confirmed via portfolio_settlements — April 1, 2026)

### Combo Results (EXTENDED markets only)
| Strategy | W | L | Win% | Avg Win | Avg Cost | Net PnL |
|----------|---|---|------|---------|----------|---------|
| NO hold  | 25 | 8 | 76% | $5.83 | $4.29 | +$51.27 |
| YES hold | 1 | 44 | 2% | $25.00 | $6.74 | -$317.89 |

### Decision
- **YES holds DISABLED** — 2% win rate, -$318 total loss
- **NO holds ONLY** — 76% win rate, positive EV per trade
- Expected value per NO trade: 0.76×$5.83 - 0.24×$4.29 = +$3.40
- Target: 10 NO holds per night = ~$34 expected profit

### NO Hold Mechanics
- Accept YES (yes_c > 0) → position_fp negative → holding NO
- Win when ANY leg fails
- Best window: T-60min to T-15min before tip (yes_c populates)
- yes_c = 0 during live games — cannot place new NO holds in-game
- Optimal: 6-10 legs, mid-range YES (40-72c), multiple games
