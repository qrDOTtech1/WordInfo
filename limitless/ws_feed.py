"""Flux WebSocket des carnets Limitless (Claude 28/09).

wss://ws.limitless.exchange, namespace /markets (Socket.IO). subscribe_market_prices
{marketSlugs:[...]} REMPLACE l'abonnement precedent -> on renvoie toujours
l'ensemble complet. Le serveur pousse le carnet ENTIER a chaque changement
(coalesce) + un snapshot initial par slug. Pas de limite de debit comme le REST
(le REST a 1 req/s/marche declenche le 429 Cloudflare, cf. doc de reference).
"""
import threading
import time

import socketio

WS_URL = "wss://ws.limitless.exchange"
NS = "/markets"


def normalize_book(bids_raw, asks_raw):
    bids = sorted(((float(x["price"]), float(x["size"]) / 1e6) for x in bids_raw or []), key=lambda t: -t[0])
    asks = sorted(((float(x["price"]), float(x["size"]) / 1e6) for x in asks_raw or []), key=lambda t: t[0])
    return {
        "yes_bids": bids, "yes_asks": asks,
        "no_bids": [(round(1 - p, 6), s) for p, s in asks],
        "no_asks": [(round(1 - p, 6), s) for p, s in bids],
    }


class LimitlessBookFeed:
    def __init__(self, log_fn=None):
        self._log = log_fn or print
        self.books = {}          # slug -> book normalise + ts + version
        self.frames = 0
        self._slugs = []
        self._lock = threading.Lock()
        self.connected = False
        self._sio = socketio.Client(reconnection=True, reconnection_delay=1,
                                    reconnection_delay_max=10, logger=False, engineio_logger=False)
        self._sio.on("connect", self._on_connect, namespace=NS)
        self._sio.on("disconnect", self._on_disconnect, namespace=NS)
        self._sio.on("orderbookUpdate", self._on_book, namespace=NS)
        self._sio.on("exception", lambda d: self._log(f"⚠️ [LMTS-WS] {str(d)[:160]}"), namespace=NS)
        self._thread = None

    def _on_connect(self):
        self.connected = True
        self._log("🔌 [LMTS-WS] connecte au flux carnets Limitless")
        self._subscribe()

    def _on_disconnect(self, *a):
        self.connected = False
        self._log("⚠️ [LMTS-WS] deconnecte (reconnexion auto)")

    def _on_book(self, d):
        slug = d.get("marketSlug")
        ob = d.get("orderbook") or {}
        ver = d.get("version") or 0
        with self._lock:
            cur = self.books.get(slug)
            # version 0 = snapshot de secours DB : seulement si rien de live
            if cur and ver and cur.get("version") and ver < cur["version"]:
                return
            if cur and ver == 0 and cur.get("version"):
                return
            b = normalize_book(ob.get("bids"), ob.get("asks"))
            b.update(ts=time.time(), version=ver)
            self.books[slug] = b
            self.frames += 1

    def _subscribe(self):
        if self.connected:
            try:
                self._sio.emit("subscribe_market_prices", {"marketSlugs": list(self._slugs)}, namespace=NS)
            except Exception as e:
                self._log(f"⚠️ [LMTS-WS] abonnement : {str(e)[:120]}")

    def set_slugs(self, slugs):
        slugs = sorted(set(slugs))
        if slugs != self._slugs:
            self._slugs = slugs
            with self._lock:
                for s in list(self.books):
                    if s not in slugs:
                        self.books.pop(s, None)
            self._subscribe()

    def book(self, slug):
        with self._lock:
            return self.books.get(slug)

    def _run(self):
        while True:
            try:
                self._sio.connect(WS_URL, namespaces=[NS], transports=["websocket"], wait_timeout=15)
                self._sio.wait()
            except Exception as e:
                self.connected = False
                self._log(f"⚠️ [LMTS-WS] connexion : {str(e)[:120]}")
            time.sleep(5)

    def start(self):
        if not self._thread:
            self._thread = threading.Thread(target=self._run, daemon=True, name="limitless-ws")
            self._thread.start()
