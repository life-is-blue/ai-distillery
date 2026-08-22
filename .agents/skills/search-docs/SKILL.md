---
name: search-docs
description: >-
  Use when users need source-grounded answers from git-library for
  API/tool/configuration/migration/troubleshooting questions, or when they ask
  for library structure/topics overview. Triggers include "search docs",
  "文档检索", "API 文档", "MCP 配置", "migration guide", "troubleshooting",
  and "最新功能".
---

# Search Agentic Knowledge Base

## Goal
Route → retrieve → read source → answer. Minimal calls.

## Config
The producer-owned machine contract is [references/capability-contract.json](references/capability-contract.json); its pinned source, commit, hash, and license are in [references/capability-provenance.json](references/capability-provenance.json). Do not copy numeric policy into this file or infer it from prose. Read the contract when thresholds, budgets, or tool compatibility affect the task.

## Search Response Schema
Each search result contains:
- `path`: Document path within library (e.g., "guides/setup.md")
- `title`: Document title (nullable)
- `summary`: Short document summary (nullable)
- `excerpt`: Matching text excerpt
- `raw_score`: BM25 raw score (nullable; lower = more relevant, e.g., -8.5)
- `display_score`: Normalized 0-100 score (nullable)
- `highlights`: Array of highlighted matching terms
- `library_id`: Library the result belongs to
- `last_updated`: ISO timestamp of last update (mode=recent only)

Wrapper payload includes:
- `total`: Total matches before pagination
- `limit`, `offset`: Pagination params
- `catalog`: Topic map or full catalog (per catalog_mode)
- `hint`: Human-readable summary of results

## Workflow: Navigate → Search → Probe → Fallback

### Navigate (preferred)
Already know the path? `search-docs read LIB_ID/PATH.md`

### Search (default)
1. Route to one primary library (explicit product/library mention wins).
2. Search inside that library:
```bash
search-docs search "QUERY" --library LIBRARY_ID --limit 8 --catalog-mode none
```
3. Check response confidence. Read top 1-3 docs before answering.

### Probe (ambiguity)
No clear library? Run cross-library probe:
```bash
search-docs search "QUERY" --limit 8
```
If results span multiple libraries with close scores under the contract's ambiguity gate:
- Interactive: ask user to clarify.
- Non-interactive: return best + second candidate, mark uncertainty.

### Fallback (once)
Search confidence low? Browse manifest:
```bash
search-docs manifest LIBRARY_ID
```
Navigate topic map → read target doc. No repeated fallback loops.

## Freshness
Queries with "latest/new/recent/最新/刚发布":
```bash
search-docs libraries --fresh-for-query "QUERY"
```
If routing still weak, `search-docs libraries --refresh` then retry once.

## Explore Path
When user asks for structure/topics/coverage (not a concrete answer):
```bash
search-docs libraries
search-docs manifest LIBRARY_ID
```
Deliver: library positioning, topic distribution, recommended starting docs.

## Commands
```bash
search-docs health                                    # connectivity check
search-docs version                                   # bundled contract version
search-docs doctor --offline                          # local integrity check
search-docs libraries                                 # list all libraries
search-docs libraries --fresh-for-query "QUERY"       # freshness-aware list
search-docs search "Q" --library LIB --limit 8        # targeted search
search-docs search "Q" --limit 8                      # cross-library search
search-docs read LIB/PATH.md                          # read full document
search-docs manifest LIB                              # browse topic map
search-docs recent --days 7                           # recent updates
```

## Anti-Patterns
- Starting with cross-library search for every query — route first.
- Answering from snippets without reading source doc.
- Repeating fallback loops beyond one round.
- Treating static routing hints as stronger than live library metadata.
- Continuing after `doctor` reports contract or installation drift.
