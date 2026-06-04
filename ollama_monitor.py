#!/usr/bin/env python3
"""
ollama-ssh-agent — Monitor a remote Ollama server via SSH.

Provides a split-terminal dashboard:
  • Top section:  htop-like system stats (CPU, memory, GPU, top processes)
  • Bottom section: real-time Ollama log stream

Usage:
  ollama-monitor <host|user@host> [-u <user>] [-i <identity_file>] [-p <port>]
"""

import argparse
import getpass
import queue
import re
import shlex
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import paramiko
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.console import Console, Group

console = Console(stderr=True)


# ─── Data ───────────────────────────────────────────────────────────────────


@dataclass
class SystemStats:
    cpu: str = "\u2014"
    memory: str = "\u2014"
    load: str = "\u2014"
    uptime: str = "\u2014"
    disk: str = "\u2014"
    os_version: str = "\u2014"
    gpu: str = "No GPU"
    gpu_mem: str = ""
    processes: list[str] = field(default_factory=list)
    ollama_procs: list[str] = field(default_factory=list)
    ollama_models: list[str] = field(default_factory=list)
    ollama_available: list[str] = field(default_factory=list)
    ollama_config: dict[str, str] = field(default_factory=dict)


# ─── CLI ────────────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Monitor a remote Ollama server via SSH with an htop-like dashboard.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s admin@192.168.1.100\n"
            "  %(prog)s my.server.com -u admin -i ~/.ssh/id_ed25519\n"
            "  %(prog)s 10.0.0.5 -p 2222 --ollama-service ollama --interval 3\n"
            "  %(prog)s user@host (will prompt for password)\n"
        ),
    )
    parser.add_argument("host", help="Remote server as host or user@host")
    parser.add_argument("-u", "--user", default=None, help="SSH username")
    parser.add_argument(
        "-p", "--port", type=int, default=22, help="SSH port (default: 22)"
    )
    parser.add_argument("-i", "--identity", help="Path to SSH private key file")
    parser.add_argument(
        "--ollama-service",
        default="ollama",
        help="Name of the Ollama service (default: ollama)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="Stats refresh interval in seconds (default: 2.0)",
    )
    parser.add_argument(
        "--log-lines",
        type=int,
        default=200,
        help="Max log lines to buffer (default: 200)",
    )
    args = parser.parse_args()

    # Support user@host syntax
    if "@" in args.host:
        parts = args.host.rsplit("@", 1)
        args.user = args.user or parts[0]
        args.host = parts[1]

    args.user = args.user or "root"

    return args


# ─── SSH helpers ────────────────────────────────────────────────────────────


def ssh_connect(
    host: str,
    port: int,
    user: str,
    identity_file: Optional[str] = None,
    password: Optional[str] = None,
) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs: dict = dict(hostname=host, port=port, username=user, timeout=10)
    if identity_file:
        kwargs["key_filename"] = identity_file
    elif password:
        kwargs["password"] = password
    client.connect(**kwargs)
    return client


# ─── Stats polling ──────────────────────────────────────────────────────────


