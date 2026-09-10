"""Taste memory: the fly learns which kinds of coins have paid it and which have bitten it.

The connectome learns WHEN to buy/sell/hold from the chart (dopamine -> KC->MBON plasticity).
This is the second, simpler memory for WHICH coin to look at: every realized sell is booked
against the feature bucket the coin had when it was bought ("new|cold|in" etc.), and the
forager biases toward buckets with a good average return. Stored in the ledger so it
survives restarts and keeps improving across sessions.
"""

from .config import D


class Taste:
    def __init__(self, ledger):
        self.l = ledger
        self.data = ledger.get("taste") or {}
        self.basis = ledger.get("basis") or {}

    def value(self, key):
        d = self.data.get(key)
        if not d or d["n"] == 0:
            return 0.0
        mean = d["sum"] / d["n"]          # mean return fraction per trade (-1 .. +big)
        conf = min(1.0, d["n"] / 5.0)     # trust it more after a few trades
        return max(-1.5, min(1.5, mean * 3.0 * conf))

    def learn(self, key, ret):
        d = self.data.setdefault(key, {"n": 0, "sum": 0.0, "wins": 0})
        d["n"] += 1
        d["sum"] += float(ret)
        d["wins"] += 1 if ret > 0 else 0
        self.l.put("taste", self.data)

    def book(self, product, key, d_tokens, d_cash, cash_before_trade):
        """Update cost basis after a fill. Returns realized pnl in ETH on sells, else None.

        d_tokens: change in tokens held (+buy / -sell); d_cash: change in cash (fees included).
        """
        b = self.basis.setdefault(product, {"tokens": "0", "eth": "0", "key": key})
        tok, eth = D(b["tokens"]), D(b["eth"])
        pnl = None
        if d_tokens > 0:
            tok += D(d_tokens)
            eth += -D(d_cash)
            if b.get("key") is None:
                b["key"] = key
        elif d_tokens < 0 and tok > 0:
            sold = min(-D(d_tokens), tok)
            avg = eth / tok
            cost = sold * avg
            pnl = D(d_cash) - cost
            tok -= sold
            eth -= cost
            spent = cost if cost > 0 else D(cash_before_trade)
            self.learn(b.get("key") or key, float(pnl / spent) if spent > 0 else 0.0)
            if pnl < 0:
                self.burn(product.split("-", 1)[1], float(pnl))
        b["tokens"], b["eth"] = str(tok), str(eth)
        if tok <= 0:
            self.basis.pop(product, None)
        self.l.put("basis", self.basis)
        return pnl

    def burn(self, coin, pnl):
        """A coin that cost him money is off the menu for BURN_HOURS (still sellable)."""
        import time
        data = self.l.get("burned") or {}
        data[coin.lower()] = {"t": time.time(), "pnl": pnl}
        self.l.put("burned", data)

    def burned(self, hours=6):
        import time
        data = self.l.get("burned") or {}
        cutoff = time.time() - hours * 3600
        return {c for c, v in data.items() if v.get("t", 0) > cutoff}

    def summary(self):
        rows = sorted(self.data.items(), key=lambda kv: kv[1]["sum"] / max(kv[1]["n"], 1), reverse=True)
        return [{"smell": k, "trades": v["n"], "wins": v["wins"], "avg_return": round(v["sum"] / max(v["n"], 1), 4)} for k, v in rows]
