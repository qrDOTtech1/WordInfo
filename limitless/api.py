"""Endpoints Flask Limitless (Claude 28/09). Enregistres par real_web/server.py,
donc proteges par le meme Bearer MMTRADE_API_TOKEN que le reste de l'API.

  GET /api/limitless/status   config (cles presentes ? dry-run ?), wallet Base, profil
  GET /api/limitless/shadow   agregats de la mesure en mode ombre
  GET /api/limitless/pairs    paires Limitless <-> Polymarket et leur verification
  GET /api/limitless/events   derniers evenements de la mesure (?n=100)
"""
import json
import time

from flask import Blueprint, jsonify, request

from limitless import config
from limitless.shadow import CrossVenueShadow

bp = Blueprint("limitless", __name__)
_shadow = None
_shadow_events = None   # marches evenements (sport, politique, eco...) : limitless/generic.py


def start_shadow(log_fn=None, pm_source=None):
    global _shadow
    if _shadow is None:
        _shadow = CrossVenueShadow(log_fn=log_fn, pm_source=pm_source)
        _shadow.start()
    return _shadow


def start_events_shadow(log_fn=None):
    global _shadow_events
    if _shadow_events is None:
        _shadow_events = CrossVenueShadow(log_fn=log_fn, mode="events")
        _shadow_events.start()
    return _shadow_events


@bp.route("/api/limitless/status")
def status():
    out = {"enabled": config.enabled(), "dry_run": config.dry_run(),
           "api_credentials": config.has_api_credentials(),
           "private_key": bool(config.private_key()), "ts": time.time()}
    c = _shadow.c if _shadow else None
    if c:
        out["address"] = c.address
        try:
            out["base"] = c.balances()
        except Exception as e:
            out["base_error"] = str(e)[:160]
        if config.has_api_credentials():
            try:
                p = c.profile(fresh=True)
                out["profile"] = {"id": p.get("id"), "trade_wallet_mode": p.get("tradeWalletOption"),
                                  "fee_rate_bps": (p.get("rank") or {}).get("feeRateBps"),
                                  "rank": (p.get("rank") or {}).get("name")}
            except Exception as e:
                out["profile_error"] = str(e)[:200]
    return jsonify(out)


@bp.route("/api/limitless/preflight")
def preflight():
    """Verification complete en LECTURE SEULE (jamais --fix depuis le web)."""
    import contextlib
    import io

    from limitless import preflight as pf

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        bad = pf.main(fix=False, client=_shadow.c if _shadow else None,
                      pair_finder=_shadow.pf if _shadow else None)
    return jsonify({"ok": bad == 0, "blocking": bad, "report": buf.getvalue()})


@bp.route("/api/limitless/shadow")
def shadow():
    return jsonify(_shadow.summary() if _shadow else {"error": "mesure non demarree"})


@bp.route("/api/limitless/windows")
def windows():
    """Fenetres en cours (prix live des 2 venues, meilleur arb vu) + closes."""
    if not _shadow:
        return jsonify({"live": [], "closed": []})
    n = min(int(request.args.get("n", 60)), 500)
    ev = _shadow_events
    ev_live = ev.live_windows() if ev else []
    # evenements : les plus proches d'un arb d'abord (cout combine minimum)
    ev_live.sort(key=lambda r: ((r.get("best_arb") or {}).get("edge") is None,
                                (r.get("closest") or {}).get("cost", 9)))
    return jsonify({"live": _shadow.live_windows(), "closed": list(_shadow.closed)[-n:][::-1],
                    "summary": _shadow.summary(),
                    "events": {"live": ev_live[:n], "n_live": len(ev_live),
                               "closed": list(ev.closed)[-n:][::-1] if ev else [],
                               "summary": ev.summary() if ev else None}})


@bp.route("/api/limitless/pairs")
def pairs():
    if not _shadow:
        return jsonify([])
    allp = list(_shadow.pf.pairs.values()) + (list(_shadow_events.pf.pairs.values()) if _shadow_events else [])
    return jsonify([{**{k: p.get(k) for k in ("lm_slug", "pm_slug", "verified", "checks", "end_ts", "oracle")},
                     "label": p.get("label"), "bucket": p.get("bucket")} for p in allp])


@bp.route("/api/limitless/events")
def events():
    n = min(int(request.args.get("n", 100)), 2000)
    path = config.DATA_DIR / "limitless_shadow.jsonl"
    if not path.exists():
        return jsonify([])
    lines = path.read_text(encoding="utf-8").splitlines()[-n:]
    return jsonify([json.loads(x) for x in lines if x.strip()])


@bp.route("/api/limitless/xarb/reset-kill", methods=["POST"])
def xarb_reset_kill():
    """Remet a zero le coupe-circuit de l'arb REEL (apres analyse de la cause)."""
    from limitless import xarb as _x
    ex = _x.EXECUTOR
    if ex is None:
        return jsonify({"ok": False, "error": "executeur non demarre"})
    return jsonify({"ok": True, "guard": ex.reset_kill()})


@bp.route("/api/limitless/xarb")
def xarb_status():
    from limitless import xarb as _x
    ex = _x.EXECUTOR
    return jsonify(ex.summary() if ex else {"error": "executeur non demarre"})
