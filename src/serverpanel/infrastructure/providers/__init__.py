"""Provider registry — automatic discovery and instantiation."""

from __future__ import annotations

from typing import Any

_registry: dict[str, type] = {}


def register_provider(name: str, cls: type) -> None:
    """Register a provider class by name."""
    _registry[name] = cls


def get_provider_class(name: str) -> type:
    """Get a registered provider class."""
    if name not in _registry:
        available = ", ".join(_registry.keys()) or "(none)"
        raise KeyError(f"Unknown provider: {name}. Available: {available}")
    return _registry[name]


def list_provider_types() -> list[dict[str, str]]:
    """List all registered provider types with metadata."""
    result = []
    for name, cls in _registry.items():
        result.append({
            "type": name,
            "display_name": getattr(cls, "DISPLAY_NAME", name),
            "description": getattr(cls, "DESCRIPTION", ""),
        })
    return result


def create_provider(name: str, credentials: dict) -> Any:
    """Create a provider instance from credentials."""
    cls = get_provider_class(name)
    return cls(**credentials)
