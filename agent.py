# ┌─────────────────────────────────────────────────┐
# │  CODE AGENT v1.0.0                              │
# │  Claude-Code clone · NVIDIA NIM backend         │
# │  Python 3.10+ · Rich · prompt_toolkit · requests│
# │  Install: pip install rich prompt_toolkit        │
# │           requests                              │
# │  Run    : python agent.py                       │
# └─────────────────────────────────────────────────┘

# ══ SECTION 0: IMPORTS ══════════════════════════════════════════════
import os
import sys
import json
import re
import subprocess
import shutil
import difflib
import fnmatch
import hashlib
import base64
import tempfile
import traceback
import platform
import time
import threading
import textwrap
import copy
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from typing import Any, Optional
from dataclasses import dataclass, field
from enum import Enum

# Windows console encoding fix — must happen before any Rich output
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
        os.system("chcp 65001 > nul")
    except Exception:
        pass

try:
    import requests
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.syntax import Syntax
    from rich.markdown import Markdown
    from rich.text import Text
    from rich.live import Live
    from rich.progress import Progress, SpinnerColumn, TextColumn
    from rich.box import DOUBLE_EDGE, ROUNDED, SIMPLE, MINIMAL
    from rich.columns import Columns
    from rich.rule import Rule
    from rich import print as rprint
    from prompt_toolkit import prompt, Application
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
    from prompt_toolkit.styles import Style as PTStyle
    from prompt_toolkit.formatted_text import HTML
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import HSplit, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.filters import Condition
    from prompt_toolkit.keys import Keys
except ImportError as e:
    print(f"[ERROR] Missing dependency: {e}")
    print("Install with: pip install rich prompt_toolkit requests")
    sys.exit(1)

# ══ SECTION 1: CONSTANTS & CONFIG ═══════════════════════════════════

VERSION = "v1.0.0"
MAX_READ_BYTES = 256 * 1024          # 256 KB
MAX_WRITE_WARN_BYTES = 1024 * 1024   # 1 MB
MAX_STDOUT_CHARS = 8000
MAX_STDERR_CHARS = 4000
MAX_RESULT_PREVIEW = 3000
MAX_ITERATIONS = 30
MAX_FIND_RESULTS = 200
MAX_SEARCH_RESULTS = 100
MAX_TREE_ENTRIES = 500
DEFAULT_TREE_DEPTH = 4
DEFAULT_CMD_TIMEOUT = 120
DEFAULT_SCRIPT_TIMEOUT = 30
MAX_HISTORY_MESSAGES = 80
COMPACT_KEEP_MESSAGES = 50
MAX_DIFF_LINES = 60
DEFAULT_MODEL = "mistralai/mistral-medium-3"
API_TEMPERATURE = 0.2
API_TOP_P = 0.7
API_MAX_TOKENS = 16384
API_TIMEOUT = 180

SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    ".env", "dist", "build", ".next", ".turbo", ".cache",
    "*.egg-info", ".mypy_cache", ".pytest_cache", ".ruff_cache",
}

DANGEROUS_PATTERNS = [
    "rm -rf", "del /f /s /q", "format ", "mkfs.", "dd if=",
    ":(){", "shutdown", "reboot", "rmdir /s", "rd /s",
    ">(", "deltree", "fdisk", "wipefs",
]

SAFETY_LOG_FILE = ".agent_safety_log"
SESSION_LOG_FILE = ".agent_session.log"
HISTORY_FILE = ".agent_history"


class ToolCategory(Enum):
    """Categories for tools, used to colour tool call panels."""
    FILE_READ = "blue"
    FILE_WRITE = "magenta"
    SEARCH = "yellow"
    EXECUTE = "red"
    INFO = "cyan"
    UTILITY = "green"


TOOL_COLORS: dict[str, str] = {
    "read_file": ToolCategory.FILE_READ.value,
    "read_file_range": ToolCategory.FILE_READ.value,
    "list_directory": ToolCategory.FILE_READ.value,
    "write_file": ToolCategory.FILE_WRITE.value,
    "edit_file": ToolCategory.FILE_WRITE.value,
    "create_directory": ToolCategory.FILE_WRITE.value,
    "delete_file": ToolCategory.UTILITY.value,
    "rename_move": ToolCategory.UTILITY.value,
    "copy_file": ToolCategory.UTILITY.value,
    "search_files": ToolCategory.SEARCH.value,
    "find_files": ToolCategory.SEARCH.value,
    "run_command": ToolCategory.EXECUTE.value,
    "run_script": ToolCategory.EXECUTE.value,
    "get_file_info": ToolCategory.INFO.value,
    "get_project_structure": ToolCategory.INFO.value,
}

# Detect emoji support
USE_EMOJI = True
if sys.platform == "win32":
    wt = os.environ.get("WT_SESSION") or os.environ.get("WINDOWS_TERMINAL")
    if not wt:
        USE_EMOJI = False

def _e(emoji: str, fallback: str) -> str:
    """Return emoji or ASCII fallback based on terminal support."""
    return emoji if USE_EMOJI else fallback


# ══ SECTION 2: GLOBALS (WORKDIR, etc.) ══════════════════════════════

WORKDIR: Path = Path.cwd().resolve()
console = Console()

def get_workdir() -> Path:
    """Get the current working directory (module-level, read at call time)."""
    return WORKDIR

def set_workdir(new_path: Path) -> None:
    """Update the global working directory."""
    global WORKDIR
    WORKDIR = new_path.resolve()
    os.chdir(WORKDIR)

def resolve_path(path: str) -> Path:
    """Resolve a path relative to WORKDIR at call time."""
    p = Path(path)
    if p.is_absolute():
        return p.resolve()
    return (get_workdir() / p).resolve()


@dataclass
class AgentConfig:
    """Stores the configuration collected during the setup wizard."""
    endpoint: str = ""
    api_key: str = ""
    model: str = DEFAULT_MODEL
    workdir: Path = field(default_factory=Path.cwd)
    project_context: str = ""
    shell: str = ""
    os_info: str = ""


# ══ SECTION 3: UI HELPERS (arrow_select, pick_directory, etc.) ══════

def detect_shell() -> str:
    """Auto-detect the current shell."""
    if sys.platform == "win32":
        ps_ver = os.environ.get("PSVersionTable", "")
        if os.environ.get("PSModulePath"):
            return "PowerShell"
        return "cmd.exe"
    shell_env = os.environ.get("SHELL", "")
    if "zsh" in shell_env:
        return "zsh"
    if "bash" in shell_env:
        return "bash"
    return "sh"


def arrow_select(title: str, options: list[str]) -> Optional[int]:
    """
    Show an arrow-key driven selection menu using prompt_toolkit.
    Returns the selected index, or None if Ctrl-C pressed.
    """
    state = {"index": 0, "done": False, "cancelled": False}
    n = len(options)

    def get_text():
        lines = [("class:title", f"  {title}\n\n")]
        for i, opt in enumerate(options):
            if i == state["index"]:
                lines.append(("class:selected", f"  {_e('❯', '>')} {opt}\n"))
            else:
                lines.append(("class:unselected", f"    {opt}\n"))
        lines.append(("class:footer", "\n  ↑/↓ move   Enter select   Ctrl-C quit\n"))
        return lines

    kb = KeyBindings()

    @kb.add("up")
    def _up(event):
        state["index"] = (state["index"] - 1) % n

    @kb.add("down")
    def _down(event):
        state["index"] = (state["index"] + 1) % n

    @kb.add("enter")
    def _enter(event):
        state["done"] = True
        event.app.exit()

    @kb.add("c-c")
    def _cancel(event):
        state["cancelled"] = True
        event.app.exit()

    style = PTStyle.from_dict({
        "title": "bold cyan",
        "selected": "bold green",
        "unselected": "",
        "footer": "dim",
    })

    layout = Layout(
        HSplit([
            Window(
                content=FormattedTextControl(get_text, focusable=False),
                always_hide_cursor=True,
            )
        ])
    )

    app: Application = Application(
        layout=layout,
        key_bindings=kb,
        style=style,
        full_screen=False,
        mouse_support=False,
        refresh_interval=0.05,
    )
    app.run()

    if state["cancelled"]:
        return None
    return state["index"]


