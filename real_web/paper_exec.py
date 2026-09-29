"""EXECUTEUR PAPER (Claude 28/09) -- meme API que ghost_poly.live.PolyLive pour
les appels du Steven Engine, mais AUCUN ordre reel : les fills sont simules sur
le VRAI carnet Polymarket avec le handicap "sac de 40 kg" du moteur :

  - achat marche (snipe_buy_market) : pire ask apres latence (>= plancher
    calibre sur les sondes reelles), plafonne au prix cap comme un FAK reel,
    et limite a la profondeur reellement disponible jusqu'au cap ;
  - vente marche (sell) : pire bid apres latence, limitee a la profondeur bid ;
  - ordres LIMITE (post_limit_sell / post_limit_buy) : ne se remplissent que si
    le carnet les TRAVERSE d'au moins 1 tick (un maker reel peut rester en file
    d'attente et ne jamais etre servi au meme prix -> on ne suppose pas le fill) ;
  - position_size : positions paper tenues ici (persistees dans l'etat).

But : que le Steven Engine tourne a l'IDENTIQUE en paper et en reel (meme
detection, memes TP/SL/trailing/DCA), seul l'executeur change.
"""
import itertools
import time

import requests

CLOB_BOOK = "https://clob.polymarket.com/book"
_ids = itertools.count(1)


class PaperExec:
    def __init__(self, trader):
        self.t = trader
        self.holdings = trader.state.setdefault("paper_holdings", {})   # token -> parts
        self.orders = trader.state.setdefault("paper_orders", {})       # id -> ordre limite

    # ── carnet reel ────────────────────────────────────────────────────
    def get_book_sync(self, token_id, attempts=2):
        live = getattr(self.t, "_live", None)
        if live is not None:
            try:
                b = live.get_book_sync(token_id)
                if b:
                    return b
            except Exception:
                pass
        for _ in range(attempts):
            try:
                d = requests.get(CLOB_BOOK, params={"token_id": token_id}, timeout=4).json()
                bids = sorted(((float(x["price"]), float(x["size"])) for x in d.get("bids", [])), key=lambda t: -t[0])
                asks = sorted(((float(x["price"]), float(x["size"])) for x in d.get("asks", [])), key=lambda t: t[0])
                return {"bids": bids, "asks": asks}
            except Exception:
                time.sleep(0.2)
        return None

    # ── marche ─────────────────────────────────────────────────────────
    def snipe_buy_market(self, token_id, cap_price, budget_usd):
        book = self.get_book_sync(token_id)
        if not book or not book.get("asks"):
            return {"filled_shares": 0.0, "error": "paper: carnet sans ask"}
        best = book["asks"][0][0]
        px = self.t._paper_fill_price(token_id, best, "buy")
        if px is None or px > cap_price:
            return {"filled_shares": 0.0, "error": f"paper: prix {px} au-dela du cap {cap_price}"}
        depth = sum(s for p, s in book["asks"] if p <= cap_price)
        shares = round(min(budget_usd / px, depth), 2)
        if shares <= 0:
            return {"filled_shares": 0.0, "error": "paper: profondeur nulle"}
        self.holdings[token_id] = round(self.holdings.get(token_id, 0.0) + shares, 4)
        return {"filled_shares": shares, "avg_cost": px, "paper": True}

    def sell(self, token_id, shares):
        """Vente marche paper -> (parts vendues, prix obtenu)."""
        book = self.get_book_sync(token_id)
        if not book or not book.get("bids"):
            return 0.0, None
        px = self.t._paper_fill_price(token_id, book["bids"][0][0], "sell")
        depth = sum(s for p, s in book["bids"] if p >= px)
        held = self.holdings.get(token_id, 0.0)
        sold = round(min(shares, held if held > 0 else shares, depth), 2)
        if sold <= 0:
            return 0.0, px
        self.holdings[token_id] = round(max(0.0, held - sold), 4)
        return sold, px

    # ── ordres limites ─────────────────────────────────────────────────
    def _post(self, token_id, side, price, size):
        oid = f"paper-{int(time.time())}-{next(_ids)}"
        self.orders[oid] = {"token_id": token_id, "side": side, "price": float(price),
                            "size": float(size), "ts": time.time()}
        return {"success": True, "order_id": oid, "paper": True}

    def post_limit_sell(self, token_id, price, size):
        return self._post(token_id, "SELL", price, size)

    def post_limit_buy(self, token_id, price, size):
        return self._post(token_id, "BUY", price, size)

    def cancel_order(self, order_id):
        return self.orders.pop(order_id, None) is not None

    def _match_resting(self, token_id):
        """Remplit les limites paper que le carnet reel TRAVERSE d'au moins 1 tick."""
        mine = [(oid, o) for oid, o in self.orders.items() if o["token_id"] == token_id]
        if not mine:
            return
        book = self.get_book_sync(token_id)
        if not book:
            return
        bid = book["bids"][0][0] if book.get("bids") else None
        ask = book["asks"][0][0] if book.get("asks") else None
        for oid, o in mine:
            if o["side"] == "SELL" and bid is not None and bid >= o["price"] + 0.01:
                sold = min(o["size"], self.holdings.get(token_id, 0.0))
                self.holdings[token_id] = round(self.holdings.get(token_id, 0.0) - sold, 4)
                self.orders.pop(oid, None)
            elif o["side"] == "BUY" and ask is not None and ask <= o["price"] - 0.01:
                self.holdings[token_id] = round(self.holdings.get(token_id, 0.0) + o["size"], 4)
                self.orders.pop(oid, None)

    def position_size(self, token_id):
        self._match_resting(token_id)
        return round(self.holdings.get(token_id, 0.0), 4)
