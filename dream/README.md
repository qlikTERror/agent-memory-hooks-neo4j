# Dream phase

Offline memory consolidation for the Claude Code hook log. Reads recent
session events from Neo4j, asks Claude to distill them into durable
markdown-style memories, and writes them back as `:Memory` nodes.

The hooks capture *what happened*. The dream phase decides *what's worth
remembering* — user profile, tool-usage patterns, project context — so
future sessions can read it cold.

## Setup

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
# Neo4j env vars are optional; defaults: bolt://localhost:7687, neo4j/password
# export HOOKS_NEO4J_URI=...
# export HOOKS_NEO4J_USER=...
# export HOOKS_NEO4J_PASSWORD=...
# export DREAM_MODEL=claude-opus-4-7   # optional override
```

## Usage

```bash
# Dream over every session with events newer than its last-dreamed watermark
python dream.py

# Dream over a single session
python dream.py --session <session_id>

# Only consider events from the last 24h / 7d / 30m
python dream.py --since 24h

# Print what would be written without touching Neo4j
python dream.py --dry-run
```

## Unified CLI backends on Windows

`dream_unified.py` supports ordered CLI/API backends, for example:

```powershell
python dream\dream_unified.py --backend claude copilot --since 15d --log-file logs\dream\dream.log
```

Copilot is invoked through PowerShell using a `.ps1` shim only. Discovery
checks `COPILOT_BIN` first, then searches `PATH` directly for `copilot.ps1`,
then checks VS Code's Copilot CLI storage under `APPDATA`. Python's
`shutil.which` can miss a `.ps1` file on Windows when `.PS1` is absent from
`PATHEXT`, even though PowerShell's `Get-Command` finds it. No global
`PATHEXT` change is required. To override discovery, set `COPILOT_BIN` to
the full `.ps1` path; `.cmd` and `.bat` shims are not supported.

Logging starts before backend setup. Copilot discovery and MCP-disable
setup run only when Copilot is actually needed, so an unavailable optional
Copilot fallback does not prevent Claude from running. Backend failures
are reported and the next backend is tried; if all fail, the run fails.
With `--log-file logs\dream\dream.log`, output and startup tracebacks are
saved in the Git-ignored `logs\dream` directory as
`dream.YYYY-MM-DD.N.log`. The directory is created automatically. Legacy
paths directly inside the source `dream` directory (including the existing
scheduled task's absolute `dream\dream.log` argument) are redirected to
the repository's `logs\dream` directory. Explicit paths elsewhere are
respected; omitting `--log-file` still logs only to the console.

Logs rotate on date changes and when the active file has reached
`--log-max-mb` (default 5 MB; checked before the next record, so a single
record can exceed the limit). Logs older than `--log-keep-days` (default
30 days) are removed on startup and rollover, including for short scheduled
runs. A nonpositive size or retention value disables that limit.
Existing logs can be moved from `dream` to `logs\dream` with their names
intact, checking for destination collisions first; source code and other
files should not be moved.

Offline regression tests (no LLM calls or Neo4j writes):

```powershell
python -m unittest discover -s dream -p "test_dream*.py"
```

A scheduled task pointing at another checkout continues to use that
checkout's files. Deploy the updated sources there after reviewing the
change; fixing an isolated worktree does not update the live task.

## Schema

Memories imitate markdown files — each `:Memory` node has a `path` and a
`content` field holding the full markdown body (YAML frontmatter + prose).

```
(:Memory {path, content, updated_at})           // path is unique
(:Memory)-[:DERIVED_FROM]->(:Session)
(:Session)-[:DREAMED]->(:Memory)
(:Session {..., last_dreamed_at})                // high-water mark
```

Paths are organized semantically by topic, e.g.:

```
profile/role.md
profile/preferences.md
tools/bash/common-flags.md
tools/edit/conventions.md
project/<short-slug>.md
general/<short-slug>.md
```

## How re-runs work

Each `:Session` carries a `last_dreamed_at` watermark. A session is
re-dreamed when it has events with `timestamp > last_dreamed_at` (or has
never been dreamed). Only the *new* events are sent to the LLM, but the
*full* set of existing memories is passed alongside so the model can merge
new evidence into prior memory bodies.

Memory writes are upserts on `path`: emitting the same path overwrites the
content. The model has no way to delete or rename — for those, edit Neo4j
directly.

## Inspecting

```bash
# List memories
cypher-shell -u neo4j -p password \
  "MATCH (m:Memory) RETURN m.path, m.updated_at ORDER BY m.path"

# Read one
cypher-shell -u neo4j -p password \
  "MATCH (m:Memory {path: 'profile/role.md'}) RETURN m.content"

# Which sessions contributed to a memory
cypher-shell -u neo4j -p password \
  "MATCH (m:Memory {path: 'profile/role.md'})-[:DERIVED_FROM]->(s:Session)
   RETURN s.session_id, s.last_dreamed_at"
```

## Resetting

```cypher
// Re-dream one session from scratch
MATCH (s:Session {session_id: '<id>'}) REMOVE s.last_dreamed_at;

// Wipe all memories and watermarks
MATCH (m:Memory) DETACH DELETE m;
MATCH (s:Session) REMOVE s.last_dreamed_at;
```