def pick_directory(start: Optional[Path] = None) -> Optional[Path]:
    """
    Interactive arrow-key directory browser built with prompt_toolkit.
    Returns selected directory or None if cancelled.
    """
    current = (start or Path.cwd()).resolve()

    while True:
        console.print(Panel(
            f"[cyan]{current}[/cyan]",
            title="[bold]Select Working Directory[/bold]",
            border_style="cyan",
        ))

        try:
            subdirs = sorted(
                [d for d in current.iterdir()
                 if d.is_dir() and not d.name.startswith(".")],
                key=lambda x: x.name.lower()
            )
        except PermissionError:
            console.print("[red]Permission denied.[/red]")
            current = current.parent
            continue

        options: list[str] = []
        options.append(f"{_e('✅', '[OK]')} USE THIS DIRECTORY")
        if current.parent != current:
            options.append(f"{_e('📁', '[DIR]')} .. (go up)")
        for d in subdirs:
            options.append(f"{_e('📁', '[DIR]')} {d.name}/")

        idx = arrow_select("Choose directory:", options)
        if idx is None:
            return None

        if idx == 0:
            return current
        elif current.parent != current and idx == 1:
            current = current.parent
        else:
            offset = 2 if current.parent != current else 1
            chosen = subdirs[idx - offset]
            current = chosen

    return current


def show_welcome_banner() -> None:
    """Display the animated welcome banner using Rich."""
    art = r"""
   ██████╗ ██████╗ ██████╗ ███████╗      █████╗  ██████╗ ███████╗███╗   ██╗████████╗
  ██╔════╝██╔═══██╗██╔══██╗██╔════╝     ██╔══██╗██╔════╝ ██╔════╝████╗  ██║╚══██╔══╝
  ██║     ██║   ██║██║  ██║█████╗       ███████║██║  ███╗█████╗  ██╔██╗ ██║   ██║
  ██║     ██║   ██║██║  ██║██╔══╝       ██╔══██║██║   ██║██╔══╝  ██║╚██╗██║   ██║
  ╚██████╗╚██████╔╝██████╔╝███████╗     ██║  ██║╚██████╔╝███████╗██║ ╚████║   ██║
   ╚═════╝ ╚═════╝ ╚═════╝ ╚══════╝     ╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝   ╚═╝
    """
    content = Text()
    content.append(art, style="bold bright_green")
    content.append("\n  Terminal AI Coding Agent — NVIDIA NIM Backend\n", style="bold white")
    content.append(f"  {VERSION}  •  Python {sys.version.split()[0]}  •  ", style="dim white")
    content.append("Powered by NVIDIA NIM", style="bold bright_green")
    content.append("\n\n  Type /help after setup to see all commands.\n", style="dim cyan")

    panel = Panel(
        content,
        box=DOUBLE_EDGE,
        border_style="bright_cyan",
        padding=(1, 2),
    )

    with Live(panel, console=console, refresh_per_second=20) as live:
        for _ in range(10):
            time.sleep(0.05)
        live.update(panel)

    console.print()


def masked_key(key: str) -> str:
    """Return a masked preview of an API key."""
    if len(key) < 12:
        return "****"
    return key[:8] + "…" + key[-4:]


# ══ SECTION 4: TOOL IMPLEMENTATIONS ════════════════════════════════

def _is_dangerous(command: str) -> Optional[str]:
    """Check a command string against DANGEROUS_PATTERNS. Returns matched pattern or None."""
    cmd_lower = command.lower()
    for pat in DANGEROUS_PATTERNS:
        if pat.lower() in cmd_lower:
            return pat
    return None


def _confirm_dangerous(command: str, pattern: str) -> bool:
    """
    Show a red warning panel and ask for explicit YES confirmation.
    Logs the decision to SAFETY_LOG_FILE.
    """
    console.print(Panel(
        f"[bold red]DANGEROUS PATTERN DETECTED[/bold red]\n\n"
        f"Command : [yellow]{command}[/yellow]\n"
        f"Pattern : [bold red]{pattern}[/bold red]\n\n"
        "[white]This command could cause irreversible damage.[/white]",
        border_style="red",
        title="[red]⚠ Safety Warning[/red]",
    ))
    try:
        answer = prompt(HTML("<ansired>⚠ Type YES to confirm (case-sensitive): </ansired>"))
    except (KeyboardInterrupt, EOFError):
        answer = ""
    decision = answer.strip() == "YES"
    _log_safety(command, pattern, decision)
    return decision


def _log_safety(command: str, pattern: str, allowed: bool) -> None:
    """Append a safety decision to the safety log file."""
    try:
        log_path = get_workdir() / SAFETY_LOG_FILE
        with open(log_path, "a", encoding="utf-8") as f:
            ts = datetime.now().isoformat()
            verdict = "ALLOWED" if allowed else "BLOCKED"
            f.write(f"[{ts}] [{verdict}] pattern='{pattern}' cmd='{command[:200]}'\n")
    except Exception:
        pass


def _log_session(entry: str) -> None:
    """Append an entry to the session log file."""
    try:
        log_path = get_workdir() / SESSION_LOG_FILE
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(entry + "\n")
    except Exception:
        pass


def _redact_key(text: str, api_key: str) -> str:
    """Replace any occurrence of the API key with a redaction marker."""
    if api_key and api_key in text:
        text = text.replace(api_key, "[API_KEY_REDACTED]")
    return text


def _skip_dir(name: str) -> bool:
    """Return True if a directory name should be skipped."""
    return name in SKIP_DIRS or name.startswith(".")


# ── Tool 01: read_file ───────────────────────────────────────────────
def tool_read_file(path: str) -> str:
    """Read a file's content, capped at MAX_READ_BYTES."""
    try:
        fp = resolve_path(path)
        if not fp.exists():
            return f"ERROR: File not found: {path}"
        if not fp.is_file():
            return f"ERROR: Not a file: {path}"
        size = fp.stat().st_size
        with open(fp, "r", encoding="utf-8", errors="replace") as f:
            if size > MAX_READ_BYTES:
                content = f.read(MAX_READ_BYTES)
                return content + f"\n\n[FILE TRUNCATED — showing first {MAX_READ_BYTES} bytes of {size} total]"
            return f.read()
    except Exception as ex:
        return f"ERROR: {type(ex).__name__}: {ex}"


# ── Tool 02: write_file ──────────────────────────────────────────────
def tool_write_file(path: str, content: str) -> str:
    """Create or overwrite a file with the given content. Never blocks."""
    try:
        fp = resolve_path(path)
        wd = get_workdir().resolve()
        if not str(fp).startswith(str(wd)):
            return f"ERROR: Refusing to write outside working directory: {fp}"
        if len(content.encode("utf-8")) > MAX_WRITE_WARN_BYTES:
            console.print(f"[yellow]Warning: writing large file ({len(content)} chars) to {path}[/yellow]")
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
        if not fp.exists():
            return f"ERROR: Write appeared to succeed but file not found: {fp}"
        byte_count = fp.stat().st_size
        rel = fp.relative_to(wd) if fp.is_relative_to(wd) else fp
        _log_session(f"[{datetime.now().isoformat()}] [TOOL] write_file path='{rel}' bytes={byte_count}")
        return f"{_e('✅', 'OK')} Written: {rel} ({byte_count:,} bytes)"
    except Exception as ex:
        return f"ERROR: {type(ex).__name__}: {ex}"


# ── Tool 03: edit_file ───────────────────────────────────────────────
def tool_edit_file(path: str, old_string: str, new_string: str) -> str:
    """Apply a precise find-and-replace edit to an existing file."""
    try:
        fp = resolve_path(path)
        wd = get_workdir().resolve()
        if not str(fp).startswith(str(wd)):
            return f"ERROR: Refusing to edit outside working directory: {fp}"
        if not fp.exists():
            return f"ERROR: File not found: {path}"
        original = fp.read_text(encoding="utf-8", errors="replace")
        count = original.count(old_string)
        if count == 0:
            return _edit_not_found_hint(original, old_string, path)
        if count > 1:
            return (f"WARNING: old_string appears {count} times in {path}. "
                    "Provide a more specific old_string that matches exactly once.")
        updated = original.replace(old_string, new_string, 1)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(updated, encoding="utf-8")
        diff = _make_diff(original, updated, str(fp))
        _log_session(f"[{datetime.now().isoformat()}] [TOOL] edit_file path='{path}'")
        return diff
    except Exception as ex:
        return f"ERROR: {type(ex).__name__}: {ex}"


