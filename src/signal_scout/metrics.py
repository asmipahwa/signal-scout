"""Prometheus metrics shared by the API, the MCP server and (later) the pipeline jobs.

One explicit `REGISTRY` instead of prometheus_client's global default, so tests can build several
apps in one process without "duplicated timeseries" errors, and `/metrics` exposes exactly what we
define. Other modules add their own metrics by passing `registry=REGISTRY`.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Histogram

REGISTRY = CollectorRegistry(auto_describe=True)

# Buckets tuned for an API whose p95 target is in the tens-to-hundreds of milliseconds.
LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

HTTP_REQUEST_SECONDS = Histogram(
    "http_request_seconds",
    "HTTP request latency by route template, method and status code.",
    labelnames=("route", "method", "status"),
    buckets=LATENCY_BUCKETS,
    registry=REGISTRY,
)

SEARCH_REQUEST_SECONDS = Histogram(
    "search_request_seconds",
    "Elasticsearch insight search latency as seen by the services layer (API and MCP).",
    buckets=LATENCY_BUCKETS,
    registry=REGISTRY,
)

MCP_TOOL_SECONDS = Histogram(
    "mcp_tool_seconds",
    "MCP tool call latency by tool name and outcome.",
    labelnames=("tool", "outcome"),
    buckets=LATENCY_BUCKETS,
    registry=REGISTRY,
)
