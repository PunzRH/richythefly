import hashlib
import json
import math
from dataclasses import asdict, dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal


def D(value):
    if isinstance(value, bool):
        raise ValueError("Boolean is not money")
    x = Decimal(str(value))
    if not x.is_finite():
        raise ValueError("Nonfinite quantity")
    return x


def down(value, step):
    return (D(value) / D(step)).to_integral_value(rounding=ROUND_DOWN) * D(step)


def up(value, step):
    return (D(value) / D(step)).to_integral_value(rounding=ROUND_UP) * D(step)


@dataclass(frozen=True)
class Settings:
    products: tuple[str, ...] = ("BTC-USDC",)
    venue: str = "coinbase"
    capital: str = "100"
    order_limit: str = "10"
    loss_stop: str = "20"
    fee_reserve: str = "0.02"
    slippage: str = "0.005"
    spread_limit: str = "0.005"
    daily_orders: int = 24
    interval_seconds: float = 60
    max_quote_age: float = 15
    neural_ms: float = 500
    neural_bin_ms: float = 10
    pulse_ms: float = 200
    pulse_current: float = 20
    reward_deadband: str = "0.01"
    decoder_threshold_hz: float = 2
    paper_fee: str = "0.006"
    learning: bool = True

    def __post_init__(self):
        if (
            not self.products
            or len(set(self.products)) != len(self.products)
            or (self.venue == "coinbase" and not set(self.products) <= set(("BTC-USDC", "ETH-USDC", "SOL-USDC")))
        ):
            raise ValueError("Only allowlisted USDC spot pairs")
        if not 0 < D(self.capital) <= 100 or not 0 < D(self.order_limit) <= min(
            D(self.capital), D(10)
        ):
            raise ValueError("Maximum capital $100; maximum order $10")
        if not 0 < D(self.loss_stop) <= D(self.capital):
            raise ValueError("Invalid loss stop")
        # Bonding-curve meme coins carry ~2% fees + price impact inside the fill, and move
        # in seconds, so the robinhood venue gets wider bounds and a faster cadence.
        rh = self.venue == "robinhood"
        max_slip, max_spread, min_interval = (D(".2"), D(".3"), 5) if rh else (D(".01"), D(".01"), 60)
        if (
            not D(0) < D(self.fee_reserve) <= D(".05")
            or not 0 <= D(self.slippage) <= max_slip
            or not 0 < D(self.spread_limit) <= max_spread
        ):
            raise ValueError("Invalid fee/spread/slippage bounds")
        if not 0 <= D(self.paper_fee) <= D(self.fee_reserve):
            raise ValueError("Invalid paper fee")
        if (
            type(self.daily_orders) is not int
            or not 1 <= self.daily_orders <= (100000 if rh else 100)
            or not math.isfinite(self.interval_seconds)
            or self.interval_seconds < min_interval
        ):
            raise ValueError("Rate limit: >=60 s between orders, <=100 orders/day")
        if D(self.reward_deadband) <= 0:
            raise ValueError("Positive reinforcement deadband required")
        for x in [
            self.max_quote_age,
            self.neural_ms,
            self.neural_bin_ms,
            self.pulse_ms,
            self.pulse_current,
            self.decoder_threshold_hz,
        ]:
            if not math.isfinite(x) or x <= 0:
                raise ValueError("Positive finite parameter required")
        if self.neural_bin_ms > 10 or self.pulse_ms > self.neural_ms:
            raise ValueError(
                "Use <=10 ms neural bins; pulse must fit a decision window"
            )
        if any(
            abs(x * 10 - round(x * 10)) > 1e-7
            for x in [self.neural_ms, self.neural_bin_ms, self.pulse_ms]
        ):
            raise ValueError("Neural intervals must be multiples of 0.1 ms")

    def signature(self):
        return hashlib.sha256(
            json.dumps(asdict(self), sort_keys=True).encode()
        ).hexdigest()