def _edit_not_found_hint(content: str, old_string: str, path: str) -> str:
    """Return a helpful context snippet when old_string is not found."""
    lines = content.splitlines()
    # Try to find the closest line using a rough heuristic
    first_line = old_string.strip().splitlines()[0].strip() if old_string.strip() else ""
    best_idx = -1
    if first_line:
        for i, line in enumerate(lines):
            if first_line[:20] in line:
                best_idx = i
                break
    if best_idx >= 0:
        start = max(0, best_idx - 2)
        end = min(len(lines), best_idx + 3)
        excerpt = "\n".join(f"{start+j+1}: {lines[start+j]}" for j in range(end - start))
    else:
        excerpt = "\n".join(f"{i+1}: {lines[i]}" for i in range(min(5, len(lines))))
    return (f"ERROR: old_string not found in {path}.\n"
            f"Nearby content:\n{excerpt}\n"
            "Check whitespace/indentation and provide an exact match.")


def _make_diff(original: str, updated: str, path: str) -> str:
    """Generate a unified diff string between original and updated content."""
    diff_lines = list(difflib.unified_diff(
        original.splitlines(keepends=True),
        updated.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        lineterm="",
    ))
    if len(diff_lines) > MAX_DIFF_LINES:
        diff_lines = diff_lines[:MAX_DIFF_LINES] + [f"\n[... diff truncated at {MAX_DIFF_LINES} lines]"]
    return "".join(diff_lines) if diff_lines else "(no changes)"


# ── Tool 04: list_directory ──────────────────────────────────────────
def tool_list_directory(path: str = "") -> str:
    """List directory contents with icons and sizes."""
    try:
        target = resolve_path(path) if path else get_workdir()
        if not target.exists():
            return f"ERROR: Directory not found: {path}"
        if not target.is_dir():
            return f"ERROR: Not a directory: {path}"
        entries = list(target.iterdir())
        dirs = sorted([e for e in entries if e.is_dir()], key=lambda x: x.name.lower())
        files = sorted([e for e in entries if e.is_file()], key=lambda x: x.name.lower())
        lines = [f"Directory: {target}\n"]
        hidden_count = 0
        for d in dirs:
            if d.name.startswith(".") or d.name in SKIP_DIRS:
                hidden_count += 1
                continue
            lines.append(f"  {_e('📁', '[D]')} {d.name}/")
        for f in files:
            if f.name.startswith("."):
                hidden_count += 1
                continue
            try:
                size = f.stat().st_size
                size_str = _human_size(size)
            except OSError:
                size_str = "?"
            lines.append(f"  {_e('📄', '[F]')} {f.name}  ({size_str})")
        if hidden_count:
            lines.append(f"\n  [{hidden_count} hidden entries not shown]")
        return "\n".join(lines)
    except Exception as ex:
        return f"ERROR: {type(ex).__name__}: {ex}"


def _human_size(n: int) -> str:
    """Convert byte count to a human-readable string."""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ── Tool 05: search_files ────────────────────────────────────────────
def tool_search_files(
    pattern: str,
    path: str = "",
    file_glob: str = "",
    max_results: int = MAX_SEARCH_RESULTS,
) -> str:
    """Recursively grep files for a regex pattern."""
    try:
        root = resolve_path(path) if path else get_workdir()
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            return f"ERROR: Invalid regex '{pattern}': {e}"
        results: list[str] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not _skip_dir(d)]
            for fname in filenames:
                if file_glob and not fnmatch.fnmatch(fname, file_glob):
                    continue
                fpath = Path(dirpath) / fname
                try:
                    with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                        for lineno, line in enumerate(f, 1):
                            if rx.search(line):
                                rel = fpath.relative_to(root)
                                results.append(f"{rel}:{lineno}: {line.rstrip()}")
                                if len(results) >= max_results:
                                    results.append(f"[results capped at {max_results}]")
                                    return "\n".join(results)
                except (OSError, UnicodeDecodeError):
                    continue
        return "\n".join(results) if results else "No matches found."
    except Exception as ex:
        return f"ERROR: {type(ex).__name__}: {ex}"


# ── Tool 06: run_command ─────────────────────────────────────────────
def tool_run_command(command: str, timeout: int = DEFAULT_CMD_TIMEOUT) -> str:
    """Execute a shell command in WORKDIR and capture output."""
    pattern = _is_dangerous(command)
    if pattern:
        if not _confirm_dangerous(command, pattern):
            return f"ERROR: Command blocked by safety check (pattern: '{pattern}')"
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["powershell.exe", "-Command", command],
                cwd=str(get_workdir()),
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
            )
        else:
            result = subprocess.run(
                command,
                shell=True,
                cwd=str(get_workdir()),
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
            )
        stdout = result.stdout[:MAX_STDOUT_CHARS]
        stderr = result.stderr[:MAX_STDERR_CHARS]
        if len(result.stdout) > MAX_STDOUT_CHARS:
            stdout += f"\n[stdout truncated at {MAX_STDOUT_CHARS} chars]"
        if len(result.stderr) > MAX_STDERR_CHARS:
            stderr += f"\n[stderr truncated at {MAX_STDERR_CHARS} chars]"
        parts = []
        if stdout:
            parts.append(stdout)
        if stderr:
            parts.append(f"[stderr]\n{stderr}")
        parts.append(f"[exit code: {result.returncode}]")
        _log_session(f"[{datetime.now().isoformat()}] [TOOL] run_command cmd='{command[:100]}' exit={result.returncode}")
        return "\n".join(parts)
    except subprocess.TimeoutExpired:
        return f"ERROR: Command timed out after {timeout}s"
    except Exception as ex:
        return f"ERROR: {type(ex).__name__}: {ex}"


# ── Tool 07: find_files ──────────────────────────────────────────────
def tool_find_files(pattern: str, path: str = "") -> str:
    """Recursively find files matching a glob pattern."""
    try:
        root = resolve_path(path) if path else get_workdir()
        matches: list[str] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not _skip_dir(d)]
            for fname in filenames:
                if fnmatch.fnmatch(fname, pattern) or fnmatch.fnmatch(
                    str(Path(dirpath) / fname), pattern
                ):
                    try:
                        rel = (Path(dirpath) / fname).relative_to(root)
                        matches.append(str(rel))
                    except ValueError:
                        matches.append(str(Path(dirpath) / fname))
                    if len(matches) >= MAX_FIND_RESULTS:
                        matches.append(f"[results capped at {MAX_FIND_RESULTS}]")
                        return "\n".join(matches)
        return "\n".join(matches) if matches else "No files matched."
    except Exception as ex:
        return f"ERROR: {type(ex).__name__}: {ex}"


# ── Tool 08: get_file_info ───────────────────────────────────────────
def tool_get_file_info(path: str) -> str:
    """Return metadata for a file or directory."""
    try:
        import mimetypes
        fp = resolve_path(path)
        if not fp.exists():
            return f"ERROR: Path not found: {path}"
        stat = fp.stat()
        is_dir = fp.is_dir()
        info_lines = [
            f"Path      : {fp}",
            f"Type      : {'Directory' if is_dir else 'File'}",
            f"Size      : {_human_size(stat.st_size)} ({stat.st_size:,} bytes)",
            f"Modified  : {datetime.fromtimestamp(stat.st_mtime).isoformat()}",
            f"Created   : {datetime.fromtimestamp(stat.st_ctime).isoformat()}",
            f"Permissions: {oct(stat.st_mode)[-4:]}",
        ]
        if not is_dir and stat.st_size <= 1024 * 1024:
            try:
                with open(fp, "r", encoding="utf-8", errors="replace") as f:
                    line_count = sum(1 for _ in f)
                info_lines.append(f"Lines     : {line_count:,}")
            except Exception:
                pass
        mime, _ = mimetypes.guess_type(str(fp))
        if mime:
            info_lines.append(f"MIME type : {mime}")
        return "\n".join(info_lines)
    except Exception as ex:
        return f"ERROR: {type(ex).__name__}: {ex}"


# ── Tool 09: create_directory ────────────────────────────────────────
def tool_create_directory(path: str) -> str:
    """Create a directory and all required parent directories."""
    try:
        fp = resolve_path(path)
        fp.mkdir(parents=True, exist_ok=True)
        return f"{_e('✅', 'OK')} Directory ready: {fp}"
    except Exception as ex:
        return f"ERROR: {type(ex).__name__}: {ex}"


