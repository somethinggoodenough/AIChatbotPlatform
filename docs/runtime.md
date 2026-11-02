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
content or credentials are written to logs. Phase 1 persists state, but durable
step replay, lease ownership and crash recovery are added in Phase 2.

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
