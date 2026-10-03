"""Shared accept gate (hotfix 1.4): ONE accept per tick for the whole team, across processes on this machine.

The team has a single accept per tick (clock["limits"]["accepts_per_team_per_tick"] = 1 today) but smart_agent.py and
page_hunter.py are separate processes that share one key and each only counted accepts inside its own process. Before
sending an accept, a process calls `try_reserve(tick)`: the first caller for that tick wins, everyone else gets False and must
not send. The reservation is a file created with O_EXCL (atomic on every OS), one per tick, in a directory shared by the
processes (default `.accept_gate/` at the repo root, override with the ACCEPT_GATE_DIR environment variable).

    gate = AcceptGate(path)          # path: directory shared by every process of the team
    gate.reserve(tick) -> bool       # True: this process may send ONE accept in `tick`

Limits, on purpose:
  * It only coordinates processes that see the same directory (same machine). Agents running on another machine with the same
    team key are not covered; the server still enforces one accept per tick, so the failure mode is a refused accept.
  * A reservation is spent even if the accept then fails (network, refusal): that tick's accept is lost, never doubled.
  * If the gate directory cannot be used (read-only disk, ...), `try_reserve` FAILS OPEN and says so on stderr: the server
    remains the final authority and the behaviour degrades to what it was before this gate existed.
  * Duel accepts are not gated: they are time-critical and rare; if the market took the slot the duel retries next tick.
"""
import os
import sys

_KEEP_TICKS = 200          # reservations older than this many ticks are deleted (best effort)
_DEFAULT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".accept_gate")


class AcceptGate:
    def __init__(self, path):
        self.dir = path

    def reserve(self, tick):
        tick = int(tick)
        os.makedirs(self.dir, exist_ok=True)
        try:
            fd = os.open(os.path.join(self.dir, f"tick_{tick}"), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        os.close(fd)
        self._prune(tick)
        return True

    def _prune(self, tick):
        try:
            for name in os.listdir(self.dir):
                if name.startswith("tick_") and name[5:].isdigit() and int(name[5:]) < tick - _KEEP_TICKS:
                    try:
                        os.remove(os.path.join(self.dir, name))
                    except OSError:
                        pass
        except OSError:
            pass


def try_reserve(tick):
    """Process-level helper used by the production scripts. Returns True if this process may accept in `tick`."""
    try:
        return AcceptGate(os.environ.get("ACCEPT_GATE_DIR") or _DEFAULT_DIR).reserve(tick)
    except (OSError, ValueError, TypeError) as e:
        print(f"accept gate unavailable ({type(e).__name__}: {e}); not blocking the accept", file=sys.stderr, flush=True)
        return True