def fetch_stats(client: paramiko.SSHClient) -> SystemStats:
    """Run a batch of remote commands and return parsed SystemStats."""
    script = (
        "echo '---CPU---'; "
        r"top -bn1 2>/dev/null | grep 'Cpu(s)' | awk '{print $2+$4}'; "
        "echo '---MEM---'; "
        "awk '/MemTotal/{t=$2} /MemAvailable/{a=$2} END{print t, a}' "
        "/proc/meminfo 2>/dev/null || echo '0 0'; "
        "echo '---LOAD---'; "
        r"cat /proc/loadavg 2>/dev/null | awk '{print $1, $2, $3}' || echo '0 0 0'; "
        "echo '---UPTIME---'; "
        r"uptime -p 2>/dev/null | sed 's/up //' || echo 'N/A'; "
        "echo '---DISK---'; "
        r"df -h / 2>/dev/null | awk 'NR==2{print $3\"/\"$2\" (\"$5\")\"}' || echo 'N/A'; "
        "echo '---OS---'; "
        r"cat /etc/os-release 2>/dev/null | grep '^PRETTY_NAME=' | cut -d= -f2 | tr -d '\"' || "
        r"cat /etc/*release 2>/dev/null | head -1 || echo 'Unknown'; "
        "echo '---GPU---'; "
        r"nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total "
        r"--format=csv,noheader,nounits 2>/dev/null | head -1 || echo 'No GPU'; "
        "echo '---PROC---'; "
        r"ps aux --sort=-%cpu 2>/dev/null | head -12 | tail -10; "
        "echo '---OLLAMA_PROC---'; "
        r"ps aux 2>/dev/null | grep '[o]llama'; "
        "echo '---OLLAMA_PS---'; "
        r"ollama ps 2>/dev/null || echo 'not found'; "
        "echo '---OLLAMA_LIST---'; "
        r"ollama list 2>/dev/null || echo 'not found'; "
        "echo '---OLLAMA_ENV---'; "
        r"ollama env 2>/dev/null; "
        r"cat ~/.ollama/config.json 2>/dev/null; "
        r"cat /etc/ollama/config.json 2>/dev/null; "
        r"cat /etc/default/ollama 2>/dev/null || "
        r"cat /etc/sysconfig/ollama 2>/dev/null; "
        r"echo ''; "
    )

    stats = SystemStats()
    try:
        _, stdout, _ = client.exec_command(script, timeout=15)
        out = stdout.read().decode("utf-8", errors="replace")
    except Exception as e:
        stats.cpu = f"SSH err: {e}"
        return stats

    section = ""
    for line in out.splitlines():
        line = line.strip()
        if line == "---CPU---":
            section = "cpu"
        elif line == "---MEM---":
            section = "mem"
        elif line == "---LOAD---":
            section = "load"
        elif line == "---UPTIME---":
            section = "uptime"
        elif line == "---DISK---":
            section = "disk"
        elif line == "---OS---":
            section = "os"
        elif line == "---GPU---":
            section = "gpu"
        elif line == "---PROC---":
            section = "proc"
        elif line == "---OLLAMA_PROC---":
            section = "ollama_proc"
        elif line == "---OLLAMA_PS---":
            section = "ollama_ps"
        elif line == "---OLLAMA_LIST---":
            section = "ollama_list"
        elif line == "---OLLAMA_ENV---":
            section = "ollama_env"
        elif section == "cpu":
            try:
                stats.cpu = f"{float(line):.1f}%"
            except ValueError:
                stats.cpu = line
        elif section == "mem":
            if line and line != "0 0":
                parts = line.split()
                if len(parts) == 2:
                    try:
                        t = float(parts[0])
                        a = float(parts[1])
                        u = t - a
                        stats.memory = f"{u / 1048576:.1f}/{t / 1048576:.1f} GB ({u * 100 / t:.0f}%)"
                    except ValueError:
                        stats.memory = line
        elif section == "load":
            if line and stats.load == "\u2014":
                stats.load = line
        elif section == "uptime":
            if line and stats.uptime == "\u2014":
                stats.uptime = line
        elif section == "disk":
            if line and stats.disk == "\u2014":
                stats.disk = line
        elif section == "os":
            if line and stats.os_version == "\u2014":
                stats.os_version = line
        elif section == "gpu":
            if line and line not in ("No GPU",):
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 4 and stats.gpu == "\u2014":
                    name, util, mem_used, mem_total = (
                        parts[0],
                        parts[1],
                        parts[2],
                        parts[3],
                    )
                    stats.gpu = f"{name} @ {util}%"
                    stats.gpu_mem = f"{mem_used} / {mem_total} MiB"
                elif stats.gpu == "\u2014":
                    stats.gpu = line
            elif stats.gpu == "\u2014":
                stats.gpu = "No GPU"
        elif section == "proc":
            stats.processes.append(line)
        elif section == "ollama_proc":
            if line and line != "OLLAMA_PROC":
                stats.ollama_procs.append(line)
        elif section == "ollama_ps":
            if (
                line
                and not line.startswith("NAME")
                and line not in ("ollama not in PATH", "not found")
            ):
                stats.ollama_models.append(line)
        elif section == "ollama_list":
            if line and not line.startswith("NAME") and line not in ("not found", ""):
                stats.ollama_available.append(line)
        elif section == "ollama_env":
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip("'\"")
                if k.startswith("OLLAMA_") and k not in stats.ollama_config:
                    stats.ollama_config[k] = v

    return stats


