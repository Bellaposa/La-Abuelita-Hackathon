"""Training rival personalities.

One common interface, three uses:
  * duel opponent        -> world.FakeBazaar duel engine calls rival.respond(t, price, days) every tick
  * dealer counterparty  -> world.RivalDealer wraps a rival as the "abuela" dealer (seller) or buyer
  * market trader        -> world.BenchMarket asks rival.quote(t, leave_t, firm) for a shaded quote

All randomness comes from the rng passed in, so runs are deterministic per seed.
Prices are integers >= 1. role is the rival's own role: "seller" (wants high) or "buyer" (wants low).
"""
from __future__ import annotations

import random

__all__ = ["Rival", "Aggressor", "Softie", "Trickster", "Patient", "Erratic", "RIVALS", "make_rival"]


class Rival:
    name = "rival"
    open_mult = 1.5     # opening distance from the reference price
    beta = 1.0          # concession exponent: >1 Boulware (slow), <1 conceder (fast)
    margin = 0.05       # profit share the rival keeps over its own limit at the deadline
    shade0 = 0.3        # market trader: initial quote shading as a fraction of the limit
    firm_prob = 0.25    # market trader: chance it never relaxes

    def __init__(self, role: str, limit: float, rng: random.Random | None = None, ticks: int = 16,
                 ref: float | None = None, w_days: float = 0.0, use_days: bool = False, seed: int = 0):
        assert role in ("seller", "buyer")
        self.role, self.limit, self.T = role, float(limit), max(2, int(ticks))
        self.side = 1 if role == "seller" else -1
        self.rng = rng or random.Random(seed)
        self.seed = seed
        self.ref = float(ref) if ref else (self.limit * 1.5 if self.side > 0 else self.limit * 0.6)
        self.w, self.use_days = float(w_days), bool(use_days)
        self.pref_days = (10 if self.w > 0 else 0) if use_days else None
        self.last_price: int | None = None
        self.last_days: int | None = None
        self.history: list[dict] = []
        self._setup()

    # ---- hooks ---------------------------------------------------------------------------------------------
    def _setup(self) -> None:
        pass

    def curve(self, frac: float) -> float:
        """Target price at fraction of the deadline (0..1)."""
        return self.p0 + (self.reserve - self.p0) * min(1.0, max(0.0, frac)) ** self.beta

    def _text(self, price: int, final: bool, t: int) -> tuple[str, bool]:
        """(words, final flag in the structure). Honest by default."""
        w = "Final offer" if final else "My offer"
        return f"{w}: {price}.", final

    def _post(self, t: int, price: int) -> tuple[int, bool]:
        """Last chance to alter price (Erratic); returns (price, forced_final_flag)."""
        return price, False

    # ---- derived -------------------------------------------------------------------------------------------
    @property
    def p0(self) -> float:
        if self.side > 0:
            return max(self.limit * 1.05, self.ref * self.open_mult)
        return min(self.limit * 0.95, self.ref / self.open_mult)

    @property
    def reserve(self) -> float:
        return self.limit * (1 + self.margin) if self.side > 0 else self.limit * (1 - self.margin)

    def utility(self, price: float, days: int | None = None) -> float:
        u = self.side * (price - self.limit)
        if self.use_days and days is not None:
            u += self.w * (days - 5)
        return u

    def target_days(self, frac: float) -> int | None:
        if not self.use_days:
            return None
        return int(round(5 + (self.pref_days - 5) * (1 - min(1.0, frac) ** 1.5)))

    def _accepts(self, t: int, u_offer: float, u_target: float) -> bool:
        if u_offer < 0:
            return False
        return u_offer >= u_target - 1e-9 or (t >= self.T - 1 and u_offer >= 0)

    # ---- duel / dealer interface ---------------------------------------------------------------------------
    def respond(self, t: int, price: float | None, days: int | None = None) -> dict:
        """React at round t (0-based) to the counterparty's standing offer (or None).
        Returns {"action": "accept"|"offer", "price", "days", "text", "final"} where "final" is the flag in
        the structured offer and "text" may say something else (Trickster)."""
        frac = t / (self.T - 1)
        tp = self.curve(frac)
        td = self.target_days(frac)
        if price is not None:
            u_off = self.utility(price, days if days is not None else 5)
            u_tgt = self.utility(tp, td if td is not None else 5)
            if self._accepts(t, u_off, u_tgt):
                self.history.append({"t": t, "action": "accept"})
                return {"action": "accept", "price": None, "days": None, "text": "Deal.", "final": False}
        p = int(round(tp))
        if self.last_price is not None:  # never retract unless _post says so
            p = max(p, self.last_price) if self.side < 0 else min(p, self.last_price)
        p, forced_final = self._post(t, p)
        p = max(1, p)
        final = forced_final or t >= self.T - 1
        text, flag = self._text(p, final, t)
        self.last_price, self.last_days = p, td
        self.history.append({"t": t, "action": "offer", "price": p})
        return {"action": "offer", "price": p, "days": td, "text": text, "final": flag}

    # ---- market trader interface ---------------------------------------------------------------------------
    def shade(self, frac: float) -> float:
        """Shading fraction (>= 0) at fraction of the trader's patience elapsed. Subclasses override."""
        return self.shade0 * (1 - frac)

    def quote(self, t: int, leave_t: int, firm: bool = False) -> int:
        frac = min(1.0, t / max(1, leave_t))
        s = self.shade0 * 0.7 if firm else max(0.0, self.shade(frac))
        q = self.limit * (1 + self.side * s)
        return max(1, int(round(q)))


