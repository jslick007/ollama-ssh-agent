# Ollama SSH Monitor

Split-terminal dashboard that monitors a remote Ollama server over SSH. Shows system stats (CPU, memory, GPU, disk, top processes) alongside real-time Ollama log streaming.

## Quick Start

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -e .              # installs deps + makes `ollama-monitor` available
ollama-monitor <hostname>     # prompts for password if auth fails
```

## Usage

```bash
ollama-monitor <host>                    # user=root, port=22
ollama-monitor user@host                 # auto-parses user@host
ollama-monitor <host> -u admin -i ~/.ssh/key -p 2222
```

| Option | Default | Description |
|--------|---------|-------------|
| `-u, --user` | `root` | SSH username |
| `-p, --port` | `22` | SSH port |
| `-i, --identity` | — | Path to SSH private key |
| `--ollama-service` | `ollama` | Systemd service name |
| `--interval` | `2.0` | Refresh interval (seconds) |
| `--log-lines` | `200` | Max log lines buffered |

## Requirements

- Python 3.7+, `paramiko>=3.0`, `rich>=13.0`
- SSH access to a Linux host with `ollama` installed
- `nvidia-smi` on the remote for GPU stats (optional)

## How It Works

Two SSH sessions run concurrently: one polls system stats via a batch shell script (`top`, `/proc/meminfo`, `nvidia-smi`, `ollama ps/list/env`), the other streams `journalctl -u ollama`. The Rich `Live` display renders both in a split-panel TUI. Log stream auto-reconnects on disconnect.
