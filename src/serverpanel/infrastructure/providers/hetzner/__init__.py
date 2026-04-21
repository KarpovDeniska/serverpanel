"""Hetzner Dedicated provider — auto-registers on import."""

from serverpanel.infrastructure.providers import register_provider
from serverpanel.infrastructure.providers.hetzner.provider import HetznerDedicatedProvider

register_provider("hetzner_dedicated", HetznerDedicatedProvider)
