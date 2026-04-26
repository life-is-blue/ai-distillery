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
-f FORMAT          Force: claude | gemini | codebuddy | codex (default: auto-detect)
-t TYPE            Output: md | txt | jsonl (default: md)
--role ROLE        Filter: user | assistant | all (default: all)
--no-thoughts      Strip reasoning/thinking blocks
--slop             Show Slop Score per message
```

## Makefile Targets

| Target | What it does |
|--------|-------------|
| `make harvest` | Convert sessions from ~/.gemini, ~/.claude-internal, ~/.codebuddy, ~/.codex → ai-logs/ |
| `make report` | Generate yesterday's daily report |
| `make push` | Push latest report to WeCom webhook |
| `make soul` | Full-context SOUL.md observation extraction (quality-gated + grounded) |
| `make distill` | Distill SOUL.md + LESSONS.md → MEMORY.md rules (structured diff, ≥7 entries threshold) |
| `make lessons` | Extract lessons learned → LESSONS.md (错题本) |
| `make gene-health` | Compute Gene freshness, rebuild registry, output health report |
| `make daily` | Generate daily health report (pure mechanical, no LLM) |
| `make sync-memory` | Commit and push ai-logs/ to ai-memory remote |
| `make test` | Run test suite |
| `make install-cron` | Daily pipeline at 08:47: harvest → report → push → soul → lessons → distill → gene-health → daily → sync-memory |
| `make uninstall-cron` | Remove cron job |

## Architecture

```
ai-distillery/                       ai-logs/ (= ai-memory repo clone)
├── ai_report.py     (pipeline)     ├── MEMORY.md        (rules)
├── ai_engine.py     (LLM backend)  ├── LESSONS.md       (lessons)
├── ai_prompts.py    (prompts)      ├── SOUL.md          (observations)
├── ai_log_converter.py (converter) ├── .genes/          (methodology genes)
├── Makefile         (automation)   ├── reports/         (daily reports)
├── scripts/                        ├── claude/gemini/   (raw session data)
│   └── extract-gene.sh             └── .git/ → (private remote)
└── .agents/skills/
    ├── self-improving/SKILL.md
    └── search-docs/SKILL.md
```

Three files split by change-axis: engine (low freq) / prompts (mid freq) / pipeline (high freq).
ai-logs/ IS the ai-memory repository — all cmd_* write directly, sync-memory commits and pushes.

## ai_report.py

Seven subcommands. Config via `.env` (auto-loaded):

```bash
# .env
LLM_API_KEY=xxx
LLM_BASE_URL=http://...          # optional
LLM_MODEL_NAME=glm-5             # optional
WECOM_WEBHOOK_URL=https://...    # optional, for push
```

- `report [--date YYYY-MM-DD]` — daily work report with precise stats → `ai-logs/reports/{date}.md`
- `push [--logs DIR]` — post latest report to WeCom group (silent if no webhook)
- `soul [--date YYYY-MM-DD] [--since YYYY-MM-DD]` — full-context observation extraction to SOUL.md (quality-gated + LLM grounding)
- `distill [--force] [--soul FILE] [--memory FILE] [--lessons FILE]` — distill SOUL.md + LESSONS.md → MEMORY.md rules (structured diff, Gene promotion suggestions)
- `lessons [--date YYYY-MM-DD] [--lessons FILE]` — extract lessons learned → LESSONS.md (错题本: 坑/因/法 + area tags)
- `gene-health [--genes-dir DIR]` — compute Gene freshness (decay model), rebuild registry.json, output health report
- `daily [--date YYYY-MM-DD]` — daily health report: knowledge summary, promotion candidates, duplicate detection, rule freshness, pipeline health (no LLM)
- `sync-memory [--logs DIR]` — commit and push ai-logs/ to ai-memory remote

## Taste Rules

1. Minimal dependencies — stdlib preferred, vendored stdlib-only modules OK when change-axes diverge
2. Streaming — never `json.load()` a JSONL
3. Silent on success — errors to stderr
4. Idempotent — every script safe to run twice
5. Structure matches change-axes — split files when they evolve at different speeds (engine vs prompts vs pipeline)
