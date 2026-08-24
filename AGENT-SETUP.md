# Nous Hermes Agent → `POST /v1/agent/prompt`

Step-by-step: install the Nous Research Hermes agent on Windows, then call it
through this service.

> **Two different things are called Hermes.** *This* service is the
> deterministic product-review verifier — no model decides anything in it. The
> **agent** ([NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent))
> is a separate self-improving LLM with a terminal UI. They share a name and
> nothing else. Everything below is about the agent, which is why the code is
> named `agent_cli.py` / `/v1/agent/*` rather than anything with "hermes" in it.

---

## Step 1 · Install the CLI

Open **PowerShell** (not Git Bash — the installer is a `.ps1`):

```powershell
iex (irm https://hermes-agent.nousresearch.com/install.ps1)
```

That is the native-Windows path from the upstream install docs; no WSL2 needed.
The installer pulls its own **Python 3.11 (via uv)**, **Node.js 22**, **ripgrep**
and **ffmpeg**, so it will not disturb your system Python — your 3.13 is fine
either way, since the project declares `requires-python = ">=3.11,<3.14"`.

There is also a desktop installer on <https://hermes-agent.nousresearch.com/> if
you would rather have the GUI too; the CLI-only route above is enough for this.

A clone already exists at `E:\hermes local\hermes-agent` for reading the source.
**It is not the install** — the installer manages its own copy, and running from
a clone is a separate contributor workflow.

## Step 2 · Verify it and pick a provider

Close and reopen PowerShell first, so PATH is refreshed:

```powershell
hermes --version
hermes doctor          # diagnoses a broken install
```

Then give it a model. The install wizard offers Nous Portal OAuth
(`hermes setup --portal`), but any provider works.

### Using a Gemini key (what this install uses)

The key goes in the agent's **own** `.env`, which is under `HERMES_HOME` — not
this project's `.env`, and not the repo you cloned:

```
C:\Users\<you>\AppData\Local\hermes\.env
```

Add either name; the agent treats them as aliases for one credential
(`hermes_cli/auth.py` registers `api_key_env_vars=("GOOGLE_API_KEY",
"GEMINI_API_KEY")`):

```dotenv
GEMINI_API_KEY=your-key-here
```

Then pick the model:

```powershell
hermes model            # choose Gemini + a model interactively
```

Gemini is natively supported — there is a dedicated
`agent/gemini_native_adapter.py`, not an OpenAI-compatible shim.

You can reuse the same key this verification service uses; they read different
`.env` files, so setting one does not configure the other.

### Or another provider

```powershell
hermes config set OPENROUTER_API_KEY sk-or-...
hermes model
```

### Install warnings that are safe to ignore

This install printed several, none fatal:

- **"Browser tools npm install failed -- exit code"** — followed by npm's own
  `✅ Node dependencies installed`, and the debug log ends `verbose exit 0`. The
  installer misread a successful run. `hermes doctor` afterwards reports browser
  tools and Playwright Chromium as present.
- **"Computer Use driver install did not produce a compatible runtime"** —
  `cua-driver` for desktop control. Unused by this endpoint.
- **"web / ui-tui workspace deps — 3 high"** — build-time tooling, not runtime.
- **Nous Portal "Login cancelled"** — expected if you closed the browser tab;
  irrelevant once a provider key is set.

## Step 3 · Confirm one-shot mode works

This is the mode the endpoint uses, so check it directly before involving HTTP:

```powershell
hermes -z "What is the capital of France?"
```

You should get `Paris.` and nothing else. `-z` (`--oneshot`) is documented as
"single prompt in, final response text out, nothing else on stdout or stderr" —
no banner, spinner, tool previews or `Session:` line. That clean-stdout contract
is what makes it usable as an API backend; `hermes chat -q` also writes tool
output into the transcript, and the bare TUI cannot be driven from a request.

If this step fails, the endpoint cannot work either — fix it here first.

## Step 4 · Switch the endpoint on

In this project's `.env`:

```dotenv
HERMES_AGENT_ENABLED=true
HERMES_AGENT_TIMEOUT_S=300
HERMES_AGENT_MAX_CONCURRENT=2
```

**Read this before you do.** The agent has terminal and filesystem tools on this
machine. Anyone who can reach `POST /v1/agent/prompt` can therefore run commands
here, and this service has no authentication of its own — it was built to be
called privately by the vnyx backend. So:

- keep it bound to localhost (`uvicorn --host 127.0.0.1`), which is the default;
- do not put it on a public interface or behind a tunnel without adding auth;
- that is why it is off unless explicitly enabled, rather than on by default.

The adapter finds the binary without PATH, so this normally needs nothing. The
installer puts it at:

```
C:\Users\<you>\AppData\Local\hermes\hermes-agent\bin\hermes.exe
```

Note that is **one level deeper** than `HERMES_HOME`
(`%LOCALAPPDATA%\hermes`, where `.env` and `config.yaml` live) — an easy pair to
confuse. Override it only if your install is somewhere else:

```dotenv
HERMES_AGENT_BIN=C:\path\to\hermes.exe
```

