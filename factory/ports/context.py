from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from factory.metrics import Metrics
from factory.redaction import Redactor

if TYPE_CHECKING:
    from factory.config.schema import FactoryConfig
    from factory.config.secrets import SecretResolver
    from factory.ports.clock import Clock, IdSource


@dataclass
class AdapterContext:
    """Handed to every adapter's `from_settings(settings, ctx)`. Adapters must
    not perform I/O in from_settings; connecting happens on first use."""

    config: FactoryConfig
    secrets: SecretResolver
    redactor: Redactor
    base_dir: Path
    metrics: Metrics = field(default_factory=Metrics)
    clock: Clock | None = None
    ids: IdSource | None = None
    port: Callable[[str], object] | None = None  # lazy lookup of other ports, for adapters that compose them
