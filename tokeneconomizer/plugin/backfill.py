"""Learning from sessions that ran before this plugin was ever installed, or before a
repository turned a mechanism on.

`fold.pending_sessions` already folds a session this plugin recorded but never saw end (its
SessionEnd was never hooked -- a killed terminal) at the next SessionStart, through a minimal
record under `sessions/<id>.json`. Backfilling reuses exactly that path for sessions the plugin
never hooked AT ALL: `seed` writes the same kind of record for every session `Host.
historical_sessions` finds straight in the agent's own logs, oldest first, so a session start's
ordinary `fold.pending_sessions` then folds those too, indistinguishable from one a hook would
have written -- in the order they really ran, not as one enormous "session" out of order.

The scan this takes (every project this agent has ever logged, on this whole machine) is cheap
per file but not free in total, so it never runs inside a hook: `controller.session_start` only
checks `pending` (one small read) and, the first time a repository is seen, spawns `te backfill`
detached, the same way `fold` itself is kept off the hook's critical path. That command does the
scan, seeds what it finds, and folds it there and then (see `cli.cmd_backfill`); a crash midway
leaves the seeded, unfolded records as ordinary pending sessions, which the next SessionStart
folds regardless.

This writes no fact and runs no model by itself; it only hands `fold` more to read.
"""
import os
import time

from . import fold


def _seeded_ids(ctx):
    sess_dir = ctx.store.path("sessions")
    try:
        return {n[:-5] for n in os.listdir(sess_dir) if n.endswith(".json")}
    except OSError:
        return set()


def pending(ctx):
    """Whether this repository and host have never been backfilled: a host with nothing to
    backfill (`Host.historical_sessions_supported`), `backfill=off`, or a first backfill
    already run (`backfill.json`), all say no. Cheap enough for a hook to check inline -- the
    scan itself is not (see module docstring)."""
    return (ctx.host.historical_sessions_supported and bool(ctx.config.backfill)
            and not ctx.store.read("backfill.json"))


def seed(ctx, exclude=(), limit=None):
    """Session records written for history `ctx.host.historical_sessions` finds that the plugin
    has not already seeded or folded. Capped at the `limit` most recent ones when given and
    nonzero, kept oldest-first even so.

    Returns (seeded_ids, found) -- the ids actually written, and how many history turned up in
    total, so a caller can report even when every one of them was already known."""
    found = ctx.host.historical_sessions(ctx.repo, ctx.env)
    skip = _seeded_ids(ctx) | set(ctx.store.read("folded.json") or {}) | set(exclude)
    candidates = [row for row in found if row[0] not in skip]
    if limit:
        candidates = candidates[-limit:]
    seeded = []
    for sid, transcript, mtime in candidates:
        ctx.store.write(os.path.join("sessions", f"{sid}.json"), {
            "session_id": sid, "host": ctx.host.name, "started": mtime, "ended": mtime,
            "cwd": ctx.repo.root, "turns": [], "nudges": 0, "aborts": 0,
            "judge_tokens": 0.0, "writer_tokens": 0.0, "transcript": transcript,
            "backfilled": True})
        seeded.append(sid)
    return seeded, len(found)


def mark_done(ctx, found, seeded):
    ctx.store.write("backfill.json", {"at": time.time(), "found": found, "seeded": seeded})


def run(ctx, limit=None):
    """Seed this repository's backfill and fold what it finds, all in this one process: what
    `te backfill` runs, and what a hook runs inline where `TE_FOLD_SYNC=1` keeps everything
    synchronous (tests). The live controller instead spawns `te backfill` detached -- the scan
    this takes is too slow for a hook's own critical path.

    Returns (records, found): the ledger record of each session actually folded (a session
    already folded by something else in the meantime is skipped, as `fold_session` always
    skips one), and how many history turned up in total."""
    seeded, found = seed(ctx, limit=limit)
    mark_done(ctx, found, len(seeded))
    records = [r for r in (fold.fold_session(ctx, sid) for sid in seeded) if r is not None]
    return records, found