# ── Tool 10: delete_file ─────────────────────────────────────────────
def tool_delete_file(path: str) -> str:
    """Delete a single file. Refuses to delete directories."""
    try:
        fp = resolve_path(path)
        if not fp.exists():
            return f"ERROR: File not found: {path}"
        if fp.is_dir():
            return "ERROR: Use delete_directory for directories (not implemented for safety)."
        fp.unlink()
        _log_session(f"[{datetime.now().isoformat()}] [TOOL] delete_file path='{path}'")
        return f"{_e('✅', 'OK')} Deleted: {fp}"
    except Exception as ex:
        return f"ERROR: {type(ex).__name__}: {ex}"


# ── Tool 11: rename_move ─────────────────────────────────────────────
def tool_rename_move(source: str, destination: str) -> str:
    """Rename or move a file or directory."""
    try:
        src = resolve_path(source)
        dst = resolve_path(destination)
        if not src.exists():
            return f"ERROR: Source not found: {source}"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        if not dst.exists():
            return f"ERROR: Move appeared to fail — destination not found: {dst}"
        return f"{_e('✅', 'OK')} Moved: {src} → {dst}"
    except Exception as ex:
        return f"ERROR: {type(ex).__name__}: {ex}"


# ── Tool 12: copy_file ───────────────────────────────────────────────
def tool_copy_file(source: str, destination: str) -> str:
    """Copy a file to a new location."""
    try:
        src = resolve_path(source)
        dst = resolve_path(destination)
        if not src.exists():
            return f"ERROR: Source not found: {source}"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(dst))
        if not dst.exists():
            return f"ERROR: Copy appeared to fail — destination not found: {dst}"
        return f"{_e('✅', 'OK')} Copied: {src} → {dst}"
    except Exception as ex:
        return f"ERROR: {type(ex).__name__}: {ex}"


# ── Tool 13: read_file_range ─────────────────────────────────────────
def tool_read_file_range(path: str, start_line: int, end_line: int) -> str:
    """Read a specific line range from a file (1-indexed)."""
    try:
        fp = resolve_path(path)
        if not fp.exists():
            return f"ERROR: File not found: {path}"
        if not fp.is_file():
            return f"ERROR: Not a file: {path}"
        with open(fp, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        total = len(all_lines)
        s = max(1, start_line) - 1
        e = min(total, end_line)
        if s >= total:
            return f"ERROR: start_line {start_line} exceeds file length ({total} lines)"
        selected = all_lines[s:e]
        numbered = [f"{s+i+1:6}: {line}" for i, line in enumerate(selected)]
        header = f"Lines {s+1}–{s+len(selected)} of {total} total\n"
        return header + "".join(numbered)
    except Exception as ex:
        return f"ERROR: {type(ex).__name__}: {ex}"


# ── Tool 14: run_script ──────────────────────────────────────────────
def tool_run_script(
    language: str,
    code: str,
    timeout: int = DEFAULT_SCRIPT_TIMEOUT,
) -> str:
    """Write a temp script and execute it with the appropriate interpreter."""
    lang_map = {
        "python": (".py", [sys.executable]),
        "powershell": (".ps1", ["powershell.exe", "-File"]),
        "bash": (".sh", ["bash"]),
        "node": (".js", ["node"]),
    }
    lang = language.lower()
    if lang not in lang_map:
        return f"ERROR: Unsupported language '{language}'. Choose: python, powershell, bash, node"
    ext, interpreter = lang_map[lang]
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=ext, mode="w", encoding="utf-8", delete=False
        ) as tmp_f:
            tmp_f.write(code)
            tmp = tmp_f.name
        cmd = interpreter + [tmp]
        result = subprocess.run(
            cmd,
            cwd=str(get_workdir()),
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        stdout = result.stdout[:MAX_STDOUT_CHARS]
        stderr = result.stderr[:MAX_STDERR_CHARS]
        parts = []
        if stdout:
            parts.append(stdout)
        if stderr:
            parts.append(f"[stderr]\n{stderr}")
        parts.append(f"[exit code: {result.returncode}]")
        return "\n".join(parts)
    except subprocess.TimeoutExpired:
        return f"ERROR: Script timed out after {timeout}s"
    except Exception as ex:
        return f"ERROR: {type(ex).__name__}: {ex}"
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except Exception:
                pass


# ── Tool 15: get_project_structure ───────────────────────────────────
def tool_get_project_structure(
    path: str = "",
    max_depth: int = DEFAULT_TREE_DEPTH,
    show_hidden: bool = False,
) -> str:
    """Return an ASCII tree view of the project directory."""
    try:
        root = resolve_path(path) if path else get_workdir()
        if not root.exists():
            return f"ERROR: Path not found: {path}"
        lines: list[str] = [f"{root.name}/"]
        count_holder = [0]
        _build_tree(root, "", max_depth, 0, show_hidden, lines, count_holder)
        if count_holder[0] >= MAX_TREE_ENTRIES:
            lines.append(f"  [tree truncated at {MAX_TREE_ENTRIES} entries]")
        return "\n".join(lines)
    except Exception as ex:
        return f"ERROR: {type(ex).__name__}: {ex}"


def _build_tree(
    directory: Path,
    prefix: str,
    max_depth: int,
    depth: int,
    show_hidden: bool,
    lines: list[str],
    count: list[int],
) -> None:
    """Recursive helper for get_project_structure."""
    if depth >= max_depth or count[0] >= MAX_TREE_ENTRIES:
        return
    try:
        entries = list(directory.iterdir())
    except PermissionError:
        return
    dirs = sorted(
        [e for e in entries if e.is_dir() and (show_hidden or not e.name.startswith("."))
         and not _skip_dir(e.name)],
        key=lambda x: x.name.lower(),
    )
    files = sorted(
        [e for e in entries if e.is_file() and (show_hidden or not e.name.startswith("."))],
        key=lambda x: x.name.lower(),
    )
    all_items = dirs + files
    for i, item in enumerate(all_items):
        if count[0] >= MAX_TREE_ENTRIES:
            break
        is_last = i == len(all_items) - 1
        connector = "└── " if is_last else "├── "
        if item.is_dir():
            lines.append(f"{prefix}{connector}{item.name}/")
        else:
            try:
                size = _human_size(item.stat().st_size)
            except OSError:
                size = "?"
            lines.append(f"{prefix}{connector}{item.name}  ({size})")
        count[0] += 1
        if item.is_dir():
            extension = "    " if is_last else "│   "
            _build_tree(item, prefix + extension, max_depth, depth + 1, show_hidden, lines, count)


# ══ SECTION 5: TOOL DEFINITIONS (JSON schema for API) ═══════════════

TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the complete UTF-8 text content of a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to working directory"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create a new file or completely overwrite an existing file with the given content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to working directory"},
                    "content": {"type": "string", "description": "Full file content to write"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Apply a precise find-and-replace to an existing file. old_string must match exactly once.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path"},
                    "old_string": {"type": "string", "description": "Exact string to find (must appear exactly once)"},
                    "new_string": {"type": "string", "description": "Replacement string"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "List the contents of a directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path (default: working directory)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Recursively grep files for a regex pattern.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern to search for"},
                    "path": {"type": "string", "description": "Directory to search (default: working dir)"},
                    "file_glob": {"type": "string", "description": "File glob filter e.g. '*.py'"},
                    "max_results": {"type": "integer", "description": "Maximum results to return"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Execute a shell command in the working directory and capture output.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (default 120)"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_files",
            "description": "Recursively find files matching a glob pattern.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern e.g. '*.py' or '**/*.ts'"},
                    "path": {"type": "string", "description": "Directory to search"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_file_info",
            "description": "Get metadata for a file or directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File or directory path"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_directory",
            "description": "Create a directory and all required parent directories.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path to create"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "Delete a single file. Will not delete directories.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to delete"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rename_move",
            "description": "Rename or move a file or directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "Source path"},
                    "destination": {"type": "string", "description": "Destination path"},
                },
                "required": ["source", "destination"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "copy_file",
            "description": "Copy a file to a new location.",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "Source file path"},
                    "destination": {"type": "string", "description": "Destination file path"},
                },
                "required": ["source", "destination"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file_range",
            "description": "Read a specific line range from a large file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path"},
                    "start_line": {"type": "integer", "description": "Start line number (1-indexed)"},
                    "end_line": {"type": "integer", "description": "End line number (inclusive)"},
                },
                "required": ["path", "start_line", "end_line"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_script",
            "description": "Write a temporary script and execute it with the appropriate interpreter.",
            "parameters": {
                "type": "object",
                "properties": {
                    "language": {
                        "type": "string",
                        "enum": ["python", "powershell", "bash", "node"],
                        "description": "Script language",
                    },
                    "code": {"type": "string", "description": "Script code to execute"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (default 30)"},
                },
                "required": ["language", "code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_project_structure",
            "description": "Return a tree-view of the project directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Root path (default: working dir)"},
                    "max_depth": {"type": "integer", "description": "Max tree depth (default 4)"},
                    "show_hidden": {"type": "boolean", "description": "Show hidden files (default false)"},
                },
                "required": [],
            },
        },
    },
]


