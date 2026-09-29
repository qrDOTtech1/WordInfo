"""Backtest TENUE DE MARCHE Limitless couverte sur Polymarket (Claude 29/09).

Steven : "on peut devenir n°1 et tenir ce marche". Question : si NOUS etions le
teneur de marche de Limitless (cotations des 2 cotes, chaque fill couvert tout de
suite sur Polymarket), combien aurions-nous gagne sur le VRAI flux de preneurs ?

Rejoue chaque transaction Limitless reelle (datalake, table trades) :
  - nos cotations a l'instant T viennent de Polymarket a T - REACT (le temps de
    reagir a un mouvement ; le delai preneur de Limitless 250-500 ms joue pour nous) :
      YES ask = UP ask PM + frais PM + marge    (vendu YES -> couverture : acheter UP sur PM)
      NO  ask = DOWN ask PM + frais PM + marge  (vendu NO  -> couverture : acheter DOWN sur PM)
  - un preneur qui a paye P aurait ete servi PAR NOUS si notre prix <= P (nous serions
    devant le teneur de marche actuel) ; il paie NOTRE prix ; taille <= QUOTE_SIZE ;
  - couverture achetee sur PM au prix REEL de T + HEDGE (le marche a pu bouger) ;
  - remise preneur : 100 % des frais du preneur sur 15 min / horaire / journalier
    (doc Limitless : "maker rebates 100% of eligible taker fees", fill-gated).
Chaque fill couvert = paire (YES LM + UP PM ou NO LM + DOWN PM... cote oppose) :
gain verrouille par part = notre prix - cout de couverture - frais PM + remise.

Limites (a garder en tete) : relevés PM a ~1 Hz (reactivite simulee a la seconde) ;
on ne compte PAS les preneurs supplementaires qu'attirerait un meilleur prix
(prudent) ; file d'attente : on suppose etre devant a prix egal (optimiste) ->
une variante "strictement meilleur" est aussi calculee.

    venv/Scripts/python -m limitless.mm_backtest
"""
import bisect
import collections
import sqlite3

from limitless.shadow import lm_buy_fee_pct, pm_fee_per_share

DB = r"D:\MMTRADE_DATA\marketdata.db"
PM_FEE_RATE = 0.07          # frais preneur Polymarket crypto (rate * p(1-p))
LM_TAKER_BPS = 300          # frais preneur Limitless (rang de base)
REBATE_BUCKETS = ("15m", "1h", "1d")


def bucket(mid):
    return ("15m" if "15-min" in mid else "5m" if "5-min" in mid else "1h" if "hourly" in mid   # 15 AVANT 5 !
            else "1d" if "daily" in mid else "autre")


def load():
    c = sqlite3.connect(DB)
    link = dict(c.execute("select market_id, linked_slug from markets where venue='lm' and linked_slug is not null"))
    trades = [r for r in c.execute("select ts, market_id, side, outcome, price, size from trades "
                                   "where market_id like 'lm:%' order by ts") if r[1] in link]
    need = {link[t[1]] for t in trades}
    ticks = collections.defaultdict(list)
    q = "select ts, market_id, up_ask, dn_ask from ticks where market_id in (%s)" % ",".join("?" * len(need))
    for ts, mid, ua, da in c.execute(q, list(need)):
        ticks[mid].append((ts, ua, da))
    for v in ticks.values():
        v.sort()
    return link, trades, ticks


def pm_at(ticks, mid, t, max_age=20.0):
    v = ticks.get(mid)
    if not v:
        return None
    i = bisect.bisect_right(v, (t, 9, 9)) - 1
    if i < 0 or t - v[i][0] > max_age:   # relevés ecrits au changement : dernier connu
        return None
    return v[i]


