# SPDX-License-Identifier: Apache-2.0
"""Meridian V1 Usage plugin factory and composition facade."""

from __future__ import annotations

from datetime import timedelta

from meridian_storage import Meridian, RuntimeState
from meridian_storage.spi import PluginManifest

from ._version import __version__
from .aggregation import AggregationRunner, AggregationSpec
from .errors import InvalidUsage
from .query import UsageQueries
from .repository import UsageRecorder, UsageRepository, UsageResources
from .retention import RetentionInput


class Usage:
    """Library entry point shared by external publisher and consumer services."""

    def __init__(
        self,
        meridian: Meridian,
        *,
        resources: UsageResources | None = None,
    ) -> None:
        if not isinstance(meridian, Meridian) or meridian.state is not RuntimeState.READY:
            raise InvalidUsage(
                "Usage requires a ready Meridian runtime",
                requirement="usage.runtime.ready",
            )
        self.repository = UsageRepository(meridian, resources)
        self.recorder = UsageRecorder(self.repository)
        self.queries: UsageQueries = self.repository.queries

    def aggregation(
        self,
        spec: AggregationSpec,
        *,
        lease_duration: timedelta = timedelta(minutes=5),
    ) -> AggregationRunner:
        return AggregationRunner(self.repository, spec, lease_duration=lease_duration)

    def retention_inputs(
        self,
        *,
        event_retention: timedelta,
        aggregate_retention: timedelta,
        correction_grace: timedelta = timedelta(0),
    ) -> tuple[RetentionInput, RetentionInput]:
        return (
            RetentionInput(
                self.repository.resources.events,
                "usage-events",
                event_retention,
                correction_grace,
            ),
            RetentionInput(
                self.repository.resources.aggregates,
                "usage-aggregates",
                aggregate_retention,
                correction_grace,
            ),
        )


class UsagePluginFactory:
    @property
    def plugin_id(self) -> str:
        return "usage"

    def manifest(self) -> PluginManifest:
        return PluginManifest(
            plugin_id=self.plugin_id,
            plugin_version=__version__,
            plugin_contract_version="1.0.0",
            core_contract="1.x",
            extensions={
                "distribution": "meridian-storage-plugin-usage",
                "catalog": "structured",
                "service": "false",
                "privateDatabase": "false",
                "nativeQuery": "false",
                "pricing": "false",
            },
        )

    def create(self, meridian: Meridian) -> Usage:
        return Usage(meridian)


__all__ = ["Usage", "UsagePluginFactory"]
