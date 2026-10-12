"""PeakWhale Berth: a free EV fleet charging planner."""

__version__ = "0.1.0"

from berth.models import Fleet  # noqa: E402
from berth.planner import plan  # noqa: E402

__all__ = ["Fleet", "plan", "__version__"]
