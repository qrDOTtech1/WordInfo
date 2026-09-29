"""EXECUTEUR D'ARBITRAGE LIMITLESS <-> POLYMARKET (Claude 28/09, Steven :
"chaque ligne ARB en positif doit declencher un trade -- on ne regarde JAMAIS
le pognon passer devant nous sans le saisir").

Chaque arb net positif valide par la mesure (persistant >= 2 evaluations)
declenche un trade. MODE PAPER (defaut, XARB_MODE=paper) : execution simulee
PLUS DURE que le reel (regle "sac de 40 kg") :
  1. LATENCE : attente = plancher calibre sur les ordres reels mesures
     (>= 1.2 x pire latence reelle) + tirage jusqu'a 1 s + DELAI TAKER
     Limitless du marche (250-500 ms) ; les carnets sont RELUS apres.
  2. ORDRES LIMITE : chaque jambe est plafonnee au prix detecte + 1 tick
     (comme un vrai ordre FAK limite) -> ne se remplit que la profondeur
     encore disponible sous ce plafond APRES la latence.
  3. PROFONDEUR DEJA CONSOMMEE : sur une meme fenetre/direction, les parts
     deja prises par nos trades precedents sont retirees du carnet.
  4. RISQUE DE JAMBE : si une jambe se remplit moins que l'autre, l'excedent
     (position nue) est REVENDU immediatement au pire bid apres latence,
     frais de vente inclus -> perte comptee.
  5. FRAIS : taker Limitless (bareme de notre profil) + taker Polymarket
     (rate*p*(1-p)), puis handicap 6% de la mise sur le gain a la cloture.
Resolution : resultats OFFICIELS des deux venues (Polymarket outcomePrices,
Limitless winningOutcomeIndex : 0 = YES = Up). Payout = somme des jambes
gagnantes -> gere aussi le cas (rare) ou les deux venues divergeraient.

MODE REEL (XARB_MODE=real + LIMITLESS_ENABLED=1 + LIMITLESS_DRY_RUN=0) : meme
sequence avec de vrais ordres (_execute_real). Pas encore teste avec des fonds.
"""
import math
import os
import random
import threading
import time
import uuid

import requests

from limitless.client import LimitlessClient
from limitless.shadow import _hm, _tleft, lm_buy_fee_pct, pm_fee_per_share

GAMMA = "https://gamma-api.polymarket.com/markets"


def _walk(levels, qty):
    """Parts reellement remplies et prix moyen en balayant levels [(p, s)]."""
    got = cost = 0.0
    for p, s in levels:
        take = min(s, qty - got)
        if take <= 0:
            break
        got += take
        cost += take * p
    return got, (cost / got if got else None)


EXECUTOR = None   # instance unique (endpoints /api/limitless/xarb*)