def run(link, trades, ticks, margin, react, hedge, size, strict=False):
    res = collections.defaultdict(lambda: {"fills": 0, "shares": 0.0, "pnl": 0.0, "rebate": 0.0,
                                           "losers": 0, "flow_shares": 0.0, "flow_trades": 0})
    for ts, mid, side, outcome, price, qty in trades:
        if side != "BUY" or not qty:
            continue
        b = bucket(mid)
        r = res[b]
        r["flow_trades"] += 1
        r["flow_shares"] += qty
        pm = link[mid]
        q0, q1 = pm_at(ticks, pm, ts - react), pm_at(ticks, pm, ts + hedge)
        if not q0 or not q1:
            continue
        # preneur achete UP (=YES) -> nous vendons YES, couverture = acheter UP sur PM
        a0 = q0[1] if outcome == "UP" else q0[2]
        a1 = q1[1] if outcome == "UP" else q1[2]
        if a0 is None or a1 is None or not (0 < a0 < 1) or not (0 < a1 < 1):
            continue
        our_ask = round(a0 + pm_fee_per_share(a0, PM_FEE_RATE) + margin, 3)
        if our_ask >= 1:
            continue
        if our_ask > price or (strict and our_ask >= price):
            continue                       # pas servi par nous
        q = min(qty, size)
        rebate = (our_ask * lm_buy_fee_pct(our_ask, LM_TAKER_BPS) / 100) if b in REBATE_BUCKETS else 0.0
        per = our_ask - a1 - pm_fee_per_share(a1, PM_FEE_RATE) + rebate
        r["fills"] += 1
        r["shares"] += q
        r["pnl"] += per * q
        r["rebate"] += rebate * q
        r["losers"] += per < 0
    return res


def main():
    link, trades, ticks = load()
    if not trades:
        print("aucune transaction Limitless appariee en base")
        return
    hours = (trades[-1][0] - trades[0][0]) / 3600
    vol = sum(p * q for _, _, _, _, p, q in trades)
    print(f"flux reel Limitless : {len(trades)} transactions, {vol:.0f} $ sur {hours:.1f} h "
          f"({vol / max(hours, 1e-6):.0f} $/h)\n")
    print(f"{'marge':>6} {'react':>5} {'couv':>5} {'taille':>6} {'strict':>6} | {'fills':>5} {'parts':>7} "
          f"{'PnL $':>8} {'dont remise':>11} {'$/h':>7} {'perdants':>8}")
    best = None
    for margin in (0.0, 0.005, 0.01, 0.02, 0.03):
        for react in (0.5, 1.0, 2.0):
            for hedge in (0.5, 1.5):
                for size in (10, 25, 50):
                    for strict in (False, True):
                        res = run(link, trades, ticks, margin, react, hedge, size, strict)
                        tot = {k: sum(v[k] for v in res.values()) for k in ("fills", "shares", "pnl", "rebate", "losers")}
                        row = (margin, react, hedge, size, strict, tot)
                        if best is None or tot["pnl"] > best[-1]["pnl"]:
                            best = row
                        if size == 25 and hedge == 1.5:
                            print(f"{margin:>6.3f} {react:>5.1f} {hedge:>5.1f} {size:>6} {str(strict):>6} | {tot['fills']:>5} "
                                  f"{tot['shares']:>7.0f} {tot['pnl']:>8.2f} {tot['rebate']:>11.2f} "
                                  f"{tot['pnl'] / max(hours, 1e-6):>7.2f} {tot['losers']:>8}")
    m, rc, hd, sz, st, tot = best
    print(f"\nmeilleur : marge {m}, reaction {rc}s, couverture {hd}s, taille {sz}, strict={st} -> "
          f"{tot['pnl']:.2f} $ ({tot['pnl'] / max(hours, 1e-6):.2f} $/h), {tot['fills']} fills")
    print("\npar horizon (meilleur reglage) :")
    for b, v in sorted(run(link, trades, ticks, m, rc, hd, sz, st).items()):
        print(f"  {b:>4} : flux {v['flow_trades']} trades / {v['flow_shares']:.0f} parts | nous : {v['fills']} fills, "
              f"{v['shares']:.0f} parts, PnL {v['pnl']:+.2f} $ (remise {v['rebate']:.2f}), perdants {v['losers']}")


if __name__ == "__main__":
    main()
