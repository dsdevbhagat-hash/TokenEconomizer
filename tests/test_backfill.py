"""Backfilling a repository's history from the agent's own logs: `Host.historical_sessions`
(Claude Code's, straight off `~/.claude/projects`), and `backfill.seed` / `run`, wired into
`controller.session_start` so a repository's first session also learns from everything that
ran before the plugin was ever installed.
"""
import json
import os
import time

import pytest

from tokeneconomizer.plugin import backfill, fold, hosts
from tokeneconomizer.plugin.context import LiveContext
from tests.test_live_hooks import call, env, repo  # noqa: F401


def _historical(home, project, sid, cwd, age_s=3700, extra_lines=()):
    """A minimal Claude Code transcript under `<home>/projects/<project>/<sid>.jsonl`, old
    enough to count as finished, with one prompt and one tool call -- real enough for
    `trace.claude_*` to read, and carrying `cwd` the way every real entry does."""
    d = os.path.join(home, "projects", project)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{sid}.jsonl")
    lines = [
        {"type": "user", "cwd": cwd, "message": {"role": "user", "content": "look around"}},
        {"type": "assistant", "cwd": cwd, "message": {
            "id": f"msg_{sid}", "model": "claude-opus-5-5", "role": "assistant",
            "usage": {"input_tokens": 10, "cache_read_input_tokens": 100,
                      "cache_creation_input_tokens": 10, "output_tokens": 20},
            "content": [{"type": "tool_use", "id": "tu1", "name": "Bash",
                        "input": {"command": "ls"}}]}},
        {"type": "user", "cwd": cwd, "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu1", "content": "a.py", "is_error": False}]}},
    ]
    lines.extend(extra_lines)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(json.dumps(x) for x in lines) + "\n")
    old = time.time() - age_s
    os.utime(path, (old, old))
    return path


def _home(env):
    return env["CLAUDE_CONFIG_DIR"]


# ---- Host.historical_sessions --------------------------------------------------------------
def test_finds_a_past_session_for_this_repository(env, repo):
    _historical(_home(env), "proj", "h1", repo)
    ctx = LiveContext.build(hosts.ClaudeCode(), repo, env)
    found = ctx.host.historical_sessions(ctx.repo, env)
    assert [sid for sid, _, _ in found] == ["h1"]


def test_leaves_out_a_session_from_a_different_repository(env, repo, tmp_path):
    other = str(tmp_path / "elsewhere")
    os.makedirs(other)
    _historical(_home(env), "proj", "h1", other)
    ctx = LiveContext.build(hosts.ClaudeCode(), repo, env)
    assert ctx.host.historical_sessions(ctx.repo, env) == []


def test_leaves_out_a_session_still_being_written(env, repo):
    _historical(_home(env), "proj", "h1", repo, age_s=5)
    ctx = LiveContext.build(hosts.ClaudeCode(), repo, env)
    assert ctx.host.historical_sessions(ctx.repo, env) == []


def test_oldest_first_across_projects(env, repo):
    _historical(_home(env), "a", "newer", repo, age_s=1000)
    _historical(_home(env), "b", "older", repo, age_s=5000)
    ctx = LiveContext.build(hosts.ClaudeCode(), repo, env)
    assert [sid for sid, _, _ in ctx.host.historical_sessions(ctx.repo, env)] == ["older", "newer"]


def test_a_worktree_of_the_same_remote_matches_too(env, repo, tmp_path):
    """Two checkouts of one repository share a key by remote, not by path (`paths.RepoId`);
    a session logged under the OTHER checkout's path still belongs to this one."""
    other = str(tmp_path / "other-clone")
    os.makedirs(os.path.join(other, ".git"))
    with open(os.path.join(other, ".git", "HEAD"), "w") as fh:
        fh.write("ref: refs/heads/main\n")
    with open(os.path.join(other, ".git", "config"), "w") as fh:
        fh.write('[remote "origin"]\n\turl = https://github.com/acme/widget.git\n')
    _historical(_home(env), "proj", "h1", other)
    ctx = LiveContext.build(hosts.ClaudeCode(), repo, env)
    assert [sid for sid, _, _ in ctx.host.historical_sessions(ctx.repo, env)] == ["h1"]


def test_a_host_with_no_support_finds_nothing(env, repo):
    ctx = LiveContext.build(hosts.Codex(), repo, env)
    assert ctx.host.historical_sessions(ctx.repo, env) == []
    assert ctx.host.historical_sessions_supported is False


# ---- backfill.seed / run -------------------------------------------------------------------
def test_seed_writes_a_pending_session_record(env, repo):
    _historical(_home(env), "proj", "h1", repo)
    ctx = LiveContext.build(hosts.ClaudeCode(), repo, env)
    seeded, found = backfill.seed(ctx)
    assert seeded == ["h1"] and found == 1
    assert "h1" in fold.pending_sessions(ctx)
    sess = ctx.store.read(os.path.join("sessions", "h1.json"))
    assert sess["backfilled"] is True and sess["ended"]


def test_seed_skips_what_is_already_known(env, repo):
    _historical(_home(env), "proj", "h1", repo)
    ctx = LiveContext.build(hosts.ClaudeCode(), repo, env)
    backfill.seed(ctx)
    seeded, found = backfill.seed(ctx)                     # already seeded
    assert seeded == [] and found == 1


