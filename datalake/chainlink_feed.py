"""Flux Chainlink temps reel via le RTDS public de Polymarket (Claude 28/09).

wss://ws-live-data.polymarket.com, topic "crypto_prices_chainlink" : ~1 prix
par seconde et par crypto (btc/eth/sol/xrp/doge/bnb/hype...), valeur Chainlink
Data Streams a 18 decimales. Les marches 5 min / 15 min des deux venues se
resolvent sur le TWAP 60 s de ce flux (cf. regle "Chainlink BTC/USD 60-second
TWAP") : on le recalcule ici = moyenne des prix de la minute precedant l'instant.
"""
import json
import threading
import time
from collections import deque

import websocket

RTDS = "wss://ws-live-data.polymarket.com"


class ChainlinkFeed:
    KEEP_S = 900

    def __init__(self, log_fn=None, on_price=None):
        self._log = log_fn or print
        self._on_price = on_price
        self.hist = {}            # coin -> deque[(ts_source, prix)]
        self.last_msg = 0
        self.msgs = 0
        self._lock = threading.Lock()
        self._thread = None

    def _handle(self, raw):
        try:
            d = json.loads(raw)
        except ValueError:
            return
        p = d.get("payload") or {}
        sym, val, ts = p.get("symbol"), p.get("value"), p.get("timestamp")
        if not sym or val is None or not ts:
            return
        coin = sym.split("/")[0].lower()
        t = ts / 1000.0
        with self._lock:
            h = self.hist.setdefault(coin, deque())
            if h and t <= h[-1][0]:
                return  # doublon / hors ordre
            h.append((t, float(val)))
            while h and t - h[0][0] > self.KEEP_S:
                h.popleft()
        self.last_msg = time.time()
        self.msgs += 1
        if self._on_price:
            self._on_price(t, coin, float(val))

    def price(self, coin):
        with self._lock:
            h = self.hist.get(coin)
            return h[-1][1] if h else None

    def twap(self, coin, at=None, window_s=60):
        """Moyenne des prix Chainlink sur ]at-window_s, at]. None si moins de
        45 points (trou de flux) : on ne devine pas un prix de resolution."""
        at = at or time.time()
        with self._lock:
            h = self.hist.get(coin) or ()
            pts = [p for t, p in h if at - window_s < t <= at]
        return sum(pts) / len(pts) if len(pts) >= 45 else None

    def connected(self):
        return time.time() - self.last_msg < 10

    def _run(self):
        while True:
            try:
                ws = websocket.create_connection(RTDS, timeout=20)
                ws.send(json.dumps({"action": "subscribe", "subscriptions": [
                    {"topic": "crypto_prices_chainlink", "type": "*", "filters": ""}]}))
                self._log("🔗 [CHAINLINK] flux temps reel connecte (RTDS Polymarket)")
                last_ping = time.time()
                while True:
                    if time.time() - last_ping > 5:
                        ws.send("PING")
                        last_ping = time.time()
                    msg = ws.recv()
                    if msg and msg != "PONG":
                        self._handle(msg)
            except Exception as e:
                self._log(f"⚠️ [CHAINLINK] flux coupe ({str(e)[:80]}), reconnexion")
                time.sleep(3)

    def start(self):
        if not self._thread:
            self._thread = threading.Thread(target=self._run, daemon=True, name="chainlink-rtds")
            self._thread.start()