# ─── Log streaming ──────────────────────────────────────────────────────────


def log_streamer(
    host: str,
    port: int,
    user: str,
    identity_file: Optional[str],
    password: Optional[str],
    service: str,
    log_queue: queue.Queue,
    stop_event: threading.Event,
) -> None:
    """Continuously stream Ollama logs from the remote host into *log_queue*."""
    delay = 2
    while not stop_event.is_set():
        try:
            client = ssh_connect(host, port, user, identity_file, password)
            log_queue.put("Connected, streaming logs...\n")
            cmd = (
                f"journalctl -u {shlex.quote(service)} -f --no-pager -n 20 "
                f"2>/dev/null || "
                f"tail -f /var/log/{shlex.quote(service)}.log "
                f"2>/dev/null || "
                f"echo 'No log source found for service: {shlex.quote(service)}'"
            )
            transport = client.get_transport()
            if not transport:
                break
            chan = transport.open_session()
            chan.exec_command(cmd)
            buf = b""
            while not stop_event.is_set():
                if chan.recv_ready():
                    data = chan.recv(4096)
                    if not data:
                        break
                    buf += data
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        log_queue.put(line.decode("utf-8", errors="replace"))
                elif chan.exit_status_ready():
                    break
                else:
                    time.sleep(0.05)
            chan.close()
            client.close()
        except Exception as e:
            if not stop_event.is_set():
                log_queue.put(f"Log stream error: {e}")
                time.sleep(delay)
                log_queue.put("Reconnecting log stream...")


# ─── UI ─────────────────────────────────────────────────────────────────────


def _colorize_cpu(val: str) -> Text:
    try:
        pct = float(val.strip("%"))
        color = "green" if pct < 50 else "yellow" if pct < 80 else "red"
        return Text(val, style=color)
    except ValueError:
        return Text(val)


def _colorize_mem(val: str) -> Text:
    m = re.search(r"(\d+)%", val)
    if m:
        pct = int(m.group(1))
        color = "green" if pct < 50 else "yellow" if pct < 80 else "red"
        return Text(val, style=color)
    return Text(val)


