"""Arb Limitless <-> Polymarket sur les marches EVENEMENTS (Claude 28/09).

Steven : "+ sera toujours mieux sans cracher sur la precision -- quitte a
elargir les marches au sport ou autre". Limitless marque lui-meme ~220 marches
(sport, politique, economie, prix d'actifs...) `metadata.isPolyArbitrage` avec
`externalSlug` = le marche Polymarket copie. Leur market maker mire le prix
Polymarket (ex. Eagles 0,6455 vs 0,645) : l'arb apparait quand Polymarket
bouge vite (match en direct, annonce) et que Limitless est en retard -- le
meme phenomene que sur les cryptos 15 min.

Une paire n'est VERIFIEE (donc tradable) que si :
  1. la REGLE de resolution est identique mot pour mot (normalisee) des deux
     cotes -- verifie sur echantillon : sport, politique, prix d'actifs ;
  2. l'echeance Limitless est entre 0 et 48 h APRES celle de Polymarket
     (sport : Limitless ajoute 24 h de marge) ;
  3. marche BINAIRE (2 issues) et YES Limitless = issue 0 Polymarket : "Yes",
     ou 1re equipe du titre "A vs. B" ; ET les prix affiches concordent
     (sinon mapping inverse -> refus) ;
  4. les deux marches sont ouverts et se terminent dans GENERIC_MAX_H.
"""
import json
import re
import time

from limitless.pairs import _RECURRING, PairFinder, _iso_to_ts, _rule_text

GENERIC_MAX_H = 7 * 24          # capital immobilise au plus 7 jours
END_TOLERANCE_S = 48 * 3600
PRICE_MISMATCH_MAX = 0.20       # |prix LM YES - prix PM issue 0| au-dela -> refus
_RECURRING_CATS = {"Hourly", "Daily", "15 min", "5 min", "Minutely", "Weekly"}
_ABBR = {"Sports": "SPORT", "Politics": "POLIT", "Economy": "ECO", "Crypto": "PRIX", "Commodities": "PRIX",
         "Company News": "SOCIETE", "Earnings": "SOCIETE", "Esports": "ESPORT", "Football": "FOOT",
         "Football Matches": "FOOT", "Specials": "SPECIAL", "Financials": "PRIX"}


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


