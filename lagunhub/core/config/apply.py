"""Carrying the configuration over into the model.

The configuration describes **decisions**, the model describes the **installation**.
They meet here and only here — which is why the model need know nothing about the file
format.
"""

from __future__ import annotations

from lagunhub.core.config.model import BindingConfig, Config, SourceRef, TargetRef
from lagunhub.core.model import BindingSpec, Installation, Source, Uid


def apply(installation: Installation, config: Config) -> None:
    """Load the configuration into the model. Only CORE calls this."""
    installation.wanted = {entry.uid: entry for entry in config.nodes}
    installation.blocked = frozenset(entry.uid for entry in config.nodes if entry.blocked)
    installation.bindings = [spec for binding in config.bindings for spec in _specs(binding)]
    for uid, node in installation.nodes.items():
        wanted = config.node(uid)
        if wanted is not None and wanted.name:
            node.name = wanted.name


def _specs(binding: BindingConfig) -> list[BindingSpec]:
    source: Source = (
        Uid(binding.source.uid),
        binding.source.endpoint,
        binding.source.element,
    )
    return [_spec(source, binding.source, target) for target in binding.targets]


def _spec(source: tuple[object, int, int], origin: SourceRef, target: TargetRef) -> BindingSpec:
    return BindingSpec(
        source=source,  # type: ignore[arg-type]
        source_label=origin.label,
        target_uid=target.uid,
        group=target.group,
        target_endpoint=target.endpoint,
        target_element=target.element,
        args=target.args,
        kind=target.kind,
        on_report=origin.on_report,
        label=target.label,
    )


__all__ = ["apply"]
