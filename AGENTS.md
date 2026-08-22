# ai-distillery

Collect personal AI tool logs → readable audit trail → daily report → WeCom push → user mental model → methodology Genes.

## Quick Start

```bash
# Single session conversion
python3 ai_log_converter.py input.jsonl output.md

# Batch harvest all tools (idempotent)
make harvest

# Daily report
python3 ai_report.py report --date 2026-04-03

# Push latest report to WeCom group
python3 ai_report.py push

# Update mental model
python3 ai_report.py soul

# Create a methodology Gene
scripts/extract-gene.sh plan-before-act
```

## Converter Flags

```
-f FORMAT          Force: claude | gemini | codebuddy | codex | cursor | agy (default: auto-detect)
-t TYPE            Output: md | txt | jsonl (default: md)
--role ROLE        Filter: user | assistant | all (default: all)
--no-thoughts      Strip reasoning/thinking blocks
--slop             Show Slop Score per message
```

## Makefile Targets

| Target | What it does |
|--------|-------------|
| `make harvest` | Convert sessions from ~/.gemini (Gemini/Agy), ~/.claude-internal, ~/.tclaude, ~/.codebuddy, ~/.codex, ~/.cursor → ai-memory/ |
| `make pull-memory` | Rebase-pull ai-memory; refuses if it has tracked local changes (sync first) |
| `make collector` | Collector role: pull-memory → harvest → sync-memory. Raw logs only, no LLM |
| `make leader` | Leader role: collector steps + full distillation chain. Exactly one machine runs this |
| `make report` | Generate yesterday's daily report |
| `make push` | Push latest report to WeCom webhook |
| `make soul` | Full-context SOUL.md observation extraction (4 categories: Identity/Preferences/Patterns/Context) |
| `make dream` | Consolidate SOUL.md + MEMORY.md: structured ID-based LLM dedup (candidate-pool remove/merge decisions) + regenerate AGENTS.md |
| `make distill` | Distill SOUL.md + LESSONS.md → MEMORY.md rules (structured diff, ≥7 entries threshold) |
| `make lessons` | Extract lessons learned → LESSONS.md (5 types: trap/toolchain/arch/correction/method) |
| `make gene-health` | Compute Gene freshness, rebuild registry, output health report |
| `make daily` | Generate daily health report (9 sections, pure mechanical, no LLM) |
| `make interventions` | Mine user intervention points → autonomy baseline (mechanical; not in cron) |
| `make sync-memory` | Commit and push ai-memory/ to ai-memory remote |
| `make setup` | New machine deployment: check Python, create .env, verify imports, install cron |
| `make backfill-soul` | Rerun soul extraction on top 8 historical dates (for pk accumulation) |
| `make test` | Run test suite |
| `make install-cron` | Install cron for one role: `CRON_ROLE=leader` (default, 08:47) or `collector` (07:47). Auto-resolves codex/python3/git/make paths into the cron PATH; override time via `CRON_HOUR`/`CRON_MINUTE` |
| `make uninstall-cron` | Remove cron job |

## Architecture

```
ai-distillery/                       ai-memory/ (= ai-memory repo clone)
├── ai_report.py     (pipeline)     ├── MEMORY.md        (rules)
├── ai_engine.py     (LLM backend)  ├── LESSONS.md       (lessons)
├── ai_prompts.py    (prompts)      ├── SOUL.md          (observations)
├── ai_log_converter.py (converter) ├── genes/          (methodology genes)
├── Makefile         (automation)   ├── reports/         (daily reports)
├── scripts/                        ├── claude/gemini/agy/ (raw session data)
│   └── extract-gene.sh             └── .git/ → (private remote)
└── .agents/skills/
    └── self-improving/SKILL.md
```

Three files split by change-axis: engine (low freq) / prompts (mid freq) / pipeline (high freq).
ai-memory/ IS the ai-memory repository — all cmd_* write directly, sync-memory commits and pushes.

## Multi-machine roles

Raw logs are written by many, derived knowledge by exactly one:

- **collector** (any number of machines) — `pull-memory → harvest → sync-memory`. Each machine only
  contributes uniquely-named raw session files, so concurrent writes never collide. No LLM needed.
- **leader** (exactly one machine) — collector steps, then the full chain
  (`report → push → soul → dream → lessons → distill → gene-health → daily`) and a final sync.
  Independently completed stages still sync even if a later stage fails.

Running two leaders would make them fight over the same derived files (SOUL/MEMORY/LESSONS/AGENTS).
`sync-memory` retries a rebase when the remote advanced, which covers concurrent collectors.

## ai_report.py

Ten subcommands. Config via `.env` (auto-loaded):

```bash
# .env
LLM_API_KEY=xxx
LLM_BASE_URL=http://...          # optional
LLM_MODEL_NAME=glm-5             # optional
WECOM_WEBHOOK_URL=https://...    # optional, for push
```

- `report [--date YYYY-MM-DD]` — daily work report with precise stats → `ai-memory/reports/{date}.md`
- `push [--logs DIR]` — post latest report to WeCom group (silent if no webhook)
- `soul [--date YYYY-MM-DD] [--since YYYY-MM-DD]` — full-context observation extraction to SOUL.md (4 categories: Identity/Preferences/Patterns/Context, quality-gated + entry-level grounding)
- `dream [--soul FILE] [--memory FILE]` — consolidate SOUL.md + MEMORY.md via structured ID-based LLM dedup (per-section/layer candidate pool → remove/merge JSON decisions, never a whole-file rewrite), regenerate AGENTS.md from the result
- `distill [--force] [--soul FILE] [--memory FILE] [--lessons FILE]` — distill SOUL.md + LESSONS.md → MEMORY.md rules (structured diff, Gene promotion suggestions)
- `lessons [--date YYYY-MM-DD] [--lessons FILE]` — extract lessons learned → LESSONS.md (错题本: 坑/因/法 + area tags)
- `gene-health [--genes-dir DIR]` — compute Gene freshness (decay model), rebuild registry.json, output health report
- `daily [--date YYYY-MM-DD] [--strict]` — daily health report: knowledge summary, promotion candidates, duplicate detection, rule freshness, pipeline health (no LLM). `--strict` exits 1 if any open findings remain (CI/local enforcement); default stays soft for the cron chain
- `interventions [--since YYYY-MM-DD] [--until YYYY-MM-DD] [--tool NAME] [--samples N] [--json-only]` — mine where the user takes over from the agent → autonomy baseline. Emits `reports/YYYY/MM/interventions-{range}.json` as SSOT plus a rendered `.md`. Mechanical, no LLM. Per-tool hard markers only (cursor/gemini uncovered — reported as such, never as zero interventions); rates are reported per tool because a single global number is dominated by tool mix
- `sync-memory [--logs DIR]` — commit and push ai-memory/ to ai-memory remote

## Taste Rules

1. Minimal dependencies — stdlib preferred, vendored stdlib-only modules OK when change-axes diverge
2. Streaming — never `json.load()` a JSONL
3. Silent on success — errors to stderr
4. Idempotent — every script safe to run twice
5. Structure matches change-axes — split files when they evolve at different speeds (engine vs prompts vs pipeline)
