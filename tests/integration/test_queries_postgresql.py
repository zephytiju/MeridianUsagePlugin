# SPDX-License-Identifier: Apache-2.0
"""Public Usage queries through the default provider and released PostgreSQL."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from integration.test_decimal_postgresql import context
from meridian_storage.plugins.usage import (
    UsageAggregateV1,
    UsageOrder,
    UsageRepository,
    UsageScope,
    UsageWindow,
    event_set_fingerprint,
)


def test_default_provider_starts(default_backend):
    default_backend.start()


def test_scoped_half_open_queries(default_backend, meter, event):
    runtime = default_backend.start()
    ctx = context()
    start = event.window.start
    end = start + timedelta(minutes=3)
    with runtime.context(ctx):
        repo = UsageRepository(runtime)
        repo.register_meter(meter)
        for index, offset in enumerate(
            (timedelta(microseconds=-1), *(timedelta(minutes=n) for n in range(4)))
        ):
            window = UsageWindow(start + offset, start + offset + timedelta(minutes=1))
            item = replace(
                event,
                event_id=f"query-{index}",
                window=window,
                recorded_at=end + timedelta(minutes=5),
            )
            stored = repo.record(item).event
            aggregate = UsageAggregateV1(
                f"query-{index}",
                1,
                event.scope,
                meter.meter_id,
                meter.version,
                window,
                {},
                Decimal("2000.000000"),
                1,
                window.end,
                event_set_fingerprint((stored,)),
                end + timedelta(minutes=5),
            )
            repo.put_aggregate(aggregate)
        other = replace(event, event_id="other-scope", scope=UsageScope({"tenant": "other"}))
        repo.record(other)
        repo.put_aggregate(
            replace(aggregate, aggregate_id="other-scope", scope=other.scope, window=event.window)
        )
        # A different runtime scope can reuse logical identities without leaking.
        with runtime.context(context()):
            repo.record(event, meter=meter)
            repo.put_aggregate(
                replace(aggregate, aggregate_id="other-runtime", window=event.window)
            )
        for queries, identity in (
            (repo.queries.events, "eventId"),
            (repo.queries.aggregates, "aggregateId"),
        ):
            query = queries(
                event.scope,
                start,
                end,
                where={"meterId": {"eq": meter.meter_id}, "meterVersion": {"gte": 1, "lt": 2}},
            )
            rows = query.execute().items
            assert [row[identity] for row in rows] == ["query-1", "query-2", "query-3"]
            reverse = replace(
                query, order_by=(UsageOrder("windowStart", "desc"), UsageOrder(identity, "desc"))
            )
            assert [row[identity] for row in reverse.execute().items] == [
                "query-3",
                "query-2",
                "query-1",
            ]
            selected = query.selecting(identity).execute().items
            assert [row[identity] for row in selected] == ["query-1", "query-2", "query-3"]
            first = query.page(limit=2).execute()
            assert len(first.items) == 2
            assert first.cursor
            second = query.page(limit=2, cursor=first.cursor).execute()
            assert [row[identity] for row in second.items] == ["query-3"]
        latest = repo.latest_aggregate(event.scope, "query-1", window_start=start, window_end=end)
        assert latest.total == Decimal("2000.000000")
