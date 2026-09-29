"""Enregistreur 1 Hz de tous les marches crypto Up/Down (Polymarket + Limitless).

Tables (data/marketdata.db, SQLite WAL) :
  markets   une ligne par fenetre : venue, slug, crypto, duree, debut/fin, strike,
            tokens, lien Limitless<->Polymarket, source de resolution
  ticks     1 ligne / marche / seconde : temps restant, meilleur bid/ask + taille
            des cotes UP et DOWN, spot Binance au meme instant
  depth     toutes les DEPTH_EVERY_S : 5 niveaux de chaque cote (JSON compact)
  spot      1 ligne / crypto / seconde (prix Binance)
  trades    trades publics (Polymarket data-api, Limitless /events)
  outcomes  gagnant de chaque fenetre une fois resolue

Conventions : cote UP = "Up"/"Yes" (YES Limitless), DOWN = l'autre. Sur
Limitless le carnet NO est deduit du YES (CLOB CTF). Aucun ordre n'est passe.
"""
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from limitless import config
from limitless.client import LimitlessClient
from limitless.pairs import _RECURRING
from limitless.ws_feed import LimitlessBookFeed
from datalake.chainlink_feed import ChainlinkFeed

GAMMA = "https://gamma-api.polymarket.com/markets"
PM_BOOKS = "https://clob.polymarket.com/books"
PM_TRADES = "https://data-api.polymarket.com/trades"
BINANCE = "https://api.binance.com/api/v3/ticker/price"
ET = ZoneInfo("America/New_York")

COINS = {  # code -> (nom slug horaire/journalier Polymarket, symbole Binance)
    "btc": ("bitcoin", "BTCUSDT"), "eth": ("ethereum", "ETHUSDT"), "sol": ("solana", "SOLUSDT"),
    "xrp": ("xrp", "XRPUSDT"), "doge": ("dogecoin", "DOGEUSDT"), "bnb": ("bnb", "BNBUSDT"),
    "hype": ("hyperliquid", "HYPEUSDT"),
}
EPOCH_TF = {"5m": 300, "15m": 900, "4h": 14400}  # slug {coin}-updown-{tf}-{debut}

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
CREATE TABLE IF NOT EXISTS markets(
  market_id TEXT PRIMARY KEY, venue TEXT, slug TEXT, coin TEXT, tf TEXT,
  start_ts INTEGER, end_ts INTEGER, strike REAL, token_up TEXT, token_down TEXT,
  condition_id TEXT, linked_slug TEXT, resolution_source TEXT, first_seen REAL,
  cl_strike REAL);
CREATE TABLE IF NOT EXISTS ticks(
  ts REAL, market_id TEXT, t_left REAL,
  up_bid REAL, up_bid_sz REAL, up_ask REAL, up_ask_sz REAL,
  dn_bid REAL, dn_bid_sz REAL, dn_ask REAL, dn_ask_sz REAL, spot REAL,
  cl_price REAL, cl_twap60 REAL);
CREATE INDEX IF NOT EXISTS ix_ticks_m_ts ON ticks(market_id, ts);
CREATE TABLE IF NOT EXISTS depth(ts REAL, market_id TEXT, book TEXT);
CREATE INDEX IF NOT EXISTS ix_depth_m_ts ON depth(market_id, ts);
CREATE TABLE IF NOT EXISTS spot(ts REAL, coin TEXT, price REAL);
CREATE INDEX IF NOT EXISTS ix_spot_c_ts ON spot(coin, ts);
CREATE TABLE IF NOT EXISTS trades(
  trade_key TEXT PRIMARY KEY, ts REAL, market_id TEXT, side TEXT, outcome TEXT,
  price REAL, size REAL, taker TEXT);