class CrossArbExecutor:
    HIGH_EDGE_H = 0.05        # edge apres handicap a partir duquel un arb est "gros"
    RESERVE_FOR_HIGH = 0.30   # part du portefeuille interdite aux petits arbs
    PAIR_SLACK = 0.15         # cout max d'une paire = prix affiche + 0,15$
    TIERS = {
        # Steven 28/09 22h : "trop petits trades qui gagnent trop peu" -> sur 15 min /
        # horaires le carnet LM offrait 200-470 parts a +0,09 et on s'arretait a 30$.
        "low": {"per_arb": 0.25, "per_window": 0.30, "long": 0.40, "stake_mult": 1.0},
        "high": {"per_arb": 0.35, "per_window": 0.50, "long": 0.60, "stake_mult": 2.0},
    }
    # JAMBES NUES (29/09, NFLX : 18 jambes LM nues = 1 090 $ accumulees en 17 min,
    # le carnet PM d'une action est mince et bouge pendant la latence) :
    NAKED_BLOCK_S = 600             # apres une jambe nue sur un marche : plus d'entree 10 min
    MAX_NAKED_PCT = 0.15            # jambes nues > 15 % du portefeuille : plus d'entree sur les EVENEMENTS (carnets minces)
    MAX_NAKED_PCT_ALL = 0.30        # > 30 % : plus AUCUN arb (crypto compris)
    REHEDGE_MARGIN = 0.03           # recouvrement : paire <= 1 - 3 c (gain verrouille)
    # POLYMARKET D'ABORD (29/09 12:15 : BTC+ETH 15m a T-1 min, jambes LM a 0,08/0,12
    # restees nues = -87 $). Une jambe LM nue est quasi invendable (bid LM ~0) ;
    # une jambe PM nue se revend tout de suite pour l'ecart du carnet. Trades a
    # risque -> PM en premier ; si LM n'est pas servi, l'excedent PM est revendu.
    # Steven 29/09 12:40 : "aucune perte ne doit etre toleree hors handicap" -> PM
    # d'abord pour TOUS les trades : plus jamais de jambe LM nue ; pire cas = l'ecart
    # du carnet PM a la revente. XARB_PM_FIRST_ALWAYS=0 pour revenir au mode mixte.
    PM_FIRST_ALWAYS = os.environ.get("XARB_PM_FIRST_ALWAYS", "0") == "1"
    # ANNULE (Steven 29/09 13:00, mesure : PM d'abord systematique = -76 $/h vs +103 $/h
    # en LM d'abord + recouvrement) : "ne plafonne rien, la boule de neige nous a amenes
    # a 5k". PM d'abord (systematique OU sur risque) desactive par defaut ; taille LM
    # non plafonnee. Reactivables : XARB_PM_FIRST_ALWAYS=1 / XARB_PM_FIRST_RISKY=1.
    PM_FIRST_RISKY = os.environ.get("XARB_PM_FIRST_RISKY", "0") == "1"
    LM_FIRST_CAP = os.environ.get("XARB_LM_FIRST_CAP", "0") == "1"
    RISKY_TLEFT_S = 180             # 5m/15m : moins de 3 min restantes
    RISKY_LM_LO, RISKY_LM_HI = 0.20, 0.80   # prix LM extreme = jambe nue loterie / sans valeur
    LM_FIRST_MAX_PCT = 0.03         # ordre LM d'abord : jambe LM <= 3 % du portefeuille (pire cas borne)
    HEALTH_EVERY_S = 300            # sante du reel : repartition du capital + rapprochement on-chain
    MIN_SIDE_PCT = 0.15             # alerte si une plateforme a < 15% du capital
    MIN_GAS_ETH = 0.0005            # alerte si l'ETH (gas Base) est presque vide
    PM_FILL_WAIT_S = 8.0            # attente max du solde on-chain apres le FAK Polymarket
    REAL_MAX_DAY_LOSS_PCT = 0.10    # coupe-circuit : perte du jour > 10% du capital (min 5$)
    REAL_MAX_NAKED_PCT = 0.25       # plus de nouvel arb si jambes nues > 25% du capital
    REAL_MAX_ERRORS_10MIN = 3       # coupe-circuit : 3 exceptions d'execution en 10 min
    PM_SPORT_LIVE_DELAY_S = 3.0   # delai Polymarket sur les ordres sport pendant le match (paper : pessimiste)
    # REGARNISSAGE LIMITLESS (mesure datalake 28/09, 4592 epuisements du carnet
    # 5/15 min) : 61% reviennent au meme prix en < 65 s, mediane 17,5 s, p75 40 s.
    # Avant : parts prises retirees pour TOUTE la fenetre (jamais de 2e prise).
    LM_REFILL_S = 60.0
    PM_REFILL_S = 300.0     # duree pendant laquelle nos achats PM restent retires du carnet
    # BOULE DE NEIGE (Steven 28/09 22h : "ca doit suivre le compte jusqu'a etre limite
    # par la profondeur") : PLUS de plafond fixe en $ -- la mise est un % du
    # portefeuille (TIERS) et la taille est bornee par la profondeur du carnet.
    # XARB_MAX_STAKE_USD > 0 dans .env remet un plafond absolu (garde-fou optionnel).
    MAX_STAKE_USD = float(os.environ.get("XARB_MAX_STAKE_USD", 0))
    COOLDOWN_S = 20          # meme fenetre + meme direction : 1 trade / 20 s max
    LEG_CAP_TICK = 0.01      # plafond limite = prix detecte + 1 tick
    MIN_T_LEFT_S = 5
    RESOLVE_EVERY_S = 30

    def __init__(self, trader, shadow, log_fn=None):
        self.t = trader
        self.sh = shadow
        self._log = log_fn or print
        self.open = trader.state.setdefault("xarb_open", {})
        # (slug, dir) -> [(ts, parts)] deja prises : retirees du carnet LM visible
        # pendant LM_REFILL_S (le paper ne vide pas le vrai carnet)
        self.taken = {}
        # parts deja ACHETEES par nous sur un token Polymarket [(ts, q)] : le
        # carnet PM ne se regarnit pas instantanement -> retirees de la
        # profondeur visible pendant PM_REFILL_S (sac de 40 kg, Claude 28/09)
        self.pm_taken = {}
        self.last = {}
        self.stats = {"signals": 0, "trades": 0, "skipped": 0, "unwinds": 0}
        self._lock = threading.Lock()
        self._real_lock = threading.Lock()   # UN arb reel a la fois (voir _execute_real)
        self.naked_block = {}                # lm_slug -> ts jusqu'auquel on n'entre plus (jambe nue recente)
        self.real_errors = []
        # COUPE-CIRCUIT REEL persiste dans l'etat (survit aux redemarrages)
        self.guard = trader.state.setdefault("xarb_real_guard", {"day": None, "pnl_day": 0.0, "cap_day": None,
                                                                  "killed": None, "killed_ts": None})
        self._lm = LimitlessClient(log_fn=lambda m: None)
        self._http = requests.Session()
        threading.Thread(target=self._resolve_loop, daemon=True, name="xarb-resolve").start()
        global EXECUTOR
        EXECUTOR = self

    @staticmethod
    def mode():
        return "real" if os.environ.get("XARB_MODE", "paper").strip().lower() == "real" else "paper"

    # ── signal ─────────────────────────────────────────────────────────
    def on_signal(self, w, cand):
        now = time.time()
        key = (w.p["lm_slug"], cand["dir"])
        self.stats["signals"] += 1
        with self._lock:
            if now - self.last.get(key, 0) < self.COOLDOWN_S:
                return
            self.last[key] = now
        if w.t_left(now) < self.MIN_T_LEFT_S:
            return
        mode = self.mode()
        if now < self.naked_block.get(w.p["lm_slug"], 0):
            self.t._tlog(f"xarb_nkblk_{w.p['lm_slug']}", f"🧯 [XARB] {w.label} : jambe nue recente sur ce marche -> "
                         f"pas de nouvelle entree avant {_hm(self.naked_block[w.p['lm_slug']])} (carnet PM trop mouvant)",
                         every=120)
            return
        naked, cap = self._naked_exposure(mode), self._capital(mode)
        lim = self.MAX_NAKED_PCT_ALL if w.bucket != "event" else self.MAX_NAKED_PCT
        if cap and naked > lim * cap:
            self.t._tlog(f"xarb_naked_cap_{w.bucket == 'event'}",
                         f"🧯 [XARB] jambes nues {naked:.2f}$ > {lim:.0%} du portefeuille ({cap:.0f}$) -> plus d'arb "
                         f"{'evenements' if w.bucket == 'event' else 'du tout'} tant qu'elles ne sont pas recouvertes/resolues",
                         every=300)
            return
        if self.mode() == "real":
            from limitless import config as _cfg
            if not (_cfg.enabled() and not _cfg.dry_run()):
                self.t._tlog("xarb_real_locked", "⛔ [XARB][REEL] XARB_MODE=real mais Limitless non arme "
                             "(LIMITLESS_ENABLED=1 et LIMITLESS_DRY_RUN=0 requis) -> signal ignore")
                return
            threading.Thread(target=self._execute_real, args=(w, dict(cand)), daemon=True, name="xarb-exec").start()
            return
        threading.Thread(target=self._execute_paper, args=(w, dict(cand)), daemon=True,
                         name="xarb-exec").start()

    # ── execution paper handicapee ─────────────────────────────────────
    def _paper_cash_free(self):
        _, cash_libre, _ = self.t._portfolio_value("paper")
        return max(0.0, cash_libre - self._bank_locked("paper"))

    @staticmethod
    def _bank_locked(mode):
        """Argent promis a un retrait / au coffre par l'onglet BANQUE : jamais engage."""
        try:
            from real_web import bank as _bank
            return _bank.locked(mode)
        except Exception:
            return 0.0

    def _execute_paper(self, w, c):
        """Jambes SEQUENTIELLES (Claude 28/09, apres -2.54$ de jambe orpheline en
        simultane) : 1) LIMITLESS d'abord -- c'est le prix rare et faux qui peut
        disparaitre ; s'il ne se remplit pas, on ne perd RIEN. 2) POLYMARKET
        ensuite, carnet profond de reference : on accepte de payer jusqu'au prix
        d'EQUILIBRE de la paire (plutot que de garder une jambe nue) ; au-dela,
        la jambe Limitless est revendue au pire bid et la perte est comptee."""
        t0 = time.time()
        p = w.p
        sym = w.coin
        sh = getattr(w, "sh", None) or self.sh
        book = self._book(w)
        if book is None:
            self.stats["skipped"] += 1
            return
        fee_bps = getattr(sh, "fee_bps", 300)
        lat_floor = self.t._paper_latency_floor()
        free = self._paper_cash_free()
        scale = min(1.0, max(0.25, c["edge"] / 0.05))   # mise proportionnelle a l'edge
        pv, _, _ = self.t._portfolio_value("paper")
        # RESERVE DE CAPITAL (Claude 28/09 : 8 arbs a +6..+18c rates car le capital
        # etait gele par des horaires) : <= 15% du portefeuille par arb, et les
        # fenetres LONGUES (horaire/journalier) plafonnees a 40% du portefeuille
        # -> le reste reste disponible pour les 5/15 min qui rendent vite le capital.
        # ALLOCATION PAR NIVEAU D'EDGE (Claude 28/09 soir) : l'analyse du log a
        # montre 84 fenetres a +0,05..+0,84 $/part IGNOREES faute de budget --
        # capital deja pris par des arbs a +1..5 c ou bloque par les plafonds.
        # Les plus gros ecarts arrivent en FIN de fenetre : on leur RESERVE du
        # capital et des plafonds plus larges.
        high = c.get("edge_h", 0) >= self.HIGH_EDGE_H
        tier = self.TIERS["high" if high else "low"]
        usable = free if high else max(0.0, free - self.RESERVE_FOR_HIGH * pv)
        # % du portefeuille (suit le compte) ; les petits arbs (< 5c/part) a proportion
        budget = min(usable, tier["per_arb"] * pv * (1.0 if high else scale))
        if self.MAX_STAKE_USD > 0:
            budget = min(budget, self.MAX_STAKE_USD * (tier["stake_mult"] if high else 1.0))
        # CONCENTRATION par fenetre (risque residuel = les 2 venues qui divergent)
        win_engaged = sum(x["cost"] for x in self.open.values()
                          if x.get("mode") == "paper" and x.get("lm_slug") == p["lm_slug"])
        budget = min(budget, max(0.0, tier["per_window"] * pv - win_engaged))
        if w.bucket in ("hourly", "daily", "weekly", "event"):
            long_engaged = sum(x["cost"] for x in self.open.values()
                               if x.get("mode") == "paper" and x.get("bucket") in ("hourly", "daily", "weekly", "event"))
            budget = min(budget, max(0.0, tier["long"] * pv - long_engaged))
        # Taille bornee par le cout MAXIMAL accepte pour une paire : prix affiche
        # + PAIR_SLACK (avant : 1$ fixe -> sur un arb a +0,84 (paire 0,16$) on
        # achetait 6x moins que le budget). La jambe PM est plafonnee pour que la
        # paire ne depasse jamais ce cout -> le budget n'est JAMAIS depasse.
        max_pair = min(1.0, c["lm_top"] + c["pm_top"] + self.PAIR_SLACK)
        target = min(c["size"], budget / max_pair)
        if target < p.get("pm_min_order", 5):
            self.stats["skipped"] += 1
            why = (f"profondeur trop fine ({c['size']:.1f} parts < minimum 5 Polymarket)"
                   if c["size"] < p.get("pm_min_order", 5) else
                   f"budget paper insuffisant ({budget:.2f}$ allouables sur {free:.2f}$ libres, "
                   f"{'GROS arb' if high else 'petit arb, reserve ' + format(self.RESERVE_FOR_HIGH * pv, '.0f') + '$ gardee pour les gros'})")
            self.t._tlog(f"xarb_skip_{p['lm_slug']}", f"⏭️ [XARB][PAPER] {w.label} {_hm(p['end_ts'])} arb +{c['edge']:.3f} "
                       f"ignore : {why}", every=30.0)
            return
        risky, why_r = self._risky(w, c)
        if risky:
            return self._execute_paper_pm_first(w, c, p, sym, sh, book, fee_bps, lat_floor, high, max_pair,
                                                target, t0, why_r)
        if self.LM_FIRST_CAP:   # desactive par defaut (boule de neige)
            target = min(target, max(20.0, self.LM_FIRST_MAX_PCT * pv) / max(0.01, c["lm_top"]))
        # ── jambe 1 : LIMITLESS (taker, delai taker du marche en plus) ──
        lat1 = random.uniform(lat_floor, max(lat_floor, 1.0)) + (p.get("lm_taker_delay_ms") or 500) / 1000.0
        time.sleep(lat1)
        lm = sh._lm_book(p["lm_slug"], time.time()) or self._lm.orderbook(p["lm_slug"])
        lm_asks = lm["yes_asks"] if c["lm_side"] == "YES" else lm["no_asks"]
        lm_bids = lm["yes_bids"] if c["lm_side"] == "YES" else lm["no_bids"]
        # marge de plafond = moitie de l'edge APRES handicap 40 kg -> une paire
        # completee normalement reste gagnante meme apres les 6% (Claude 28/09)
        slack = max(0.0, c.get("edge_h", c["edge"])) / 2
        cap_lm = round(c["lm_top"] + slack, 3)
        key = (p["lm_slug"], c["dir"])
        now_lm = time.time()
        hist_lm = [(ts, qq) for ts, qq in self.taken.get(key, []) if now_lm - ts < self.LM_REFILL_S]
        used = sum(qq for _, qq in hist_lm)
        avail_lm, skip = [], used
        for px, sz in lm_asks:
            if px > cap_lm:
                break
            take = min(sz, skip)
            skip -= take
            if sz - take > 0:
                avail_lm.append((px, sz - take))
        q_lm, avg_lm = _walk(avail_lm, target)
        if q_lm < p.get("pm_min_order", 5):
            self.stats["skipped"] += 1
            self._log(f"🫥 [XARB][PAPER] {w.label} {_hm(p['end_ts'])} arb +{c['edge']:.3f} : jambe Limitless "
                      f"non servie apres {lat1:.2f}s ({q_lm:.1f} parts sous {cap_lm:.3f}) -> rien engage, 0$ perdu")
            return
        fee_lm_ps = avg_lm * lm_buy_fee_pct(avg_lm, fee_bps) / 100
        self.taken[key] = hist_lm + [(now_lm, q_lm)]
        # ── jambe 2 : POLYMARKET, plafond = equilibre de la paire ──
        lat2 = random.uniform(lat_floor, max(lat_floor, 1.0))
        if self._pm_live_sport(p):   # match en cours : Polymarket retarde les ordres sport
            lat2 += self.PM_SPORT_LIVE_DELAY_S
        time.sleep(lat2)
        pm_tok = p["pm_down_token"] if c["pm_side"] == "DOWN" else p["pm_up_token"]
        pmb = sh._pm_books([p["pm_up_token"], p["pm_down_token"]])
        pm_asks = (pmb.get(pm_tok) or {}).get("asks") or []
        cap_pm = 1.0
        for _ in range(3):  # prix max tel que 1 - (avg_lm + frais_lm + px + frais_pm(px)) >= 0
            cap_pm = 1 - avg_lm - fee_lm_ps - pm_fee_per_share(min(cap_pm, 0.99), p["pm_fee_rate"], p["pm_fee_exp"])
        cap_pm = min(cap_pm, max_pair - avg_lm)   # paire <= cout max budgete
        now_pm = time.time()
        hist = [(ts, qq) for ts, qq in self.pm_taken.get(pm_tok, []) if now_pm - ts < self.PM_REFILL_S]
        skip, avail_pm = sum(qq for _, qq in hist), []
        for px, sz in pm_asks:
            if px > cap_pm:
                break
            take = min(sz, skip)
            skip -= take
            if sz - take > 0:
                avail_pm.append((px, sz - take))
        q_pm, avg_pm = _walk(avail_pm, q_lm)
        q = round(min(q_lm, q_pm), 3) if q_pm >= p.get("pm_min_order", 5) else 0.0
        latency = lat1 + lat2
        unwind_pnl = 0.0
        held = 0.0
        excess = q_lm - q
        if 1e-6 < excess < 0.5 and q > 0:   # poussiere d'arrondi : gardee dans la paire, sans bruit
            held = excess
        elif excess > 1e-6:   # jambe Limitless (partiellement) nue
            time.sleep(lat_floor)
            lm2 = sh._lm_book(p["lm_slug"], time.time()) or lm
            bids2 = (lm2["yes_bids"] if c["lm_side"] == "YES" else lm2["no_bids"]) or lm_bids
            keep, v_sell, v_hold = self._hold_or_sell(p, c["lm_side"], bids2, excess, sh)
            if keep:   # garder jusqu'a la resolution (resultat REEL de l'oracle, pas d'estimation)
                held = excess
                self._mark_naked(p["lm_slug"])
                self.stats["held"] = self.stats.get("held", 0) + 1
                self._log(f"🧷 [XARB][PAPER] {w.label} {_hm(p['end_ts'])} {excess:.1f} parts LM {c['lm_side']} nues "
                          f"GARDEES jusqu'a resolution (revente LM {v_sell:.2f}$ < valeur Polymarket {v_hold:.2f}$)")
            else:      # revendue au pire bid
                sold, px = _walk(bids2, excess)
                unwind_pnl = sold * ((px or 0) * 0.985 - avg_lm - fee_lm_ps) - (excess - sold) * (avg_lm + fee_lm_ps)
                self.stats["unwinds"] += 1
        if q <= 0 and held > 0:   # aucune paire : position Limitless seule, resolue par l'oracle
            cost = held * (avg_lm + fee_lm_ps)
            pid = uuid.uuid4().hex[:10]
            self.open[pid] = {
                "id": pid, "symbol": sym, "book": book, "label": w.label, "pm_yes_idx": p.get("pm_yes_idx"),
                "pm_token": p["pm_down_token"] if c["pm_side"] == "DOWN" else p["pm_up_token"], "lm_token": str((p.get("lm_tokens") or {}).get("yes" if c["lm_side"] == "YES" else "no") or ""),
                "slug": p["pm_slug"], "lm_slug": p["lm_slug"], "side": "ARB",
                "mode": "paper", "strat": "xarb_lm_naked", "dir": c["dir"], "lm_side": c["lm_side"],
                "pm_side": c["pm_side"], "filled_shares": 0.0, "entry_price": round(avg_lm, 4),
                "cost": round(cost, 3), "fees_venues": round(held * fee_lm_ps, 3),
                "legs": {"lm": {"side": c["lm_side"], "shares": held, "avg": round(avg_lm, 4)},
                         "pm": {"side": c["pm_side"], "shares": 0.0, "avg": None}},
                "unwind_pnl": 0.0, "opened_ts": t0, "end_ts": p["end_ts"], "t_left_s": int(p["end_ts"] - t0),
                "latency_s": round(lat1 + lat2, 3), "bucket": w.bucket,
            }
            self._log(f"🦺 [XARB][PAPER] {w.label} {_hm(p['end_ts'])} Polymarket au-dela de l'equilibre {cap_pm:.3f} "
                      f"-> jambe LM {held:.1f}@{avg_lm:.3f} gardee NUE jusqu'a resolution (mise {cost:.2f}$)")
            self.t._save()
            return
        if q <= 0:
            self.stats["skipped"] += 1
            self._log(f"🦺 [XARB][PAPER] {w.label} {_hm(p['end_ts'])} Limitless rempli ({q_lm:.1f}@{avg_lm:.3f}) mais "
                      f"Polymarket au-dela de l'equilibre {cap_pm:.3f} -> jambe revendue, {unwind_pnl:+.2f}$")
            self._book_unwind_only(w, c, unwind_pnl, latency)
            return
        self.pm_taken[pm_tok] = hist + [(now_pm, q)]
        fee_lm = (q + held) * fee_lm_ps
        fee_pm = q * pm_fee_per_share(avg_pm, p["pm_fee_rate"], p["pm_fee_exp"])
        cost = q * (avg_lm + avg_pm) + held * avg_lm + fee_lm + fee_pm
        pid = uuid.uuid4().hex[:10]
        pos = {
            "id": pid, "symbol": sym, "book": book, "label": w.label, "pm_yes_idx": p.get("pm_yes_idx"),
            "pm_token": p["pm_down_token"] if c["pm_side"] == "DOWN" else p["pm_up_token"], "lm_token": str((p.get("lm_tokens") or {}).get("yes" if c["lm_side"] == "YES" else "no") or ""),
            "slug": p["pm_slug"], "lm_slug": p["lm_slug"], "side": "ARB",
            "mode": "paper", "strat": "xarb_lm_pm", "dir": c["dir"], "lm_side": c["lm_side"],
            "pm_side": c["pm_side"], "filled_shares": q, "entry_price": round(avg_lm + avg_pm, 4),
            "cost": round(cost, 3), "fees_venues": round(fee_lm + fee_pm, 3),
            "legs": {"lm": {"side": c["lm_side"], "shares": q + held, "avg": round(avg_lm, 4)},
                     "pm": {"side": c["pm_side"], "shares": q, "avg": round(avg_pm, 4)}},
            "naked_lm": round(held, 3),
            "unwind_pnl": round(unwind_pnl, 3), "opened_ts": t0, "caps": [cap_lm, round(cap_pm, 3)],
            "end_ts": p["end_ts"], "t_left_s": int(p["end_ts"] - t0), "latency_s": round(latency, 3),
            "signal": {k: c.get(k) for k in ("lm_px", "pm_px", "lm_top", "pm_top", "size", "edge", "edge_h", "usd", "usd_h")},
            "bucket": w.bucket, "tier": "high" if high else "low", "max_pair": round(max_pair, 3),
        }
        self.open[pid] = pos
        self.stats["trades"] += 1
        pv, _, engaged = self.t._portfolio_value("paper")
        self._log(
            f"⚡ [XARB][PAPER] {w.label} {_hm(p['end_ts'])} ({_tleft(pos['t_left_s'])}) "
            f"1) LM {c['lm_side']} {q_lm:.1f}@{avg_lm:.3f} -> 2) PM {c['pm_side']} {q:.1f}@{avg_pm:.3f} | "
            f"frais {fee_lm + fee_pm:.2f}$ | mise {cost:.2f}$ | garanti si meme resolution "
            f"{q - cost + unwind_pnl:+.2f}$ (signal {c['edge']:+.3f}/part, {c.get('edge_h', 0):+.3f} apres 40kg, "
            f"latence {lat1:.2f}+{lat2:.2f}s{', GROS arb' if high else ''})"
            + (f" | reliquat LM revendu {unwind_pnl:+.2f}$" if unwind_pnl else "")
            + f" | portefeuille paper {pv:.2f}$ (engage {engaged:.2f}$)")
        self.t._save()

    def _execute_real(self, w, c):
        """EXECUTION REELLE v2 (audit Claude 29/09, AVANT tout argent reel). Meme
        sequence que le paper, blindee :
          - UN SEUL arb reel a la fois (verrou) : deux executions simultanees
            liraient le meme solde et depenseraient deux fois le meme argent ;
          - jambe Polymarket = NOMBRE EXACT de parts obtenues sur Limitless (FAK
            plafonne, arrondi vers le bas au tick) -- l'ancien snipe_buy prenait un
            montant en $ et pouvait acheter PLUS de parts que la jambe LM ;
          - toute erreur reseau est rattrapee par une lecture du solde reel ;
          - TOUTE part achetee est enregistree (paire, reliquat nu, jambe gardee) :
            rien ne peut echapper a la resolution / au redeem."""
        if not self._real_lock.acquire(blocking=False):
            self.t._tlog("xarb_real_busy", f"⏳ [XARB][REEL] {w.label} : un arb reel est deja en cours -> signal ignore")
            return
        try:
            self._execute_real_locked(w, c)
        except Exception as e:   # jamais silencieux : une exception ici peut laisser une jambe nue
            self._log(f"🚨 [XARB][REEL] {w.label} EXCEPTION pendant l'execution : {type(e).__name__}: {str(e)[:200]} "
                      f"-> verifier les positions a la main")
            self.real_errors.append({"ts": time.time(), "label": w.label, "err": f"{type(e).__name__}: {str(e)[:200]}"})
        finally:
            self._real_lock.release()

    def _pm_buy_exact(self, live, tok, cap, shares):
        """Achat Polymarket de `shares` parts EXACTEMENT (FAK = rempli ou tue, jamais
        pose), prix max `cap` arrondi vers le BAS au tick. Fill confirme par le solde
        on-chain (le retour HTTP peut mentir ou lever apres un fill). -> (parts, prix moyen)"""
        from py_clob_client_v2 import OrderArgsV2, OrderType
        px = math.floor(min(cap, 0.99) * 100 + 1e-9) / 100
        qty = math.floor(shares * 100 + 1e-9) / 100
        if px < 0.01 or qty < 1:
            return 0.0, 0.0, "plafond/quantite trop bas"
        cl = live.client()
        before = live.position_size(tok)
        before = before if before >= 0 else 0.0
        resp, err = None, None
        try:
            resp = cl.post_order(cl.create_order(OrderArgsV2(token_id=tok, price=px, size=qty, side="BUY")), OrderType.FAK)
        except Exception as e:   # un fill a pu passer avant l'erreur -> on lit le solde quand meme
            err = str(e)[:160]
        got, t_end = 0.0, time.time() + self.PM_FILL_WAIT_S
        while time.time() < t_end:
            time.sleep(0.5)
            after = live.position_size(tok)
            if after >= 0:
                got = round(after - before, 4)
                if got >= qty - 0.01:
                    break
        got = max(0.0, got)
        avg = px
        try:   # prix moyen reel si la reponse le donne (BUY : making = USDC, taking = parts)
            mk, tk = float((resp or {}).get("makingAmount") or 0), float((resp or {}).get("takingAmount") or 0)
            if mk > 0 and tk > 0:
                avg = round(mk / tk, 4)
        except (TypeError, ValueError, AttributeError):
            pass
        return got, avg, err

    def _record_real(self, w, c, t0, book, q_lm, avg_lm, fee_lm_ps, q_pm, avg_pm, unwind, note):
        p = w.p
        q = min(q_lm, q_pm)
        cost = (q_pm * (avg_pm + pm_fee_per_share(avg_pm, p["pm_fee_rate"], p["pm_fee_exp"])) if q_pm else 0.0) \
            + q_lm * (avg_lm + fee_lm_ps)
        pid = uuid.uuid4().hex[:10]
        try:
            cond = (self._lm.market(p["lm_slug"]) or {}).get("conditionId")
        except Exception:
            cond = None
        if abs(q_lm - q_pm) >= 0.5:
            self._mark_naked(p["lm_slug"])
        self.open[pid] = {
            "id": pid, "symbol": w.coin, "book": book, "label": w.label, "pm_yes_idx": p.get("pm_yes_idx"),
            "slug": p["pm_slug"], "lm_slug": p["lm_slug"], "side": "ARB",
            "mode": "real", "strat": "xarb_lm_pm" if q_pm > 0 else "xarb_lm_naked", "dir": c["dir"],
            "lm_side": c["lm_side"], "pm_side": c["pm_side"], "filled_shares": round(q, 3),
            "entry_price": round(avg_lm + (avg_pm or 0), 4), "cost": round(cost, 3),
            "legs": {"lm": {"side": c["lm_side"], "shares": round(q_lm, 4), "avg": round(avg_lm, 4)},
                     "pm": {"side": c["pm_side"], "shares": round(q_pm, 4), "avg": round(avg_pm or 0, 4)}},
            "unwind_pnl": round(unwind, 3), "opened_ts": t0, "end_ts": p["end_ts"], "bucket": w.bucket,
            "condition_id": cond, "note": note,
            # token Polymarket de la jambe : la reconciliation du moteur doit le
            # reconnaitre comme DEJA suivi (sinon elle l'adopte en "orphan")
            "pm_token": p["pm_down_token"] if c["pm_side"] == "DOWN" else p["pm_up_token"],
            "lm_token": str((p.get("lm_tokens") or {}).get("yes" if c["lm_side"] == "YES" else "no") or ""),
            "signal": {k: c.get(k) for k in ("lm_px", "pm_px", "lm_top", "pm_top", "size", "edge", "edge_h")},
        }
        self.stats["trades"] += 1
        self.t._save()
        return pid, q, cost

    def _execute_real_locked(self, w, c):
        from limitless.client import BUY, SELL
        t0 = time.time()
        p = w.p
        sh = getattr(w, "sh", None) or self.sh
        book = self._book(w)
        live = getattr(self.t, "_live", None)
        if live is None or book is None:
            self._log("⛔ [XARB][REEL] client Polymarket indisponible -> abandon")
            return
        if self.real_killed():
            self.t._tlog("xarb_real_killed", f"⛔ [XARB][REEL] coupe-circuit actif ({self.kill_reason}) -> signal ignore")
            return
        try:
            lm_usdc = (self._lm.balances() or {}).get("usdc", 0.0)
        except Exception:
            lm_usdc = 0.0
        pm_cash, _ = self.t._read_cash()
        pm_cash = pm_cash or 0.0
        locked = self._bank_locked("real")   # retraits / coffre de l'onglet BANQUE : hors jeu
        scale = min(1.0, max(0.25, c["edge"] / 0.05))
        high = c.get("edge_h", 0) >= self.HIGH_EDGE_H
        tier = self.TIERS["high" if high else "low"]
        stake = tier["per_arb"] * max(0.0, lm_usdc + pm_cash - locked) * (1.0 if high else scale)   # suit le compte reel
        if self.MAX_STAKE_USD > 0:
            stake = min(stake, self.MAX_STAKE_USD * (tier["stake_mult"] if high else 1.0))
        # chaque jambe doit pouvoir etre payee sur SA plateforme (le capital est coupe en 2)
        target = min(c["size"], stake / max(0.01, c["lm_top"] + c["pm_top"] + self.PAIR_SLACK),
                     0.95 * lm_usdc / max(0.01, c["lm_top"] + c.get("edge_h", 0) / 2),
                     0.95 * pm_cash / max(0.01, 1 - c["lm_top"]))
        target = math.floor(target * 100) / 100
        if target < p.get("pm_min_order", 5):
            self.t._tlog("xarb_real_funds", f"⏭️ [XARB][REEL] {w.label} fonds insuffisants pour {p.get('pm_min_order', 5):.0f} "
                         f"parts (LM {lm_usdc:.2f}$ / PM {pm_cash:.2f}$)")
            return
        risky, why_r = self._risky(w, c)
        if risky:
            return self._execute_real_pm_first(w, c, p, sh, book, live, target, t0, why_r)
        if self.LM_FIRST_CAP:   # desactive par defaut (boule de neige)
            target = math.floor(min(target, max(20.0, self.LM_FIRST_MAX_PCT * (lm_usdc + pm_cash - locked))
                                    / max(0.01, c["lm_top"])) * 100) / 100
        if target < p.get("pm_min_order", 5):
            return
        slack = max(0.0, c.get("edge_h", c["edge"])) / 2
        cap_lm = round(c["lm_top"] + slack, 3)
        outcome = "yes" if c["lm_side"] == "YES" else "no"
        coid = "xarb-" + uuid.uuid4().hex[:16]
        try:
            self._lm.place_order(p["lm_slug"], outcome, BUY, cap_lm, target, order_type="FAK", client_order_id=coid)
        except Exception as e:
            self._log(f"⚠️ [XARB][REEL] {w.label} ordre Limitless refuse : {str(e)[:160]} -> 0$ engage")
            return
        q_lm, usd_lm, st = self._lm.wait_fill(coid, timeout_s=3.0 + (p.get("lm_taker_delay_ms") or 500) / 1000)
        if q_lm <= 0:
            self._log(f"🫥 [XARB][REEL] {w.label} Limitless {st} : 0 part -> 0$ engage")
            return
        avg_lm = usd_lm / q_lm
        fee_lm_ps = avg_lm * lm_buy_fee_pct(avg_lm, getattr(sh, "fee_bps", 300)) / 100
        if q_lm < p.get("pm_min_order", 5):
            # reliquat sous le minimum Polymarket : non couvrable -> ENREGISTRE (resolution + redeem)
            self._record_real(w, c, t0, book, q_lm, avg_lm, fee_lm_ps, 0.0, 0.0, 0.0, "reliquat LM < min PM")
            self._log(f"🧷 [XARB][REEL] {w.label} Limitless {st} : {q_lm:.2f} parts seulement (< min PM) -> gardees "
                      f"nues jusqu'a resolution ({q_lm * (avg_lm + fee_lm_ps):.2f}$)")
            return
        cap_pm = 1 - avg_lm - fee_lm_ps - pm_fee_per_share(0.5, p["pm_fee_rate"], p["pm_fee_exp"])
        pm_tok = p["pm_down_token"] if c["pm_side"] == "DOWN" else p["pm_up_token"]
        with self.t._order_lock:
            q_pm, avg_pm, pm_err = self._pm_buy_exact(live, pm_tok, cap_pm, q_lm)
        if pm_err:
            self._log(f"⚠️ [XARB][REEL] {w.label} Polymarket : {pm_err} (solde relu : {q_pm:.2f} parts)")
        q = min(q_lm, q_pm)
        excess = q_lm - q
        unwind, sold = 0.0, 0.0
        if excess >= 1:
            ob = self._lm.orderbook(p["lm_slug"])
            bids = ob["yes_bids"] if outcome == "yes" else ob["no_bids"]
            keep, v_sell, v_hold = self._hold_or_sell(p, c["lm_side"], bids, excess, sh)
            if keep:
                self._log(f"🧷 [XARB][REEL] {w.label} {excess:.1f} parts LM nues GARDEES jusqu'a resolution "
                          f"(revente {v_sell:.2f}$ < valeur Polymarket {v_hold:.2f}$)")
            elif bids:
                coid2 = "xarb-u-" + uuid.uuid4().hex[:12]
                try:
                    self._lm.place_order(p["lm_slug"], outcome, SELL, max(0.01, round(bids[0][0] - 0.02, 3)),
                                         math.floor(excess * 100) / 100, order_type="FAK", client_order_id=coid2)
                    sold, usd_s, _ = self._lm.wait_fill(coid2, timeout_s=3.5)
                    unwind = usd_s - sold * (avg_lm + fee_lm_ps)
                except Exception as e:
                    self._log(f"🚨 [XARB][REEL] {w.label} debouclage Limitless echoue ({str(e)[:100]}) : "
                              f"{excess:.1f} parts NUES gardees")
        lm_kept = q_lm - sold
        if lm_kept < 0.01 and q_pm < 0.01:
            self._log(f"🦺 [XARB][REEL] {w.label} PM non servi -> jambe LM revendue {unwind:+.2f}$")
            self.real_pnl_day(unwind)
            return
        pid, q, cost = self._record_real(w, c, t0, book, lm_kept, avg_lm, fee_lm_ps, q_pm, avg_pm, unwind,
                                         "paire" if abs(lm_kept - q_pm) < 0.01 else "paire + jambe nue")
        if unwind:
            self.real_pnl_day(unwind)
        self._log(f"💥 [XARB][REEL] {w.label} {_hm(p['end_ts'])} LM {c['lm_side']} {q_lm:.2f}@{avg_lm:.3f} + "
                  f"PM {c['pm_side']} {q_pm:.2f}@{avg_pm:.3f} | mise {cost:.2f}$ | garanti {q - cost + unwind:+.2f}$"
                  + (f" | {lm_kept - q_pm:+.2f} parts LM nues" if abs(lm_kept - q_pm) >= 0.01 else ""))

    def _book(self, w):
        """Marche du moteur qui porte le trade (solde paper, historique). Cryptos :
        le leur. Evenements (sport, politique...) : le marche paper le mieux
        dote -- le portefeuille paper est la SOMME des 6 soldes, donc le PnL
        compte pareil ; le libelle reel est garde dans pos["symbol"]/["label"]."""
        mks = self.t.state.get("markets", {})
        if w.coin in mks:
            return w.coin
        from real_web.trader import SYMBOLS
        cands = [s_ for s_ in SYMBOLS if s_ in mks]
        return max(cands, key=lambda s_: float(mks[s_].get("paper_balance") or 0)) if cands else None

    @staticmethod
    def _pm_live_sport(p):
        st = p.get("live_start_ts")
        return bool(p.get("sport")) and bool(st) and time.time() >= float(st)

    def _hold_or_sell(self, p, lm_side, lm_bids, qty, sh=None):
        """Jambe Limitless NUE (Polymarket non servi) : la revendre au bid LM ou
        la GARDER jusqu'a la resolution ? (Claude 28/09 : BTC 5m a T-12s, 20 parts
        payees 0,259 bradees a ~0 = -5,12$ alors que Polymarket valorisait encore
        ce cote ~0,25.) Valeur de garde = ce que Polymarket paierait TOUT DE SUITE
        pour le meme resultat (son BID, pas le milieu : prudent). On garde si ca
        vaut plus que la revente LM (bid - frais). -> (garder?, val_vente, val_garde)"""
        tok = p["pm_up_token"] if lm_side == "YES" else p["pm_down_token"]
        try:
            pm_bids = ((sh or self.sh)._pm_books([tok]).get(tok) or {}).get("bids") or []
        except Exception:
            pm_bids = []
        sold, px = _walk(lm_bids or [], qty)
        sell_val = sold * (px or 0) * 0.985
        got, px2 = _walk(pm_bids, qty)
        hold_val = got * (px2 or 0)
        return hold_val > sell_val, sell_val, hold_val

    # ── POLYMARKET D'ABORD pour les trades a risque (Claude 29/09) ──
    def _risky(self, w, c):
        """-> (vrai, raison) si une jambe LM nue coulerait cher : fin de fenetre
        5m/15m, prix LM extreme, ou Polymarket en mouvement au signal."""
        tl = w.t_left(time.time())
        if self.PM_FIRST_ALWAYS:
            return True, "systematique"
        if not self.PM_FIRST_RISKY:
            return False, ""
        if w.bucket in ("5-min", "15-min") and tl < self.RISKY_TLEFT_S:
            return True, f"fin de fenetre T-{int(tl)}s"
        if not (self.RISKY_LM_LO <= c["lm_top"] <= self.RISKY_LM_HI):
            return True, f"prix LM extreme {c['lm_top']:.3f}"
        if c.get("pm_move") is not None and c["pm_move"] > (c.get("lm_move") or 0) + 0.01:
            return True, f"Polymarket en mouvement ({c['pm_move']:.3f} en 3 s)"
        return False, ""

    @staticmethod
    def _lm_cap_for(avg_pm, fee_pm_ps, fee_bps, lm_fee=True):
        """Prix LM max tel que la paire reste <= 1$ (frais compris)."""
        x = 1 - avg_pm - fee_pm_ps
        for _ in range(4):
            x = 1 - avg_pm - fee_pm_ps - ((x * lm_buy_fee_pct(x, fee_bps) / 100) if lm_fee else 0.0)
        return max(0.0, x)

    def _execute_paper_pm_first(self, w, c, p, sym, sh, book, fee_bps, lat_floor, high, max_pair, target, t0, why):
        rate, expn = p["pm_fee_rate"], p["pm_fee_exp"]
        pmf = lambda x: pm_fee_per_share(x, rate, expn)  # noqa: E731
        lmf = (lambda x: x * lm_buy_fee_pct(x, fee_bps) / 100) if p.get("lm_fee", True) else (lambda x: 0.0)
        pm_tok = p["pm_down_token"] if c["pm_side"] == "DOWN" else p["pm_up_token"]
        slack = max(0.0, c.get("edge_h", c["edge"])) / 2
        cap_pm = round(c["pm_top"] + slack, 3)
        pmin = p.get("pm_min_order", 5)
        # jambe 1 : POLYMARKET (pire prix apres latence)
        lat1 = random.uniform(lat_floor, max(lat_floor, 1.0))
        if self._pm_live_sport(p):
            lat1 += self.PM_SPORT_LIVE_DELAY_S
        time.sleep(lat1)
        asks = (sh._pm_books([pm_tok]).get(pm_tok) or {}).get("asks") or []
        now_pm = time.time()
        hist = [(ts, qq) for ts, qq in self.pm_taken.get(pm_tok, []) if now_pm - ts < self.PM_REFILL_S]
        skip, avail = sum(qq for _, qq in hist), []
        for px, sz in asks:
            if px > cap_pm:
                break
            take = min(sz, skip)
            skip -= take
            if sz - take > 0:
                avail.append((px, sz - take))
        q_pm, avg_pm = _walk(avail, target)
        if q_pm < pmin:
            self.stats["skipped"] += 1
            self._log(f"🫥 [XARB][PAPER] {w.label} {_hm(p['end_ts'])} (PM d'abord : {why}) Polymarket non servi "
                      f"sous {cap_pm:.3f} ({q_pm:.1f} parts) -> rien engage, 0$ perdu")
            return
        self.pm_taken[pm_tok] = hist + [(now_pm, q_pm)]
        fee_pm_ps = pmf(avg_pm)
        # jambe 2 : LIMITLESS, plafonnee a l'equilibre de la paire
        lat2 = random.uniform(lat_floor, max(lat_floor, 1.0)) + (p.get("lm_taker_delay_ms") or 500) / 1000.0
        time.sleep(lat2)
        lm = sh._lm_book(p["lm_slug"], time.time()) or self._lm.orderbook(p["lm_slug"])
        lm_asks = lm["yes_asks"] if c["lm_side"] == "YES" else lm["no_asks"]
        cap_lm = self._lm_cap_for(avg_pm, fee_pm_ps, fee_bps, p.get("lm_fee", True))
        key = (p["lm_slug"], c["dir"])
        now_lm = time.time()
        hist_lm = [(ts, qq) for ts, qq in self.taken.get(key, []) if now_lm - ts < self.LM_REFILL_S]
        skip, avail_lm = sum(qq for _, qq in hist_lm), []
        for px, sz in lm_asks:
            if px > cap_lm:
                break
            take = min(sz, skip)
            skip -= take
            if sz - take > 0:
                avail_lm.append((px, sz - take))
        q_lm, avg_lm = _walk(avail_lm, q_pm)
        q = q_lm if q_lm >= 1 else 0.0
        avg_lm = avg_lm or 0.0
        if q > 0:
            self.taken[key] = hist_lm + [(now_lm, q)]
        # excedent PM revendu tout de suite au pire bid (apres latence)
        excess = q_pm - q
        unwind = 0.0
        if excess > 1e-6:
            time.sleep(lat_floor)
            bids = (sh._pm_books([pm_tok]).get(pm_tok) or {}).get("bids") or []
            sold, px = _walk(bids, excess)
            px = px or 0.0
            unwind = sold * (px - pmf(px)) - excess * (avg_pm + fee_pm_ps)   # invendu = perdu (pessimiste)
            self.stats["unwinds"] += 1
        latency = lat1 + lat2
        if q <= 0:
            self.stats["skipped"] += 1
            self._log(f"🦺 [XARB][PAPER] {w.label} {_hm(p['end_ts'])} (PM d'abord : {why}) Limitless non servi sous "
                      f"{cap_lm:.3f} -> jambe PM {q_pm:.1f}@{avg_pm:.3f} REVENDUE {unwind:+.2f}$ (perte bornee)")
            self._book_unwind_only(w, c, unwind, latency)
            return
        fee_lm_ps = lmf(avg_lm)
        cost = q * (avg_lm + fee_lm_ps + avg_pm + fee_pm_ps)
        pid = uuid.uuid4().hex[:10]
        pos = {
            "id": pid, "symbol": sym, "book": book, "label": w.label, "pm_yes_idx": p.get("pm_yes_idx"),
            "pm_token": pm_tok,
            "lm_token": str((p.get("lm_tokens") or {}).get("yes" if c["lm_side"] == "YES" else "no") or ""),
            "slug": p["pm_slug"], "lm_slug": p["lm_slug"], "side": "ARB",
            "mode": "paper", "strat": "xarb_lm_pm", "dir": c["dir"], "lm_side": c["lm_side"],
            "pm_side": c["pm_side"], "filled_shares": round(q, 3), "entry_price": round(avg_lm + avg_pm, 4),
            "cost": round(cost, 3), "fees_venues": round(q * (fee_lm_ps + fee_pm_ps), 3),
            "legs": {"lm": {"side": c["lm_side"], "shares": round(q, 4), "avg": round(avg_lm, 4)},
                     "pm": {"side": c["pm_side"], "shares": round(q, 4), "avg": round(avg_pm, 4)}},
            "naked_lm": 0.0, "unwind_pnl": round(unwind, 3), "opened_ts": t0, "caps": [round(cap_lm, 3), cap_pm],
            "end_ts": p["end_ts"], "t_left_s": int(p["end_ts"] - t0), "latency_s": round(latency, 3),
            "signal": {k: c.get(k) for k in ("lm_px", "pm_px", "lm_top", "pm_top", "size", "edge", "edge_h", "usd", "usd_h")},
            "bucket": w.bucket, "tier": "high" if high else "low", "max_pair": round(max_pair, 3),
            "order": "pm_first", "risk": why,
        }
        self.open[pid] = pos
        self.stats["trades"] += 1
        pv, _, engaged = self.t._portfolio_value("paper")
        self._log(
            f"⚡ [XARB][PAPER] {w.label} {_hm(p['end_ts'])} ({_tleft(pos['t_left_s'])}) PM D'ABORD ({why}) "
            f"1) PM {c['pm_side']} {q_pm:.1f}@{avg_pm:.3f} -> 2) LM {c['lm_side']} {q:.1f}@{avg_lm:.3f} | "
            f"mise {cost:.2f}$ | garanti si meme resolution {q - cost + unwind:+.2f}$ "
            f"(signal +{c['edge']:.3f}/part, {c.get('edge_h', 0):+.3f} apres 40kg)"
            + (f" | excedent PM revendu {unwind:+.2f}$" if unwind else "")
            + f" | portefeuille paper {pv:.2f}$ (engage {engaged:.2f}$)")
        self.t._save()

    def _execute_real_pm_first(self, w, c, p, sh, book, live, target, t0, why):
        """Reel, trade a risque : PM (nb exact de parts) -> LM plafonne a l'equilibre ;
        l'excedent PM est revendu tout de suite (FAK) : perte bornee a l'ecart."""
        from limitless.client import BUY
        rate, expn = p["pm_fee_rate"], p["pm_fee_exp"]
        pm_tok = p["pm_down_token"] if c["pm_side"] == "DOWN" else p["pm_up_token"]
        slack = max(0.0, c.get("edge_h", c["edge"])) / 2
        cap_pm = c["pm_top"] + slack
        with self.t._order_lock:
            q_pm, avg_pm, err = self._pm_buy_exact(live, pm_tok, cap_pm, target)
        if q_pm < 1:
            self._log(f"🫥 [XARB][REEL] {w.label} (PM d'abord : {why}) Polymarket non servi"
                      + (f" ({err})" if err else "") + " -> 0$ engage")
            return
        fee_pm_ps = pm_fee_per_share(avg_pm, rate, expn)
        fee_bps = getattr(sh, "fee_bps", 300)
        cap_lm = round(self._lm_cap_for(avg_pm, fee_pm_ps, fee_bps, p.get("lm_fee", True)), 3)
        outcome = "yes" if c["lm_side"] == "YES" else "no"
        q_lm = usd_lm = 0.0
        st = "non envoye"
        if cap_lm >= 0.01:
            coid = "xarb-" + uuid.uuid4().hex[:16]
            try:
                self._lm.place_order(p["lm_slug"], outcome, BUY, cap_lm, math.floor(q_pm * 100) / 100,
                                     order_type="FAK", client_order_id=coid)
                q_lm, usd_lm, st = self._lm.wait_fill(coid, timeout_s=3.0 + (p.get("lm_taker_delay_ms") or 500) / 1000)
            except Exception as e:
                st = f"refuse ({str(e)[:80]})"
        avg_lm = usd_lm / q_lm if q_lm else 0.0
        fee_lm_ps = avg_lm * lm_buy_fee_pct(avg_lm, fee_bps) / 100 if q_lm else 0.0
        excess = q_pm - q_lm
        unwind, sold = 0.0, 0.0
        if excess >= 0.01:
            bids = (sh._pm_books([pm_tok]).get(pm_tok) or {}).get("bids") or []
            got, px = _walk(bids, excess)
            level, cum = 0.01, 0.0   # prix du dernier niveau de bid necessaire pour tout revendre
            for b_px, b_sz in bids:
                level = b_px
                cum += b_sz
                if cum >= excess:
                    break
            before = live.position_size(pm_tok)
            before = before if before >= 0 else 0.0
            try:
                with self.t._order_lock:
                    live.sell_position(pm_tok, max(0.01, round(level - 0.01, 2)), math.floor(excess * 100) / 100,
                                       aggressive=True, marge=0.0)
            except Exception as e:
                self._log(f"🚨 [XARB][REEL] {w.label} revente PM echouee ({str(e)[:100]}) : {excess:.2f} parts PM nues")
            t_end = time.time() + self.PM_FILL_WAIT_S
            while time.time() < t_end:
                time.sleep(0.5)
                after = live.position_size(pm_tok)
                if after >= 0:
                    sold = max(0.0, round(before - after, 4))
                    if sold >= math.floor(excess * 100) / 100 - 0.01:
                        break
            px_s = px or level
            unwind = sold * (px_s - pm_fee_per_share(px_s, rate, expn)) - sold * (avg_pm + fee_pm_ps)
        pm_kept = q_pm - sold
        if q_lm < 0.01 and pm_kept < 0.01:
            self._log(f"🦺 [XARB][REEL] {w.label} (PM d'abord : {why}) Limitless {st} -> jambe PM revendue "
                      f"{unwind:+.2f}$ (perte bornee)")
            self.real_pnl_day(unwind)
            return
        pid, q, cost = self._record_real(w, c, t0, book, q_lm, avg_lm, fee_lm_ps, pm_kept, avg_pm, unwind,
                                         f"PM d'abord ({why})")
        self.open[pid]["order"] = "pm_first"
        if unwind:
            self.real_pnl_day(unwind)
        self._log(f"💥 [XARB][REEL] {w.label} PM D'ABORD ({why}) PM {c['pm_side']} {q_pm:.2f}@{avg_pm:.3f} + "
                  f"LM {c['lm_side']} {q_lm:.2f}@{avg_lm:.3f} | mise {cost:.2f}$ | garanti {q - cost + unwind:+.2f}$"
                  + (f" | excedent PM revendu {unwind:+.2f}$" if unwind else ""))

    # ── JAMBES NUES : exposition, blocage, RECOUVREMENT (Claude 29/09) ──
    @staticmethod
    def _naked_qty(pos):
        lg = pos.get("legs") or {}
        return float((lg.get("lm") or {}).get("shares") or 0) - float((lg.get("pm") or {}).get("shares") or 0)

    def _naked_exposure(self, mode):
        tot = 0.0
        for x in self.open.values():
            if (x.get("mode") == "real") != (mode == "real"):
                continue
            nq = self._naked_qty(x)
            if nq >= 0.5:
                tot += nq * float((x.get("legs") or {}).get("lm", {}).get("avg") or 0)
        return round(tot, 2)

    def _capital(self, mode):
        try:
            return self._real_capital() if mode == "real" else self.t._portfolio_value("paper")[0]
        except Exception:
            return 0.0

    def _mark_naked(self, lm_slug):
        self.naked_block[lm_slug] = time.time() + self.NAKED_BLOCK_S

    def _pair_for(self, lm_slug):
        cands = [self.sh]
        try:
            from limitless import api as _api
            if _api._shadow_events is not None:
                cands.append(_api._shadow_events)
        except Exception:
            pass
        for sh in cands:
            p = (getattr(sh, "pf", None) and sh.pf.pairs.get(lm_slug)) or None
            if p:
                return p
            w = getattr(sh, "windows", {}).get(lm_slug)
            if w is not None:
                return w.p
        return None

    def _try_rehedge(self, pid, pos):
        """Jambe LM nue : si Polymarket repasse SOUS le prix d'equilibre (marge
        REHEDGE_MARGIN, et en paper apres le handicap 6 %), on achete la jambe PM
        manquante -> la position devient une PAIRE a gain verrouille.
        Paper : pire prix apres latence (sac de 40 kg) ; reel : FAK nb exact de parts."""
        naked = self._naked_qty(pos)
        if naked < 1 or time.time() > pos["end_ts"] - 5:
            return
        pr = self._pair_for(pos["lm_slug"]) or {}
        tok = pos.get("pm_token") or (pr.get("pm_down_token") if pos["pm_side"] == "DOWN" else pr.get("pm_up_token"))
        if not tok:
            return
        rate = pr.get("pm_fee_rate", 0.07)
        expn = pr.get("pm_fee_exp", 1.0)
        lg = pos["legs"]
        avg_lm = float(lg["lm"]["avg"])
        fee_lm_ps = avg_lm * lm_buy_fee_pct(avg_lm, getattr(self.sh, "fee_bps", 300)) / 100
        cap = 1 - avg_lm - fee_lm_ps - pm_fee_per_share(0.5, rate, expn) - self.REHEDGE_MARGIN
        if pos.get("mode") == "paper":   # la paire doit rester gagnante APRES les 6 % du handicap
            cap = (1 - avg_lm - fee_lm_ps - pm_fee_per_share(0.5, rate, expn) - 0.06 * avg_lm) / 1.06 - self.REHEDGE_MARGIN
        if cap < 0.01:
            return
        try:
            asks = (self.sh._pm_books([tok]).get(tok) or {}).get("asks") or []
        except Exception:
            return
        if not asks or asks[0][0] > cap:
            return
        if pos.get("mode") == "real":
            live = getattr(self.t, "_live", None)
            if live is None or not self._real_lock.acquire(blocking=False):
                return
            try:
                with self.t._order_lock:
                    q, avg, err = self._pm_buy_exact(live, tok, cap, naked)
            finally:
                self._real_lock.release()
        else:
            lat_floor = self.t._paper_latency_floor()
            time.sleep(random.uniform(lat_floor, max(lat_floor, 1.0)))
            try:
                asks = (self.sh._pm_books([tok]).get(tok) or {}).get("asks") or []
            except Exception:
                return
            now_pm = time.time()
            hist = [(ts, qq) for ts, qq in self.pm_taken.get(tok, []) if now_pm - ts < self.PM_REFILL_S]
            skip, avail = sum(qq for _, qq in hist), []
            for px, sz in asks:
                if px > cap:
                    break
                take = min(sz, skip)
                skip -= take
                if sz - take > 0:
                    avail.append((px, sz - take))
            q, avg = _walk(avail, naked)
            avg = avg or 0.0
            if q >= 1:
                self.pm_taken[tok] = hist + [(now_pm, q)]
        if q < 1:
            return
        from real_web.trader import _STATE_SAVE_LOCK
        with _STATE_SAVE_LOCK:
            old_q = float(lg["pm"]["shares"] or 0)
            old_avg = float(lg["pm"]["avg"] or 0)
            new_q = old_q + q
            lg["pm"]["shares"] = round(new_q, 4)
            lg["pm"]["avg"] = round((old_q * old_avg + q * avg) / new_q, 4)
            pos["cost"] = round(pos["cost"] + q * (avg + pm_fee_per_share(avg, rate, expn)), 3)
            pos["filled_shares"] = round(min(float(lg["lm"]["shares"]), new_q), 3)
            pos["rehedged"] = round(float(pos.get("rehedged") or 0) + q, 3)
            pos["naked_lm"] = round(max(0.0, float(lg["lm"]["shares"]) - new_q), 3)
            if pos["naked_lm"] < 0.01:
                pos["strat"] = "xarb_lm_pm"
            pos["pm_token"] = tok
            self.stats["rehedged"] = self.stats.get("rehedged", 0) + 1
            try:
                self.t._save()
            except Exception:
                pass
        lock = q * (1 - avg_lm - fee_lm_ps - avg - pm_fee_per_share(avg, rate, expn))
        self._log(f"🔒 [XARB][{pos.get('mode', 'paper').upper()}] {pos.get('label', pos['symbol'])} jambe nue RECOUVERTE : "
                  f"PM {pos['pm_side']} {q:.1f}@{avg:.3f} (plafond {cap:.3f}) -> paire {avg_lm + avg:.3f} "
                  f"= +{lock:.2f}$ verrouilles" + (f" | reste {pos['naked_lm']:.1f} parts nues" if pos["naked_lm"] >= 0.01 else ""))

    def _book_unwind_only(self, w, c, unwind_pnl, latency):
        """Aucune paire formee mais une jambe nue revendue : la perte est un
        vrai trade (sinon le paper cacherait le risque de jambe)."""
        book = self._book(w)
        mk = self.t.state["markets"][book]
        pos = {"id": uuid.uuid4().hex[:10], "symbol": w.coin, "book": book, "label": w.label,
               "slug": w.p["pm_slug"], "lm_slug": w.p["lm_slug"],
               "side": "ARB", "mode": "paper", "strat": "xarb_lm_pm_unwind", "dir": c["dir"],
               "filled_shares": 0.0, "entry_price": None, "cost": 0.0, "opened_ts": time.time(),
               "end_ts": w.p["end_ts"], "latency_s": round(latency, 3), "resolved_by": "xarb_unwind"}
        pnl = self.t._paper_close_handicap(mk, pos, round(unwind_pnl, 3))
        pos.update(pnl=pnl, win=pnl > 0)
        mk["trades"].append(pos)

    # ── resolution ─────────────────────────────────────────────────────
    def _pm_outcome(self, slug, yes_idx=None, need_closed=False):
        """-> "UP" (issue appariee au YES Limitless gagne), "DOWN", "SPLIT" (50/50)
        ou None. Marches EVENEMENTS (need_closed) : on exige le marche CLOS -- un
        prix a 0,995 avant resolution officielle n'est PAS un resultat."""
        # Gamma NE RENVOIE PLUS un marche clos sans closed=true (bug du 28/09 :
        # 14 arbs bloques 40-90 min, capital gele) -> on interroge les deux.
        g = self._http.get(GAMMA, params={"slug": slug}, timeout=8).json()
        if not g:
            g = self._http.get(GAMMA, params={"slug": slug, "closed": "true"}, timeout=8).json()
        if not g:
            return None
        import json as _j
        prices = [float(x) for x in _j.loads(g[0].get("outcomePrices") or "[]")]
        outs = [o.lower() for o in _j.loads(g[0].get("outcomes") or "[]")]
        if not prices:
            return None
        if need_closed and not g[0].get("closed"):
            return None
        if g[0].get("closed") and len(prices) == 2 and abs(prices[0] - 0.5) < 0.01 and abs(prices[1] - 0.5) < 0.01:
            return "SPLIT"
        if max(prices) < 0.99:
            return None
        win = prices.index(max(prices))
        if yes_idx is not None:
            return "UP" if win == int(yes_idx) else "DOWN"
        return "UP" if outs[win] in ("up", "yes") else "DOWN"

    def _lm_outcome(self, slug):
        m = self._lm.market(slug, fresh=True)
        idx = m.get("winningOutcomeIndex")
        if idx is None:
            num = m.get("payoutNumerators") or []
            if len(num) == 2 and num[0] and num[0] == num[1]:
                return "SPLIT"
            return None
        return "UP" if int(idx) == 0 else "DOWN"   # 0 = YES = Up (verifie 28/09)

    def _resolve_loop(self):
        while True:
            time.sleep(self.RESOLVE_EVERY_S)
            now = time.time()
            if (self.mode() == "real" or any(x.get("mode") == "real" for x in self.open.values())) \
                    and now - getattr(self, "_last_health_ts", 0) >= self.HEALTH_EVERY_S:
                self._last_health_ts = now
                try:
                    self.real_health()
                except Exception as e:
                    self.t._tlog("xarb_health_err", f"⚠️ [XARB][SANTE] {str(e)[:150]}")
            for pid, pos in list(self.open.items()):   # RECOUVREMENT des jambes nues encore ouvertes
                if pos.get("mode") in ("paper", "real") and self._naked_qty(pos) >= 1 and now < pos["end_ts"] - 5:
                    try:
                        self._try_rehedge(pid, pos)
                    except Exception as e:
                        self.t._tlog(f"xarb_rehedge_err_{pid}", f"⚠️ [XARB] recouvrement {pos.get('label')}: {str(e)[:150]}")
            for pid, pos in list(self.open.items()):
                if now < pos["end_ts"] + 60:
                    continue
                try:
                    pm_o = self._pm_outcome(pos["slug"], pos.get("pm_yes_idx"), need_closed=pos.get("bucket") == "event")
                    lm_o = self._lm_outcome(pos["lm_slug"])
                except Exception:
                    continue
                if pm_o is None or lm_o is None:
                    # ALERTE : normal = 2-12 min apres la fin ; au-dela de 30 min c'est
                    # anormal (cf. bug Gamma closed=true du 28/09 : capital gele 1h30)
                    # evenements : resolution UMA (>= 2 h) -> alerte seulement apres 48 h
                    if now > pos["end_ts"] + (172800 if pos.get("bucket") == "event" else 1800):
                        self.t._tlog(f"xarb_unres_{pid}", f"⚠️ [XARB] {pos['symbol']} {pos['lm_slug']} NON RESOLU "
                                     f"{int((now - pos['end_ts']) / 60)} min apres la fin (PM={pm_o}, LM={lm_o}) "
                                     f"-> {pos['cost']:.2f}$ geles", every=600.0)
                    continue
                q = pos["filled_shares"]
                lm_win = 0.5 if lm_o == "SPLIT" else float((pos["lm_side"] == "YES") == (lm_o == "UP"))
                pm_win = 0.5 if pm_o == "SPLIT" else float((pos["pm_side"] == "UP") == (pm_o == "UP"))
                legs = pos.get("legs") or {}
                q_lm = (legs.get("lm") or {}).get("shares", q)
                q_pm = (legs.get("pm") or {}).get("shares", q)
                payout = q_lm * lm_win + q_pm * pm_win   # jambe LM nue gardee comprise
                pnl = round(payout - pos["cost"] + pos.get("unwind_pnl", 0.0), 3)
                mk = self.t.state["markets"][pos.get("book") or pos["symbol"]]
                pos.update(outcomes={"pm": pm_o, "lm": lm_o}, payout=round(payout, 3),
                           resolved_by="xarb", exit_price=round(payout / max(q, q_lm), 3) if max(q, q_lm) else None, closed_ts=now)
                # ATOMIQUE vis-a-vis des sauvegardes (Claude 28/09) : credit du solde +
                # trade clos + position retiree + sauvegarde sous le MEME verrou que
                # _save -> un redemarrage ne peut jamais voir un etat a moitie
                # applique (solde credite mais position encore ouverte = double gain).
                from real_web.trader import _STATE_SAVE_LOCK
                with _STATE_SAVE_LOCK:
                    if pos.get("mode") == "paper":
                        pnl = self.t._paper_close_handicap(mk, pos, pnl)
                    else:
                        pos.update(pnl=pnl, win=pnl > 0)
                    mk["trades"].append(pos)
                    self.open.pop(pid, None)
                    try:
                        self.t._save()
                    except Exception:
                        pass
                if pos.get("mode") != "paper" and pos.get("condition_id"):
                    try:   # encaisse la jambe Limitless (Polymarket : reconciliation du moteur)
                        self._lm.redeem_onchain(pos["condition_id"])
                    except Exception as e:
                        self._log(f"⚠️ [XARB][REEL] redeem Limitless : {str(e)[:120]}")
                div = "" if pm_o == lm_o else f" ⚠️ VENUES DIVERGENTES (PM {pm_o} / LM {lm_o})"
                if pos.get("mode") == "real":
                    self.real_pnl_day(pnl)
                    if pm_o != lm_o and "SPLIT" not in (pm_o, lm_o):
                        self._kill(f"venues DIVERGENTES a la resolution de {pos['lm_slug']} (PM {pm_o} / LM {lm_o})")
                self._log(f"🏁 [XARB][{pos.get('mode', 'paper').upper()}] {pos['symbol']} {pos.get('bucket', '')} {pos['lm_slug'][-10:]} resolu "
                          f"PM={pm_o} LM={lm_o} | payout {payout:.2f}$ - mise {pos['cost']:.2f}$ "
                          f"-> PnL {pnl:+.2f}$ apres handicap{div}")
            try:
                self.t._save()
            except Exception:
                pass

    def summary(self):
        return {**self.stats, "mode": self.mode(), "open": len(self.open),
                "engaged": round(sum(p["cost"] for p in self.open.values()), 3),
                "real_guard": dict(self.guard), "real_errors": self.real_errors[-10:],
                "real_health": getattr(self, "last_health", None)}

    # ── COUPE-CIRCUIT REEL (Claude 29/09, "pas le droit a l'erreur") ───────
    @property
    def kill_reason(self):
        return self.guard.get("killed")

    def real_killed(self):
        """Vrai si le coupe-circuit est declenche ; verifie aussi l'exposition
        en jambes nues et la rafale d'erreurs AVANT chaque nouvel arb reel."""
        if self.guard.get("killed"):
            return True
        now = time.time()
        recent = [e for e in self.real_errors if now - e["ts"] < 600]
        if len(recent) >= self.REAL_MAX_ERRORS_10MIN:
            return self._kill(f"{len(recent)} erreurs d'execution en 10 min")
        cap = self._real_capital()
        naked = sum(x["cost"] for x in self.open.values() if x.get("mode") == "real"
                    and abs(float((x.get("legs") or {}).get("lm", {}).get("shares") or 0)
                            - float((x.get("legs") or {}).get("pm", {}).get("shares") or 0)) >= 0.01)
        if cap and naked > self.REAL_MAX_NAKED_PCT * cap:
            self.t._tlog("xarb_real_naked", f"⛔ [XARB][REEL] jambes nues {naked:.2f}$ > "
                         f"{self.REAL_MAX_NAKED_PCT:.0%} du capital ({cap:.2f}$) -> pas de nouvel arb", every=300)
            return True
        return False

    def _real_capital(self):
        try:
            lm = (self._lm.balances() or {}).get("usdc", 0.0)
        except Exception:
            lm = 0.0
        try:
            pm = self.t._read_cash()[0] or 0.0
        except Exception:
            pm = 0.0
        eng = sum(x["cost"] for x in self.open.values() if x.get("mode") == "real")
        return lm + pm + eng

    def real_pnl_day(self, pnl):
        """Cumule le PnL reel du jour (UTC) ; coupe si la perte depasse
        REAL_MAX_DAY_LOSS_PCT du capital du debut de journee (min 5$)."""
        day = time.strftime("%Y-%m-%d", time.gmtime())
        g = self.guard
        if g.get("day") != day:
            g.update(day=day, pnl_day=0.0, cap_day=round(self._real_capital(), 2))
        g["pnl_day"] = round(g.get("pnl_day", 0.0) + pnl, 3)
        lim = max(5.0, self.REAL_MAX_DAY_LOSS_PCT * (g.get("cap_day") or 0))
        if g["pnl_day"] < -lim:
            self._kill(f"perte du jour {g['pnl_day']:.2f}$ > {lim:.2f}$")
        self.t._save()

    def _kill(self, reason):
        if not self.guard.get("killed"):
            self.guard.update(killed=reason, killed_ts=time.time())
            self._log(f"⛔🔥 [XARB][REEL] COUPE-CIRCUIT DECLENCHE : {reason} -> plus AUCUN arb reel "
                      f"jusqu'a remise a zero manuelle (POST /api/limitless/xarb/reset-kill)")
            self.t._save()
        return True

    def real_health(self):
        """SANTE DU REEL (Claude 29/09) :
        1) REPARTITION DU CAPITAL : chaque arb paie sur UNE seule plateforme -> le
           capital migre ; alerte avant qu'un cote ne puisse plus payer sa jambe.
        2) RAPPROCHEMENT ON-CHAIN : parts reellement detenues (Base / Polygon) vs
           ce que le bot croit detenir pour chaque arb reel ouvert."""
        out = {"ts": time.time(), "alerts": []}
        try:
            bal = self._lm.balances() or {}
        except Exception as e:
            bal = {}
            out["alerts"].append(f"soldes Limitless illisibles ({str(e)[:80]})")
        lm, eth = float(bal.get("usdc") or 0), float(bal.get("eth") or 0)
        try:
            pm = float(self.t._read_cash()[0] or 0)
        except Exception:
            pm = 0.0
        tot = lm + pm
        out.update(lm_usdc=round(lm, 2), pm_cash=round(pm, 2), eth_gas=round(eth, 6))
        if tot > 0:
            for name, v in (("Limitless", lm), ("Polymarket", pm)):
                if v < self.MIN_SIDE_PCT * tot or v < 5.0:
                    out["alerts"].append(f"{name} bas : {v:.2f}$ sur {tot:.2f}$ -> REEQUILIBRER (le capital a "
                                         f"migre vers l'autre plateforme)")
            if eth < self.MIN_GAS_ETH:
                out["alerts"].append(f"ETH (gas Base) presque vide : {eth:.6f} -> ordres/redeem Limitless bloques")
        exp_lm, exp_pm = {}, {}
        for x in self.open.values():
            if x.get("mode") != "real":
                continue
            lg = x.get("legs") or {}
            if x.get("lm_token"):
                exp_lm[x["lm_token"]] = exp_lm.get(x["lm_token"], 0.0) + float((lg.get("lm") or {}).get("shares") or 0)
            if x.get("pm_token"):
                exp_pm[x["pm_token"]] = exp_pm.get(x["pm_token"], 0.0) + float((lg.get("pm") or {}).get("shares") or 0)
        live = getattr(self.t, "_live", None)
        for venue, exp, read in (("Limitless", exp_lm, lambda tk: self._lm.token_balance(tk)),
                                 ("Polymarket", exp_pm, lambda tk: live.position_size(tk) if live else -1)):
            for tk, q in exp.items():
                try:
                    have = read(tk)
                except Exception:
                    have = None
                if have is None or have < 0:
                    continue
                if have + 0.01 < q:
                    out["alerts"].append(f"{venue} : {have:.2f} parts detenues < {q:.2f} attendues (token ...{str(tk)[-6:]}) "
                                         f"-> position PERDUE ou vendue hors bot")
        out["n_real_open"] = sum(1 for x in self.open.values() if x.get("mode") == "real")
        self.last_health = out
        for a in out["alerts"]:
            self.t._tlog("xarb_health_" + a[:20], f"🚨 [XARB][REEL][SANTE] {a}", every=1800)
        return out

    def reset_kill(self):
        self.guard.update(killed=None, killed_ts=None)
        self.real_errors.clear()
        self.t._save()
        return dict(self.guard)
