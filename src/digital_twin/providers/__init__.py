"""State providers (effectful): fetch raw vendor state for a scope."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import (
    FetchError,
    FetchFailure,
    NacFetch,
    OrgScope,
    OrgTemplateContext,
    OrgWlanContext,
    OrgWlanTemplateContext,
    RawSiteState,
    SiteScope,
    StateMeta,
    StateProvider,
)

if TYPE_CHECKING:
    from .mist_api import MistApiProvider


def __getattr__(name: str) -> type[MistApiProvider]:
    # Importing pure provider contracts or replay capture must not load the
    # SDK. Preserve the public class export when callers explicitly request it.
    if name != "MistApiProvider":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from .mist_api import MistApiProvider

    return MistApiProvider


__all__ = [
    "FetchError",
    "FetchFailure",
    "NacFetch",
    "OrgScope",
    "OrgTemplateContext",
    "OrgWlanContext",
    "OrgWlanTemplateContext",
    "RawSiteState",
    "SiteScope",
    "StateMeta",
    "StateProvider",
    "MistApiProvider",
]
