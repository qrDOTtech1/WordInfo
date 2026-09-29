"""SONDE DE LATENCE REELLE (Claude 28/09, Steven : "envoie un trade reel pour
voir a quelle vitesse on nous repond que les fonds sont manquants").

Envoie de VRAIS ordres signes aux deux venues et chronometre la reponse. Pour
qu'aucun ordre ne puisse couter plus que quelques centimes, chaque sonde est :
  - un ACHAT de 5 parts a 0.01$ (0.05$ de notionnel max), GTC maker :
    il ne peut pas traverser le carnet ;
  - envoyee seulement sur un token dont le meilleur ask est >= 0.10 (jamais sur
    un cote en train de mourir ou il pourrait etre servi) ;
  - annulee IMMEDIATEMENT si par hasard elle etait acceptee.
Avec des comptes a 0$, la reponse attendue est un refus "fonds insuffisants" :
c'est exactement le temps d'aller-retour de l'execution reelle qu'on mesure.

    venv/Scripts/python -m datalake.latency_probe [N]      (N sondes par venue, defaut 5)

Resultats : table latency_probes de data/marketdata.db + resume a l'ecran.
"""
import json
import sqlite3
import statistics
import sys
import time

import requests

from limitless import config
from limitless.client import BUY, LimitlessClient, LimitlessError

PROBE_PRICE = 0.01
PROBE_SHARES = 5
MIN_ASK_FOR_PROBE = 0.10


def _db():
    from datalake import db_path

    db = sqlite3.connect(str(db_path()), timeout=30)
    db.execute("CREATE TABLE IF NOT EXISTS latency_probes(ts REAL, venue TEXT, kind TEXT, ms REAL, status INTEGER, detail TEXT)")
    return db


def _pm_target():
    """Token Polymarket d'un marche ACTIF (meilleur ask entre 0.10 et 0.90),
    cherche parmi les 15 min puis 5 min de toutes les cryptos."""
    for tf, sec in (("15m", 900), ("5m", 300)):
        t = int(time.time()) // sec * sec
        for coin in ("btc", "eth", "sol", "xrp", "doge", "bnb"):
            g = requests.get("https://gamma-api.polymarket.com/markets",
                             params={"slug": f"{coin}-updown-{tf}-{t}"}, timeout=8).json()
            if not g:
                continue
            for tok in json.loads(g[0]["clobTokenIds"]):
                b = requests.get("https://clob.polymarket.com/book", params={"token_id": tok}, timeout=8).json()
                asks = [float(x["price"]) for x in b.get("asks", [])]
                if asks and MIN_ASK_FOR_PROBE <= min(asks) <= 0.90:
                    return g[0]["slug"], tok, min(asks)
    return None


def probe_polymarket(n, out):
    import os

    from ghost_poly.live import PolyLive

    live = PolyLive(os.environ["PRIVATE_KEY"], os.environ["POLY_FUNDER_ADDRESS"])
    t0 = time.perf_counter()
    cash = live.get_cash_usdc_fast()
    out.append(("pm", "read_cash", (time.perf_counter() - t0) * 1000, 200, f"cash={cash}"))
    for i in range(n):
        tgt = _pm_target()
        if not tgt:
            out.append(("pm", "skip", 0, 0, "aucun token a ask >= 0.10"))
            continue
        slug, tok, ask = tgt
        t0 = time.perf_counter()
        r = live.post_limit_buy(tok, PROBE_PRICE, PROBE_SHARES)
        ms = (time.perf_counter() - t0) * 1000
        if r.get("order_id"):
            live.cancel_order(r["order_id"])  # accepte malgre tout : on retire tout de suite
        detail = (r.get("error") or json.dumps(r.get("raw"))[:160]) + f" | timing={r.get('timing')} | {slug} ask={ask}"
        out.append(("pm", "order_reject" if not r.get("order_id") else "order_ACCEPTED_cancelled", ms,
                    0 if not r.get("order_id") else 1, detail[:400]))
        time.sleep(1.5)


def probe_limitless(n, out):
    c = LimitlessClient(log_fn=lambda m: None)
    t0 = time.perf_counter()
    c.profile(fresh=True)
    out.append(("lm", "auth_profile", (time.perf_counter() - t0) * 1000, 200, "GET /profiles/me"))
    for i in range(n):
        t = int(time.time()) // 900 * 900
        slug = f"btc-up-or-down-15-min-{t}"
        t0 = time.perf_counter()
        ob = c.orderbook(slug)
        out.append(("lm", "orderbook_rest", (time.perf_counter() - t0) * 1000, 200, slug))
        side = "yes" if ob["yes_asks"] and ob["yes_asks"][0][0] >= MIN_ASK_FOR_PROBE else "no"
        best_ask = (ob["yes_asks"] if side == "yes" else ob["no_asks"])
        if not best_ask or best_ask[0][0] < MIN_ASK_FOR_PROBE:
            out.append(("lm", "skip", 0, 0, "aucun cote a ask >= 0.10"))
            continue
        ts_sign = time.perf_counter()
        signed, info = c.build_order(slug, side, BUY, PROBE_PRICE, PROBE_SHARES)
        sign_ms = (time.perf_counter() - ts_sign) * 1000
        body = {"order": signed, "ownerId": c.owner_id(), "orderType": "GTC", "marketSlug": slug,
                "postOnly": True, "timestamp": c.server_now_ms(), "recvWindow": 5000}
        # Envoi DIRECT (hors garde dry-run) : sonde explicite, 0.05$ max, postOnly, annulee si acceptee.
        t0 = time.perf_counter()
        status, detail = 200, ""
        try:
            resp = c._request("POST", "/orders", body, auth=True, retries=0)
            oid = (resp.get("order") or {}).get("id") or resp.get("id")
            if oid:
                c._request("DELETE", f"/orders/{oid}", auth=True, retries=0)
            detail = json.dumps(resp)[:300]
        except LimitlessError as e:
            status, detail = e.status, json.dumps(e.body)[:300]
        ms = (time.perf_counter() - t0) * 1000
        out.append(("lm", "order_reject" if status >= 400 else "order_ACCEPTED_cancelled", ms, status,
                    f"sign={sign_ms:.1f}ms | {detail}"))
        time.sleep(1.5)


def main(n=5):
    out = []
    off = LimitlessClient.clock_offset_ms(max_age_s=0)
    out.append(("sys", "clock_offset", off, 200, f"horloge serveur - PC = {off:+.0f} ms (rtt {LimitlessClient._clock.get('rtt_ms', 0):.0f} ms)"))
    for name, fn in (("Polymarket", probe_polymarket), ("Limitless", probe_limitless)):
        try:
            fn(n, out)
        except Exception as e:
            out.append((name[:2].lower(), "probe_error", 0, 0, str(e)[:300]))
    ts = time.time()
    db = _db()
    db.executemany("INSERT INTO latency_probes VALUES (?,?,?,?,?,?)", [(ts,) + r for r in out])
    db.commit()
    for v in ("sys", "pm", "lm"):
        rows = [r for r in out if r[0] == v]
        for r in rows:
            print(f"{v.upper()} {r[1]:<26} {r[2]:8.1f} ms  status={r[3]}  {r[4][:170]}")
        rej = [r[2] for r in rows if r[1].startswith("order_")]
        if rej:
            print(f"== {v.upper()} ordre->reponse : min {min(rej):.0f} / med {statistics.median(rej):.0f} / max {max(rej):.0f} ms\n")
    return out


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 5)
