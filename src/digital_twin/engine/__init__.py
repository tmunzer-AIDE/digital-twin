"""Engine: orchestration-only modules (no business logic)."""

from .capability_check import CapabilityGapError, validate_supply

__all__ = ["CapabilityGapError", "validate_supply"]