# ══ SECTION 6: TOOL DISPATCH TABLE ══════════════════════════════════

TOOL_DISPATCH: dict[str, Any] = {
    "read_file": lambda args: tool_read_file(**args),
    "write_file": lambda args: tool_write_file(**args),
    "edit_file": lambda args: tool_edit_file(**args),
    "list_directory": lambda args: tool_list_directory(**args),
    "search_files": lambda args: tool_search_files(**args),
    "run_command": lambda args: tool_run_command(**args),
    "find_files": lambda args: tool_find_files(**args),
    "get_file_info": lambda args: tool_get_file_info(**args),
    "create_directory": lambda args: tool_create_directory(**args),
    "delete_file": lambda args: tool_delete_file(**args),
    "rename_move": lambda args: tool_rename_move(**args),
    "copy_file": lambda args: tool_copy_file(**args),
    "read_file_range": lambda args: tool_read_file_range(**args),
    "run_script": lambda args: tool_run_script(**args),
    "get_project_structure": lambda args: tool_get_project_structure(**args),
}


# ══ SECTION 7: NVIDIA AGENT CLASS ═══════════════════════════════════

def _build_system_prompt(config: AgentConfig) -> str:
    """Build the complete system prompt string from the agent config."""
    return f"""[IDENTITY]
You are an expert AI coding assistant embedded in a terminal-based code agent.
You have direct read/write/execute access to the user's project files via
structured tool calls. You behave like Claude Code: methodical, safe, precise.
Always chain tool calls logically: explore → read → plan → write → verify.

[WORKING ENVIRONMENT]
- Working directory: {config.workdir.resolve()}
- Shell: {config.shell}
- OS: {config.os_info}
- Date/time: {datetime.now().isoformat()}
- Project context: {config.project_context or 'not specified'}

[TOOL USAGE RULES]
1. ALWAYS call list_directory before creating or editing files in a new directory.
2. ALWAYS call read_file before editing an existing file.
3. Use write_file ONLY for new files or full rewrites.
4. Use edit_file for ALL targeted edits — provide exact old_string matches.
5. After EVERY file write or edit, call read_file to verify the result.
6. After running tests or build commands, report exit codes and any errors.
7. NEVER guess file contents — always read first.
8. If a required file does not exist, create it with write_file.
9. Always create parent directories with create_directory before write_file.
10. Chain tool calls logically: explore → read → plan → write → verify.

[RESPONSE FORMAT RULES]
1. Before each tool call, write one sentence explaining what you are about to do and why.
2. After all tool calls complete, write a clear summary of what was done.
3. Use markdown for all prose: headers, bullet points, code blocks.
4. Keep prose concise. Prefer action over explanation.
5. When showing code in prose, always specify the language in the code fence.

[SAFETY RULES]
1. Never run destructive commands (rm -rf, format, dd) without explicit user confirmation.
2. Never write outside the working directory without asking.
3. Never expose the API key in any output.
4. Never delete files without asking.
"""


