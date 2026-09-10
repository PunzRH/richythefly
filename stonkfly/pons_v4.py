"""Graduated Pons coins: trading in their Uniswap V4 pool on Robinhood Chain.

When a curve graduates, Pons opens a V4 pool: currency0 = ETH (0x0), currency1 = coin,
fee 0 (dynamic), tickSpacing 200, hook 0xe5e7026… (takes ~3% of the output as its fee).
Bot-made junk pools with 80-90% fees also exist for the same coins; only the hook pool
is real. Pool id is deterministic (keccak of the PoolKey), so no log scanning.

Route = the same router Pons' own site uses (0x65050a9b…, `swap(routes[],addr,in,minOut,deadline)`),
copied byte-for-byte from live buys/sells (2026-09-10). Sells need ERC20 approve(router).
Exact buy quotes come from eth_simulateV1 (Swap + Transfer logs); sell quotes from the pool
price with the hook fee and a haircut, or exact via simulation once the router is approved.
"""

import time

from web3 import Web3

from .pons import SEL, _pad, _u

ROUTER = "0x65050a9b7e5075a2ba5ced7b1b64ee66262c40dc"
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
HOOK = "0xe5e702641ea86f4ae6cc3cdaed2b886f976be044"
ETH = "0x" + "0" * 40
TICK_SPACING = 200
HOOK_FEE = 0.03       # measured: 3% of output to the hook
SELL_HAIRCUT = 0.015  # price impact allowance when quoting a sell without simulation
POOLS_SLOT = 6        # PoolManager._pools mapping slot (v4-core)
SWAP_TOPIC = "0x" + Web3.keccak(text="Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)").hex().replace("0x", "")
TRANSFER_TOPIC = "0x" + Web3.keccak(text="Transfer(address,address,uint256)").hex().replace("0x", "")


def _w(v):
    return hex(int(v))[2:].rjust(64, "0")


def pool_id(coin):
    key = bytes.fromhex(_pad(ETH) + _pad(coin) + _w(0) + _w(TICK_SPACING) + _pad(HOOK))
    return "0x" + Web3.keccak(key).hex().replace("0x", "")


def swap_calldata(token_in, token_out, amount_in, min_out, deadline=None):
    deadline = deadline or int(time.time()) + 600
    words = [_w(0xA0), _w(0), _w(amount_in), _w(min_out), _w(deadline), _w(1), _w(0x20), _w(2),
             _pad(token_in), _pad(token_out), _w(0), _w(0), _w(TICK_SPACING), _pad(HOOK), _w(0x140), _pad(POOL_MANAGER), _w(0), _w(0)]
    return "0x4d819a2a" + "".join(words)


def approve_router_calldata(amount=2**256 - 1):
    return "0x" + SEL["approve"] + _pad(ROUTER) + _w(amount)


class V4:
    def __init__(self, rpc):
        self.rpc = rpc

    def slot0(self, coin):
        """(sqrtPriceX96, tick) or None if the hook pool is not initialized."""
        slot = "0x" + Web3.keccak(bytes.fromhex(pool_id(coin)[2:]) + POOLS_SLOT.to_bytes(32, "big")).hex().replace("0x", "")
        try:
            v = int(self.rpc.eth_call(POOL_MANAGER, "1e2eaeaf", slot[2:]), 16)  # extsload(bytes32)
        except Exception:
            return None
        sqrt = v & ((1 << 160) - 1)
        if sqrt == 0:
            return None
        tick = (v >> 160) & 0xFFFFFF
        tick = tick - 2**24 if tick >= 2**23 else tick
        return sqrt, tick

    def spot(self, coin):
        """ETH per token (float) from the pool price, or None."""
        s = self.slot0(coin)
        if not s:
            return None
        coins_per_eth = (s[0] / 2**96) ** 2
        return 1 / coins_per_eth if coins_per_eth > 0 else None

    def simulate(self, sender, coin, token_in, token_out, amount_in):
        """Exact output (wei) for a swap from `sender`, via eth_simulateV1. None if it would revert."""
        data = swap_calldata(token_in, token_out, amount_in, 0)
        call = {"from": sender, "to": ROUTER, "data": data}
        if token_in == ETH:
            call["value"] = hex(amount_in)
        try:
            sim = self.rpc.call("eth_simulateV1", [{"blockStateCalls": [{"calls": [call]}], "validation": False, "traceTransfers": True}, "latest"])
            c = sim[0]["calls"][0]
        except Exception:
            return None
        if c.get("status") != "0x1":
            return None
        out = 0
        for l in c.get("logs", []):
            if l["topics"][0] == TRANSFER_TOPIC and l["address"].lower() == token_out.lower() and len(l["topics"]) > 2 and l["topics"][2][-40:].lower() == sender[2:].lower():
                out += int(l["data"], 16)
        if token_out == ETH:
            # ETH leaves as a WETH unwrap; count value transfers to the sender
            for l in c.get("logs", []):
                if l["address"].lower() == "0x0bd7d308f8e1639fab988df18a8011f41eacad73" and l["topics"][0] == TRANSFER_TOPIC and l["topics"][2][-40:].lower() == sender[2:].lower():
                    out += int(l["data"], 16)
            if out == 0:
                for t in c.get("traceTransfers", c.get("transfers", [])) or []:
                    if str(t.get("to", "")).lower() == sender.lower():
                        out += int(t.get("value", "0x0"), 16)
        return out or None

    def quote_buy(self, sender, coin, eth_in):
        return self.simulate(sender, coin, ETH, coin, eth_in)

    def quote_sell(self, sender, coin, tokens_in, approved=False):
        if approved:
            out = self.simulate(sender, coin, coin, ETH, tokens_in)
            if out:
                return out
        spot = self.spot(coin)
        if not spot:
            return None
        return int(tokens_in * spot * (1 - HOOK_FEE) * (1 - SELL_HAIRCUT))


if __name__ == "__main__":
    import os, sys
    from .pons import Rpc
    rpc = Rpc(); v = V4(rpc)
    coin = sys.argv[1] if len(sys.argv) > 1 else "0x149667b7e8f79b754a537d68c835551080e7ed44"
    print("pool id", pool_id(coin))
    print("slot0", v.slot0(coin), "spot ETH/token", v.spot(coin))
    me = os.environ.get("HOODFLY_ADDRESS", "0x08E26febEB68ad943BA40302efb9b62ddF0258C4")
    q = v.quote_buy(me, coin, 2 * 10**15)
    print("buy 0.002 ETH ->", q and q / 1e18, "tokens; implied ETH/token", q and 2e15 / q)
    print("sell 1.8M tokens (pool math) ->", v.quote_sell(me, coin, int(1.8e24)) / 1e18, "ETH")