class Aggressor(Rival):
    """Opens extreme, concedes slowly, keeps a fat margin."""
    name = "aggressor"
    open_mult, beta, margin = 2.0, 3.0, 0.12
    shade0, firm_prob = 0.45, 0.4

    def shade(self, frac):
        return self.shade0 * (1 - 0.35 * frac)


class Softie(Rival):
    """Concedes fast, small margin."""
    name = "softie"
    open_mult, beta, margin = 1.15, 0.5, 0.02
    shade0, firm_prob = 0.2, 0.05

    def shade(self, frac):
        return self.shade0 * (1 - frac ** 0.5)


class Trickster(Rival):
    """Lies in text: says one price and structures another; fake 'final' words; fake concessions in the market."""
    name = "trickster"
    open_mult, beta, margin = 1.6, 1.4, 0.08
    shade0, firm_prob = 0.35, 0.1

    def _setup(self):
        self.fake_final_used = 0

    def _text(self, price, final, t):
        # words quote a price better for the counterparty than the structure; "final" in words only
        lie = int(round(price * (0.9 if self.side > 0 else 1.1)))
        if final:
            return f"This is my final offer: {lie}. Take it or leave it.", True
        if t % 3 == 1 and self.fake_final_used < 3:
            self.fake_final_used += 1
            return f"FINAL offer, {lie}, I cannot go lower, my last word.", False  # words say final, flag says no
        if t % 3 == 2:
            return f"Great, we agree at {lie}, just confirm.", False  # structure still says `price`
        return f"I can do {lie} for you, friend.", False

    def shade(self, frac):
        # fake relaxation, then snap back
        if frac < 0.5:
            return self.shade0 * (1 - 1.4 * frac)
        return self.shade0 * 0.95


class Patient(Rival):
    """Boulware: holds a firm line and only gives in at the deadline."""
    name = "patient"
    open_mult, beta, margin = 1.5, 7.0, 0.04
    shade0, firm_prob = 0.4, 0.15

    def _accepts(self, t, u_off, u_tgt):
        if u_off < 0:
            return False
        if t >= self.T - 2:
            return True
        return u_off >= u_tgt - 1e-9

    def shade(self, frac):
        return self.shade0 if frac < 0.8 else self.shade0 * max(0.05, 1 - (frac - 0.8) / 0.2 * 0.95)


class Erratic(Rival):
    """Random noisy concessions; sometimes retracts or repeats its last offer."""
    name = "erratic"
    open_mult, beta, margin = 1.5, 1.3, 0.06
    shade0, firm_prob = 0.3, 0.2

    def _setup(self):
        self.open_mult = self.rng.uniform(1.2, 1.9)
        self.beta = self.rng.uniform(0.7, 2.2)

    def _accepts(self, t, u_off, u_tgt):
        if u_off < 0:
            return False
        return u_off >= u_tgt - 1e-9 or self.rng.random() < 0.08 or t >= self.T - 1

    def _post(self, t, price):
        r = self.rng.random()
        span = abs(self.p0 - self.limit)
        if self.last_price is not None and r < 0.15:
            return self.last_price, False            # repeat
        if self.last_price is not None and r < 0.30:
            back = int(round(span * self.rng.uniform(0.03, 0.12)))
            return self.last_price + self.side * back, False   # retract
        noise = int(round(span * self.rng.gauss(0, 0.05)))
        p = price + noise
        lo, hi = (self.limit, 10 ** 7) if self.side > 0 else (1, self.limit)
        return int(min(hi, max(lo, p))), self.rng.random() < 0.05

    def shade(self, frac):
        r = random.Random(f"{self.seed}-{self.limit}-{int(frac * 100)}")
        return max(0.0, self.shade0 * (1 - 0.5 * frac) + r.uniform(-0.1, 0.1))


RIVALS = {c.name: c for c in (Aggressor, Softie, Trickster, Patient, Erratic)}


def make_rival(name: str, role: str, limit: float, rng: random.Random | None = None, **kw) -> Rival:
    return RIVALS[name.lower()](role, limit, rng, **kw)
