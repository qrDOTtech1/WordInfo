"""Appariement Limitless <-> Polymarket (Claude 28/09).

Limitless declare lui-meme la source de ses marches recurrents crypto :
metadata.externalProvider == "polymarket" + metadata.externalSlug (ex:
btc-up-or-down-15-min-1790597700 -> btc-updown-15m-1790597700).

MAIS un titre ou un slug commun ne suffit pas (doc officielle cross-market-mm :
"Title overlap is not enough"). Une paire n'est VERIFIEE que si :
  1. la source de resolution est la MEME URL d'oracle (ex. flux Chainlink
     btc-usd-twap-60s-streams) des deux cotes ;
  2. l'echeance est la MEME seconde ;
  3. on sait quel cote Polymarket correspond a YES Limitless ("Up"/"Yes").
Une paire non verifiee est mesuree pour info mais JAMAIS tradee.
"""
import html
import json
import re
import time

import requests

from limitless.client import LimitlessClient

GAMMA = "https://gamma-api.polymarket.com/markets"
_RECURRING = re.compile(r"^(btc|eth|solana|sol|xrp|doge|dogecoin|bnb)-up-or-down-")


def _norm_url(u):
    return (u or "").strip().lower().rstrip("/").replace("https://", "").replace("http://", "")


def _lm_oracle_url(m):
    md = m.get("metadata") or {}
    po = m.get("priceOracleMetadata") or {}
    return ((md.get("chainlinkDataStream") or {}).get("streamUrl")
            or po.get("chainlinkStreamUrl") or "")


def _rule_text(desc):
    t = html.unescape(re.sub(r"<[^>]+>", " ", desc or ""))
    t = re.sub(r"https?://\S+", "", t)
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def _iso_to_ts(s):
    from datetime import datetime
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


class PairFinder:
    def __init__(self, client: LimitlessClient, log_fn=None):
        self.c = client
        self._log = log_fn or print
        self._pm_cache = {}
        self.pairs = {}  # lm_slug -> pair dict
        self.last_added = []

    def _pm_market(self, slug):
        c = self._pm_cache.get(slug)
        if c and time.time() - c[0] < 300:
            return c[1]
        r = requests.get(GAMMA, params={"slug": slug}, timeout=10).json()
        m = r[0] if r else None
        self._pm_cache[slug] = (time.time(), m)
        return m

    def build_pair(self, lm_slug):
        m = self.c.market(lm_slug)
        md = m.get("metadata") or {}
        if md.get("externalProvider") != "polymarket" or not md.get("externalSlug"):
            return None
        pm = self._pm_market(md["externalSlug"])
        if not pm:
            return None
        outcomes = json.loads(pm["outcomes"]) if isinstance(pm["outcomes"], str) else pm["outcomes"]
        tokens = json.loads(pm["clobTokenIds"]) if isinstance(pm["clobTokenIds"], str) else pm["clobTokenIds"]
        lower = [o.lower() for o in outcomes]
        yes_idx = lower.index("up") if "up" in lower else (lower.index("yes") if "yes" in lower else None)
        lm_end = int(m["expirationTimestamp"]) // 1000
        pm_end = _iso_to_ts(pm["endDate"]) if pm.get("endDate") else None
        lm_src, pm_src = _norm_url(_lm_oracle_url(m)), _norm_url(pm.get("resolutionSource"))
        # Horaires/journaliers (source Binance) : pas de champ oracle structure
        # cote Limitless, mais la regle est recopiee de Polymarket -> on exige
        # que l'URL source Polymarket soit citee dans la regle Limitless ET que
        # le texte de la regle soit identique (normalise).
        lm_rule, pm_rule = _rule_text(m.get("description")), _rule_text(pm.get("description"))
        lm_urls = {_norm_url(u) for u in re.findall(r"https?://[^\s\"'<>)]+", html.unescape(m.get("description") or ""))}
        same_rule = bool(lm_rule) and lm_rule[:400] == pm_rule[:400]
        checks = {
            "same_oracle": bool(pm_src) and (lm_src == pm_src or (pm_src in lm_urls and same_rule)),
            "same_end": pm_end == lm_end,
            "yes_mapped": yes_idx is not None and len(tokens) == 2,
        }
        fs = pm.get("feeSchedule") or {}
        return {
            "lm_slug": lm_slug, "pm_slug": md["externalSlug"],
            "lm_tokens": m["tokens"], "lm_fee": bool(md.get("fee")),
            "lm_taker_delay_ms": (m.get("settings") or {}).get("takerDelayMs"),
            "lm_min_size_lp": int((m.get("settings") or {}).get("minSize") or 0) / 1e6,
            "lm_max_spread_lp": float((m.get("settings") or {}).get("maxSpread") or 0),
            "pm_up_token": tokens[yes_idx] if checks["yes_mapped"] else None,
            "pm_down_token": tokens[1 - yes_idx] if checks["yes_mapped"] else None,
            "pm_fee_rate": float(fs.get("rate") or 0.07) if pm.get("feesEnabled", True) else 0.0,
            "pm_fee_exp": float(fs.get("exponent") or 1),
            "pm_min_order": float(pm.get("orderMinSize") or 5),
            "pm_tick": float(pm.get("orderPriceMinTickSize") or 0.01),
            "start_ts": _iso_to_ts(m["startAt"]) if m.get("startAt") else None,
            "end_ts": lm_end, "checks": checks, "verified": all(checks.values()),
            "oracle": lm_src,
        }

    def refresh(self):
        """Liste les marches recurrents crypto actifs et (re)construit les paires."""
        now = time.time()
        slugs = []
        for it in self.c.active_slugs():
            s = it.get("slug") or ""
            if _RECURRING.match(s):
                slugs.append(s)
        fresh = {}
        for s in slugs:
            try:
                p = self.pairs.get(s) or self.build_pair(s)
            except Exception as e:  # un marche casse ne doit pas bloquer les autres
                self._log(f"⚠️ [LMTS-PAIRS] {s}: {str(e)[:120]}")
                continue
            if p and p["end_ts"] > now:
                fresh[s] = p
        added = [fresh[s] for s in set(fresh) - set(self.pairs)]
        self.last_added = added
        refused = [p for p in added if not p["verified"]]
        for p in refused:  # rare et important : toujours visible
            self._log(f"⛔ [LMTS-PAIRS] {p['lm_slug']} <-> {p['pm_slug']} REFUSEE {p['checks']}")
        self.pairs = fresh
        return self.pairs
