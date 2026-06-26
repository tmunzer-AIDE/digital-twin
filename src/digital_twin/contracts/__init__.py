"""Cross-cutting value types (pure) — imported by everyone, imports only ir."""

from .change_plan import ChangeOp, ChangePlan, ChangeScope
from .config_diff import FieldChange, ObjectConfigDiff
from .diagram import Diagram
from .finding import Cause, Finding, FindingCategory, FindingSource, ObjectRef, Severity
from .rejection import Rejection
from .visual_map import FindingRef, VisualEntry, VisualMap, VisualTier, entity_key

__all__ = [
    "Cause",
    "ChangeOp",
    "ChangePlan",
    "ChangeScope",
    "Diagram",
    "FieldChange",
    "Finding",
    "FindingCategory",
    "FindingRef",
    "FindingSource",
    "ObjectConfigDiff",
    "ObjectRef",
    "Severity",
    "Rejection",
    "VisualEntry",
    "VisualMap",
    "VisualTier",
    "entity_key",
]
