"""The Nous agent CLI adapter.

Runs against a FAKE `hermes` — a tiny Python script that mimics the contract
`-z` promises (final text on stdout, a JSON usage report at --usage-file). That
keeps these tests runnable on a machine where the real CLI is not installed,
which is the point: the adapter's job is argv construction, timeouts, process
cleanup and error mapping, none of which need a real LLM to verify.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import textwrap
from pathlib import Path

import pytest

from app import agent_cli


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Every test starts from a known switch state."""
    for var in (
        "HERMES_AGENT_ENABLED",
        "HERMES_AGENT_BIN",
        "HERMES_AGENT_TIMEOUT_S",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


def _fake_cli(tmp_path: Path, body: str) -> Path:
    """Write a fake `hermes` that behaves like the real one's -z contract."""
    script = tmp_path / "fake_hermes.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")

    # A launcher so the adapter can exec a single path, as it would a real binary.
    if sys.platform == "win32":
        launcher = tmp_path / "hermes.cmd"
        launcher.write_text(
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8"
        )
    else:
        launcher = tmp_path / "hermes"
        launcher.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8"
        )
        launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
    return launcher


ECHO_CLI = """
    import json, sys
    # Mimic `hermes -z`: prompt is argv[2]; write the usage report; print only
    # the final answer.
    argv = sys.argv[1:]
    prompt = argv[1] if len(argv) > 1 else ''
    if '--usage-file' in argv:
        path = argv[argv.index('--usage-file') + 1]
        json.dump({'model': 'fake/model-1', 'provider': 'fake',
                   'session_id': 'sess-123', 'total_tokens': 42,
                   'estimated_cost_usd': 0.0001, 'completed': True},
                  open(path, 'w'))
    # Echo the prompt back so a test can assert it arrived verbatim.
    sys.stdout.write('ECHO:' + prompt)
"""


# --------------------------------------------------------------------------- #
# Gating
# --------------------------------------------------------------------------- #

def test_disabled_by_default(monkeypatch):
    """It grants shell access to the host, so it must be opted into."""
    assert agent_cli.is_enabled() is False
    with pytest.raises(agent_cli.AgentUnavailable, match="disabled"):
        asyncio.run(agent_cli.run_prompt("hi"))


def test_enabled_but_missing_binary_is_a_distinct_error(monkeypatch, tmp_path):
    """"Not installed" and "switched off" are different problems."""
    monkeypatch.setenv("HERMES_AGENT_ENABLED", "true")
    monkeypatch.setenv("HERMES_AGENT_BIN", str(tmp_path / "nope"))
    with pytest.raises(agent_cli.AgentUnavailable, match="not found"):
        asyncio.run(agent_cli.run_prompt("hi"))


def test_status_reports_both_switches(monkeypatch, tmp_path):
    cli = _fake_cli(tmp_path, ECHO_CLI)
    monkeypatch.setenv("HERMES_AGENT_ENABLED", "true")
    monkeypatch.setenv("HERMES_AGENT_BIN", str(cli))
    s = agent_cli.status()
    assert s["enabled"] is True and s["installed"] is True


# --------------------------------------------------------------------------- #
# argv construction — the safety property
# --------------------------------------------------------------------------- #

def test_prompt_is_its_own_argv_element():
    """No shell, so shell metacharacters cannot be interpreted.

    This is what makes it safe to forward request text straight through. If the
    prompt were ever folded into a command STRING, `$(...)` in a request body
    would execute on the host.
    """
    argv = agent_cli._build_argv(
        "hermes", "rm -rf / ; $(whoami) `id`", None, None, Path("u.json")
    )
    assert argv[0] == "hermes"
    assert argv[1] == "-z"
    assert argv[2] == "rm -rf / ; $(whoami) `id`"  # one element, untouched


