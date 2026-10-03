"""TRADING_V2 mode switch: off | shadow | on, hot-reloadable.

  off     the legacy trading (smart_agent.phase_market + page_hunter) is the only thing that trades.   [default]
  shadow  trading_v2 runs and LOGS what it would do; it performs no write against Bazaar. Legacy keeps trading.
  on      trading_v2 is the single trading authority; smart_agent.phase_market and page_hunter step aside.

The mode is read from the file `.trading_v2_mode` at the repo root (one word; re-read every tick so it can be flipped in
seconds with no restart), else from the TRADING_V2 environment variable, else `off`. Anything else is treated as `off`.
Override the file location with TRADING_V2_FILE (the tests do).
"""
import os

MODES = ("off", "shadow", "on")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def mode_file():
    return os.environ.get("TRADING_V2_FILE") or os.path.join(_ROOT, ".trading_v2_mode")


def get_mode():
    try:
        with open(mode_file(), encoding="utf-8") as f:
            word = f.read().strip().lower()
        if word:
            return word if word in MODES else "off"
    except OSError:
        pass
    word = os.environ.get("TRADING_V2", "off").strip().lower()
    return word if word in MODES else "off"


def legacy_should_trade():
    """False when trading_v2 is the single trading authority (mode `on`): legacy market code must not trade."""
    return get_mode() != "on"
