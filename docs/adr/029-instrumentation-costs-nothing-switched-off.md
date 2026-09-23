# ADR-029: Instrumentation is unconditional, so `span()` is what makes it free
Date / Status: 2026-09-23 / accepted

Context: ADR-026 put spans at the seams unconditionally, on the stated grounds
that the OpenTelemetry *API* is a no-op until an SDK provider is installed and
therefore costs nothing switched off. That claim was never measured. It is
false: `NoOpTracer.start_as_current_span` is a `@contextmanager`, `use_span`
inside it is another, and `tracing.span` wrapped both in a third — so four spans
per request is twelve generator context managers built, entered and unwound on a
gateway with tracing disabled, which is every default deployment. Measured on the
buffered mock path with a `/metrics` registry live and no Redis: **158.5 →
131.1 µs per request, 17%, 6307 → 7649 req/s in one process** (`bench/`,
docs/notes/day-13.md).

Options: A) Accept it — 17% is the price of being able to turn tracing on.
B) Guard every call site with `if settings.tracing_enabled:`, which is a branch
and a settings reference at six seams and puts the switch back into the modules
ADR-026 removed it from. C) Sample at zero when disabled — an SDK provider with
`ParentBased(TraceIdRatioBased(0))` still allocates a span context per span, so
it is cheaper than a no-op tracer and nowhere near free. D) Short-circuit inside
`span()`: with no provider installed, return one shared, stateless context
manager that yields `trace.INVALID_SPAN`.

Decision: D. The cost of the seam belongs to the module that owns the seam's
API, not to its six callers, and the test is `_provider is None` — the module's
own global, which `configure_tracing` sets at most once per process and never
back, so this flips once at boot and is a single `is None` afterwards. It yields
`INVALID_SPAN` rather than a bespoke stub because that is already a real `Span`
whose `is_recording()` is `False` and whose setters are no-ops, so no call site
needs a guard it did not already have and `trace_context()` keeps returning
nothing for the reason it always did. It deliberately does not attach the span
to the current context: nothing reads it, and attaching is most of what the real
path costs.

Consequences: The claim in ADR-026 is now true, and it is true because of code
rather than by assumption — `config.py` and `AGENTS.md` are corrected to say so,
because "a no-op tracer is free" is the kind of plausible sentence that gets
repeated. Two tests pin the optimisation rather than the behaviour: one asserts
`span()` returns the *same object* twice, one passes an attribute mapping that
raises if it is walked. A third asserts `__exit__` returns `False`, since a
no-op that swallowed a provider failure would turn a 502 into a 200. The cost
is a second code path through `span()` that only runs when tracing is off,
which is the path nobody watches — hence the test that a provider being
installed puts recording back. Revisit if OpenTelemetry's no-op tracer becomes
genuinely free, or if anything starts depending on a disabled span being present
in the context.
