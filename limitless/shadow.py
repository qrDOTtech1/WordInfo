"""MESURE EN MODE OMBRE de l'edge Limitless <-> Polymarket (Claude 28/09, v2).

Aucun ordre, aucun centime. Toutes les paires verifiees (5 min, 15 min,
horaire, journalier...) sont suivies EN MEME TEMPS, evaluees chaque seconde :
  - carnets Limitless par WebSocket (pousse a chaque changement),
  - carnets Polymarket par un seul appel groupe /books par seconde.

Par FENETRE (= un marche Limitless, ex. BTC 15 min 12:30->12:45) on garde :
  - meilleur ARB NET TAKER/TAKER vu : edge/part, taille executable en balayant
    la profondeur des DEUX carnets, $ capturable, prix des deux jambes, temps
    restant, duree pendant laquelle l'arb a existe ;
  - l'arb "le plus proche" meme negatif (cout combine minimum, frais inclus) ;
  - spreads moyens Limitless vs Polymarket, trades Limitless reels, fills
    simules de la strategie market-making couverte.
A la cloture, une ligne resume la fenetre dans le log + data/limitless_windows.jsonl.

Hypotheses PESSIMISTES (regle anti-Goodhart de Steven) :
  - un arb n'est compte que s'il PERSISTE >= 2 evaluations (sinon non executable
    avec le delai taker Limitless de 250-500 ms) ;
  - frais officiels des deux venues, au bareme de NOTRE profil (feeRateBps) ;
  - market making : on se place derriere toute la file au meme prix, et la
    couverture Polymarket est payee au prix vu HEDGE_LATENCY_S plus tard.

Fichiers :
  data/limitless_windows.jsonl        une ligne par fenetre close
  data/limitless_ticks.jsonl          chaque changement de haut de carnet (max data)
  data/limitless_shadow.jsonl         evenements (arb, fills)
  data/limitless_shadow_summary.json  agregats (GET /api/limitless/shadow)
"""
import json
import math
import os
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

from limitless import config
from limitless.client import LimitlessClient
from limitless.pairs import PairFinder
from limitless.ws_feed import LimitlessBookFeed

PM_BOOKS = "https://clob.polymarket.com/books"

# HANDICAP "SAC DE 40 KG" (Steven 28/09), identique au paper du moteur :
# 6% de la mise preleves sur chaque gain. La latence (0.3-1 s) est deja
# couverte : un arb n'est compte que s'il est encore la a l'evaluation
# suivante (>= 1 s plus tard), et la couverture MM est payee 3 s apres.
HANDICAP_WIN_FEE_PCT = 0.06
# Marge minimale APRES handicap pour declencher un trade (Claude 28/09) : les
# signaux a +0.003..+0.006 passaient negatifs avec la latence (1 perte / 6).
MIN_EDGE_AFTER_HANDICAP = 0.01
# FILTRE "POLYMARKET EN MOUVEMENT" (backtest Claude 28/09 sur 2075 signaux) : si le
# prix Polymarket de la jambe a bouge (> Limitless + 1c) dans les 3 s avant le
# signal, Polymarket continue souvent sur sa lancee pendant la latence -> 15% de
# jambes nues ; rentable seulement a partir de +0,03/part apres handicap
# (0,01-0,03 : +0,0006/part en moyenne = du risque pour rien).
PM_MOVING_LOOKBACK_S = 3.0
PM_MOVING_MIN_EDGE_H = 0.03

# Bareme taker Limitless publie (user-guide/fees) pour le palier de base
# (300 bps). Pas de formule fermee -> interpolation. % du notionnel.
_LM_BUY_FEE = [(0.50, 3.00), (0.55, 2.52), (0.60, 2.13), (0.65, 1.80), (0.70, 1.51),
               (0.75, 1.26), (0.80, 1.05), (0.85, 0.85), (0.90, 0.68), (0.95, 0.53),
               (0.99, 0.42), (0.999, 0.40)]


def lm_buy_fee_pct(p, fee_bps=300):
    if p <= 0.50:
        base = 3.00
    else:
        base = 0.40
        for (x0, y0), (x1, y1) in zip(_LM_BUY_FEE, _LM_BUY_FEE[1:]):
            if p <= x1:
                base = y0 + (y1 - y0) * (p - x0) / (x1 - x0)
                break
    return base * fee_bps / 300.0


def pm_fee_per_share(p, rate=0.07, exp=1.0):
    """Frais taker Polymarket officiels : rate * (p*(1-p))^exp par part."""
    return rate * (p * (1 - p)) ** exp if 0 < p < 1 else 0.0


def _iso(ts_str):
    return datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()


