"""PREFLIGHT Limitless -- a lancer apres avoir colle les cles dans .env :

    venv/Scripts/python -m limitless.preflight            (lecture seule)
    venv/Scripts/python -m limitless.preflight --fix      (passe le profil en mode EOA
                                                           + approuve les contrats ;
                                                           exige LIMITLESS_DRY_RUN=0)

Chaque ligne = un point bloquant pour le reel. Tout vert = on peut armer.
"""
import sys

from limitless import config
from limitless.client import LimitlessClient
from limitless.pairs import PairFinder

OK, KO, WARN = "✅", "❌", "⚠️"


def main(fix=False, client=None, pair_finder=None):
    """client/pair_finder : reutilise ceux du mesureur (dashboard) pour aller vite."""
    c = client or LimitlessClient(log_fn=print)
    bad = 0

    def line(ok, msg, warn=False):
        nonlocal bad
        print(f"{OK if ok else (WARN if warn else KO)} {msg}")
        if not ok and not warn:
            bad += 1

    line(bool(config.private_key()), f"cle privee EOA chargee -> {c.address}")
    line(config.has_api_credentials(), "token API LMTS_TOKEN_ID / LMTS_TOKEN_SECRET")
    try:
        mt = c.maintenance()
        line(True, f"API joignable, maintenance: {str(mt)[:120]}")
    except Exception as e:
        line(False, f"API injoignable : {e}")
    if config.has_api_credentials():
        try:
            p = c.profile(fresh=True)
            line(True, f"profil id={p.get('id')} rang={(p.get('rank') or {}).get('name')} "
                       f"feeRateBps={(p.get('rank') or {}).get('feeRateBps')}")
            addr_ok = (p.get("account") or "").lower() == (c.address or "").lower()
            line(addr_ok, f"le token appartient bien a l'EOA du bot (profil {p.get('account')})")
            mode = p.get("tradeWalletOption")
            if mode != "eoa" and fix:
                print("   -> passage en mode EOA :", c.set_eoa_mode())
                mode = c.profile(fresh=True).get("tradeWalletOption")
            line(mode == "eoa", f"mode de trading = {mode} (doit etre 'eoa' pour signer via API)")
        except Exception as e:
            line(False, f"profil illisible : {e}")
    try:
        b = c.balances()
        line(b["usdc"] > 1, f"USDC sur Base : {b['usdc']:.2f}$")
        line(b["eth"] > 0.0003, f"ETH (gas approbations/redeem) sur Base : {b['eth']:.6f}", warn=True)
    except Exception as e:
        line(False, f"lecture chain Base : {e}")
    pf = pair_finder or PairFinder(c, log_fn=lambda m: None)
    pairs = pf.pairs if (pair_finder and pf.pairs) else pf.refresh()
    ver = [p for p in pairs.values() if p["verified"]]
    line(bool(ver), f"paires Limitless<->Polymarket verifiees : {len(ver)}/{len(pairs)}")
    exchanges = {}
    for p in ver:
        ex = c.market(p["lm_slug"])["venue"]["exchange"]
        exchanges.setdefault(ex, p["lm_slug"])
    for ex, slug in exchanges.items():
        try:
            st = c.approvals(slug)
            good = all(v["usdc_allowance"] > 1e6 and v["ctf_approved"] for v in st.values())
            if not good and fix:
                print("   -> approbations :", c.approve_market(slug))
                st = c.approvals(slug)
                good = all(v["usdc_allowance"] > 1e6 and v["ctf_approved"] for v in st.values())
            line(good, f"approbations exchange {ex[:10]}… ({slug[:28]}) {st}")
        except Exception as e:
            line(False, f"approbations {ex[:10]}… : {e}")
    line(config.enabled(), "LIMITLESS_ENABLED=1", warn=True)
    line(not config.dry_run(), "LIMITLESS_DRY_RUN=0 (reel)", warn=True)
    print(f"\n{'PRET' if bad == 0 else f'{bad} point(s) bloquant(s)'}")
    return bad


if __name__ == "__main__":
    sys.exit(1 if main(fix="--fix" in sys.argv) else 0)
