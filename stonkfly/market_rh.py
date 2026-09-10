"""Robinhood-Chain / Pons V2 market for the fly, read straight from the contracts.

Drop-in for CoinbaseMarket: `snapshot() -> {product: Quote}`, `history`, `record()`.
Plus `choose()`: THE FLY PICKS WHICH COIN TO TRADE ITSELF.

Universe: every Pons launch (factory LAUNCH events) in the last hour, newest first,
ETH-paired, on the curve OR graduated into its V4 pool, plus anything the fly holds.
No token list is given to it. Nothing here is a third-party price feed.

Product ids are "SYMBOL-0xcoinaddress". Quotes are EXECUTABLE ETH-per-token prices:
  ask = ETH you pay per token to buy `order_eth` worth (fees + impact included)
  bid = ETH you get per token selling what you hold (fees + impact included)

Smell (per coin, all from chain): ETH raised, ETH/min flowing in, progress to graduation,
age, and the CROWD: unique buyers, share of the biggest buyer, buys bundled into the launch
block. Wash-traded "fake charts" look like one or two wallets doing all the volume; real
volume looks like many wallets. These become the feature bucket the taste memory learns on.
"""

import math
import os
import random
import time

from web3 import Web3

from .config import D
from .market import Quote
from .pons import WEI, Pons
from .pons_v4 import ROUTER, V4

BLOCKS_PER_HOUR = 36000  # ~0.1 s blocks
HOT_WINDOW = 3000        # ~5 min of blocks for the volume leaderboard
SWAP_TOPIC = "0x" + Web3.keccak(text="Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)").hex().replace("0x", "")
CURVE_BUY = "0x" + Web3.keccak(text="CurveBuy(address,address,uint256,uint256,uint256,uint256)").hex().replace("0x", "")
CURVE_SELL = "0x" + Web3.keccak(text="CurveSell(address,address,uint256,uint256,uint256,uint256)").hex().replace("0x", "")
ROUTER_SWAP = "0x" + Web3.keccak(text="Swap(address,address,address,uint256,uint256,(uint8,address,address,address,uint24,int24,address,bytes,address,bytes32)[])").hex().replace("0x", "")
CROWD_SCAN_BLOCKS = 20000  # launch-block bundle scan only for coins younger than this


def _i(x):
    """int of an eth_call result; 0 for empty/None (e.g. a curve that is not a curve)."""
    try:
        return int(x, 16) if x and len(x) > 2 else 0
    except Exception:
        return 0
IMPACT_CAP = 0.0145      # keeps (1+x)^2-1 under the curve's 3% max price impact
TRANSFER = "0x" + Web3.keccak(text="Transfer(address,address,uint256)").hex().replace("0x", "")


