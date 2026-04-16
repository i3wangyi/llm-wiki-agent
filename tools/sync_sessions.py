#!/usr/bin/env python3
"""
Sync Claude Code and Gemini CLI session histories into the wiki.

Discovers sessions from:
  ~/.claude/projects/<slug>/*.jsonl          (Claude Code)
  ~/.gemini/tmp/<project-hash>/chats/*.json  (Gemini CLI)

Converts each new session to a structured markdown summary in raw/sessions/,
then optionally triggers wiki ingestion via tools/ingest.py.

Usage:
    python tools/sync_sessions.py                # dry run — show what would be synced
    python tools/sync_sessions.py --ingest       # convert sessions + ingest into wiki
    python tools/sync_sessions.py --all          # include all projects (default: current project only)
    python tools/sync_sessions.py --force        # re-process already-synced sessions
    python tools/sync_sessions.py --no-summary   # skip LLM summarization, dump raw transcript
    python tools/sync_sessions.py --min-turns N  # skip sessions with fewer than N turns (default: 3)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
RAW_SESSIONS_DIR = REPO_ROOT / "raw" / "sessions"
PROCESSED_STATE_FILE = RAW_SESSIONS_DIR / ".processed.json"

CLAUDE_HISTORY_ROOT = Path.home() / ".claude" / "projects"
GEMINI_HISTORY_ROOT = Path.home() / ".gemini" / "tmp"


# ---------------------------------------------------------------------------
# State tracking
# ---------------------------------------------------------------------------

def load_processed() -> set[str]:
    if PROCESSED_STATE_FILE.exists():
        try:
            return set(json.loads(PROCESSED_STATE_FILE.read_text()))
        except (json.JSONDecodeError, OSError):
            return set()
    return set()


def save_processed(processed: set[str]):
    RAW_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_STATE_FILE.write_text(json.dumps(sorted(processed), indent=2))


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------

def call_llm(prompt: str, max_tokens: int = 2048) -> str:
    try:
        from litellm import completion
    except ImportError:
        print("Error: litellm not installed. Run: pip install litellm")
        sys.exit(1)

    model = os.getenv("LLM_MODEL", os.getenv("LLM_MODEL_FAST", "claude-3-5-haiku-latest"))
    response = completion(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
    )
    return response.choices[0].message.content.strip()


# ---------------------------------------------------------------------------
# Claude Code JSONL parsing
# ---------------------------------------------------------------------------

def _extract_text_from_content(content) -> str:
    """Extract readable text from a Claude message content field."""
    if isinstance(content, str):
        # Strip XML-style tags used by slash commands
        text = re.sub(r"<[^>]+>", " ", content).strip()
        return text
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                parts.append(block.get("text", "").strip())
            elif block.get("type") == "tool_use":
                name = block.get("name", "tool")
                parts.append(f"[tool: {name}]")
            # skip thinking, tool_result, image, etc.
        return "\n".join(p for p in parts if p)
    return ""


def parse_claude_jsonl(path: Path) -> dict | None:
    """
    Parse a Claude Code .jsonl session file.

    Returns a dict with keys:
        session_id, cwd, start_time, end_time, source, turns
    or None if the file is unreadable / has fewer turns than min_turns.
    """
    try:
        lines = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    except (OSError, json.JSONDecodeError):
        return None

    session_id = None
    cwd = None
    start_time = None
    end_time = None
    turns = []

    for record in lines:
        rtype = record.get("type")

        # Grab metadata from early records
        if not session_id:
            session_id = record.get("sessionId")
        if not cwd:
            cwd = record.get("cwd")
        ts = record.get("timestamp")
        if ts:
            if not start_time:
                start_time = ts
            end_time = ts

        if rtype == "user":
            msg = record.get("message", {})
            text = _extract_text_from_content(msg.get("content", ""))
            if text:
                turns.append({"role": "user", "text": text})

        elif rtype == "assistant":
            msg = record.get("message", {})
            text = _extract_text_from_content(msg.get("content", []))
            if text:
                turns.append({"role": "assistant", "text": text})

    if not session_id:
        session_id = path.stem

    return {
        "session_id": session_id,
        "cwd": cwd or str(REPO_ROOT),
        "start_time": start_time,
        "end_time": end_time,
        "source": "claude-code",
        "turns": turns,
    }


def find_claude_sessions(current_project_only: bool) -> list[tuple[str, Path]]:
    """
    Returns list of (session_key, path) for Claude Code sessions.
    session_key = "claude:<session_id>"
    """
    if not CLAUDE_HISTORY_ROOT.exists():
        return []

    # Build a slug for the current project directory
    current_slug = "-" + str(REPO_ROOT).replace("/", "-").lstrip("-")

    results = []
    for project_dir in CLAUDE_HISTORY_ROOT.iterdir():
        if not project_dir.is_dir():
            continue
        if current_project_only and project_dir.name != current_slug:
            continue
        for jsonl_file in project_dir.glob("*.jsonl"):
            session_id = jsonl_file.stem
            results.append((f"claude:{session_id}", jsonl_file))

    return results


# ---------------------------------------------------------------------------
# Gemini CLI JSON parsing
# ---------------------------------------------------------------------------

def _gemini_project_hash(cwd: Path) -> str:
    return hashlib.sha256(str(cwd).encode()).hexdigest()


def parse_gemini_json(path: Path) -> dict | None:
    """Parse a Gemini CLI session JSON file."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    session_id = data.get("sessionId", path.stem)
    start_time = data.get("startTime")
    end_time = data.get("lastUpdated")
    messages = data.get("messages", [])

    turns = []
    for msg in messages:
        mtype = msg.get("type", "")
        if mtype == "user":
            content = msg.get("content", [])
            if isinstance(content, list):
                text = " ".join(c.get("text", "") for c in content if isinstance(c, dict)).strip()
            else:
                text = str(content).strip()
            if text:
                turns.append({"role": "user", "text": text})
        elif mtype == "gemini":
            text = msg.get("content", "")
            if isinstance(text, str) and text.strip():
                turns.append({"role": "assistant", "text": text.strip()})

    return {
        "session_id": session_id,
        "cwd": str(REPO_ROOT),
        "start_time": start_time,
        "end_time": end_time,
        "source": "gemini-cli",
        "turns": turns,
    }


