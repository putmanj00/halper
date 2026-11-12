# Retries with backoff

Transient failures (timeouts, 429, 503) are worth retrying; client errors are not.
Use exponential backoff with jitter so many clients do not retry in lockstep.
Cap both the delay and the number of attempts. Retried operations must be idempotent,
or carry an idempotency key so the server can discard duplicates.
