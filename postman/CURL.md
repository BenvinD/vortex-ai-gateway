# Vortex AI Gateway — curl cookbook

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


## 00 · Health & observability

### Liveness · GET /healthz

```bash
curl -sS -X GET "$BASE_URL/healthz"
```

### Readiness · GET /readyz

```bash
curl -sS -X GET "$BASE_URL/readyz"
```

### Prometheus metrics · GET /metrics

```bash
curl -sS -X GET "$BASE_URL/metrics"
```


## 01 · Authentication (ADR-003, ADR-013)

### 401 · no Authorization header

```bash
curl -sS -X POST "$BASE_URL/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d '{
  "model": "gpt-4o-mini",
  "messages": [
    {
      "role": "user",
      "content": "Say hello in exactly five words."
    }
  ]
}'
```

### 401 · malformed Authorization header

```bash
curl -sS -X POST "$BASE_URL/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Token $API_KEY" \
  -d '{
  "model": "gpt-4o-mini",
  "messages": [
    {
      "role": "user",
      "content": "Say hello in exactly five words."
    }
  ]
}'
```

### 401 · bearer with no token

```bash
curl -sS -X POST "$BASE_URL/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer " \
  -d '{
  "model": "gpt-4o-mini",
  "messages": [
    {
      "role": "user",
      "content": "Say hello in exactly five words."
    }
  ]
}'
```

### 200 · valid bearer key

```bash
curl -sS -X POST "$BASE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
  "model": "gpt-4o-mini",
  "messages": [
    {
      "role": "user",
      "content": "Say hello in exactly five words."
    }
  ]
}'
```


## 02 · Chat completions (ADR-009, ADR-010, ADR-012)

### Basic completion

```bash
curl -sS -X POST "$BASE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
  "model": "gpt-4o-mini",
  "messages": [
    {
      "role": "user",
      "content": "Explain an API gateway in one sentence."
    }
  ]
}'
```

### Multi-turn conversation

```bash
curl -sS -X POST "$BASE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
  "model": "gpt-4o-mini",
  "messages": [
    {
      "role": "system",
      "content": "You are terse. Never exceed one sentence."
    },
    {
      "role": "user",
      "content": "What does a circuit breaker do?"
    },
    {
      "role": "assistant",
      "content": "It stops calling a provider that keeps failing."
    },
    {
      "role": "user",
      "content": "And when does it try again?"
    }
  ]
}'
```

### Sampling parameters

```bash
curl -sS -X POST "$BASE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
  "model": "gpt-4o-mini",
  "messages": [
    {
      "role": "user",
      "content": "Name three cache eviction policies."
    }
  ],
  "temperature": 0.2,
  "top_p": 0.9,
  "max_completion_tokens": 64,
  "frequency_penalty": 0.1,
  "presence_penalty": 0.1,
  "stop": [
    "\n\n"
  ],
  "seed": 42
}'
```

### Multiple choices (n=3)

```bash
curl -sS -X POST "$BASE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
  "model": "gpt-4o-mini",
  "messages": [
    {
      "role": "user",
      "content": "Suggest a name for a load balancer."
    }
  ],
  "n": 3
}'
```

### JSON mode (response_format)

```bash
curl -sS -X POST "$BASE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
  "model": "gpt-4o-mini",
  "messages": [
    {
      "role": "user",
      "content": "Return {\"status\": \"ok\"} and nothing else."
    }
  ],
  "response_format": {
    "type": "json_object"
  }
}'
```

### Tool calling (forced tool_choice)

```bash
curl -sS -X POST "$BASE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
  "model": "gpt-4o-mini",
  "messages": [
    {
      "role": "user",
      "content": "What is the weather in Lisbon?"
    }
  ],
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "get_weather",
        "description": "Current weather for a city.",
        "parameters": {
          "type": "object",
          "properties": {
            "city": {
              "type": "string"
            }
          },
          "required": [
            "city"
          ]
        }
      }
    }
  ],
  "tool_choice": {
    "type": "function",
    "function": {
      "name": "get_weather"
    }
  }
}'
```

### Gateway metadata · who actually served it

```bash
curl -sS -X POST "$BASE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
  "model": "claude-3-5-haiku",
  "messages": [
    {
      "role": "user",
      "content": "Which provider answered this?"
    }
  ]
}'
```


## 03 · Streaming (ADR-018, ADR-019)

### Stream · usage withheld (default)

```bash
curl -sS -X POST "$BASE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
  "model": "gpt-4o-mini",
  "messages": [
    {
      "role": "user",
      "content": "Count from one to eight."
    }
  ],
  "stream": true
}'
```

### Stream · usage requested (stream_options)

```bash
curl -sS -X POST "$BASE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
  "model": "gpt-4o-mini",
  "messages": [
    {
      "role": "user",
      "content": "Count from one to eight."
    }
  ],
  "stream": true,
  "stream_options": {
    "include_usage": true
  }
}'
```


