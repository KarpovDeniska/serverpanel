"""Storage provider registry — parallel to server provider registry."""

from __future__ import annotations

from typing import Any

_registry: dict[str, type] = {}


def register_storage(name: str, cls: type) -> None:
    _registry[name] = cls


def get_storage_class(name: str) -> type:
    if name not in _registry:
        available = ", ".join(_registry.keys()) or "(none)"
        raise KeyError(f"Unknown storage provider: {name}. Available: {available}")
    return _registry[name]


def list_storage_types() -> list[dict[str, str]]:
    result = []
    for name, cls in _registry.items():
        result.append({
            "type": name,
            "display_name": getattr(cls, "DISPLAY_NAME", name),
            "description": getattr(cls, "DESCRIPTION", ""),
        })
    return result


def create_storage(name: str, credentials: dict) -> Any:
    cls = get_storage_class(name)
    return cls(**credentials)