This matters because a service started *before* the installer ran keeps the old
PATH, so `hermes` resolves in a fresh PowerShell yet not in the running app.
That is exactly why the fallback list exists.

## Step 5 · Restart and check health

```powershell
uvicorn app.main:app --reload --port 8080
```

```powershell
curl http://127.0.0.1:8080/v1/agent/health
```

```json
{ "enabled": true, "installed": true,
  "binary": "C:\\Users\\you\\AppData\\Local\\hermes\\hermes.cmd",
  "default_timeout_s": 300.0, "max_concurrent": 2, "default_model": null }
```

Two separate switches, deliberately: `installed: false` means go and install it,
`enabled: false` means it is there but gated off.

## Step 6 · Send a prompt

```powershell
curl -X POST http://127.0.0.1:8080/v1/agent/prompt `
  -H "content-type: application/json" `
  -d '{\"prompt\":\"What is the capital of France?\"}'
```

```json
{
  "response": "Paris.",
  "duration_ms": 2841,
  "model": "anthropic/claude-sonnet-4.6",
  "provider": "openrouter",
  "session_id": "…",
  "usage": { "total_tokens": 812, "estimated_cost_usd": 0.0031, "api_calls": 1 },
  "truncated": false,
  "notes": []
}
```

### Request fields

| Field | Default | Notes |
|---|---|---|
| `prompt` | required | The only required field. |
| `model` | configured default | Per-run only — does not touch `~/.hermes/config.yaml`. |
| `provider` | configured default | Same. |
| `timeout_s` | 300 | Clamped to 900 server-side. |
| `cwd` | service's cwd | The agent reads and writes files relative to this. Point it at the project you want it to act on. |

`usage` comes from the CLI's own `--usage-file` report, so token counts and cost
are the agent's numbers, not an estimate. Upstream writes that report **even on
failure**, so it is read back regardless of exit status.

### Status codes

| Code | Means |
|---|---|
| 200 | Ran. `response` is the final answer. |
| 400 / 422 | Empty prompt, or a bad field. |
| 502 | The CLI ran and exited non-zero. The detail carries its **stderr** — usually "no provider configured". |
| 503 | Disabled, or the CLI is not installed. The message says which. |
| 504 | Exceeded the timeout; the whole process tree was killed. |

---

## Things worth knowing

**Each call is stateless.** A fresh `-z` run, no `--continue` / `--resume`, so
nothing leaks between callers. Conversation memory would need a session id
threaded through the API — a different feature, not a config change.

**Every call carries ~20,000 input tokens of overhead.** Measured on this
install: "What is the capital of France?" reported `input_tokens: 20591` for
7 output tokens. That is the agent's system prompt, 82 bundled skills and its
tool definitions — not something this endpoint adds. Budget for it, and prefer
this endpoint for work that needs an agent, not for cheap one-line completions
where a plain model call would do.

**Prompt length is capped by the launcher, not the model context.** The prompt
travels as one argv element, so:

| Launcher | Cap | Why |
|---|---|---|
| `hermes.exe` (the normal install) | 30,000 | Direct `CreateProcess`; hard ceiling 32,767. Measured: 32,000 works. |
| a `.cmd` / `.bat` wrapper | 7,000 | `cmd.exe` parses the line; hard ceiling 8,191. Measured: 8,000 works, 8,191 fails with "The command line is too long." |
| Linux / macOS | 32,000 | `ARG_MAX` is megabytes; this is just a sane request size. |

Over the cap the prompt is **truncated with a note** in `notes` rather than
rejected — letting an over-long line reach `cmd.exe` surfaces as a bare exit
code 1, which is far harder to diagnose. For genuinely long input the CLI also
takes `hermes chat --query-file -` on stdin with no length limit, but `chat`
emits tool output too, so it is not a drop-in for `-z`'s final-answer-only
contract.

**Shell metacharacters in a prompt are inert.** The process is launched with an
argv list and no shell, so `$(...)`, backticks, quotes and newlines arrive
verbatim. Two tests pin this — if anyone ever rewrites it to build a command
string, they will fail.

**A timeout kills the whole process tree**, not just the direct child. The agent
spawns Node and whatever a tool shells out to; killing only the parent would
leave those running and spending.

**Concurrency is capped at 2** by default. Each run holds a model connection and
a shell, so further requests queue rather than piling up.

## Troubleshooting

| Symptom | Cause |
|---|---|
| 503 "disabled" | `HERMES_AGENT_ENABLED` is not `true` |
| 503 "not found" | Not installed, or the service started before PATH was updated — set `HERMES_AGENT_BIN` |
| 502 with "no provider configured" | Step 2 was skipped; run `hermes setup --portal` |
| 502 "The command line is too long" | A prompt over the Windows cap slipped through — check `truncated` in the response |
| 504 on every call | Model is slow or the agent is stuck in a tool loop; raise `timeout_s`, and try the same prompt with `hermes -z` directly |
| Works in PowerShell, 503 from the API | Two different environments. The service reads PATH from the process that started it |
