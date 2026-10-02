# Dealer intel: Abuela Carmen and El Chato

Source: `dealers_intel.json` (raw dump: dealer sheets, levels, all 17 threads with full messages and offers), captured at tick ~110.
**Observed** = literally in the data. **Inferred** = my reading, with the sample size; treat n < 5 as a hypothesis.

## Abuela Carmen (`abuela`, level 1, open to everyone)

### Observed: the sheet
- Traits: patience 0.85, generosity 0.8, shrewdness 0.2, memory 0.15, strictness 0.1, chattiness 0.75.
- Sells: `sobre_barrio` (list 26, opening ask 30, 3 per team per hour), commons (list 10), uncommons (list 25).
- Buys: commons and uncommons of released sets. At most 8 deals per team per hour.
- Bio: forty years at the same table; "likes kindness"; she gave a present once (a *Cartel de Conciertos*) in thread 22.

### Observed: what happened (14 threads with her)
| thread | topic | outcome | sequence (us / her; F = final) |
|---|---|---|---|
| 2 | pack | deal | us 15, 16 -> her 17 ("a kind face", first deal, beginner pack) |
| 22 | card LAV-06 | deal at 21 | us 11/her 29, 15/25, 17/23, 20/22, **21 accepted** |
| 30 | card LAV-07 | deal at 22 | 11/29, 15/26, 18/24, 19, 21/24, 22/23 |
| 33 | pack | deal at 24 | 11/30, 15/27, 18/26, 20/25, 22/**24F** |
| 39 | pack | deal at 21 | 11/30, 15/25, 17/24, 19/23, 20/22, 21 |
| 52 | card LAV-03 | deal | 4/12, 6 |
| 109 | pack | **walked, `no_progress`** | the old agent repeated 27 (3x) and 2 (2x) before moving |
| 137 | pack | deal | 16/30, 18/25, 20/23, 22 |
| 148 | pack | **`cooloff`** | the old agent said 27 twenty times against her 30 |
| 50, 55, 58, 61 | pack | closed (3 of them `persona_quota`) | we opened and hit the hourly quota |
| 181 | pack | open | us 25 (inherited), her 30 |

### Observed: her pattern
- Opens at 29-30 for a pack (list 26) and 29 for a card with list 25.
- Concedes every round, by shrinking amounts: for example 30 -> 27 -> 26 -> 25 -> 24 (thread 33), 29 -> 25 -> 23 -> 22 (thread 22).
- She **accepted our offer whenever it was within 1 prima of her current ask** (threads 22, 30, 39, 137: 21 vs 22, 22 vs 23, 21 vs 22, 22 vs 23).
- She announced one final (24, thread 33), at the moment our offer was 22.
- Lowest price we ever got: 17 for a pack (thread 2, our very first deal). Every later pack deal closed between 21 and 24.

### Observed: the mistakes that cost us
- Repeating the same price: threads 109 and 148. Thread 148 ended in **cooloff**, thread 109 in `no_progress`. Her sheet says memory 0.15 and strictness 0.1, and she still punished repeated words with no new price.
- Opening 4+ conversations in the same hour: `persona_quota` closed 3 of them.

### Inferred
- (n=4) Her effective floor for a pack is about 21-22; she never went below 22 in an ask. We paid 21 twice.
- (n=4) Starting around 11 and moving +3..+4 early, then +1..+2, closes in 5-6 rounds. Fewer rounds are possible only by starting higher.
- (n=1, hypothesis) The 17 on the first deal looks like a beginner discount; it never came back.
- Her accept margin of ~1 prima is the most useful fact: **bid exactly her ask minus 1 and she takes it**, no need to wait for her next concession.

## El Chato (`chato`, level 2, unlocked for us; open to all at +2.63 h)

### Observed: the sheet
- Traits: patience 0.35, generosity 0.25, **shrewdness 0.85, memory 0.9, strictness 0.85**, chattiness 0.3.
- Bio: knows the price of every card in Madrid and "has a long memory for people who try to be clever".
- Sells: `sobre_plata` (list 150, opening ask 188, 2 per team per hour), uncommons (list 26), rares (list 77).
- Buys: uncommons and rares of released sets. At most 6 deals per team per hour.
- Unlock: 3 negotiated deals with Abuela (we have 11 deals; unlocked early at +1.63 h).

### Observed: what happened (2 threads, both selling MAL-07)
- Thread 194: we asked 26 (his list), he bid **13**, we went 21, he stayed at **13**, we went 20 (our floor), he stayed at **13**. We walked: `closed`.
- Thread 206: the agent re-opened the same card at 26 right after walking. We closed it before he answered. This was a mistake (fixed: 30-tick pause, 90 ticks per card).

### Inferred (n=1 thread, hypotheses only)
- His first bid for an uncommon is about 50% of his list price (13 of 26).
- He did not move across 2 concessions of ours (26 -> 21 -> 20). He may not concede to small steps, or his limit is 13.
- Selling him a card only makes sense if our value of it is low **and** his limit exceeds our floor. MAL-07 is worth 12.5 to us, so 13 barely covered it and gave no gain.
- Do not retry the same card soon; do not repeat any price. He is the dealer most likely to enter `cooloff`.

## What to do next (suggestions, not facts)
1. Abuela: bid her ask minus 1 once the gap is small; never repeat a price; at most 3 conversations per hour.
2. Chato: one honest negotiation at a time, short messages, different card each time; do not expect a concession for small steps.
3. Ladder: the three best deals per level count and a missing one counts as zero. We have level-1 deals and **no level-2 deal**.
4. Keep `dealers_intel.json` refreshed; `memory.json` (written by `smart_agent.py`) holds the structured rounds the agent learns from.
