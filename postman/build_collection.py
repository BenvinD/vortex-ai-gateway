#!/usr/bin/env python3
"""Generate the Postman collection and environments for Vortex AI Gateway.

The collection is generated rather than hand-written because the interesting
part is the *assertions*, and thirty of those embedded in raw JSON is a file
nobody can review. Everything here is derived from the code it tests:

* routes      -- ``gateway.py`` (``/healthz``, ``/readyz``, ``/metrics``) and
                 ``routes.py`` (``/v1/chat/completions``, ``/v1/usage``)
* headers     -- ``cache.CACHE_HEADER``, ``cache.BYPASS_HEADER``,
                 ``semantic.SEMANTIC_HEADER``, ``ratelimit.Decision.headers``,
                 ``middleware.REQUEST_ID_HEADER``
* statuses    -- ``routes.FAILURE_STATUSES`` and
                 ``error_handling.install_error_handlers`` (validation is 400,
                 not 422)
* cache keys  -- ``cache.NON_SEMANTIC_FIELDS``

Run it after changing any of those::

    uv run python postman/build_collection.py

Output is Postman Collection Format v2.1, which the desktop app imports
directly and ``postman collection run`` executes unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Environments
# ---------------------------------------------------------------------------

# Every gating flag below mirrors a VORTEX_* setting, because the same
# assertions have to pass against a laptop with no Redis and against the
# compose stack with metering and the cache on. A test that cannot tell the
# two apart is a test that has to be deleted before a demo.
ENVIRONMENTS: list[dict[str, Any]] = [
    {
        "file": "local.postman_environment.json",
        "name": "Vortex · Local (mock, no Redis)",
        "doc": (
            "VORTEX_MODEL_ROUTES= VORTEX_METERING_ENABLED=false VORTEX_CACHE_ENABLED=false "
            "uv run uvicorn vortex_ai_gateway.gateway:app --reload"
        ),
        "values": {
            "base_url": "http://localhost:8000",
            "api_key": "vtx-local-demo-key",
            "vortex_environment": "local",
            "routes_configured": "false",
            "expect_mock": "true",
            "metering_enabled": "false",
            "cache_enabled": "false",
            "semantic_cache_enabled": "false",
            "prometheus_url": "",
            "grafana_url": "",
            "jaeger_url": "",
        },
    },
    {
        "file": "docker-demo.postman_environment.json",
        "name": "Vortex · Docker Demo (dev)",
        "doc": "make demo  --  gateway + redis + prometheus + grafana + jaeger",
        "values": {
            "base_url": "http://localhost:8000",
            "api_key": "vtx-demo-key",
            "vortex_environment": "dev",
            "routes_configured": "false",
            "expect_mock": "true",
            "metering_enabled": "true",
            "cache_enabled": "true",
            "semantic_cache_enabled": "false",
            "prometheus_url": "http://localhost:9090",
            "grafana_url": "http://localhost:3000",
            "jaeger_url": "http://localhost:16686",
        },
    },
    {
        "file": "local-semantic.postman_environment.json",
        "name": "Vortex · Local + semantic cache (ADR-024)",
        "doc": (
            "docker compose up -d redis && VORTEX_MODEL_ROUTES= VORTEX_METERING_ENABLED=true "
            "VORTEX_CACHE_ENABLED=true VORTEX_SEMANTIC_CACHE_ENABLED=true "
            "VORTEX_EMBEDDING_MODEL=nomic-embed-text "
            "VORTEX_EMBEDDING_BASE_URL=http://localhost:11434 "
            "VORTEX_RATE_LIMIT_DEFAULT_RPM=600 VORTEX_RATE_LIMIT_DEFAULT_TPM=150000 "
            "uv run uvicorn vortex_ai_gateway.gateway:app"
        ),
        "values": {
            "base_url": "http://localhost:8000",
            "api_key": "vtx-semantic-demo-key",
            "vortex_environment": "local",
            "routes_configured": "false",
            "expect_mock": "true",
            "metering_enabled": "true",
            "cache_enabled": "true",
            "semantic_cache_enabled": "true",
            "prometheus_url": "",
            "grafana_url": "",
            "jaeger_url": "",
        },
    },
    {
        "file": "staging.postman_environment.json",
        "name": "Vortex · Staging",
        "doc": "Placeholder host and key. Point base_url at your staging gateway.",
        "values": {
            "base_url": "https://vortex-staging.internal.example.com",
            "api_key": "",
            "vortex_environment": "staging",
            "routes_configured": "true",
            "expect_mock": "false",
            "metering_enabled": "true",
            "cache_enabled": "true",
            "semantic_cache_enabled": "false",
            "prometheus_url": "",
            "grafana_url": "",
            "jaeger_url": "",
        },
    },
    {
        "file": "production.postman_environment.json",
        "name": "Vortex · Production",
        "doc": "Placeholder host. Read-only requests are safe; the rest spend money.",
        "values": {
            "base_url": "https://vortex.example.com",
            "api_key": "",
            "vortex_environment": "prod",
            "routes_configured": "true",
            "expect_mock": "false",
            "metering_enabled": "true",
            "cache_enabled": "true",
            "semantic_cache_enabled": "false",
            "prometheus_url": "",
            "grafana_url": "",
            "jaeger_url": "",
        },
    },
]

# Model names shared by every environment, kept on the collection so one edit
# retargets all four. These are the three `scripts/generate_traffic.py` uses,
# so Postman traffic and benchmark traffic land on the same dashboard series.
COLLECTION_VARIABLES: dict[str, str] = {
    "base_url": "http://localhost:8000",
    "api_key": "vtx-demo-key",
    "model_primary": "gpt-4o-mini",
    "model_secondary": "claude-3-5-haiku",
    "model_tertiary": "llama3.1-8b",
    "unroutable_model": "no-such-model-anywhere",
    "usage_days": "7",
    "cache_nonce": "",
    "last_completion_id": "",
}

SECRET_KEYS = {"api_key"}


def environment(spec: dict[str, Any]) -> dict[str, Any]:
    """One Postman environment file."""
    return {
        "id": f"vortex-env-{spec['file'].split('.')[0]}",
        "name": spec["name"],
        "values": [
            {
                "key": key,
                "value": value,
                "type": "secret" if key in SECRET_KEYS else "default",
                "enabled": True,
            }
            for key, value in spec["values"].items()
        ],
        "_postman_variable_scope": "environment",
        "_postman_exported_using": "vortex-ai-gateway/postman/build_collection.py",
    }


# ---------------------------------------------------------------------------
# Collection builders
# ---------------------------------------------------------------------------


def script(lines: list[str], kind: str = "test") -> dict[str, Any]:
    """One Postman event, as the app stores it."""
    return {
        "listen": kind,
        "script": {"type": "text/javascript", "exec": lines},
    }


def body(payload: dict[str, Any]) -> dict[str, Any]:
    """A raw JSON body, pretty-printed so it is readable in the app."""
    return {
        "mode": "raw",
        "raw": json.dumps(payload, indent=2),
        "options": {"raw": {"language": "json"}},
    }


def url(path: str, query: list[dict[str, str]] | None = None) -> dict[str, Any]:
    """A URL in the object form, so query params show up as rows in the app."""
    raw = "{{base_url}}" + path
    if query:
        raw += "?" + "&".join(f"{item['key']}={item['value']}" for item in query)
    spec: dict[str, Any] = {
        "raw": raw,
        "host": ["{{base_url}}"],
        "path": path.strip("/").split("/"),
    }
    if query:
        spec["query"] = query
    return spec


def request(
    name: str,
    *,
    method: str = "GET",
    path: str,
    description: str,
    tests: list[str],
    payload: dict[str, Any] | None = None,
    headers: list[dict[str, str]] | None = None,
    query: list[dict[str, str]] | None = None,
    prerequest: list[str] | None = None,
    noauth: bool = False,
) -> dict[str, Any]:
    """One request, with its assertions attached."""
    hdrs = list(headers or [])
    if payload is not None and not any(h["key"].lower() == "content-type" for h in hdrs):
        hdrs.insert(0, {"key": "Content-Type", "value": "application/json"})

    item: dict[str, Any] = {
        "name": name,
        "event": [],
        "request": {
            "method": method,
            "header": hdrs,
            "url": url(path, query),
            "description": description,
        },
        "response": [],
    }
    if noauth:
        item["request"]["auth"] = {"type": "noauth"}
    if payload is not None:
        item["request"]["body"] = body(payload)
    if prerequest:
        item["event"].append(script(prerequest, kind="prerequest"))
    item["event"].append(script(tests))
    return item


def folder(name: str, description: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    """One folder, which is how the app groups a demo into chapters."""
    return {"name": name, "description": description, "item": items}


# A prompt that is unique per collection run, so the cache folder's first
# request is a real MISS on the second run too. Without this the cache tests
# pass once and then quietly assert nothing.
NONCE_PRELUDE = [
    "// A fresh prompt per run, so 'MISS' means missed and not 'ran twice'.",
    "const nonce = Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 8);",
    "pm.collectionVariables.set('cache_nonce', nonce);",
]

CHAT = "/v1/chat/completions"


# ---------------------------------------------------------------------------
# 00 - Health and observability
# ---------------------------------------------------------------------------

HEALTH = folder(
    "00 · Health & observability",
    "The three open endpoints. `/healthz` and `/readyz` follow the Kubernetes "
    "probe split — liveness restarts the container, readiness drains it — and "
    "`/metrics` is published unauthenticated on purpose, because a scraper has "
    "no API key (ADR-028). All three deliberately carry no `Authorization` "
    "header here, which is what proves they are open.",
    [
        request(
            "Liveness · GET /healthz",
            path="/healthz",
            noauth=True,
            description=(
                "Liveness probe. Touches nothing external — no Redis, no provider, no "
                "config — because a dependency outage that made this fail would have "
                "the orchestrator restart a perfectly healthy process and make the "
                "outage worse."
            ),
            tests=[
                "pm.test('200 OK', () => pm.response.to.have.status(200));",
                "pm.test(\"body is {status: 'ok'}\", () => {",
                "  pm.expect(pm.response.json()).to.eql({ status: 'ok' });",
                "});",
                "pm.test('answers without a key (probe is open)', () => {",
                "  pm.expect(pm.request.headers.has('Authorization')).to.be.false;",
                "});",
                "pm.test('carries a request id', () => {",
                "  pm.expect(pm.response.headers.has('x-request-id')).to.be.true;",
                "});",
            ],
        ),
        request(
            "Readiness · GET /readyz",
            path="/readyz",
            noauth=True,
            description=(
                "Readiness probe: runs every registered dependency check and returns "
                "503 with a per-check breakdown on any failure.\n\n"
                "Redis is pointedly **not** one of those checks. The limiter and the "
                "ledger both fail open, so draining a working instance because Redis "
                "is degraded would turn a degradation into an outage (ADR-021)."
            ),
            tests=[
                "pm.test('200 OK', () => pm.response.to.have.status(200));",
                "pm.test(\"status is 'ready'\", () => {",
                "  pm.expect(pm.response.json().status).to.eql('ready');",
                "});",
                "// Redis is deliberately absent from the checks, so a Redis-less",
                "// gateway is still ready. Any check present must report 'ok'.",
                "pm.test('every registered check reports ok', () => {",
                "  const report = pm.response.json();",
                "  Object.keys(report)",
                "    .filter((k) => k !== 'status')",
                "    .forEach((k) => pm.expect(report[k], k).to.eql('ok'));",
                "});",
            ],
        ),
        request(
            "Prometheus metrics · GET /metrics",
            path="/metrics",
            noauth=True,
            description=(
                "This worker's counters in the Prometheus text format, from a registry "
                "of its own — nothing is here that `metrics.py` did not name (ADR-027).\n\n"
                "Open like the probes: boring by construction (counts and latencies, "
                "never a prompt, a completion or a key), and expected to be reachable "
                "only from inside the network. Run **one worker per port** — these "
                "numbers are per process, like the breaker state and both cache "
                "counters."
            ),
            tests=[
                "pm.test('200 OK', () => pm.response.to.have.status(200));",
                "pm.test('Prometheus text format', () => {",
                "  pm.expect(pm.response.headers.get('Content-Type')).to.include('text/plain');",
                "});",
                "const text = pm.response.text();",
                "pm.test('publishes vortex_build_info', () => {",
                "  pm.expect(text).to.include('vortex_build_info');",
                "});",
                "pm.test('build info is labelled with the running environment', () => {",
                "  const expected = pm.environment.get('vortex_environment');",
                "  if (!expected) { pm.expect.fail('set vortex_environment on the environment'); }",
                "  pm.expect(text).to.include('environment=\"' + expected + '\"');",
                "});",
                "// The instrument names come from metrics.py, which is the only",
                "// file allowed to name one -- nothing reaches /metrics that it",
                "// did not declare.",
                "[",
                "  'vortex_requests_total',",
                "  'vortex_request_duration_seconds',",
                "  'vortex_cache_lookups_total',",
                "  'vortex_provider_attempts_total',",
                "  'vortex_tokens_total',",
                "  'vortex_cost_usd_total',",
                "].forEach((instrument) => {",
                "  pm.test('publishes ' + instrument, () => {",
                "    pm.expect(text).to.include(instrument);",
                "  });",
                "});",
            ],
        ),
    ],
)


# ---------------------------------------------------------------------------
# 01 - Authentication
# ---------------------------------------------------------------------------

SIMPLE_PROMPT = {
    "model": "{{model_primary}}",
    "messages": [{"role": "user", "content": "Say hello in exactly five words."}],
}

AUTH = folder(
    "01 · Authentication (ADR-003, ADR-013)",
    "Auth answers one question — *who is calling* — and returns a `Principal`, "
    "never a token. `key_id` is the public, stable half, which is what every "
    "rate limit, ledger entry, cache namespace and log line downstream is keyed "
    "on.\n\n"
    "Keys come from exactly one of three sources, never merged: the SQLite key "
    "store (`VORTEX_KEY_DB_PATH`), the plaintext `VORTEX_API_KEYS` list, or "
    "development mode. Two allow-lists would mean revoking from one and still "
    "being let in by the other.\n\n"
    "Note the rejection message is identical for an unknown, a revoked and a "
    "wrong key: telling them apart tells an attacker which key IDs exist.",
    [
        request(
            "401 · no Authorization header",
            method="POST",
            path=CHAT,
            noauth=True,
            payload=SIMPLE_PROMPT,
            description=(
                "Development mode accepts *any* well-formed key, but a request "
                "carrying no key is still rejected — so the 401 path is exercised by "
                "default rather than discovered in production."
            ),
            tests=[
                "pm.test('401 Unauthorized', () => pm.response.to.have.status(401));",
                "const err = pm.response.json().error;",
                "pm.test('OpenAI error envelope', () => {",
                "  pm.expect(err).to.be.an('object');",
                "  pm.expect(err).to.have.property('message');",
                "});",
                "pm.test(\"code is 'missing_api_key'\", () => {",
                "  pm.expect(err.code).to.eql('missing_api_key');",
                "});",
                "pm.test('message tells the caller the header to send', () => {",
                "  pm.expect(err.message).to.include('Bearer');",
                "});",
            ],
        ),
        request(
            "401 · malformed Authorization header",
            method="POST",
            path=CHAT,
            noauth=True,
            headers=[{"key": "Authorization", "value": "Token {{api_key}}"}],
            payload=SIMPLE_PROMPT,
            description=(
                "`Token <key>` instead of `Bearer <key>`. Kept distinct from the "
                "missing-header case because they are different bugs at the caller: a "
                "missing config value versus a malformed one."
            ),
            tests=[
                "pm.test('401 Unauthorized', () => pm.response.to.have.status(401));",
                "pm.test(\"code is 'invalid_authorization_header'\", () => {",
                "  pm.expect(pm.response.json().error.code).to.eql('invalid_authorization_header');",
                "});",
                "pm.test('distinguished from a missing key', () => {",
                "  pm.expect(pm.response.json().error.code).to.not.eql('missing_api_key');",
                "});",
            ],
        ),
        request(
            "401 · bearer with no token",
            method="POST",
            path=CHAT,
            noauth=True,
            headers=[{"key": "Authorization", "value": "Bearer "}],
            payload=SIMPLE_PROMPT,
            description=(
                "`Bearer` with nothing after it. Still a 401, and the *reason* depends "
                "on what reaches the gateway:\n\n"
                "* With the trailing space preserved (curl `-H 'Authorization: Bearer '`) "
                "the header is readable but the token is empty — `missing_api_key`.\n"
                "* Postman trims trailing whitespace from a header value, so the gateway "
                "sees a bare `Bearer`, which does not match the `'Bearer '` prefix — "
                "`invalid_authorization_header`.\n\n"
                "Both are asserted rather than one being forced, because the difference "
                "is Postman's, not the gateway's."
            ),
            tests=[
                "pm.test('401 Unauthorized', () => pm.response.to.have.status(401));",
                "const code = pm.response.json().error.code;",
                "pm.test('rejected as a missing key or a malformed header', () => {",
                "  pm.expect(code).to.be.oneOf(['missing_api_key', 'invalid_authorization_header']);",
                "});",
                "console.log('bearer-with-no-token reported as: ' + code);",
            ],
        ),
        request(
            "200 · valid bearer key",
            method="POST",
            path=CHAT,
            payload=SIMPLE_PROMPT,
            description=(
                "The collection's `{{api_key}}`, sent as collection-level Bearer auth. "
                "Every request below inherits it.\n\n"
                "With neither a key store nor `VORTEX_API_KEYS` set, this is "
                "development mode: the key is accepted for being well formed, and the "
                "`key_id` downstream is a digest of it."
            ),
            tests=[
                "pm.test('200 OK', () => pm.response.to.have.status(200));",
                "pm.test('an API key is actually being sent', () => {",
                "  pm.expect(pm.request.headers.get('Authorization')).to.include('Bearer');",
                "});",
                "pm.test(\"object is 'chat.completion'\", () => {",
                "  pm.expect(pm.response.json().object).to.eql('chat.completion');",
                "});",
            ],
        ),
    ],
)


# ---------------------------------------------------------------------------
# 02 - Chat completions
# ---------------------------------------------------------------------------

COMPLETION_SHAPE = [
    "pm.test('200 OK', () => pm.response.to.have.status(200));",
    "const res = pm.response.json();",
    "pm.test('OpenAI chat-completion shape', () => {",
    "  pm.expect(res.object).to.eql('chat.completion');",
    "  pm.expect(res.id).to.be.a('string').and.not.empty;",
    "  pm.expect(res.created).to.be.a('number');",
    "  pm.expect(res.model).to.be.a('string').and.not.empty;",
    "  pm.expect(res.choices).to.be.an('array').with.lengthOf.at.least(1);",
    "});",
    "pm.test('first choice is an assistant turn with a finish reason', () => {",
    "  pm.expect(res.choices[0].index).to.eql(0);",
    "  pm.expect(res.choices[0].message.role).to.eql('assistant');",
    "  pm.expect(res.choices[0].finish_reason).to.be.a('string').and.not.empty;",
    "});",
]

USAGE_SHAPE = [
    "pm.test('usage adds up', () => {",
    "  pm.expect(res.usage, 'a buffered completion always reports usage').to.be.an('object');",
    "  const u = res.usage;",
    "  pm.expect(u.prompt_tokens).to.be.at.least(0);",
    "  pm.expect(u.completion_tokens).to.be.at.least(0);",
    "  pm.expect(u.total_tokens).to.eql(u.prompt_tokens + u.completion_tokens);",
    "});",
]

CHAT_COMPLETIONS = folder(
    "02 · Chat completions (ADR-009, ADR-010, ADR-012)",
    "One wire format: `POST /v1/chat/completions` accepts and returns OpenAI's "
    "chat-completions shape, so an existing OpenAI SDK works by changing its "
    "base URL and nothing else.\n\n"
    '`extra="forbid"` is load-bearing on every contract model. A request '
    "carrying `temperture` is a 400 naming the field, not a silent default — "
    "the cost being that a parameter OpenAI ships tomorrow is a 400 here until "
    "it is declared (ADR-010).",
    [
        request(
            "Basic completion",
            method="POST",
            path=CHAT,
            payload={
                "model": "{{model_primary}}",
                "messages": [
                    {"role": "user", "content": "Explain an API gateway in one sentence."}
                ],
            },
            description=(
                "The happy path, and the baseline for everything below. Saves the "
                "completion id into `{{last_completion_id}}` so the demo can refer "
                "back to it."
            ),
            tests=[
                *COMPLETION_SHAPE,
                *USAGE_SHAPE,
                "pm.test('the assistant actually said something', () => {",
                "  const choice = res.choices[0].message;",
                "  pm.expect(choice.content || choice.tool_calls, 'content or tool_calls').to.exist;",
                "});",
                "pm.collectionVariables.set('last_completion_id', res.id);",
            ],
        ),
        request(
            "Multi-turn conversation",
            method="POST",
            path=CHAT,
            payload={
                "model": "{{model_primary}}",
                "messages": [
                    {"role": "system", "content": "You are terse. Never exceed one sentence."},
                    {"role": "user", "content": "What does a circuit breaker do?"},
                    {
                        "role": "assistant",
                        "content": "It stops calling a provider that keeps failing.",
                    },
                    {"role": "user", "content": "And when does it try again?"},
                ],
            },
            description=(
                "System, user, assistant, user — the full role set, forwarded in order. "
                "The gateway holds no conversation state; the caller sends the history "
                "every time, exactly as with OpenAI."
            ),
            tests=[
                *COMPLETION_SHAPE,
                "pm.test('the whole history was accepted', () => {",
                "  const sent = JSON.parse(pm.request.body.raw).messages;",
                "  pm.expect(sent).to.have.lengthOf(4);",
                "  pm.expect(sent.map((m) => m.role)).to.eql(['system', 'user', 'assistant', 'user']);",
                "});",
            ],
        ),
        request(
            "Sampling parameters",
            method="POST",
            path=CHAT,
            payload={
                "model": "{{model_primary}}",
                "messages": [{"role": "user", "content": "Name three cache eviction policies."}],
                "temperature": 0.2,
                "top_p": 0.9,
                "max_completion_tokens": 64,
                "frequency_penalty": 0.1,
                "presence_penalty": 0.1,
                "stop": ["\n\n"],
                "seed": 42,
            },
            description=(
                "Every sampling knob at once, to show the contract carries them "
                "through. An adapter that cannot express one of these **refuses** the "
                "request naming the parameter rather than dropping it silently "
                "(ADR-014, ADR-015) — a dropped `max_completion_tokens` is a bill, not "
                "a warning."
            ),
            tests=[
                *COMPLETION_SHAPE,
                *USAGE_SHAPE,
                "pm.test('respects the output cap it was given', () => {",
                "  const cap = JSON.parse(pm.request.body.raw).max_completion_tokens;",
                "  pm.expect(res.usage.completion_tokens).to.be.at.most(cap);",
                "});",
            ],
        ),
        request(
            "Multiple choices (n=3)",
            method="POST",
            path=CHAT,
            payload={
                "model": "{{model_primary}}",
                "messages": [{"role": "user", "content": "Suggest a name for a load balancer."}],
                "n": 3,
            },
            description="`n` is honoured end to end, including by the mock provider.",
            tests=[
                *COMPLETION_SHAPE,
                "pm.test('three choices, indexed 0..2', () => {",
                "  pm.expect(res.choices).to.have.lengthOf(3);",
                "  pm.expect(res.choices.map((c) => c.index)).to.eql([0, 1, 2]);",
                "});",
            ],
        ),
        request(
            "JSON mode (response_format)",
            method="POST",
            path=CHAT,
            payload={
                "model": "{{model_primary}}",
                "messages": [
                    {"role": "user", "content": 'Return {"status": "ok"} and nothing else.'}
                ],
                "response_format": {"type": "json_object"},
            },
            description=(
                "`response_format: json_object`. The mock provider honours it too, so "
                "this assertion holds with no vendor key."
            ),
            tests=[
                *COMPLETION_SHAPE,
                "pm.test('content parses as JSON', () => {",
                "  const content = res.choices[0].message.content;",
                "  pm.expect(content, 'JSON mode must return content').to.be.a('string');",
                "  pm.expect(() => JSON.parse(content), content).to.not.throw();",
                "});",
            ],
        ),
        request(
            "Tool calling (forced tool_choice)",
            method="POST",
            path=CHAT,
            payload={
                "model": "{{model_primary}}",
                "messages": [{"role": "user", "content": "What is the weather in Lisbon?"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "description": "Current weather for a city.",
                            "parameters": {
                                "type": "object",
                                "properties": {"city": {"type": "string"}},
                                "required": ["city"],
                            },
                        },
                    }
                ],
                "tool_choice": {"type": "function", "function": {"name": "get_weather"}},
            },
            description=(
                "A forced `tool_choice` must come back as a tool call, not prose. The "
                "function schema is validated against JSON Schema by the vendor, not "
                "here — the contract's job is to carry it through unchanged."
            ),
            tests=[
                *COMPLETION_SHAPE,
                "pm.test('answered with a tool call', () => {",
                "  const calls = res.choices[0].message.tool_calls;",
                "  pm.expect(calls, 'tool_calls').to.be.an('array').with.lengthOf.at.least(1);",
                "  pm.expect(calls[0].type).to.eql('function');",
                "  pm.expect(calls[0].function.name).to.eql('get_weather');",
                "});",
                "pm.test(\"finish_reason is 'tool_calls'\", () => {",
                "  pm.expect(res.choices[0].finish_reason).to.eql('tool_calls');",
                "});",
            ],
        ),
        request(
            "Gateway metadata · who actually served it",
            method="POST",
            path=CHAT,
            payload={
                "model": "{{model_secondary}}",
                "messages": [{"role": "user", "content": "Which provider answered this?"}],
            },
            description=(
                "`response.vortex` exists because *who served this* is not answerable "
                "from the call site: with a router or a fallback chain in front, the "
                "provider that was called and the provider that answered are different "
                "names (ADR-020).\n\n"
                "With no routing table the answer is `mock` — the substitution is "
                "logged loudly, because a deployment answering from canned replies is "
                "the worst failure this service could have."
            ),
            tests=[
                *COMPLETION_SHAPE,
                "pm.test('reports the adapter that served the request', () => {",
                "  pm.expect(res.vortex, 'vortex metadata').to.be.an('object');",
                "  pm.expect(res.vortex.provider).to.be.a('string').and.not.empty;",
                "  pm.expect(res.vortex.upstream_model).to.be.a('string').and.not.empty;",
                "});",
                "const expectMock = pm.environment.get('expect_mock') === 'true';",
                "if (expectMock) {",
                "  pm.test(\"no routing table configured, so the provider is 'mock'\", () => {",
                "    pm.expect(res.vortex.provider).to.eql('mock');",
                "  });",
                "} else {",
                "  pm.test('a real vendor adapter answered', () => {",
                "    pm.expect(res.vortex.provider).to.not.eql('mock');",
                "  });",
                "}",
                "console.log('served by: ' + res.vortex.provider + ' / ' + res.vortex.upstream_model);",
            ],
        ),
    ],
)


# ---------------------------------------------------------------------------
# 03 - Streaming
# ---------------------------------------------------------------------------

SSE_PRELUDE = [
    "pm.test('200 OK', () => pm.response.to.have.status(200));",
    "pm.test('served as server-sent events', () => {",
    "  pm.expect(pm.response.headers.get('Content-Type')).to.include('text/event-stream');",
    "});",
    "pm.test('not stored by any intermediary', () => {",
    "  pm.expect(pm.response.headers.get('Cache-Control')).to.include('no-store');",
    "});",
    "const raw = pm.response.text();",
    "pm.test('terminates with the [DONE] sentinel', () => {",
    "  pm.expect(raw.trim().endsWith('data: [DONE]')).to.be.true;",
    "});",
    "// Every event but the sentinel, parsed back into chunk objects.",
    "const chunks = raw",
    "  .split('\\n\\n')",
    "  .map((block) => block.replace(/^data: /, '').trim())",
    "  .filter((payload) => payload && payload !== '[DONE]')",
    "  .map((payload) => JSON.parse(payload));",
    "pm.test('every event is a chat.completion.chunk', () => {",
    "  pm.expect(chunks).to.have.lengthOf.at.least(1);",
    "  chunks.forEach((c) => pm.expect(c.object).to.eql('chat.completion.chunk'));",
    "});",
]

STREAMING = folder(
    "03 · Streaming (ADR-018, ADR-019)",
    "Streams are always paid for. The gateway asks **every** provider for usage "
    "regardless of what the caller requested, and strips the usage chunk back "
    "out when the caller did not ask for it — so the ledger has a number even "
    "when the client never sees one (ADR-018).\n\n"
    "A client that hangs up mid-stream has its cancellation carried into the "
    "provider's own generator, whose `finally` closes the upstream and stops "
    "tokens nobody will read. That request is logged as `abandoned` and still "
    "settled (ADR-019). Postman always reads to the end, so the abandoned path "
    "is the one thing here you cannot demo from this collection — "
    "`bench/streaming.js` is where it is driven.",
    [
        request(
            "Stream · usage withheld (default)",
            method="POST",
            path=CHAT,
            payload={
                "model": "{{model_primary}}",
                "messages": [{"role": "user", "content": "Count from one to eight."}],
                "stream": True,
            },
            description=(
                "`stream: true` with no `stream_options`. The provider was still asked "
                "for usage — the gateway needs it to bill — and the usage chunk was "
                "stripped before the caller saw it. That asymmetry is ADR-018, and the "
                "assertion below is the visible half of it."
            ),
            tests=[
                *SSE_PRELUDE,
                "pm.test('content arrives as incremental deltas', () => {",
                "  const text = chunks",
                "    .map((c) => (c.choices[0] && c.choices[0].delta.content) || '')",
                "    .join('');",
                "  pm.expect(text.length, 'reassembled content').to.be.above(0);",
                "});",
                "pm.test('usage was NOT forwarded (caller did not ask)', () => {",
                "  const withUsage = chunks.filter((c) => c.usage);",
                "  pm.expect(withUsage, 'chunks carrying usage').to.have.lengthOf(0);",
                "});",
                "pm.test('exactly one chunk carries a finish_reason', () => {",
                "  const finished = chunks.filter(",
                "    (c) => c.choices[0] && c.choices[0].finish_reason",
                "  );",
                "  pm.expect(finished).to.have.lengthOf(1);",
                "});",
            ],
        ),
        request(
            "Stream · usage requested (stream_options)",
            method="POST",
            path=CHAT,
            payload={
                "model": "{{model_primary}}",
                "messages": [{"role": "user", "content": "Count from one to eight."}],
                "stream": True,
                "stream_options": {"include_usage": True},
            },
            description=(
                "The same request with `stream_options.include_usage: true`. Nothing "
                "changes upstream — usage was always requested — only whether the "
                "chunk is forwarded."
            ),
            tests=[
                *SSE_PRELUDE,
                "pm.test('usage IS forwarded when asked for', () => {",
                "  const withUsage = chunks.filter((c) => c.usage);",
                "  pm.expect(withUsage, 'chunks carrying usage').to.have.lengthOf(1);",
                "});",
                "pm.test('the usage chunk adds up', () => {",
                "  const u = chunks.filter((c) => c.usage)[0].usage;",
                "  pm.expect(u.total_tokens).to.eql(u.prompt_tokens + u.completion_tokens);",
                "});",
            ],
        ),
    ],
)


# ---------------------------------------------------------------------------
# 04 - Exact response cache
# ---------------------------------------------------------------------------

# Gates every assertion in the folder, so the same requests pass against a
# laptop with no Redis (no header at all) and against the compose stack.
CACHE_GATE = [
    "const cacheOn = pm.environment.get('cache_enabled') === 'true';",
    "// has() first: Postman's headers.get() answers `undefined` for a header",
    "// that is absent, which is not the same claim as 'the cache said BYPASS'.",
    "const served = pm.response.headers.has('x-cache');",
    "const outcome = served ? pm.response.headers.get('x-cache') : null;",
    "if (!cacheOn) {",
    "  pm.test('cache is off, so no X-Cache header is published', () => {",
    "    pm.expect(served, 'X-Cache present').to.be.false;",
    "  });",
    "}",
]

CACHE_PROMPT = "Summarise the CAP theorem. [run {{cache_nonce}}]"

EXACT_CACHE = folder(
    "04 · Exact response cache (ADR-004)",
    "Keyed on a SHA-256 of the **validated** request dumped with sorted keys, "
    "minus the four fields that cannot change a generated token — `stream`, "
    "`stream_options`, `user`, `metadata` — and namespaced by `key_id`, so one "
    "tenant's completion is never served to another.\n\n"
    "A hit is settled at **zero tokens**, not at the cached response's usage: "
    "those tokens were bought once, and recording them twice would put spend "
    "in the ledger against an invoice line that does not exist.\n\n"
    "Run these **in order** — request 1 seeds the entry that 2, 3 and 4 are "
    "about. A fresh nonce is generated per run, so `MISS` means missed rather "
    "than *ran twice*.",
    [
        request(
            "1 · MISS · first time this exact request is seen",
            method="POST",
            path=CHAT,
            prerequest=NONCE_PRELUDE,
            payload={
                "model": "{{model_primary}}",
                "messages": [{"role": "user", "content": CACHE_PROMPT}],
                "temperature": 0,
                "seed": 7,
            },
            description=(
                "Seeds the entry. The nonce in the prompt is regenerated on every run "
                "of this request, which is what keeps the assertion honest on the "
                "second run."
            ),
            tests=[
                "pm.test('200 OK', () => pm.response.to.have.status(200));",
                *CACHE_GATE,
                "if (cacheOn) {",
                "  pm.test('X-Cache: MISS', () => pm.expect(outcome).to.eql('MISS'));",
                "  pm.test('a real completion was generated', () => {",
                "    pm.expect(pm.response.json().usage.completion_tokens).to.be.above(0);",
                "  });",
                "}",
                "pm.collectionVariables.set('last_completion_id', pm.response.json().id);",
            ],
        ),
        request(
            "2 · HIT · byte-identical request",
            method="POST",
            path=CHAT,
            payload={
                "model": "{{model_primary}}",
                "messages": [{"role": "user", "content": CACHE_PROMPT}],
                "temperature": 0,
                "seed": 7,
            },
            description=(
                "The same body as request 1, including the same nonce. Served from "
                "Redis without touching a provider — and the completion id is the one "
                "generated before, which is the proof it is the same response rather "
                "than a similar one."
            ),
            tests=[
                "pm.test('200 OK', () => pm.response.to.have.status(200));",
                *CACHE_GATE,
                "if (cacheOn) {",
                "  pm.test('X-Cache: HIT', () => pm.expect(outcome).to.eql('HIT'));",
                "  pm.test('the identical response came back, id and all', () => {",
                "    pm.expect(pm.response.json().id).to.eql(",
                "      pm.collectionVariables.get('last_completion_id')",
                "    );",
                "  });",
                "}",
            ],
        ),
        request(
            "3 · HIT · non-semantic fields do not change the key",
            method="POST",
            path=CHAT,
            payload={
                "model": "{{model_primary}}",
                "messages": [{"role": "user", "content": CACHE_PROMPT}],
                "temperature": 0,
                "seed": 7,
                "user": "a-different-end-user",
                "metadata": {"tenant": "acme", "ticket": "SUP-421"},
            },
            description=(
                "Same prompt, different `user` and `metadata`. Both are in "
                "`cache.NON_SEMANTIC_FIELDS`: neither can change a generated token, so "
                "neither is allowed to change the cache key. Still a HIT.\n\n"
                "This is the request that shows the key is canonical rather than a "
                "hash of the raw bytes."
            ),
            tests=[
                "pm.test('200 OK', () => pm.response.to.have.status(200));",
                *CACHE_GATE,
                "if (cacheOn) {",
                "  pm.test('still a HIT despite user and metadata differing', () => {",
                "    pm.expect(outcome).to.eql('HIT');",
                "  });",
                "  pm.test('same cached completion', () => {",
                "    pm.expect(pm.response.json().id).to.eql(",
                "      pm.collectionVariables.get('last_completion_id')",
                "    );",
                "  });",
                "}",
            ],
        ),
        request(
            "4 · BYPASS · X-Vortex-Cache-Bypass",
            method="POST",
            path=CHAT,
            headers=[{"key": "X-Vortex-Cache-Bypass", "value": "true"}],
            payload={
                "model": "{{model_primary}}",
                "messages": [{"role": "user", "content": CACHE_PROMPT}],
                "temperature": 0,
                "seed": 7,
            },
            description=(
                "The debugging escape hatch: skips the lookup **and** the store, so an "
                "answer the cache cannot have touched comes back and the entry already "
                "there is left alone.\n\n"
                "A bypass is a decision about the request, not about one tier — which "
                "is why the semantic tier is not consulted either."
            ),
            tests=[
                "pm.test('200 OK', () => pm.response.to.have.status(200));",
                *CACHE_GATE,
                "if (cacheOn) {",
                "  pm.test('X-Cache: BYPASS', () => pm.expect(outcome).to.eql('BYPASS'));",
                "  pm.test('a freshly generated response, not the cached one', () => {",
                "    pm.expect(pm.response.json().id).to.not.eql(",
                "      pm.collectionVariables.get('last_completion_id')",
                "    );",
                "  });",
                "  pm.test('the bypass suppresses the semantic tier too', () => {",
                "    if (!pm.response.headers.has('x-semantic-cache')) { return; }",
                "    pm.expect(pm.response.headers.get('x-semantic-cache')).to.eql('BYPASS');",
                "  });",
                "}",
            ],
        ),
        request(
            "5 · BYPASS · streams are never cached",
            method="POST",
            path=CHAT,
            payload={
                "model": "{{model_primary}}",
                "messages": [{"role": "user", "content": CACHE_PROMPT}],
                "temperature": 0,
                "seed": 7,
                "stream": True,
            },
            description=(
                "Streaming requests bypass the cache in **both** directions: never "
                "served from it, never stored into it.\n\n"
                "Note `stream` is itself a non-semantic field, so this body hashes to "
                "the same key as request 1 — the bypass is a rule about streaming, not "
                "a different key."
            ),
            tests=[
                "pm.test('200 OK', () => pm.response.to.have.status(200));",
                "pm.test('answered as a stream', () => {",
                "  pm.expect(pm.response.headers.get('Content-Type')).to.include('text/event-stream');",
                "});",
                *CACHE_GATE,
                "if (cacheOn) {",
                "  pm.test('X-Cache: BYPASS for a stream', () => {",
                "    pm.expect(outcome).to.eql('BYPASS');",
                "  });",
                "}",
            ],
        ),
    ],
)


# ---------------------------------------------------------------------------
# 05 - Semantic cache
# ---------------------------------------------------------------------------

SEMANTIC_TIER_GATE = [
    "const semanticOn = pm.environment.get('semantic_cache_enabled') === 'true';",
    "const hasTier = pm.response.headers.has('x-semantic-cache');",
    "const tier = hasTier ? pm.response.headers.get('x-semantic-cache') : null;",
    "const hasScore = pm.response.headers.has('x-semantic-cache-score');",
    "const score = hasScore ? pm.response.headers.get('x-semantic-cache-score') : null;",
    "if (!semanticOn) {",
    "  pm.test('tier is off, so it publishes no header', () => {",
    "    pm.expect(hasTier, 'X-Semantic-Cache present').to.be.false;",
    "  });",
    "}",
]

SEMANTIC_CACHE = folder(
    "05 · Semantic cache (ADR-005, ADR-024, ADR-025)",
    "The second tier, consulted **only** when the exact tier looked and missed "
    "— never when it bypassed, because a bypass is a decision about the "
    "request, not about one tier. The pipeline is embed → cosine against the "
    "stored unit vectors → threshold → hit or miss, in numpy, per worker "
    "process.\n\n"
    "Only `messages` is embedded. Everything else in the request is hashed into "
    "the namespace the vector is searched in, so the threshold is about wording "
    "alone — which is why the two requests below differ **only** in phrasing. A "
    "semantic hit is promoted into the exact tier, so the next identical "
    "request costs a hash and not an embedding.\n\n"
    "**Shipped off, deliberately.** ADR-005 records that with the two embedders "
    "measured so far, no threshold admits paraphrases without also admitting "
    "one-token near misses. `X-Semantic-Cache-Score` is published on misses "
    "too, because that distribution is how the threshold eventually gets tuned.\n\n"
    "To turn it on locally, against the `nomic-embed-text` already pulled in "
    "Ollama — the *Local + semantic cache* environment carries this:\n\n"
    "```\nVORTEX_SEMANTIC_CACHE_ENABLED=true \\\n"
    "VORTEX_EMBEDDING_MODEL=nomic-embed-text \\\n"
    "VORTEX_EMBEDDING_BASE_URL=http://localhost:11434\n```\n\n"
    "With the tier off both requests still pass: they assert the *absence* of "
    "the headers, which is the honest claim.",
    [
        request(
            "1 · Seed the index",
            method="POST",
            path=CHAT,
            prerequest=NONCE_PRELUDE,
            payload={
                "model": "{{model_primary}}",
                "messages": [
                    {"role": "user", "content": "Explain the CAP theorem. [run {{cache_nonce}}]"}
                ],
                "temperature": 0,
            },
            description=(
                "Stores one vector, so request 2 has something to be scored against.\n\n"
                "On a **cold index** this reports `MISS` with no score at all, and that "
                "is correct rather than a gap: there is no nearest neighbour to report a "
                "distance to.\n\n"
                "On a **second run of the collection** it often reports `HIT` instead — "
                "and that is ADR-005 in one line. The `[run <nonce>]` suffix changes on "
                "every run, so these are different bytes and the exact tier misses; but "
                "a nonce is a few characters against a whole sentence, so the embedding "
                "barely moves and the pair still clears 0.95. **A one-token difference "
                "that still hits is precisely the failure mode that keeps this tier off "
                "by default.** Both outcomes are asserted, because forcing a `MISS` here "
                "would be hiding the finding."
            ),
            tests=[
                "pm.test('200 OK', () => pm.response.to.have.status(200));",
                *SEMANTIC_TIER_GATE,
                "if (semanticOn) {",
                "  pm.test('the tier was consulted', () => {",
                "    pm.expect(tier).to.be.oneOf(['HIT', 'MISS']);",
                "  });",
                "  pm.test('a cold index reports no score; a warm one reports a number', () => {",
                "    if (!hasScore) { pm.expect(tier, 'no score means nothing to compare').to.eql('MISS'); }",
                "    else { pm.expect(Number(score)).to.be.within(-1, 1); }",
                "  });",
                "  if (tier === 'HIT') {",
                "    console.log(",
                "      'ADR-005, live: a changed nonce still scored ' + score + ' and hit. ' +",
                "      'One token of difference does not move the embedding enough.'",
                "    );",
                "  } else {",
                "    console.log('cold index seeded; nearest score: ' + (score || 'none'));",
                "  }",
                "}",
            ],
        ),
        request(
            "2 · Paraphrase · nearest score",
            method="POST",
            path=CHAT,
            payload={
                "model": "{{model_primary}}",
                "messages": [
                    {
                        "role": "user",
                        "content": "Could you lay out what the CAP theorem says? [run {{cache_nonce}}]",
                    }
                ],
                "temperature": 0,
            },
            description=(
                "A reworded version of request 1, with every other field identical so "
                "both land in the same namespace. The exact tier misses — different "
                "bytes — and the semantic tier reports how close the two prompts landed.\n\n"
                "Whether this is a `HIT` depends on `VORTEX_SEMANTIC_CACHE_THRESHOLD` "
                "(0.95 by default). The **score is the demo**: it is published on a "
                "miss too, and that distribution is the evidence ADR-005 is waiting on."
            ),
            tests=[
                "pm.test('200 OK', () => pm.response.to.have.status(200));",
                *SEMANTIC_TIER_GATE,
                "if (semanticOn) {",
                "  pm.test('reports its outcome', () => {",
                "    pm.expect(tier).to.be.oneOf(['HIT', 'MISS']);",
                "  });",
                "  pm.test('publishes the nearest score now the index is seeded', () => {",
                "    pm.expect(hasScore, 'X-Semantic-Cache-Score present').to.be.true;",
                "    pm.expect(Number(score), 'cosine similarity').to.be.within(-1, 1);",
                "  });",
                "  pm.test('a HIT only above the configured threshold', () => {",
                "    if (tier === 'HIT') { pm.expect(Number(score)).to.be.at.least(0.9); }",
                "  });",
                "  console.log('paraphrase scored ' + score + ' -> ' + tier);",
                "}",
            ],
        ),
    ],
)


# ---------------------------------------------------------------------------
# 06 - Rate limits and the usage ledger
# ---------------------------------------------------------------------------

METERING = folder(
    "06 · Rate limits & usage ledger (ADR-021, ADR-022)",
    "A per-key RPM/TPM token bucket in **one Lua script**, because a request "
    "refused by the bucket must not have already spent a request. The "
    "`X-RateLimit-*` sextet rides on every response, not just a rejection — a "
    "client that only learns its allowance by exceeding it can only back off "
    "after being throttled.\n\n"
    "The ledger stores integer token counts and prices them at **read time**, "
    "so a corrected price corrects the history. Both halves hang off one Redis "
    "connection and are absent together under `VORTEX_METERING_ENABLED`, and "
    "both fail **open**.",
    [
        request(
            "Rate limit headers · X-RateLimit-*",
            method="POST",
            path=CHAT,
            payload={
                "model": "{{model_tertiary}}",
                "messages": [{"role": "user", "content": "One short sentence about Redis."}],
            },
            description=(
                "The sextet is published per unit — requests and tokens — and only for "
                "a unit that is actually configured. Zero means unlimited, and an "
                "unlimited key never touches Redis at all, which is what lets a laptop "
                "run with no Redis.\n\n"
                "To drive this to a 429 for the demo, mint a deliberately tiny key:\n"
                "`uv run vortex-keys create --name demo-429 --rpm 2 --tpm 500`"
            ),
            tests=[
                "pm.test('200 OK or 429 when the bucket is empty', () => {",
                "  pm.expect(pm.response.code).to.be.oneOf([200, 429]);",
                "});",
                "const meteringOn = pm.environment.get('metering_enabled') === 'true';",
                "const hasLimit = pm.response.headers.has('x-ratelimit-limit-requests');",
                "const limit = hasLimit ? pm.response.headers.get('x-ratelimit-limit-requests') : null;",
                "if (!meteringOn) {",
                "  pm.test('metering off, so no limiter headers', () => {",
                "    pm.expect(hasLimit, 'X-RateLimit-Limit-Requests present').to.be.false;",
                "  });",
                "} else {",
                "  pm.test('publishes the request bucket', () => {",
                "    pm.expect(hasLimit, 'X-RateLimit-Limit-Requests present').to.be.true;",
                "    pm.expect(pm.response.headers.has('x-ratelimit-remaining-requests')).to.be.true;",
                "    pm.expect(pm.response.headers.has('x-ratelimit-reset-requests')).to.be.true;",
                "  });",
                "  pm.test('remaining is below the limit, and never negative', () => {",
                "    const max = Number(limit);",
                "    const left = Number(pm.response.headers.get('x-ratelimit-remaining-requests'));",
                "    pm.expect(left).to.be.at.least(0);",
                "    pm.expect(left).to.be.below(max);",
                "  });",
                "  if (pm.response.code === 429) {",
                "    pm.test('a rejection carries Retry-After, never zero', () => {",
                "      const after = Number(pm.response.headers.get('Retry-After'));",
                "      pm.expect(after).to.be.at.least(1);",
                "    });",
                "  }",
                "}",
            ],
        ),
        request(
            "Usage report · GET /v1/usage",
            path="/v1/usage",
            description=(
                "Only ever the caller's own usage. There is no `key` parameter, "
                "deliberately: a report that can name another key is an authorisation "
                "system, and this gateway does not have one — every key is equal, so "
                "the only safe scope is *yours*.\n\n"
                "With no ledger configured this answers an **empty report** rather than "
                "a 404, so a client can be written once."
            ),
            tests=[
                "pm.test('200 OK', () => pm.response.to.have.status(200));",
                "const report = pm.response.json();",
                "pm.test(\"object is 'usage.report'\", () => {",
                "  pm.expect(report.object).to.eql('usage.report');",
                "});",
                "pm.test('scoped to the calling key and nothing else', () => {",
                "  pm.expect(report.key_id).to.be.a('string').and.not.empty;",
                "  pm.expect(report, 'no key parameter exists, so no other key can appear')",
                "    .to.not.have.property('keys');",
                "});",
                "pm.test('reports a date window', () => {",
                "  pm.expect(report.start_date).to.match(/^\\d{4}-\\d{2}-\\d{2}$/);",
                "  pm.expect(report.end_date).to.match(/^\\d{4}-\\d{2}-\\d{2}$/);",
                "});",
                "pm.test('totals are non-negative integers', () => {",
                "  pm.expect(report.total_requests).to.be.at.least(0);",
                "  pm.expect(report.total_tokens).to.be.at.least(0);",
                "});",
                "const meteringOn = pm.environment.get('metering_enabled') === 'true';",
                "if (meteringOn) {",
                "  pm.test('the requests made above are in the ledger', () => {",
                "    pm.expect(report.total_requests, 'this run already spent requests').to.be.above(0);",
                "  });",
                "  pm.test('daily breakdown is present', () => {",
                "    pm.expect(report.daily).to.be.an('array').with.lengthOf.at.least(1);",
                "  });",
                "  console.log(",
                "    'ledger: ' + report.total_requests + ' requests, ' +",
                "    report.total_tokens + ' tokens, $' + (report.total_cost_usd || 0)",
                "  );",
                "} else {",
                "  pm.test('no ledger, so an empty report rather than a 404', () => {",
                "    pm.expect(report.total_requests).to.eql(0);",
                "    pm.expect(report.daily).to.be.an('array').that.is.empty;",
                "  });",
                "}",
            ],
        ),
        request(
            "Usage report · narrowed window",
            path="/v1/usage",
            query=[{"key": "days", "value": "1"}],
            description=(
                "`days` is bounded 1..365. The ledger keeps per-day token counts for "
                "`VORTEX_USAGE_RETENTION_DAYS`, so that retention is also how far back "
                "this endpoint can see."
            ),
            tests=[
                "pm.test('200 OK', () => pm.response.to.have.status(200));",
                "pm.test('window is one day wide', () => {",
                "  const report = pm.response.json();",
                "  pm.expect(report.start_date).to.eql(report.end_date);",
                "});",
            ],
        ),
        request(
            "400 · days out of range",
            path="/v1/usage",
            query=[{"key": "days", "value": "0"}],
            description=(
                "`days` is declared `ge=1, le=365`. FastAPI's own 422 carrying "
                "pydantic's raw error list is translated into a **400** plus the OpenAI "
                "envelope, because an OpenAI client understands neither of the former."
            ),
            tests=[
                "pm.test('400, not FastAPIs default 422', () => {",
                "  pm.response.to.have.status(400);",
                "});",
                "const err = pm.response.json().error;",
                "pm.test(\"type is 'invalid_request_error'\", () => {",
                "  pm.expect(err.type).to.eql('invalid_request_error');",
                "});",
                "pm.test('names the offending parameter', () => {",
                "  pm.expect(err.param, 'param').to.include('days');",
                "});",
            ],
        ),
    ],
)


# ---------------------------------------------------------------------------
# 07 - Errors and contract strictness
# ---------------------------------------------------------------------------

ENVELOPE = [
    "const err = pm.response.json().error;",
    "pm.test('single OpenAI error envelope', () => {",
    "  pm.expect(pm.response.json()).to.have.property('error');",
    "  pm.expect(err.message).to.be.a('string').and.not.empty;",
    "});",
]

ERRORS = folder(
    "07 · Errors & contract strictness (ADR-010, ADR-015)",
    "Every failure comes back as one envelope — "
    '`{"error": {message, type, param, code}}` — whether it originated here '
    "(a malformed request) or upstream (a provider outage), so an OpenAI "
    "client's existing error handling keeps working.\n\n"
    "`param` is what makes it useful: it points at the offending field by path, "
    "which is the difference between *something in your request is wrong* and "
    "`messages[2].content`.\n\n"
    "Two status mappings are deliberate refusals to pass the upstream through: "
    "a vendor's 401 means **our** key is wrong, so reporting 401 would tell the "
    "caller to fix a key that is perfectly good — it is a 502. And an upstream "
    "timeout is a gateway timeout, 504, not a 500.",
    [
        request(
            "400 · unknown field (a typo)",
            method="POST",
            path=CHAT,
            payload={
                "model": "{{model_primary}}",
                "messages": [{"role": "user", "content": "Hello."}],
                "temperture": 0.7,
            },
            description=(
                '`temperture` instead of `temperature`. `extra="forbid"` is the '
                "load-bearing setting on every contract model: this is rejected with a "
                "message naming the key, instead of being silently accepted with the "
                "default — which would be a request that ran at a temperature the "
                "caller did not choose.\n\n"
                "The cost, taken deliberately, is that a parameter OpenAI adds tomorrow "
                "is a 400 here until it is declared."
            ),
            tests=[
                "pm.test('400 Bad Request', () => pm.response.to.have.status(400));",
                *ENVELOPE,
                "pm.test(\"type is 'invalid_request_error'\", () => {",
                "  pm.expect(err.type).to.eql('invalid_request_error');",
                "});",
                "pm.test('names the misspelled field, so it is fixable', () => {",
                "  pm.expect(err.param).to.eql('temperture');",
                "});",
                "pm.test(\"code is 'extra_forbidden'\", () => {",
                "  pm.expect(err.code).to.eql('extra_forbidden');",
                "});",
            ],
        ),
        request(
            "400 · empty messages array",
            method="POST",
            path=CHAT,
            payload={"model": "{{model_primary}}", "messages": []},
            description="`messages` is declared with `min_length=1`. There is no completion to generate from nothing.",
            tests=[
                "pm.test('400 Bad Request', () => pm.response.to.have.status(400));",
                *ENVELOPE,
                "pm.test(\"param points at 'messages'\", () => {",
                "  pm.expect(err.param).to.eql('messages');",
                "});",
            ],
        ),
        request(
            "400 · temperature out of range",
            method="POST",
            path=CHAT,
            payload={
                "model": "{{model_primary}}",
                "messages": [{"role": "user", "content": "Hello."}],
                "temperature": 3.0,
            },
            description="`temperature` is bounded 0.0..2.0, matching OpenAI's own range.",
            tests=[
                "pm.test('400 Bad Request', () => pm.response.to.have.status(400));",
                *ENVELOPE,
                "pm.test(\"param points at 'temperature'\", () => {",
                "  pm.expect(err.param).to.eql('temperature');",
                "});",
            ],
        ),
        request(
            "400 · several bad fields, one round trip",
            method="POST",
            path=CHAT,
            payload={
                "messages": [{"role": "user", "content": "Hello."}],
                "temperature": 9,
                "n": 0,
            },
            description=(
                "No `model`, a `temperature` out of range and an `n` below its minimum. "
                "**Every** failure is reported in the message — a request with three "
                "bad fields should take one round trip to fix, not three — while "
                "`param` and `code` describe the first, since the envelope has room "
                "for only one of each."
            ),
            tests=[
                "pm.test('400 Bad Request', () => pm.response.to.have.status(400));",
                *ENVELOPE,
                "pm.test('all three failures are reported at once', () => {",
                "  pm.expect(err.message).to.include('model');",
                "  pm.expect(err.message).to.include('temperature');",
                "  pm.expect(err.message).to.include('n');",
                "});",
                "pm.test('param describes the first of them', () => {",
                "  pm.expect(err.param).to.be.a('string').and.not.empty;",
                "});",
            ],
        ),
        request(
            "401 · wrong API key",
            method="POST",
            path=CHAT,
            noauth=True,
            headers=[{"key": "Authorization", "value": "Bearer vtx_deadbeef_not-a-real-secret"}],
            payload=SIMPLE_PROMPT,
            description=(
                "A well-formed key that no store knows. Rejected **only** where a key "
                "store or `VORTEX_API_KEYS` is configured — in development mode any "
                "well-formed key is accepted by design, and this answers 200.\n\n"
                "Either way the gateway logs the public `key_id`, which is the half an "
                "operator needs and the half that is not a secret."
            ),
            tests=[
                "pm.test('401 where keys are enforced, 200 in development mode', () => {",
                "  pm.expect(pm.response.code).to.be.oneOf([200, 401]);",
                "});",
                "if (pm.response.code === 401) {",
                "  pm.test(\"code is 'invalid_api_key'\", () => {",
                "    pm.expect(pm.response.json().error.code).to.eql('invalid_api_key');",
                "  });",
                "  pm.test('says nothing about *why* the key failed', () => {",
                "    const message = pm.response.json().error.message;",
                "    pm.expect(message).to.not.match(/revoked|unknown|expired/i);",
                "  });",
                "} else {",
                "  console.log('development mode: any well-formed key is accepted (ADR-003)');",
                "}",
            ],
        ),
        request(
            "404 · model nothing routes to",
            method="POST",
            path=CHAT,
            payload={
                "model": "{{unroutable_model}}",
                "messages": [{"role": "user", "content": "Hello."}],
            },
            description=(
                "`VORTEX_DEFAULT_PROVIDER` is empty by default, which means *reject a "
                "model that matches no rule*. That is the safer default: a typo'd model "
                "name is a 404 rather than a surprise bill on whichever provider "
                "happened to be listed first.\n\n"
                "With **no** routing table at all the gateway is on its mock, which "
                "answers to any model name — so this is a 200 on the demo stack and a "
                "404 wherever `VORTEX_MODEL_ROUTES` is set. Both are asserted, because "
                "pretending otherwise is a test that has to be disabled before a demo."
            ),
            tests=[
                "const routed = pm.environment.get('routes_configured') === 'true';",
                "if (routed) {",
                "  pm.test('404 Not Found', () => pm.response.to.have.status(404));",
                "  pm.test(\"type is 'invalid_request_error'\", () => {",
                "    pm.expect(pm.response.json().error.type).to.eql('invalid_request_error');",
                "  });",
                "} else {",
                "  pm.test('no routing table, so the mock answers anything', () => {",
                "    pm.response.to.have.status(200);",
                "    pm.expect(pm.response.json().vortex.provider).to.eql('mock');",
                "  });",
                "  console.log('set VORTEX_MODEL_ROUTES to make this a 404 (ADR-016, ADR-017)');",
                "}",
            ],
        ),
    ],
)


# ---------------------------------------------------------------------------
# Collection assembly
# ---------------------------------------------------------------------------

COLLECTION_PREREQUEST = [
    "// Fail loudly and once, rather than thirty times with a connection error.",
    "['base_url', 'api_key'].forEach((name) => {",
    "  const value = pm.environment.get(name) || pm.collectionVariables.get(name);",
    "  if (!value) {",
    "    throw new Error(",
    "      name + ' is not set. Pick an environment (top right) before running: ' +",
    "      'Local, Docker Demo, Staging or Production.'",
    "    );",
    "  }",
    "});",
]

COLLECTION_TEST = [
    "// Runs after every request in the collection.",
    "// RequestIDMiddleware is the outermost middleware, so there is no response",
    "// it does not reach -- error envelopes and streams included (ADR-008).",
    "pm.test('[all] response carries X-Request-Id', () => {",
    "  pm.expect(pm.response.headers.has('x-request-id'), 'x-request-id').to.be.true;",
    "});",
    "pm.test('[all] no unhandled server error', () => {",
    "  pm.expect(pm.response.code, 'a 500 means an exception escaped a handler')",
    "    .to.not.eql(500);",
    "});",
]

DESCRIPTION = """# Vortex AI Gateway

