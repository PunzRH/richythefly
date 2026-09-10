"""Pons V2 on Robinhood Chain, read straight from the contracts. No third-party price API.

Discovery: every launch is a LAUNCH event on the Pons factories, so the fly sees each
coin the block it is born. Pricing: each coin lives on a bonding curve (constant product
with a phantom quote reserve); price = quoteReserve / tokenReserve. Verified 2026-09-10
against eth_call simulation: a buy deducts feeBps + creatorTaxBps (+ any snipe tax) from
the ETH in, then tokensOut = tokenReserve * in / (quoteReserve + in).

Execution: curve.buy(quoteIn, minTokensOut, recipient) payable and
curve.sell(tokensIn, minQuoteOut, recipient). Signing key comes from the local .env
(HOODFLY_PK) and never leaves this process.
"""

import json
import os
import re
import time
import urllib.request
from decimal import Decimal

from .config import D

CHAIN_ID = 4663
FACTORIES = [
    "0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e",
    "0xD3C2280f23d813BE8F9A6B5452753efFF7799fd7",
    "0x7E1EAbd52Ae29598e6483F72dCf1a70b14284dB8",
    "0x050e5C224466e2d377a7E555E139D51268239b39",
]
LAUNCH_TOPIC = "0x8d4aad4953d0ca700d468f3753aa14432d1b35b43ec6409f051fb6aa43a89607"
PUBLIC_RPCS = [
    "https://robinhood-rpc.publicnode.com",
    "https://rpc.mainnet.chain.robinhood.com",
]
# curve selectors (resolved from bytecode + signature db, 2026-09-10)
SEL = {
    "getReserves": "0902f1ac",
    "realQuoteReserve": "4f1f58fd",
    "feeBps": "24a9d853",
    "creatorTaxBps": "c1bb8901",
    "graduated": "e7c2b772",
    "isNativeQuote": "dc08e094",
    "graduationThreshold": "8b0bc501",
    "launchedAt": "bf56b371",
    "currentSnipeTaxBps": "d7e1ef39",
    "maxInternalPriceImpactBps": "90addc1e",
    "token": "fc0c546a",
    "buy": "59a87bc1",
    "sell": "d04c6983",
    "symbol": "95d89b41",
    "name": "06fdde03",
    "balanceOf": "70a08231",
    "allowance": "dd62ed3e",
    "approve": "095ea7b3",
}
WEI = Decimal(10) ** 18


def _pad(addr):
    return addr.lower().replace("0x", "").rjust(64, "0")


def _u(hexdata, i=0):
    h = hexdata[2:] if hexdata.startswith("0x") else hexdata
    return int(h[i * 64 : (i + 1) * 64] or "0", 16)


def _str(hexdata):
    h = hexdata[2:] if hexdata.startswith("0x") else hexdata
    if len(h) < 128:  # bytes32-style symbol
        return bytes.fromhex(h).rstrip(b"\0").decode(errors="replace")
    n = int(h[64:128], 16)
    return bytes.fromhex(h[128 : 128 + n * 2]).decode(errors="replace")


