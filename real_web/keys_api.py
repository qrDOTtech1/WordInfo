"""GESTION DES CLES DEPUIS LE DASHBOARD -- ECRITURE SEULE (Claude 28/09,
Steven : "un point pour modifier des cles sans pouvoir les lire").

Regles de securite :
  - Aucune valeur secrete n'est JAMAIS renvoyee : l'API ne repond que
    "defini / non defini" + un indice non secret (adresse publique derivee
    d'une cle privee, 4 derniers caracteres d'un token id).
  - Reserve a la machine locale (127.0.0.1) EN PLUS du Bearer global : sur un
    deploiement public, ces routes refusent tout.
  - Avant chaque remplacement, l'ancien .env est copie dans data/env_history/
    (gitignore) : une cle ecrasee par erreur = acces aux fonds perdu, donc on
    garde toujours la precedente.
  - Validation du format avant ecriture (cle privee 32 octets, adresse EIP-55,
    secret base64) : une faute de frappe est refusee, pas enregistree.
  - Les valeurs ne sont jamais loggees.
"""
import base64
import binascii
import os
import re
import shutil
import time
from pathlib import Path

from eth_account import Account
from flask import Blueprint, jsonify, request
from web3 import Web3

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
HISTORY_DIR = ROOT / "data" / "env_history"

bp = Blueprint("keys", __name__)
_hooks = []  # callables(changed_names:set) appeles apres un enregistrement


def on_change(fn):
    _hooks.append(fn)
    return fn


def _v_pk(v):
    h = v[2:] if v.lower().startswith("0x") else v
    if not re.fullmatch(r"[0-9a-fA-F]{64}", h):
        raise ValueError("cle privee invalide (64 caracteres hexadecimaux attendus)")
    Account.from_key("0x" + h)
    return "0x" + h.lower()


def _v_addr(v):
    if not Web3.is_address(v):
        raise ValueError("adresse invalide")
    return Web3.to_checksum_address(v)


def _v_b64(v):
    try:
        if len(base64.b64decode(v, validate=True)) < 16:
            raise ValueError
    except (binascii.Error, ValueError):
        raise ValueError("secret invalide (base64 attendu, tel que fourni par Limitless)")
    return v


def _v_text(v):
    if not re.fullmatch(r"[A-Za-z0-9_\-.:]{4,200}", v):
        raise ValueError("identifiant invalide")
    return v


def _v_url(v):
    if not re.fullmatch(r"https://[^\s]{6,400}", v):
        raise ValueError("URL invalide (https://... attendu)")
    return v


def _v_bool(v):
    if v not in ("0", "1"):
        raise ValueError("0 (desactive) ou 1 (active) attendu")
    return v


def _v_usd(v):
    try:
        x = float(v)
    except ValueError:
        raise ValueError("montant en $ attendu (ex: 200)")
    if not 0 <= x <= 1_000_000:
        raise ValueError("montant hors limites (0 - 1 000 000 $)")
    return str(round(x, 2))


def _hint_url(v):
    m = re.match(r"https://([^/]+)", v)
    return "defini (" + (m.group(1) if m else "?") + ")"


def _hint_pk(v):
    try:
        return "adresse " + Account.from_key(v if v.startswith("0x") else "0x" + v).address
    except Exception:
        return "defini (format illisible)"


