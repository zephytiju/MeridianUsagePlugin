# Configuration

This package accepts logical Resources only. The default names are supplied by
`UsageResources`; applications may inject other logical structured
Resource references when Platform IaC renders a different namespace.

The schema provider entry point contributes all six resource definitions.
Deployment configuration must:

- place event and aggregate Resources on a bounded query/append-capable
  structured binding;
- place meter, batch, checkpoint, and claim Resources on a binding supporting
  conditional structured writes;
- select the atomic batch profile only where the events placement supports one
  Meridian transaction;
- apply identity, ACL, physical scope isolation, migration, backup/recovery,
  and retention policy outside this library.

`RetentionInput` emits logical policy labels and whole-second durations
for Platform/Vangu IaC. It never applies a TTL or executes a migration.

No environment variable or constructor accepts an endpoint, engine name,
secret, token, username, or password.
