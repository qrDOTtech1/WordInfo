"""BANQUE (Claude 29/09, Steven : "Set and Forget, don't forget ton withdraw !").

Onglet dashboard qui gere l'argent GAGNE comme une banque :
  - combien on peut retirer par jour / semaine (possible et GARANTISSABLE) ;
  - cash disponible, capital engage, coffre (argent mis de cote, hors trading) ;
  - demandes : retrait, depot / allocation de cash, mise au coffre, remise en jeu ;
  - politique "Set and Forget" : chaque jour, X % du benefice de la veille part
    automatiquement au coffre (le reste continue la boule de neige).

PAPER : tout est SIMULE (les soldes paper sont reellement debites/credites,
grand livre ajuste -> aucun faux ecart de rapprochement).
REEL : la banque ne DEPLACE JAMAIS d'argent toute seule. Un retrait = une
RESERVE (le bot n'engage plus cet argent dans de nouveaux arbs) + une consigne ;
c'est Steven qui retire a la main sur Limitless / Polymarket, puis confirme.

Le montant "reserve" (hold) est lu par limitless/xarb.py : l'argent promis a un
retrait n'est jamais remis en jeu, meme si le retrait n'est pas encore faisable
(capital engage dans des arbs en cours) -- il se libere a leur resolution.
"""
import math
import threading
import time
import uuid
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

bp = Blueprint("bank", __name__)
_BANK = None

KINDS = {
    "withdraw": "Retrait",
    "deposit": "Depot / allocation de cash",
    "to_vault": "Mise au coffre",
    "from_vault": "Remise en jeu (coffre -> trading)",
}


