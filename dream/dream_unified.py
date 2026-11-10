#!/usr/bin/env python3
"""
Unified dream phase: read recent session events from Neo4j, ask an LLM to
distill them into durable memories, write them back.

Supports multiple backends with fallback:
  - claude     → Claude Code CLI (claude.exe -p)
  - copilot    → GitHub Copilot CLI (copilot -p)
  - anthropic  → Anthropic API (requires ANTHROPIC_API_KEY)

Usage:
    python dream_unified.py --backend claude              # use Claude Code CLI (default model)
    python dream_unified.py --backend claude-haiku         # Claude with haiku model
    python dream_unified.py --backend copilot              # use Copilot CLI
    python dream_unified.py --backend claude-haiku copilot # haiku first, copilot fallback
    python dream_unified.py --backend anthropic            # use Anthropic API
    python dream_unified.py --since 24h --dry-run          # preview, default backend
    python dream_unified.py --session <id>                 # dream one session

Backend format: <backend>[-<model>]  e.g. claude, claude-haiku, claude-sonnet,
  copilot, copilot-gpt-5.2, anthropic, anthropic-claude-sonnet-4-6
"""

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

from neo4j import GraphDatabase

# ── Neo4j ────────────────────────────────────────────────────────────────────

NEO4J_URI = os.environ.get("HOOKS_NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.environ.get("HOOKS_NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("HOOKS_NEO4J_PASSWORD", "password")

# ── Backend-specific env vars ────────────────────────────────────────────────

CLAUDE_CODE_MODEL = os.environ.get("CLAUDE_CODE_MODEL")
CLAUDE_CODE_BIN = os.environ.get("CLAUDE_CODE_BIN")

COPILOT_MODEL = os.environ.get("COPILOT_MODEL")
COPILOT_BIN = os.environ.get("COPILOT_BIN")

ANTHROPIC_MODEL = os.environ.get("DREAM_MODEL", "claude-opus-4-7")
ANTHROPIC_MAX_TOKENS = 4096
DISABLE_HOOKS_ENV = "AGENT_MEMORY_DISABLE_HOOKS"

DREAM_EFFORT: str | None = None  # set by --effort CLI arg
_COPILOT_MCP_DISABLE_ARGS: list[str] | None = None


def _normalized_effort() -> str:
    """Enforce a minimum effort of low.

    - missing effort -> low
    - none/low -> low
    - medium/high/xhigh/max -> unchanged
    """
    effort = (DREAM_EFFORT or "").strip().lower()
    if effort in ("", "none", "low"):
        return "low"
    if effort in ("medium", "high", "xhigh", "max"):
        return effort
    return "low"

# ── Settings cache ───────────────────────────────────────────────────────────

_SETTINGS_PATH = os.path.join(tempfile.gettempdir(), ".dream-settings.json")
_COPILOT_ALLOWED_TOOLS = "view"


def _load_settings() -> dict:
    if os.path.isfile(_SETTINGS_PATH):
        try:
            with open(_SETTINGS_PATH, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_settings(data: dict) -> None:
    with open(_SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _model_family(model: str) -> tuple:
    """Extract word-parts as family key, e.g. 'gpt-5.4-mini' -> ('gpt','mini')."""
    return tuple(p for p in re.split(r'[-.]', model.lower()) if not p.isdigit())


def _model_version(model: str) -> tuple:
    """Extract version numbers, e.g. 'gpt-5.4-mini' -> (5, 4)."""
    return tuple(int(p) for p in re.split(r'[-.]', model.lower()) if p.isdigit())


def _version_sort_key(model: str) -> tuple:
    """Oldest-first sort: version numbers then family name."""
    return (_model_version(model), _model_family(model))


def _display_sort(models: list[str]) -> list[str]:
    """Sort for display: group by family, families ordered by their highest
    version (desc), shortest family name first as tiebreaker, within each
    family versions go highest to lowest."""
    from itertools import groupby
    # Build family -> models mapping, each family sorted version-desc
    fam_map: dict[tuple, list[str]] = {}
    for m in models:
        fam = _model_family(m)
        fam_map.setdefault(fam, []).append(m)
    for fam in fam_map:
        fam_map[fam].sort(key=_model_version, reverse=True)
    # Sort families: highest max-version desc, then shortest name first
    sorted_fams = sorted(
        fam_map.keys(),
        key=lambda f: (max(_model_version(m) for m in fam_map[f]),),
        reverse=True,
    )
    # Tiebreak: shortest family name first (stable within same max-version)
    result = []
    for fam in sorted_fams:
        result.extend(fam_map[fam])
    return result


def _discover_copilot_models() -> list[str]:
    """Ask copilot CLI for available models, parse the JSON array from output."""
    cmd = _detect_copilot_command() + [
        "-p", "list all available model ids as a JSON array, nothing else",
        "--silent", "--output-format", "json",
        "--no-custom-instructions",
        f"--available-tools={_COPILOT_ALLOWED_TOOLS}",
    ] + _get_copilot_mcp_disable_args()
    env = os.environ.copy()
    env[DISABLE_HOOKS_ENV] = "1"
    with tempfile.TemporaryDirectory(prefix="dream-copilot-models-") as td:
        env["COPILOT_HOME"] = td
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", cwd=td, env=env)
    if proc.returncode != 0:
        raise RuntimeError(f"copilot model discovery failed (rc={proc.returncode})")
    # Extract JSON array from JSONL output — look for assistant.message with content
    text = ""
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if obj.get("type") == "assistant.message" and obj.get("data", {}).get("content"):
                text = obj["data"]["content"]
        except json.JSONDecodeError:
            continue
    if not text:
        text = proc.stdout or ""
    # Extract JSON array from text (may have markdown fences or surrounding prose)
    m = re.search(r'\[[\s\S]*?\]', text)
    if m:
        try:
            models = json.loads(m.group())
            if isinstance(models, list) and all(isinstance(x, str) for x in models):
                return sorted(models, key=_version_sort_key)
        except json.JSONDecodeError:
            pass
    raise RuntimeError(f"could not parse model list from copilot output: {text[:300]}")


def _get_models(backend: str) -> list[str]:
    """Return cached models for backend, discovering if missing."""
    if backend == "claude":
        return ["haiku", "sonnet", "opus"]  # Claude accepts aliases directly
    settings = _load_settings()
    key = f"{backend}_models"
    if key in settings and isinstance(settings[key], list):
        return sorted(settings[key], key=_version_sort_key)
    if backend == "copilot":
        print(f"  discovering {backend} models ...", flush=True)
        models = _discover_copilot_models()
        settings[key] = models
        _save_settings(settings)
        print(f"  cached {len(models)} models in {_SETTINGS_PATH}")
        return models
    return []


def _word_match(model: str, query: str) -> bool:
    """Check if all word-components of query appear in model.
    e.g. 'gpt-mini' matches 'gpt-5-mini' and 'gpt-5.4-mini'."""
    # Split on hyphens, dots, and version boundaries
    q_parts = set(re.split(r'[-.]', query.lower()))
    m_parts = set(re.split(r'[-.]', model.lower()))
    return q_parts.issubset(m_parts)


def _resolve_model(backend: str, model: str | None) -> str | None:
    """Resolve model name. Word-component matching, picks oldest version on ambiguity."""
    if model is None:
        return None
    if backend == "claude":
        return model  # Claude accepts aliases directly
    models = _get_models(backend)  # already version-sorted (oldest first)
    if model in models:
        return model
    # Word-component match — pick the first (oldest) match
    matches = [m for m in models if _word_match(m, model)]
    if matches:
        return matches[0]
    # Substring fallback
    matches = [m for m in models if model in m]
    if matches:
        return matches[0]
    # Invalidate cache and retry
    print(f"  model {model!r} not in cache, refreshing ...", flush=True)
    settings = _load_settings()
    settings.pop(f"{backend}_models", None)
    _save_settings(settings)
    models = _get_models(backend)
    if model in models:
        return model
    matches = [m for m in models if _word_match(m, model)]
    if matches:
        return matches[0]
    matches = [m for m in models if model in m]
    if matches:
        return matches[0]
    raise RuntimeError(f"model {model!r} not available for {backend}. Available: {models}")


# ── Shared ───────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are the dream phase for a coding-memory system.
You receive chronological hook events plus existing markdown memories.
Distill durable memories that help future sessions.

Each memory imitates a markdown file with a path and full markdown body
(frontmatter + prose). Organize by topic, e.g.:

  profile/role.md
  profile/preferences.md
  tools/bash/common-flags.md
  tools/edit/conventions.md
  project/<short-slug>.md
  general/<short-slug>.md

Output STRICT JSON only:

{
  "memories": [
    {
      "path": "profile/role.md",
      "content": "---\\ntitle: User role\\nkind: profile\\n---\\n\\n<markdown body>"
    }
  ]
}

Frontmatter must include title and kind (profile, tool, project, general).

Rules:
- If a memory path already exists, return a full UPDATED body merged with new evidence.
- Remove contradictions, avoid duplicates.
- Skip ephemeral details and obvious repo facts.
- Prefer fewer, sharper memories.
- If nothing is worth remembering, return {"memories": []}.
- Each memory must stand alone.
- IMPORTANT: Do NOT use any tools. Respond with JSON text only.
"""

MAX_PROMPT_CHARS = 500_000  # ~125K tokens; both Claude and Copilot CLI pass via file, no shell limit
VALID_BACKENDS = ("claude", "copilot", "anthropic")


# ── Neo4j helpers ────────────────────────────────────────────────────────────

def get_driver():
    return GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))


def parse_since(s: str) -> datetime:
    m = re.fullmatch(r"(\d+)([hdm])", s)
    if not m:
        raise ValueError(f"--since must look like '24h', '7d', '30m'; got {s!r}")
    n, unit = int(m.group(1)), m.group(2)
    delta = {"h": timedelta(hours=n), "d": timedelta(days=n), "m": timedelta(minutes=n)}[unit]
    return datetime.now(timezone.utc) - delta


def fetch_events(driver, session_id: str | None, since: datetime | None):
    where, params = ["(s.last_dreamed_at IS NULL OR e.timestamp > s.last_dreamed_at)"], {}
    if session_id:
        where.append("s.session_id = $session_id")
        params["session_id"] = session_id
    if since:
        where.append("e.timestamp >= $since")
        params["since"] = since.isoformat()

    query = f"""
    MATCH (s:Session)-[:FIRST_EVENT|NEXT*0..]->(e:Event)
    WHERE {' AND '.join(where)}
    RETURN s.session_id AS session_id, e
    ORDER BY s.session_id, e.timestamp
    """
    grouped: dict[str, list] = {}
    with driver.session() as ses:
        for record in ses.run(query, **params):
            grouped.setdefault(record["session_id"], []).append(dict(record["e"]))
    return list(grouped.items())


def fetch_existing_memories(driver) -> list[dict]:
    with driver.session() as ses:
        result = ses.run("MATCH (m:Memory) RETURN m.path AS path, m.content AS content ORDER BY path")
        return [dict(r) for r in result]


def render_events(events: list[dict]) -> str:
    lines = []
    for e in events:
        head = f"[{e.get('timestamp', '?')}] {e.get('event_name', '?')}"
        if e.get("tool_name"):
            head += f" tool={e['tool_name']}"
        lines.append(head)
        if e.get("prompt"):
            lines.append(f"  prompt: {e['prompt'][:500]}")
        if e.get("tool_input"):
            lines.append(f"  input:  {str(e['tool_input'])[:500]}")
        if e.get("tool_response"):
            lines.append(f"  output: {str(e['tool_response'])[:500]}")
    return "\n".join(lines)


def render_existing(memories: list[dict]) -> str:
    if not memories:
        return "(no existing memories)"
    parts = []
    for m in memories:
        parts.append(f"### {m['path']}\n```\n{m['content']}\n```")
    return "\n\n".join(parts)


def write_memories(driver, session_id: str, memories: list[dict], watermark: str) -> int:
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        {"path": m["path"], "content": m["content"], "updated_at": now}
        for m in memories
        if m.get("path") and m.get("content")
    ]
    with driver.session() as ses:
        ses.run("CREATE CONSTRAINT IF NOT EXISTS FOR (m:Memory) REQUIRE m.path IS UNIQUE")
        ses.run(
            """
            MATCH (s:Session {session_id: $session_id})
            SET s.last_dreamed_at = $watermark
            WITH s
            UNWIND $rows AS row
            MERGE (m:Memory {path: row.path})
            SET m.content = row.content, m.updated_at = row.updated_at
            MERGE (s)-[:DREAMED]->(m)
            MERGE (m)-[:DERIVED_FROM]->(s)
            """,
            session_id=session_id,
            watermark=watermark,
            rows=rows,
        )
    return len(rows)


# ── Prompt builders ──────────────────────────────────────────────────────────

def _build_user_prompt(transcript: str, existing: str) -> str:
    # Trim existing memories first (less important), keep as much transcript as possible.
    budget = MAX_PROMPT_CHARS
    existing_budget = min(len(existing), budget // 4)
    transcript_budget = budget - existing_budget
    trimmed_existing = existing[-existing_budget:] if len(existing) > existing_budget else existing
    trimmed_transcript = transcript[-transcript_budget:] if len(transcript) > transcript_budget else transcript
    return (
        f"<existing_memories>\n{trimmed_existing}\n</existing_memories>\n\n"
        f"<events>\n{trimmed_transcript}\n</events>"
    )


def _parse_memories_json(text: str, backend_name: str) -> list[dict]:
    text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON object in {backend_name} output: {text[:200]}")
    return json.loads(text[start : end + 1]).get("memories", [])


def _build_json_repair_prompt(raw_text: str) -> str:
    trimmed = raw_text.strip()
    if len(trimmed) > 12000:
        trimmed = trimmed[:12000]
    return (
        "Convert the following assistant output into STRICT JSON only.\n"
        "Return exactly this schema and nothing else:\n"
        "{\"memories\": [{\"path\": \"...\", \"content\": \"...\"}]}\n"
        "If there is no valid memory content, return {\"memories\": []}.\n\n"
        "Output to convert:\n"
        "<assistant_output>\n"
        f"{trimmed}\n"
        "</assistant_output>"
    )


def _parse_memories_with_repair(
    text: str,
    backend_name: str,
    repair_call: callable,
) -> list[dict]:
    try:
        return _parse_memories_json(text, backend_name)
    except Exception as first_error:
        repaired = repair_call(_build_json_repair_prompt(text))
        try:
            return _parse_memories_json(repaired, backend_name)
        except Exception as second_error:
            raise RuntimeError(
                f"{backend_name} output was not valid JSON after one repair attempt. "
                f"first_error={first_error}; second_error={second_error}"
            )


# ── Backend: Claude Code CLI ────────────────────────────────────────────────

def _detect_claude_command() -> list[str]:
    if CLAUDE_CODE_BIN:
        return [CLAUDE_CODE_BIN]
    home = os.environ.get("USERPROFILE") or os.environ.get("HOME", "")
    local_bin = os.path.join(home, ".local", "bin", "claude.exe")
    if os.path.isfile(local_bin):
        return [local_bin]
    resolved = shutil.which("claude") or shutil.which("claude.exe")
    if resolved:
        return [resolved]
    appdata = os.environ.get("APPDATA", "")
    claude_code_dir = os.path.join(appdata, "Claude", "claude-code")
    if os.path.isdir(claude_code_dir):
        for entry in sorted(os.listdir(claude_code_dir), reverse=True):
            candidate = os.path.join(claude_code_dir, entry, "claude.exe")
            if os.path.isfile(candidate):
                return [candidate]
    raise RuntimeError("Could not find Claude Code CLI. Set CLAUDE_CODE_BIN or install via 'irm https://claude.ai/install.ps1 | iex'.")


def _call_claude_cli(user_prompt: str, model: str | None = None) -> str:
    # Run from a temp directory with hooks overridden to empty so neither
    # project-level nor user-level hooks interfere with the dream session.
    isolated_cwd = tempfile.mkdtemp(prefix="dream-claude-cwd-")
    try:
        system_file = os.path.join(isolated_cwd, "dream_system.txt")
        with open(system_file, "w", encoding="utf-8") as f:
            f.write(SYSTEM_PROMPT)

        settings_file = os.path.join(isolated_cwd, "dream_settings.json")
        with open(settings_file, "w", encoding="utf-8") as f:
            json.dump({"hooks": {}}, f)

        cmd = _detect_claude_command() + [
            "-p", "-",
            "--system-prompt-file", system_file,
            "--output-format", "json",
            "--no-session-persistence",
            "--settings", settings_file,
            "--max-turns", "3",
        ]
        if model:
            cmd += ["--model", model]
        elif CLAUDE_CODE_MODEL:
            cmd += ["--model", CLAUDE_CODE_MODEL]
        else:
            cmd += ["--model", "haiku"]  # default to cheapest

        cmd += ["--effort", _normalized_effort()]

        env = os.environ.copy()
        env[DISABLE_HOOKS_ENV] = "1"

        proc = subprocess.run(
            cmd,
            input=user_prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=isolated_cwd,
            env=env,
        )
    finally:
        shutil.rmtree(isolated_cwd, ignore_errors=True)

    if proc.returncode != 0:
        safe_cmd = " ".join(shlex.quote(c) for c in cmd[:8]) + " ..."
        message = (
            f"Claude Code CLI failed (rc={proc.returncode}): {safe_cmd}\n"
            f"stderr: {(proc.stderr or '').strip()}\n"
            f"stdout: {(proc.stdout or '').strip()[:500]}"
        )
        status_code = _extract_api_error_status(proc.stdout)
        if status_code == 429:
            raise BackendRateLimitedError("claude", status_code, message)
        raise RuntimeError(
            message
        )

    try:
        obj = json.loads(proc.stdout)
        return obj.get("result", "")
    except json.JSONDecodeError:
        for line in (proc.stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if "result" in obj:
                    return obj["result"]
            except json.JSONDecodeError:
                continue
        raise RuntimeError(f"Could not parse Claude Code CLI output: {(proc.stdout or '')[:500]}")


def call_claude(transcript: str, existing: str, model: str | None = None) -> list[dict]:
    text = _call_claude_cli(_build_user_prompt(transcript, existing), model=model)
    return _parse_memories_with_repair(
        text,
        "claude",
        lambda prompt: _call_claude_cli(prompt, model=model),
    )


# ── Backend: Copilot CLI ────────────────────────────────────────────────────

def _detect_copilot_command() -> list[str]:
    if COPILOT_BIN:
        if COPILOT_BIN.lower().endswith(".ps1"):
            return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", COPILOT_BIN]
        raise RuntimeError("COPILOT_BIN must point to a .ps1 shim for this script")

    # PowerShell-only resolution. Do not fallback to cmd/bat shims.
    # shutil.which applies PATHEXT on Windows, which commonly excludes .PS1.
    resolved = None
    for directory in os.get_exec_path():
        candidate = os.path.join(directory.strip('"'), "copilot.ps1")
        if os.path.isfile(candidate):
            resolved = os.path.abspath(candidate)
            break
    if not resolved:
        appdata = os.environ.get("APPDATA", "")
        candidate = os.path.join(appdata, "Code", "User", "globalStorage", "github.copilot-chat", "copilotCli", "copilot.ps1")
        if os.path.isfile(candidate):
            resolved = candidate

    if resolved:
        return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", resolved]

    raise RuntimeError("Could not find copilot.ps1. Set COPILOT_BIN to the copilot PowerShell shim.")


def _discover_copilot_mcp_server_names() -> list[str]:
    """List MCP server names once via `copilot mcp list --json`.

    Returns [] if listing fails or schema is unexpected.
    """
    cmd = _detect_copilot_command() + ["mcp", "list", "--json"]
    with tempfile.TemporaryDirectory(prefix="dream-copilot-mcp-list-") as td:
        env = os.environ.copy()
        env[DISABLE_HOOKS_ENV] = "1"
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=td,
            env=env,
        )
    if proc.returncode != 0:
        return []

    payload = (proc.stdout or "").strip()
    if not payload:
        return []

    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        start, end = payload.find("{"), payload.rfind("}")
        if start == -1 or end == -1:
            return []
        try:
            obj = json.loads(payload[start : end + 1])
        except json.JSONDecodeError:
            return []

    servers = obj.get("mcpServers") if isinstance(obj, dict) else None
    if not isinstance(servers, dict):
        return []
    return sorted({name for name in servers.keys() if isinstance(name, str) and name})


def _get_copilot_mcp_disable_args() -> list[str]:
    """Build and cache Copilot MCP disable flags once per process."""
    global _COPILOT_MCP_DISABLE_ARGS
    if _COPILOT_MCP_DISABLE_ARGS is not None:
        return _COPILOT_MCP_DISABLE_ARGS

    args = ["--disable-builtin-mcps"]
    names = _discover_copilot_mcp_server_names()
    for name in names:
        args += ["--disable-mcp-server", name]

    _COPILOT_MCP_DISABLE_ARGS = args
    return _COPILOT_MCP_DISABLE_ARGS


def _call_copilot_cli(user_prompt: str, model: str | None = None) -> str:
    # Isolate in temp COPILOT_HOME + temp cwd and disable MCP/tools so dream runs
    # as a pure text task.
    env = os.environ.copy()
    env[DISABLE_HOOKS_ENV] = "1"
    detected_cmd = _detect_copilot_command()
    with tempfile.TemporaryDirectory(prefix="dream-copilot-home-") as isolated_home:
        with tempfile.TemporaryDirectory(prefix="dream-copilot-cwd-") as isolated_cwd:
            env["COPILOT_HOME"] = isolated_home
            resolved_model = model or COPILOT_MODEL
            # Copilot doesn't have --system-prompt, so bake it into the user message.
            full_prompt = f"{SYSTEM_PROMPT}\n\n{user_prompt}"
            prompt_file = os.path.join(isolated_cwd, "dream_prompt.txt")
            with open(prompt_file, "w", encoding="utf-8") as f:
                f.write(full_prompt)
            # Keep @file relative so Copilot can resolve it from cwd via the view tool.
            prompt_arg = "@dream_prompt.txt"

            cmd = detected_cmd + [
                "-p", prompt_arg,
                "--silent",
                "--output-format", "json",
                "--no-custom-instructions",
                f"--add-dir={isolated_cwd}",
                "--allow-tool=view",
                f"--available-tools={_COPILOT_ALLOWED_TOOLS}",
            ] + _get_copilot_mcp_disable_args()
            if resolved_model:
                cmd += ["--model", resolved_model]
            elif COPILOT_MODEL:
                cmd += ["--model", COPILOT_MODEL]

            cmd += ["--effort", _normalized_effort()]

            def _run(current_cmd: list[str]) -> subprocess.CompletedProcess:
                return subprocess.run(
                    current_cmd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    cwd=isolated_cwd,
                    env=env,
                )

            proc = _run(cmd)
            # Some Copilot models (e.g. gpt-5-mini) reject --effort none.
            # If that still appears, retry once with low explicitly.
            if proc.returncode != 0 and _normalized_effort() in ("none", "low"):
                merged = f"{proc.stderr or ''}\n{proc.stdout or ''}"
                if "Unsupported value: 'none'" in merged:
                    retry_cmd = cmd[:]
                    replaced = False
                    for i in range(len(retry_cmd) - 1):
                        if retry_cmd[i] == "--effort":
                            retry_cmd[i + 1] = "low"
                            replaced = True
                            break
                    if not replaced:
                        retry_cmd += ["--effort", "low"]
                    proc = _run(retry_cmd)
                    cmd = retry_cmd

    if proc.returncode != 0:
        safe_cmd = " ".join(shlex.quote(c) for c in cmd[:8]) + " ..."
        message = (
            f"Copilot CLI failed (rc={proc.returncode}): {safe_cmd}\n"
            f"stderr: {(proc.stderr or '').strip()}\n"
            f"stdout: {(proc.stdout or '').strip()[:500]}"
        )
        status_code = _extract_api_error_status(proc.stdout)
        if status_code == 429:
            raise BackendRateLimitedError("copilot", status_code, message)
        raise RuntimeError(
            message
        )

    # Copilot JSON mode emits JSONL events; extract final assistant message content.
    last_content = ""
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "assistant.message":
            data = obj.get("data") or {}
            if isinstance(data.get("content"), str):
                last_content = data["content"]

    if not last_content:
        raise RuntimeError("Copilot CLI output did not contain an assistant.message payload")
    return last_content


def call_copilot(transcript: str, existing: str, model: str | None = None) -> list[dict]:
    text = _call_copilot_cli(_build_user_prompt(transcript, existing), model=model)
    return _parse_memories_with_repair(
        text,
        "copilot",
        lambda prompt: _call_copilot_cli(prompt, model=model),
    )


# ── Backend: Anthropic API ──────────────────────────────────────────────────

def call_anthropic(transcript: str, existing: str, model: str | None = None) -> list[dict]:
    try:
        from anthropic import Anthropic
    except ImportError:
        raise RuntimeError("anthropic package not installed. Run: pip install anthropic")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY is not set")

    client = Anthropic()
    user_msg = _build_user_prompt(transcript, existing)
    msg = client.messages.create(
        model=model or ANTHROPIC_MODEL,
        max_tokens=ANTHROPIC_MAX_TOKENS,
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_msg}],
    )
    text = "".join(b.text for b in msg.content if b.type == "text")
    return _parse_memories_with_repair(
        text,
        "anthropic",
        lambda prompt: "".join(
            b.text
            for b in client.messages.create(
                model=model or ANTHROPIC_MODEL,
                max_tokens=ANTHROPIC_MAX_TOKENS,
                system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": prompt}],
            ).content
            if b.type == "text"
        ),
    )


# ── Dispatch with fallback ──────────────────────────────────────────────────

BACKEND_MAP = {
    "claude": call_claude,
    "copilot": call_copilot,
    "anthropic": call_anthropic,
}

UNAVAILABLE_BACKENDS_THIS_RUN: set[str] = set()


class BackendRateLimitedError(RuntimeError):
    def __init__(self, backend_label: str, status_code: int, message: str):
        super().__init__(message)
        self.backend_label = backend_label
        self.status_code = status_code


def _iter_json_objects(text: str) -> list[dict]:
    objects: list[dict] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            objects.append(obj)
    return objects


def _extract_api_error_status(text: str) -> int | None:
    for obj in _iter_json_objects(text):
        status = obj.get("api_error_status")
        if isinstance(status, int):
            return status
    return None


def _parse_backend_spec(spec: str) -> tuple[str, str | None]:
    """Parse 'backend-model' into (backend, model). e.g. 'claude-haiku' -> ('claude', 'haiku')."""
    for prefix in BACKEND_MAP:
        if spec == prefix:
            return prefix, None
        if spec.startswith(prefix + "-"):
            return prefix, spec[len(prefix) + 1:]
    raise ValueError(f"unknown backend spec: {spec!r}. Valid prefixes: {', '.join(BACKEND_MAP)}")


def _backend_run_key(backend: str, model: str | None) -> str:
    return f"{backend}:{model or ''}"


def call_with_fallback(backend_specs: list[str], transcript: str, existing: str) -> list[dict]:
    last_error = None
    for spec in backend_specs:
        backend, model = _parse_backend_spec(spec)
        label = spec
        try:
            model = _resolve_model(backend, model)
            label = f"{backend}({model})" if model else spec
            run_key = _backend_run_key(backend, model)
            if run_key in UNAVAILABLE_BACKENDS_THIS_RUN:
                print(f"  skipping backend: {label} (previously returned 429 in this run)", file=sys.stderr)
                continue
            print(f"  trying backend: {label} ...", flush=True)
            result = BACKEND_MAP[backend](transcript, existing, model=model)
            print(f"  ✓ {label} succeeded")
            return result
        except Exception as e:
            last_error = e
            print(f"  ✗ {label} failed: {e}", file=sys.stderr)
            if isinstance(e, BackendRateLimitedError) and e.status_code == 429:
                UNAVAILABLE_BACKENDS_THIS_RUN.add(_backend_run_key(backend, model))
                print(f"  marking {label} unavailable for the rest of this run (429)", file=sys.stderr)
            if spec != backend_specs[-1]:
                print(f"  falling back to next backend ...", file=sys.stderr)
    raise RuntimeError(f"all backends failed. Last error: {last_error}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Dream phase with multi-backend fallback.")
    ap.add_argument(
        "--backend", nargs="+", default=["claude"],
        help="Backend(s) with optional model: claude, claude-haiku, copilot, copilot-gpt-5.2, anthropic. First is primary, rest are fallbacks.",
    )
    ap.add_argument("--session", help="dream over a single session_id")
    ap.add_argument("--since", help="only include events newer than e.g. 24h, 7d, 30m")
    ap.add_argument("--effort", choices=["none", "low", "medium", "high", "xhigh", "max"],
                    default=None, help="reasoning effort level for CLI backends")
    ap.add_argument("--list-models", nargs="*", metavar="BACKEND",
                    help="list available models for given backends (or all if none specified) and exit")
    ap.add_argument("--dry-run", action="store_true", help="print memories, don't write")
    ap.add_argument("--log-file", default=None, help="log file path (enables file logging; paths in dream are redirected to logs/dream)")
    ap.add_argument("--log-max-mb", type=float, default=5.0, help="max log file size in MB before rotation")
    ap.add_argument("--log-keep-days", type=int, default=30, help="delete rotated logs older than N days")
    args = ap.parse_args()

    from dream_logging import setup_logging
    setup_logging(log_file=args.log_file, max_bytes=int(args.log_max_mb * 1024 * 1024), keep_days=args.log_keep_days)

    if args.list_models is not None:
        backends = args.list_models if args.list_models else ["claude", "copilot"]
        for b in backends:
            models = _display_sort(_get_models(b))
            print(f"{b}: {', '.join(models)}")
        return

    if args.effort:
        global DREAM_EFFORT
        DREAM_EFFORT = args.effort

    since = parse_since(args.since) if args.since else None
    driver = get_driver()
    try:
        sessions = fetch_events(driver, args.session, since)
        if not sessions:
            print("nothing to dream about.")
            return
        existing = render_existing(fetch_existing_memories(driver))
        for session_id, events in sessions:
            print(f"\n=== dreaming over {session_id} ({len(events)} new events) ===")
            memories = call_with_fallback(args.backend, render_events(events), existing)
            for m in memories:
                print(f"\n--- {m.get('path')} ---")
                print(m.get("content", ""))
            if not args.dry_run:
                watermark = events[-1].get("timestamp")
                n = write_memories(driver, session_id, memories, watermark)
                print(f"\n  wrote/updated {n} memories; watermark -> {watermark}")
    finally:
        driver.close()


if __name__ == "__main__":

    main()