def test_seed_respects_a_cap_keeping_the_most_recent(env, repo):
    _historical(_home(env), "proj", "old", repo, age_s=5000)
    _historical(_home(env), "proj", "new", repo, age_s=1000)
    ctx = LiveContext.build(hosts.ClaudeCode(), repo, env)
    seeded, found = backfill.seed(ctx, limit=1)
    assert seeded == ["new"] and found == 2


def test_run_seeds_and_folds(env, repo):
    _historical(_home(env), "proj", "h1", repo)
    ctx = LiveContext.build(hosts.ClaudeCode(), repo, env)
    records, found = backfill.run(ctx)
    assert found == 1 and [r["session_id"] for r in records] == ["h1"]
    assert "h1" in ctx.store.read("folded.json")
    assert ctx.store.read("backfill.json")["seeded"] == 1


# ---- wired into SessionStart ----------------------------------------------------------------
def test_a_repositorys_first_session_folds_its_history(env, repo):
    _historical(_home(env), "proj", "h1", repo)
    base = {"session_id": "live", "transcript_path": "", "cwd": repo, "source": "startup"}
    out = call(env, dict(base, hook_event_name="SessionStart"))
    assert out == {}                                        # microskills=off here by default
    from tokeneconomizer.plugin.paths import repo_identity, repo_state_dir
    from tokeneconomizer.plugin.store import Store
    st = Store(repo_state_dir(repo_identity(repo, env), "claude", env))
    assert "h1" in st.read("folded.json")
    bf = st.read("backfill.json")
    assert bf["found"] == 1 and bf["seeded"] == 1 and abs(bf["at"] - time.time()) < 10


def test_backfill_runs_once_per_repository(env, repo):
    _historical(_home(env), "proj", "h1", repo)
    base = {"session_id": "live", "transcript_path": "", "cwd": repo, "source": "startup"}
    call(env, dict(base, hook_event_name="SessionStart"))
    _historical(_home(env), "proj", "h2", repo)              # appears only after install
    call(env, dict(base, hook_event_name="SessionStart", session_id="live2"))
    from tokeneconomizer.plugin.paths import repo_identity, repo_state_dir
    from tokeneconomizer.plugin.store import Store
    st = Store(repo_state_dir(repo_identity(repo, env), "claude", env))
    assert "h1" in st.read("folded.json") and "h2" not in st.read("folded.json")


def test_backfill_off_skips_it(env, repo):
    _historical(_home(env), "proj", "h1", repo)
    base = {"session_id": "live", "transcript_path": "", "cwd": repo, "source": "startup"}
    call(dict(env, TE_BACKFILL="off"), dict(base, hook_event_name="SessionStart"))
    from tokeneconomizer.plugin.paths import repo_identity, repo_state_dir
    from tokeneconomizer.plugin.store import Store
    st = Store(repo_state_dir(repo_identity(repo, env), "claude", env))
    assert st.read("backfill.json") is None and st.read("folded.json") is None


def test_a_non_claude_host_never_spawns_or_scans(env, repo, monkeypatch):
    spawned = []
    monkeypatch.setattr(LiveContext, "spawn", lambda self, argv, log: spawned.append(argv))
    base = {"session_id": "live", "turn_id": "t", "cwd": repo, "source": "startup"}
    call(env, dict(base, hook_event_name="SessionStart"), host="codex")
    assert spawned == []


def test_a_detached_backfill_is_spawned_without_fold_sync(env, repo, monkeypatch):
    _historical(_home(env), "proj", "h1", repo)
    spawned = []
    monkeypatch.setattr(LiveContext, "spawn", lambda self, argv, log: spawned.append(argv))
    base = {"session_id": "live", "transcript_path": "", "cwd": repo, "source": "startup"}
    live_env = {k: v for k, v in env.items() if k != "TE_FOLD_SYNC"}
    call(live_env, dict(base, hook_event_name="SessionStart"))
    assert ["backfill"] in spawned


# ---- `te backfill` -------------------------------------------------------------------------
def test_cli_backfill_reports_and_folds(env, repo, capsys, monkeypatch):
    from tokeneconomizer.plugin import cli
    _historical(_home(env), "proj", "h1", repo)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert cli.main(["--host", "claude", "--cwd", repo, "backfill"]) == 0
    out = capsys.readouterr().out
    assert "1 earlier session(s) found" in out and "folded 1" in out


def test_cli_backfill_all_ignores_the_cap(env, repo, monkeypatch):
    from tokeneconomizer.plugin import cli
    _historical(_home(env), "proj", "old", repo, age_s=5000)
    _historical(_home(env), "proj", "new", repo, age_s=1000)
    env = dict(env, TE_BACKFILL_SESSIONS="1")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert cli.main(["--host", "claude", "--cwd", repo, "backfill", "--all"]) == 0
    from tokeneconomizer.plugin.paths import repo_identity, repo_state_dir
    from tokeneconomizer.plugin.store import Store
    st = Store(repo_state_dir(repo_identity(repo, env), "claude", env))
    assert set(st.read("folded.json")) == {"old", "new"}


# ---- config ---------------------------------------------------------------------------------
def test_backfill_settings_round_trip():
    from tokeneconomizer.plugin.config import LiveConfig
    cfg = LiveConfig()
    assert cfg.backfill is True and cfg.backfill_sessions == 200
    assert cfg.set("backfill", "off") and cfg.backfill is False
    assert cfg.set("backfill_sessions", "0") and cfg.backfill_sessions == 0
    assert not cfg.set("backfill_sessions", "-1")
