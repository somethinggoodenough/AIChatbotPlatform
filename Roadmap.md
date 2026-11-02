# Roadmap

This is the implementation plan for the AI Chatbot Serving Platform (working title): an LLM serving and agent runtime system. Local models run on SGLang for now; a self-written engine will replace SGLang later behind the same interface. External LLM APIs sit next to the local models as first-class backends. A Discord chatbot is the first workload.

## Principles

**Offline implementation run (2026-11-02):** Phase commits distinguish implemented code from real-system acceptance. See [offline evidence](docs/reports/offline-acceptance.md). GPU/provider/Discord-dependent items remain unchecked until measured on those systems; mock results do not satisfy them.

- **Every phase ends in a shippable state.** The README is current and every benchmark number can be reproduced with one command. The project can stop at any phase boundary and still be complete.
- **Benchmark from day one.** Each new layer reports what it costs (latency, tokens, money) and what it buys.
- **Acceptance criteria come first.** Each phase lists its criteria up front. A phase is done when they pass, not when the code exists.
- **Infra over app features.** Adapters and agent features stay minimal. Most of the effort goes into the runtime, the gateway, and the serving layer.

## Decisions made

| Area | Choice | Why |
| --- | --- | --- |
| Inference engine (now) | SGLang | Prefix caching (RadixAttention), structured outputs, request abort, and an OpenAI-compatible server out of the box. Runs on RTX 40-series cards |
| Inference engine (later) | Own engine behind the same backend interface | Benchmarked against SGLang with the same harness on this project's workloads |
| Model strategy | Heterogeneous. Local small models handle frequent judgment calls; external APIs handle user-facing replies and heavy Worker tasks. Each call type is assigned a model in config | Cheap and fast where calls are frequent and short; high quality where users read the output |
| Benchmark mode | An all-local mode that sends every call type to local models | Reproducible numbers with no API cost and no provider variance |
| Gateway | Built in-house. SGLang Model Gateway is a baseline to compare against, not a dependency | Our gateway is agent-aware (call type, task, step, journal, fallback across backends). A generic model gateway is not |
| Positioning | General platform behind an OpenAI-compatible API; the chatbot is the first workload | Any client can use it; the platform is not a chatbot backend |
| Agent architecture | Two tiers: a long-lived Router and ephemeral Workers. The Router has no tools | Delegation is enforced by the architecture, not left to the model. The component that reads raw user input cannot call tools |
| Agent fault tolerance | Durable execution: journal every step, replay on recovery | Agent paths are non-deterministic, so recovery cannot mean "run it again" |
| Process model | Stateless processes; all state lives in a state store | A crash is just a restart |
| First channel | Discord | Official bot API, open to individual developers |
| QQ | Later, official bot platform only | Unofficial protocol bridges risk account bans and violate QQ's terms |
| Hardware | One RTX 4060 (8 GB) | Runs the small local models (0.5B–3B) and an embedding model. Heavy generation goes to APIs. Mock replicas cover scale tests |
| Language | Python 3.11+, asyncio (default, see open decisions) | Fastest path; SGLang and most LLM SDKs are Python |
| State store | SQLite in WAL mode first; Postgres later if needed | Zero ops to start |

### Default model assignment

| Call type | Default backend | Why |
| --- | --- | --- |
| Event filter, second stage | Local small model | Runs on many messages, so it must be cheap |
| Input classifier, output check | Local small model | Runs on every request, so it must be fast; data stays local |
| Router decision | Local small model with structured output (a JSON action schema) | Frequent and short, and it must always parse |
| Router reply (user-facing text) | External API | Users read this output directly |
| Worker steps | External API (can be switched to local) | Multi-step reasoning and tool use |
| RAG embeddings | Local embedding model | Cheap and frequent |

---

## Phase 0: Serving baseline and model backends

**Goal:** local and API models behind one interface, plus a benchmark harness that every later phase reuses.

**Build**

- [ ] SGLang serving a small local model on the 4060 (Linux or WSL2)
- [ ] Verify the SGLang features later phases depend on: prefix cache hits, request abort on client disconnect, and structured output with a JSON schema
- [x] `ModelBackend` interface with two implementations: local (SGLang through its OpenAI-compatible API) and one external API provider. Both report streaming, cancellation, and token usage the same way (contract tested offline; real serving acceptance pending)
- [x] Model assignment per call type, loaded from config (see the table above)
- [x] `/metrics` endpoint (Prometheus format): TTFT, inter-chunk latency, tokens, known/unknown cost, and errors per backend (true ITL needs token-level timestamps)
- [x] Load generator: concurrency sweeps, configurable length distributions, shared-prefix workloads, and a judgment-style workload (short prompt, long shared prefix, short structured output)
- [x] Results saved as JSON plus plots; one script reproduces them (mock baseline; real-model baseline pending)

**Acceptance criteria**

