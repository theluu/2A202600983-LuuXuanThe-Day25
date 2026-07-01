"""Interactive test UI for the reliability gateway.

Serves a small web app that drives the REAL ReliabilityGateway / CircuitBreaker /
ResponseCache / SharedRedisCache implementations — nothing is re-simulated in JS.

Run:
    pip install -e ".[web]"
    uvicorn app:app --reload
    open http://localhost:8000
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.chaos import (
    build_gateway,
    calculate_recovery_time_ms,
    load_queries,
    run_scenario,
)
from reliability_lab.config import LabConfig, ScenarioConfig, load_config
from reliability_lab.gateway import GatewayResponse, ReliabilityGateway
from reliability_lab.metrics import RunMetrics

CONFIG_PATH = "configs/default.yaml"
WEB_DIR = Path(__file__).parent / "web"

app = FastAPI(title="Reliability Gateway — Test UI")


class _Session:
    """Holds the live gateway + accumulated metrics for the manual playground."""

    def __init__(self) -> None:
        self.base_config: LabConfig = load_config(CONFIG_PATH)
        self.queries: list[str] = load_queries()
        self.overrides: dict[str, float] = {}
        self.gateway: ReliabilityGateway = build_gateway(self.base_config)
        self.metrics: RunMetrics = RunMetrics()

    def rebuild(self) -> None:
        self.gateway = build_gateway(self.base_config, self.overrides or None)
        self.metrics = RunMetrics()

    def record(self, result: GatewayResponse) -> None:
        m = self.metrics
        m.total_requests += 1
        m.estimated_cost += result.estimated_cost
        if result.cache_hit:
            m.cache_hits += 1
            m.estimated_cost_saved += 0.001
        if result.route == "fallback":
            m.fallback_successes += 1
            m.successful_requests += 1
        elif result.route == "static_fallback":
            m.static_fallbacks += 1
            m.failed_requests += 1
        else:
            m.successful_requests += 1
        if result.latency_ms > 0:
            m.latencies_ms.append(result.latency_ms)


session = _Session()


# --------------------------------------------------------------------------- #
# Serialization helpers
# --------------------------------------------------------------------------- #
def breakers_state() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for name, cb in session.gateway.breakers.items():
        out.append(
            {
                "name": name,
                "state": cb.state.value,
                "failure_count": cb.failure_count,
                "success_count": cb.success_count,
                "failure_threshold": cb.failure_threshold,
                "transitions": [
                    {"from": t["from"], "to": t["to"], "reason": t["reason"]}
                    for t in cb.transition_log[-6:]
                ],
                "fail_rate": next(
                    (p.fail_rate for p in session.gateway.providers if p.name == name), None
                ),
            }
        )
    return out


def cache_state() -> dict[str, Any]:
    cache = session.gateway.cache
    if cache is None:
        return {"backend": "disabled", "size": 0, "false_hits": 0}
    backend = type(cache).__name__
    size = 0
    entries = getattr(cache, "_entries", None)
    if entries is not None:
        size = len(entries)
    elif hasattr(cache, "_redis"):
        try:
            size = sum(1 for _ in cache._redis.scan_iter(f"{cache.prefix}*"))  # type: ignore[attr-defined]
        except Exception:
            size = -1
    false_log = getattr(cache, "false_hit_log", [])
    return {
        "backend": backend,
        "size": size,
        "false_hits": len(false_log),
        "false_hit_examples": [
            {"query": str(e.get("query", "")), "cached_key": str(e.get("cached_key", ""))}
            for e in false_log[-3:]
        ],
    }


def metrics_state() -> dict[str, Any]:
    m = session.metrics
    m.recovery_time_ms = calculate_recovery_time_ms(session.gateway)
    d = m.to_report_dict()
    d["circuit_open_count"] = sum(
        1
        for cb in session.gateway.breakers.values()
        for t in cb.transition_log
        if t["to"] == "open"
    )
    return d


def snapshot() -> dict[str, Any]:
    return {
        "breakers": breakers_state(),
        "cache": cache_state(),
        "metrics": metrics_state(),
        "overrides": session.overrides,
        "cache_enabled": session.base_config.cache.enabled,
        "backend": session.base_config.cache.backend,
        "sample_queries": session.queries[:8],
    }


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
class CompleteReq(BaseModel):
    prompt: str


class ConfigReq(BaseModel):
    primary_fail: float | None = None
    backup_fail: float | None = None
    cache_enabled: bool | None = None
    backend: str | None = None  # "memory" or "redis"


class ChaosReq(BaseModel):
    scenario: str  # "primary_timeout_100" | "primary_flaky_50" | "all_healthy"


class SimReq(BaseModel):
    a: str
    b: str


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (WEB_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/api/state")
def api_state() -> dict[str, Any]:
    return snapshot()


@app.post("/api/complete")
def api_complete(req: CompleteReq) -> dict[str, Any]:
    result = session.gateway.complete(req.prompt)
    session.record(result)
    return {
        "response": {
            "text": result.text,
            "route": result.route,
            "provider": result.provider,
            "cache_hit": result.cache_hit,
            "latency_ms": round(result.latency_ms, 2),
            "estimated_cost": round(result.estimated_cost, 6),
            "error": result.error,
        },
        "state": snapshot(),
    }


@app.post("/api/config")
def api_config(req: ConfigReq) -> dict[str, Any]:
    if req.primary_fail is not None:
        session.overrides["primary"] = max(0.0, min(1.0, req.primary_fail))
    if req.backup_fail is not None:
        session.overrides["backup"] = max(0.0, min(1.0, req.backup_fail))
    if req.cache_enabled is not None:
        session.base_config.cache.enabled = req.cache_enabled
    if req.backend is not None:
        session.base_config.cache.backend = req.backend
    session.rebuild()
    return snapshot()


@app.post("/api/reset")
def api_reset() -> dict[str, Any]:
    session.rebuild()
    return snapshot()


@app.post("/api/chaos")
def api_chaos(req: ChaosReq) -> dict[str, Any]:
    overrides_map = {
        "primary_timeout_100": {"primary": 1.0},
        "primary_flaky_50": {"primary": 0.5},
        "all_healthy": {},
    }
    scenario = ScenarioConfig(
        name=req.scenario,
        description=f"UI chaos run: {req.scenario}",
        provider_overrides=overrides_map.get(req.scenario, {}),
    )
    result: RunMetrics = run_scenario(session.base_config, session.queries, scenario)
    result.circuit_open_count = result.circuit_open_count  # already set by run_scenario
    report = result.to_report_dict()
    report["scenario"] = req.scenario
    report["requests"] = session.base_config.load_test.requests
    return report


@app.post("/api/tests")
def api_tests() -> dict[str, Any]:
    """Run the real pytest suite and return per-test results grouped by file."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-v", "--no-header", "-p", "no:cacheprovider"],
        cwd=str(Path(__file__).parent),
        capture_output=True,
        text=True,
        timeout=180,
    )
    line_re = re.compile(
        r"^(tests/[\w/]+\.py)::(\S+)\s+(PASSED|FAILED|XFAIL|XPASS|SKIPPED|ERROR)"
    )
    files: dict[str, list[dict[str, str]]] = {}
    counts: dict[str, int] = {}
    for line in proc.stdout.splitlines():
        m = line_re.match(line.strip())
        if not m:
            continue
        fname, test, status = m.group(1), m.group(2), m.group(3)
        files.setdefault(fname, []).append({"name": test, "status": status})
        counts[status] = counts.get(status, 0) + 1

    summary = ""
    for line in reversed(proc.stdout.splitlines()):
        if " in " in line and ("passed" in line or "failed" in line or "error" in line):
            summary = line.strip().strip("=").strip()
            break

    return {
        "files": files,
        "counts": counts,
        "summary": summary,
        "total": sum(counts.values()),
        "ok": counts.get("FAILED", 0) == 0 and counts.get("ERROR", 0) == 0,
    }