## 04 · Exact response cache (ADR-004)

### 1 · MISS · first time this exact request is seen

```bash
curl -sS -i -X POST "$BASE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
  "model": "gpt-4o-mini",
  "messages": [
    {
      "role": "user",
      "content": "Summarise the CAP theorem. [run curl-demo]"
    }
  ],
  "temperature": 0,
  "seed": 7
}'
```

### 2 · HIT · byte-identical request

```bash
curl -sS -i -X POST "$BASE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
  "model": "gpt-4o-mini",
  "messages": [
    {
      "role": "user",
      "content": "Summarise the CAP theorem. [run curl-demo]"
    }
  ],
  "temperature": 0,
  "seed": 7
}'
```

### 3 · HIT · non-semantic fields do not change the key

```bash
curl -sS -i -X POST "$BASE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
  "model": "gpt-4o-mini",
  "messages": [
    {
      "role": "user",
      "content": "Summarise the CAP theorem. [run curl-demo]"
    }
  ],
  "temperature": 0,
  "seed": 7,
  "user": "a-different-end-user",
  "metadata": {
    "tenant": "acme",
    "ticket": "SUP-421"
  }
}'
```

### 4 · BYPASS · X-Vortex-Cache-Bypass

```bash
curl -sS -i -X POST "$BASE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -H "X-Vortex-Cache-Bypass: true" \
  -d '{
  "model": "gpt-4o-mini",
  "messages": [
    {
      "role": "user",
      "content": "Summarise the CAP theorem. [run curl-demo]"
    }
  ],
  "temperature": 0,
  "seed": 7
}'
```

### 5 · BYPASS · streams are never cached

```bash
curl -sS -i -X POST "$BASE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
  "model": "gpt-4o-mini",
  "messages": [
    {
      "role": "user",
      "content": "Summarise the CAP theorem. [run curl-demo]"
    }
  ],
  "temperature": 0,
  "seed": 7,
  "stream": true
}'
```


## 05 · Semantic cache (ADR-005, ADR-024, ADR-025)

### 1 · Seed the index

```bash
curl -sS -i -X POST "$BASE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
  "model": "gpt-4o-mini",
  "messages": [
    {
      "role": "user",
      "content": "Explain the CAP theorem. [run curl-demo]"
    }
  ],
  "temperature": 0
}'
```

### 2 · Paraphrase · nearest score

```bash
curl -sS -i -X POST "$BASE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
  "model": "gpt-4o-mini",
  "messages": [
    {
      "role": "user",
      "content": "Could you lay out what the CAP theorem says? [run curl-demo]"
    }
  ],
  "temperature": 0
}'
```


## 06 · Rate limits & usage ledger (ADR-021, ADR-022)

### Rate limit headers · X-RateLimit-*

```bash
curl -sS -i -X POST "$BASE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
  "model": "llama3.1-8b",
  "messages": [
    {
      "role": "user",
      "content": "One short sentence about Redis."
    }
  ]
}'
```

### Usage report · GET /v1/usage

```bash
curl -sS -X GET "$BASE_URL/v1/usage" \
  -H "Authorization: Bearer $API_KEY"
```

### Usage report · narrowed window

```bash
curl -sS -X GET "$BASE_URL/v1/usage?days=1" \
  -H "Authorization: Bearer $API_KEY"
```

### 400 · days out of range

```bash
curl -sS -X GET "$BASE_URL/v1/usage?days=0" \
  -H "Authorization: Bearer $API_KEY"
```


## 07 · Errors & contract strictness (ADR-010, ADR-015)

### 400 · unknown field (a typo)

```bash
curl -sS -X POST "$BASE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
  "model": "gpt-4o-mini",
  "messages": [
    {
      "role": "user",
      "content": "Hello."
    }
  ],
  "temperture": 0.7
}'
```

### 400 · empty messages array

```bash
curl -sS -X POST "$BASE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
  "model": "gpt-4o-mini",
  "messages": []
}'
```

### 400 · temperature out of range

```bash
curl -sS -X POST "$BASE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
  "model": "gpt-4o-mini",
  "messages": [
    {
      "role": "user",
      "content": "Hello."
    }
  ],
  "temperature": 3.0
}'
```

### 400 · several bad fields, one round trip

```bash
curl -sS -X POST "$BASE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
  "messages": [
    {
      "role": "user",
      "content": "Hello."
    }
  ],
  "temperature": 9,
  "n": 0
}'
```

### 401 · wrong API key

```bash
curl -sS -X POST "$BASE_URL/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer vtx_deadbeef_not-a-real-secret" \
  -d '{
  "model": "gpt-4o-mini",
  "messages": [
    {
      "role": "user",
      "content": "Say hello in exactly five words."
    }
  ]
}'
```

### 404 · model nothing routes to

```bash
curl -sS -X POST "$BASE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
  "model": "no-such-model-anywhere",
  "messages": [
    {
      "role": "user",
      "content": "Hello."
    }
  ]
}'
```
