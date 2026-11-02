# Agent runtime (offline first)

`Message` and `ChannelAdapter` are channel-independent contracts. `FakeChannel`
records sends and throttled edits in memory. `DiscordAdapter` uses the same
stream-to-edits function, imports `discord.py` only when used, and disables
automatic mentions in outgoing messages. Real Discord acceptance remains skipped.

`AgentRuntime` applies the allowlist, bot/address filter and per-user token bucket,
then checks a persisted thread subscription. Subscribed follow-ups go directly to
their Worker. Other messages enter one serialized Router decision loop.

The Router owns only a gateway and a store. It generates a validated JSON decision
or streams a reply. It never imports the tool registry or Worker, and never receives
an execution callback. The coordinator starts Workers and passes their reports back
to the Router. Workers have isolated task context, lexical local-document lookup,
addition and a delay placeholder; they have no channel object.

SQLite WAL stores messages, replies, task specifications, acceptance criteria,
reports and thread subscriptions. These are application state, not logs. No message
content or credentials are written to logs.

## Phase 2: ownership and recovery

A Worker claims a task in a short `BEGIN IMMEDIATE` transaction. The claim contains
an owner, lease expiry and monotonically increasing fencing token. Heartbeats renew
the lease; journal writes, task completion and the built-in effect sink all check
the owner, token and expiry. A paused old worker cannot write after a newer claim.
Leases do not themselves fence an arbitrary external service: additional tools
must implement the same idempotency/fencing contract at their effect destination.

Each model/tool call is stored before execution, with `task_id:step_index` as its
idempotency key. Completed outputs are reused when a Worker resumes. Follow-ups
start new revision rounds with new step indices; old reports are never mistaken
for a new follow-up's answer. Read-only calls may repeat, and effectful calls must
accept a `ToolContext` with the lease and idempotency key. The example counter tool
commits its effect and receipt in one SQLite transaction. Its compensating action
is journaled before execution and rolled back in reverse execution order, using
separate idempotency keys.

If a model response was received but not journaled, a normal provider is marked
`uncertain` on recovery. It is not silently charged again. Only the explicit
`MockIdempotentBackend` uses our mock's verified `Idempotency-Key` replay. The mock
server remains alive during Worker crash tests; its receipts are not durable across
a mock-server restart. No claim of universal exactly-once external execution or
billing is made.

Infra interruption replays the completed steps and resumes the incomplete call
when its replay policy allows it. A progress timeout pauses a task and returns a
failure report; `resume_pending()` can retry it. Unknown model outcomes become
`uncertain` instead. Invalid plans or failed deterministic acceptance checks create
a journaled error checkpoint and a new model call with the error in context.
Repeated identical actions and exhausted step budgets end with a failure report.
The deterministic acceptance forms are `report_contains:TEXT` and `artifact:NAME`.
Free-form natural-language criteria are supplied to the model but are not an
independent proof of correctness.

The Router inbox persists the exact decision request, its parsed decision and the
assigned task. Saved spawn decisions use a stable task id, so Router restarts do
not create duplicate tasks. `resume_pending()` processes persisted messages and
unfinished tasks. A crash during a decision without verified backend idempotency,
or during partially acknowledged channel delivery, is recorded as uncertain.
Discord has no verified end-to-end exactly-once delivery guarantee here.

`AgentRuntime.stop(task_id, user_id=...)` revokes the lease and cancels the running
Worker. `/stop` in an owned subscribed thread uses this path. Only the task owner
can submit follow-ups or stop it. The Router itself still has no tool execution
capability.

Reproduce actual `SIGKILL` injection before calls, after calls/before journal
commit, and after journal commit, for both model and tool steps:

```sh
uv run python -m unittest discover -s tests/fault_injection -p 'test_durable_runtime.py' -v
uv run python benchmarks/harness/recovery.py
```

The second command writes `artifacts/benchmarks/phase2/recovery.json`: recovery
time including lease wait, replayed steps, re-prefilled tokens, model tokens/cost
avoided, effect receipts and generation counts. Mock prices are explicitly zero;
they are not estimated savings for a paid provider. The tests also cover competing
Workers, stale fencing, reverse compensation, hung/looping Workers, Router restart
under load, uncertain outcomes, new follow-up rounds and cancellation.
The harness measures actual simulated re-prefill from server counters, separately
from the Worker's usage-based estimate, because an idempotency replay can return
old usage without performing new inference. Cross-replica/fallback replay remains
uncertain: only a fixed plain Gateway assignment has verified step replay here.

Offline integration tests run real HTTP calls to the configurable mock model server
and publish to the fake channel. A few hundred deterministic Router decisions test
parsing and routing contracts, not real model quality. The fake all-local assignment
tests that the same runtime works with a local backend profile; actual GPU and paid
provider tests stay skipped.

Run the fake conversation with `uv run python -m ai_serving.runtime.demo`.
It prints counts only, never the messages. No `.env` is read in this mode.

When real Discord validation is authorized, install the optional Discord dependency,
enable the bot's Message Content intent, set `DISCORD_BOT_TOKEN` locally, then use:

```sh
uv run --extra discord python -m ai_serving.runtime.demo --discord \
  --profile configs/providers/local_sglang.toml --channel-id YOUR_PRIVATE_CHANNEL_ID
```

The adapter follows the official [send/edit API](https://discordpy.readthedocs.io/en/stable/api.html)
and [gateway intents](https://discordpy.readthedocs.io/en/stable/intents.html).
This command is intentionally not run in offline acceptance.
