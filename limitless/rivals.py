"""Carte des CONCURRENTS de l'arb Limitless <-> Polymarket (Claude 29/09).

Steven : "devenir les n°1 de l'arb Poly-Limitless". Pour battre les autres
arbitrageurs il faut savoir QUI ils sont, COMBIEN ils ramassent et en COMBIEN DE
TEMPS ils reagissent. Le flux de transactions Limitless donne le pseudo du
preneur ; on le croise avec les carnets Polymarket (datalake).

Une transaction est classee "ARB" si le preneur a achete une issue sur Limitless
moins cher que la meme issue ne coutait sur Polymarket au meme instant (l'ask PM
de cette issue, frais compris) : il a pris un prix perime. Pour ces
transactions on mesure la REACTIVITE : combien de secondes apres que le prix PM
a rendu ce prix Limitless perime (1er relevé PM ou l'ecart est apparu).

    venv/Scripts/python -m limitless.rivals
"""
import bisect
import collections
import sqlite3
import statistics

from limitless.shadow import pm_fee_per_share

DB = r"D:\MMTRADE_DATA\marketdata.db"


def main(min_edge=0.02):
    c = sqlite3.connect(DB)
    link = dict(c.execute("select market_id, linked_slug from markets where venue='lm' and linked_slug is not null"))
    trades = [r for r in c.execute("select ts, market_id, side, outcome, price, size, taker from trades "
                                   "where market_id like 'lm:%' and side='BUY' order by ts") if r[1] in link]
    need = {link[t[1]] for t in trades}
    ticks = collections.defaultdict(list)
    q = "select ts, market_id, up_bid, up_ask, dn_bid, dn_ask from ticks where market_id in (%s)" % ",".join("?" * len(need))
    for row in c.execute(q, list(need)):
        ticks[row[1]].append((row[0], row[2], row[3], row[4], row[5]))
    for v in ticks.values():
        v.sort()

    def at(mid, t):
        v = ticks.get(mid)
        if not v:
            return None, -1
        i = bisect.bisect_right(v, (t, 9, 9, 9, 9)) - 1
        return (v[i], i) if i >= 0 and t - v[i][0] < 60 else (None, -1)

    per = collections.defaultdict(lambda: {"n": 0, "usd": 0.0, "arb_n": 0, "arb_usd": 0.0, "edge_usd": 0.0, "lags": []})
    for ts, mid, side, outcome, price, size, taker in trades:
        who = taker or "?"
        p = per[who]
        p["n"] += 1
        p["usd"] += price * size
        pm = link[mid]
        row, idx = at(pm, ts)
        if row is None:
            continue
        # valeur PM de l'issue achetee = son BID (ce qu'on en tirerait tout de suite) ; son ASK = cout de la meme issue
        bid = row[1] if outcome == "UP" else row[3]
        if bid is None:
            continue
        edge = bid - price - pm_fee_per_share(bid)    # gain par part si on la revendait sur PM
        if edge < min_edge:
            continue
        p["arb_n"] += 1
        p["arb_usd"] += price * size
        p["edge_usd"] += edge * size
        # reactivite : remonte les relevés PM jusqu'au 1er ou l'ecart existait deja
        v = ticks[pm]
        j = idx
        while j > 0:
            b = v[j - 1][1] if outcome == "UP" else v[j - 1][3]
            if b is None or b - price - pm_fee_per_share(b) < min_edge:
                break
            j -= 1
        p["lags"].append(ts - v[j][0])

    rows = sorted(per.items(), key=lambda kv: -kv[1]["edge_usd"])
    hours = (trades[-1][0] - trades[0][0]) / 3600 if trades else 0
    tot_edge = sum(v["edge_usd"] for v in per.values())
    print(f"{len(trades)} achats Limitless sur {hours:.1f} h, {len(per)} preneurs distincts ; "
          f"valeur d'arb ramassee par tous : {tot_edge:.2f} $ ({tot_edge / max(hours, 1e-6):.2f} $/h)\n")
    print(f"{'preneur':24s} {'trades':>6} {'volume $':>9} | {'arbs':>5} {'vol arb $':>9} {'gain arb $':>10} "
          f"{'part':>5} | {'reaction med':>12} {'p25':>6}")
    for who, v in rows[:20]:
        lag = v["lags"]
        med = f"{statistics.median(lag):.1f}s" if lag else "-"
        p25 = f"{sorted(lag)[len(lag) // 4]:.1f}s" if lag else "-"
        print(f"{who[:24]:24s} {v['n']:>6} {v['usd']:>9.0f} | {v['arb_n']:>5} {v['arb_usd']:>9.0f} "
              f"{v['edge_usd']:>10.2f} {v['edge_usd'] / max(tot_edge, 1e-9):>5.0%} | {med:>12} {p25:>6}")


if __name__ == "__main__":
    main()
