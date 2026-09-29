"""Banc de test de l'EXECUTION REELLE de l'arb Limitless <-> Polymarket (Claude 29/09).

Aucun argent, aucun reseau : Limitless et Polymarket sont remplaces par des faux
scenarises (rempli, partiel, refuse, exception, fill malgre exception...). On
verifie que le vrai code de limitless/xarb.py :
  - n'achete jamais plus de parts Polymarket que la jambe Limitless ;
  - enregistre TOUTE part achetee (paire, reliquat nu, jambe gardee) ;
  - ne laisse jamais une exception perdre une jambe ;
  - n'execute qu'un arb reel a la fois ;
  - resout correctement (payout, redeem Limitless, PnL du jour) ;
  - declenche le coupe-circuit (perte du jour, venues divergentes, erreurs).

Lancer :  venv/Scripts/python -m pytest tests/test_xarb_real.py -q
"""
import threading
import time
import types

import pytest

import limitless.xarb as X
from limitless.client import BUY, SELL


# ── faux Polymarket ────────────────────────────────────────────────────
class FakeClob:
    def __init__(self, live):
        self.live = live

    def create_order(self, args):
        return args

    def post_order(self, order, otype):
        lv = self.live
        lv.orders.append((order.token_id, order.price, order.size, order.side, str(otype)))
        sc = lv.scenario
        fill = min(order.size, sc.get("pm_fill", order.size))
        if order.price < sc.get("pm_ask", 0.0):      # plafond sous le meilleur ask : rien
            fill = 0.0
        lv.pending_fill += fill
        if sc.get("pm_raise"):
            raise RuntimeError("timeout HTTP (simule)")
        px = sc.get("pm_avg", order.price)
        return {"success": True, "makingAmount": str(round(fill * px, 6)), "takingAmount": str(fill)}


class FakeLive:
    def __init__(self, scenario):
        self.scenario = scenario
        self.pos = {}
        self.pending_fill = 0.0
        self.orders = []
        self.sold = []

    def client(self):
        return FakeClob(self)

    def sell_position(self, tok, price, size, aggressive=False, marge=0.02):
        self.sold.append((tok, price, size))
        q = min(size, self.pos.get(tok, 0.0))
        self.pos[tok] = self.pos.get(tok, 0.0) - q
        return {"success": True}

    def position_size(self, tok):
        # le fill se propage au 1er relevu (comme la custody Polymarket, en retard)
        if self.pending_fill:
            self.pos[tok] = self.pos.get(tok, 0.0) + self.pending_fill
            self.pending_fill = 0.0
        return self.pos.get(tok, 0.0)


# ── faux Limitless ─────────────────────────────────────────────────────
class FakeLM:
    def __init__(self, scenario):
        self.sc = scenario
        self.orders = []
        self.redeemed = []

    def balances(self):
        return {"usdc": self.sc.get("lm_usdc", 500.0)}

    def place_order(self, slug, outcome, side, price, shares, order_type="GTC", client_order_id=None, **kw):
        if self.sc.get("lm_raise") and side == BUY:
            raise RuntimeError("400 bad signature (simule)")
        self.orders.append({"slug": slug, "outcome": outcome, "side": side, "price": price,
                            "shares": shares, "type": order_type, "coid": client_order_id})
        return {"ok": True}

    def wait_fill(self, coid, timeout_s=4.0):
        o = next(o for o in self.orders if o["coid"] == coid)
        if o["side"] == BUY:
            q = min(o["shares"], self.sc.get("lm_fill", o["shares"]))
            return q, q * self.sc.get("lm_avg", o["price"]), "MATCHED" if q else "UNMATCHED"
        q = min(o["shares"], self.sc.get("lm_sell_fill", o["shares"]))
        return q, q * self.sc.get("lm_sell_px", o["price"]), "MATCHED"

    def orderbook(self, slug):
        b = self.sc.get("lm_bids", [(0.30, 100.0)])
        return {"yes_bids": b, "no_bids": b, "yes_asks": [], "no_asks": []}

    def market(self, slug, fresh=False):
        return {"conditionId": "0xcond", "winningOutcomeIndex": self.sc.get("lm_win_idx")}

    def redeem_onchain(self, cond):
        self.redeemed.append(cond)

    def token_balance(self, tok):
        return self.sc.get("lm_token_bal", {}).get(tok, 1e9)


