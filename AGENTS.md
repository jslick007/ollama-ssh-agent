# AGENTS.md — ollama-ssh-agent

Single-file Python TUI that monitors a remote Ollama server via SSH.

## Entrypoint

`ollama_monitor.py` is the only source file (~800 lines). No tests, no CI, no linter/formatter/typecheck config.

## Commands

```bash
ollama-monitor <host>                    # default: user=root, port=22
ollama-monitor <host> -u user -i ~/.ssh/key
ollama-monitor user@host                 # auto-parses user@host syntax
```

Key flags: `-u` (user, default root), `-i` (identity file), `-p` (port, default 22), `--interval` (refresh, default 2.0), `--ollama-service` (default `ollama`), `--log-lines` (default 200).

Without `-i`, prompts for a password via `getpass`.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate   # Windows
pip install -r requirements.txt
```

Two deps: `paramiko>=3.0`, `rich>=13.0`. No lockfile.

## Architecture

- `ollama_monitor.py` — argparse -> SSH connect -> two threads: stats poller (main loop) + log streamer (daemon thread via `threading.Thread`). Rich `Live` display renders the split-panel dashboard.
- Stats gathered via a batch shell script over SSH (`top`, `/proc/meminfo`, `nvidia-smi`, `ollama ps`, `ollama list`, `ollama env`, journalctl, etc.)
- Log streamer reconnects automatically on disconnect (exponential 2s backoff).
- Active model/phase/tps extracted via regex from log lines.

## Gotchas

- No lint/format/typecheck configured — `ruff check`, `black`, `mypy` etc. have no config and would need setup.
- `ssh_connect` uses `AutoAddPolicy()` — automatically accepts unknown host keys.
- The remote script hardcodes paths (`/proc/meminfo`, `journalctl`, `nvidia-smi`); assumes Linux remote host.
- `ollama` binary must be on the remote's PATH for model listing to work.
