# ROBIE durable Job Engine

This package adds a server-owned completion authority beside the existing Hermes runtime.
The Computer Worker can only return an action receipt. `JobEngine` independently reads the
destination, stores tamper-evident evidence, and is the only code path that may set `COMPLETE`.

Lifecycle:

`PENDING -> RUNNING -> VERIFYING -> COMPLETE | UNVERIFIED | FAILED`

Retries use persisted `RETRY_WAIT` wakeups with exponential backoff and jitter. The action
checkpoint prevents a successful action from being repeated when only verification needs a
retry. Idempotency keys deduplicate repeated Gmail/Google Chat delivery.

Integration boundaries:

- Keep the current `hermes-gateway`, Gmail watcher, Gmail/Google Chat reporting, and Drive skill
  sync services unchanged.
- Enqueue each inbound request with the Gmail message ID or Google Chat event ID as the
  `idempotency_key`.
- Register the existing Hermes/cua-driver implementation as `hermes-cua`.
- Route the three hardened EZLynx actions through `HermesCuaEzlynxWorker`.
- Implement `EzlynxReadback.api_state` from an observed EZLynx network endpoint when available.
  Return `None` when it is not; the adapter must then create a fresh navigation/session and read
  the server-backed UI state.
- Reporting reads the persisted status/evidence. It must never infer success from worker text.
- A future Gemini native Computer Use worker can implement the same `ComputerWorker` protocol;
  it is intentionally not required for this reliability gate.

Run the acceptance suite with:

```sh
PYTHONPATH=. python3 -m unittest discover -s tests -v
```