An OpenAI-compatible gateway that routes, limits, caches, retries and observes
calls to OpenAI, Anthropic and Ollama from one process. Point any OpenAI SDK at
it by changing the base URL.

**Everything here runs with no vendor keys and no bill.** With no routing table
configured the gateway serves canned replies from its built-in mock provider,
which is what makes the whole collection exercisable end to end on a laptop.

## Pick an environment first

| Environment | Backed by | Metering | Exact cache |
|---|---|---|---|
| **Local (mock, no Redis)** | `uv run uvicorn vortex_ai_gateway.gateway:app --reload` | off | off |
| **Docker Demo (dev)** | `make demo` | on | on |
| **Staging** | your staging host | on | on |
| **Production** | your production host | on | on |

The assertions are gated on `metering_enabled`, `cache_enabled`,
`semantic_cache_enabled` and `routes_configured`, so the same collection passes
green against all four rather than needing requests disabled before a demo.

## Run order

Folders are numbered because folder **04** depends on its own order — request 1
seeds the cache entry that 2, 3 and 4 are about. Run the collection top to
bottom (Runner, or `postman collection run`) and folder 06's ledger assertions
will have traffic to report.

## Endpoints

| Method | Path | Auth |
|---|---|---|
| GET | `/healthz` | open |
| GET | `/readyz` | open |
| GET | `/metrics` | open (ADR-028) |
| POST | `/v1/chat/completions` | Bearer |
| GET | `/v1/usage` | Bearer |