def _hm(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M")


def _tleft(s):
    s = max(0, int(s))
    if s >= 3600:
        return f"T-{s // 3600}h{(s % 3600) // 60:02d}"
    return f"T-{s // 60}:{s % 60:02d}"


def _depth_price(levels, qty):
    got, cost = 0.0, 0.0
    for p, s in levels:
        take = min(s, qty - got)
        cost += take * p
        got += take
        if got >= qty - 1e-9:
            return cost / qty
    return None


def walk_arb(lm_asks, pm_asks, lm_fee_fn, pm_fee_fn, max_shares=10_000):
    """Balaye les deux carnets d'asks tant que la part marginale rapporte :
    1 - (ask_lm + frais_lm + ask_pm + frais_pm) > 0. Renvoie taille, profit,
    edge moyen, prix moyens, et la meilleure part (edge au meilleur niveau)."""
    i = j = 0
    rl = lm_asks[0][1] if lm_asks else 0
    rp = pm_asks[0][1] if pm_asks else 0
    size = profit = cost_lm = cost_pm = 0.0
    top_edge = None
    while i < len(lm_asks) and j < len(pm_asks) and size < max_shares:
        pl, pp = lm_asks[i][0], pm_asks[j][0]
        unit = 1 - (pl + lm_fee_fn(pl) + pp + pm_fee_fn(pp))
        if top_edge is None:
            top_edge = unit
        if unit <= 0:
            break
        q = min(rl, rp, max_shares - size)
        size += q
        profit += q * unit
        cost_lm += q * pl
        cost_pm += q * pp
        rl -= q
        rp -= q
        if rl <= 1e-9:
            i += 1
            rl = lm_asks[i][1] if i < len(lm_asks) else 0
        if rp <= 1e-9:
            j += 1
            rp = pm_asks[j][1] if j < len(pm_asks) else 0
    return {"size": size, "profit": profit, "top_edge": top_edge,
            "avg_lm": cost_lm / size if size else None, "avg_pm": cost_pm / size if size else None}


class Window:
    """Etat d'une fenetre (un marche Limitless) du debut a la cloture."""

    def __init__(self, p):
        self.p = p
        slug = p["lm_slug"]
        self.sh = None            # shadow proprietaire (carnets), pose a la creation
        if p.get("bucket") == "event":   # marche evenement (limitless/generic.py)
            self.coin, self.bucket, self.label = p["coin"], "event", p["label"]
        else:
            self.coin = slug.split("-up-or-down")[0].upper().replace("SOLANA", "SOL").replace("DOGECOIN", "DOGE")
            self.bucket = next((k for k in ("15-min", "5-min", "hourly", "daily", "weekly") if k in slug), "autre")  # 15 avant 5 !
            self.label = f"{self.coin} {self.bucket.replace('-min', 'm')}"
        self.first_obs = None
        self.n_obs = 0
        self.best = None          # meilleur arb net persistant
        self.closest = None       # cout combine minimum (meme si > 1)
        self.arb_seconds = 0.0
        self.arb_streak = {}      # dir -> nb d'evaluations consecutives positives
        self.lm_spread_sum = self.pm_spread_sum = 0.0
        self.spread_n = 0
        self.lm_trades = 0
        self.missing = 0
        self.lm_volume = 0.0
        self.mm_fills = 0
        self.mm_pnl = 0.0
        self.mm_pnl_h = 0.0
        self.last_top = None
        self.last_mid = None
        self.last_logged_edge = -1.0
        self.last_log_ts = 0.0

    def t_left(self, now):
        return self.p["end_ts"] - now

    def record(self):
        return {
            "slug": self.p["lm_slug"], "pm_slug": self.p["pm_slug"], "label": self.label,
            "coin": self.coin, "bucket": self.bucket, "start_ts": self.p.get("start_ts"),
            "end_ts": self.p["end_ts"], "first_obs": self.first_obs, "n_obs": self.n_obs,
            "best_arb": self.best, "closest": self.closest,
            "arb_seconds": round(self.arb_seconds, 1),
            "lm_spread_avg": round(self.lm_spread_sum / self.spread_n, 4) if self.spread_n else None,
            "pm_spread_avg": round(self.pm_spread_sum / self.spread_n, 4) if self.spread_n else None,
            "lm_trades": self.lm_trades, "lm_volume_usd": round(self.lm_volume, 2), "missing_obs": self.missing,
            "mm_fills": self.mm_fills, "mm_pnl": round(self.mm_pnl, 4), "mm_pnl_h": round(self.mm_pnl_h, 4),
            "last_pm_up_mid": self.last_mid,
        }


class CrossVenueShadow:
    MARGIN = 0.01            # marge des cotations MM sous le carnet PM (plancher doc ref)
    QUOTE_SIZE = 10.0
    HEDGE_LATENCY_S = 3.0
    EVAL_PERIOD_S = 1.0
    TRADES_PERIOD_S = 20.0   # /events est cache 30s cote CDN
    PAIRS_PERIOD_S = 30.0
    HIST_S = 900
    CLOSE_GRACE_S = 25  # le thread lent relit les trades (/events cache 30 s) avant cloture
    PERSIST_EVALS = 2

    def __init__(self, log_fn=None, pm_source=None, mode="crypto"):
        self._log = log_fn or print
        # mode "events" (Claude 28/09, Steven "elargir au sport ou autre") : marches
        # isPolyArbitrage (sport, politique, eco, prix d'actifs) -- appariement toutes
        # les 10 min, eval toutes les 2 s, carnets PM lus en parallele, pas de
        # releve des trades LM (le MM ombre ne concerne que les cryptos).
        self.mode = mode
        self.tag = "EVT" if mode == "events" else "LMTS"
        if mode == "events":
            self.PAIRS_PERIOD_S = 600.0
            self.EVAL_PERIOD_S = 2.0
        else:
            # DETECTION RAPIDE (Claude 28/09) : 45% des signaux "Limitless non servi"
            # -> on detectait 1-2 s trop tard (REST 1 Hz + 2 evals d'1 s). Carnets
            # Polymarket pousses par WebSocket + Limitless WS -> eval 4 Hz en memoire.
            # ANNULE le 28/09 21:08 : 0 paire / 3 jambes nues en 8 min (contre 189/7
            # avant) -> retour 1 Hz + carnets REST tant que le WS n'est pas valide.
            self.EVAL_PERIOD_S = 1.0
        self._pm_ws = None
        if mode != "events" and os.environ.get("LMTS_PM_WS") == "1":
            try:
                from real_web.ws_feed import get_feed
                self._pm_ws = get_feed()
            except Exception as e:
                self._log(f"⚠️ [LMTS-SHADOW] flux WS Polymarket indisponible ({str(e)[:80]}) -> REST")
        self.pm_ws_hits = self.pm_ws_miss = 0
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix=f"{self.tag.lower()}-books")
        # pm_source = MarketRecorder du datalake : on reutilise SES carnets PM
        # (deja lus chaque seconde) au lieu de refaire les memes requetes.
        self.pm_source = pm_source
        self.on_arb = None  # branche par le serveur sur CrossArbExecutor.on_signal
        self._http = requests.Session()
        self.c = LimitlessClient(log_fn=self._log)
        if mode == "events":
            from limitless.generic import GenericPairFinder
            self.pf = GenericPairFinder(self.c, log_fn=self._log)
        else:
            self.pf = PairFinder(self.c, log_fn=self._log)
        self.feed = LimitlessBookFeed(log_fn=self._log)
        self.hist = defaultdict(deque)
        self.windows = {}
        self._closed_slugs = set()  # evite de rouvrir une fenetre deja close
        self._pairs_dirty = False
        self.closed = deque(maxlen=500)
        self.seen = set()
        self.last_trades = {}
        self.last_pairs = 0
        self.last_rest = {}
        self.fee_bps = 300
        self.stats = defaultdict(lambda: defaultdict(float))
        self.started = time.time()
        self._stop = threading.Event()
        self._thread = None
        d = config.DATA_DIR
        d.mkdir(parents=True, exist_ok=True)
        sfx = "_events" if mode == "events" else ""
        self.events_path = d / f"limitless_shadow{sfx}.jsonl"
        self.windows_path = d / f"limitless_windows{sfx}.jsonl"
        self.ticks_path = d / f"limitless_ticks{sfx}.jsonl"
        self.summary_path = d / f"limitless_shadow_summary{sfx}.json"
        self._load_closed()

    # ── persistance ────────────────────────────────────────────────────
    def _load_closed(self):
        try:
            for ln in self.windows_path.read_text(encoding="utf-8").splitlines()[-500:]:
                if ln.strip():
                    self.closed.append(json.loads(ln))
        except (OSError, ValueError):
            pass

    @staticmethod
    def _append(path, rec):
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, separators=(",", ":")) + "\n")
        except OSError:
            pass

    def _emit(self, kind, **kw):
        self._append(self.events_path, {"ts": round(time.time(), 3), "kind": kind, **kw})

    # ── sources ────────────────────────────────────────────────────────
    def _pm_books_chunk(self, chunk):
        r = self._http.post(PM_BOOKS, json=[{"token_id": t} for t in chunk], timeout=8)
        r.raise_for_status()
        out = {}
        for b in r.json():
            bids = sorted(((float(x["price"]), float(x["size"])) for x in b.get("bids", [])), key=lambda t: -t[0])
            asks = sorted(((float(x["price"]), float(x["size"])) for x in b.get("asks", [])), key=lambda t: t[0])
            out[b["asset_id"]] = {"bids": bids, "asks": asks}
        return out

    def _pm_books(self, tokens):
        chunks = [tokens[i:i + 50] for i in range(0, len(tokens), 50)]
        if len(chunks) <= 1:
            return self._pm_books_chunk(chunks[0]) if chunks else {}
        out = {}   # plusieurs lots (marches evenements : ~250 paires) -> en parallele
        for part in self._pool.map(self._pm_books_chunk, chunks):
            out.update(part)
        return out

    def _lm_book(self, slug, now):
        b = self.feed.book(slug) if self.feed.connected else None  # WS coupe -> carnet fige, on ne s'y fie pas
        if b is not None:
            return b
        # WS pas encore servi / coupe : REST de secours, au plus 1 fois / 10 s / marche
        if now - self.last_rest.get(slug, 0) < 10:
            return None
        self.last_rest[slug] = now
        try:
            return self.c.orderbook(slug)
        except Exception:
            return None

    def _snap_at(self, slug, ts, after=False):
        h = list(self.hist.get(slug) or ())  # copie : lu par le thread lent pendant que la boucle 1 Hz ecrit
        if after:
            for t, s in h:
                if t >= ts:
                    return t, s
            return None
        best = None
        for t, s in h:
            if t <= ts:
                best = (t, s)
            else:
                break
        return best

    # ── evaluation ─────────────────────────────────────────────────────
    def _evaluate(self, w, snap, now):
        p, lm, up, dn = w.p, snap["lm"], snap["up"], snap["dn"]
        w.n_obs += 1
        w.first_obs = w.first_obs or now
        tl = w.t_left(now)
        lm_fee = (lambda px: px * lm_buy_fee_pct(px, self.fee_bps) / 100) if p["lm_fee"] else (lambda px: 0.0)
        pm_fee = lambda px: pm_fee_per_share(px, p["pm_fee_rate"], p["pm_fee_exp"])  # noqa: E731
        if lm["yes_bids"] and lm["yes_asks"] and up["bids"] and up["asks"]:
            w.lm_spread_sum += lm["yes_asks"][0][0] - lm["yes_bids"][0][0]
            w.pm_spread_sum += up["asks"][0][0] - up["bids"][0][0]
            w.spread_n += 1
        ub = up["bids"][0][0] if up["bids"] else (1 - dn["asks"][0][0] if dn["asks"] else None)
        ua = up["asks"][0][0] if up["asks"] else (1 - dn["bids"][0][0] if dn["bids"] else None)
        if ub is not None and ua is not None:
            w.last_mid = round((ub + ua) / 2, 4)
        any_pos = False
        for name, lm_side, pm_side, lm_asks, pm_asks in (
                ("LM_YES+PM_DOWN", "YES", "DOWN", lm["yes_asks"], dn["asks"]),
                ("LM_NO+PM_UP", "NO", "UP", lm["no_asks"], up["asks"])):
            if not lm_asks or not pm_asks:
                w.arb_streak[name] = 0
                continue
            a_l, a_p = lm_asks[0][0], pm_asks[0][0]
            comb = a_l + lm_fee(a_l) + a_p + pm_fee(a_p)
            if w.closest is None or comb < w.closest["cost"]:
                w.closest = {"cost": round(comb, 4), "dir": name, "lm_px": a_l, "pm_px": a_p,
                             "t_left_s": int(tl), "ts": round(now, 1)}
            r = walk_arb(lm_asks, pm_asks, lm_fee, pm_fee)
            if r["profit"] > 0 and r["size"] >= 1:
                w.arb_streak[name] = w.arb_streak.get(name, 0) + 1
                any_pos = True
                if w.arb_streak[name] >= self.PERSIST_EVALS:
                    cand = {"dir": name, "lm_side": lm_side, "pm_side": pm_side,
                            "lm_px": round(r["avg_lm"], 4), "pm_px": round(r["avg_pm"], 4),
                            "lm_top": a_l, "pm_top": a_p, "size": round(r["size"], 2),
                            "edge": round(r["profit"] / r["size"], 4), "top_edge": round(r["top_edge"], 4),
                            "usd": round(r["profit"], 4),
                            "usd_h": round(r["profit"] - HANDICAP_WIN_FEE_PCT * r["size"] * (r["avg_lm"] + r["avg_pm"]), 4),
                            "t_left_s": int(tl), "ts": round(now, 1),
                            "pm_min_ok": r["size"] >= p["pm_min_order"]}
                    # POSITIF = positif APRES le handicap paper 40 kg (6% de la mise) :
                    # les 1ers trades ont montre qu'un arb a 0-1.5c/part perd ~1$ une
                    # fois handicape et bloque le capital des gros ecarts.
                    cand["edge_h"] = round(cand["edge"] - HANDICAP_WIN_FEE_PCT * (cand["lm_px"] + cand["pm_px"]), 4)
                    if w.best is None or cand["usd"] > w.best["usd"]:
                        w.best = cand
                    if cand["edge_h"] < MIN_EDGE_AFTER_HANDICAP:
                        self._log_sub_handicap(w, cand, now)
                        continue
                    lmv, pmv = self._moves(w, lm_side, pm_side, a_l, a_p, now)
                    cand["lm_move"], cand["pm_move"] = lmv, pmv
                    if pmv is not None and pmv > (lmv or 0) + 0.01 and cand["edge_h"] < PM_MOVING_MIN_EDGE_H:
                        self._log_pm_moving(w, cand, now)
                        continue
                    self._log_new_best(w, cand, now)
                    # CHAQUE arb positif persistant declenche l'executeur (Steven 28/09 :
                    # "on ne regarde jamais le pognon passer sans le saisir")
                    if self.on_arb is not None:
                        try:
                            self.on_arb(w, cand)
                        except Exception as e:
                            self._log(f"⚠️ [XARB] declenchement : {str(e)[:120]}")
            else:
                w.arb_streak[name] = 0
        if any_pos:
            w.arb_seconds += self.EVAL_PERIOD_S
        top = (lm["yes_bids"][:1], lm["yes_asks"][:1], up["bids"][:1], up["asks"][:1], dn["bids"][:1], dn["asks"][:1])
        if top != w.last_top:
            w.last_top = top
            if False:  # remplace par la base datalake (ticks 1 Hz, hors OneDrive) depuis le 28/09
             self._append(self.ticks_path, {
                "ts": round(now, 2), "slug": p["lm_slug"], "tl": int(tl),
                "lm_b": lm["yes_bids"][0] if lm["yes_bids"] else None,
                "lm_a": lm["yes_asks"][0] if lm["yes_asks"] else None,
                "up_b": up["bids"][0] if up["bids"] else None, "up_a": up["asks"][0] if up["asks"] else None,
                "dn_b": dn["bids"][0] if dn["bids"] else None, "dn_a": dn["asks"][0] if dn["asks"] else None})

    def _moves(self, w, lm_side, pm_side, a_l, a_p, now):
        """|variation| de l'ask LM et de l'ask PM de chaque jambe sur les
        PM_MOVING_LOOKBACK_S dernieres secondes (None si pas d'historique)."""
        old = self._snap_at(w.p["lm_slug"], now - PM_MOVING_LOOKBACK_S)
        if not old or now - old[0] > PM_MOVING_LOOKBACK_S + 2.5:
            return None, None
        o = old[1]
        la = (o["lm"]["yes_asks"] if lm_side == "YES" else o["lm"]["no_asks"]) or []
        pa = ((o["up"] if pm_side == "UP" else o["dn"]).get("asks")) or []
        lmv = abs(la[0][0] - a_l) if la else None
        pmv = abs(pa[0][0] - a_p) if pa else None
        return lmv, pmv

    def _log_pm_moving(self, w, c, now):
        if now - getattr(w, "_pmmov_log_ts", 0) < 60:
            return
        w._pmmov_log_ts = now
        self.stats[w.bucket]["pm_moving_skips"] += 1
        self._log(f"🌊 [arb PM en mouvement] {w.label} {_hm(w.p['end_ts'])} ({_tleft(c['t_left_s'])}) "
                  f"LM {c['lm_side']}@{c['lm_px']:.3f} + PM {c['pm_side']}@{c['pm_px']:.3f} {c['edge_h']:+.3f} apres handicap "
                  f"mais PM a bouge de {c['pm_move']:.3f} en {PM_MOVING_LOOKBACK_S:.0f}s (LM {c['lm_move'] or 0:.3f}) "
                  f"-> exige >= {PM_MOVING_MIN_EDGE_H} (risque de jambe nue)")

    def _log_sub_handicap(self, w, c, now):
        """Arb net positif mais qui ne survit pas au handicap 40 kg : pas de trade,
        1 ligne / minute / fenetre max (le bilan de fenetre garde tout)."""
        if now - getattr(w, "_sub_log_ts", 0) < 60:
            return
        w._sub_log_ts = now
        self._log(f"🔸 [arb<40kg] {w.label} {_hm(w.p['end_ts'])} ({_tleft(c['t_left_s'])}) "
                  f"LM {c['lm_side']}@{c['lm_px']:.3f} + PM {c['pm_side']}@{c['pm_px']:.3f} "
                  f"+{c['edge']:.3f}/part net mais {c['edge_h']:+.3f} apres handicap (< {MIN_EDGE_AFTER_HANDICAP}) -> pas de trade")

    def _log_new_best(self, w, c, now):
        if c["edge"] < w.last_logged_edge + 0.005 and now - w.last_log_ts < 30:
            return
        w.last_logged_edge, w.last_log_ts = c["edge"], now
        st = self.stats[w.bucket]
        st["arb_windows_new_best"] += 1
        self._emit("arb_best", slug=w.p["lm_slug"], **c)
        self._log(f"💰 [ARB] {w.label} {_hm(w.p['end_ts'])} ({_tleft(c['t_left_s'])}) "
                  f"LM {c['lm_side']}@{c['lm_px']:.3f} + PM {c['pm_side']}@{c['pm_px']:.3f} "
                  f"-> +{c['edge']:.3f}/part x{c['size']:.0f} = +{c['usd']:.2f}$ (handicap 40kg {c['usd_h']:+.2f}$)"
                  f"{'' if c['pm_min_ok'] else ' (sous min 5 parts PM)'}")

    def _close_window(self, w):
        rec = w.record()
        self.closed.append(rec)
        self._append(self.windows_path, rec)
        st = self.stats[w.bucket]
        st["windows"] += 1
        if w.best:
            st["windows_with_arb"] += 1
            st["arb_usd"] += w.best["usd"]
            st["arb_usd_h"] += w.best["usd_h"]
            st["arb_best_edge"] = max(st["arb_best_edge"], w.best["edge"])
        b, c = w.best, w.closest
        arb_txt = (f"meilleur ARB +{b['edge']:.3f}/part x{b['size']:.0f} = +{b['usd']:.2f}$ [40kg {b['usd_h']:+.2f}$] "
                   f"(LM {b['lm_side']}@{b['lm_px']:.3f} + PM {b['pm_side']}@{b['pm_px']:.3f}) a {_tleft(b['t_left_s'])} "
                   f"· {w.arb_seconds:.0f}s d'arb") if b else "aucun arb net"
        close_txt = (f"plus proche {c['cost']:.3f} ({c['dir'].replace('+', '/')}) a {_tleft(c['t_left_s'])}") if c else "carnets vides"
        spr = (f"spread LM {rec['lm_spread_avg']:.3f} / PM {rec['pm_spread_avg']:.3f}"
               if rec["lm_spread_avg"] is not None else "LM sans carnet 2 cotes")
        self._log(f"🪟 [FENETRE] {w.label} {_hm(w.p.get('start_ts') or w.p['end_ts'])}->{_hm(w.p['end_ts'])} | {arb_txt} | "
                  f"{close_txt} | {spr} | {w.lm_trades} trades LM ({w.lm_volume:.0f}$) | "
                  f"MM ombre {w.mm_fills} fills {w.mm_pnl:+.2f}$ [40kg {w.mm_pnl_h:+.2f}$] | {w.n_obs} obs")

    # ── trades reels Limitless + fills simules ─────────────────────────
    def _poll_trades(self, w):
        p = w.p
        slug = p["lm_slug"]
        r = self.c.trades(slug, limit=100)
        yes_tok, no_tok = str(p["lm_tokens"]["yes"]), str(p["lm_tokens"]["no"])
        st = self.stats[w.bucket]
        for e in reversed(r.get("events", [])):
            key = (e.get("txHash"), e.get("tokenId"), e.get("matchedSize"), e.get("createdAt"))
            if key in self.seen:
                continue
            self.seen.add(key)
            ts = _iso(e["createdAt"])
            if ts < self.started:
                continue
            px, qty = float(e["price"]), int(e["matchedSize"]) / 1e6
            tok, side = str(e.get("tokenId")), int(e.get("side", 0))
            if tok == yes_tok:
                taker, py = ("buy_yes" if side == 0 else "sell_yes"), px
            elif tok == no_tok:
                taker, py = ("sell_yes" if side == 0 else "buy_yes"), 1 - px
            else:
                continue
            w.lm_trades += 1
            w.lm_volume += px * qty
            st["lm_trades"] += 1
            st["lm_volume_usd"] += px * qty
            self._counterfactual_fill(w, ts, taker, py, qty, st)

    def _counterfactual_fill(self, w, ts, taker, py, qty, st):
        p = w.p
        slug = p["lm_slug"]
        before = self._snap_at(slug, ts)
        after = self._snap_at(slug, ts + self.HEDGE_LATENCY_S, after=True)
        if not before or not after or ts - before[0] > 10:
            st["fills_unjudgeable"] += 1
            return
        s0, s1 = before[1], after[1]
        if not (s0["up"]["bids"] and s0["dn"]["bids"] and s0["up"]["asks"]):
            st["fills_unjudgeable"] += 1  # carnet PM a un seul cote : pas de cotation MM
            return
        # COTATION PAR LE COUT DE COUVERTURE (Claude 28/09, correction apres le
        # 1er fill ombre perdant) : coter "bid PM - 1ct" perdait par construction
        # (bid + ask + frais PM > 1). On cote au prix qui laisse MARGIN apres avoir
        # paye la couverture a l'ask PM ET ses frais :
        #   YES = 1 - ask_Down_PM - frais(ask_Down) - MARGIN
        #   NO  = 1 - ask_Up_PM   - frais(ask_Up)   - MARGIN
        if not (s0["up"]["asks"] and s0["dn"]["asks"]):
            st["fills_unjudgeable"] += 1
            return
        ua0, da0 = s0["up"]["asks"][0][0], s0["dn"]["asks"][0][0]
        yes_q = math.floor((1 - da0 - pm_fee_per_share(da0, p["pm_fee_rate"], p["pm_fee_exp"]) - self.MARGIN) * 1000) / 1000
        no_q = math.floor((1 - ua0 - pm_fee_per_share(ua0, p["pm_fee_rate"], p["pm_fee_exp"]) - self.MARGIN) * 1000) / 1000
        fair_yes = (s0["up"]["bids"][0][0] + s0["up"]["asks"][0][0]) / 2
        st["retail_dev_sum"] += abs(py - fair_yes)
        st["retail_dev_n"] += 1
        lm = s0["lm"]
        if taker == "buy_yes":
            our_px, our_ask_yes = no_q, 1 - no_q
            crosses = our_ask_yes <= py + 1e-9
            ahead = sum(s for pr, s in lm["yes_asks"] if pr <= our_ask_yes + 1e-9)
            hedge_levels, held, exit_book = s1["up"]["asks"], "NO", s1["up"]
        else:
            our_px = yes_q
            crosses = yes_q >= py - 1e-9
            ahead = sum(s for pr, s in lm["yes_bids"] if pr >= yes_q - 1e-9)
            hedge_levels, held, exit_book = s1["dn"]["asks"], "YES", s1["dn"]
        if our_px < 0.01 or not crosses:
            return
        filled = min(self.QUOTE_SIZE, max(0.0, qty - ahead))
        if filled <= 0:
            st["fills_queue_blocked"] += 1
            return
        hedge_qty = max(filled, p["pm_min_order"])
        h_px = _depth_price(hedge_levels, hedge_qty)
        if h_px is None:
            st["fills_unhedgeable"] += 1
            return
        fee = pm_fee_per_share(h_px, p["pm_fee_rate"], p["pm_fee_exp"])
        excess = hedge_qty - filled
        exit_bid = exit_book["bids"][0][0] if exit_book["bids"] else 0.0
        pnl = filled * (1 - our_px - h_px - fee) - excess * (h_px + fee - exit_bid)
        stake = filled * our_px + hedge_qty * (h_px + fee)
        pnl_h = pnl - HANDICAP_WIN_FEE_PCT * stake if pnl > 0 else pnl
        w.mm_fills += 1
        w.mm_pnl += pnl
        w.mm_pnl_h += pnl_h
        st["fills_pnl_h"] += pnl_h
        st["fills"] += 1
        st["fills_shares"] += filled
        st["fills_pnl"] += pnl
        st["fills_wins"] += 1 if pnl > 0 else 0
        self._emit("mm_fill", slug=slug, taker=taker, taker_px=round(py, 4), held=held,
                   our_px=round(our_px, 3), qty=round(filled, 3), hedge_px=round(h_px, 4),
                   hedge_fee=round(fee, 4), excess=round(excess, 3), pnl=round(pnl, 4), pnl_h=round(pnl_h, 4),
                   t_left_s=int(w.t_left(ts)))
        self._log(f"🎯 [MM OMBRE] {w.label} {_hm(p['end_ts'])} ({_tleft(w.t_left(ts))}) taker {taker}@{py:.3f} "
                  f"-> notre {held}@{our_px:.3f} x{filled:.1f}, couverture PM@{h_px:.3f} -> {pnl:+.3f}$ [40kg {pnl_h:+.3f}$]")

    # ── boucle ─────────────────────────────────────────────────────────
    def _slow_loop(self):
        """Taches lentes (reseau sequentiel) HORS de la boucle d'evaluation 1 Hz :
        appariement des paires (30 s) et trades reels Limitless (20 s/marche).
        Avant : elles bloquaient l'evaluation jusqu'a 5 s (16 requetes a la suite)."""
        while not self._stop.is_set():
            now = time.time()
            if now - self.last_pairs > self.PAIRS_PERIOD_S:
                self.last_pairs = now
                try:
                    self.pf.refresh()
                    self._pairs_dirty = True
                except Exception as e:
                    self._log(f"⚠️ [LMTS-PAIRS] refresh : {str(e)[:120]}")
            for w in (list(self.windows.values()) if self.mode != "events" else []):
                if now - self.last_trades.get(w.p["lm_slug"], 0) >= self.TRADES_PERIOD_S:
                    self.last_trades[w.p["lm_slug"]] = now
                    try:
                        self._poll_trades(w)
                    except Exception as e:
                        self._log(f"⚠️ [LMTS-SHADOW] trades {w.label}: {str(e)[:100]}")
            self._stop.wait(1.0)

    def tick(self):
        now = time.time()
        if self._pairs_dirty:
            self._pairs_dirty = False
            if config.has_api_credentials():
                try:
                    self.fee_bps = self.c.fee_rate_bps() or 300
                except Exception:
                    pass
            new = []
            for slug, p in self.pf.pairs.items():
                if p["verified"] and slug not in self.windows and slug not in self._closed_slugs:
                    self.windows[slug] = Window(p)
                    self.windows[slug].sh = self
                    new.append(self.windows[slug])
            if new:
                new.sort(key=lambda w: w.p["end_ts"])
                shown = new[:10]
                self._log(f"🔭 [{self.tag}] +{len(new)} fenetre(s) suivie(s) : "
                          + ", ".join(f"{w.label} ->{_hm(w.p['end_ts'])}" for w in shown)
                          + (f" (+{len(new) - len(shown)} autres)" if len(new) > len(shown) else "")
                          + f" | {len(self.windows)} en cours")
            self.feed.set_slugs([s for s, w in self.windows.items() if w.t_left(now) > -self.CLOSE_GRACE_S])
        for slug, w in list(self.windows.items()):
            if w.t_left(now) < -self.CLOSE_GRACE_S:
                self._close_window(w)
                self._closed_slugs.add(slug)
                del self.windows[slug]
                self.hist.pop(slug, None)
        live = [w for w in self.windows.values() if w.t_left(now) > 0]
        if not live:
            return
        toks = [t for w in live for t in (w.p["pm_up_token"], w.p["pm_down_token"])]
        src = self.pm_source
        if src is not None:
            src.extra_tokens.update(toks)
        pm = None
        if self._pm_ws is not None:
            self._pm_ws.want_tokens(toks)
            got = {t: self._pm_ws.book_levels(t, max_age=1.5) for t in toks}
            if all(got.values()):
                pm = got
                self.pm_ws_hits += 1
            else:
                self.pm_ws_miss += 1
        if pm is not None:
            pass
        elif src is not None and time.time() - src.latest_pm_ts < 1.5 and all(t in src.latest_pm for t in toks):
            pm = {t: {"bids": src.latest_pm[t][0], "asks": src.latest_pm[t][1]} for t in toks}
        else:
            pm = self._pm_books(toks)
        for w in live:
            lm = self._lm_book(w.p["lm_slug"], now)
            up, dn = pm.get(w.p["pm_up_token"]), pm.get(w.p["pm_down_token"])
            # Un marche presque tranche n'a souvent qu'UN cote par token (ex. Up : bids
            # seulement a 0.99) : la donnee reste exploitable, on ne la jette plus.
            if not lm or not up or not dn or not (up["bids"] or up["asks"]) or not (dn["bids"] or dn["asks"]):
                w.missing += 1
                continue
            snap = {"lm": lm, "up": up, "dn": dn}
            h = self.hist[w.p["lm_slug"]]
            if not h or now - h[-1][0] >= 1.0:   # historique a 1 Hz (eval a 4 Hz : memoire)
                h.append((now, snap))
            while h and now - h[0][0] > self.HIST_S:
                h.popleft()
            self._evaluate(w, snap, now)

    def live_windows(self):
        now = time.time()
        out = []
        for w in sorted(self.windows.values(), key=lambda x: x.p["end_ts"]):
            snap = (self.hist.get(w.p["lm_slug"]) or [(None, None)])[-1][1]
            cur = None
            if snap:
                lm, up, dn = snap["lm"], snap["up"], snap["dn"]
                cur = {"lm_b": lm["yes_bids"][0][0] if lm["yes_bids"] else None,
                       "lm_a": lm["yes_asks"][0][0] if lm["yes_asks"] else None,
                       "up_b": up["bids"][0][0] if up["bids"] else None, "up_a": up["asks"][0][0] if up["asks"] else None,
                       "dn_b": dn["bids"][0][0] if dn["bids"] else None, "dn_a": dn["asks"][0][0] if dn["asks"] else None}
            out.append({**w.record(), "t_left_s": int(w.t_left(now)), "now": cur})
        return out

    def summary(self):
        hours = max((time.time() - self.started) / 3600, 1e-6)
        out = {"since": self.started, "hours": round(hours, 3), "margin": self.MARGIN,
               "quote_size": self.QUOTE_SIZE, "hedge_latency_s": self.HEDGE_LATENCY_S,
               "fee_bps": self.fee_bps, "ws_connected": self.feed.connected, "ws_frames": self.feed.frames,
               "eval_hz": round(1 / self.EVAL_PERIOD_S, 2), "pm_ws_hits": self.pm_ws_hits, "pm_ws_miss": self.pm_ws_miss,
               "windows_live": len(self.windows), "buckets": {}}
        tot = defaultdict(float)
        for b, st in self.stats.items():
            d = {k: round(v, 4) for k, v in st.items()}
            d["retail_dev_avg"] = round(st["retail_dev_sum"] / st["retail_dev_n"], 4) if st["retail_dev_n"] else None
            d["fills_pnl_per_hour"] = round(st["fills_pnl"] / hours, 4)
            out["buckets"][b] = d
            for k in ("windows", "windows_with_arb", "arb_usd", "arb_usd_h", "fills", "fills_pnl", "fills_pnl_h", "fills_wins",
                      "lm_trades", "lm_volume_usd"):
                tot[k] += st[k]
        tot = dict(tot)
        tot["fills_pnl_per_hour"] = round(tot.get("fills_pnl", 0) / hours, 4)
        tot["arb_usd_per_hour"] = round(tot.get("arb_usd", 0) / hours, 4)
        tot["arb_usd_h_per_hour"] = round(tot.get("arb_usd_h", 0) / hours, 4)
        tot["fills_pnl_h_per_hour"] = round(tot.get("fills_pnl_h", 0) / hours, 4)
        tot["fills_winrate"] = round(tot["fills_wins"] / tot["fills"], 3) if tot.get("fills") else None
        out["total"] = {k: round(v, 4) if isinstance(v, float) else v for k, v in tot.items()}
        return out

    def write_summary(self):
        try:
            self.summary_path.write_text(json.dumps(self.summary(), indent=1), encoding="utf-8")
        except OSError:
            pass

    def _loop(self):
        self._log(f"👻 [{self.tag}-SHADOW] mesure inter-plateformes v2 demarree ({self.mode}, WS + 1 eval/"
                  f"{self.EVAL_PERIOD_S:g}s, aucun ordre)")
        self.feed.start()
        threading.Thread(target=self._slow_loop, daemon=True, name="limitless-slow").start()
        last_sum = 0
        while not self._stop.is_set():
            t0 = time.time()
            try:
                self.tick()
            except Exception as e:
                self._log(f"⚠️ [LMTS-SHADOW] tick: {str(e)[:160]}")
                self._stop.wait(3)
            if t0 - last_sum > 10:
                last_sum = t0
                self.write_summary()
            self._stop.wait(max(0.05, self.EVAL_PERIOD_S - (time.time() - t0)))

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name=f"limitless-shadow-{self.mode}")
        self._thread.start()

    def stop(self):
        self._stop.set()


if __name__ == "__main__":
    s = CrossVenueShadow(log_fn=lambda m: print(time.strftime("%H:%M:%S"), m, flush=True))
    s.start()
    try:
        while True:
            time.sleep(60)
            print(json.dumps(s.summary()["total"]), flush=True)
    except KeyboardInterrupt:
        s.stop()