def find_gemini_sessions(current_project_only: bool) -> list[tuple[str, Path]]:
    """
    Returns list of (session_key, path) for Gemini CLI sessions.
    session_key = "gemini:<session_id>"
    """
    if not GEMINI_HISTORY_ROOT.exists():
        return []

    current_hash = _gemini_project_hash(REPO_ROOT)

    results = []
    for project_dir in GEMINI_HISTORY_ROOT.iterdir():
        if not project_dir.is_dir():
            continue
        if current_project_only and project_dir.name != current_hash:
            continue
        chats_dir = project_dir / "chats"
        if not chats_dir.exists():
            continue
        for json_file in chats_dir.glob("session-*.json"):
            session_id = json_file.stem
            results.append((f"gemini:{session_id}", json_file))

    return results


# ---------------------------------------------------------------------------
# Markdown generation
# ---------------------------------------------------------------------------

def _format_date(iso_str: str | None) -> str:
    if not iso_str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return iso_str[:10]


def _chunk_turns(turns: list[dict], turns_per_chunk: int = 12) -> list[str]:
    """Split turns into chunks, each rendered as a readable transcript string.

    No truncation — every turn is included in full so nothing is lost.
    """
    chunks = []
    for i in range(0, len(turns), turns_per_chunk):
        slice_ = turns[i : i + turns_per_chunk]
        lines = []
        for t in slice_:
            role = "User" if t["role"] == "user" else "Agent"
            lines.append(f"{role}: {t['text']}")
        chunks.append("\n".join(lines))
    return chunks


def _extract_chunk_notes(chunk_transcript: str, chunk_index: int, total_chunks: int) -> str:
    """Map phase: extract exhaustive bullet-point notes from one chunk."""
    prompt = f"""Extract every notable fact from this excerpt of a coding session (part {chunk_index + 1} of {total_chunks}).

Be exhaustive — capture every decision, insight, problem encountered, tool used, file changed, error seen, approach tried, and next step mentioned. Use bullet points. No synthesis needed yet, just facts.

Transcript:
{chunk_transcript}"""
    return call_llm(prompt, max_tokens=1024)


def summarize_session(session: dict) -> str:
    """Produce a detailed structured wiki page via map-reduce over the full transcript.

    Map:    extract exhaustive bullet notes from each chunk (no truncation)
    Reduce: synthesize all notes into a structured, detail-preserving wiki page
    """
    source_label = "Claude Code" if session["source"] == "claude-code" else "Gemini CLI"
    date_str = _format_date(session["start_time"])
    chunks = _chunk_turns(session["turns"])

    # Map phase — process every chunk, nothing is skipped
    all_notes = []
    for i, chunk in enumerate(chunks):
        notes = _extract_chunk_notes(chunk, i, len(chunks))
        all_notes.append(f"### Part {i + 1}\n{notes}")
    combined_notes = "\n\n".join(all_notes)

    # Reduce phase — structure the extracted notes into a wiki page
    prompt = f"""You are writing a detailed wiki page for a coding session.

Session metadata:
- Agent: {source_label}
- Date: {date_str}
- Project: {session["cwd"]}
- Turns: {len(session["turns"])} across {len(chunks)} parts

Raw extracted notes from every part of the session:
{combined_notes}

Synthesize these into a detailed wiki page. Preserve ALL specific details — file names, error messages, tool names, exact decisions, code patterns. Do not drop any fact that appeared in the notes.

Use this exact structure:

## Goal
What was the main task or question being worked on?

## Key Discussions
Every major topic explored, with specifics (file names, commands, errors, approaches tried).

## Decisions Made
Every concrete choice, approach selected, or conclusion reached. Include reasoning if it appeared.

## Insights & Patterns
Non-obvious discoveries, recurring patterns, lessons learned.

## Next Steps
Everything left to do or explicitly planned.

## Connections
- [[PageName]] — how it relates
(Use TitleCase. Include every concept, tool, person, framework, or project mentioned.)

Return only the markdown sections above, no preamble."""

    return call_llm(prompt, max_tokens=4096)


