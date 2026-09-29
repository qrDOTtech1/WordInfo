"""Configuration Limitless, lue depuis le .env du projet.

Variables (toutes optionnelles tant que Limitless n'est pas arme) :
  LMTS_TOKEN_ID        id du token API scoped (limitless.exchange -> profil -> Api keys -> Derive)
  LMTS_TOKEN_SECRET    secret base64 du token (affiche UNE seule fois a la creation)
  LIMITLESS_PRIVATE_KEY cle EOA qui signe les ordres Limitless. Vide = PRIVATE_KEY
                       (meme EOA que Polymarket : c'est le modele "une seule cle"
                       de l'implementation officielle cross-market-mm)
  LIMITLESS_ENABLED    1 = le moteur a le droit d'utiliser Limitless
  LIMITLESS_DRY_RUN    1 (DEFAUT) = aucun ordre n'est poste, tout est logge.
                       Il faut ecrire explicitement 0 pour passer en reel.
  LIMITLESS_RPC_URL    RPC Base (defaut https://mainnet.base.org)
"""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

API_BASE = "https://api.limitless.exchange"
WS_URL = "wss://ws.limitless.exchange"
CHAIN_ID = 8453  # Base
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
CTF_BASE = "0xC9c98965297Bc527861c898329Ee280632B76e18"
DATA_DIR = ROOT / "data"


def _env(name, default=""):
    return (os.environ.get(name) or default).strip()


def token_id():
    return _env("LMTS_TOKEN_ID")


def token_secret():
    return _env("LMTS_TOKEN_SECRET")


def private_key():
    pk = _env("LIMITLESS_PRIVATE_KEY") or _env("PRIVATE_KEY")
    if pk and not pk.startswith("0x"):
        pk = "0x" + pk
    return pk


def enabled():
    return _env("LIMITLESS_ENABLED", "0") == "1"


def dry_run():
    # Fail-safe : tout ce qui n'est pas exactement "0" = dry-run.
    return _env("LIMITLESS_DRY_RUN", "1") != "0"


def rpc_url():
    return _env("LIMITLESS_RPC_URL", "https://mainnet.base.org")


def has_api_credentials():
    return bool(token_id() and token_secret())