class RobinhoodChainMarket:
    def __init__(self, order_eth="0.002", universe=20, lookback_blocks=6 * BLOCKS_PER_HOUR, wallet=None, pons=None):
        self.pons = pons or Pons()
        self.v4 = V4(self.pons.rpc)
        self.order_eth = D(order_eth)
        self.universe = int(universe)
        self.lookback = int(lookback_blocks)
        self.wallet = wallet or "0x0000000000000000000000000000000000000001"
        self.products = ()
        self.meta = {}      # product -> {coin, curve, symbol, name, block}
        self.states = {}    # product -> last curve state (+ "graduated")
        self.history = {}   # product -> [mid prices]
        self.hype = {}      # product -> smell features (for UI + forager)
        self.features = {}  # product -> feature bucket key (for taste memory)
        self.holdings = {}  # product -> tokens held (set by the run loop each tick)
        self._last = {}     # product -> (real_quote, t) for momentum
        self._peak = {}     # coin -> highest real ETH seen on its curve (rug detection)
        self._crowd = {}    # coin -> {"scanned": block, "buyers": {addr: tokens}, "buys": n, "bundle": n}
        self._first = True
        self._graduated = []  # newest graduated ETH-paired coins (refreshed every few minutes)
        self._grad_at = 0.0
        self._hot = []         # coins with the most buys in the last few minutes, any age
        self._hot_at = 0.0
        self.activity = {}     # coin -> buys in the hot window (for the smell)

    # ---- helpers ----------------------------------------------------------------------
    def _product(self, coin):
        m = self.pons.coins[coin]
        return f"{m['symbol'] or 'COIN'}-{coin}"

    @staticmethod
    def _quote(product, bid, ask, now):
        return Quote(
            product=product, bid=bid, ask=ask, timestamp=now,
            base_increment=D("0.000001"), quote_increment=D("1e-12"),
            price_increment=D("1e-15"), minimum_quote=D("0.00001"), minimum_base=D("1"),
        )

    def _max_sell(self, s):
        return int(s["token_reserve"] * IMPACT_CAP / (1 - IMPACT_CAP))

    # ---- crowd: who is actually buying (from the coin's Transfer events) -------------------
    def _scan_crowd(self, coins, head):
        calls, spans = [], []
        for coin in coins:
            m = self.pons.coins[coin]
            c = self._crowd.setdefault(coin, {"scanned": m["block"] - 1, "buyers": {}, "buys": 0, "bundle": 0})
            frm = c["scanned"] + 1
            if not m["block"] or head - m["block"] > CROWD_SCAN_BLOCKS:
                continue
            while frm <= head:
                to = min(head, frm + 8999)
                calls.append(("eth_getLogs", [{"address": coin, "topics": [TRANSFER, "0x" + m["curve"][2:].rjust(64, "0")], "fromBlock": hex(frm), "toBlock": hex(to)}]))
                spans.append((coin, to))
                frm = to + 1
        if not calls:
            return
        res = self.pons.rpc.batch(calls)
        for (coin, to), logs in zip(spans, res):
            if logs is None:
                continue
            c = self._crowd[coin]
            launch = self.pons.coins[coin]["block"]
            for l in logs:
                who = "0x" + l["topics"][2][-40:]
                amt = int(l["data"], 16)
                c["buyers"][who] = c["buyers"].get(who, 0) + amt
                c["buys"] += 1
                if int(l["blockNumber"], 16) <= launch + 2:
                    c["bundle"] += 1
            c["scanned"] = max(c["scanned"], to)

    # ---- market contract ----------------------------------------------------------------
    def snapshot(self):
        self.pons.discover(lookback_blocks=self.lookback if self._first else 0)
        self._first = False
        held_coins = {p.split("-", 1)[1] for p in self.holdings}
        fresh = sorted(self.pons.coins.values(), key=lambda m: m["block"], reverse=True)
        pick = [m["coin"] for m in fresh if int(m["pair"], 16) == 0][: self.universe * 2]
        self._refresh_graduated(fresh)
        self._refresh_hot(fresh)
        # Composition of what he smells each tick: the whole registry is thousands of coins; this is the
        # slice he can afford to read in one tick. Hot = chain-wide by activity, any age.
        n_hot = max(6, int(self.universe * 0.42))
        n_grad = max(4, int(self.universe * 0.17))
        n_new = max(6, int(self.universe * 0.25))
        n_explore = max(3, int(self.universe * 0.16))
        pool = [m["coin"] for m in fresh if int(m["pair"], 16) == 0 and m["block"]]
        explore = random.sample(pool, min(n_explore, len(pool))) if pool else []
        coins = list(dict.fromkeys(list(held_coins) + self._hot[:n_hot] + self._graduated[:n_grad] + pick[:n_new] + explore))
        own = (os.environ.get("RICHY_COIN") or "").lower()
        if own:
            coins = [c for c in coins if c.lower() != own]  # the dev never trades his own coin
        need = [c for c in coins if self.pons.coins[c].get("symbol") is None]
        if need:
            self.pons.load_meta(need)
        states = self.pons.curve_states(coins, self.wallet)
        head = self.pons.rpc.block_number()
        self._scan_dev(coins)
        try:
            self._scan_crowd([c for c in coins if states.get(c)], head)
        except Exception:
            pass
        now = time.time()  # stamp AFTER the chain reads so freshness reflects the data, not the scan
        result, products, meta = {}, [], {}
        for coin in coins:
            s = states.get(coin)
            product = self._product(coin)
            held = product in self.holdings
            if s is None:
                if held and product in self.states:
                    s = self.states[product]
                else:
                    continue
            if not s["native"]:
                continue
            if s["graduated"]:
                q = self._quote_v4(product, coin, held)
            else:
                q = self._quote_curve(product, s, held)
            if q is None:
                continue
            bid, ask, spot = q
            if not held and len(products) >= self.universe + 10:
                continue
            result[product] = self._quote(product, bid, ask, now)
            products.append(product)
            m = self.pons.coins[coin]
            meta[product] = {"coin": coin, "curve": m["curve"], "symbol": m["symbol"], "name": m["name"], "block": m["block"], "venue": "v4" if s["graduated"] else "curve"}
            self.states[product] = s
            self.history.setdefault(product, [])
            self._smell(product, coin, s, spot, now)
        if not result:
            raise RuntimeError("No tradeable Pons coins on the curve right now")
        self.products = tuple(products)
        self.meta = meta
        self.hype = {p: self.hype[p] for p in products if p in self.hype}
        return result

    def _refresh_graduated(self, fresh):
        """Every few minutes, find which recent ETH-paired launches have graduated (one cheap read each)."""
        if time.time() - self._grad_at < 300:
            return
        self._grad_at = time.time()
        cands = [m["coin"] for m in fresh if int(m["pair"], 16) == 0][:600]
        try:
            res = self.pons.rpc.batch([("eth_call", [{"to": self.pons.coins[c]["curve"], "data": "0xe7c2b772"}, "latest"]) for c in cands])
        except Exception:
            return
        self._graduated = [c for c, x in zip(cands, res) if _i(x)]

    def _scan_dev(self, coins):
        """Deployer's share of supply: a dev sitting on a big bag is the rug-ball setup."""
        if not hasattr(self, "_dev"):
            self._dev = {}
        need = [c for c in coins if self.pons.coins[c].get("deployer")]
        # coins registered without a deployer: look it up on the factory once
        miss = [c for c in coins if not self.pons.coins[c].get("deployer")]
        if miss:
            res = self.pons.rpc.batch([("eth_call", [{"to": "0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e", "data": "0x3cf28b5a" + c[2:].rjust(64, "0")}, "latest"]) for c in miss])
            for c, x in zip(miss, res):
                if x and len(x) > 2 + 3 * 64:
                    self.pons.coins[c]["deployer"] = "0x" + x[2 + 2 * 64: 2 + 3 * 64][-40:]
                    need.append(c)
        if not need:
            return
        res = self.pons.rpc.batch([("eth_call", [{"to": c, "data": "0x70a08231" + self.pons.coins[c]["deployer"][2:].rjust(64, "0")}, "latest"]) for c in need])
        for c, x in zip(need, res):
            try:
                self._dev[c] = _i(x) / 1e27  # share of the 1B launch supply
            except Exception:
                pass

    def _refresh_hot(self, fresh):
        """Chain-wide volume sense, any age. Two log queries cover EVERY Pons coin:
        every curve emits CurveBuy/CurveSell, and every graduated trade goes through the
        Pons router's Swap. Coins the fly has never seen get registered on the spot, so a
        coin launched days ago that is moving now is just as visible as a fresh launch."""
        if time.time() - self._hot_at < 60:
            return
        self._hot_at = time.time()
        rpc = self.pons.rpc
        head = rpc.block_number()
        frm = hex(max(0, head - HOT_WINDOW))
        stats = {}  # coin -> {"trades","buyers":{addr: eth},"eth","buys":[(block, eth, who)],"sells":n}
        def bump(coin, who, eth, block=0, is_buy=True):
            st = stats.setdefault(coin, {"trades": 0, "buyers": {}, "eth": 0.0, "buys": [], "sells": 0})
            st["trades"] += 1
            st["eth"] += eth
            if is_buy:
                st["buys"].append((block, eth, who))
                if who:
                    st["buyers"][who] = st["buyers"].get(who, 0.0) + eth
            else:
                st["sells"] += 1
        by_curve = {m["curve"].lower(): c for c, m in self.pons.coins.items()}
        unknown_curves = set()
        try:
            logs = rpc.call("eth_getLogs", [{"topics": [[CURVE_BUY, CURVE_SELL]], "fromBlock": frm, "toBlock": "latest"}]) or []
            pending = []
            for l in logs:
                cv = l["address"].lower()
                coin = by_curve.get(cv)
                if coin is None:
                    unknown_curves.add(cv)
                pending.append((cv, l))
            if unknown_curves:
                self._register_curves(list(unknown_curves))
                by_curve = {m["curve"].lower(): c for c, m in self.pons.coins.items()}
            for cv, l in pending:
                coin = by_curve.get(cv)
                if not coin:
                    continue
                is_buy = l["topics"][0] == CURVE_BUY
                eth = int(l["data"][2:66], 16) / 1e18 if is_buy else 0.0
                bump(coin, "0x" + l["topics"][1][-40:] if is_buy else None, eth, int(l["blockNumber"], 16), is_buy)
        except Exception:
            pass
        try:
            from .pons_v4 import HOOK, ROUTER
            logs = rpc.call("eth_getLogs", [{"address": ROUTER, "topics": [ROUTER_SWAP], "fromBlock": frm, "toBlock": "latest"}]) or []
            hook = HOOK[2:].lower()
            new_coins = set()
            for l in logs:
                d = l["data"][2:]
                words = [d[i * 64:(i + 1) * 64] for i in range(len(d) // 64)]
                for i, w in enumerate(words):
                    if i >= 5 and w[-40:].lower() == hook:
                        tin, tout = "0x" + words[i - 5][-40:], "0x" + words[i - 4][-40:]
                        coin = tout if int(tin, 16) == 0 else tin if int(tout, 16) == 0 else None
                        if not coin:
                            continue
                        coin = coin.lower()
                        if coin not in self.pons.coins:
                            new_coins.add(coin)
                        eth = min(int(words[0], 16), int(words[1], 16)) / 1e18 if int(tin, 16) == 0 else 0.0  # ETH leg is the smaller amount
                        bump(coin, "0x" + l["topics"][1][-40:] if int(tin, 16) == 0 else None, eth, int(l["blockNumber"], 16), int(tin, 16) == 0)
            if new_coins:
                self._register_coins(list(new_coins))
        except Exception:
            pass
        self.activity = stats
        self._hot = [c for c, st in sorted(stats.items(), key=lambda kv: kv[1]["trades"], reverse=True) if st["trades"] >= 3 and c in self.pons.coins]

    def _register_curves(self, curves):
        """Unknown curve addresses (older coins) -> their coins, registered for trading."""
        res = self.pons.rpc.batch([("eth_call", [{"to": cv, "data": "0xfc0c546a"}, "latest"]) for cv in curves])
        coins = []
        for cv, x in zip(curves, res):
            if x and len(x) >= 66:
                coin = "0x" + x[-40:]
                if coin not in self.pons.coins:
                    self.pons.coins[coin] = {"coin": coin, "curve": cv, "pair": "0x" + "0" * 40, "block": 0, "symbol": None, "name": None}
                    coins.append(coin)
        if coins:
            self._fill_pairs(coins)
            self.pons.load_meta(coins)

    def _register_coins(self, coins):
        """Unknown coins seen in the graduation router -> factory lookup for their curve."""
        res = self.pons.rpc.batch([("eth_call", [{"to": "0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e", "data": "0x3cf28b5a" + c[2:].rjust(64, "0")}, "latest"]) for c in coins])
        got = []
        for c, x in zip(coins, res):
            if x and len(x) > 2 + 15 * 64 and int(x[2 + 14 * 64: 2 + 15 * 64], 16):
                self.pons.coins[c] = {"coin": c, "curve": "0x" + x[2 + 64: 2 + 128][-40:], "pair": "0x" + x[2 + 4 * 64: 2 + 5 * 64][-40:], "block": 0, "deployer": "0x" + x[2 + 2 * 64: 2 + 3 * 64][-40:], "symbol": None, "name": None}
                got.append(c)
        if got:
            self.pons.load_meta(got)

    def _fill_pairs(self, coins):
        res = self.pons.rpc.batch([("eth_call", [{"to": self.pons.coins[c]["curve"], "data": "0xdc08e094"}, "latest"]) for c in coins])
        for c, x in zip(coins, res):
            if not _i(x):
                self.pons.coins[c]["pair"] = "0x" + "1" * 40  # not ETH-paired: excluded from the fly's universe

    def _quote_curve(self, product, s, held):
        spot = Pons.spot(s)
        if spot <= 0:
            return None
        if s["snipe_bps"] > 400 and not held:  # never buy into the opening snipe tax
            return None
        qin = int(self.order_eth * WEI)
        if qin > int(IMPACT_CAP * s["quote_reserve"]):
            return None
        out = Pons.buy_out(s, qin)
        if out <= 0:
            return None
        ask = D(qin) / D(out)
        tokens = int(self.holdings.get(product, D(0)) * WEI) or out
        tokens = min(tokens, self._max_sell(s))
        got = Pons.sell_out(s, tokens) if tokens > 0 else 0
        bid = D(got) / D(tokens) if tokens > 0 and got > 0 else spot * D("0.97")
        if bid > ask:
            bid = ask
        if not held and (ask - bid) / bid > D("0.2"):
            return None  # too wide to trade at this size; never let one odd curve halt the run
        return bid, ask, spot

    def _quote_v4(self, product, coin, held):
        spot_f = self.v4.spot(coin)
        if not spot_f:
            return None  # graduated but hook pool not live yet
        spot = D(str(spot_f))
        qin = int(self.order_eth * WEI)
        out = self.v4.quote_buy(self.wallet, coin, qin)
        if not out:
            return None
        ask = D(qin) / D(out)
        tokens = int(self.holdings.get(product, D(0)) * WEI) or out
        approved = False
        if held:
            try:
                approved = self.pons.allowance(coin, self.wallet, ROUTER) >= tokens
            except Exception:
                approved = False
        got = self.v4.quote_sell(self.wallet, coin, tokens, approved)
        bid = D(got) / D(tokens) if got else spot * D("0.94")
        if bid > ask:
            bid = ask
        if not held and (ask - bid) / bid > D("0.2"):
            return None
        return bid, ask, spot

    def _smell(self, product, coin, s, spot, now):
        real = s["real_quote"] / 1e18
        prev = self._last.get(product)
        flow = (real - prev[0]) / max(now - prev[1], 1) * 60 if prev else 0.0  # ETH/min net into the curve
        self._last[product] = (real, now)
        peak = max(self._peak.get(coin, 0.0), real); self._peak[coin] = peak
        rugged = (not s["graduated"]) and peak > 0.02 and real < 0.3 * peak   # curve drained: the money left
        age = max(0.0, now - s["launched_at"]) if s["launched_at"] else 0.0
        progress = min(1.0, real / (s["threshold"] / 1e18)) if s["threshold"] else (1.0 if s["graduated"] else 0.0)
        c = self._crowd.get(coin) or {"buyers": {}, "buys": 0, "bundle": 0}
        st = self.activity.get(coin) if isinstance(self.activity.get(coin), dict) else None
        if st and st["buyers"]:
            buyers = len(st["buyers"])
            total = sum(st["buyers"].values()) or 1e-18
            top_share = max(st["buyers"].values()) / total
        else:
            buyers = len(c["buyers"])
            total = sum(c["buyers"].values()) or 1
            top_share = max(c["buyers"].values()) / total if c["buyers"] else 0.0
        self.hype[product] = {
            "symbol": self.pons.coins[coin]["symbol"],
            "real_eth": round(real, 5),
            "flow_eth_min": round(flow, 5),
            "progress": round(progress, 4),
            "age_s": int(age),
            "graduated": s["graduated"],
            "held": product in self.holdings,
            "buyers": buyers,
            "buys": c["buys"],
            "top_buyer_share": round(top_share, 3),
            "bundled_buys": c["bundle"],
            "dev_share": round(getattr(self, "_dev", {}).get(coin, 0.0), 4),
            "rugged": rugged,
            "peak_eth": round(peak, 4),
            "fake_score": 1.0 if rugged else round(self._fake_score(buyers, c["buys"], top_share, c["bundle"], getattr(self, "_dev", {}).get(coin, 0.0)), 2),
            "trades_5m": (self.activity.get(coin) or {}).get("trades", 0) if isinstance(self.activity.get(coin), dict) else 0,
            "eth_5m": round((self.activity.get(coin) or {}).get("eth", 0.0), 4) if isinstance(self.activity.get(coin), dict) else 0.0,
        }
        age_b = "new" if age < 120 else "young" if age < 900 else "old"
        prog_b = "rug" if rugged else "grad" if s["graduated"] else "cold" if progress < 0.02 else "warm" if progress < 0.25 else "hot"
        flow_b = "in" if flow > 0 else "out" if flow < 0 else "flat"
        crowd_b = "solo" if buyers < 3 else "few" if buyers < 10 else "crowd"
        dev_share = getattr(self, "_dev", {}).get(coin, 0.0)
        conc_b = "devbag" if dev_share > 0.05 else "whale" if top_share > 0.5 else "spread"
        ramp = self._ramp_score(st)
        self.hype[product]["ramp_score"] = round(ramp, 2)
        if ramp >= 0.5:
            self.hype[product]["fake_score"] = round(min(1.0, self.hype[product]["fake_score"] + ramp), 2)
        shape_b = "ramp" if ramp >= 0.5 else "organic"
        self.features[product] = f"{age_b}|{prog_b}|{flow_b}|{crowd_b}|{conc_b}|{shape_b}"

    @staticmethod
    def _ramp_score(st):
        """Bundler ramp: many buys of near-identical size in consecutive blocks, few wallets, no sells."""
        if not st or len(st["buys"]) < 8:
            return 0.0
        buys = sorted(st["buys"])
        sizes = [b[1] for b in buys if b[1] > 0]
        if len(sizes) < 8:
            return 0.0
        mean = sum(sizes) / len(sizes)
        cv = (sum((x - mean) ** 2 for x in sizes) / len(sizes)) ** 0.5 / mean if mean > 0 else 9
        gaps = [buys[i + 1][0] - buys[i][0] for i in range(len(buys) - 1)]
        tight = sum(1 for g in gaps if g <= 3) / len(gaps)          # share of buys landing within 3 blocks of the previous
        diversity = len({b[2] for b in buys if b[2]}) / len(buys)   # distinct wallets per buy
        sells = st["sells"]
        score = 0.0
        if cv < 0.35: score += 0.35          # same-sized clips
        if tight > 0.7: score += 0.25        # machine cadence
        if diversity < 0.4: score += 0.25    # few wallets doing the buying
        if sells == 0 and len(buys) >= 12: score += 0.25  # nobody ever sells into a real pump; bots do not
        return min(1.0, score)

    @staticmethod
    def _fake_score(buyers, buys, top_share, bundle, dev_share=0.0):
        """0 = looks organic, 1 = looks like one wallet painting a chart or a dev sitting on a rug bag."""
        s = 0.0
        if dev_share > 0.05:
            s += min(0.6, (dev_share - 0.05) * 6 + 0.3)  # 5 % dev bag → +0.3, 10 % → +0.6
        if buys >= 3 and buyers <= 2:
            s += 0.5
        s += max(0.0, top_share - 0.4) * 0.8
        if buys and bundle / buys > 0.5:
            s += 0.3
        return min(1.0, s)

    def price_for(self, product, side, amount):
        """Executable ETH-per-token price for a specific size: BUY amount = ETH in, SELL amount = tokens."""
        s = self.states.get(product); coin = product.split("-", 1)[1]
        if not s:
            return None
        try:
            if s["graduated"]:
                if side == "BUY":
                    out = self.v4.quote_buy(self.wallet, coin, int(D(amount) * WEI))
                    return D(amount) / (D(out) / WEI) if out else None
                got = self.v4.quote_sell(self.wallet, coin, int(D(amount) * WEI), self.pons.allowance(coin, self.wallet, ROUTER) >= int(D(amount) * WEI))
                return (D(got) / WEI) / D(amount) if got and D(amount) > 0 else None
            if side == "BUY":
                qin = int(D(amount) * WEI)
                if qin > int(IMPACT_CAP * s["quote_reserve"]):
                    qin = int(IMPACT_CAP * s["quote_reserve"])  # the curve's own 3% impact rule caps the fill
                out = Pons.buy_out(s, qin)
                return (D(qin) / WEI) / (D(out) / WEI) if out else None
            tokens = min(int(D(amount) * WEI), self._max_sell(s))
            got = Pons.sell_out(s, tokens)
            return (D(got) / WEI) / (D(tokens) / WEI) if got and tokens else None
        except Exception:
            return None

    def max_buy_eth(self, product):
        """Largest ETH buy the venue itself allows this tick (curve impact cap; unlimited on V4)."""
        s = self.states.get(product)
        if not s or s["graduated"]:
            return None
        return D(int(IMPACT_CAP * s["quote_reserve"])) / WEI

    def record(self, quotes):
        for p, q in quotes.items():
            h = self.history.setdefault(p, [])
            h.append(float((q.bid + q.ask) / 2))
            del h[:-120]

    # ---- the fly's choice --------------------------------------------------------------
    def choose(self, tick, quotes, positions, taste):
        """Forage. Holdings get a look (so the fly can sell); otherwise follow the scent.

        scent  = money flowing in now, how many different wallets are buying, progress
        fake   = wash-trade smell (few wallets, one whale, bundled launch buys) pushes it away
        taste  = what this fly has learned about coins that smelled like this (see taste.py)
        curiosity keeps it exploring so it does not fixate.
        """
        held = [p for p, v in positions.items() if v > 0 and p in quotes]
        if held and random.random() < min(0.6, 0.3 * len(held)):
            return random.choice(held)
        burned = taste.burned() if taste else set()
        cands = [p for p in quotes if p not in held and p.split("-", 1)[1].lower() not in burned and not self.hype[p].get("rugged")]
        if not cands:
            return held[0] if held else next(iter(quotes))
        scores = []
        for p in cands:
            h = self.hype[p]
            scent = (math.log1p(max(h["flow_eth_min"], 0) * 50) + 0.6 * math.log1p(h["real_eth"] * 20)
                     + 0.4 * min(h["progress"], 1) + 0.5 * math.log1p(h["buyers"]) + 0.6 * math.log1p(h.get("trades_5m", 0)))
            fake = 2.0 * h["fake_score"]
            learned = taste.value(self.features[p]) if taste else 0.0
            curiosity = random.random() * 0.8
            scores.append(scent - fake + learned + curiosity)
        m = max(scores)
        w = [math.exp(s - m) for s in scores]
        r = random.random() * sum(w)
        acc = 0.0
        for p, x in zip(cands, w):
            acc += x
            if r <= acc:
                return p
        return cands[-1]


class FixtureRHMarket:
    """Deterministic offline Pons-like curves for testing the loop with no network."""

    def __init__(self, order_eth="0.002", **_):
        self.order_eth = D(order_eth)
        self.names = ["PONSCAT", "HOODDOG", "ROBINAPE"]
        self.products = tuple(f"{n}-0x{i:040x}" for i, n in enumerate(self.names, 1))
        self.meta = {p: {"coin": p.split("-")[1], "curve": "0xfixture", "symbol": n, "name": n, "block": 0, "venue": "curve"} for p, n in zip(self.products, self.names)}
        self.history = {p: [] for p in self.products}
        self.states = {p: {"graduated": False} for p in self.products}
        self.hype = {}
        self.features = {p: "young|warm|in|few|spread" for p in self.products}
        self.holdings = {}
        self.tick = 0

    def snapshot(self):
        self.tick += 1
        now = time.time()
        result = {}
        for j, p in enumerate(self.products):
            base = D("1.7e-9") * (1 + j)
            mid = base * D(str(1 + 0.08 * math.sin(self.tick * 0.4 + j)))
            result[p] = Quote(
                product=p, bid=mid * D("0.98"), ask=mid * D("1.02"), timestamp=now,
                base_increment=D("0.000001"), quote_increment=D("1e-12"),
                price_increment=D("1e-15"), minimum_quote=D("0.00001"), minimum_base=D("1"),
            )
            self.hype[p] = {"symbol": self.names[j], "real_eth": 0.05 * (j + 1), "flow_eth_min": 0.01 * math.sin(self.tick * 0.3 + j), "progress": 0.05 * (j + 1), "age_s": 300, "graduated": False, "held": p in self.holdings, "buyers": 4 + j, "buys": 9, "top_buyer_share": 0.3, "bundled_buys": 0, "fake_score": 0.0, "trades_5m": 5, "eth_5m": 0.1}
        return result

    def record(self, quotes):
        for p, q in quotes.items():
            h = self.history.setdefault(p, [])
            h.append(float((q.bid + q.ask) / 2))
            del h[:-120]

    def price_for(self, product, side, amount):
        h = self.history.get(product) or [1.7e-9]
        return D(str(h[-1])) * (D("1.02") if side == "BUY" else D("0.98"))

    def max_buy_eth(self, product):
        return None

    def choose(self, tick, quotes, positions, taste):
        held = [p for p, v in positions.items() if v > 0 and p in quotes]
        if held and tick % 2:
            return held[0]
        return list(quotes)[tick % len(quotes)]
