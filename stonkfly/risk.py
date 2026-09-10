"""Execution limits can veto a neural proposal, never substitute a strategy."""

import time

from .config import D, down, up


class Veto(Exception):
    pass


class Guard:
    def __init__(self, settings, ledger, stop_file):
        self.s = settings
        self.l = ledger
        self.stop_file = stop_file

    def check(self, quotes, now):
        if self.stop_file.exists():
            raise Veto("STOP file present")
        if self.l.get("halted"):
            raise Veto(self.l.get("halted"))
        if self.l.pending():
            raise Veto("Order outcome unresolved")
        if self.s.venue == "robinhood":
            # The fly's universe is whatever is live on the curves; every held coin must be marked.
            held = {p for p, v in self.l.positions.items() if v > 0}
            if not quotes or not held <= set(quotes):
                raise Veto("Incomplete market snapshot")
        elif set(quotes) != set(self.s.products):
            raise Veto("Incomplete market snapshot")
        for product, q in quotes.items():
            if q.product != product:
                raise Veto("Quote identity mismatch")
            if not -0.5 <= now - q.timestamp <= self.s.max_quote_age:
                raise Veto("Stale or future quote")
            if (q.ask - q.bid) / q.bid > D(self.s.spread_limit):
                raise Veto("Spread limit")
        if self.l.equity(quotes) <= D(self.l.get("initial_cash")) - D(self.s.loss_stop):
            self.l.halt("Loss stop reached; holdings remain exposed")
            raise Veto("Loss stop reached")

    def plan(self, product, side, quotes, now=None, budget=None, sell_fraction=None):
        """budget (quote units) / sell_fraction (0..1) let the fly size its own order; defaults are the fixed limits."""
        now = time.time() if now is None else now
        self.check(quotes, now)
        universe = quotes if self.s.venue == "robinhood" else self.s.products
        if product not in universe or side not in ("BUY", "SELL"):
            raise Veto("Invalid neural proposal")
        if now - self.l.get("last_attempt") < self.s.interval_seconds:
            raise Veto("Order cooldown")
        if self.l.attempts_today(now) >= self.s.daily_orders:
            raise Veto("Daily order limit")
        q = quotes[product]
        reserve = D(self.s.fee_reserve)
        if side == "BUY":
            limit = up(q.ask * (1 + D(self.s.slippage)), q.price_increment)
            cap = D(self.s.order_limit) if budget is None else min(D(budget), D(self.s.order_limit))
            budget = min(cap, self.l.cash) / (1 + reserve)
            size = down(budget / limit, q.base_increment)
        else:
            limit = down(q.bid * (1 - D(self.s.slippage)), q.price_increment)
            held = self.l.positions.get(product, D(0))
            want = held if sell_fraction is None else held * D(str(sell_fraction))
            size = down(min(want, D(self.s.order_limit) / q.ask), q.base_increment)
        if limit <= 0 or size < q.minimum_base or size * limit < q.minimum_quote:
            raise Veto("Insufficient funds/position or below exchange minimum")
        return {
            "product": product,
            "side": side,
            "base_size": str(size),
            "limit_price": str(limit),
            "fee_ceiling": str(max(size * limit * reserve, D("0.0005")) if self.s.venue == "robinhood" else size * limit * reserve),  # on-chain: gas floor so dust orders never trip the ceiling
            "observed_bid": str(q.bid),
            "observed_ask": str(q.ask),
            "quote_timestamp": q.timestamp,
            "settings": self.s.signature(),
            "order_type": "limit_limit_fok",
        }

    def before_submit(self, plan):
        # Called after exchange preview and balance checks, at the final send boundary.
        if self.stop_file.exists() or self.l.get("halted"):
            raise Veto("Execution stopped")
        if not -0.5 <= time.time() - plan["quote_timestamp"] <= self.s.max_quote_age:
            raise Veto("Quote expired before submission")
        pending = self.l.pending()
        if (
            len(pending) != 1
            or pending[0]["id"] != plan["client_order_id"]
            or pending[0]["status"] != "PREPARED"
        ):
            raise Veto("Intent ownership mismatch")