class Rpc:
    """Tiny JSON-RPC client with batch support and endpoint failover."""

    def __init__(self, urls=None):
        env = os.environ.get("RH_RPC_URL", "").strip()
        self.urls = [u for u in ([env] if env else []) + PUBLIC_RPCS if u]

    def _post(self, payload):
        body = json.dumps(payload).encode()
        last = None
        for url in self.urls:
            try:
                req = urllib.request.Request(
                    url, data=body, headers={"content-type": "application/json", "user-agent": "hoodfly/1.0"}
                )
                return json.load(urllib.request.urlopen(req, timeout=30))
            except Exception as e:  # try the next endpoint
                last = e
        raise RuntimeError(f"all RPC endpoints failed: {last}")

    def call(self, method, params):
        r = self._post({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if "error" in r:
            raise RuntimeError(f"rpc {method}: {r['error']}")
        return r["result"]

    def batch(self, calls):
        """calls = [(method, params), ...] -> results in order (None on per-item error)."""
        if not calls:
            return []
        out = [None] * len(calls)
        step = 40  # public/Chainstack endpoints drop oversized batches
        for start in range(0, len(calls), step):
            chunk = calls[start : start + step]
            payload = [{"jsonrpc": "2.0", "id": start + i, "method": m, "params": p} for i, (m, p) in enumerate(chunk)]
            try:
                r = self._post(payload)
            except Exception:
                r = None
            if not isinstance(r, list):  # endpoint refused batching
                for i, (m, p) in enumerate(chunk):
                    out[start + i] = self._one(m, p)
                continue
            for item in r:
                if isinstance(item, dict) and "result" in item and isinstance(item.get("id"), int):
                    out[item["id"]] = item["result"]
        return out

    def _one(self, m, p):
        try:
            return self.call(m, p)
        except Exception:
            return None

    def eth_call(self, to, sel, args=""):
        return self.call("eth_call", [{"to": to, "data": "0x" + sel + args}, "latest"])

    def block_number(self):
        return int(self.call("eth_blockNumber", []), 16)


class Pons:
    def __init__(self, rpc=None):
        self.rpc = rpc or Rpc()
        self.coins = {}  # coin address -> {"curve","pair","block","symbol","name"}
        self._scanned_to = None

    # ---- discovery ------------------------------------------------------------------
    def scan_launches(self, from_block, to_block):
        logs = self.rpc.call(
            "eth_getLogs",
            [{"address": FACTORIES, "topics": [LAUNCH_TOPIC], "fromBlock": hex(from_block), "toBlock": hex(to_block)}],
        ) or []
        return self._ingest(logs)

    def _ingest(self, logs):
        new = []
        for l in logs:
            t = l["topics"]
            coin = "0x" + t[1][-40:]
            if coin in self.coins:
                continue
            self.coins[coin] = {
                "coin": coin,
                "curve": "0x" + t[2][-40:],
                "pair": "0x" + l["data"][2:66][-40:],
                "block": int(l["blockNumber"], 16),
                "deployer": "0x" + t[3][-40:] if len(t) > 3 else None,
                "symbol": None,
                "name": None,
            }
            new.append(coin)
        return new

    def discover(self, lookback_blocks=6000, chunk=2000):
        """First call scans the recent window; later calls only scan new blocks."""
        head = self.rpc.block_number()
        start = (self._scanned_to + 1) if self._scanned_to else max(0, head - lookback_blocks)
        new = []
        ranges = []
        b = start
        while b <= head:
            e = min(head, b + chunk - 1)
            ranges.append((b, e))
            b = e + 1
        # one batched request for the whole window instead of a slow sequential scan
        res = self.rpc.batch([
            ("eth_getLogs", [{"address": FACTORIES, "topics": [LAUNCH_TOPIC], "fromBlock": hex(f), "toBlock": hex(t)}])
            for f, t in ranges
        ])
        for (f, t), logs in zip(ranges, res):
            if logs is None:  # chunk failed: fall back to a direct call so nothing is missed
                new += self.scan_launches(f, t)
            else:
                new += self._ingest(logs)
        self._scanned_to = head
        return new  # names/symbols are loaded lazily for coins that enter the fly's view

    def load_meta(self, coins):
        calls = []
        for c in coins:
            calls.append(("eth_call", [{"to": c, "data": "0x" + SEL["symbol"]}, "latest"]))
            calls.append(("eth_call", [{"to": c, "data": "0x" + SEL["name"]}, "latest"]))
        res = self.rpc.batch(calls)
        for i, c in enumerate(coins):
            sym, name = res[2 * i], res[2 * i + 1]
            try:
                s = _str(sym) if sym else ""
            except Exception:
                s = ""
            s = re.sub(r"[^A-Za-z0-9]", "", s).upper()[:12] or "COIN"
            try:
                n = _str(name) if name else ""
            except Exception:
                n = ""
            self.coins[c]["symbol"] = s
            self.coins[c]["name"] = n[:48]

    # ---- curve state ------------------------------------------------------------------
    def curve_states(self, coins, recipient=None):
        """Batch-read every curve the fly cares about. Returns {coin: state or None}."""
        keys = ["getReserves", "realQuoteReserve", "feeBps", "creatorTaxBps", "graduated", "isNativeQuote", "graduationThreshold", "launchedAt"]
        calls = []
        for c in coins:
            cv = self.coins[c]["curve"]
            for k in keys:
                calls.append(("eth_call", [{"to": cv, "data": "0x" + SEL[k]}, "latest"]))
            calls.append(("eth_call", [{"to": cv, "data": "0x" + SEL["currentSnipeTaxBps"] + _pad(recipient or cv)}, "latest"]))
        res = self.rpc.batch(calls)
        n = len(keys) + 1
        out = {}
        for i, c in enumerate(coins):
            r = res[i * n : (i + 1) * n]
            if any(x is None for x in r[:6]):
                out[c] = None
                continue
            try:
                qr, tr = _u(r[0], 0), _u(r[0], 1)
                out[c] = {
                    "quote_reserve": qr,
                    "token_reserve": tr,
                    "real_quote": _u(r[1]),
                    "fee_bps": _u(r[2]),
                    "creator_bps": _u(r[3]),
                    "graduated": bool(_u(r[4])),
                    "native": bool(_u(r[5])),
                    "threshold": _u(r[6]) if r[6] else 0,
                    "launched_at": _u(r[7]) if r[7] else 0,
                    "snipe_bps": _u(r[8]) if r[8] else 0,
                }
            except Exception:
                out[c] = None
        return out

    # ---- pricing (verified against chain simulation) ----------------------------------
    @staticmethod
    def buy_out(state, quote_in_wei):
        cut = 10000 - state["fee_bps"] - state["creator_bps"] - state["snipe_bps"]
        qn = quote_in_wei * cut // 10000
        return state["token_reserve"] * qn // (state["quote_reserve"] + qn)

    @staticmethod
    def sell_out(state, tokens_in_wei):
        gross = state["quote_reserve"] * tokens_in_wei // (state["token_reserve"] + tokens_in_wei)
        cut = 10000 - state["fee_bps"] - state["creator_bps"]
        return gross * cut // 10000

    @staticmethod
    def spot(state):
        """ETH per token (Decimal)."""
        if state["token_reserve"] == 0:
            return D(0)
        return D(state["quote_reserve"]) / D(state["token_reserve"])

    # ---- wallet -----------------------------------------------------------------------
    def eth_balance(self, addr):
        return int(self.rpc.call("eth_getBalance", [addr, "latest"]), 16)

    def token_balance(self, token, addr):
        return _u(self.rpc.eth_call(token, SEL["balanceOf"], _pad(addr)))

    def allowance(self, token, owner, spender):
        return _u(self.rpc.eth_call(token, SEL["allowance"], _pad(owner) + _pad(spender)))


class Signer:
    """Holds the fly's wallet. Key from HOODFLY_PK in the local .env only."""

    def __init__(self, rpc, pk=None):
        from web3 import Web3

        pk = (pk or os.environ.get("HOODFLY_PK", "")).strip()
        if not pk:
            raise RuntimeError("Set HOODFLY_PK in ./.env (a fresh wallet funded with a little ETH on Robinhood Chain)")
        url = rpc.urls[0]
        self.w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 30}))
        if self.w3.eth.chain_id != CHAIN_ID:
            raise RuntimeError("RPC is not Robinhood Chain (chainId 4663)")
        self.acct = self.w3.eth.account.from_key(pk)
        self.address = self.acct.address

    def sign(self, to, data_hex, value=0, gas=None):
        """Build + sign. Returns (raw_tx_bytes, tx_hash) so the hash can be persisted BEFORE broadcast."""
        w3 = self.w3
        tx = {
            "chainId": CHAIN_ID,
            "from": self.address,
            "to": w3.to_checksum_address(to),
            "data": data_hex,
            "value": int(value),
            "nonce": w3.eth.get_transaction_count(self.address, "pending"),
        }
        gp = w3.eth.gas_price
        tx["gasPrice"] = int(gp * 1.2) + 1
        tx["gas"] = int((gas or w3.eth.estimate_gas(tx)) * 1.25)
        signed = self.acct.sign_transaction(tx)
        h = signed.hash.hex()
        return signed.raw_transaction, (h if h.startswith("0x") else "0x" + h)

    def broadcast(self, raw):
        h = self.w3.eth.send_raw_transaction(raw).hex()
        return h if h.startswith("0x") else "0x" + h

    def send(self, to, data_hex, value=0, gas=None):
        raw, h = self.sign(to, data_hex, value, gas)
        return self.broadcast(raw)

    def estimate(self, to, data_hex, value=0):
        return self.w3.eth.estimate_gas({"from": self.address, "to": self.w3.to_checksum_address(to), "data": data_hex, "value": int(value)})

    def receipt(self, tx_hash, timeout=90):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                r = self.w3.eth.get_transaction_receipt(tx_hash)
                if r is not None:
                    return r
            except Exception:
                pass
            time.sleep(1.5)
        return None


def buy_calldata(quote_in, min_out, recipient):
    return "0x" + SEL["buy"] + hex(quote_in)[2:].rjust(64, "0") + hex(min_out)[2:].rjust(64, "0") + _pad(recipient)


def sell_calldata(tokens_in, min_out, recipient):
    return "0x" + SEL["sell"] + hex(tokens_in)[2:].rjust(64, "0") + hex(min_out)[2:].rjust(64, "0") + _pad(recipient)


def approve_calldata(spender, amount):
    return "0x" + SEL["approve"] + _pad(spender) + hex(amount)[2:].rjust(64, "0")