def session_to_markdown(session: dict, summarize: bool = True) -> str:
    """Convert a parsed session dict to a wiki-ready markdown document."""
    date_str = _format_date(session["start_time"])
    source = session["source"]
    sid = session["session_id"][:8]

    if summarize:
        body = summarize_session(session)
    else:
        # Raw transcript fallback — full text, no truncation
        lines = []
        for t in session["turns"]:
            role = "**User**" if t["role"] == "user" else "**Agent**"
            lines.append(f"{role}: {t['text']}\n")
        body = "\n".join(lines)

    # Infer a short title from the first user turn
    first_user = next((t["text"] for t in session["turns"] if t["role"] == "user"), "")
    title_hint = first_user[:60].strip().rstrip(".,;").replace('"', "'") or "Session"
    title = f"{date_str} Session: {title_hint}"

    frontmatter = f"""---
title: "{title}"
type: source
tags: [session, {source}]
date: {date_str}
source_file: raw/sessions/{date_str}-{source}-{sid}.md
session_id: {session["session_id"]}
---"""

    return f"{frontmatter}\n\n{body.strip()}\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Sync agent session histories into the wiki")
    parser.add_argument("--ingest", action="store_true", help="Ingest generated markdown files into the wiki")
    parser.add_argument("--all", dest="all_projects", action="store_true",
                        help="Include sessions from all projects (default: current project only)")
    parser.add_argument("--force", action="store_true", help="Re-process already-synced sessions")
    parser.add_argument("--no-summary", action="store_true", help="Skip LLM, dump raw transcript")
    parser.add_argument("--min-turns", type=int, default=3,
                        help="Minimum conversation turns to process a session (default: 3)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be processed, no writes")
    args = parser.parse_args()

    current_only = not args.all_projects
    processed = load_processed()

    # Discover sessions
    all_sessions = find_claude_sessions(current_only) + find_gemini_sessions(current_only)

    if not all_sessions:
        print("No sessions found.")
        print(f"  Claude Code: {CLAUDE_HISTORY_ROOT}")
        print(f"  Gemini CLI:  {GEMINI_HISTORY_ROOT}")
        return

    to_process = []
    skipped_processed = 0
    skipped_short = 0

    for key, path in all_sessions:
        if not args.force and key in processed:
            skipped_processed += 1
            continue

        # Parse
        if "claude:" in key:
            session = parse_claude_jsonl(path)
        else:
            session = parse_gemini_json(path)

        if session is None:
            continue

        if len(session["turns"]) < args.min_turns:
            skipped_short += 1
            continue

        to_process.append((key, session))

    print(f"Sessions found: {len(all_sessions)}")
    print(f"  Already processed: {skipped_processed}")
    print(f"  Too short (<{args.min_turns} turns): {skipped_short}")
    print(f"  To process: {len(to_process)}")

    if not to_process:
        print("Nothing new to sync.")
        return

    if args.dry_run:
        for key, session in to_process:
            date_str = _format_date(session["start_time"])
            print(f"  [{session['source']}] {date_str} — {len(session['turns'])} turns — {key}")
        return

    RAW_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    new_files = []

    for i, (key, session) in enumerate(to_process, 1):
        date_str = _format_date(session["start_time"])
        sid = session["session_id"][:8]
        source = session["source"]
        filename = f"{date_str}-{source}-{sid}.md"
        out_path = RAW_SESSIONS_DIR / filename

        summarize = not args.no_summary
        action = "Summarizing" if summarize else "Transcribing"
        print(f"[{i}/{len(to_process)}] {action}: {filename} ({len(session['turns'])} turns)...", end=" ", flush=True)

        try:
            markdown = session_to_markdown(session, summarize=summarize)
            out_path.write_text(markdown, encoding="utf-8")
            processed.add(key)
            new_files.append(out_path)
            print("done")
        except Exception as e:
            print(f"ERROR: {e}")

    save_processed(processed)
    print(f"\nWrote {len(new_files)} session file(s) to raw/sessions/")

    if args.ingest and new_files:
        print("\nIngesting into wiki...")
        ingest_script = REPO_ROOT / "tools" / "ingest.py"
        if not ingest_script.exists():
            print("  tools/ingest.py not found — run /wiki-ingest manually for each file.")
        else:
            for f in new_files:
                rel = f.relative_to(REPO_ROOT)
                print(f"  Ingesting {rel}...")
                result = subprocess.run(
                    [sys.executable, str(ingest_script), str(rel)],
                    cwd=str(REPO_ROOT),
                    capture_output=False,
                )
                if result.returncode != 0:
                    print(f"  [WARN] ingest.py exited with code {result.returncode} for {rel}")


if __name__ == "__main__":
    main()