class GenericPairFinder(PairFinder):
    """Meme interface que PairFinder (pairs / refresh / last_added)."""

    def _candidates(self):
        """(parent, sous-marche) pour tous les marches isPolyArbitrage actifs."""
        out, page = [], 1
        now = time.time()
        while page <= 60:
            r = self.c._request("GET", f"/markets/active?page={page}&limit=25")
            data = (r or {}).get("data") or []
            for m in data:
                md = m.get("metadata") or {}
                # seules les cryptos RECURRENTES (5m/15m/1h/1j, suivies par la mesure
                # crypto) sont exclues -- avant, toute la categorie "Daily" l'etait,
                # ce qui ratait les actions Up/Down (NFLX, PLTR, HOOD, SPCX) et les
                # groupes "quel prix atteindra BTC/ETH aujourd'hui" (Steven 28/09)
                if not md.get("isPolyArbitrage") or _RECURRING.match(m.get("slug") or ""):
                    continue
                exp = (m.get("expirationTimestamp") or 0) / 1000
                if not exp or exp - now > GENERIC_MAX_H * 3600 or exp < now:
                    continue
                if m.get("marketType") == "group":
                    try:
                        full = self.c.market(m["slug"])
                    except Exception:
                        continue
                    for sm in full.get("markets") or []:
                        if (sm.get("metadata") or {}).get("externalSlug") and not sm.get("expired"):
                            out.append((m, sm))
                elif md.get("externalSlug"):
                    out.append((m, m))
            if len(data) < 25:
                break
            page += 1
        return out

    def build_generic_pair(self, parent, sm):
        md = sm.get("metadata") or {}
        pm = self._pm_market(md["externalSlug"])
        if not pm or pm.get("closed") or not pm.get("clobTokenIds"):
            return None
        full = sm if sm.get("description") else self.c.market(sm["slug"])
        outcomes = json.loads(pm["outcomes"]) if isinstance(pm["outcomes"], str) else pm["outcomes"]
        tokens = json.loads(pm["clobTokenIds"]) if isinstance(pm["clobTokenIds"], str) else pm["clobTokenIds"]
        lm_end = int(sm.get("expirationTimestamp") or parent["expirationTimestamp"]) // 1000
        pm_end = _iso_to_ts(pm["endDate"]) if pm.get("endDate") else None
        lm_rule = _rule_text(full.get("description") or parent.get("description"))
        pm_rule = _rule_text(pm.get("description"))
        # mapping YES Limitless -> issue Polymarket
        low = [_norm(o) for o in outcomes]
        yes_idx = None
        if len(outcomes) == 2 and len(tokens) == 2:
            if low == ["yes", "no"]:
                yes_idx = 0
            elif low == ["up", "down"]:   # actions/indices Up or Down : YES = Up (meme convention que les cryptos)
                yes_idx = 0
            else:
                title = _norm(sm.get("title") if sm is not parent else parent.get("title"))
                if title.startswith(low[0]) and low[1] in title:
                    yes_idx = 0
        # garde-fou prix : le MM Limitless mire Polymarket -> prix proches si mapping juste
        price_ok = True
        try:
            lm_yes = float((sm.get("prices") or [None])[0])
            pm_px = [float(x) for x in json.loads(pm.get("outcomePrices") or "[]")]
            # seulement pour un mapping DEDUIT (equipes) : "Yes"<->"Yes" est certain,
            # et un ecart de prix y est justement l'arb recherche (ou un carnet vide)
            if yes_idx is not None and len(pm_px) == 2 and low != ["yes", "no"]:
                price_ok = abs(lm_yes - pm_px[yes_idx]) <= PRICE_MISMATCH_MAX
        except (TypeError, ValueError):
            pass
        checks = {
            "same_rule": bool(lm_rule) and lm_rule[:400] == pm_rule[:400],
            "same_end": pm_end is not None and 0 <= lm_end - pm_end <= END_TOLERANCE_S,
            "yes_mapped": yes_idx is not None,
            "prices_agree": price_ok,
            "pm_open": bool(pm.get("acceptingOrders", True)) and not pm.get("closed"),
        }
        cats = parent.get("categories") or []
        tag = next((_ABBR[c] for c in cats if c in _ABBR), "EVT")
        name = parent.get("title") or ""
        if sm is not parent and sm.get("title"):
            name = f"{sm['title'].strip()} · {name}"   # le seuil/candidat d'abord (sinon tronque)
        fs = pm.get("feeSchedule") or {}
        st = sm.get("settings") or parent.get("settings") or {}
        return {
            "lm_slug": sm["slug"], "pm_slug": md["externalSlug"],
            "lm_tokens": sm.get("tokens") or {}, "lm_fee": bool(md.get("fee", True)),
            "lm_taker_delay_ms": st.get("takerDelayMs"),
            "lm_min_size_lp": int(st.get("minSize") or 0) / 1e6,
            "lm_max_spread_lp": float(st.get("maxSpread") or 0),
            "pm_up_token": tokens[yes_idx] if yes_idx is not None else None,
            "pm_down_token": tokens[1 - yes_idx] if yes_idx is not None else None,
            "pm_yes_idx": yes_idx, "pm_outcomes": outcomes,
            "pm_fee_rate": float(fs.get("rate") or 0.07) if pm.get("feesEnabled", True) else 0.0,
            "pm_fee_exp": float(fs.get("exponent") or 1),
            "pm_min_order": float(pm.get("orderMinSize") or 5),
            "pm_tick": float(pm.get("orderPriceMinTickSize") or 0.01),
            "start_ts": None, "end_ts": lm_end, "pm_end_ts": pm_end,
            # match en direct : Polymarket retarde les ordres sport -> latence paper majoree
            "live_start_ts": md.get("startMatchTimestampInUTC"),
            "sport": "Sports" in cats or "Esports" in cats or "Football Matches" in cats or "NHL" in cats,
            "checks": checks, "verified": all(checks.values()),
            "oracle": pm.get("resolutionSource") or "uma",
            "coin": tag, "bucket": "event", "label": f"{tag} {name[:64]}", "category": ",".join(cats),
        }

    def refresh(self):
        now = time.time()
        fresh = {}
        for parent, sm in self._candidates():
            s = sm["slug"]
            try:
                p = self.pairs.get(s) or self.build_generic_pair(parent, sm)
            except Exception as e:
                self._log(f"⚠️ [EVT-PAIRS] {s}: {str(e)[:120]}")
                continue
            if p and p["end_ts"] > now:
                fresh[s] = p
        added = [fresh[s] for s in set(fresh) - set(self.pairs)]
        self.last_added = added
        ok = [p for p in added if p["verified"]]
        refused = [p for p in added if not p["verified"]]
        if added:
            why = {}
            for p in refused:
                for k, v in p["checks"].items():
                    if not v:
                        why[k] = why.get(k, 0) + 1
            self._log(f"🧭 [EVT-PAIRS] +{len(ok)} paires evenements VERIFIEES, {len(refused)} refusees {why or ''} "
                      f"| {sum(1 for p in fresh.values() if p['verified'])} tradables au total")
        self.pairs = fresh
        return self.pairs
