"""Shared access checks for Slack slash commands.

The command surface must always be limited to the configured score channel.
Setting ``ADMIN_USER_IDS`` to a comma-separated set of Slack user IDs further
restricts commands to those users. An empty set intentionally preserves the
existing friends-group behavior: any member who can invoke the command in the
score channel may use it. Deployments that need admin-only operations should
configure this allowlist.
"""

from typing import Any, Mapping, Optional


CHANNEL_DENIED_MESSAGE = "This command is only available in the configured score channel."
USER_DENIED_MESSAGE = "You are not authorized to use this command."


def command_access_error(body: Mapping[str, Any], bot_module: Any) -> Optional[str]:
    """Return a safe denial message unless the command is allowed to proceed."""
    configured_channel = str(getattr(bot_module, "SCORE_CHANNEL_ID", "") or "").strip()
    invoking_channel = str(body.get("channel_id") or "").strip()
    if not configured_channel or invoking_channel != configured_channel:
        return CHANNEL_DENIED_MESSAGE

    admin_user_ids = frozenset(
        str(user_id).strip()
        for user_id in (getattr(bot_module, "ADMIN_USER_IDS", ()) or ())
        if str(user_id).strip()
    )
    user_id = str(body.get("user_id") or "").strip()
    if admin_user_ids and user_id not in admin_user_ids:
        return USER_DENIED_MESSAGE

    return None