def test_model_and_provider_are_passed_through():
    argv = agent_cli._build_argv(
        "hermes", "p", "anthropic/claude-sonnet-4.6", "openrouter", Path("u.json")
    )
    assert argv[argv.index("--model") + 1] == "anthropic/claude-sonnet-4.6"
    assert argv[argv.index("--provider") + 1] == "openrouter"


def test_omitted_overrides_are_absent_not_empty():
    """An empty --model would override the configured default with nothing."""
    argv = agent_cli._build_argv("hermes", "p", None, None, Path("u.json"))
    assert "--model" not in argv and "--provider" not in argv


# --------------------------------------------------------------------------- #
# A real round trip against the fake CLI
# --------------------------------------------------------------------------- #

def test_round_trip_returns_stdout_and_usage(monkeypatch, tmp_path):
    cli = _fake_cli(tmp_path, ECHO_CLI)
    monkeypatch.setenv("HERMES_AGENT_ENABLED", "true")
    monkeypatch.setenv("HERMES_AGENT_BIN", str(cli))

    run = asyncio.run(agent_cli.run_prompt("what is 2+2?"))
    assert run.response == "ECHO:what is 2+2?"
    assert run.exit_code == 0
    assert run.model == "fake/model-1"
    assert run.provider == "fake"
    assert run.session_id == "sess-123"
    assert run.usage["total_tokens"] == 42
    assert run.duration_ms >= 0


def test_shell_metacharacters_survive_the_round_trip(monkeypatch, tmp_path):
    """The end-to-end version of the argv test."""
    cli = _fake_cli(tmp_path, ECHO_CLI)
    monkeypatch.setenv("HERMES_AGENT_ENABLED", "true")
    monkeypatch.setenv("HERMES_AGENT_BIN", str(cli))
    nasty = 'a "quoted" $(echo pwned) `id` & | ; b'
    run = asyncio.run(agent_cli.run_prompt(nasty))
    assert run.response == "ECHO:" + nasty


def test_usage_file_is_cleaned_up(monkeypatch, tmp_path):
    cli = _fake_cli(tmp_path, ECHO_CLI)
    monkeypatch.setenv("HERMES_AGENT_ENABLED", "true")
    monkeypatch.setenv("HERMES_AGENT_BIN", str(cli))
    before = set(Path(os.environ.get("TEMP", "/tmp")).glob("hermes-agent-usage-*"))
    asyncio.run(agent_cli.run_prompt("hi"))
    after = set(Path(os.environ.get("TEMP", "/tmp")).glob("hermes-agent-usage-*"))
    assert after <= before


# --------------------------------------------------------------------------- #
# Failure mapping
# --------------------------------------------------------------------------- #

FAILING_CLI = """
    import json, sys
    argv = sys.argv[1:]
    if '--usage-file' in argv:
        path = argv[argv.index('--usage-file') + 1]
        # Upstream writes the report even on failure, so the adapter must read it.
        json.dump({'model': 'fake/model-1', 'completed': False, 'failed': True},
                  open(path, 'w'))
    sys.stderr.write('no provider configured')
    sys.exit(3)
"""


def test_nonzero_exit_becomes_AgentFailed_carrying_stderr(monkeypatch, tmp_path):
    cli = _fake_cli(tmp_path, FAILING_CLI)
    monkeypatch.setenv("HERMES_AGENT_ENABLED", "true")
    monkeypatch.setenv("HERMES_AGENT_BIN", str(cli))
    with pytest.raises(agent_cli.AgentFailed) as err:
        asyncio.run(agent_cli.run_prompt("hi"))
    # Without stderr, "the agent failed" is unactionable.
    assert "no provider configured" in err.value.stderr
    assert err.value.exit_code == 3


SLOW_CLI = """
    import time, sys
    time.sleep(30)
    sys.stdout.write('too late')
"""