- The same client code runs against both the local and the API backend, streaming and non-streaming
- A baseline table for the local models: TTFT p50/p99, ITL p50/p99, throughput, and structured-output latency for a request the size of a Router decision
- API call latency is split into gateway overhead and provider time
- A client disconnect aborts generation on both backends (on the API side, the upstream stream is closed)

---

## Phase 1: Agent vertical slice (Discord → Router → Worker → models)

**Goal:** one end-to-end path working in a private Discord server, with the Router/Worker split in place from the start.

**Build**

- [x] `ChannelAdapter` interface and a unified `Message` schema (user, channel, thread, text, attachments, `mentions_bot`, `reply_to`)
- [ ] Discord adapter (`discord.py`): receives messages and posts replies. Long replies stream through throttled message edits, because Discord has no native token streaming
- [x] Router: a single long-lived loop that owns the persona, the global context, and the task table. Each turn makes up to two calls, and neither carries tools:
  - a **decision**: local model, structured output with an action of `reply`, `spawn`, `forward_to_worker`, or `ignore`
  - a **reply**: API model, generating the text the user sees
- [x] Worker: an isolated context that receives a task spec plus acceptance criteria. It has tools (RAG lookup over a small document set, plus one or two toy tools), returns a report and artifacts, and never posts to Discord itself
- [x] Thread subscriptions: follow-ups in a thread tied to a task go straight to that task's Worker (fake-channel tested)
- [x] Rule-based event filter: ignore bots, channels not on the allowlist, and messages not addressed to the bot
- [x] Per-user rate limit (a simple token bucket)
- [x] SQLite state store for tasks, threads, subscriptions, and sessions
- [x] Tracing (OpenTelemetry): one trace per user request, spanning the Router decision, the reply model call, the Worker and tools, and each model call with its backend and cost

**Acceptance criteria**

- The Router answers chit-chat on its own. A request that needs a tool produces Worker spawn → report → Router reply
- Router decisions always parse, verified on a few hundred test messages
- The Router has no code path that executes tools. This is enforced in code and covered by a test
- A follow-up in the same thread reaches the subscribed Worker without a new Router routing decision
- Every request in the test server produces a complete trace
- The all-local mode runs the same flow end to end

> The bot stays in a private test server until the Phase 4 guardrails land.

---

## Phase 2: Durable agent runtime

**Goal:** a Worker or Router crash at any point loses no work, duplicates no side effects, and never pays twice for the same API call.

**Build**

- [x] Step journal: every Worker model call and tool call is recorded as a step `{task_id, step_index, kind, backend, input, output, status, idempotency_key}`, written before its result is used; Router decisions use a durable inbox
- [x] Replay: on restart, completed steps return their journaled outputs and execution resumes at the first incomplete step. Unknown model outcomes require verified idempotency or reconciliation
- [x] Tool side-effect classes `read_only`, `idempotent`, and `side_effecting`. Side-effecting tools must accept an idempotency key (`task_id:step_index`)
- [x] Compensation: each side-effecting step records its compensating action in the journal. Rollback runs current-round actions in reverse order, preserving earlier completed rounds
- [x] Two recovery modes:
  - **Infra failure** (crash, timeout, provider error): replay, then resume
  - **Logic failure** (invalid tool call, failed acceptance check): re-plan from the last checkpoint with the error in context. A plain replay would only repeat the mistake
- [x] Stateless Router: durable inbox, decisions, batch membership, tasks and subscriptions survive restart; ambiguous channel delivery is explicit
- [x] Watchdog: heartbeats detect crashed Workers; progress checks detect stuck or looping ones (no new steps, or the same step repeated)
- [x] Crash-injection harness: `kill -9` at each step boundary (before the call; after the call but before the journal write; after the journal write), with lease + fencing checked at the state/effect sink

**Acceptance criteria**

- At every crash point in the harness, the task completes and a side-effecting mock tool applies its effect exactly once, as checked against its idempotency log
- Restarting the Router under load loses no tasks or subscriptions
- The Watchdog catches an injected hung tool and an injected loop within a configured deadline, and the user gets a report
- Reported metrics: recovery time, steps replayed, tokens re-prefilled on resume, and API tokens and cost avoided by replay

---

## Phase 3: Multi-backend routing and failover

**Goal:** the serving layer survives failures of both local replicas and API providers, and routes each call to the right backend efficiently.

**Build**

- [ ] Several local replicas (SGLang instances plus mock replicas) and at least two API providers behind the gateway
- [x] Mock replica that simulates TTFT, decode timing, and failures, so routing and failover can be tested with more replicas than one GPU can hold
- [ ] Prefix-aware routing across local replicas, benchmarked against SGLang Model Gateway's `cache_aware` policy as the baseline
- [x] Per-backend timeouts, explicit safe-retry budgets (exponential backoff plus jitter), and circuit breakers; handling for provider 429s and rate limits (no retries after output; no universal billing idempotency claim)
- [x] Fallback across backends: primary → secondary → local → configured canned/busy reply, with separate fixed prompt templates (mock-tested; real prompt tuning pending)
- [x] A stable prompt layout (fixed content first, variable content last), with simulated prefix locality tests
- [x] Cost accounting per task and per user, including unknown attempts
- [x] Router event batching: events that arrive within a time window are coalesced into one Router decision
- [x] Fault injection under load: close a mock replica; simulate timeout, 429, 5xx and dropped connections (real replica test pending)