def build_layout(
    stats: SystemStats,
    log_lines: list[str],
    log_total: int,
    connected: bool,
    host: str,
    port: int,
    active_model: str = "",
    request_count: int = 0,
    tick: int = 0,
    phase: str = "",
    prompt_time: str = "",
    gen_time: str = "",
    tps_str: str = "",
) -> Layout:
    """Assemble the full-screen Rich Layout."""

    layout = Layout()
    layout.split_column(
        Layout(name="header", size=1),
        Layout(name="main"),
        Layout(name="footer", size=1),
    )

    # ── Header ──
    status_icon = "\u25cf" if connected else "\u25cf"
    status_style = "bold green" if connected else "bold red"
    header = Text.assemble(
        (" Ollama SSH Monitor  ", "bold cyan"),
        (f"  {status_icon} ", status_style),
        (f"{host}:{port}", "white" if connected else "red"),
        ("  [disconnected]", "red") if not connected else "",
    )
    layout["header"].update(Panel(header, border_style="bold"))

    # ── Main body ──
    body = Layout()
    body.split_row(
        Layout(name="left", ratio=4),
        Layout(name="right", ratio=5),
    )

    # ---- Left column: stats (top) + ollama (middle) + processes (bottom) ----
    left = Layout()
    left.split_column(
        Layout(name="stats", ratio=2),
        Layout(name="ollama_panel", ratio=2),
        Layout(name="procs", ratio=3),
    )

    # Stats table
    tbl = Table.grid(padding=(0, 2))
    tbl.add_column(style="bold yellow", justify="right")
    tbl.add_column(style="white")
    tbl.add_row("CPU", _colorize_cpu(stats.cpu))
    tbl.add_row("Memory", _colorize_mem(stats.memory))
    tbl.add_row(
        "GPU Mem",
        Text(stats.gpu_mem, style="magenta")
        if stats.gpu_mem
        else Text("\u2014", style="dim"),
    )
    tbl.add_row("Load", Text(stats.load, style="cyan"))
    tbl.add_row("Uptime", Text(stats.uptime, style="cyan"))
    tbl.add_row("Disk", Text(stats.disk, style="cyan"))
    tbl.add_row("OS", Text(stats.os_version, style="yellow"))
    tbl.add_row("GPU", Text(stats.gpu, style="magenta"))
    if stats.ollama_procs:
        pids = ", ".join(p.split()[1] for p in stats.ollama_procs if len(p.split()) > 1)
        tbl.add_row("Ollama PIDs", Text(pids, style="green"))

    left["stats"].update(Panel(tbl, title="System Stats", border_style="green"))

    # Ollama models & config panel
    ollama_renderables: list = []

    # Active model indicator
    if active_model:
        pulse = "\u2726" if (tick % 2 == 0) else "\u2727"
        active_style = "bold yellow" if (tick % 2 == 0) else "yellow"
        parts = [(f"{pulse} {active_model}", active_style)]
        if phase:
            phase_colors = {"prompting": "cyan", "generating": "green", "idle": "dim"}
            pc = phase_colors.get(phase.lower(), "yellow")
            parts.append((f"  [{phase}]", pc))
        if prompt_time:
            parts.append((f"  prompt:{prompt_time}", "dim"))
        if gen_time:
            parts.append((f"  gen:{gen_time}", "cyan"))
        if tps_str:
            parts.append((f"  {tps_str} t/s", "green"))
        parts.append((f"  [{request_count} req]", "dim"))
        ollama_renderables.append(Text.assemble(*parts))

    # Available models (ollama list)
    if stats.ollama_available:
        atable = Table(
            show_header=True,
            header_style="bold white",
            box=None,
            padding=(0, 1),
            collapse_padding=True,
        )
        atable.add_column("MODEL", no_wrap=True)
        atable.add_column("ID", width=10)
        atable.add_column("SIZE", width=8)
        for line in stats.ollama_available:
            parts = line.split(None, 3)
            if len(parts) >= 3:
                size = parts[2]
                if len(parts) > 3:
                    size += " " + parts[3].split()[0]
                atable.add_row(parts[0], parts[1][:10], size)
        ollama_renderables.append(atable)
    else:
        ollama_renderables.append(Text("No models available", style="dim"))

    # Loaded models (ollama ps)
    if stats.ollama_models:
        ollama_renderables.append(Text(""))
        mtable = Table(
            show_header=True,
            header_style="bold green",
            box=None,
            padding=(0, 1),
            collapse_padding=True,
        )
        mtable.add_column("LOADED", no_wrap=True)
        mtable.add_column("SIZE", width=8)
        mtable.add_column("PROC", width=8)
        mtable.add_column("CTX", width=6)
        mtable.add_column("UNTIL", width=10)
        for line in stats.ollama_models:
            parts = line.split(None, 6)
            if len(parts) >= 4:
                name = parts[0]
                # parts[1] = ID hash (skip)
                # SIZE: "5.3GB" (3 tokens) or "4.2 GB" (4 tokens)
                size_units = ("GB", "MB", "GiB", "MiB", "TB", "KB", "B")
                if len(parts) > 3 and parts[3] in size_units:
                    size = parts[2] + " " + parts[3]
                    proc_parts = parts[4:]
                else:
                    size = parts[2]
                    proc_parts = parts[3:]
                proc = (
                    " ".join(proc_parts[:2])
                    if len(proc_parts) >= 2
                    else (proc_parts[0] if proc_parts else "")
                )
                ctx = proc_parts[2] if len(proc_parts) > 2 else ""
                until = " ".join(proc_parts[3:]) if len(proc_parts) > 3 else ""
                mtable.add_row(name, size, proc, ctx, until)
        ollama_renderables.append(mtable)

    if stats.ollama_config:
        config_lines: list[str] = []
        env_labels = {
            "OLLAMA_HOST": "Host",
            "OLLAMA_MODELS": "Models dir",
            "OLLAMA_NUM_PARALLEL": "Parallel",
            "OLLAMA_MAX_LOADED_MODELS": "Max loaded",
            "OLLAMA_KEEP_ALIVE": "Keep-Alive",
            "OLLAMA_CONTEXT_LENGTH": "Context",
            "OLLAMA_GPU": "GPU layers",
            "OLLAMA_LOAD": "Load",
            "OLLAMA_SCHED_SPREAD": "Sched spread",
            "OLLAMA_FLASH_ATTENTION": "Flash Attn",
            "OLLAMA_KV_CACHE": "KV Cache",
        }
        for key, label in env_labels.items():
            if key in stats.ollama_config:
                config_lines.append(f"{label}: {stats.ollama_config[key]}")
        if config_lines:
            ollama_renderables.append(Text("\n".join(config_lines), style="cyan"))

    left["ollama_panel"].update(
        Panel(
            Group(*ollama_renderables),
            title="Ollama Runtime",
            border_style="green",
        )
    )

    # Top processes table
    pt = Table(
        show_header=True,
        header_style="bold cyan",
        box=None,
        padding=(0, 1),
        collapse_padding=True,
    )
    pt.add_column("USER", style="dim", width=8, no_wrap=True)
    pt.add_column("PID", style="dim", width=6)
    pt.add_column("CPU%", justify="right", width=5)
    pt.add_column("CMD", no_wrap=True)

    for line in stats.processes:
        parts = line.split(None, 10)
        if len(parts) >= 11:
            pt.add_row(parts[0], parts[1], parts[2], " ".join(parts[10:]))

    left["procs"].update(Panel(pt, title="Top Processes", border_style="blue"))
    body["left"].update(left)

    # ---- Right column: log stream ----
    display = log_lines[-60:] if log_lines else ["(waiting for logs...)"]
    log_text = Text("\n".join(display))
    if not connected:
        log_text = Text("Disconnected \u2014 check SSH connection", style="bold red")
    body["right"].update(
        Panel(
            log_text,
            title=f"Ollama Logs ({log_total})",
            border_style="blue",
            highlight=True,
        )
    )

    layout["main"].update(body)

    # ── Footer ──
    layout["footer"].update(
        Panel(Text("Ctrl+C to exit", style="dim"), border_style="dim")
    )

    return layout