@app.post("/api/compare")
def api_compare() -> dict[str, Any]:
    """Live cache vs no-cache comparison on a quick sample run (same seed of queries)."""

    def quick(cache_enabled: bool) -> dict[str, Any]:
        cfg = session.base_config.model_copy(deep=True)
        cfg.cache.enabled = cache_enabled
        cfg.cache.backend = "memory"
        cfg.load_test.requests = 40
        sc = ScenarioConfig(name="compare", provider_overrides={})
        return run_scenario(cfg, session.queries, sc).to_report_dict()

    without = quick(False)
    with_cache = quick(True)
    cost_w = float(without["estimated_cost"]) or 1e-9
    saved_pct = round((cost_w - float(with_cache["estimated_cost"])) / cost_w * 100, 1)
    return {"without": without, "with": with_cache, "cost_saved_pct": saved_pct}


@app.post("/api/similarity")
def api_similarity(req: SimReq) -> dict[str, Any]:
    """Expose the REAL n-gram cosine similarity + guardrail decisions."""
    from reliability_lab.cache import _is_uncacheable, _looks_like_false_hit

    score = ResponseCache.similarity(req.a, req.b)
    threshold = session.base_config.cache.similarity_threshold
    return {
        "score": round(score, 4),
        "threshold": threshold,
        "would_hit": score >= threshold,
        "privacy_blocked": _is_uncacheable(req.a) or _is_uncacheable(req.b),
        "false_hit": _looks_like_false_hit(req.a, req.b),
    }


@app.post("/api/redis-demo")
def api_redis_demo() -> dict[str, Any]:
    """Prove Redis shared state: write via instance A, read via instance B."""
    prefix = "rl:uidemo:"
    try:
        a = SharedRedisCache("redis://localhost:6379/0", 60, 0.9, prefix=prefix)
        if not a.ping():
            return {"available": False, "error": "Redis không chạy (docker compose up -d)"}
        b = SharedRedisCache("redis://localhost:6379/0", 60, 0.9, prefix=prefix)
        a.flush()
        a.set("What are circuit breaker states?", "[via A] CLOSED, OPEN, HALF_OPEN")
        value, score = b.get("What are circuit breaker states?")
        keys = [k for k in a._redis.scan_iter(f"{prefix}*")]  # type: ignore[attr-defined]
        a.close()
        b.close()
        return {
            "available": True,
            "written_by": "instance A",
            "read_by": "instance B",
            "value": value,
            "score": score,
            "keys": keys,
        }
    except Exception as exc:  # pragma: no cover - defensive
        return {"available": False, "error": str(exc)}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
