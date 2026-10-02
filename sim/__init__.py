"""Offline training simulator for The Bazaar (stdlib only). See sim/README.md."""
from .world import FakeBazaar, FakeBroker  # noqa: F401
from .rivals import RIVALS, make_rival  # noqa: F401