**Acceptance criteria**

- During an API provider outage under load, tasks still complete through fallback. The p99 latency delta and the fallback rate are reported
- Killing a local replica under load causes no user-visible failures. The p99 TTFT delta is reported
- Our prefix-aware routing compared with the `cache_aware` baseline: prefix cache hit rate and TTFT
- Router batching: per-decision latency and the number of Router calls, with and without batching
- Resume after losing a replica: re-prefill cost, with and without routing to a replica that still holds the prefix

---

## Phase 4: Guardrails and abuse resistance

**Goal:** safe enough to run in a public server, with the guardrails kept off the critical path.

**Build**

- [x] Input normalization (NFKC, common homoglyphs, zero-width characters, bounded percent/HTML/base64 decoding; not exhaustive)
- [ ] Input classifier (topic and injection, local model) running in parallel with the main model call. On reject, generation is cancelled and a fixed reply goes out. For API calls, the upstream request is aborted to stop further spend
- [x] Streaming output check with a small buffer, tiered by risk, plus a canary token in the system prompt (deterministic leak checks; real semantic safety pending)
- [x] Untrusted content isolation inside Workers: RAG and tool results are delimited and marked as data
- [x] Fail-open/fail-closed policy per component, set in config: safety checks fail closed, the topic classifier fails open
- [x] Abuse economics: cap max output tokens and turns; reserve spend per user and deny paid/unknown-price routes without a verified cost bound
- [ ] Optional: a model-based second stage for the event filter, which decides whether an unaddressed group message should reach the Router
- [ ] Red-team set covering role nesting, instruction override, encoding tricks, multi-turn crescendo, and indirect injection through RAG documents. It is scored by an LLM-as-judge with a written rubric, calibrated on a small hand-labeled sample

**Acceptance criteria**

- Guardrail overhead on p50/p99 TTFT is measured and reported. Target: the classifier is never on the critical path
- The red-team pass rate is reported per category
- With the classifier killed, topic checks fall back to rules and safety checks block, as verified by tests

---

## Phase 5: Ablation study and write-up

**Goal:** reproduce DeepSeek's Super-agent scheduling ablation at small scale on this platform, and write it up.

**Build**

- [ ] Synthetic Discord message stream mixing chit-chat, work requests, follow-ups, and stop requests. Real work is replaced by a time-consuming placeholder tool, so the results measure scheduling rather than model ability
- [ ] Configurations:
  - (a) Router/Worker
  - (b) Router/Worker without thread passthrough
  - (c) Router/Worker without Router batching
  - (d) single session with a subagent tool, neutral prompt
  - (e) single session with a subagent tool, delegate-first prompt
- [ ] Run (d) and (e) with both a local model and an API model, because how often a model delegates depends on how strong it is
- [ ] At least 5 runs per configuration

**Outputs**

- Task completion time p50/p95, number of Router decisions, prefix cache read tokens, total tokens and cost per task, and the delegation rate for (d) and (e)
- A write-up in `docs/` that states the caveats: small local models and a synthetic workload

---

## Later / stretch

- **Own inference engine** as a drop-in `ModelBackend`, benchmarked against SGLang on this project's workloads. The judgment-style workload (short prompts, long shared prefixes, short structured outputs) is the natural first target
- Batch-invariant kernels in the own engine, for deterministic decoding
- Multimodal input (images posted in Discord) through a local VLM, if the open decision below says yes
- QQ adapter through the official QQ bot platform
- Prefill/decode disaggregation: KV cache transfer between nodes, and failure in the middle of a transfer
- Distributed rate limiter across gateway replicas
- Postgres state store
- Rewrite the gateway in Go if Python turns out to be the bottleneck (measure first)
- Autoscaling: weight-loading cold starts, backpressure

## Open decisions

- **API providers:** which provider writes replies and runs Workers, and which one is the secondary for fallback
- **Image input:** should the bot also accept images posted in Discord (multimodal), on top of the multi-model setup?
- **Local models:** which Router/guardrail model and which embedding model fit in 8 GB together with useful KV cache headroom
- **OS for the 4060 machine:** native Linux or WSL2. SGLang runs on Linux, and WSL2 works
- **Language:** Python asyncio everywhere (the current default), or Go for the gateway from the start?
- **Repo name**
- **Friend's bot:** whether to also contribute a bounded module (for example, the gateway) to an existing bot with real users
