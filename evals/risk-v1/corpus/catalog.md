# Component catalogue

The ingestion worker writes to the durable queue. The query planner reads from
the column store. The scheduler owns the retry policy for failed batches.