def test_timeout_kills_the_run(monkeypatch, tmp_path):
    cli = _fake_cli(tmp_path, SLOW_CLI)
    monkeypatch.setenv("HERMES_AGENT_ENABLED", "true")
    monkeypatch.setenv("HERMES_AGENT_BIN", str(cli))
    with pytest.raises(agent_cli.AgentTimeout):
        asyncio.run(agent_cli.run_prompt("hi", timeout_s=2))


def test_timeout_is_clamped(monkeypatch, tmp_path):
    """A caller cannot ask to hold a worker indefinitely."""
    assert agent_cli.MAX_TIMEOUT_S <= 900
    cli = _fake_cli(tmp_path, ECHO_CLI)
    monkeypatch.setenv("HERMES_AGENT_ENABLED", "true")
    monkeypatch.setenv("HERMES_AGENT_BIN", str(cli))
    # A huge request still completes fast, proving the clamp is not a floor.
    run = asyncio.run(agent_cli.run_prompt("hi", timeout_s=10_000_000))
    assert run.exit_code == 0


def test_empty_prompt_is_rejected(monkeypatch, tmp_path):
    cli = _fake_cli(tmp_path, ECHO_CLI)
    monkeypatch.setenv("HERMES_AGENT_ENABLED", "true")
    monkeypatch.setenv("HERMES_AGENT_BIN", str(cli))
    with pytest.raises(ValueError, match="empty"):
        asyncio.run(agent_cli.run_prompt("   "))


def test_overlong_prompt_is_truncated_and_says_so(monkeypatch, tmp_path):
    cli = _fake_cli(tmp_path, ECHO_CLI)
    monkeypatch.setenv("HERMES_AGENT_ENABLED", "true")
    monkeypatch.setenv("HERMES_AGENT_BIN", str(cli))
    limit = agent_cli.max_prompt_chars(str(cli))
    run = asyncio.run(agent_cli.run_prompt("x" * (limit + 500)))
    assert run.truncated is True
    assert any("truncated" in n for n in run.notes)
    # Actually truncated to the limit, not merely flagged.
    assert len(run.response) == len("ECHO:") + limit


def test_prompt_limit_depends_on_the_launcher():
    """A .cmd is parsed by cmd.exe (8,191 ceiling); a .exe is not (32,767).

    Measured on Windows: through a .cmd shim, 8,000 chars succeeded and 8,191
    failed with "The command line is too long."; launching a .exe directly,
    32,000 succeeded. One cap for both would either break long prompts on a shim
    or needlessly truncate them on the normal install, which ships hermes.exe.
    """
    if sys.platform != "win32":
        assert agent_cli.max_prompt_chars("/usr/bin/hermes") == 32_000
        return
    assert agent_cli.max_prompt_chars("C:/x/hermes.cmd") == 7_000
    assert agent_cli.max_prompt_chars("C:/x/hermes.bat") == 7_000
    assert agent_cli.max_prompt_chars("C:/x/hermes.exe") == 30_000
    # Unknown binary → the direct-launch cap, since that is the real install.
    assert agent_cli.max_prompt_chars(None) == 30_000


def test_real_install_layout_is_discoverable(monkeypatch, tmp_path):
    """The launcher lives in %LOCALAPPDATA%/hermes/hermes-agent/bin.

    One level deeper than HERMES_HOME (%LOCALAPPDATA%/hermes) — the easy mistake,
    and one an earlier version of _candidate_paths made: it looked only at the
    data directory, so it would have missed a real install whose PATH had not
    been reloaded yet.
    """
    fake_local = tmp_path / "Local"
    binary = fake_local / "hermes" / "hermes-agent" / "bin" / "hermes.exe"
    binary.parent.mkdir(parents=True)
    binary.write_text("", encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(fake_local))
    monkeypatch.delenv("HERMES_AGENT_BIN", raising=False)
    # Force the PATH lookup to miss so the fallback list is what answers.
    monkeypatch.setattr(agent_cli.shutil, "which", lambda _n: None)
    assert agent_cli.resolve_binary() == str(binary)