KEYS = {
    # nom : (libelle, venue, validateur, indice, secret?, effacable?)
    "PRIVATE_KEY": ("Cle privee du wallet (EOA)", "Polymarket + Limitless", _v_pk, _hint_pk, True, False),
    "POLY_FUNDER_ADDRESS": ("Adresse funder / deposit wallet Polymarket", "Polymarket", _v_addr,
                            lambda v: v, False, False),
    "LMTS_TOKEN_ID": ("Token API Limitless - ID", "Limitless", _v_text, lambda v: "…" + v[-4:], True, True),
    "LMTS_TOKEN_SECRET": ("Token API Limitless - secret", "Limitless", _v_b64, lambda v: "defini", True, True),
    "LIMITLESS_PRIVATE_KEY": ("Cle privee dediee Limitless (optionnel, vide = meme cle)", "Limitless",
                              _v_pk, _hint_pk, True, True),
    # BANQUE (Claude 29/09, Steven : "les cles a config seront configurables dans l'onglet
    # cles comme les autres") : reequilibrage Base <-> Polygon + retraits (plus tard, reel).
    "BANK_WITHDRAW_ADDRESS": ("Adresse de retrait perso (MetaMask) - SEULE destination autorisee des retraits",
                              "Banque", _v_addr, lambda v: v, False, True),
    "POLYGON_RPC_URL": ("RPC Polygon (Polymarket : pont, soldes)", "Banque", _v_url, _hint_url, True, True),
    "LIMITLESS_RPC_URL": ("RPC Base (Limitless : pont, soldes, redeem)", "Banque", _v_url, _hint_url, True, True),
    "BANK_AUTO_REBALANCE": ("Reequilibrage automatique entre NOS 2 portefeuilles (0 = non, 1 = oui)", "Banque",
                            _v_bool, lambda v: "ACTIVE" if v == "1" else "desactive", False, True),
    "BANK_REBALANCE_MAX_USD": ("Plafond par transfert de reequilibrage ($)", "Banque", _v_usd,
                               lambda v: v + " $", False, True),
    "BANK_REBALANCE_MAX_DAY_USD": ("Plafond de reequilibrage par jour ($)", "Banque", _v_usd,
                                   lambda v: v + " $ / jour", False, True),
}


def _local_only():
    return request.remote_addr in ("127.0.0.1", "::1")


def _read_env_lines():
    return ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []


def _write_env(updates):
    """Remplace/ajoute les cles en gardant l'ordre et les commentaires du .env.
    Ecriture atomique (fichier temporaire + replace)."""
    lines = _read_env_lines()
    done = set()
    out = []
    for ln in lines:
        m = re.match(r"^\s*([A-Z0-9_]+)\s*=", ln)
        if m and m.group(1) in updates:
            out.append(f"{m.group(1)}={updates[m.group(1)]}")
            done.add(m.group(1))
        else:
            out.append(ln)
    for k, v in updates.items():
        if k not in done:
            out.append(f"{k}={v}")
    if ENV_PATH.exists():
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        stamp = f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1_000_000_000:09d}"
        shutil.copy2(ENV_PATH, HISTORY_DIR / f".env.{stamp}")
    tmp = ENV_PATH.with_suffix(".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.replace(tmp, ENV_PATH)


@bp.route("/api/keys/status")
def keys_status():
    if not _local_only():
        return jsonify({"error": "gestion des cles reservee a la machine locale"}), 403
    out = []
    for name, (label, venue, _v, hint, secret, clearable) in KEYS.items():
        val = (os.environ.get(name) or "").strip()
        out.append({"name": name, "label": label, "venue": venue, "set": bool(val),
                    "hint": hint(val) if val else None, "secret": secret, "clearable": clearable})
    return jsonify({"keys": out, "history_backups": len(list(HISTORY_DIR.glob(".env.*"))) if HISTORY_DIR.exists() else 0})


@bp.route("/api/keys/set", methods=["POST"])
def keys_set():
    if not _local_only():
        return jsonify({"ok": False, "error": "gestion des cles reservee a la machine locale"}), 403
    data = request.get_json(silent=True) or {}
    name, value, clear = data.get("name"), (data.get("value") or "").strip(), bool(data.get("clear"))
    if name not in KEYS:
        return jsonify({"ok": False, "error": "cle inconnue"}), 400
    label, _venue, validate, hint, _secret, clearable = KEYS[name]
    if clear:
        if not clearable:
            return jsonify({"ok": False, "error": "cette cle ne peut pas etre effacee, seulement remplacee"}), 400
        value = ""
    else:
        if not value:
            return jsonify({"ok": False, "error": "valeur vide"}), 400
        try:
            value = validate(value)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 400
    _write_env({name: value})
    os.environ[name] = value
    notes = []
    for fn in _hooks:
        try:
            r = fn({name})
            if r:
                notes.append(r)
        except Exception as e:
            notes.append(f"rechargement partiel : {str(e)[:120]}")
    return jsonify({"ok": True, "name": name, "set": bool(value),
                    "hint": hint(value) if value else None, "notes": notes})