## What this collection cannot show you

* **Abandoned streams** (ADR-019) — Postman always reads to the end. Driven by
  `bench/streaming.js`.
* **Retries, the circuit breaker and fallback chains** (ADR-001, ADR-002,
  ADR-020) — they need a provider that fails. Driven by
  `bench/provider-failure.js`.
* **A 429** — the demo key is 600 rpm. Mint a small one:
  `uv run vortex-keys create --name demo-429 --rpm 2 --tpm 500`.

Generated by `postman/build_collection.py`. Edit that, not this.
"""


def collection() -> dict[str, Any]:
    """The whole collection, in Postman Collection Format v2.1."""
    return {
        "info": {
            "_postman_id": "9b1f7c42-5ad8-4e7a-9d31-vortexgateway01",
            "name": "Vortex AI Gateway",
            "description": DESCRIPTION,
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "auth": {
            "type": "bearer",
            "bearer": [{"key": "token", "value": "{{api_key}}", "type": "string"}],
        },
        "event": [
            script(COLLECTION_PREREQUEST, kind="prerequest"),
            script(COLLECTION_TEST),
        ],
        "variable": [
            {"key": key, "value": value, "type": "string"}
            for key, value in COLLECTION_VARIABLES.items()
        ],
        "item": [
            HEALTH,
            AUTH,
            CHAT_COMPLETIONS,
            STREAMING,
            EXACT_CACHE,
            SEMANTIC_CACHE,
            METERING,
            ERRORS,
        ],
    }


# ---------------------------------------------------------------------------
# curl cookbook
# ---------------------------------------------------------------------------

CURL_HEADER = """# Vortex AI Gateway — curl cookbook

