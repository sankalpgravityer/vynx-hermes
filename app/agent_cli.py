"""Adapter for the Nous Research Hermes Agent CLI.

NAMING — two different things are called Hermes here, and conflating them will
cost someone an afternoon:

  * THIS service (`app/`) is the deterministic product-review verifier. It makes
    no model decisions; rules over a policy table decide everything.
  * The Hermes AGENT (https://github.com/NousResearch/hermes-agent) is a
    separate, self-improving LLM agent with a terminal UI. It is not part of
    this service and shares nothing with it but a name.

Everything in this module is about the second one. It is deliberately named
`agent_*` rather than `hermes_*` so the distinction survives grep.

HOW IT TALKS TO THE AGENT
The agent ships no Python library and no HTTP server — only an interactive TUI
and a messaging gateway. It does, however, have a purpose-built scripted entry
point, which is what this uses:

    hermes -z "<prompt>"

The upstream docs describe `-z` / `--oneshot` as "single prompt in, final
response text out, nothing else on stdout or stderr" — no banner, spinner, tool
previews or Session line. That is the only mode fit for an API: `hermes chat -q`
also emits tool output into the transcript, and the bare TUI cannot be driven
from a request at all.

`--usage-file` is passed alongside it so each call can report tokens and cost.
The upstream docs note the report is written EVEN WHEN THE RUN FAILS, which is
why it is read back regardless of exit status.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Imported for its side effect: app.config calls load_dotenv() at import time,
# which is what puts .env values into os.environ. Every switch below reads
# os.getenv, so without this, importing agent_cli on its own — from a script, a
# REPL, a one-off test — would see an unpopulated environment and report the
# endpoint "disabled" even though .env says otherwise.
#
# That failure is worth one line to prevent, because the error message it
# produces points the reader straight at .env, which would look correct.
# Currently it happens to work only because app/main.py imports app.config
# before this module; relying on import order for correctness is not a property
# worth keeping.
from app import config as _config  # noqa: F401

log = logging.getLogger("hermes.agent_cli")

# One prompt can spawn a long agent run holding a model connection and a shell.
# Unbounded concurrency would let a handful of requests exhaust the machine, so
# runs queue instead. Small by default — this is a local developer tool.
_MAX_CONCURRENT = int(os.getenv("HERMES_AGENT_MAX_CONCURRENT", "2"))
_semaphore = asyncio.Semaphore(_MAX_CONCURRENT)

DEFAULT_TIMEOUT_S = float(os.getenv("HERMES_AGENT_TIMEOUT_S", "300"))
# Generous, but not unbounded: a runaway prompt should not be able to pin a
# worker forever, and an agent turn that has not finished in 15 minutes is stuck.
MAX_TIMEOUT_S = 900.0

# The prompt travels as a single argv element, so the platform's command-line
# limit is the binding constraint — nothing to do with the model's context size.
#
# On Windows there are TWO limits, and which applies depends on what is being
# launched. Both measured on this machine:
#
#   through a .cmd/.bat shim   cmd.exe parses the line and caps it at 8,191
#                              chars — 8,000 succeeded, 8,191 failed with "The
#                              command line is too long." and exit code 1.
#   a .exe launched directly   no cmd.exe involved, so the cap is the
#                              CreateProcess limit of 32,767 — 32,000 succeeded.
#
# The installer provides `hermes.exe`, so the generous limit is the normal case.
# The tight one still has to be handled because HERMES_AGENT_BIN can legitimately
# point at a `.cmd` wrapper.
#
# Elsewhere the limit is ARG_MAX (megabytes), so the cap is only about keeping a
# single request reasonable.
_SHIM_SUFFIXES = {".cmd", ".bat"}
# Conservative default, used when the binary is not known yet.
MAX_PROMPT_CHARS = 7_000 if sys.platform == "win32" else 32_000


def max_prompt_chars(binary: str | None = None) -> int:
    """Longest prompt this binary can accept as an argv element.

    Leaves headroom for the rest of the command line: the binary path, `-z`, the
    --usage-file temp path, and the optional --model / --provider.
    """
    if sys.platform != "win32":
        return 32_000
    if binary and Path(binary).suffix.lower() in _SHIM_SUFFIXES:
        return 7_000  # cmd.exe parses it — 8,191 hard ceiling
    return 30_000  # direct CreateProcess — 32,767 hard ceiling


class AgentUnavailable(RuntimeError):
    """The CLI is not installed, or the feature is switched off."""


class AgentTimeout(RuntimeError):
    """The run exceeded its timeout and was killed."""


class AgentFailed(RuntimeError):
    """The CLI ran and exited non-zero."""

    def __init__(self, message: str, *, exit_code: int, stderr: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.stderr = stderr


@dataclass
class AgentRun:
    response: str
    exit_code: int
    duration_ms: int
    model: str | None = None
    provider: str | None = None
    session_id: str | None = None
    usage: dict[str, Any] | None = None
    stderr: str = ""
    truncated: bool = False
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Locating the binary
# --------------------------------------------------------------------------- #

def _candidate_paths() -> list[Path]:
    """Places the Windows installer is known to put things.

    `shutil.which` is tried first and normally settles it. These are the fallback
    for a shell whose PATH has not been reloaded since the install — a common
    state, because the installer edits the user PATH and an already-running
    uvicorn keeps the old environment.
    """
    out: list[Path] = []
    local = os.getenv("LOCALAPPDATA")
    home = Path.home()
    if local:
        base = Path(local) / "hermes"
        # The real layout, confirmed on this machine: the installer clones into
        # %LOCALAPPDATA%\hermes\hermes-agent and puts the launcher in its bin/.
        # HERMES_HOME is the parent (%LOCALAPPDATA%\hermes), which is why the
        # two are easy to confuse — the binary is one level deeper than the data
        # directory.
        out += [
            base / "hermes-agent" / "bin" / "hermes.exe",
            base / "hermes-agent" / "bin" / "hermes.cmd",
            base / "hermes-agent" / "bin" / "hermes",
            base / "hermes.exe",
            base / "hermes.cmd",
            base / "bin" / "hermes.exe",
            base / "bin" / "hermes.cmd",
        ]
    out += [
        home / ".local" / "bin" / "hermes.exe",
        home / ".local" / "bin" / "hermes.cmd",
        home / ".local" / "bin" / "hermes",
        home / ".hermes" / "bin" / "hermes",
    ]
    return out


def resolve_binary() -> str | None:
    """Absolute path to the `hermes` executable, or None.

    `HERMES_AGENT_BIN` wins — needed when the CLI is installed somewhere
    unusual, and the only way to point at a specific build.

    Resolved to an ABSOLUTE path on purpose. Subprocess launching on Windows
    does not apply PATHEXT the way a shell does, so a bare "hermes" can fail to
    start even when `hermes` works in a terminal; `shutil.which` performs that
    resolution for us.
    """
    override = os.getenv("HERMES_AGENT_BIN", "").strip()
    if override:
        p = Path(override)
        return str(p) if p.exists() else None

    found = shutil.which("hermes")
    if found:
        return found

    for candidate in _candidate_paths():
        if candidate.exists():
            return str(candidate)
    return None


def is_enabled() -> bool:
    """Off unless explicitly switched on.

    This endpoint runs an autonomous agent that has terminal and filesystem
    tools on the host. Anyone who can reach it can, in effect, run commands on
    this machine. That is not something to leave on by default in a service
    whose other endpoints are unauthenticated, so it has to be asked for.
    """
    return os.getenv("HERMES_AGENT_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def status() -> dict[str, Any]:
    binary = resolve_binary()
    return {
        "enabled": is_enabled(),
        "installed": binary is not None,
        "binary": binary,
        "default_timeout_s": DEFAULT_TIMEOUT_S,
        "max_concurrent": _MAX_CONCURRENT,
        "default_model": os.getenv("HERMES_INFERENCE_MODEL") or None,
    }


# --------------------------------------------------------------------------- #
# Running a prompt
# --------------------------------------------------------------------------- #

def _kill_tree(pid: int) -> None:
    """Kill the process AND its children.

    `proc.kill()` reaches only the direct child, and the agent spawns others
    (Node for browser automation, ripgrep, whatever a tool shells out to). On a
    timeout those would survive and keep working — and keep spending — so the
    whole tree goes.
    """
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                capture_output=True,
                timeout=15,
                check=False,
            )
        else:
            os.killpg(os.getpgid(pid), 9)
    except Exception as exc:  # noqa: BLE001 - best effort by definition
        log.warning("could not kill agent process tree %s: %s", pid, exc)


def _build_argv(
    binary: str,
    prompt: str,
    model: str | None,
    provider: str | None,
    usage_file: Path,
) -> list[str]:
    # The prompt is its own argv element and the process is launched WITHOUT a
    # shell, so quotes, $(...), backticks and newlines in it are inert. This is
    # the property that makes it safe to forward request text straight through;
    # building a command string and handing it to a shell would not have it.
    argv = [binary, "-z", prompt, "--usage-file", str(usage_file)]
    if model:
        argv += ["--model", model]
    if provider:
        argv += ["--provider", provider]
    return argv


def _read_usage(path: Path) -> dict[str, Any] | None:
    """Read the usage report. Written even on failure, per upstream docs."""
    try:
        if not path.exists():
            return None
        raw = path.read_text(encoding="utf-8", errors="replace").strip()
        return json.loads(raw) if raw else None
    except Exception as exc:  # noqa: BLE001
        log.warning("could not parse agent usage report: %s", exc)
        return None


def _blocking_run(
    argv: list[str], timeout_s: float, cwd: str | None
) -> tuple[int, str, str]:
    """subprocess.run fallback, used when the loop cannot spawn children.

    Needed because a uvicorn configured with the Windows *Selector* event loop
    raises NotImplementedError from asyncio subprocess support. That is a real
    configuration, so rather than fail the request this runs the same argv in a
    worker thread.
    """
    kwargs: dict[str, Any] = {}
    if sys.platform != "win32":
        kwargs["start_new_session"] = True  # own process group, so _kill_tree works
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            cwd=cwd,
            check=False,
            **kwargs,
        )
    except subprocess.TimeoutExpired as exc:
        raise AgentTimeout(f"agent run exceeded {timeout_s:.0f}s") from exc
    return proc.returncode, proc.stdout or "", proc.stderr or ""


async def run_prompt(
    prompt: str,
    *,
    model: str | None = None,
    provider: str | None = None,
    timeout_s: float | None = None,
    cwd: str | None = None,
) -> AgentRun:
    """Send one prompt to the agent and return its final answer.

    Stateless: each call is a fresh `-z` run with no `--continue` / `--resume`,
    so nothing leaks between callers. Conversation memory would need a session id
    threaded through the API, which is a different feature.
    """
    if not is_enabled():
        raise AgentUnavailable(
            "The agent endpoint is disabled. Set HERMES_AGENT_ENABLED=true to "
            "switch it on, and read the security note in agent_cli.is_enabled "
            "first — it grants callers shell access to this host."
        )

    binary = resolve_binary()
    if not binary:
        raise AgentUnavailable(
            "The `hermes` CLI was not found. Install it, then either restart "
            "this service so it picks up the new PATH, or set HERMES_AGENT_BIN "
            "to the executable's full path."
        )

    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("prompt is empty")
    notes: list[str] = []
    truncated = False
    # Depends on the resolved binary: a .cmd wrapper is parsed by cmd.exe and
    # capped far lower than a directly-launched .exe.
    limit = max_prompt_chars(binary)
    if len(prompt) > limit:
        prompt = prompt[:limit]
        truncated = True
        notes.append(f"prompt truncated to {limit} characters")

    budget = min(float(timeout_s or DEFAULT_TIMEOUT_S), MAX_TIMEOUT_S)

    # NamedTemporaryFile(delete=False) then unlink: the CLI writes this path
    # itself, and on Windows a still-open handle would block it.
    fd, usage_path_str = tempfile.mkstemp(prefix="hermes-agent-usage-", suffix=".json")
    os.close(fd)
    usage_path = Path(usage_path_str)

    argv = _build_argv(binary, prompt, model, provider, usage_path)
    started = time.perf_counter()

    try:
        async with _semaphore:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    stdin=asyncio.subprocess.DEVNULL,
                    cwd=cwd,
                    # Own process group so a timeout can take the whole tree.
                    start_new_session=(sys.platform != "win32"),
                )
            except NotImplementedError:
                notes.append(
                    "event loop cannot spawn subprocesses; ran in a worker thread"
                )
                code, out, err = await asyncio.to_thread(
                    _blocking_run, argv, budget, cwd
                )
            else:
                try:
                    raw_out, raw_err = await asyncio.wait_for(
                        proc.communicate(), timeout=budget
                    )
                except asyncio.TimeoutError as exc:
                    _kill_tree(proc.pid)
                    # Reap, so the child is not left as a zombie.
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=10)
                    except asyncio.TimeoutError:
                        pass
                    raise AgentTimeout(
                        f"agent run exceeded {budget:.0f}s and was terminated"
                    ) from exc
                code = proc.returncode if proc.returncode is not None else -1
                out = (raw_out or b"").decode("utf-8", errors="replace")
                err = (raw_err or b"").decode("utf-8", errors="replace")

        duration_ms = int((time.perf_counter() - started) * 1000)
        usage = _read_usage(usage_path)

        if code != 0:
            # stderr is where the CLI puts the reason; keep it, trimmed, because
            # "the agent failed" with no detail is unactionable.
            raise AgentFailed(
                f"`hermes -z` exited with code {code}",
                exit_code=code,
                stderr=err.strip()[:2000],
            )

        # `-z` is documented to emit nothing but the final reply, so the whole of
        # stdout IS the answer. Only whitespace is stripped.
        return AgentRun(
            response=out.strip(),
            exit_code=code,
            duration_ms=duration_ms,
            model=(usage or {}).get("model") or model,
            provider=(usage or {}).get("provider") or provider,
            session_id=(usage or {}).get("session_id"),
            usage=usage,
            stderr=err.strip()[:2000],
            truncated=truncated,
            notes=notes,
        )
    finally:
        try:
            usage_path.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
