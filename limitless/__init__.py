"""Connecteur LIMITLESS EXCHANGE (Base L2) pour MMTRADE (Claude 28/09, demande
Steven : "on va trader sur les deux en meme temps").

Modules :
  - config.py   : lecture .env (cles LMTS_*, garde DRY_RUN)
  - client.py   : REST + auth HMAC + signature EIP-712 des ordres + chain (Base)
  - pairs.py    : appariement Limitless <-> Polymarket (meme evenement, meme oracle)
  - shadow.py   : MESURE en mode ombre de l'edge inter-plateformes (aucun ordre)
  - preflight.py: verification complete avant de passer en reel
  - api.py      : endpoints Flask /api/limitless/* pour le dashboard

Doc officielle rapatriee dans docs/limitless/ (llms-full.txt, openapi.json,
pages/*.md, agents-starter/ = implementation de reference cross-market-mm).
"""