class NvidiaAgent:
    """Main agent class: manages conversation, tool execution, and API calls."""

    def __init__(self, config: AgentConfig) -> None:
        """Initialise the agent with the provided configuration."""
        self.config = config
        self.endpoint = config.endpoint
        self.api_key = config.api_key
        self.model = config.model
        self.workdir = config.workdir
        self.system_prompt = _build_system_prompt(config)
        self.conversation: list[dict] = [
            {"role": "system", "content": self.system_prompt}
        ]
        self.total_tokens_used: int = 0
        self.tool_call_count: int = 0
        self.session_start: datetime = datetime.now()
        self.modified_files: set[str] = set()
        self.last_user_message: str = ""
        self.turn_tool_count: int = 0

    def call_api(self) -> dict:
        """
        Call the NVIDIA NIM API with stream=False (CRITICAL — never stream).
        Returns the full response dict.
        """
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": self.conversation,
            "tools": TOOLS,
            "tool_choice": "auto",
            "temperature": API_TEMPERATURE,
            "top_p": API_TOP_P,
            "max_tokens": API_MAX_TOKENS,
            "stream": False,  # NEVER change this — streaming breaks tool JSON
        }
        resp = requests.post(
            self.endpoint,
            headers=headers,
            json=payload,
            timeout=API_TIMEOUT,
        )
        if not resp.ok:
            body_preview = resp.text[:800]
            console.print(f"[red]API Error {resp.status_code}:[/red] {body_preview}")
            resp.raise_for_status()
        data = resp.json()
        usage = data.get("usage", {})
        self.total_tokens_used += usage.get("total_tokens", 0)
        return data

    def parse_tool_calls(self, tool_calls_raw: list[dict]) -> list[dict]:
        """
        Parse raw tool call objects from the API response.
        Handles both dict and JSON-string argument formats (BUG 05 fix).
        """
        parsed: list[dict] = []
        for tc in tool_calls_raw:
            tc_id = tc.get("id", "")
            fn = tc.get("function", {})
            name = fn.get("name", "")
            raw_args = fn.get("arguments", {})
            # BUG 05: arguments may be dict OR JSON string
            if isinstance(raw_args, dict):
                args = raw_args
            elif isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args)
                except json.JSONDecodeError:
                    console.print(f"[yellow]Warning: could not parse tool args for {name}: {raw_args[:200]}[/yellow]")
                    args = {}
            else:
                args = {}
            parsed.append({"id": tc_id, "name": name, "args": args})
        return parsed

    def execute_tool(self, name: str, args: dict) -> str:
        """
        Execute a tool by name with the given arguments.
        Returns a descriptive string result; never raises.
        """
        if name not in TOOL_DISPATCH:
            return f"ERROR: unknown tool '{name}'"
        t0 = time.time()
        try:
            result = TOOL_DISPATCH[name](args)
        except Exception as ex:
            result = f"ERROR: {type(ex).__name__}: {ex}"
        elapsed = time.time() - t0
        # Track modified files
        if name in ("write_file", "edit_file") and not result.startswith("ERROR"):
            path_arg = args.get("path", "")
            if path_arg:
                self.modified_files.add(path_arg)
        # Redact API key
        result = _redact_key(str(result), self.api_key)
        args_preview = str(args)[:120]
        result_preview = result[:120]
        _log_session(
            f"[{datetime.now().isoformat()}] [TOOL] {name} "
            f"args='{args_preview}' result='{result_preview}' elapsed={elapsed:.2f}s"
        )
        return result

    def _show_tool_call(self, name: str, args: dict) -> None:
        """Display the tool call panel before execution."""
        color = TOOL_COLORS.get(name, "white")
        args_str = json.dumps(args, indent=None, ensure_ascii=False)
        if len(args_str) > 200:
            args_str = args_str[:197] + "..."
        console.print(Panel(
            f"  [bold]Name[/bold] : [cyan]{name}[/cyan]\n"
            f"  [bold]Args[/bold] : {args_str}",
            title=f"[bold]{_e('🔧', '[TOOL]')} Tool Call[/bold]",
            border_style=color,
            expand=False,
        ))

    def _show_tool_result(self, name: str, result: str) -> None:
        """Display the tool result panel after execution."""
        color = TOOL_COLORS.get(name, "white")
        is_error = result.startswith("ERROR")
        icon = _e("❌", "[ERR]") if is_error else _e("✅", "[OK]")
        border = "red" if is_error else color

        preview = result
        truncated = ""
        if len(result) > MAX_RESULT_PREVIEW:
            truncated = f"\n[dim][... {len(result) - MAX_RESULT_PREVIEW} more chars][/dim]"
            preview = result[:MAX_RESULT_PREVIEW]

        # Choose rendering style based on tool
        if name == "read_file" and not is_error:
            _render_syntax_result(name, preview, truncated, border, icon)
        elif name in ("run_command", "run_script") and not is_error:
            _render_bash_result(name, preview, truncated, border, icon)
        elif name == "edit_file" and not is_error:
            _render_diff_result(preview, truncated, border, icon)
        else:
            console.print(Panel(
                preview + truncated,
                title=f"[bold]{icon} {name} result[/bold]",
                border_style=border,
                expand=False,
            ))

    def _show_write_preview(self, path: str, content: str) -> None:
        """Show a syntax-highlighted preview of a file before writing."""
        lines = content.splitlines()
        if len(lines) > 50:
            preview_lines = lines[:25] + ["", "  … (content truncated for preview) …", ""] + lines[-10:]
            preview = "\n".join(preview_lines)
        else:
            preview = content
        ext = Path(path).suffix.lstrip(".") or "text"
        syntax = Syntax(preview, ext, line_numbers=True, theme="monokai")
        console.print(Panel(
            syntax,
            title=f"[magenta]{_e('📄', '[FILE]')} Preview: {path}[/magenta]",
            border_style="magenta",
        ))

    def chat(self, user_message: str) -> None:
        """
        Main conversation loop. Appends user message, calls API,
        handles tool calls, and loops until the model stops calling tools.
        """
        self.last_user_message = user_message
        self.turn_tool_count = 0
        self.conversation.append({"role": "user", "content": user_message})
        _log_session(f"[{datetime.now().isoformat()}] [USER] {user_message[:200]}")

        # Auto-compact if conversation is too long
        if len(self.conversation) > MAX_HISTORY_MESSAGES:
            self.compact_history()

        turn_start = time.time()

        for iteration in range(MAX_ITERATIONS):
            # Call API with spinner
            response_data = None
            with console.status(
                f"[bold green]{_e('⠋', '...')} Thinking…[/bold green]",
                spinner="dots",
            ):
                try:
                    response_data = self.call_api()
                except Exception as ex:
                    console.print(Panel(
                        f"[red]API call failed:[/red] {ex}",
                        border_style="red",
                        title="[red]Error[/red]",
                    ))
                    return

            choice = response_data.get("choices", [{}])[0]
            message = choice.get("message", {})
            content = message.get("content") or ""
            tool_calls_raw = message.get("tool_calls") or []

            # Build the assistant message to append (preserve tool_calls if present)
            assistant_msg: dict = {"role": "assistant", "content": content}
            if tool_calls_raw:
                assistant_msg["tool_calls"] = tool_calls_raw
            self.conversation.append(assistant_msg)

            # Render prose content
            if content:
                _render_model_response(content)

            # If no tool calls, we're done
            if not tool_calls_raw:
                break

            # Process tool calls
            parsed_calls = self.parse_tool_calls(tool_calls_raw)
            for tc in parsed_calls:
                tc_id = tc["id"]   # BUG 04: always use the exact ID from API
                name = tc["name"]
                args = tc["args"]

                # Show write preview before writing
                if name == "write_file" and "content" in args:
                    self._show_write_preview(args.get("path", "?"), args["content"])

                self._show_tool_call(name, args)
                result = self.execute_tool(name, args)
                self._show_tool_result(name, result)

                # Append tool result with the exact tool_call_id (BUG 04)
                self.conversation.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": result,
                })
                self.tool_call_count += 1
                self.turn_tool_count += 1

        else:
            console.print(f"[yellow]Warning: reached MAX_ITERATIONS ({MAX_ITERATIONS}). Stopping.[/yellow]")

        # Status bar
        elapsed = time.time() - turn_start
        tokens = self.total_tokens_used
        _print_status_bar(self.model, tokens, self.turn_tool_count, elapsed)

    def compact_history(self) -> None:
        """Compact conversation history keeping system prompt + last N messages."""
        if len(self.conversation) <= COMPACT_KEEP_MESSAGES + 1:
            return
        system = self.conversation[0]
        recent = self.conversation[-(COMPACT_KEEP_MESSAGES):]
        self.conversation = [system] + recent
        console.print(
            f"[dim]History compacted — keeping system prompt + last {COMPACT_KEEP_MESSAGES} messages.[/dim]"
        )

    def get_stats(self) -> str:
        """Return a formatted session statistics string."""
        duration = datetime.now() - self.session_start
        hours, rem = divmod(int(duration.total_seconds()), 3600)
        mins, secs = divmod(rem, 60)
        dur_str = f"{hours:02d}:{mins:02d}:{secs:02d}"
        files_list = ", ".join(sorted(self.modified_files)) or "none"
        return (
            f"Session time    : {dur_str}\n"
            f"Total tokens    : {self.total_tokens_used:,}\n"
            f"Total tool calls: {self.tool_call_count}\n"
            f"Messages in ctx : {len(self.conversation)}\n"
            f"Modified files  : {files_list}"
        )

    def rebuild_system_prompt(self) -> None:
        """Rebuild the system prompt (e.g., after /cd changes workdir)."""
        self.config.workdir = get_workdir()
        self.system_prompt = _build_system_prompt(self.config)
        if self.conversation and self.conversation[0]["role"] == "system":
            self.conversation[0]["content"] = self.system_prompt


# ══ Rendering helpers ═══════════════════════════════════════════════

def _guess_language(name: str) -> str:
    """Guess syntax language from a file path extension."""
    ext_map = {
        ".py": "python", ".js": "javascript", ".ts": "typescript",
        ".jsx": "jsx", ".tsx": "tsx", ".json": "json", ".yaml": "yaml",
        ".yml": "yaml", ".md": "markdown", ".html": "html", ".css": "css",
        ".sh": "bash", ".bash": "bash", ".zsh": "bash", ".ps1": "powershell",
        ".rs": "rust", ".go": "go", ".java": "java", ".c": "c",
        ".cpp": "cpp", ".h": "c", ".rb": "ruby", ".php": "php",
        ".sql": "sql", ".toml": "toml", ".xml": "xml", ".txt": "text",
        ".env": "bash", ".dockerfile": "dockerfile",
    }
    return "text"


def _render_syntax_result(name: str, preview: str, truncated: str, border: str, icon: str) -> None:
    """Render a read_file result with syntax highlighting."""
    console.print(Panel(
        Syntax(preview, "text", line_numbers=True, theme="monokai"),
        title=f"[bold]{icon} {name} result[/bold]",
        border_style=border,
    ))
    if truncated:
        console.print(truncated)


def _render_bash_result(name: str, preview: str, truncated: str, border: str, icon: str) -> None:
    """Render a run_command/run_script result with bash syntax highlighting."""
    console.print(Panel(
        Syntax(preview, "bash", theme="monokai"),
        title=f"[bold]{icon} {name} result[/bold]",
        border_style=border,
    ))
    if truncated:
        console.print(truncated)


def _render_diff_result(preview: str, truncated: str, border: str, icon: str) -> None:
    """Render an edit_file diff result with diff syntax highlighting."""
    console.print(Panel(
        Syntax(preview, "diff", theme="monokai"),
        title=f"[bold]{icon} Changes Applied[/bold]",
        border_style=border,
    ))
    if truncated:
        console.print(truncated)


def _render_model_response(content: str) -> None:
    """Render the model's text response as Rich Markdown in a panel."""
    md = Markdown(content, code_theme="monokai")
    console.print(Panel(md, border_style="white", box=ROUNDED, expand=True))


def _print_status_bar(model: str, tokens: int, tools: int, elapsed: float) -> None:
    """Print the dim status bar line after a response."""
    console.print(
        f"[dim][model: {model}] [tokens: {tokens:,}] [tools: {tools}] [time: {elapsed:.1f}s][/dim]"
    )


# ══ SECTION 8: SLASH COMMAND HANDLER ════════════════════════════════

SLASH_COMMANDS: dict[str, str] = {
    "/help": "Show this help table",
    "/quit": "Show session summary and exit",
    "/exit": "Alias for /quit",
    "/clear": "Reset conversation to system prompt only",
    "/compact": "Manually compact conversation history",
    "/dir": "Print current working directory",
    "/ls": "List working directory contents",
    "/tree": "Show project directory tree",
    "/model": "Show current model or switch: /model <name>",
    "/stats": "Show session statistics",
    "/history": "Show last 10 conversation turns",
    "/tokens": "Show token usage",
    "/undo": "Remove last user+assistant turn",
    "/retry": "Re-send the last user message",
    "/cd <path>": "Change working directory",
    "/run <cmd>": "Run a shell command directly",
    "/read <file>": "Read and display a file directly",
    "/find <glob>": "Find files matching a glob pattern",
    "/search <regex>": "Search files for a regex pattern",
    "/context": "Show the full system prompt",
}


