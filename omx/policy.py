"""Pre-trade policy: the ten controls every child order passes before it can leave.

All checks run on the user's machine. A child order is sent only when every
check passes; any failure blocks it and the reasons go to the audit log.

Exposure is measured worst case: current position plus every resting order on
the same side filling at its own price, plus the new child. Positions and resting
orders on other coins count toward the account total the same way, valued at the
mark price the caller passes (ref_px_by_coin); an exposed coin without a mark blocks.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .account_mode import mode_block_reason
from .types import AccountState, ChildOrder, Decision, Intent, MarketSnapshot, Side


@dataclass
class PolicyConfig:
    allowed_coins: frozenset = frozenset({"BTC", "ETH", "SOL"})
    min_day_ntl_usd: float = 50_000_000.0      # liquidity floor: 24 h traded notional
    min_depth_usd: float = 250_000.0           # resting depth within depth_band_bps on the side we take
    depth_band_bps: float = 25.0
    price_band_bps: float = 100.0              # child px must sit within this of mid
    max_coin_notional_usd: dict = field(default_factory=dict)   # coin -> cap; missing -> default
    default_coin_notional_usd: float = 10_000.0
    max_account_notional_usd: float = 25_000.0
    max_leverage: float = 3.0                  # gross worst-case notional / equity
    max_daily_loss_usd: float = 500.0          # stop new risk once day P&L <= -this
    max_data_age_ms: int = 3_000               # book older than this blocks orders
    max_child_notional_usd: float = 5_000.0
    allow_agent_sources: bool = True
    # Hyperliquid's own TWAP: the protocol sends the suborders, so OMX checks the whole order once (exposure,
    # leverage, loss, allowlist, an estimated suborder cap) but cannot hold each suborder to price_band_bps;
    # every suborder may slip up to 3 %. Off unless the user turns it on.
    allow_native_twap: bool = False

    def coin_cap(self, coin: str) -> float:
        return self.max_coin_notional_usd.get(coin, self.default_coin_notional_usd)


def _worst_case_coin_notional(acct: AccountState, coin: str, extra: Optional[ChildOrder], ref_px: float) -> float:
    """Largest absolute position the coin can reach if all same-side resting orders fill."""
    pos = acct.position(coin)
    buys = sum(o.size for o in acct.open_orders if o.coin == coin and o.side is Side.BUY and not o.reduce_only)
    sells = sum(o.size for o in acct.open_orders if o.coin == coin and o.side is Side.SELL and not o.reduce_only)
    if extra is not None and extra.coin == coin and not extra.reduce_only:
        if extra.side is Side.BUY:
            buys += extra.size
        else:
            sells += extra.size
    return max(abs(pos + buys), abs(pos - sells)) * ref_px


def evaluate(child: ChildOrder, intent: Intent, snap: MarketSnapshot, acct: AccountState,
             cfg: PolicyConfig, now_ms: int, killed: bool, ref_px_by_coin: Optional[dict] = None) -> Decision:
    reasons: list[str] = []
    checks: dict = {}
    mid = snap.mid

    # 9 kill switch
    checks["kill_switch"] = not killed
    if killed:
        reasons.append("KILL_SWITCH_ACTIVE")

    # account mode: a venue that reports one must report a supported mode (omx.account_mode);
    # an unsupported or unreadable mode blocks every child, reduce-only included (fail closed)
    if acct.account_mode is not None:
        checks["account_mode"] = acct.account_mode
        why = mode_block_reason(acct.account_mode)
        if why:
            reasons.append(why)

    # source gate (agents can be turned off entirely)
    if intent.source.startswith("agent") and not cfg.allow_agent_sources:
        reasons.append("AGENT_SOURCE_DISABLED")

    # 1 allowlist
    checks["allowlist"] = child.coin in cfg.allowed_coins
    if not checks["allowlist"]:
        reasons.append(f"COIN_NOT_ALLOWED:{child.coin}")

    # 8 data freshness
    age = now_ms - snap.ts_ms
    checks["data_age_ms"] = age
    if age > cfg.max_data_age_ms or age < -1_000:
        reasons.append(f"STALE_DATA:{age}ms")
    if mid is None:
        reasons.append("NO_TWO_SIDED_BOOK")

    # 2 liquidity floor
    checks["day_ntl_usd"] = snap.day_ntl_usd
    if snap.day_ntl_usd is None or snap.day_ntl_usd < cfg.min_day_ntl_usd:
        reasons.append("LIQUIDITY_BELOW_FLOOR")
    depth = snap.depth_usd(child.side, cfg.depth_band_bps)
    checks["depth_usd"] = depth
    if depth < cfg.min_depth_usd and not child.reduce_only:
        reasons.append("DEPTH_BELOW_FLOOR")

    # 3 price band (vs mid) and intent limit / slippage cap
    if mid is not None:
        dev_bps = (child.px - mid) / mid * 1e4 * child.side.sign
        checks["px_vs_mid_bps"] = dev_bps
        if abs(dev_bps) > cfg.price_band_bps:
            reasons.append(f"PRICE_OUTSIDE_BAND:{dev_bps:.1f}bps")
        if dev_bps > intent.max_slippage_bps:
            reasons.append(f"SLIPPAGE_CAP:{dev_bps:.1f}>{intent.max_slippage_bps}bps")
    if intent.limit_px is not None:
        if (child.side is Side.BUY and child.px > intent.limit_px) or (child.side is Side.SELL and child.px < intent.limit_px):
            reasons.append("BEYOND_INTENT_LIMIT")

    # child size cap
    ref = mid or child.px
    child_ntl = child.size * child.px
    checks["child_ntl_usd"] = child_ntl
    if child_ntl > cfg.max_child_notional_usd and not child.reduce_only:
        reasons.append("CHILD_NOTIONAL_CAP")

    # reduce-only must actually reduce
    if child.reduce_only:
        pos = acct.position(child.coin)
        if pos == 0 or (pos > 0 and child.side is Side.BUY) or (pos < 0 and child.side is Side.SELL) or child.size > abs(pos) + 1e-12:
            reasons.append("REDUCE_ONLY_WOULD_INCREASE")

    # 4 per-coin worst case notional
    coin_wc = _worst_case_coin_notional(acct, child.coin, child, ref)
    checks["coin_worst_case_usd"] = coin_wc
    if coin_wc > cfg.coin_cap(child.coin) and not child.reduce_only:
        reasons.append("COIN_NOTIONAL_CAP")

    # 5 account total worst case (all coins, resting orders included)
    refs = dict(ref_px_by_coin or {})
    refs[child.coin] = ref
    coins = {p.coin for p in acct.positions} | {o.coin for o in acct.open_orders} | {child.coin}
    total = 0.0
    no_mark = []
    for c in coins:
        px = refs.get(c)
        if px is None:
            # no mark for an exposed coin: value it at the highest price we know of and block
            # new risk, since entry / order prices can understate a coin that has moved
            no_mark.append(c)
            px = max([p.entry_px for p in acct.positions if p.coin == c] +
                     [o.px for o in acct.open_orders if o.coin == c] + [0.0])
        total += _worst_case_coin_notional(acct, c, child if c == child.coin else None, px)
    checks["coins_without_mark"] = sorted(no_mark)
    if no_mark and not child.reduce_only:
        reasons.extend(f"NO_MARK:{c}" for c in sorted(no_mark))
    checks["account_worst_case_usd"] = total
    if total > cfg.max_account_notional_usd and not child.reduce_only:
        reasons.append("ACCOUNT_NOTIONAL_CAP")

    # 6 leverage on worst case
    lev = total / acct.equity_usd if acct.equity_usd > 0 else float("inf")
    checks["worst_case_leverage"] = lev
    if lev > cfg.max_leverage and not child.reduce_only:
        reasons.append(f"LEVERAGE_CAP:{lev:.2f}")

    # 7 daily loss limit (deposits/withdrawals excluded from P&L)
    dpl = acct.day_pnl_usd()
    checks["day_pnl_usd"] = dpl
    if dpl <= -cfg.max_daily_loss_usd and not child.reduce_only:
        reasons.append("DAILY_LOSS_LIMIT")

    return Decision(allow=not reasons, reasons=tuple(reasons), checks=checks)
