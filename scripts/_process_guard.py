"""Refuse to mutate portfolio state while the live Claude bot is running.

Used by every close / migration script. Without this guard, the live bot's
next `save_snapshot()` (every cycle) overwrites whatever the script just
wrote — this is exactly the race that ate the Iran Dec 31 close earlier
on 2026-05-19.

Audit 2026-05-19, CRITICAL #1.
"""
from __future__ import annotations

import subprocess
import sys

# Match the Claude bot's entry point. run_paper.sh changes directory into
# `python/` and then execs `python main.py --console`, so the actual argv
# is literally "python main.py --console" with NO path prefix on main.py.
# The first version of this guard included a "python/" prefix and matched
# nothing -- the smoke test on 2026-05-20 walked straight past it and a
# 74431-PID zombie main.py was discovered later only by chance.
# Require both `main.py` and `--console` in the argv.
_BOT_SIGNATURE = r"main\.py.*--console"


def _own_pid() -> str:
    import os
    return str(os.getpid())


def refuse_if_bot_running(*, signature: str = _BOT_SIGNATURE) -> None:
    """Exit the calling script if the live bot is still alive.

    Prints a clear, copy-pasteable instruction for stopping the bot and
    re-running the script. Returns silently when no live bot is detected.
    """
    try:
        result = subprocess.run(
            ["pgrep", "-f", signature],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except FileNotFoundError:
        print("WARNING: pgrep not found; race guard skipped.", file=sys.stderr)
        return
    except subprocess.TimeoutExpired:
        print("WARNING: pgrep timed out; race guard skipped.", file=sys.stderr)
        return

    # Filter out our own PID in the unlikely case the signature matches.
    own = _own_pid()
    pids = [p for p in result.stdout.split() if p.strip() and p.strip() != own]
    if not pids:
        return

    print("=" * 70)
    print("REFUSED — the live Claude bot is still running.")
    print("=" * 70)
    print(f"Found process(es) matching '{signature}': {', '.join(pids)}")
    print()
    print("Stop the bot first, then re-run this script.")
    print()
    print("To stop the bot, run in your bot's terminal tab:")
    print(f"  kill {' '.join(pids)}")
    print()
    print("To verify the bot is fully down, run:")
    print(f"  ps aux | grep -v grep | grep '{signature}' || echo '(no bot running)'")
    print()
    print("Then re-run this script.")
    sys.exit(2)
