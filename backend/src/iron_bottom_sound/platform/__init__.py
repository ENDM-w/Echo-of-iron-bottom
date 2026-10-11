"""Online match orchestration around the authoritative IBS rules engine.

The platform package deliberately does not duplicate movement or combat rules.
It owns identity, ship-level seats, private orders, and referee approval.
"""

from .service import PlatformService

__all__ = ["PlatformService"]
