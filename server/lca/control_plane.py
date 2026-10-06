"""Boundary for future cloud entitlement/configuration calls.

The initial implementation is local and never receives raw chat content.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class EntitlementDecision:
    allowed: bool
    reason: str = ""


class ControlPlane(Protocol):
    def authorize_stream(self, device_id: str, platform: str) -> EntitlementDecision:
        """Authorize a feature without receiving channel or chat data."""


class LocalControlPlane:
    """Free-beta control plane used while cloud entitlements are unavailable."""

    def authorize_stream(self, device_id: str, platform: str) -> EntitlementDecision:
        del device_id, platform
        return EntitlementDecision(allowed=True)