def _day(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def _pct(vals, q):
    if not vals:
        return None
    v = sorted(vals)
    k = (len(v) - 1) * q
    f, c = math.floor(k), math.ceil(k)
    return v[f] if f == c else v[f] + (v[c] - v[f]) * (k - f)


class Bank:
    PROCESS_EVERY_S = 10

    def __init__(self, trader, log_fn=None):
        self.t = trader
        self._log = log_fn or print
        self._lock = threading.RLock()
        b = trader.state.setdefault("bank", {})
        for m in ("paper", "real"):
            b.setdefault(m, {"vault": 0.0, "withdrawn": 0.0, "deposited": 0.0})
        b.setdefault("requests", [])
        b.setdefault("movements", [])
        b.setdefault("policy", {"sweep_pct": 0.0, "min_trading": 100.0, "last_sweep_day": None})
        self.b = b
        threading.Thread(target=self._loop, daemon=True, name="bank").start()

    # ── capital ────────────────────────────────────────────────────────
    def hold(self, mode):
        """Argent PROMIS a des retraits/mises au coffre pas encore executes :
        jamais remis en jeu par l'executeur d'arb."""
        return round(sum(r["amount"] for r in self.b["requests"]
                         if r["mode"] == mode and r["status"] == "reserve"
                         and r["kind"] in ("withdraw", "to_vault")), 2)

    def locked(self, mode):
        """Argent que l'executeur d'arb ne doit JAMAIS engager : reserves en attente
        + (reel) le coffre, qui reste physiquement sur les plateformes."""
        return round(self.hold(mode) + (self.b[mode]["vault"] if mode == "real" else 0.0), 2)

    def _real_venues(self):
        lm = pm = None
        try:
            from limitless import xarb as _x
            if _x.EXECUTOR is not None:
                lm = float((_x.EXECUTOR._lm.balances() or {}).get("usdc") or 0)
        except Exception:
            lm = None
        try:
            pm = float(self.t._read_cash()[0] or 0)
        except Exception:
            pm = None
        return lm, pm

    def capital(self, mode):
        """-> dict : trading (portefeuille de trading), engage, libre, reserve,
        coffre, retire, avoirs totaux."""
        acct = self.b[mode]
        if mode == "paper":
            pv, free, engaged = self.t._portfolio_value("paper")
            lm = pm = None
        else:
            lm, pm = self._real_venues()
            engaged = self.t._open_engaged("real")
            free = (lm or 0) + (pm or 0)
            pv = free + engaged
        hold = self.hold(mode)
        vault = acct["vault"]
        return {
            "mode": mode, "trading": round(pv, 2), "engage": round(engaged, 2),
            "libre": round(max(0.0, free - self.locked(mode)), 2), "cash": round(free, 2), "reserve": hold,
            "coffre": round(vault, 2), "retire": round(acct["withdrawn"], 2), "depose": round(acct["deposited"], 2),
            # paper : le coffre est SORTI des soldes -> s'ajoute ; reel : le coffre est une
            # reserve virtuelle sur les plateformes -> deja dans le cash
            "avoirs": round(pv + (vault if mode == "paper" else 0.0), 2),
            "lm_usdc": lm, "pm_cash": pm,
        }

    # ── mouvements paper (soldes reellement debites/credites) ──────────
    def _paper_move(self, delta):
        """delta < 0 : sort du trading ; > 0 : entre. Reparti au prorata des soldes
        des marches paper ; le grand livre est ajuste (pas de faux ecart)."""
        from real_web.trader import SYMBOLS
        mks = self.t.state["markets"]
        bals = {s: max(0.0, float(mks[s].get("paper_balance") or 0)) for s in SYMBOLS}
        tot = sum(bals.values())
        if tot <= 0 and delta < 0:
            return False
        led = (self.t.state.get("ledger") or {}).get("paper") or {}
        for s in SYMBOLS:
            share = delta * (bals[s] / tot if tot > 0 else 1 / len(SYMBOLS))
            mks[s]["paper_balance"] = round(float(mks[s].get("paper_balance") or 0) + share, 4)
            if s in led and led[s].get("start") is not None:
                led[s]["start"] = round(led[s]["start"] + share, 4)
        return True

    def _movement(self, mode, label, amount, kind):
        self.b["movements"].append({"ts": time.time(), "mode": mode, "label": label,
                                    "amount": round(amount, 2), "kind": kind})
        self.b["movements"] = self.b["movements"][-2000:]

    # ── demandes ───────────────────────────────────────────────────────
    def request(self, mode, kind, amount, note=""):
        if mode not in ("paper", "real") or kind not in KINDS:
            return {"ok": False, "error": "mode ou type invalide"}
        try:
            amount = round(float(amount), 2)
        except (TypeError, ValueError):
            return {"ok": False, "error": "montant invalide"}
        if amount <= 0:
            return {"ok": False, "error": "montant doit etre > 0"}
        with self._lock:
            cap = self.capital(mode)
            if kind in ("withdraw", "to_vault") and amount > cap["trading"] + (cap["coffre"] if kind == "withdraw" else 0) + 1e-6:
                return {"ok": False, "error": f"montant > avoirs disponibles ({cap['trading']:.2f}$)"}
            if kind == "from_vault" and amount > self.b[mode]["vault"] + 1e-6:
                return {"ok": False, "error": f"le coffre ne contient que {self.b[mode]['vault']:.2f}$"}
            r = {"id": uuid.uuid4().hex[:8], "ts": time.time(), "mode": mode, "kind": kind, "amount": amount,
                 "status": "reserve", "note": str(note)[:120], "done_ts": None, "log": []}
            self.b["requests"].append(r)
            self._process_one(r)
            self.t._save()
            return {"ok": True, "request": r}

    def _process_one(self, r):
        """Fait avancer une demande. Etats : reserve -> pret (retrait) / fait / annule."""
        mode, kind, amt = r["mode"], r["kind"], r["amount"]
        acct = self.b[mode]
        now = time.time()
        if r["status"] != "reserve":
            return
        if kind == "withdraw":
            # 1) le coffre sert en premier ; 2) le reste doit etre LIBRE (pas engage)
            from_vault = min(acct["vault"], amt)
            rest = amt - from_vault
            free = self.capital(mode)["cash"] - (self.locked(mode) - amt)   # hors autres reserves (et coffre reel)
            if rest > free + 1e-6:
                r["waiting"] = f"attend {rest - max(0.0, free):.2f}$ encore engages dans des arbs"
                return
            if mode == "paper":
                if rest > 0:
                    self._paper_move(-rest)
                acct["vault"] = round(acct["vault"] - from_vault, 4)
                acct["vault"] = round(acct["vault"] + amt, 4)   # tout le montant est pret au coffre
            else:
                acct["vault"] = round(acct["vault"] - from_vault + amt, 4)
            r.update(status="pret", ready_ts=now, waiting=None)
            r["log"].append([now, "pret a retirer"])
            self._movement(mode, f"Retrait {amt:.2f}$ pret (coffre)", -rest, "withdraw_ready")
        elif kind == "to_vault":
            free = self.capital(mode)["cash"] - (self.locked(mode) - amt)
            if amt > free + 1e-6:
                r["waiting"] = f"attend {amt - max(0.0, free):.2f}$ encore engages"
                return
            if mode == "paper":
                self._paper_move(-amt)
            acct["vault"] = round(acct["vault"] + amt, 4)
            r.update(status="fait", done_ts=now, waiting=None)
            self._movement(mode, f"Mise au coffre {amt:.2f}$", -amt, "to_vault")
        elif kind == "from_vault":
            acct["vault"] = round(acct["vault"] - amt, 4)
            if mode == "paper":
                self._paper_move(+amt)
            r.update(status="fait", done_ts=now)
            self._movement(mode, f"Remise en jeu {amt:.2f}$", +amt, "from_vault")
        elif kind == "deposit":
            if mode == "paper":
                self._paper_move(+amt)
                acct["deposited"] = round(acct["deposited"] + amt, 4)
                r.update(status="fait", done_ts=now)
                self._movement(mode, f"Depot {amt:.2f}$", +amt, "deposit")
            else:
                # reel : consigne de depot ; confirmee a la main quand l'argent est arrive
                lm, pm = self._real_venues()
                tot = (lm or 0) + (pm or 0) + amt
                tgt = tot / 2
                r["advice"] = (f"deposer ~{max(0.0, tgt - (lm or 0)):.2f}$ USDC sur Limitless (Base) et "
                               f"~{max(0.0, tgt - (pm or 0)):.2f}$ sur Polymarket pour equilibrer les 2 cotes")
                r.update(status="pret")

    def confirm(self, rid):
        """Retrait : 'j'ai retire' (paper : l'argent sort du coffre ; reel : Steven
        confirme l'avoir retire a la main). Depot reel : 'argent arrive'."""
        with self._lock:
            r = next((x for x in self.b["requests"] if x["id"] == rid), None)
            if not r or r["status"] != "pret":
                return {"ok": False, "error": "demande introuvable ou pas prete"}
            acct = self.b[r["mode"]]
            if r["kind"] == "withdraw":
                acct["vault"] = round(max(0.0, acct["vault"] - r["amount"]), 4)
                acct["withdrawn"] = round(acct["withdrawn"] + r["amount"], 4)
                self._movement(r["mode"], f"Retrait effectue {r['amount']:.2f}$", -r["amount"], "withdrawn")
            elif r["kind"] == "deposit":
                acct["deposited"] = round(acct["deposited"] + r["amount"], 4)
                self._movement(r["mode"], f"Depot recu {r['amount']:.2f}$", +r["amount"], "deposit")
            r.update(status="fait", done_ts=time.time())
            self.t._save()
            return {"ok": True, "request": r}

    def cancel(self, rid):
        with self._lock:
            r = next((x for x in self.b["requests"] if x["id"] == rid), None)
            if not r or r["status"] not in ("reserve", "pret"):
                return {"ok": False, "error": "demande introuvable ou deja terminee"}
            if r["status"] == "pret" and r["kind"] == "withdraw":
                # l'argent etait pret au coffre : il y reste (Steven peut le remettre en jeu)
                pass
            r.update(status="annule", done_ts=time.time())
            self.t._save()
            return {"ok": True, "request": r}

    def set_policy(self, sweep_pct=None, min_trading=None):
        with self._lock:
            pol = self.b["policy"]
            if sweep_pct is not None:
                pol["sweep_pct"] = max(0.0, min(100.0, float(sweep_pct)))
            if min_trading is not None:
                pol["min_trading"] = max(0.0, float(min_trading))
            self.t._save()
            return dict(pol)

    # ── statistiques ───────────────────────────────────────────────────
    def _closed(self, mode):
        out = []
        for mk in self.t.state["markets"].values():
            for t in mk.get("trades", []):
                if t.get("pnl") is None or (t.get("mode") == "real") != (mode == "real"):
                    continue
                ts = t.get("closed_ts") or t.get("opened_ts")
                if ts:
                    out.append((ts, float(t["pnl"])))
        return sorted(out)

    def stats(self, mode):
        now = time.time()
        cl = self._closed(mode)
        days = {}
        for ts, p in cl:
            days[_day(ts)] = days.get(_day(ts), 0.0) + p
        today = _day(now)
        last14 = []
        for i in range(13, -1, -1):
            d = _day(now - i * 86400)
            last14.append({"day": d, "pnl": round(days.get(d, 0.0), 2)})
        full_days = [v for d, v in days.items() if d != today]
        h24 = [p for ts, p in cl if now - ts <= 86400]
        hours = {}
        for ts, p in cl:
            if now - ts <= 48 * 3600:
                k = int(ts // 3600)
                hours[k] = hours.get(k, 0.0) + p
        first_ts = cl[0][0] if cl else now
        span_h = max(1.0, min(48.0, (now - max(first_ts, now - 48 * 3600)) / 3600))
        hourly = [hours.get(k, 0.0) for k in range(int((now - span_h * 3600) // 3600), int(now // 3600) + 1)]
        avg_h = sum(hourly) / len(hourly) if hourly else 0.0
        if len(full_days) >= 3:
            possible_day = sum(full_days) / len(full_days)
            guaranteed_day = max(0.0, _pct(full_days, 0.25))
            basis, confidence = f"{len(full_days)} jours complets", "bonne" if len(full_days) >= 7 else "moyenne"
        elif span_h >= 24:
            # 1 a 3 jours : moyenne horaire x24 ; garanti = quartile bas des heures x24, divise par 2
            possible_day = avg_h * 24
            guaranteed_day = max(0.0, (_pct(hourly, 0.25) or 0.0) * 24 * 0.5)
            basis, confidence = f"{span_h:.0f} h de donnees", "faible"
        else:
            # MOINS D'UN JOUR : AUCUNE extrapolation (2 h x12 = chiffres de reve) -- on
            # annonce ce qui a VRAIMENT ete gagne ; garanti = la moitie
            real = sum(p for ts, p in cl if now - ts <= 86400)
            possible_day = max(0.0, real)
            guaranteed_day = max(0.0, real * 0.5)
            basis, confidence = f"seulement {span_h:.1f} h de donnees : gains reels, non extrapoles", "insuffisante"
        cap = self.capital(mode)
        pol = self.b["policy"]
        sweep = pol["sweep_pct"] / 100.0
        # projection 30 j : SEULEMENT avec >= 3 jours complets (sinon chiffres de reve),
        # rendement journalier MEDIAN reinvesti (hors part retiree)
        base = max(1.0, cap["trading"])
        r = (_pct(full_days, 0.5) or 0.0) / base if len(full_days) >= 3 else None
        c, wd = base, 0.0
        for _ in range(30 if r is not None else 0):
            g = c * r
            c += g * (1 - sweep)
            wd += g * sweep
        allocation = self._allocation(mode, cap)
        return {
            "mode": mode, "capital": cap, "policy": dict(pol),
            "retrait": {"possible_jour": round(possible_day, 2), "garanti_jour": round(guaranteed_day, 2),
                        "possible_semaine": round(possible_day * 7, 2), "garanti_semaine": round(guaranteed_day * 7, 2),
                        "base": basis, "confiance": confidence,
                        "par_heure": round(avg_h, 3), "dernieres_24h": round(sum(h24), 2),
                        "aujourdhui": round(days.get(today, 0.0), 2)},
            "projection_30j": ({"capital": round(c, 2), "retire": round(wd, 2), "rendement_jour_pct": round(r * 100, 2),
                                "note": "rendement journalier median reinvesti ; plafonne en realite par la profondeur des carnets"}
                               if r is not None else
                               {"capital": None, "retire": None, "rendement_jour_pct": None,
                                "note": f"disponible apres 3 jours complets de donnees ({len(full_days)} pour l'instant)"}),
            "jours": last14,
            "requests": [x for x in self.b["requests"] if x["mode"] == mode][-50:][::-1],
            "mouvements": [x for x in self.b["movements"] if x["mode"] == mode][-100:][::-1],
            "allocation": allocation,
            "releve": self._statement(mode, cl),
        }

    def _allocation(self, mode, cap):
        """Qui gere l'allocation des trades : l'executeur d'arb (limitless/xarb.py)."""
        try:
            from limitless import xarb as _x
            ex = _x.EXECUTOR
            tiers, reserve, slack = ex.TIERS, ex.RESERVE_FOR_HIGH, ex.PAIR_SLACK
            by_bucket = {}
            for p in ex.open.values():
                if (p.get("mode") == "real") == (mode == "real"):
                    by_bucket[p.get("bucket", "?")] = round(by_bucket.get(p.get("bucket", "?"), 0.0) + p["cost"], 2)
        except Exception:
            return None
        return {"gestionnaire": "Executeur d'arb Limitless <-> Polymarket (limitless/xarb.py)",
                "tiers": tiers, "reserve_gros_arbs_pct": reserve, "pair_slack": slack,
                "engage_par_horizon": by_bucket, "reserve_banque": cap["reserve"]}

    def _statement(self, mode, cl):
        """Releve de compte : 1 ligne par jour de trading + chaque mouvement de banque."""
        rows = []
        days = {}
        for ts, p in cl:
            d = _day(ts)
            e = days.setdefault(d, {"ts": ts, "n": 0, "pnl": 0.0})
            e["n"] += 1
            e["pnl"] += p
        for d, e in days.items():
            rows.append({"ts": e["ts"], "date": d, "label": f"Gains de trading ({e['n']} arbs)", "amount": round(e["pnl"], 2)})
        for m in self.b["movements"]:
            if m["mode"] == mode and m["kind"] in ("withdrawn", "deposit", "to_vault", "from_vault"):
                sign = -1 if m["kind"] in ("withdrawn",) else 1
                amt = abs(m["amount"]) * sign if m["kind"] in ("withdrawn", "deposit") else 0.0
                rows.append({"ts": m["ts"], "date": _day(m["ts"]), "label": m["label"], "amount": round(amt, 2),
                             "interne": m["kind"] in ("to_vault", "from_vault")})
        rows.sort(key=lambda x: x["ts"])
        return rows[-60:][::-1]

    # ── boucle ─────────────────────────────────────────────────────────
    def _sweep(self):
        """Set and Forget : a 00:00 UTC, X % du benefice d'HIER part au coffre."""
        pol = self.b["policy"]
        today = _day(time.time())
        if pol.get("sweep_pct", 0) <= 0 or pol.get("last_sweep_day") == today:
            pol["last_sweep_day"] = today if pol.get("sweep_pct", 0) <= 0 else pol.get("last_sweep_day")
            return
        yday = _day(time.time() - 86400)
        for mode in ("paper", "real"):
            prof = sum(p for ts, p in self._closed(mode) if _day(ts) == yday)
            amt = round(prof * pol["sweep_pct"] / 100.0, 2)
            cap = self.capital(mode)
            if amt >= 0.5 and cap["trading"] - amt >= pol.get("min_trading", 0):
                self.request(mode, "to_vault", amt, note=f"Set and Forget : {pol['sweep_pct']:.0f}% du benefice du {yday}")
                self._log(f"🏦 [BANQUE][{mode.upper()}] Set and Forget : {amt:.2f}$ ({pol['sweep_pct']:.0f}% des "
                          f"{prof:.2f}$ gagnes le {yday}) -> coffre")
        pol["last_sweep_day"] = today

    def _loop(self):
        while True:
            time.sleep(self.PROCESS_EVERY_S)
            try:
                with self._lock:
                    for r in self.b["requests"]:
                        if r["status"] == "reserve":
                            self._process_one(r)
                    self._sweep()
            except Exception as e:
                self._log(f"⚠️ [BANQUE] {str(e)[:150]}")


def start(trader, log_fn=None):
    global _BANK
    if _BANK is None:
        _BANK = Bank(trader, log_fn=log_fn)
    return _BANK


def locked(mode):
    """Montant bloque par la banque (lu par l'executeur d'arb) -- 0 si pas demarree."""
    try:
        return _BANK.locked(mode) if _BANK is not None else 0.0
    except Exception:
        return 0.0


@bp.route("/api/bank")
def api_bank():
    mode = "real" if request.args.get("mode") == "real" else "paper"
    return jsonify(_BANK.stats(mode) if _BANK else {"error": "banque non demarree"})


@bp.route("/api/bank/request", methods=["POST"])
def api_bank_request():
    d = request.get_json(silent=True) or {}
    return jsonify(_BANK.request(d.get("mode", "paper"), d.get("kind"), d.get("amount"), d.get("note", "")))


@bp.route("/api/bank/confirm", methods=["POST"])
def api_bank_confirm():
    d = request.get_json(silent=True) or {}
    return jsonify(_BANK.confirm(d.get("id")))


@bp.route("/api/bank/cancel", methods=["POST"])
def api_bank_cancel():
    d = request.get_json(silent=True) or {}
    return jsonify(_BANK.cancel(d.get("id")))


@bp.route("/api/bank/policy", methods=["POST"])
def api_bank_policy():
    d = request.get_json(silent=True) or {}
    return jsonify({"ok": True, "policy": _BANK.set_policy(d.get("sweep_pct"), d.get("min_trading"))})


# ── METAMASK (Claude 29/09, niveau 1 : "la banque prepare, MetaMask signe") ──
# Le dashboard ouvre MetaMask avec la transaction pre-remplie ; les cles restent
# dans MetaMask. Ce module ne fait que donner les ADRESSES PUBLIQUES de destination
# (jamais un secret) et LIRE des soldes on-chain.
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"          # USDC natif Base (Limitless)
USDC_POLYGON = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"       # USDC natif Polygon
USDCE_POLYGON = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"      # USDC.e Polygon
_ERC20_BAL = "0x70a08231"


def _bot_addresses():
    import os
    from eth_account import Account
    out = {"limitless_trading": None, "polymarket_funder": os.environ.get("POLY_FUNDER_ADDRESS") or None,
           "retrait_perso": os.environ.get("BANK_WITHDRAW_ADDRESS") or None}
    pk = os.environ.get("LIMITLESS_PRIVATE_KEY") or os.environ.get("PRIVATE_KEY")
    if pk:
        try:
            out["limitless_trading"] = Account.from_key(pk if pk.startswith("0x") else "0x" + pk).address
        except Exception:
            pass
    return out


def _rpc(url, method, params):
    import requests
    r = requests.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=8)
    return r.json().get("result")


def _balances(address):
    import os
    from web3 import Web3
    if not Web3.is_address(address):
        return {"error": "adresse invalide"}
    a = Web3.to_checksum_address(address)
    data = _ERC20_BAL + a[2:].lower().rjust(64, "0")
    base = os.environ.get("LIMITLESS_RPC_URL") or "https://mainnet.base.org"
    poly = os.environ.get("POLYGON_RPC_URL") or "https://polygon-bor-rpc.publicnode.com"
    out = {"address": a}

    def tok(url, token, dec=6):
        try:
            v = _rpc(url, "eth_call", [{"to": token, "data": data}, "latest"])
            return round(int(v, 16) / 10 ** dec, 4) if v else None
        except Exception:
            return None

    def native(url):
        try:
            v = _rpc(url, "eth_getBalance", [a, "latest"])
            return round(int(v, 16) / 1e18, 6) if v else None
        except Exception:
            return None
    out["base"] = {"usdc": tok(base, USDC_BASE), "eth": native(base)}
    out["polygon"] = {"usdc": tok(poly, USDC_POLYGON), "usdc_e": tok(poly, USDCE_POLYGON), "pol": native(poly)}
    return out


@bp.route("/api/bank/wallets")
def api_bank_wallets():
    """Adresses PUBLIQUES du bot + tokens/chaines, pour pre-remplir MetaMask."""
    addrs = _bot_addresses()
    return jsonify({"addresses": addrs,
                    "chains": {"base": {"chain_id": 8453, "hex": "0x2105", "usdc": USDC_BASE},
                               "polygon": {"chain_id": 137, "hex": "0x89", "usdc": USDC_POLYGON,
                                           "usdc_e": USDCE_POLYGON}},
                    "bot_balances": _balances(addrs["limitless_trading"]) if addrs["limitless_trading"] else None})


@bp.route("/api/bank/balance")
def api_bank_balance():
    """Soldes on-chain (lecture seule) d'une adresse : USDC/ETH Base, USDC/USDC.e/POL Polygon."""
    return jsonify(_balances(request.args.get("address", "")))
