import time


def should_check_stall(
    *,
    now,
    last_sent_at,
    last_stall_at,
    threshold,
    captcha,
    sleep,
    stopped,
):
    """Return whether an account is due for a stall recovery pass."""
    return (
        not stopped
        and not captcha
        and not sleep
        and threshold > 0
        and now - last_sent_at >= threshold
        and now - last_stall_at >= threshold
    )


async def recover_stalled_account(client, now):
    """Log one stalled state, clear pauses, then restart dead command loops."""
    status = client.ch.command_handler_status
    wall_now = time.time()
    owners = []
    for owner in client.ch.pause_owners:
        paused_at = client.ch.paused_at.get(owner, wall_now)
        owners.append(f"{owner}:{max(0.0, wall_now - paused_at):.0f}s")

    pending = [
        command_id
        for command_id, state in client.ch.cmds_state.items()
        if command_id != "global" and state.get("pending_send")
    ]
    checks = [command.get("id", "?") for command in client.ch.checks]
    name = client.username or f"account #{client.account_index}"
    line = (
        f"STALL - {name} - state={'active' if status.state else 'paused'} "
        f"captcha={status.captcha} sleep={status.sleep} "
        f"queue={client.ch.queue.qsize()} pending={pending} checks={checks} "
        f"paused_by={owners}"
    )
    client.last_stall_at = now
    await client.log(line, "#e6e65a")
    await client.ch.resume_stat()

    for cog_name, loop_name in (
        ("Commands", "send_commands"),
        ("Commands", "monitor_checks"),
        ("Looper", "initiate_loop"),
    ):
        cog = client.get_cog(cog_name)
        loop = getattr(cog, loop_name, None) if cog is not None else None
        if loop is None or loop.is_running():
            continue
        try:
            loop.start()
        except Exception as error:
            await client.log(
                f"Warning - unable to restart {cog_name}.{loop_name}: {type(error).__name__}",
                "#e6e65a",
            )
