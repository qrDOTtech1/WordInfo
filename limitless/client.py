"""Client Limitless Exchange -- REST + HMAC + EIP-712 + chain Base.

Ecrit d'apres la doc officielle (docs/limitless/pages/*) et non devine :
  - auth       : developers__authentication.md (HMAC lmts-*, fenetre 30s)
  - ordres     : api-reference__trading__create-order.md + developers__eip712-signing.md
  - approvals  : developers__venue-system.md (USDC -> venue.exchange, CTF -> exchange[/adapter])
  - heartbeat  : api-reference__trading__heartbeats.md (annulation auto si le bot meurt)

GARDE-FOU : toute methode qui ECRIT (ordre, annulation, approve, redeem,
changement de profil) passe par _guard_write() -> en LIMITLESS_DRY_RUN (defaut)
elle ne fait RIEN et renvoie {"dry_run": True, ...}. Les lectures marchent
toujours, meme sans cles (endpoints publics).
"""
import base64
import hashlib
import hmac
import json
import threading
import time
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal

import requests
from eth_account import Account
from eth_account.messages import encode_typed_data
from web3 import Web3

from limitless import config

ZERO_ADDR = "0x0000000000000000000000000000000000000000"
BUY, SELL = 0, 1

ORDER_TYPES = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ],
    "Order": [
        {"name": "salt", "type": "uint256"},
        {"name": "maker", "type": "address"},
        {"name": "signer", "type": "address"},
        {"name": "taker", "type": "address"},
        {"name": "tokenId", "type": "uint256"},
        {"name": "makerAmount", "type": "uint256"},
        {"name": "takerAmount", "type": "uint256"},
        {"name": "expiration", "type": "uint256"},
        {"name": "nonce", "type": "uint256"},
        {"name": "feeRateBps", "type": "uint256"},
        {"name": "side", "type": "uint8"},
        {"name": "signatureType", "type": "uint8"},
    ],
}

_ERC20_ABI = [
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "a", "type": "address"}], "outputs": [{"type": "uint256"}]},
    {"name": "allowance", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "o", "type": "address"}, {"name": "s", "type": "address"}],
     "outputs": [{"type": "uint256"}]},
    {"name": "approve", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "s", "type": "address"}, {"name": "v", "type": "uint256"}],
     "outputs": [{"type": "bool"}]},
]
_CTF_ABI = [
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "account", "type": "address"}, {"name": "id", "type": "uint256"}],
     "outputs": [{"type": "uint256"}]},
    {"name": "isApprovedForAll", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "o", "type": "address"}, {"name": "op", "type": "address"}],
     "outputs": [{"type": "bool"}]},
    {"name": "setApprovalForAll", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "op", "type": "address"}, {"name": "ok", "type": "bool"}],
     "outputs": []},
    {"name": "redeemPositions", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "collateralToken", "type": "address"},
                {"name": "parentCollectionId", "type": "bytes32"},
                {"name": "conditionId", "type": "bytes32"},
                {"name": "indexSets", "type": "uint256[]"}],
     "outputs": []},
]


class LimitlessError(Exception):
    def __init__(self, status, body, path):
        self.status, self.body, self.path = status, body, path
        super().__init__(f"Limitless {status} sur {path}: {str(body)[:300]}")