Every request in the Postman collection, as a curl one-liner. Generated from
the same definitions by `postman/build_collection.py`, so the two cannot drift.

Set the two variables once:

```bash
export BASE_URL=http://localhost:8000
export API_KEY=vtx-demo-key
```

`-i` is on the requests whose **headers** are the point — `X-Cache`,
`X-Semantic-Cache`, `X-RateLimit-*`. Those headers are the demo; the body is
not.

Add `| jq .` to anything you want pretty-printed.
"""

# Requests whose response headers carry the answer, so curl needs -i to show it.
HEADERS_MATTER = ("04 ·", "05 ·", "Rate limit headers")


def curl_for(item: dict[str, Any], folder_name: str) -> str:
    """One request as a copy-pasteable curl invocation."""
    spec = item["request"]
    method = spec["method"]
    path = "/" + "/".join(spec["url"]["path"])
    query = spec["url"].get("query") or []
    if query:
        path += "?" + "&".join(f"{q['key']}={q['value']}" for q in query)

    show_headers = folder_name.startswith(HEADERS_MATTER) or item["name"].startswith(HEADERS_MATTER)
    flags = "-sS -i" if show_headers else "-sS"
    lines = [f'curl {flags} -X {method} "$BASE_URL{path}"']

    # Collection-level bearer auth, unless the request opted out of it.
    if spec.get("auth", {}).get("type") != "noauth":
        lines.append('  -H "Authorization: Bearer $API_KEY"')
    for header in spec.get("header", []):
        value = header["value"].replace("{{api_key}}", "$API_KEY")
        lines.append(f'  -H "{header["key"]}: {value}"')

    if "body" in spec:
        payload = spec["body"]["raw"]
        # A fixed nonce, not a per-run one: the cache folder's point is that the
        # *same* body hits, so a curl reader needs to be able to paste request 1
        # twice and watch MISS become HIT.
        payload = payload.replace("{{cache_nonce}}", "curl-demo")
        for name, default in COLLECTION_VARIABLES.items():
            payload = payload.replace("{{" + name + "}}", default or name)
        lines.append("  -d '" + payload + "'")

    return " \\\n".join(lines)


def curl_cookbook() -> str:
    """The whole collection rendered as a shell cookbook."""
    out = [CURL_HEADER]
    for group in collection()["item"]:
        out.append(f"\n## {group['name']}\n")
        for item in group["item"]:
            out.append(f"### {item['name']}\n")
            out.append("```bash")
            out.append(curl_for(item, group["name"]))
            out.append("```\n")
    return "\n".join(out)


def main() -> None:
    """Write the collection and every environment, and report what landed."""
    collection_path = HERE / "vortex-ai-gateway.postman_collection.json"
    collection_path.write_text(json.dumps(collection(), indent=2) + "\n")

    requests = sum(len(group["item"]) for group in collection()["item"])
    print(f"{collection_path.relative_to(HERE.parent)}  ({requests} requests)")

    curl_path = HERE / "CURL.md"
    curl_path.write_text(curl_cookbook())
    print(f"{curl_path.relative_to(HERE.parent)}  ({requests} curl commands)")

    env_dir = HERE / "environments"
    env_dir.mkdir(exist_ok=True)
    for spec in ENVIRONMENTS:
        path = env_dir / spec["file"]
        path.write_text(json.dumps(environment(spec), indent=2) + "\n")
        print(f"{path.relative_to(HERE.parent)}  -- {spec['doc']}")


if __name__ == "__main__":
    main()