# ─── Main ───────────────────────────────────────────────────────────────────


def main() -> None:
    args = parse_args()

    password: Optional[str] = None

    # Primary SSH connection (stats polling)
    try:
        stats_client = ssh_connect(
            args.host, args.port, args.user, args.identity
        )
        connected = True
    except Exception as e:
        if not args.identity and "Authentication" in str(e):
            password = getpass.getpass(f"Password for {args.user}@{args.host}: ")
            try:
                stats_client = ssh_connect(
                    args.host, args.port, args.user, args.identity, password
                )
                connected = True
            except Exception as e2:
                console.print(f"[red]SSH connection failed:[/] {e2}")
                sys.exit(1)
        else:
            console.print(f"[red]SSH connection failed:[/] {e}")
            sys.exit(1)

    # Background log streamer
    log_queue: queue.Queue = queue.Queue()
    stop_event = threading.Event()
    threading.Thread(
        target=log_streamer,
        args=(
            args.host,
            args.port,
            args.user,
            args.identity,
            password,
            args.ollama_service,
            log_queue,
            stop_event,
        ),
        daemon=True,
    ).start()

    # Initial stats fetch so data is ready on first render
    try:
        stats = fetch_stats(stats_client)
    except Exception:
        stats = SystemStats()

    log_lines: list[str] = []
    log_total = 0
    active_model = ""
    request_count = 0
    phase = ""
    prompt_time = ""
    gen_time = ""
    tps_str = ""
    tick = 0

    try:
        with Live(
            screen=True,
            refresh_per_second=1 / args.interval,
            vertical_overflow="visible",
        ) as live:
            # Render first frame immediately with initial data
            layout = build_layout(
                stats,
                log_lines,
                log_total,
                connected,
                args.host,
                args.port,
                active_model,
                request_count,
                tick,
                phase,
                prompt_time,
                gen_time,
                tps_str,
            )
            live.update(layout)
            tick += 1
            while not stop_event.is_set():
                try:
                    stats = fetch_stats(stats_client)
                    connected = True
                except Exception:
                    connected = False
                    try:
                        stats_client.close()
                    except Exception:
                        pass
                    try:
                        stats_client = ssh_connect(
                            args.host, args.port, args.user, args.identity, password
                        )
                        connected = True
                    except Exception:
                        stats = SystemStats()

                while not log_queue.empty():
                    raw = log_queue.get_nowait()
                    log_lines.append(raw)
                    log_total += 1

                    # Track active model from logs
                    m = re.search(
                        r'\b(model|name)["\']?\s*[=:]\s*["\']?([a-zA-Z0-9_./:-]+)', raw
                    )
                    if m:
                        val = m.group(2).strip("\"' ,")
                        if (
                            val
                            and len(val) < 100
                            and (
                                ":" in val
                                or "/" in val
                                or any(
                                    val.startswith(p)
                                    for p in (
                                        "llama",
                                        "qwen",
                                        "mistral",
                                        "deepseek",
                                        "gemma",
                                        "phi",
                                        "nomic",
                                        "dolphin",
                                    )
                                )
                            )
                        ):
                            active_model = val

                    # Phase detection
                    raw_lower = raw.lower()
                    has_dur = "duration" in raw_lower or "timing" in raw_lower
                    if re.search(r'msg\s*[=:]\s*["\']?prompt', raw_lower):
                        phase = "prompting"
                    elif re.search(
                        r'msg\s*[=:]\s*["\']?(?:generate|predict|embed)', raw_lower
                    ):
                        phase = "generating"
                    # Broader fallback if msg= not present
                    if not phase and has_dur:
                        if re.search(
                            r"(?:^|\s)(?:prompt|eval|pp)(?:\s|=|:|$)", raw_lower
                        ):
                            phase = "prompting"
                        elif re.search(
                            r"(?:^|\s)(?:generate|predict|gen)(?:\s|=|:|$)", raw_lower
                        ):
                            phase = "generating"

                    # Duration extraction
                    d = re.search(
                        r"(?:duration|prompt_duration|gen_duration)\s*[:=]\s*([\d.]+)",
                        raw,
                        re.I,
                    )
                    if d:
                        val = d.group(1)
                        if phase == "prompting":
                            prompt_time = f"{val}s"
                        elif phase == "generating":
                            gen_time = f"{val}s"
                        elif "generate" in raw_lower or "predict" in raw_lower:
                            gen_time = f"{val}s"
                            phase = "generating"
                        elif "prompt" in raw_lower:
                            prompt_time = f"{val}s"
                            phase = "prompting"

                    # TPS extraction (log format: "14.50 tokens per second")
                    t = re.search(r"([\d.]+)\s*tokens per second", raw, re.I)
                    if t:
                        tps_str = t.group(1)
                        request_count += 1
                    else:
                        t = re.search(r"\btps\s*[:=]\s*([\d.]+)", raw, re.I)
                        if t:
                            tps_str = t.group(1)

                # Fallback: if no model detected from logs, use first loaded from ollama ps
                if not active_model and stats.ollama_models:
                    first = stats.ollama_models[0].split(None, 1)[0]
                    if first and len(first) < 100:
                        active_model = first

                if len(log_lines) > args.log_lines:
                    log_lines = log_lines[-args.log_lines :]

                layout = build_layout(
                    stats,
                    log_lines,
                    log_total,
                    connected,
                    args.host,
                    args.port,
                    active_model,
                    request_count,
                    tick,
                    phase,
                    prompt_time,
                    gen_time,
                    tps_str,
                )
                live.update(layout)
                tick += 1
                time.sleep(args.interval)

    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        try:
            stats_client.close()
        except Exception:
            pass
        console.print("[green]Shutdown complete.[/]")


if __name__ == "__main__":
    main()