class FakeShadow:
    fee_bps = 300

    def __init__(self, scenario):
        self.sc = scenario

    def _pm_books(self, toks):
        return {t: {"bids": self.sc.get("pm_bids", [(0.20, 100.0)]), "asks": []} for t in toks}

    def _lm_book(self, slug, now):
        return None


# ── faux moteur ────────────────────────────────────────────────────────
class FakeTrader:
    def __init__(self, scenario, live):
        self.state = {"markets": {s: {"trades": [], "paper_balance": 33.3} for s in
                                  ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB")}}
        self._live = live
        self._order_lock = threading.Lock()
        self.sc = scenario
        self.logs = []
        self.saves = 0

    def _read_cash(self):
        return self.sc.get("pm_cash", 500.0), None

    def _save(self):
        self.saves += 1

    def _tlog(self, key, msg, every=15.0):
        self.logs.append(msg)

    def _portfolio_value(self, mode):
        return 200.0, 200.0, 0.0

    def _paper_latency_floor(self):
        return 0.0

    def _paper_close_handicap(self, mk, pos, pnl):
        pos.update(pnl=pnl, win=pnl > 0)
        return pnl


def make(scenario):
    live = FakeLive(scenario)
    tr = FakeTrader(scenario, live)
    orig = X.CrossArbExecutor._resolve_loop
    X.CrossArbExecutor._resolve_loop = lambda self: None     # pas de thread reseau
    try:
        ex = X.CrossArbExecutor(tr, FakeShadow(scenario), log_fn=tr.logs.append)
    finally:
        X.CrossArbExecutor._resolve_loop = orig
    ex._lm = FakeLM(scenario)
    ex.PM_FIRST_ALWAYS = False     # les tests historiques couvrent le chemin LM d'abord
    ex.PM_FIRST_RISKY = scenario.get("pm_first_risky", False)
    ex.PM_FILL_WAIT_S = 1.2
    w = types.SimpleNamespace(
        coin="BTC", label="BTC 15m", bucket="15-min", sh=ex.sh,
        p={"lm_slug": "btc-up-or-down-15-min-1", "pm_slug": "btc-updown-15m-1", "end_ts": time.time() + 600,
           "pm_up_token": "UP", "pm_down_token": "DOWN", "pm_fee_rate": 0.07, "pm_fee_exp": 1.0,
           "pm_min_order": 5, "lm_taker_delay_ms": 0, "pm_yes_idx": 0,
           "lm_tokens": {"yes": "111", "no": "222"}})
    w.t_left = lambda now: w.p["end_ts"] - now
    c = {"dir": "LM_YES+PM_DOWN", "lm_side": "YES", "pm_side": "DOWN", "lm_top": 0.40, "pm_top": 0.45,
         "lm_px": 0.40, "pm_px": 0.45, "size": 20.0, "edge": 0.12, "edge_h": 0.07}
    return ex, tr, live, w, c


def legs(pos):
    return pos["legs"]["lm"]["shares"], pos["legs"]["pm"]["shares"]


# ── scenarios d'execution ──────────────────────────────────────────────
def test_paire_complete():
    ex, tr, live, w, c = make({})
    ex._execute_real(w, c)
    assert len(ex.open) == 1
    pos = next(iter(ex.open.values()))
    assert legs(pos) == (20.0, 20.0)
    assert pos["strat"] == "xarb_lm_pm" and pos["mode"] == "real"
    assert pos["pm_token"] == "DOWN"                # la reconciliation du moteur ne l'adoptera pas
    # PM : nombre EXACT de parts, plafond arrondi vers le BAS au centime
    tok, px, size, side, _ = live.orders[0]
    assert tok == "DOWN" and side == "BUY" and size == 20.0 and px == round(px, 2)
    assert 0.40 + px < 1.0


def test_pm_moins_cher_n_achete_pas_plus_de_parts():
    """L'ancien snipe_buy prenait un montant en $ : ask plus bas => PLUS de parts que LM."""
    ex, tr, live, w, c = make({"pm_avg": 0.30})
    ex._execute_real(w, c)
    pos = next(iter(ex.open.values()))
    assert legs(pos) == (20.0, 20.0)
    assert live.pos["DOWN"] == 20.0


def test_limitless_zero_rien_engage():
    ex, tr, live, w, c = make({"lm_fill": 0.0})
    ex._execute_real(w, c)
    assert not ex.open and not live.orders


def test_limitless_refuse():
    ex, tr, live, w, c = make({"lm_raise": True})
    ex._execute_real(w, c)
    assert not ex.open and not live.orders


def test_reliquat_limitless_sous_minimum_enregistre():
    ex, tr, live, w, c = make({"lm_fill": 3.0})
    ex._execute_real(w, c)
    assert len(ex.open) == 1                       # AVANT : perdu de vue
    pos = next(iter(ex.open.values()))
    assert legs(pos) == (3.0, 0.0) and pos["strat"] == "xarb_lm_naked"
    assert not live.orders                          # pas de jambe PM impossible (< 5 parts)


def test_pm_non_servi_jambe_gardee_si_pm_la_valorise_plus():
    # PM ne remplit rien ; revente LM a 0,05 < valeur PM (bid 0,55) -> on GARDE
    ex, tr, live, w, c = make({"pm_fill": 0.0, "lm_bids": [(0.05, 100.0)], "pm_bids": [(0.55, 100.0)]})
    ex._execute_real(w, c)
    pos = next(iter(ex.open.values()))
    assert legs(pos) == (20.0, 0.0)
    assert not [o for o in ex._lm.orders if o["side"] == SELL]


def test_pm_non_servi_jambe_revendue_si_meilleur():
    ex, tr, live, w, c = make({"pm_fill": 0.0, "lm_bids": [(0.39, 100.0)], "pm_bids": [(0.10, 100.0)]})
    ex._execute_real(w, c)
    assert not ex.open                              # tout revendu -> rien d'ouvert
    assert [o for o in ex._lm.orders if o["side"] == SELL]
    assert ex.guard["pnl_day"] < 0                  # la perte de revente est comptee


def test_pm_partiel_excedent_gere():
    ex, tr, live, w, c = make({"pm_fill": 12.0, "lm_bids": [(0.05, 100.0)], "pm_bids": [(0.55, 100.0)]})
    ex._execute_real(w, c)
    pos = next(iter(ex.open.values()))
    assert legs(pos) == (20.0, 12.0)                # 8 parts LM nues gardees ET enregistrees


def test_pm_exception_mais_rempli():
    """Le POST leve (timeout) alors que l'ordre a ete execute : le solde fait foi."""
    ex, tr, live, w, c = make({"pm_raise": True})
    ex._execute_real(w, c)
    pos = next(iter(ex.open.values()))
    assert legs(pos) == (20.0, 20.0)


def test_un_seul_arb_reel_a_la_fois():
    ex, tr, live, w, c = make({})
    ex._real_lock.acquire()
    try:
        ex._execute_real(w, c)
    finally:
        ex._real_lock.release()
    assert not ex.open and any("deja en cours" in m for m in tr.logs)


def test_exception_interne_journalisee():
    ex, tr, live, w, c = make({})
    ex._lm.wait_fill = lambda *a, **k: (_ for _ in ()).throw(ValueError("boom"))
    ex._execute_real(w, c)
    assert ex.real_errors and any("EXCEPTION" in m for m in tr.logs)


def test_fonds_insuffisants():
    ex, tr, live, w, c = make({"lm_usdc": 1.0, "pm_cash": 1.0})
    ex._execute_real(w, c)
    assert not ex.open and not ex._lm.orders


# ── coupe-circuit ──────────────────────────────────────────────────────
def test_coupe_circuit_bloque_les_arbs():
    ex, tr, live, w, c = make({})
    ex._kill("test")
    ex._execute_real(w, c)
    assert not ex.open and not ex._lm.orders
    ex.reset_kill()
    ex._execute_real(w, c)
    assert len(ex.open) == 1


def test_coupe_circuit_perte_du_jour():
    ex, tr, live, w, c = make({})
    ex.real_pnl_day(-3.0)
    assert not ex.guard["killed"]
    ex.real_pnl_day(-200.0)
    assert ex.guard["killed"] and "perte du jour" in ex.guard["killed"]


def test_coupe_circuit_rafale_erreurs():
    ex, tr, live, w, c = make({})
    for _ in range(3):
        ex.real_errors.append({"ts": time.time(), "label": "x", "err": "e"})
    assert ex.real_killed()


# ── resolution ─────────────────────────────────────────────────────────
def _run_resolve_once(ex, pm_o, lm_o):
    calls = {"n": 0}

    def fake_sleep(_s):
        calls["n"] += 1
        if calls["n"] > 1:
            raise StopIteration
    ex._pm_outcome = lambda slug, yes_idx=None, need_closed=False: pm_o
    ex._lm_outcome = lambda slug: lm_o
    # fausse horloge LOCALE au module xarb (ne touche pas les autres threads)
    orig = X.time
    X.time = types.SimpleNamespace(sleep=fake_sleep, time=time.time, strftime=time.strftime, gmtime=time.gmtime)
    try:
        with pytest.raises(StopIteration):
            ex._resolve_loop()
    finally:
        X.time = orig


def test_resolution_paire_et_redeem():
    ex, tr, live, w, c = make({})
    ex._execute_real(w, c)
    pos = next(iter(ex.open.values()))
    pos["end_ts"] = time.time() - 120
    _run_resolve_once(ex, "DOWN", "DOWN")           # PM DOWN gagne
    assert not ex.open
    t = tr.state["markets"]["BTC"]["trades"][-1]
    assert t["payout"] == 20.0 and t["pnl"] == pytest.approx(20.0 - pos["cost"], abs=0.01)
    assert ex._lm.redeemed == ["0xcond"]            # jambe LM encaissee
    assert not ex.guard["killed"]


def test_resolution_divergente_coupe_tout():
    ex, tr, live, w, c = make({})
    ex._execute_real(w, c)
    next(iter(ex.open.values()))["end_ts"] = time.time() - 120
    _run_resolve_once(ex, "DOWN", "UP")
    assert ex.guard["killed"] and "DIVERGENTES" in ex.guard["killed"]


# ── sante du reel ──────────────────────────────────────────────────────
def test_sante_ok():
    ex, tr, live, w, c = make({"lm_usdc": 200.0, "pm_cash": 200.0})
    ex._lm.balances = lambda: {"usdc": 200.0, "eth": 0.01}
    ex._execute_real(w, c)
    h = ex.real_health()
    assert h["alerts"] == [] and h["n_real_open"] == 1


def test_sante_capital_desequilibre_et_gas():
    ex, tr, live, w, c = make({"pm_cash": 3.0})
    ex._lm.balances = lambda: {"usdc": 300.0, "eth": 0.0}
    h = ex.real_health()
    assert any("Polymarket bas" in a for a in h["alerts"])
    assert any("ETH" in a for a in h["alerts"])


def test_sante_position_perdue_detectee():
    ex, tr, live, w, c = make({})
    ex._lm.balances = lambda: {"usdc": 200.0, "eth": 0.01}
    ex._execute_real(w, c)
    live.pos["DOWN"] = 5.0                          # 15 parts PM disparues (vendues hors bot)
    ex._lm.sc["lm_token_bal"] = {"111": 20.0}
    h = ex.real_health()
    assert any("Polymarket : 5.00 parts" in a for a in h["alerts"])
    assert not any("Limitless :" in a for a in h["alerts"])


# ── jambes nues : blocage + recouvrement ───────────────────────────────
def test_jambe_nue_bloque_le_marche():
    ex, tr, live, w, c = make({"pm_fill": 0.0, "lm_bids": [(0.05, 100.0)], "pm_bids": [(0.55, 100.0)]})
    ex._execute_real(w, c)
    assert ex.naked_block.get(w.p["lm_slug"], 0) > time.time()


def test_recouvrement_reel_verrouille_la_paire():
    ex, tr, live, w, c = make({"pm_fill": 0.0, "lm_bids": [(0.05, 100.0)], "pm_bids": [(0.55, 100.0)]})
    ex._execute_real(w, c)
    pid, pos = next(iter(ex.open.items()))
    assert legs(pos) == (20.0, 0.0)
    # Polymarket repasse sous l'equilibre : le recouvrement achete les 20 parts manquantes
    live.scenario.pop("pm_fill")
    ex.sh._pm_books = lambda toks: {t: {"bids": [], "asks": [(0.30, 100.0)]} for t in toks}
    ex._pair_for = lambda slug: w.p
    ex._try_rehedge(pid, pos)
    assert legs(pos) == (20.0, 20.0) and pos["strat"] == "xarb_lm_pm" and pos["rehedged"] == 20.0


def test_recouvrement_refuse_au_dessus_de_l_equilibre():
    ex, tr, live, w, c = make({"pm_fill": 0.0, "lm_bids": [(0.05, 100.0)], "pm_bids": [(0.55, 100.0)]})
    ex._execute_real(w, c)
    pid, pos = next(iter(ex.open.items()))
    ex.sh._pm_books = lambda toks: {t: {"bids": [], "asks": [(0.62, 100.0)]} for t in toks}   # paire 0.40+0.62 > 1
    ex._pair_for = lambda slug: w.p
    ex._try_rehedge(pid, pos)
    assert legs(pos) == (20.0, 0.0)


# ── POLYMARKET D'ABORD (trades a risque) ────────────────────────────────
def _risky_signal(c):
    c = dict(c)
    c.update(lm_top=0.08, lm_px=0.08, pm_top=0.80, pm_px=0.80, edge=0.10, edge_h=0.05)
    return c


def test_pm_first_paire_complete():
    ex, tr, live, w, c = make({"pm_avg": 0.80, "lm_avg": 0.08, "pm_first_risky": True})
    c = _risky_signal(c)
    ex._execute_real(w, c)
    pos = next(iter(ex.open.values()))
    assert pos["order"] == "pm_first" and legs(pos) == (20.0, 20.0)
    assert live.orders[0][0] == "DOWN"             # Polymarket achete EN PREMIER


def test_pm_first_limitless_non_servi_revend_pm():
    """Le cas du 29/09 12:15 : LM non servi -> au lieu d'une jambe LM nue a -58 $,
    la jambe PM est revendue tout de suite : perte bornee a l'ecart."""
    ex, tr, live, w, c = make({"pm_avg": 0.80, "lm_fill": 0.0, "pm_bids": [(0.79, 500.0)], "pm_first_risky": True})
    c = _risky_signal(c)
    ex._execute_real(w, c)
    assert not ex.open                              # rien de nu
    assert live.sold and live.pos["DOWN"] == 0.0    # jambe PM revendue
    assert -2.0 < ex.guard["pnl_day"] < 0           # perte = ecart, pas la mise


def test_pm_first_limitless_partiel():
    ex, tr, live, w, c = make({"pm_avg": 0.80, "lm_fill": 12.0, "pm_bids": [(0.79, 500.0)], "pm_first_risky": True})
    c = _risky_signal(c)
    ex._execute_real(w, c)
    pos = next(iter(ex.open.values()))
    assert legs(pos) == (12.0, 12.0)                # excedent PM (8 parts) revendu


def test_risky_detection():
    ex, tr, live, w, c = make({"pm_first_risky": True})
    assert ex._risky(w, c)[0] is False
    w.p["end_ts"] = time.time() + 60
    assert ex._risky(w, c)[0] is True               # fin de fenetre
    w.p["end_ts"] = time.time() + 600
    assert ex._risky(w, _risky_signal(c))[0] is True   # prix LM extreme


def test_mode_systematique_tout_pm_d_abord():
    ex, tr, live, w, c = make({"pm_avg": 0.45, "lm_avg": 0.40})
    ex.PM_FIRST_ALWAYS = True
    assert ex._risky(w, c) == (True, "systematique")
    ex._execute_real(w, c)
    pos = next(iter(ex.open.values()))
    assert pos["order"] == "pm_first" and live.orders[0][0] == "DOWN"


def test_defaut_lm_d_abord_sans_plafond():
    ex, tr, live, w, c = make({})
    assert ex.PM_FIRST_ALWAYS is False and ex.LM_FIRST_CAP is False
    assert ex._risky(w, c) == (False, "")
