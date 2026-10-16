# Atlas service runbook

The Atlas ingestion service listens on port 8443 for control traffic and port
9090 for metrics. Operators restart it with `atlasctl restart` after rotating
credentials.

Retention is 30 days for raw events and 400 days for aggregated counters.