def handle_slash_command(line: str, agent: NvidiaAgent) -> bool:
    """
    Handle a slash command. Returns True if handled, False if not a slash command.
    """
    parts = line.strip().split(None, 1)
    cmd = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if cmd == "/help":
        _cmd_help()
        return True
    if cmd in ("/quit", "/exit"):
        _cmd_quit(agent)
        raise SystemExit(0)
    if cmd == "/clear":
        _cmd_clear(agent)
        return True
    if cmd == "/compact":
        agent.compact_history()
        return True
    if cmd == "/dir":
        console.print(f"[cyan]{get_workdir()}[/cyan]")
        return True
    if cmd == "/ls":
        console.print(tool_list_directory(""))
        return True
    if cmd == "/tree":
        console.print(tool_get_project_structure())
        return True
    if cmd == "/model":
        if arg:
            agent.model = arg.strip()
            console.print(f"[green]Model switched to: {agent.model}[/green]")
        else:
            console.print(f"[cyan]Current model: {agent.model}[/cyan]")
        return True
    if cmd == "/stats":
        console.print(Panel(agent.get_stats(), title="[bold]Session Stats[/bold]", border_style="cyan"))
        return True
    if cmd == "/history":
        _cmd_history(agent)
        return True
    if cmd == "/tokens":
        console.print(f"[cyan]Total tokens used: {agent.total_tokens_used:,}[/cyan]")
        return True
    if cmd == "/undo":
        _cmd_undo(agent)
        return True
    if cmd == "/retry":
        _cmd_retry(agent)
        return True
    if cmd == "/cd":
        _cmd_cd(arg, agent)
        return True
    if cmd == "/run":
        if not arg:
            console.print("[yellow]Usage: /run <command>[/yellow]")
        else:
            result = tool_run_command(arg)
            console.print(Syntax(result, "bash", theme="monokai"))
        return True
    if cmd == "/read":
        if not arg:
            console.print("[yellow]Usage: /read <file>[/yellow]")
        else:
            result = tool_read_file(arg)
            ext = Path(arg).suffix.lstrip(".") or "text"
            console.print(Syntax(result, ext, line_numbers=True, theme="monokai"))
        return True
    if cmd == "/find":
        if not arg:
            console.print("[yellow]Usage: /find <glob>[/yellow]")
        else:
            result = tool_find_files(arg)
            console.print(result)
        return True
    if cmd == "/search":
        if not arg:
            console.print("[yellow]Usage: /search <regex>[/yellow]")
        else:
            result = tool_search_files(arg)
            console.print(result)
        return True
    if cmd == "/context":
        console.print(Panel(agent.system_prompt, title="[bold]System Prompt[/bold]", border_style="dim"))
        return True
    if cmd.startswith("/"):
        console.print(
            f"[yellow]Unknown command: {cmd}[/yellow]\n"
            "Type [bold]/help[/bold] to see available commands."
        )
        return True
    return False


def _cmd_help() -> None:
    """Display the help table of slash commands."""
    table = Table(title="Slash Commands", box=ROUNDED, border_style="cyan", show_lines=True)
    table.add_column("Command", style="bold green", no_wrap=True)
    table.add_column("Description", style="white")
    for cmd, desc in SLASH_COMMANDS.items():
        table.add_row(cmd, desc)
    console.print(table)


def _cmd_quit(agent: NvidiaAgent) -> None:
    """Show the session summary panel and exit."""
    duration = datetime.now() - agent.session_start
    hours, rem = divmod(int(duration.total_seconds()), 3600)
    mins, secs = divmod(rem, 60)
    dur_str = f"{hours:02d}:{mins:02d}:{secs:02d}"
    files = "\n".join(f"  • {f}" for f in sorted(agent.modified_files)) or "  (none)"
    summary = (
        f"[bold]Duration[/bold]      : {dur_str}\n"
        f"[bold]Messages[/bold]      : {len(agent.conversation)}\n"
        f"[bold]Tool calls[/bold]    : {agent.tool_call_count}\n"
        f"[bold]Tokens used[/bold]   : {agent.total_tokens_used:,}\n"
        f"[bold]Files modified[/bold]:\n{files}"
    )
    console.print(Panel(
        summary,
        title=f"[bold cyan]{_e('🎯', '[END]')} Session Complete[/bold cyan]",
        border_style="cyan",
        box=DOUBLE_EDGE,
    ))
    _log_session(
        f"[{datetime.now().isoformat()}] [SESSION_END] "
        f"duration={dur_str} tools={agent.tool_call_count} tokens={agent.total_tokens_used}"
    )


def _cmd_clear(agent: NvidiaAgent) -> None:
    """Reset conversation to system prompt, with confirmation."""
    try:
        answer = prompt(HTML("<ansiyellow>Clear conversation history? [y/N]: </ansiyellow>"))
    except (KeyboardInterrupt, EOFError):
        answer = "n"
    if answer.strip().lower() == "y":
        agent.conversation = [agent.conversation[0]]
        console.print("[green]Conversation cleared.[/green]")
    else:
        console.print("[dim]Cancelled.[/dim]")


def _cmd_history(agent: NvidiaAgent) -> None:
    """Show last 10 conversation turns."""
    turns = agent.conversation[-10:]
    for msg in turns:
        role = msg.get("role", "?")
        content = str(msg.get("content") or "")[:200]
        color = {"user": "green", "assistant": "blue", "system": "dim", "tool": "yellow"}.get(role, "white")
        console.print(f"[{color}][{role}][/{color}] {content}")


def _cmd_undo(agent: NvidiaAgent) -> None:
    """Remove the last user+assistant turn from conversation history."""
    conv = agent.conversation
    # Walk backwards to remove the last assistant and user messages
    removed = 0
    for role in ("assistant", "user"):
        for i in range(len(conv) - 1, 0, -1):
            if conv[i].get("role") == role:
                conv.pop(i)
                removed += 1
                break
    if removed:
        console.print(f"[green]Undid last turn ({removed} messages removed).[/green]")
    else:
        console.print("[yellow]Nothing to undo.[/yellow]")


def _cmd_retry(agent: NvidiaAgent) -> None:
    """Re-send the last user message."""
    if not agent.last_user_message:
        console.print("[yellow]No previous message to retry.[/yellow]")
        return
    console.print(f"[dim]Retrying: {agent.last_user_message[:100]}[/dim]")
    agent.chat(agent.last_user_message)


def _cmd_cd(arg: str, agent: NvidiaAgent) -> None:
    """Change the working directory."""
    if not arg:
        console.print("[yellow]Usage: /cd <path>[/yellow]")
        return
    try:
        new_path = (get_workdir() / arg).resolve()
        if not new_path.exists():
            console.print(f"[red]Directory not found: {new_path}[/red]")
            return
        if not new_path.is_dir():
            console.print(f"[red]Not a directory: {new_path}[/red]")
            return
        set_workdir(new_path)
        agent.workdir = new_path
        agent.rebuild_system_prompt()
        console.print(f"[green]Working directory: {get_workdir()}[/green]")
    except Exception as ex:
        console.print(f"[red]Error: {ex}[/red]")


# ══ SECTION 9: SETUP WIZARD ═════════════════════════════════════════

