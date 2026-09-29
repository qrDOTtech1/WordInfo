"""DATALAKE -- base de donnees massive des marches crypto Up/Down (Claude 28/09,
Steven : "pour tout marche crypto les 5min + 15min + 1h, toutes les minutes ou
plus court, pour se creer une DB de data massive").

recorder.py : enregistre CHAQUE SECONDE le carnet de TOUS les marches crypto
Up/Down de Polymarket (7 cryptos x 5m/15m/1h/4h/journalier) et de Limitless,
+ le spot Binance, la profondeur 5 niveaux (toutes les 5 s), les trades
publics et le resultat de chaque fenetre. Fichier : voir db_path() (hors OneDrive).
"""

import os
from pathlib import Path


def data_dir():
    r"""Dossier de la base massive. JAMAIS dans OneDrive : OneDrive intercepte
    chaque ecriture SQLite (mesure 28/09 : 2-5 s par commit au lieu de 1-2 ms)
    et re-uploaderait en permanence un fichier de plusieurs Go/jour.
    Priorite : DATALAKE_DIR (.env) > D:\MMTRADE_DATA > %LOCALAPPDATA%\MMTRADE."""
    d = os.environ.get("DATALAKE_DIR")
    if not d:
        d = r"D:\MMTRADE_DATA" if os.path.isdir("D:\\") else os.path.join(
            os.environ.get("LOCALAPPDATA", str(Path.home())), "MMTRADE")
    Path(d).mkdir(parents=True, exist_ok=True)
    return Path(d)


def db_path():
    return data_dir() / "marketdata.db"
