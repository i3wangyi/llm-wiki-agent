Sync Claude Code and Gemini CLI session histories into the wiki (agent path — no Python needed).

Usage: /wiki-sync-history

This command reads session history files directly and ingests meaningful sessions into the wiki.

---

## Step 1 — Discover unprocessed sessions

Load the state file to know which sessions are already done:
- Read `raw/sessions/.processed.json` (create it as `[]` if it doesn't exist)

Find new Claude Code sessions for this project:
- Derive the project slug from the current working directory: replace each `/` with `-`, then prepend `-` (e.g. `/home/alice/my-wiki` → `-home-alice-my-wiki`)
- List files matching: `~/.claude/projects/<slug>/*.jsonl`
- Skip any session IDs already in .processed.json

Find new Gemini CLI sessions for this project:
- The project hash is SHA256 of the absolute path of the current working directory
- List files matching: `~/.gemini/tmp/<hash>/chats/session-*.json`
- Skip any session IDs already in .processed.json

---

## Step 2 — Parse each session

For **Claude Code JSONL** files (one JSON object per line):
- Collect lines with `"type": "user"` → extract `message.content` as user turn
- Collect lines with `"type": "assistant"` → extract text blocks from `message.content[]` array (skip thinking/tool_use blocks)
- Skip sessions with fewer than 3 user+assistant turns

For **Gemini CLI JSON** files (single JSON object):
- Read `messages[]` array
- `type == "user"`: extract `content[].text`
- `type == "gemini"`: extract `content` string directly
- Skip sessions with fewer than 3 turns

---

## Step 3 — Summarize and write to raw/sessions/

For each session with enough turns, synthesize a structured markdown summary.

Use this frontmatter + structure:

```markdown
---
title: "YYYY-MM-DD Session: <short topic>"
type: source
tags: [session, claude-code|gemini-cli]
date: YYYY-MM-DD
source_file: raw/sessions/<filename>
session_id: <id>
---

## Goal
What was the main task or question being worked on?

## Key Discussions
- Topic 1
- Topic 2

## Decisions Made
- Decision 1 (omit section if none)

## Insights & Patterns
- Insight 1 (omit section if none)

## Next Steps
- Step 1 (omit section if none)

## Connections
- [[ConceptName]] — how it relates
```

Save each file as: `raw/sessions/YYYY-MM-DD-<source>-<session-id-first-8-chars>.md`

---

## Step 4 — Ingest the new session pages

For each new file written in Step 3, run the standard Ingest Workflow (as defined in CLAUDE.md):
1. Create `wiki/sources/<slug>.md`
2. Update `wiki/index.md`
3. Update `wiki/overview.md` if warranted
4. Create/update entity and concept pages
5. Append to `wiki/log.md`

---

## Step 5 — Update the state file

Append all newly processed session IDs to `raw/sessions/.processed.json`.

---

After completing all sessions, print a summary:
- How many sessions were found, skipped (already processed), skipped (too short), processed
- Which files were written
- Which wiki pages were created or updated
