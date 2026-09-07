# SPDX-License-Identifier: Apache-2.0
"""Keep the executed released predicate AST equal to the public logical plan."""

import pytest

from meridian_storage.plugins.usage import UsageRepository, UsageSchemaProvider, usage_schemas
from meridian_storage.query import parse_filter


@pytest.mark.parametrize(
    "where",
    [
        {"meterId": "api.requests"},
        {"meterVersion": {"eq": 1, "ne": 2, "gt": 0, "gte": 1, "lt": 3, "lte": 2}},
        {"meterId": {"in": ["a", "$a", "$$a"], "notIn": ["b", "$b"]}},
        {"correctionOf": {"isNull": True}},
        {"correctionOf": {"isNull": False}},
        {"subjectId": {"eq": "$subject", "ne": "$$other"}},
        {"subjectId": "$literal"},
    ],
)
def test_executed_predicate_matches_logical_plan(executor, event, where):
    query = UsageRepository(executor).queries.events(
        event.scope, event.window.start, event.window.end, where=where
    )
    lowered = parse_filter(query.expression.arguments["where"])
    assert lowered.to_dict() == query.logical_plan.filter.to_dict()
    assert query.predicates["windowStart"] == {
        "gte": "2026-08-26T10:00:00Z",
        "lt": "2026-08-26T10:01:00Z",
    }


def test_default_resource_profiles_match_unchanged_schemas():
    documents = {doc.to_core_definition().ref: doc for doc in usage_schemas()}
    bundle = UsageSchemaProvider().load()
    for resource in bundle.resources:
        assert resource.profile == documents[resource.schema].semantic_kind.value
