# sim: offline Bazaar simulator

Stdlib only. Run from the repo root: `python -m sim.run --scenario all --seeds 3`.

## Files
- `sim/world.py`: `FakeBazaar` (duck-types `bazaar_sdk.Bazaar`), `FakeBroker` (duck-types `Broker`), `SimDone`.
  Raises the real `bazaar_sdk.BazaarError` (`wait_for_tick`, `insufficient_cash`, `persona_quota`, `cooloff`, `locked`,
  `thread_exists`, `missing_days`, `not_found`, `invalid`, `offer_closed`, ...).
- `sim/rivals.py`: `Aggressor, Softie, Trickster, Patient, Erratic`; `RIVALS`, `make_rival(name, role, limit, rng, ...)`.
- `sim/run.py`: CLI, baselines, `run_scenario`, `run_suite`.

## Plug in an agent
An agent is `step(env, ctx)` called once per tick; the runner advances the world afterwards.
```python
from sim.run import run_scenario
def my_step(env, ctx):       # env: FakeBazaar (dealer, duel) or FakeBroker (broker)
    ...                      # ctx["scenario"], ctx["task"], ctx["mem"] (your scratch dict)
run_scenario(my_step, "dealer", rival="abuela", seed=0)   # -> {"score", "deal", "errors", "crash", ...}
run_scenario(my_step, "duel", rival="patient", seed=1, issues=("price", "days"))
run_scenario(my_step, "broker", rival="all", seed=2)
```
CLI: `python -m sim.run --scenario dealer --rival chato --seeds 20 --agent mypkg.mod:my_step [--days] [--no-baselines]`.
Rival names: dealer = `abuela chato aggressor softie trickster patient erratic`; duel/broker = the five rivals
(`broker` also `all` = random mix of styles).

Scenario tasks and score:
- dealer: `ctx["task"] = {dealer, topic, list, budget}`; buy one pack (Abuela: `sobre_barrio`, list 26, opens 30; Chato:
  `sobre_plata`, list 150, opens 188; a rival name drives the dealer id `abuela`). Score = (opening ask - price) / (opening
  ask - hidden floor), clamped 0..1, best deal of the episode, 0 if none. 60 ticks max.
- duel: two episodes per seed (you as seller, then buyer); `ctx["task"] = {role, issues}`; 16 ticks. Score = your surplus
  after the per-round shrink `(1-decay)^rounds` / the initial pie (can be negative outside your limit; no deal = 0),
  averaged over both roles.
- broker: 3 synthetic runs x (8 sellers + 8 buyers), 30 ticks. Score = realised gains between true limits / best possible.

Using the world directly (e.g. to run a real agent loop): `b = FakeBazaar(seed=1)`; `b.wait_tick()` advances one tick
instantly (no sleep) and raises `SimDone` (a `BaseException`) after `max_ticks`; `b.advance(n)`; `b.net_worth()`;
`b.deals` is the ground-truth dealer ledger (price, open, floor); `FakeBazaar(auto_wait=True)` mimics the SDK's default
of waiting out `wait_for_tick`. `b.broker(key)` returns a `FakeBroker` that advances with the world.
Constructor knobs: `seed, tick_seconds=30, team, dealer_style, chato_unlocked, beginner_discount, auto_wait, max_ticks,
duel={role, rival, issues, ticks, decay}, limits, cash, market`.

## Shapes (inferred; check against the real server)
- `me()`: `{id, name, cash, level, unlocked, assets[{id, kind: card|pack, ref, name, rarity, set, serial, print_run,
  your_value, locked}], affinity{set: mult}, open_threads[ids], album, score{score, rank}}`. Score = net worth change
  (cash + private asset values), not the real formula.
- `catalog()`: `{currency_symbol, sets[{id, name, released, cards[{id, name, rarity, book, print_run, minted}]}],
  packs[{id, name, price, odds, expected_book}]}`. Books: common 10, uncommon 25, rare 77, epic 200, legendary 500.
- `thread(id)`: `{id, with, topic, status: open|deal|walked|closed|cooloff, closed_reason, until_tick, messages[{id, tick,
  sender, text, offer}], standing_offers[offer]}`; offer: `{id, maker, to, thread, status, give{cash, assets, types},
  want{...}, final, expires_tick}` (dealer asks you pay: `want.cash`, `give.types=["pack:sobre_barrio"]`).
- `dealers()`: `{"dealers": [..]}` (list; real wrapper unknown). `levels()`, `schedule()`, `feed()`, `leaderboard()` are stubs.
- `duels()`: `{"duels": [{id, status, role, your_limit, rival, rival_offer{price, days, text, final}, your_offer, deadline
  (absolute tick), duel_ticks, decay, issues, round, your_days_weight (days sessions only)}]}`.
- Broker `book()`: `{offers: [], bench_offers[{id "b<run>-<n>", give, want, ...}], settlements, fee_bps: 0, fee_per_card: 0}`;
  sellers have `want.cash` = ask and `give.assets`; buyers `give.cash` = bid. `match(sell, buy, price)` needs the same run
  and `ask <= price <= bid`.

## Assumptions
- Timing: a dealer reacts at the next tick advance (its opening ask appears one tick after `open_thread`). `accept` settles
  at the next tick. Limits: 1 accept/tick (shared with duel accepts), 1 message per thread/duel per tick, 12 listings/tick,
  6 open threads, 30 open offers, one open thread per dealer (`thread_exists`).
- Abuela: floor 80-86 % of list (21-22 for a pack), opens at 30, patience 5-7 conceding rounds, concession ~0.9 x our last
  raise (scaled down near the floor, +1 for polite words half the time), accepts a bid within 1 P of her ask, announces
  `final` when patient or at the floor (then walks if refused), 3 non-increasing bids -> `walked/no_progress`, 3 with the
  same text -> `cooloff` (30 ticks). Quota: 3 pack threads opened per game hour (4th is `closed/persona_quota`), 8 deals/hour.
  `beginner_discount=True` makes the first deal 16-17.
- El Chato: needs 3 negotiated Abuela deals (or `chato_unlocked=True`); floor 100-110 % of list (pack 150-165, opens 188),
  patience 3-4, concedes only to steps >= 10 % of the gap (35 % of the step), accepts only a bid >= his ask, 2 non-progress
  bids -> `cooloff` (60 ticks); walking away from him makes his next conversations tougher for 60 ticks.
- Dealers buying from you (`topic {"sell": {"assets": [ids]}}`): open at 55 % (Abuela) / 50 % (Chato) of list, hidden cap
  62-80 % / 50-60 %. Same haggling engine mirrored.
- Rival-driven dealers: `FakeBazaar(dealer_style=name)` makes `abuela` follow that rival (hidden floor like Abuela's, 8-round
  deadline, no quota/cooloff punishment, Trickster's words lie).
- Duels: cost 40-60, value cost+30..70; rival acts every tick on your standing offer (even when you stay silent); decay 6 %
  per round; days utility `w*(days-5)` with private weights in [-4, 4] (sign random, sign convention of the real game is
  unknown); the rival opens at tick 0. Practice flag unused.
- Market board: ~12 NPC offers at 0.5-1.5 x book, 5 % + 1 P fee; NPCs fill your sell listings at random if the ask is below
  their hidden ceiling. Enough for plumbing tests, not for strategy tuning.
- Bench: traders shade quotes 20-45 % off their hidden limit, relax by rival style (Softie fast, Patient only near the exit,
  Trickster fakes a relaxation then snaps back, Erratic noisy), firm traders (style-dependent chance) never relax, each
  leaves at a random tick 4-30. Matches are immediate and free of fees.
