# Oracle advisor transport plugin

This is a source-only platform plugin. It leaves the generic webhook adapter
unchanged and does not add a route to Hermes defaults. It has not been
activated, registered with Oracle, or given credentials.

## Integration boundary and compatibility

The adapter belongs outside Hermes core. Current canonical Hermes upstream
(`NousResearch/hermes-agent` at `f4ac0bea1ea50bba62886fb0b7e308e4edee8fd3`)
provides the supported community adapter seam:

- [`PluginContext.register_platform()`](https://github.com/NousResearch/hermes-agent/blob/f4ac0bea1ea50bba62886fb0b7e308e4edee8fd3/hermes_cli/plugins.py)
  registers a plugin-owned adapter factory.
- [`gateway.platform_registry`](https://github.com/NousResearch/hermes-agent/blob/f4ac0bea1ea50bba62886fb0b7e308e4edee8fd3/gateway/platform_registry.py)
  owns generic adapter discovery and creation.
- The [platform adapter guide](https://github.com/NousResearch/hermes-agent/blob/f4ac0bea1ea50bba62886fb0b7e308e4edee8fd3/gateway/platforms/ADDING_A_PLATFORM.md)
  recommends the plugin path for third-party adapters and says it requires no
  core edits.

The Hermes fork target is still at
`161606642d76c76701857172fdb2307ce0fa50ba`. That source has no
`gateway/platform_registry.py`, no `PluginContext.register_platform()`, and no
dynamic plugin platform names. The plugin therefore cannot be loaded by that
exact fork head. The missing seam is already present upstream; the fork must
first adopt that upstream platform-plugin support. Do not add an
Oracle-specific branch to `gateway/platforms/webhook.py` to bridge the gap.

The changes formerly made in `gateway/platforms/webhook.py` and its generic
gateway test directory were removed. The protocol parser and transport now
live under `plugins/platforms/oracle_advisor/`; there is no Hermes-core patch
to carry or later remove. Once the fork includes the pinned upstream plugin
seam, this adapter can remain an independently disabled plugin.

The implementation follows the upstream platform plugin pattern at the
upstream commit above. The fork at the older Hermes base cannot provide a
runtime integration test for that registry yet. The plugin registration test
checks the public `register_platform` call shape; it does not claim that the
old fork can load the plugin.

## Pinned producer and Oracle boundaries

- Producer contract: [Oracle draft PR #1520 at
  `d0b3debebb081df7bdb2145078c0683f5e6136d7`](https://github.com/AndehCreates/oracle-dev-gateway/blob/d0b3debebb081df7bdb2145078c0683f5e6136d7/src/advisor-mcp-events.ts).
  It is unmerged and not advertised by the connected runtime.
- Oracle effect/admission boundaries are pinned to
  [`af089f2c147fdcd2329dec06e88b9c83f1805229`](https://github.com/AndehCreates/oracle-dev-gateway/tree/af089f2c147fdcd2329dec06e88b9c83f1805229):
  `management-store.ts` owns effect identity and pending/completed/unknown
  transitions; `mesh-control.ts` owns admitted turn start and reconciliation;
  `work-envelope.ts` owns outcome/writeback receipt identity and ambiguity
  reconciliation. This plugin does not replace those records or transitions.

## Wire contract

The transport verifies the raw request bytes using `webhook-id`,
`webhook-timestamp`, `webhook-signature`, and `X-MCP-Subscription-Id`. The
signature is `v1,<base64 HMAC-SHA256>` over
`eventId.timestamp.rawBody`, using a canonical base64-decoded `whsec_` key.
It accepts current and one previous secret for key rotation and enforces the
producer's inclusive five-minute timestamp bound. Secrets are read through
Hermes' scoped secret API, never from `os.environ` in adapter code. No secret
has been provisioned for this task.

The signed verification body is exactly
`{"type":"verification","challenge":"..."}`. A valid challenge with the
producer's challenge and message-ID shapes receives HTTP 200 JSON echoing the
challenge. It does not dispatch an agent or create a Work effect.

The event envelope is strictly limited to `eventId`, `name`, `timestamp`,
`data`, and `cursor`, with `name: "advisor.requested"`. The request data
contains `requestId`, `originAgent`, `originSession`, optional `workId`,
`question`, and `evidenceRefs`. Validation follows the pinned Zod limits:

- request ID: 8–128 ASCII characters matching
  `[A-Za-z0-9][A-Za-z0-9._:-]*`;
- origin agent, origin session, and optional Work ID: trimmed strings, 1–160
  JavaScript UTF-16 code units, no controls;
- question: trimmed, 1–2,000 UTF-16 code units; tab, newline, and carriage
  return are permitted;
- evidence refs: at most 16 trimmed strings, 1–512 UTF-16 code units, with
  controls and the producer's credential patterns rejected;
- timestamp: UTC ISO string with exactly millisecond precision; cursor: a
  positive decimal sequence bounded by JavaScript's safe integer limit.

The receiver body ceiling is 256 KiB, matching the pinned producer. The event
ID must match both `webhook-id` and the producer's owner-derived request ID.
The configured `X-MCP-Subscription-Id` binding must match exactly. Standard
Webhooks does not sign that subscription header, and this source-only plugin
cannot establish key uniqueness across profiles or processes. Reusing one
key across bindings therefore cannot safely authenticate profile identity;
the adapter does not dispatch or persist advice, and production use requires
unique per-binding keys plus a signed owner/profile binding. The plugin
preserves `requestId` as
`correlationId` and preserves optional `workId` in its parsed request object.
It does not create an effect ID, delivery-attempt ID, receipt ID, or result
causation link.

The pinned advisor schema has no recipient role, role-profile ID/version,
explicit origin incarnation/sequence, effect ID, or attempt ID. The adapter
does not infer these from the question, agent name, session name, or other
payload text, and does not select skills, context, tools, or a harness role.
Graph-native role profiles and harness selection belong in their own
evidence-backed contracts. Future selection must use actual cost and quality
outcomes; token volume alone is not a quality measure. Inference-provider
contracts remain separate from harness-runner contracts. Future OpenCode or
Ollama Cloud adapters are outside this change.

## Fail-closed behavior and unresolved runtime requirements

A valid request returns HTTP 501 with `stage: "received"`,
`dispatch: "unsupported"`, and `result_submission: "unsupported"`. This is
not a durable receipt or claim. The plugin does not call `handle_message`,
start a harness, write an answer, or close Work. Its send method reports
result submission as unsupported. A 2xx challenge response only proves the
webhook verification handshake.

No persistent request ledger, duplicate/conflict protection, durable outbox,
partition recovery, unknown-outcome reconciliation, result writer, or
authoritative result readback is implemented. Repeated, conflicting,
concurrent, and post-restart valid requests remain unclaimed and return 501.
The plugin does not claim production durability, independent closure
verification, or ChatGPT conversation wake. The wire contract has no
profile/version field for multiplex-profile correlation.

Production integration still needs explicit per-origin incarnation/sequence,
role and profile/version binding, durable stage evidence for received,
claimed, admitted, running, result-persisted, and independently verified
closure, same-identity reconciliation for unknown productive outcomes, and
correlated result readback/writeback. OMP/Pi runtime support is outside this
slice.

## Checks

The focused command for the generic webhook behavior and plugin protocol is:

```bash
python -m pytest tests/gateway/test_webhook_adapter.py tests/gateway/test_webhook_dynamic_routes.py tests/gateway/test_webhook_integration.py tests/plugins/platforms/test_oracle_advisor.py -q
```

The current saved environment could not install pytest because package access
failed through the configured proxy. `compileall` and an aiohttp HTTP smoke
check passed against the final plugin layout. The smoke check covered a signed
challenge, a valid event returning 501, invalid signature and subscription
binding, timestamp bounds, plugin registration shape, and the unchanged generic
webhook 202/dispatch path. The exact focused pytest suite remains unrun because
pytest is unavailable in the saved environment.
