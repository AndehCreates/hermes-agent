# Oracle Advisor Hermes platform plugin

This source-only plugin implements the pinned Oracle advisor Standard
Webhooks parser behind Hermes' `PluginContext.register_platform()` extension
seam. It is designed for the current upstream plugin platform API at
`f4ac0bea1ea50bba62886fb0b7e308e4edee8fd3`.

The Hermes fork head targeted by this task (`161606642d76c76701857172fdb2307ce0fa50ba`)
does not yet contain that extension seam, so this checkout cannot load or
activate the plugin. The fork must adopt upstream's generic platform plugin
support first. No Oracle-specific change to Hermes core is included.

When used with a compatible Hermes build, configuration requires an explicit
owner, subscription ID, host, and port. The current and optional previous
Standard Webhooks secrets are read from Hermes' profile-scoped secret API as
`ORACLE_ADVISOR_WEBHOOK_SECRET` and
`ORACLE_ADVISOR_WEBHOOK_PREVIOUS_SECRET`. There are no default routes or
credentials.

Valid advisor requests are validated and then fail closed with HTTP 501:
dispatch and result submission are unsupported until Oracle's authoritative
effect, Work admission, and result receipt contracts can be bound durably.
See [`docs/oracle-advisor-webhook.md`](../../../docs/oracle-advisor-webhook.md)
for source pins, wire limits, evidence boundaries, and unresolved requirements.