CREATE INDEX IF NOT EXISTS ix_trades_m_ts ON trades(market_id, ts);
CREATE TABLE IF NOT EXISTS chainlink(ts REAL, coin TEXT, price REAL);
CREATE INDEX IF NOT EXISTS ix_cl_c_ts ON chainlink(coin, ts);
CREATE TABLE IF NOT EXISTS latency_probes(ts REAL, venue TEXT, kind TEXT, ms REAL, status INTEGER, detail TEXT);
CREATE TABLE IF NOT EXISTS outcomes(market_id TEXT PRIMARY KEY, winner TEXT, resolved_ts REAL);
"""


def _iso_ts(s):
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()) if s else None


def _jl(x):
    return json.loads(x) if isinstance(x, str) else (x or [])


def _top(levels):
    return (levels[0][0], levels[0][1]) if levels else (None, None)


class MarketRecorder:
    TICK_S = 1.0
    DEPTH_EVERY_S = 5
    DISCOVER_S = 20
    TRADES_S = 30
    OUTCOMES_S = 60

    def __init__(self, log_fn=None, db_path=None):
        self._log = log_fn or print
        from datalake import db_path as _default_db

        self.db_path = str(db_path or _default_db())
        self._db = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
        self._db.executescript(SCHEMA)
        self._dblock = threading.Lock()
        self._http = requests.Session()
        self._http.headers["User-Agent"] = "Mozilla/5.0 MMTRADE-datalake"
        self._pool = ThreadPoolExecutor(max_workers=6)
        self.lm = LimitlessClient(log_fn=self._log)
        self.feed = LimitlessBookFeed(log_fn=self._log)
        self._cl_buf = []
        self.cl = ChainlinkFeed(log_fn=self._log, on_price=lambda t, c, p: self._cl_buf.append((t, c, p)))
        self.markets = {}          # market_id -> meta (actives ou a venir)
        self._neg = {}             # slug PM inexistant -> ts (cache negatif)
        self._seen_trades = set()
        self._last_depth = 0
        self.latest_pm = {}      # token -> (bids, asks) du dernier echantillon (partage avec la mesure Limitless)
        self.latest_pm_ts = 0.0
        self.extra_tokens = set()  # tokens demandes par d'autres modules (mesure Limitless)
        self._stop = threading.Event()
        self.stats = {"ticks": 0, "depth": 0, "spot": 0, "trades": 0, "outcomes": 0,
                      "last_tick_ms": 0, "errors": 0, "started": time.time()}

    # ── ecriture ───────────────────────────────────────────────────────
    def _exec(self, sql, rows):
        if not rows:
            return
        with self._dblock:
            self._db.executemany(sql, rows)
            self._db.commit()

    def _upsert_market(self, m):
        self._exec("INSERT OR IGNORE INTO markets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [(
            m["id"], m["venue"], m["slug"], m["coin"], m["tf"], m["start"], m["end"], m.get("strike"),
            m["tok_up"], m["tok_dn"], m.get("condition_id"), m.get("linked"), m.get("source"), time.time(), None)])

    # ── decouverte ─────────────────────────────────────────────────────
    def _pm_candidates(self, now):
        out = []
        for c, (name, _) in COINS.items():
            for tf, sec in EPOCH_TF.items():
                start = int(now // sec * sec)
                for st in (start, start + sec):
                    out.append((f"{c}-updown-{tf}-{st}", c, tf, st, st + sec))
            et_now = datetime.fromtimestamp(now, ET).replace(minute=0, second=0, microsecond=0)
            for h in (et_now, et_now + timedelta(hours=1)):
                hh = h.strftime("%I").lstrip("0") + h.strftime("%p").lower()
                slug = f"{name}-up-or-down-{h.strftime('%B').lower()}-{h.day}-{h.year}-{hh}-et"
                st = int(h.timestamp())
                out.append((slug, c, "1h", st, st + 3600))
            noon = datetime.fromtimestamp(now, ET).replace(hour=12, minute=0, second=0, microsecond=0)
            for d in (noon, noon + timedelta(days=1)):
                slug = f"{name}-up-or-down-on-{d.strftime('%B').lower()}-{d.day}-{d.year}"
                end = int(d.timestamp())
                out.append((slug, c, "1d", end - 86400, end))
        return out

    def _discover_pm(self, now):
        found = 0
        for slug, coin, tf, st, end in self._pm_candidates(now):
            mid = f"pm:{slug}"
            if mid in self.markets or end < now - 5:
                continue
            if now - self._neg.get(slug, 0) < 60:
                continue
            try:
                r = self._http.get(GAMMA, params={"slug": slug}, timeout=8).json()
            except Exception:
                self.stats["errors"] += 1
                continue
            if not r:
                self._neg[slug] = now
                continue
            g = r[0]
            outs, toks = _jl(g.get("outcomes")), _jl(g.get("clobTokenIds"))
            if len(outs) != 2 or len(toks) != 2:
                continue
            lo = [o.lower() for o in outs]
            iu = lo.index("up") if "up" in lo else (lo.index("yes") if "yes" in lo else 0)
            m = {"id": mid, "venue": "pm", "slug": slug, "coin": coin, "tf": tf, "start": st,
                 "end": _iso_ts(g.get("endDate")) or end, "tok_up": toks[iu], "tok_dn": toks[1 - iu],
                 "condition_id": g.get("conditionId"), "source": g.get("resolutionSource")}
            self.markets[mid] = m
            self._upsert_market(m)
            found += 1
        return found

    def _discover_lm(self, now):
        found = 0
        try:
            listing = self.lm.active_slugs()
        except Exception:
            self.stats["errors"] += 1
            return 0
        for it in listing:
            slug = it.get("slug") or ""
            if not _RECURRING.match(slug):
                continue
            mid = f"lm:{slug}"
            if mid in self.markets:
                continue
            try:
                g = self.lm.market(slug)
            except Exception:
                self.stats["errors"] += 1
                continue
            md = g.get("metadata") or {}
            coin = slug.split("-up-or-down")[0].replace("solana", "sol").replace("dogecoin", "doge")
            tf = next((v for k, v in (("15-min", "15m"), ("5-min", "5m"), ("hourly", "1h"),
                                      ("daily", "1d"), ("weekly", "1w")) if k in slug), "?")
            end = int(g["expirationTimestamp"]) // 1000
            m = {"id": mid, "venue": "lm", "slug": slug, "coin": coin, "tf": tf,
                 "start": _iso_ts(g.get("startAt")), "end": end,
                 "strike": float(it["strikePrice"]) if it.get("strikePrice") else None,
                 "tok_up": str(g["tokens"]["yes"]), "tok_dn": str(g["tokens"]["no"]),
                 "condition_id": g.get("conditionId"),
                 "linked": f"pm:{md['externalSlug']}" if md.get("externalSlug") else None,
                 "source": ((md.get("chainlinkDataStream") or {}).get("streamUrl") or "binance")}
            self.markets[mid] = m
            self._upsert_market(m)
            found += 1
        return found

    def discover(self):
        now = time.time()
        n_pm = self._discover_pm(now)
        n_lm = self._discover_lm(now)
        for mid, m in list(self.markets.items()):  # purge des fenetres finies
            if m["end"] < now - 120:
                self.markets.pop(mid, None)
        self.feed.set_slugs([m["slug"] for m in self.markets.values() if m["venue"] == "lm"])
        if n_pm or n_lm:
            by = {}
            for m in self.markets.values():
                by.setdefault(f"{m['venue'].upper()} {m['tf']}", set()).add(m["coin"].upper())
            self._log(f"🗄️ [DATALAKE] +{n_pm} PM +{n_lm} LM | suivis : " +
                      " · ".join(f"{k} [{','.join(sorted(v))}]" for k, v in sorted(by.items())))

    # ── echantillonnage 1 Hz ───────────────────────────────────────────
    def _pm_books(self, tokens):
        out = {}
        tokens = list(dict.fromkeys(tokens + list(self.extra_tokens)))

        def fetch(chunk):
            r = self._http.post(PM_BOOKS, json=[{"token_id": t} for t in chunk], timeout=6)
            r.raise_for_status()
            return r.json()

        chunks = [tokens[i:i + 50] for i in range(0, len(tokens), 50)]
        for res in self._pool.map(fetch, chunks):
            for b in res:
                bids = sorted(((float(x["price"]), float(x["size"])) for x in b.get("bids", [])), key=lambda t: -t[0])
                asks = sorted(((float(x["price"]), float(x["size"])) for x in b.get("asks", [])), key=lambda t: t[0])
                out[b["asset_id"]] = (bids, asks)
        return out

    def _spot(self):
        syms = [v[1] for v in COINS.values()]
        r = self._http.get(BINANCE, params={"symbols": json.dumps(syms, separators=(",", ":"))}, timeout=4)
        r.raise_for_status()
        inv = {v[1]: k for k, v in COINS.items()}
        return {inv[x["symbol"]]: float(x["price"]) for x in r.json()}

    def sample(self):
        t0 = time.time()
        now = t0
        live = [m for m in self.markets.values() if (m["start"] or 0) - 2 <= now <= m["end"] + 2]
        pm_tokens = [t for m in live if m["venue"] == "pm" for t in (m["tok_up"], m["tok_dn"])]
        f_books = self._pool.submit(self._pm_books, pm_tokens) if pm_tokens else None
        f_spot = self._pool.submit(self._spot)
        try:
            books = f_books.result() if f_books else {}
            self.stats["books_ms"] = round((time.time() - t0) * 1000)
            if books:
                self.latest_pm, self.latest_pm_ts = books, time.time()
        except Exception:
            books = {}
            self.stats["errors"] += 1
        try:
            spot = f_spot.result()
        except Exception:
            spot = {}
            self.stats["errors"] += 1
        self.stats["fetch_ms"] = round((time.time() - t0) * 1000)
        ts = round(time.time(), 3)
        ticks, depth, strikes = [], [], []
        cl_twap = {c: self.cl.twap(c, at=ts) for c in COINS}
        do_depth = ts - self._last_depth >= self.DEPTH_EVERY_S
        for m in live:
            if m["venue"] == "pm":
                up, dn = books.get(m["tok_up"]), books.get(m["tok_dn"])
                if up is None or dn is None:
                    continue
                ub, ua = up
                db, da = dn
            else:
                b = self.feed.book(m["slug"]) if self.feed.connected else None
                if b is None:
                    continue  # WS coupe : on n'enregistre PAS un carnet fige
                ub, ua, db, da = b["yes_bids"], b["yes_asks"], b["no_bids"], b["no_asks"]
            (u_b, u_bs), (u_a, u_as) = _top(ub), _top(ua)
            (d_b, d_bs), (d_a, d_as) = _top(db), _top(da)
            ticks.append((ts, m["id"], round(m["end"] - ts, 2), u_b, u_bs, u_a, u_as,
                          d_b, d_bs, d_a, d_as, spot.get(m["coin"]),
                          self.cl.price(m["coin"]), cl_twap.get(m["coin"])))
            # PRICE TO BEAT Chainlink : TWAP 60 s a l'ouverture (5/15 min), fige une fois
            if m["tf"] in ("5m", "15m") and m.get("cl_strike") is None and m["start"] and ts >= m["start"] + 2:
                k = self.cl.twap(m["coin"], at=m["start"])
                if k is not None:
                    m["cl_strike"] = k
                    strikes.append((k, m["id"]))
            if do_depth:
                depth.append((ts, m["id"], json.dumps({"ub": ub[:5], "ua": ua[:5], "db": db[:5], "da": da[:5]},
                                                      separators=(",", ":"))))
        t_db = time.time()
        self.stats["build_ms"] = round((t_db - t0) * 1000) - self.stats["fetch_ms"]
        self._exec("INSERT INTO ticks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ticks)
        self._exec("UPDATE markets SET cl_strike=? WHERE market_id=?", strikes)
        buf, self._cl_buf = self._cl_buf, []
        self._exec("INSERT INTO chainlink VALUES (?,?,?)", buf)
        self.stats["chainlink"] = self.stats.get("chainlink", 0) + len(buf)
        self._exec("INSERT INTO spot VALUES (?,?,?)", [(ts, c, p) for c, p in spot.items()])
        if do_depth:
            self._exec("INSERT INTO depth VALUES (?,?,?)", depth)
            self._last_depth = ts
            self.stats["depth"] += len(depth)
        self.stats["db_ms"] = round((time.time() - t_db) * 1000)
        self.stats["ticks"] += len(ticks)
        self.stats["spot"] += len(spot)
        self.stats["last_tick_ms"] = round((time.time() - t0) * 1000)
        self.stats["live_markets"] = len(live)

    # ── trades + resultats (thread lent) ───────────────────────────────
    def _pm_trades(self, m):
        if not m.get("condition_id"):
            return []
        r = self._http.get(PM_TRADES, params={"market": m["condition_id"], "limit": 500}, timeout=8).json()
        rows = []
        for t in r if isinstance(r, list) else []:
            key = f"pm:{t.get('transactionHash')}:{t.get('asset')}:{t.get('size')}:{t.get('side')}"
            if key in self._seen_trades:
                continue
            self._seen_trades.add(key)
            outcome = "UP" if str(t.get("asset")) == m["tok_up"] else "DOWN"
            rows.append((key, float(t.get("timestamp") or 0), m["id"], t.get("side"), outcome,
                         float(t.get("price") or 0), float(t.get("size") or 0), t.get("proxyWallet")))
        return rows

    def _lm_trades(self, m):
        r = self.lm.trades(m["slug"], limit=100)
        rows = []
        for e in r.get("events", []):
            key = f"lm:{e.get('txHash')}:{e.get('tokenId')}:{e.get('matchedSize')}:{e.get('createdAt')}"
            if key in self._seen_trades:
                continue
            self._seen_trades.add(key)
            outcome = "UP" if str(e.get("tokenId")) == m["tok_up"] else "DOWN"
            ts = datetime.fromisoformat(e["createdAt"].replace("Z", "+00:00")).timestamp()
            rows.append((key, ts, m["id"], "BUY" if int(e.get("side", 0)) == 0 else "SELL", outcome,
                         float(e.get("price") or 0), int(e.get("matchedSize") or 0) / 1e6,
                         ((e.get("profile") or {}).get("username"))))
        return rows

    def _record_outcomes(self, now):
        with self._dblock:
            todo = self._db.execute(
                "SELECT market_id, venue, slug, token_up FROM markets WHERE end_ts < ? AND end_ts > ? "
                "AND market_id NOT IN (SELECT market_id FROM outcomes)", (now - 90, now - 86400 * 3)).fetchall()
        rows = []
        for mid, venue, slug, tok_up in todo[:40]:
            try:
                if venue == "pm":
                    g = self._http.get(GAMMA, params={"slug": slug}, timeout=8).json()
                    if not g:  # marche clos : Gamma ne le renvoie qu'avec closed=true
                        g = self._http.get(GAMMA, params={"slug": slug, "closed": "true"}, timeout=8).json()
                    if not g:
                        continue
                    g = g[0]
                    prices, outs = [float(x) for x in _jl(g.get("outcomePrices"))], _jl(g.get("outcomes"))
                    if prices and max(prices) >= 0.99:
                        w = outs[prices.index(max(prices))].upper()
                        rows.append((mid, "UP" if w in ("UP", "YES") else "DOWN", now))
                else:
                    g = self.lm.market(slug, fresh=True)
                    idx = g.get("winningOutcomeIndex")
                    if idx is not None:
                        rows.append((mid, "UP" if int(idx) == 0 else "DOWN", now))
            except Exception:
                self.stats["errors"] += 1
        self._exec("INSERT OR IGNORE INTO outcomes VALUES (?,?,?)", rows)
        self.stats["outcomes"] += len(rows)

    def _slow_loop(self):
        last_disc = last_trades = last_out = 0
        while not self._stop.is_set():
            now = time.time()
            try:
                if now - last_disc >= self.DISCOVER_S:
                    last_disc = now
                    self.discover()
                if now - last_trades >= self.TRADES_S:
                    last_trades = now
                    rows = []
                    for m in list(self.markets.values()):
                        if m["start"] and m["start"] > now:
                            continue
                        try:
                            rows += self._pm_trades(m) if m["venue"] == "pm" else self._lm_trades(m)
                        except Exception:
                            self.stats["errors"] += 1
                    self._exec("INSERT OR IGNORE INTO trades VALUES (?,?,?,?,?,?,?,?)", rows)
                    self.stats["trades"] += len(rows)
                if now - last_out >= self.OUTCOMES_S:
                    last_out = now
                    self._record_outcomes(now)
            except Exception as e:
                self.stats["errors"] += 1
                self._log(f"⚠️ [DATALAKE] {str(e)[:150]}")
            self._stop.wait(1.0)

    def _fast_loop(self):
        last_log = 0
        while not self._stop.is_set():
            t0 = time.time()
            try:
                self.sample()
            except Exception as e:
                self.stats["errors"] += 1
                self._log(f"⚠️ [DATALAKE] echantillon : {str(e)[:150]}")
            if t0 - last_log >= 300:
                last_log = t0
                self._log(f"🗄️ [DATALAKE] {self.stats.get('live_markets', 0)} marches live · "
                          f"{self.stats['ticks']} ticks · {self.stats['depth']} profondeurs · "
                          f"{self.stats['trades']} trades · {self.stats['outcomes']} resultats · "
                          f"tick {self.stats['last_tick_ms']} ms · base {self.size_mb():.1f} Mo")
            self._stop.wait(max(0.05, self.TICK_S - (time.time() - t0)))

    def size_mb(self):
        import os
        tot = 0
        for suf in ("", "-wal"):
            try:
                tot += os.path.getsize(self.db_path + suf)
            except OSError:
                pass
        return tot / 1e6

    def summary(self):
        by = {}
        for m in self.markets.values():
            by.setdefault(f"{m['venue']}:{m['tf']}", []).append(m["coin"])
        return {**self.stats, "db_path": self.db_path, "db_mb": round(self.size_mb(), 2),
                "ws_connected": self.feed.connected, "chainlink_connected": self.cl.connected(),
                "chainlink_twap60": {c: (round(v, 6) if v else None) for c, v in ((c, self.cl.twap(c)) for c in COINS)},
                "markets": {k: sorted(set(v)) for k, v in sorted(by.items())},
                "uptime_s": round(time.time() - self.stats["started"])}

    def start(self):
        self.feed.start()  # la decouverte (lente) se fait dans le thread lent
        self.cl.start()
        threading.Thread(target=self._slow_loop, daemon=True, name="datalake-slow").start()
        threading.Thread(target=self._fast_loop, daemon=True, name="datalake-1hz").start()
        self._log(f"🗄️ [DATALAKE] enregistrement 1 Hz demarre -> {self.db_path}")

    def stop(self):
        self._stop.set()


if __name__ == "__main__":
    r = MarketRecorder(log_fn=lambda m: print(time.strftime("%H:%M:%S"), m, flush=True))
    r.start()
    try:
        while True:
            time.sleep(30)
            print(json.dumps(r.summary()), flush=True)
    except KeyboardInterrupt:
        r.stop()
