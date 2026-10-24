# ADR-035: Staging and prod refuse to start without a key source
Date / Status: 2026-10-24 / accepted
Context: With neither `VORTEX_KEY_DB_PATH` nor `VORTEX_API_KEYS` set, the gateway
accepts any well-formed key (ADR-013, ADR-003). That is right for a laptop and
an open proxy to paid providers anywhere else, and `VORTEX_ENVIRONMENT` — the
one setting that says which — was only a metric label and a log field. ADR-013's
update recorded this as the check the repo still owed.
Options: A) refuse to start in staging and prod, from `create_app` B) the same
for prod alone C) a `Settings` validator D) a loud warning at startup.
Decision: A. `ensure_key_source` runs first in the factory and raises
`OpenGatewayError` before tracing, Redis or a provider is touched. Staging is
included because it holds real provider credentials as prod does, and an open
staging gateway spends them for whoever finds it; B leaves that hole. C would
also fire inside `vortex-keys`, which builds `Settings` and is the tool that
fixes the problem. D is the status quo with more words: a warning on a
gateway that is already serving is read after the bill. There is no override
flag: a deployment that wants to be open says so with `VORTEX_ENVIRONMENT=dev`.
Consequences: A misconfigured staging or prod deploy crash-loops at boot with a
message naming both variables, instead of serving. Serving the mock provider
in prod is not refused here — a wrong answer, not an open door. Revisit if an
environment other than these four is added, since the guard names the ones it
guards rather than the ones it lets through.
