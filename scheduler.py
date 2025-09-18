# /home/socialapp/social-executor/scheduler.py
"""
Channel scheduler for twice-daily posting.

Rules (default):
- Odd day-of-year:   AM = instagram, PM = facebook
- Even day-of-year:  AM = facebook,  PM = instagram

You can change which channel starts on odd days via env:
  SCHEDULER_ODD_AM = "instagram"  (default)  or "facebook"

Timezone:
- Uses TZ env if set (e.g., "America/New_York"), otherwise defaults to that.
"""

from __future__ import annotations
import os
from datetime import datetime, date
try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except Exception:  # pragma: no cover
    ZoneInfo = None  # Fallback handled below


# ---------- Configuration ----------
DEFAULT_TZ = os.getenv("TZ", "America/New_York")
ODD_AM = os.getenv("SCHEDULER_ODD_AM", "instagram").strip().lower()
if ODD_AM not in ("instagram", "facebook"):
    ODD_AM = "instagram"  # safety

CHANNELS = ("instagram", "facebook")


# ---------- Time helpers ----------
def _now() -> datetime:
    """Current local time in the configured timezone."""
    if ZoneInfo:
        try:
            return datetime.now(ZoneInfo(DEFAULT_TZ))
        except Exception:
            pass
    # Fallback to naive local time if zone not available
    return datetime.now()


def _other(channel: str) -> str:
    """Return the opposite channel."""
    channel = (channel or "").lower()
    return "facebook" if channel == "instagram" else "instagram"


# ---------- Public API ----------
def channels_for_day(d: date | None = None) -> tuple[str, str]:
    """
    Return (am_channel, pm_channel) for the given date (or today if None),
    applying the odd/even day-of-year rule with the configured anchor.
    """
    if d is None:
        d = _now().date()
    doy = d.timetuple().tm_yday
    if doy % 2 == 1:  # odd
        am = ODD_AM
        pm = _other(ODD_AM)
    else:             # even
        am = _other(ODD_AM)
        pm = ODD_AM
    return am, pm


def slot_channel_for_today(slot: str) -> str:
    """
    Return the channel to use for today's AM or PM slot.
    Expected slot values: "am" or "pm" (case-insensitive).
    """
    slot = (slot or "am").lower()
    am, pm = channels_for_day()
    return am if slot == "am" else pm


# ---------- CLI / debug ----------
if __name__ == "__main__":
    now = _now()
    am, pm = channels_for_day(now.date())
    print(f"[scheduler] TZ={DEFAULT_TZ}  ODD_AM={ODD_AM}")
    print(f"[scheduler] Today {now.date()}  AM={am}  PM={pm}")

