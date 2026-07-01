# Day 10 Reliability Report

Reproducible: `docker compose up -d && make test && make run-chaos`. Runs use a fixed RNG
seed (`seed: 13` in `configs/default.yaml`), so the **behavioral** metrics below —
availability, error rate, cache hit rate, cost, circuit-open count, and per-scenario
pass/fail — are identical on every run. Latency percentiles and recovery time measure real
wall-clock time (`time.perf_counter()` around a real `sleep` in `FakeLLMProvider`), so they
vary by ~1–2 ms of OS scheduling noise between runs; the values shown are from a representative
run (`reports/metrics.json`). Run size = 300 requests (100 × 3 scenarios).

## 1. Architecture summary

The gateway routes every request through three defensive layers. A request is served by the
cheapest layer that can satisfy it: a cache hit costs nothing, a healthy provider costs one
call, and the static fallback guarantees the caller never sees a hard failure.

```
User Request
    |
    v
[Gateway.complete()]
    |
    v
[Cache: memory or Redis] --HIT--> return GatewayResponse(route="cache_hit:<score>", cost=0)
    |  MISS / uncacheable
    v
[Circuit Breaker: primary] --call--> Provider "primary"
    |   OPEN? fail fast (CircuitOpenError)         | success -> cache.set(), route="primary"
    |   ProviderError -> record_failure, continue  |
    v
[Circuit Breaker: backup] --call--> Provider "backup"
    |   OPEN? fail fast                            | success -> cache.set(), route="fallback"
    |   ProviderError -> record_failure, continue  |
    v
[Static fallback]  route="static_fallback", error=<last provider error>
                   "The service is temporarily degraded. Please try again soon."
```

- **Circuit breaker** — a 3-state machine per provider (`CLOSED → OPEN → HALF_OPEN → CLOSED`).
  OPEN fails fast until `reset_timeout_seconds` elapses, then a single HALF_OPEN probe decides
  whether to close (`probe_success`) or re-open (`probe_failure`). This prevents retry storms
  against a provider that is already down.
- **Cache** — semantic cache (character-3-gram + word-token cosine similarity) with a privacy
  guardrail (never store/serve queries containing balance/password/SSN/user-id patterns) and a
  false-hit guardrail (reject a match if the query and cached key contain different 4-digit
  numbers, e.g. different years).
- **Fallback chain** — providers are tried in order; the first is `primary`, the rest are
  `fallback`. If all fail, a static degraded message is returned so availability never drops to 0.

## 2. Configuration

| Setting | Value | Reason |
|---|---:|---|
| failure_threshold | 3 | Tolerate transient blips; open only after 3 consecutive failures so one flaky call doesn't trip the breaker. |
| reset_timeout_seconds | 2 | Short enough to recover quickly after a provider heals, long enough to avoid hammering a down provider. Observed avg recovery ≈ 2.4 s. |
| success_threshold | 1 | One good HALF_OPEN probe is enough to trust `primary` again; raising it delays recovery with little benefit at this failure rate. |
| cache TTL | 300 s | Answers here are stable (FAQ/policy/technical). 5 min bounds staleness while keeping hit rate high (~63%). |
| similarity_threshold | 0.92 | Tested lower values: 0.85 produced false hits across near-duplicate phrasings. 0.92 keeps paraphrase hits while the false-hit guard catches year/ID drift. |
| load_test requests | 100 (×3 scenarios = 300) | Enough samples for stable P95/P99 without a slow run. |
| seed | 13 | Fixes the RNG so behavioral metrics are reproducible across runs and match this report. |
| providers | primary (fail 0.25) → backup (fail 0.05) | primary is cheaper/faster but flakier; backup is the reliable, slightly pricier safety net. |

## 3. SLO definitions

| SLI | SLO target | Actual value | Met? |
|---|---|---:|---|
| Availability | >= 99% | 98.67% (memory) / 99.33% (redis) | ⚠️ memory just under; ✅ redis |
| Latency P95 | < 2500 ms | 318.68 ms | ✅ |
| Fallback success rate | >= 95% | 95.12% | ✅ |
| Cache hit rate | >= 10% | 63.0% | ✅ |
| Recovery time | < 5000 ms | 2412 ms | ✅ |

Note: availability is measured against a `primary_timeout_100` scenario (primary fails 100% of
the time), which is deliberately adversarial. The redis run reaches 99.33% because a warmer
shared cache absorbs more traffic before it ever reaches a provider.

## 4. Metrics

From `reports/metrics.json` (memory cache backend, `seed: 13`):

| Metric | Value |
|---|---:|
| availability | 0.9867 |
| error_rate | 0.0133 |
| latency_p50_ms | 290.03 |
| latency_p95_ms | 318.68 |
| latency_p99_ms | 321.99 |
| fallback_success_rate | 0.9512 |
| cache_hit_rate | 0.63 |
| estimated_cost | 0.04477 |
| estimated_cost_saved | 0.189 |
| circuit_open_count | 10 |
| recovery_time_ms | 2411.82 |

## 5. Cache comparison

Same config, cache enabled vs `cache.enabled: false`
(`reports/metrics.json` vs `reports/metrics_no_cache.json`):

