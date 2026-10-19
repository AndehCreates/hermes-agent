"""Oracle advisor transport plugin entry point.

This plugin targets Hermes' public ``PluginContext.register_platform`` seam.
It is intentionally kept outside ``gateway/platforms`` and does not modify the
generic webhook adapter.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def check_requirements() -> bool:
    """Passive dependency check; aiohttp is already a Hermes gateway dependency."""
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        return False
    return True


def validate_config(config) -> bool:
    """Require explicit route binding and a scoped Standard Webhooks secret."""
    from .adapter import validate_config as _validate_config

    return _validate_config(config)


def is_connected(config) -> bool:
    """Use Hermes' profile-aware env check for status and setup surfaces."""
    from gateway.platforms._shared import env_is_connected

    return env_is_connected("ORACLE_ADVISOR_WEBHOOK_SECRET")(config)


def register(ctx) -> None:
    """Register the opt-in inbound transport through Hermes' platform plugin API."""
    from .adapter import OracleAdvisorAdapter

    ctx.register_platform(
        name="oracle_advisor",
        label="Oracle Advisor",
        adapter_factory=OracleAdvisorAdapter,
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["ORACLE_ADVISOR_WEBHOOK_SECRET"],
        install_hint="Uses Hermes' bundled aiohttp dependency",
        allow_update_command=False,
        trusted_inbound=False,
        emoji="\U0001f9ed",
    )
