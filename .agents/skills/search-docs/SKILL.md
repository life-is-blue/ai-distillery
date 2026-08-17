---
name: search-docs
description: >-
  Retrieves source-grounded answers from the git-library knowledge base
  by searching and reading official docs. Use when the user asks how a
  documented product, API, SDK, CLI, or MCP tool works; wants official
  docs, setup, configuration, migration, troubleshooting, latest features,
  or a library topic overview; or prefers cited documentation over a
  guessed answer. Typical asks include search docs, 查文档, 文档检索,
  API 文档, 官方文档, 怎么配, MCP 配置, migration guide, troubleshooting,
  排错, 最新功能, and how-does-X-work questions about documented tools.
  Do not use for writing or reviewing application code that is not a
  docs or API question.
allowed-tools: Bash
argument-hint: "[query]"
---

# Search Agentic Knowledge Base

## Goal

Use `search-docs` as the frontend orchestration layer for MCP-backed retrieval:
- Route to the right library first.
- Retrieve with minimal calls.
- Read source docs before final answer.

Project term baseline: git-library is an **AI 智能体知识库 (Agentic Knowledge Base)**.

## Command Contract (Single Entry)

Always use one command:

```bash
search-docs <subcommand> ...
```

Default endpoint is built in: `https://mcp.100100086.xyz`.
Override with `--url` or `GIT_LIBRARY_URL` only when needed.

## Environment Branch

### MCP-aware environment

- If tool description/catalog is already available, do not repeat discovery.
- Go directly to routing and retrieval.

### CLI-only environment

- Run discovery first:

```bash
search-docs libraries
```

- For freshness-sensitive questions (latest/new/recent), use:

```bash
search-docs libraries --fresh-for-query "USER_QUERY"
```

## Workflow (L0/L1/L2)

### L0: Navigate-Direct (preferred)

If catalog/manifest already reveals target doc path or obvious title:

```bash
search-docs read LIBRARY_ID/DOC_PATH.md
```

### L1: Targeted Retrieval

1. Route to one primary library (explicit product/library mention wins).
2. Search inside that library first:

```bash
search-docs search "QUERY" --library LIBRARY_ID --limit 8 --catalog-mode none
```

3. Read top 1-3 candidates before answering.

### L1.5: Ambiguity Gate (before committing single-library answer)

Trigger this gate when all conditions are true:
- no explicit library/product mention from user
- routing confidence is split by distribution (top libraries are close, no clear winner)
- cross-library top results have noticeable title/path overlap (same intent, different docs)

Practical proxy from `search` output:
- if top library candidates are close (`top1-top2` score gap around `<= 0.12`), treat as ambiguous
- if overlap is high (roughly `>= 0.45`) and score gap is still moderate, treat as ambiguous

Action:
1. Run one cross-library probe:

```bash
search-docs search "QUERY" --limit 8
```

2. If top candidates belong to multiple libraries with close confidence:
   - Interactive mode: ask one short clarification question before `read`.
   - Non-interactive mode: return best candidate + second candidate and mark uncertainty.

3. Suppress early-stop in this branch (do not finalize after the first high-looking hit).

### L2: Cross-library Fallback (once)

Only when L1 confidence is low or zero hits:

```bash
search-docs search "QUERY" --limit 8
```

No repeated cross-library loops.

## Confidence Rules

- High: `total > 0` and `top1.display_score >= 0.70`
- Medium: `total > 0` and `0.45 <= top1.display_score < 0.70`
- Low: `total == 0` or `top1.display_score < 0.45`
- Ambiguous: split confidence by distribution (`top1-top2` gap is small) or high cross-library overlap -> enter L1.5

If confidence is low after one retry, provide best candidate + uncertainty + next refinement.
`cli_hint` from CLI output is presentation-only and must not override runtime routing signals.

## Freshness Policy (Important)

For queries containing freshness intent, force library refresh before routing:
- English signals: `latest`, `new`, `recent`, `release`, `changelog`
- Chinese signals: `最新`, `新功能`, `刚发布`, `最近更新`

Use:

```bash
search-docs libraries --fresh-for-query "USER_QUERY"
```

If routing is still weak, run one explicit refresh:

```bash
search-docs libraries --refresh
```

Then retry L1 once before L2 fallback.

## Search vs Explore Paths

### Search path

Use when user asks a concrete question and expects an answer.

Steps:
1. discover (if needed)
2. route primary library
3. L0 or L1 retrieve
4. evaluate confidence
5. L2 fallback once if needed
6. deliver answer with evidence paths

### Explore path

Use when user asks for structure, topics, or coverage.

```bash
search-docs libraries
search-docs manifest LIBRARY_ID
```

Deliver:
- library定位
- topic分布
- 推荐起读路径

## Complexity Guardrails

- Default budget: <= 5 CLI calls per request
- Hard cap: <= 8 CLI calls
- Query variants: <= 2
- Fallback: <= 1 cross-library expansion

## Command Quick Reference

```bash
search-docs health
search-docs libraries
search-docs libraries --refresh
search-docs libraries --fresh-for-query "最新发布的命令"

search-docs search "QUERY" --library LIBRARY_ID --limit 8 --catalog-mode none
search-docs search "QUERY" --limit 8
search-docs search --library LIBRARY_ID -- "--output-format jsonl stream"

search-docs read LIBRARY_ID/DOC_PATH.md
search-docs manifest LIBRARY_ID

search-docs recent --days 7
search-docs recent --library LIBRARY_ID --days 7
```

## Troubleshooting

- Connectivity: `search-docs health`
- Off-topic results: force `--library`, shorten query
- Zero results: rewrite once in primary library, then one L2 fallback
- Self-hosted endpoint: set `--url` or `GIT_LIBRARY_URL`

## Anti-Patterns

- Starting with cross-library search for every query
- Answering from snippets without reading source doc
- Repeating fallback loops beyond one round
- Treating static routing hints as stronger than live library metadata
