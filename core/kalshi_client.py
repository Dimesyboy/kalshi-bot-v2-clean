#!/usr/bin/env python3
"""
core/kalshi_client.py
─────────────────────────────────────────────────────────────────────────────
Single Kalshi API client for kalshi-bot-v2.
Uses RSA signed requests matching the proven pattern from v1.
"""

import base64
import logging
import time
import requests
from typing import Optional
from core.config import config

log = logging.getLogger("kalshi_bot.client")

_kalshi_client = None


def get_client():
    """Return shared KalshiClient instance."""
    global _kalshi_client
    if _kalshi_client is None:
        _kalshi_client = _init_client()
    return _kalshi_client


def _init_client():
    import kalshi_python
    cfg = kalshi_python.Configuration(host=config.KALSHI_BASE)
    try:
        with open(config.KALSHI_KEY_FILE, "r") as f:
            cfg.private_key_pem = f.read()
        cfg.api_key_id = config.KALSHI_KEY_ID
        log.info(f"[Client] Loaded PEM key for {config.KALSHI_KEY_ID[:8]}...")
    except FileNotFoundError:
        log.error(f"[Client] PEM key not found: {config.KALSHI_KEY_FILE}")
    return kalshi_python.KalshiClient(cfg)


def get_portfolio_api():
    import kalshi_python
    return kalshi_python.PortfolioApi(api_client=get_client())


def _signed_get(path: str, params: dict = None) -> dict:
    """RSA-signed GET request — fallback when SDK auth fails."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    with open(config.KALSHI_KEY_FILE, "rb") as f:
        private_key = serialization.load_pem_private_key(f.read(), password=None)

    ts_ms   = str(int(time.time() * 1000))
    msg     = (ts_ms + "GET" + path).encode()
    sig     = private_key.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH), hashes.SHA256())
    sig_b64 = base64.b64encode(sig).decode()

    headers = {
        "KALSHI-ACCESS-KEY":       config.KALSHI_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": sig_b64,
        "KALSHI-ACCESS-TIMESTAMP": ts_ms,
    }
    r = requests.get(
        f"https://api.elections.kalshi.com{path}",
        headers=headers, timeout=8
    )
    r.raise_for_status()
    return r.json()


def get_balance() -> float:
    """Return current balance in dollars."""
    try:
        pa = get_portfolio_api()
        resp = pa.get_balance()
        if resp and hasattr(resp, "balance"):
            return float(resp.balance) / 100.0
    except Exception as e:
        log.debug(f"[Client] SDK balance failed, trying REST: {e}")
    try:
        data = _signed_get("/trade-api/v2/portfolio/balance")
        return float(data.get("balance", 0.0)) / 100.0
    except Exception as e:
        log.warning(f"[Client] Balance fetch failed: {e}")
        return 0.0


def get_market(ticker: str) -> Optional[dict]:
    """Fetch a single market. Returns raw market dict or None."""
    try:
        r = requests.get(
            f"{config.KALSHI_BASE}/markets/{ticker}", timeout=6
        )
        r.raise_for_status()
        return r.json().get("market", {})
    except Exception as e:
        log.debug(f"[Client] Market fetch failed {ticker}: {e}")
        return None


def get_market_price(ticker: str, side: str) -> int:
    """Return current bid price in cents. Returns 0 on failure."""
    m = get_market(ticker)
    if not m:
        return 0
    key = "no_bid_dollars" if side == "no" else "yes_bid_dollars"
    bid = float(m.get(key, 0) or 0)
    return max(1, int(bid * 100))


def get_markets(series_ticker: str, limit: int = 100) -> list:
    """Fetch all open markets for a series ticker."""
    markets = []
    cursor = None
    try:
        while True:
            params = {"series_ticker": series_ticker, "limit": limit, "status": "open"}
            if cursor:
                params["cursor"] = cursor
            r = requests.get(f"{config.KALSHI_BASE}/markets", params=params, timeout=8)
            r.raise_for_status()
            data = r.json()
            batch = data.get("markets", [])
            markets.extend(batch)
            cursor = data.get("cursor")
            if not cursor or len(batch) < limit:
                break
    except Exception as e:
        log.warning(f"[Client] Markets fetch failed {series_ticker}: {e}")
    return markets


def place_order(ticker: str, side: str, price_cents: int,
                contracts: int, client_order_id: str) -> Optional[str]:
    """Place a limit order. Returns order_id on success, None on failure."""
    try:
        pa = get_portfolio_api()
        yes_price = price_cents if side == "yes" else (100 - price_cents)
        order = pa.create_order(
            ticker          = ticker,
            action          = "buy",
            side            = side,
            type            = "limit",
            yes_price       = yes_price,
            count           = contracts,
            client_order_id = client_order_id,
        )
        return order.order.order_id
    except Exception as e:
        log.error(f"[Client] Order placement failed {ticker}: {e}")
        return None


def get_positions_raw() -> list:
    """Get open positions using raw API (SDK get_positions is broken)."""
    try:
        import requests as _req, time as _time, base64 as _b64
        from cryptography.hazmat.primitives import hashes as _h, serialization as _s
        from cryptography.hazmat.primitives.asymmetric import padding as _p
        BASE = "https://api.elections.kalshi.com"
        path = "/trade-api/v2/portfolio/positions"
        ts   = str(int(_time.time() * 1000))
        msg  = (ts + "GET" + path).encode()
        key  = _s.load_pem_private_key(open(config.KALSHI_KEY_FILE,"rb").read(), password=None)
        sig  = key.sign(msg, _p.PSS(mgf=_p.MGF1(_h.SHA256()), salt_length=_p.PSS.MAX_LENGTH), _h.SHA256())
        hdrs = {"KALSHI-ACCESS-KEY": config.KALSHI_KEY_ID,
                "KALSHI-ACCESS-SIGNATURE": _b64.b64encode(sig).decode(),
                "KALSHI-ACCESS-TIMESTAMP": ts}
        r    = _req.get(f"{BASE}{path}", headers=hdrs, params={"limit": "200"}, timeout=10)
        r.raise_for_status()
        data      = r.json()
        positions = data.get('market_positions', [])
        open_pos  = [p for p in positions if float(p.get('position_fp', 0) or 0) != 0]
        return open_pos
    except Exception as e:
        log.warning(f"[Client] get_positions_raw failed: {e}")
        return []


def get_market_exposure() -> float:
    """Total dollar exposure across all open positions."""
    try:
        positions = get_positions_raw()
        return sum(float(p.get('market_exposure_dollars', 0) or 0) for p in positions)
    except Exception:
        return 0.0


def cancel_order(order_id: str) -> bool:
    """Cancel an order. Returns True on success."""
    try:
        pa = get_portfolio_api()
        pa.cancel_order(order_id=order_id)
        return True
    except Exception as e:
        log.warning(f"[Client] Cancel failed {order_id[:8]}: {e}")
        return False


def get_order(order_id: str) -> Optional[object]:
    """Fetch a single order by ID."""
    try:
        pa = get_portfolio_api()
        resp = pa.get_order(order_id=order_id)
        return resp.order
    except Exception as e:
        log.debug(f"[Client] get_order failed {order_id[:8]}: {e}")
        return None


def get_fills(limit: int = 100) -> list:
    """Fetch recent fills."""
    try:
        pa = get_portfolio_api()
        resp = pa.get_fills(limit=limit)
        return resp.fills or []
    except Exception as e:
        log.warning(f"[Client] Fills fetch failed: {e}")
        return []
