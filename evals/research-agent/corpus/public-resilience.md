# Resilience frozen note

The durable task queue records task transitions in a local append-only journal.

The circuit breaker opens after a configured number of failures, while the rate limiter bounds calls in one process.
