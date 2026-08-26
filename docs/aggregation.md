# Aggregation

`aggregate_events` is a pure, order-independent Decimal sum over
normalized events. It groups by aligned half-open windows and selected
dimensions. Signed corrections participate naturally in the total.

`AggregationRunner` adds portable coordination:

1. acquire or renew a scoped leased claim using a conditional structured write;
2. read the current watermark checkpoint;
3. scan bounded event pages, including the configured lateness lookback;
4. calculate deterministic source-set fingerprints;
5. append immutable aggregate versions;
6. advance the checkpoint with compare-and-set;
7. release the claim.

If a process stops after aggregate writes but before checkpoint advancement,
the next run recomputes the same source fingerprint and replays the existing
versions before advancing. If late data changes a previously closed window, a
new revision is appended with `supersedes` naming the prior version.

Claims and checkpoints are Usage library control records. They are not
Projection outbox checkpoints and do not introduce a cache Catalog or service.