| Metric | Without cache | With cache | Delta |
|---|---:|---:|---|
| latency_p50_ms | 278.87 | 290.03 | +11.16 ms (cache lookup overhead) |
| latency_p95_ms | 317.62 | 318.68 | +1.06 ms |
| estimated_cost | 0.121746 | 0.04477 | **−63.2% cost** |
| availability | 0.9733 | 0.9867 | +1.34 pts |
| circuit_open_count | 24 | 10 | −14 (cache shields providers) |
| cache_hit_rate | 0 | 0.63 | +0.63 |

The cache trades a few ms of lookup latency for a **63% cost reduction** and higher availability:
by serving ~63% of traffic from cache, far fewer requests reach the flaky primary, so its
breaker opens less than half as often (24 → 10).

> Why the latency percentiles barely move: cache hits are recorded with `latency_ms = 0` and are
> excluded from the latency sample (`if result.latency_ms > 0`), so P50/P95/P99 measure only the
> *provider-call* tail — which caching does not speed up. The cache's win shows up in **cost** and
> **availability**, not in these provider-only percentiles.

## 6. Redis shared cache

- **Why in-memory cache is insufficient for multi-instance deployments:** each gateway process
  keeps its own `ResponseCache` in local RAM. With N replicas behind a load balancer, a query
  cached on replica A is a miss on replicas B…N, so hit rate degrades roughly to `1/N` of the
  single-instance rate and cost/latency savings mostly evaporate. The cache is also lost on every
  restart/deploy (cold start).
- **How `SharedRedisCache` solves this:** all instances read/write the same Redis keyspace
  (`rl:cache:<md5(query)>` hashes with `query`/`response` fields and a server-side `EXPIRE` TTL).
  A write on any instance is immediately visible to all others, so hit rate scales with total
  fleet traffic instead of per-instance traffic, and the cache survives process restarts. In the
  runs above the shared cache reached a **70.3% hit rate** (vs 63% memory) and the lowest cost
  (0.037426, a further −16% vs memory, −69% vs no cache) because it was pre-warmed across scenarios.

### Evidence of shared state

Two independent `SharedRedisCache` instances pointed at the same Redis; instance 2 reads a value
written only by instance 1:

```
Instance c2 sees c1's write: '[demo] CLOSED, OPEN, HALF_OPEN' score 1.0
```

`tests/test_redis_cache.py::test_shared_state_across_instances` asserts the same behavior and passes.

### Redis CLI output

```bash
$ docker compose exec redis redis-cli KEYS "rl:cache:*"
rl:cache:844ef0143a5c
rl:cache:b2a52f7dc795
rl:cache:3dab98c0e49e
rl:cache:095946136fea
rl:cache:734852f3cf4a
... (12 keys total)

$ docker compose exec redis redis-cli HGETALL "rl:cache:844ef0143a5c"
response  [backup] reliable answer for: List three benefits of response caching in LLM gateways.
query     List three benefits of response caching in LLM gateways.
```

### In-memory vs Redis latency comparison

| Metric | In-memory cache | Redis cache | Notes |
|---|---:|---:|---|
| latency_p50_ms | 290.03 | 287.07 | Redis adds a small network round-trip per lookup |
| latency_p95_ms | 318.68 | 318.51 | Difference is within run-to-run noise |
| cost | 0.04477 | 0.037426 | Redis cheaper here due to higher (70%) hit rate |

## 7. Chaos scenarios

Pass/fail criteria implemented in `chaos._scenario_passed()`.

| Scenario | Expected behavior | Observed behavior | Pass/Fail |
|---|---|---|---|
| primary_timeout_100 | primary fails 100% → all traffic to backup/cache, circuit opens | availability ≥ 0.9 and circuit_open_count ≥ 1; every primary call fails and is absorbed by backup + cache | **Pass** |
| primary_flaky_50 | primary fails 50% → breaker oscillates, mix of primary + fallback | breaker repeatedly opens/half-opens; availability stayed ≥ 0.8; recovery avg ≈ 2.4 s | **Pass** |
| all_healthy | both providers healthy → served by primary, ~no circuit opens | high availability, minimal breaker activity, cache warms | **Pass** |

Combined: `circuit_open_count = 10`, `recovery_time_ms = 2412`, availability `0.9867`.

## 8. Failure analysis

**What could still go wrong?**
Circuit-breaker state lives in per-process memory. In a multi-instance deployment each replica
learns "primary is down" independently, so during an outage every replica still sends its own
probe traffic to the failing provider — the fleet as a whole can still generate a small retry
storm, and recovery timing is uncoordinated. The shared *cache* is fixed via Redis, but the
shared *breaker state* is not.

**What would I change before production?**
Store breaker counters/state in Redis (`INCR` failure counts with `EXPIRE`, a shared `opened_at`),
so all instances trip and recover together (this is one of the lab's stretch goals). I would also
add graceful degradation: if Redis is unreachable, fall back to the in-memory cache instead of
failing lookups, so a Redis outage can't take the gateway down with it.

## 9. Next steps

1. **Shared breaker state in Redis** — coordinate open/half-open across instances to eliminate
   fleet-wide retry storms and give a single, coherent recovery signal.
2. **Redis graceful degradation** — catch Redis connection errors in `SharedRedisCache` and fall
   back to a local `ResponseCache` so cache-layer failures degrade instead of erroring.
3. **Cost-aware routing** — track cumulative spend; past 80% of budget prefer the cheaper
   provider/cache-only, and expose a per-user rate limit to protect the shared cost budget.