def run_setup_wizard() -> AgentConfig:
    """
    Run the interactive first-run setup wizard.
    Returns a fully populated AgentConfig.
    """
    show_welcome_banner()
    config = AgentConfig()
    config.shell = detect_shell()
    config.os_info = f"{platform.system()} {platform.release()}"

    # STEP 1 — API Endpoint
    console.print(Rule("[bold cyan]Step 1 — API Endpoint[/bold cyan]"))
    endpoint_options = [
        "https://integrate.api.nvidia.com/v1/chat/completions",
        "https://integrate.api.nvidia.com/v1",
    ]
    idx = arrow_select("Select NVIDIA NIM API endpoint:", endpoint_options)
    if idx is None:
        console.print("[yellow]Setup cancelled.[/yellow]")
        sys.exit(0)
    chosen_endpoint = endpoint_options[idx]
    # Normalise to always end with /chat/completions
    if not chosen_endpoint.endswith("/chat/completions"):
        chosen_endpoint = chosen_endpoint.rstrip("/") + "/chat/completions"
    config.endpoint = chosen_endpoint
    console.print(f"[green]Endpoint: {config.endpoint}[/green]\n")

    # STEP 2 — API Key
    console.print(Rule("[bold cyan]Step 2 — API Key[/bold cyan]"))
    while True:
        try:
            key = prompt(
                HTML("<bold><ansigreen>Enter your NVIDIA API key: </ansigreen></bold>"),
                is_password=True,
            )
        except (KeyboardInterrupt, EOFError):
            console.print("\n[yellow]Setup cancelled.[/yellow]")
            sys.exit(0)
        key = key.strip()
        if not key:
            console.print("[red]API key cannot be empty. Try again.[/red]")
            continue
        if not key.startswith("nvapi-"):
            console.print("[yellow]Warning: key does not start with 'nvapi-'. Proceeding anyway.[/yellow]")
        config.api_key = key
        console.print(f"[green]Key accepted: {masked_key(key)}[/green]\n")
        break

    # STEP 3 — Model Name
    console.print(Rule("[bold cyan]Step 3 — Model[/bold cyan]"))
    while True:
        try:
            model = prompt(
                HTML("<bold><ansigreen>Model name: </ansigreen></bold>"),
                default=DEFAULT_MODEL,
            )
        except (KeyboardInterrupt, EOFError):
            console.print("\n[yellow]Setup cancelled.[/yellow]")
            sys.exit(0)
        model = model.strip()
        if not model:
            model = DEFAULT_MODEL
        config.model = model
        # Test connectivity
        console.print(f"[dim]Testing connectivity to model '{model}'…[/dim]")
        ok, err = _test_api(config)
        if ok:
            console.print(f"[green]{_e('✅', 'OK')} Model reachable.[/green]\n")
            break
        else:
            console.print(f"[red]{_e('❌', 'ERR')} Connection failed: {err}[/red]")
            console.print("[yellow]Check your API key and model name, then try again.[/yellow]")

    # STEP 4 — Working Directory
    console.print(Rule("[bold cyan]Step 4 — Working Directory[/bold cyan]"))
    chosen_dir = pick_directory(Path.cwd())
    if chosen_dir is None:
        console.print("[yellow]Setup cancelled.[/yellow]")
        sys.exit(0)
    config.workdir = chosen_dir.resolve()
    set_workdir(config.workdir)
    console.print(f"[green]Working directory: {config.workdir}[/green]\n")

    # STEP 5 — Project Context
    console.print(Rule("[bold cyan]Step 5 — Project Context[/bold cyan]"))
    console.print("[dim]Optional: describe your project (max 500 chars). Press Enter to skip.[/dim]")
    try:
        ctx = prompt(HTML("<bold><ansigreen>Project context: </ansigreen></bold>"))
    except (KeyboardInterrupt, EOFError):
        ctx = ""
    ctx = ctx.strip()[:500]
    config.project_context = ctx
    console.print(f"[green]Context: {ctx or '(none)'}[/green]\n")

    # STEP 6 — Confirmation Summary
    console.print(Rule("[bold cyan]Step 6 — Confirmation[/bold cyan]"))
    table = Table(title="Configuration Summary", box=ROUNDED, border_style="cyan")
    table.add_column("Setting", style="bold")
    table.add_column("Value", style="white")
    table.add_row("Endpoint", config.endpoint)
    table.add_row("Model", config.model)
    table.add_row("Working Dir", str(config.workdir))
    table.add_row("Project Context", config.project_context or "(none)")
    table.add_row("Shell", config.shell)
    table.add_row("OS", config.os_info)
    console.print(table)
    console.print()
    try:
        answer = prompt(HTML("<bold><ansigreen>Start agent? [Y/n]: </ansigreen></bold>"))
    except (KeyboardInterrupt, EOFError):
        answer = "n"
    if answer.strip().lower() == "n":
        console.print("[yellow]Exiting.[/yellow]")
        sys.exit(0)
    return config


def _test_api(config: AgentConfig) -> tuple[bool, str]:
    """Send a ping request to verify API connectivity. Returns (success, error_msg)."""
    with console.status("[bold green]Testing API…[/bold green]", spinner="dots"):
        try:
            headers = {
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": config.model,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 8,
                "stream": False,
            }
            resp = requests.post(config.endpoint, headers=headers, json=payload, timeout=30)
            if resp.ok:
                return True, ""
            return False, f"HTTP {resp.status_code}: {resp.text[:300]}"
        except Exception as ex:
            return False, str(ex)


# ══ SECTION 10: MAIN REPL ═══════════════════════════════════════════

def run_repl(agent: NvidiaAgent) -> None:
    """
    Main REPL loop: reads user input, dispatches slash commands or sends to agent.
    """
    console.print(Panel(
        "[bold green]Agent ready.[/bold green] Type your request, or [bold]/help[/bold] for commands.\n"
        "[dim]Alt+Enter for newline  •  Ctrl+D to quit  •  Ctrl+L to clear screen[/dim]",
        border_style="green",
        box=ROUNDED,
    ))

    history = FileHistory(HISTORY_FILE)
    auto_suggest = AutoSuggestFromHistory()

    kb = KeyBindings()

    @kb.add("c-d")
    def _exit(event):
        event.app.exit(exception=EOFError)

    @kb.add("c-l")
    def _clear_screen(event):
        os.system("cls" if sys.platform == "win32" else "clear")
        event.app.current_buffer.reset()

    @kb.add("c-c")
    def _cancel(event):
        event.app.current_buffer.reset()

    pt_style = PTStyle.from_dict({
        "prompt": "bold ansigreen",
    })

    while True:
        try:
            user_input = prompt(
                HTML("<bold><ansigreen>You ❯ </ansigreen></bold>"),
                history=history,
                auto_suggest=auto_suggest,
                multiline=False,
                key_bindings=kb,
                style=pt_style,
            )
        except EOFError:
            _cmd_quit(agent)
            break
        except KeyboardInterrupt:
            console.print("\n[dim]Use Ctrl+D or /quit to exit.[/dim]")
            continue

        user_input = user_input.strip()
        if not user_input:
            continue

        # Check token budget warning
        if agent.total_tokens_used > 100_000:
            console.print(
                "[yellow]Warning: total tokens > 100,000. Consider /compact to free context.[/yellow]"
            )

        # Dispatch slash command or send to agent
        try:
            if user_input.startswith("/"):
                handle_slash_command(user_input, agent)
            else:
                agent.chat(user_input)
        except SystemExit:
            break
        except Exception as ex:
            _handle_crash(ex, agent)


def _handle_crash(ex: Exception, agent: NvidiaAgent) -> None:
    """Handle an unexpected exception gracefully with a red panel and save offer."""
    tb = traceback.format_exc()
    console.print(Panel(
        f"[red]{tb}[/red]",
        title="[bold red]Unexpected Error[/bold red]",
        border_style="red",
    ))
    try:
        answer = prompt(HTML("<ansiyellow>Save conversation to JSON? [y/N]: </ansiyellow>"))
    except Exception:
        answer = "n"
    if answer.strip().lower() == "y":
        _save_conversation_json(agent)


def _save_conversation_json(agent: NvidiaAgent) -> None:
    """Save the current conversation to a JSON file."""
    try:
        fname = f"agent_conversation_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        path = get_workdir() / fname
        with open(path, "w", encoding="utf-8") as f:
            json.dump(agent.conversation, f, indent=2, ensure_ascii=False)
        console.print(f"[green]Conversation saved to: {path}[/green]")
    except Exception as ex:
        console.print(f"[red]Failed to save: {ex}[/red]")


# ══ SECTION 11: ENTRY POINT ══════════════════════════════════════════

def main() -> None:
    """Entry point: run setup wizard, then start the REPL."""
    try:
        config = run_setup_wizard()
        agent = NvidiaAgent(config)
        _log_session(
            f"[{datetime.now().isoformat()}] [SESSION_START] "
            f"model={config.model} workdir={config.workdir}"
        )
        run_repl(agent)
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted. Goodbye![/yellow]")
    except SystemExit:
        pass
    except Exception as ex:
        tb = traceback.format_exc()
        console.print(Panel(
            f"[red]Fatal error during startup:\n{tb}[/red]",
            border_style="red",
            title="[red]Fatal Error[/red]",
        ))
        sys.exit(1)


if __name__ == "__main__":
    main()