class LimitlessClient:
    MARKET_CACHE_S = 600  # venue + tokens sont statiques par marche

    def __init__(self, log_fn=None):
        self._log = log_fn or (lambda m: print(m))
        self._http = requests.Session()
        self._http.headers["User-Agent"] = "MMTRADE/1.0"
        self._market_cache = {}
        self._profile = None
        self._lock = threading.Lock()
        self._w3 = None
        pk = config.private_key()
        self.account = Account.from_key(pk) if pk else None
        self.address = self.account.address if self.account else None

    _clock = {"offset_ms": 0.0, "ts": 0.0}

    @classmethod
    def clock_offset_ms(cls, max_age_s=300):
        """Decalage (ms) horloge serveur - horloge locale, mesure contre
        l'heure Binance (precision ms) au milieu de l'aller-retour. Limitless
        rejette (425) un ordre dont le timestamp devie de plus de 1000 ms : le
        PC de Steven avance de ~2 s (mesure 28/09), ce qui aurait fait rejeter
        TOUS les ordres reels. On corrige sans toucher a l'horloge systeme."""
        if time.time() - cls._clock["ts"] < max_age_s:
            return cls._clock["offset_ms"]
        best = None
        for _ in range(3):
            t0 = time.time()
            try:
                srv = requests.get("https://api.binance.com/api/v3/time", timeout=3).json()["serverTime"]
            except Exception:
                continue
            t1 = time.time()
            rtt = (t1 - t0) * 1000
            off = srv - (t0 + t1) / 2 * 1000
            if best is None or rtt < best[0]:
                best = (rtt, off)
        if best:
            cls._clock = {"offset_ms": best[1], "ts": time.time(), "rtt_ms": best[0]}
        return cls._clock["offset_ms"]

    @classmethod
    def server_now_ms(cls):
        return int(time.time() * 1000 + cls.clock_offset_ms())

    def reload_credentials(self):
        """Relit la cle privee apres une modification depuis le dashboard."""
        pk = config.private_key()
        self.account = Account.from_key(pk) if pk else None
        self.address = self.account.address if self.account else None
        self._profile = None

    # ── transport ──────────────────────────────────────────────────────
    @staticmethod
    def _sign_headers(method, path, body=""):
        ts = datetime.now(timezone.utc).isoformat()
        msg = f"{ts}\n{method}\n{path}\n{body}"
        sig = base64.b64encode(
            hmac.new(base64.b64decode(config.token_secret()), msg.encode("utf-8"),
                     hashlib.sha256).digest()
        ).decode("utf-8")
        return {"lmts-api-key": config.token_id(), "lmts-timestamp": ts, "lmts-signature": sig}

    def _request(self, method, path, body=None, auth=False, retries=3, timeout=10):
        """path inclut la query string (elle fait partie du message HMAC).
        Retry uniquement sur 429/5xx (doc : ne JAMAIS retenter 400/401)."""
        data = json.dumps(body, separators=(",", ":")) if body is not None else ""
        for attempt in range(retries + 1):
            headers = {}
            if auth:
                if not config.has_api_credentials():
                    raise LimitlessError(0, "LMTS_TOKEN_ID/LMTS_TOKEN_SECRET absents du .env", path)
                headers.update(self._sign_headers(method, path, data))
            if data:
                headers["Content-Type"] = "application/json"
            r = self._http.request(method, config.API_BASE + path, data=data or None,
                                   headers=headers, timeout=timeout)
            if r.status_code == 429 or r.status_code >= 500:
                if attempt < retries:
                    wait = float(r.headers.get("Retry-After") or (2 ** attempt))
                    time.sleep(min(wait, 10))
                    continue
            if r.status_code >= 400:
                try:
                    err = r.json()
                except ValueError:
                    err = r.text
                raise LimitlessError(r.status_code, err, path)
            if not r.content:
                return {}
            try:
                return r.json()
            except ValueError:
                return r.text
        raise LimitlessError(429, "rate limit persistant", path)

    def _guard_write(self, what, payload=None):
        if config.dry_run():
            self._log(f"🧪 [LIMITLESS DRY-RUN] {what} NON envoye : {json.dumps(payload)[:240] if payload else ''}")
            return {"dry_run": True, "what": what, "payload": payload}
        return None

    # ── lectures publiques ─────────────────────────────────────────────
    def maintenance(self):
        return self._request("GET", "/maintenance/status")

    def active_slugs(self):
        return self._request("GET", "/markets/active/slugs")

    def market(self, slug, fresh=False):
        c = self._market_cache.get(slug)
        if c and not fresh and time.time() - c[0] < self.MARKET_CACHE_S:
            return c[1]
        m = self._request("GET", f"/markets/{slug}")
        self._market_cache[slug] = (time.time(), m)
        return m

    def orderbook(self, slug):
        """Carnet normalise. L'API ne sert que le cote YES ; le cote NO s'en
        deduit (un BUY NO a p equivaut a un SELL YES a 1-p dans le CLOB CTF).
        Tailles converties en parts (raw / 1e6)."""
        ob = self._request("GET", f"/markets/{slug}/orderbook")
        bids = sorted(((float(x["price"]), int(x["size"]) / 1e6) for x in ob.get("bids", [])),
                      key=lambda t: -t[0])
        asks = sorted(((float(x["price"]), int(x["size"]) / 1e6) for x in ob.get("asks", [])),
                      key=lambda t: t[0])
        return {
            "yes_bids": bids, "yes_asks": asks,
            "no_bids": [(round(1 - p, 6), s) for p, s in asks],
            "no_asks": [(round(1 - p, 6), s) for p, s in bids],
            "min_size": int(ob.get("minSize") or 0) / 1e6,
            "max_spread": float(ob.get("maxSpread") or 0),
            "last": ob.get("lastTradePrice"),
            "ts": time.time(),
        }

    def trades(self, slug, limit=100, page=1):
        """Trades MINED publics (plus recent d'abord). side = cote TAKER
        (0=BUY, 1=SELL), sur le token YES. Cache CDN 30s cote Limitless."""
        return self._request("GET", f"/markets/{slug}/events?page={page}&limit={limit}")

    # ── compte (HMAC) ──────────────────────────────────────────────────
    def profile(self, fresh=False):
        if self._profile is None or fresh:
            self._profile = self._request("GET", "/profiles/me", auth=True)
        return self._profile

    def owner_id(self):
        return int(self.profile()["id"])

    def fee_rate_bps(self):
        return int((self.profile().get("rank") or {}).get("feeRateBps") or 0)

    def trade_wallet_mode(self):
        return self.profile().get("tradeWalletOption")

    def set_eoa_mode(self):
        """Obligatoire pour signer soi-meme (sinon 400 'Signer does not match').
        Reversible (smartWallet)."""
        g = self._guard_write("PUT /profiles tradeWalletOption=eoa")
        if g:
            return g
        r = self._request("PUT", "/profiles", {"tradeWalletOption": "eoa"}, auth=True)
        self._profile = None
        return r

    def positions(self):
        return self._request("GET", "/portfolio/positions", auth=True)

    def user_orders(self, slug):
        return self._request("GET", f"/markets/{slug}/user-orders", auth=True)

    def allowance_api(self, kind="clob"):
        return self._request("GET", f"/portfolio/trading/allowance?type={kind}", auth=True)

    # ── ordres ─────────────────────────────────────────────────────────
    @staticmethod
    def amounts(side, price, shares):
        """Montants bruts EXACTS (la doc exige price x contrats = collateral
        sans arrondi). price <= 3 decimales, parts arrondies au 0.001 inferieur
        -> collateral = prix_milli x parts_raw / 1000 toujours entier."""
        p = Decimal(str(price)).quantize(Decimal("0.001"), rounding=ROUND_DOWN)
        if not (Decimal("0.01") <= p <= Decimal("0.99")):
            raise ValueError(f"prix hors bornes 0.01-0.99 : {price}")
        sh_raw = int(Decimal(str(shares)) * 1_000_000) // 1000 * 1000
        coll_raw = int(p * 1000) * sh_raw // 1000
        if side == BUY:
            return float(p), coll_raw, sh_raw  # maker=collateral, taker=contrats
        return float(p), sh_raw, coll_raw      # maker=contrats, taker=collateral

    def build_order(self, slug, outcome, side, price, shares):
        """outcome 'yes'|'no'. Renvoie (order_signe, info). Ne poste rien."""
        if not self.account:
            raise LimitlessError(0, "aucune cle privee (LIMITLESS_PRIVATE_KEY/PRIVATE_KEY)", slug)
        m = self.market(slug)
        token = m["tokens"][outcome]
        exchange = m["venue"]["exchange"]
        fee_bps = 0
        if (m.get("metadata") or {}).get("fee"):
            if config.has_api_credentials():
                fee_bps = self.fee_rate_bps()
            elif not config.dry_run():
                raise LimitlessError(0, "feeRateBps inconnu sans token API (marche a frais)", slug)
        px, maker_amt, taker_amt = self.amounts(side, price, shares)
        if maker_amt < 100:
            raise ValueError("ordre trop petit (makerAmount < 100 raw)")
        addr = Web3.to_checksum_address(self.address)
        order = {
            "salt": int(time.time() * 1000) * 1000 + int.from_bytes(hashlib.sha256(
                f"{slug}{outcome}{side}{px}{time.time_ns()}".encode()).digest()[:2], "big") % 1000,
            "maker": addr, "signer": addr, "taker": ZERO_ADDR,
            "tokenId": str(token), "makerAmount": maker_amt, "takerAmount": taker_amt,
            "expiration": 0, "nonce": 0, "feeRateBps": fee_bps,
            "side": side, "signatureType": 0,
        }
        msg = {**order, "tokenId": int(token)}
        encoded = encode_typed_data(full_message={
            "types": ORDER_TYPES, "primaryType": "Order",
            "domain": {"name": "Limitless CTF Exchange", "version": "1",
                       "chainId": config.CHAIN_ID,
                       "verifyingContract": Web3.to_checksum_address(exchange)},
            "message": msg,
        })
        sig = self.account.sign_message(encoded).signature.hex()
        if not sig.startswith("0x"):
            sig = "0x" + sig
        signed = {**order, "salt": str(order["salt"]), "expiration": "0",
                  "price": px, "signature": sig}
        return signed, {"price": px, "shares": taker_amt / 1e6 if side == BUY else maker_amt / 1e6,
                        "collateral": maker_amt / 1e6 if side == BUY else taker_amt / 1e6,
                        "fee_bps": fee_bps, "exchange": exchange}

    def place_order(self, slug, outcome, side, price, shares, order_type="GTC",
                    post_only=False, client_order_id=None, recv_window_ms=3000):
        signed, info = self.build_order(slug, outcome, side, price, shares)
        body = {"order": signed, "ownerId": self.owner_id() if config.has_api_credentials() else 0,
                "orderType": order_type, "marketSlug": slug,
                "timestamp": self.server_now_ms(), "recvWindow": recv_window_ms}
        if post_only and order_type == "GTC":
            body["postOnly"] = True
        if client_order_id:
            body["clientOrderId"] = client_order_id[:128]
        g = self._guard_write(f"POST /orders {slug} {outcome} {'BUY' if side == BUY else 'SELL'} "
                              f"{info['shares']}@{info['price']} {order_type}", info)
        if g:
            return {**g, "info": info}
        r = self._request("POST", "/orders", body, auth=True, retries=0)
        return {"response": r, "info": info}

    def order_status(self, client_order_id):
        """Statut d'un ordre par clientOrderId (POST /orders/status/batch)."""
        r = self._request("POST", "/orders/status/batch", {"items": [{"clientOrderId": client_order_id}]},
                          auth=True, retries=1)
        return (r.get("results") or [{}])[0] if isinstance(r, dict) else {}

    def wait_fill(self, client_order_id, timeout_s=4.0):
        """Attend l'etat terminal d'un ordre taker (le delai taker renvoie DELAYED
        a la creation). -> (parts nettes recues, USDC brut, statut)."""
        t_end = time.time() + timeout_s
        last = None
        while time.time() < t_end:
            try:
                res = self.order_status(client_order_id)
            except LimitlessError:
                res = {}
            ex = (res.get("data") or {}).get("execution") or {}
            st = ex.get("settlementStatus")
            last = st
            if st in ("MATCHED", "MINED", "CONFIRMED", "FAILED") or (st == "UNMATCHED" and not ex.get("eligibleAt")):
                tot = ex.get("totalsRaw") or {}
                return int(tot.get("contractsNet") or 0) / 1e6, int(tot.get("usdGross") or 0) / 1e6, st
            time.sleep(0.25)
        return 0.0, 0.0, f"timeout ({last})"

    def cancel(self, order_id):
        g = self._guard_write(f"DELETE /orders/{order_id}")
        return g or self._request("DELETE", f"/orders/{order_id}", auth=True)

    def cancel_all(self, slug):
        g = self._guard_write(f"DELETE /orders/all/{slug}")
        return g or self._request("DELETE", f"/orders/all/{slug}", auth=True)

    def heartbeat(self, deadline_s=20):
        """Si le bot meurt, Limitless annule nos ordres apres deadline_s."""
        g = self._guard_write("POST /heartbeats")
        return g or self._request("POST", "/heartbeats",
                                  {"cancelAt": int((time.time() + deadline_s) * 1000)}, auth=True)

    # ── chain Base ─────────────────────────────────────────────────────
    def w3(self):
        if self._w3 is None:
            self._w3 = Web3(Web3.HTTPProvider(config.rpc_url(), request_kwargs={"timeout": 10}))
        return self._w3

    def balances(self):
        """USDC + ETH (gas) de l'EOA sur Base."""
        if not self.address:
            return None
        w3 = self.w3()
        usdc = w3.eth.contract(address=Web3.to_checksum_address(config.USDC_BASE), abi=_ERC20_ABI)
        return {"address": self.address,
                "usdc": usdc.functions.balanceOf(self.address).call() / 1e6,
                "eth": w3.eth.get_balance(self.address) / 1e18}

    def token_balance(self, token_id):
        """Parts detenues on-chain pour un token de position Limitless (CTF ERC-1155,
        6 decimales). Sert au rapprochement reel (Claude 29/09)."""
        if not self.address:
            return None
        ctf = self.w3().eth.contract(address=Web3.to_checksum_address(config.CTF_BASE), abi=_CTF_ABI)
        return ctf.functions.balanceOf(self.address, int(token_id)).call() / 1e6

    def approvals(self, slug):
        """Etat des approbations requises pour ce marche (venue system)."""
        m = self.market(slug)
        venue = m["venue"]
        w3 = self.w3()
        usdc = w3.eth.contract(address=Web3.to_checksum_address(config.USDC_BASE), abi=_ERC20_ABI)
        ctf = w3.eth.contract(address=Web3.to_checksum_address(config.CTF_BASE), abi=_CTF_ABI)
        out = {}
        spenders = [venue["exchange"]] + ([venue["adapter"]] if venue.get("adapter") else [])
        for sp in spenders:
            sp = Web3.to_checksum_address(sp)
            out[sp] = {
                "usdc_allowance": usdc.functions.allowance(self.address, sp).call() / 1e6,
                "ctf_approved": ctf.functions.isApprovedForAll(self.address, sp).call(),
            }
        return out

    def _send_tx(self, fn, what):
        g = self._guard_write(f"TX {what}")
        if g:
            return g
        w3 = self.w3()
        tx = fn.build_transaction({
            "from": self.address, "nonce": w3.eth.get_transaction_count(self.address),
            "chainId": config.CHAIN_ID,
        })
        signed = self.account.sign_transaction(tx)
        h = w3.eth.send_raw_transaction(signed.raw_transaction)
        rcpt = w3.eth.wait_for_transaction_receipt(h, timeout=120)
        self._log(f"⛓️ [LIMITLESS] {what} tx={h.hex()} status={rcpt.status}")
        return {"tx": h.hex(), "status": rcpt.status}

    def approve_market(self, slug):
        """Approbations one-shot (USDC max + CTF) vers exchange (+adapter NegRisk).
        Coute un peu d'ETH de gas sur Base."""
        w3 = self.w3()
        usdc = w3.eth.contract(address=Web3.to_checksum_address(config.USDC_BASE), abi=_ERC20_ABI)
        ctf = w3.eth.contract(address=Web3.to_checksum_address(config.CTF_BASE), abi=_CTF_ABI)
        res = []
        for sp, st in self.approvals(slug).items():
            if st["usdc_allowance"] < 1e9:
                res.append(self._send_tx(usdc.functions.approve(sp, 2 ** 256 - 1), f"approve USDC -> {sp}"))
            if not st["ctf_approved"]:
                res.append(self._send_tx(ctf.functions.setApprovalForAll(sp, True), f"approve CTF -> {sp}"))
        return res

    def redeem_onchain(self, condition_id):
        """Encaisse un marche resolu (EOA : on-chain, pas via /portfolio/redeem
        qui est reserve aux sous-comptes server-wallet)."""
        ctf = self.w3().eth.contract(address=Web3.to_checksum_address(config.CTF_BASE), abi=_CTF_ABI)
        fn = ctf.functions.redeemPositions(Web3.to_checksum_address(config.USDC_BASE),
                                           b"\x00" * 32, bytes.fromhex(condition_id[2:]), [1, 2])
        return self._send_tx(fn, f"redeem {condition_id[:12]}")
