.PHONY: test clean harvest pull-memory collector leader report push soul dream distill lessons gene-health daily interventions sync-memory install-cron uninstall-cron backfill-soul setup

LOGS     := $(CURDIR)/ai-memory
CONVERTER := python3 ai_log_converter.py

export AI_LOGS_DIR := $(LOGS)

test:
	python3 -m unittest discover -s tests -p 'test_*.py'

clean:
	rm -rf __pycache__ tests/__pycache__

harvest:
	@# --- Gemini ---
	@for src in $(HOME)/.gemini/tmp/*/chats/*.json; do \
		[ -f "$$src" ] || continue; \
		session=$$(basename "$$src" .json); \
		project=$$(basename $$(dirname $$(dirname "$$src"))); \
		tgt=$(LOGS)/gemini/$$project/$$session; \
		[ -f "$$tgt.jsonl" ] && [ "$$tgt.jsonl" -nt "$$src" ] && continue; \
		mkdir -p $$(dirname "$$tgt"); \
		$(CONVERTER) -f gemini "$$src" "$$tgt.md" && \
		$(CONVERTER) -f gemini -t jsonl "$$src" "$$tgt.jsonl" && \
		echo "OK $$tgt" >&2; \
	done
	@# --- Claude (legacy ~/.claude/projects + active ~/.claude-internal/projects) ---
	@for base in $(HOME)/.claude/projects $(HOME)/.claude-internal/projects; do \
		find "$$base" -maxdepth 3 -name '*.jsonl' -not -path '*/subagents/*' 2>/dev/null | while read src; do \
			session=$$(basename "$$src" .jsonl); \
			project=$$(echo "$$src" | sed 's|.*/projects/||' | cut -d/ -f1 | sed 's|^-\?[^-]*-home-[^-]*-project-\?||;s|^Users-[^-]*-Coding-projects-\(active-\)\?||;s|^-||'); \
			project=$${project:-project}; \
			tgt=$(LOGS)/claude/$$project/$$session; \
			[ -f "$$tgt.jsonl" ] && [ "$$tgt.jsonl" -nt "$$src" ] && continue; \
			mkdir -p $$(dirname "$$tgt"); \
			$(CONVERTER) -f claude "$$src" "$$tgt.md" && \
			$(CONVERTER) -f claude -t jsonl "$$src" "$$tgt.jsonl" && \
			echo "OK $$tgt" >&2; \
		done; \
	done
	@# --- tclaude (Tencent internal Claude Code fork, same JSONL schema) ---
	@find $(HOME)/.tclaude/projects -maxdepth 3 -name '*.jsonl' -not -path '*/subagents/*' 2>/dev/null | while read src; do \
		session=$$(basename "$$src" .jsonl); \
		project=$$(echo "$$src" | sed 's|.*/projects/||' | cut -d/ -f1 | sed 's|^-\?[^-]*-home-[^-]*-project-\?||;s|^Users-[^-]*-Coding-projects-\(active-\)\?||;s|^-||'); \
		project=$${project:-project}; \
		tgt=$(LOGS)/tclaude/$$project/$$session; \
		[ -f "$$tgt.jsonl" ] && [ "$$tgt.jsonl" -nt "$$src" ] && continue; \
		mkdir -p $$(dirname "$$tgt"); \
		$(CONVERTER) -f claude "$$src" "$$tgt.md" && \
		$(CONVERTER) -f claude -t jsonl "$$src" "$$tgt.jsonl" && \
		echo "OK $$tgt" >&2; \
	done
	@# --- CodeBuddy ---
	@find $(HOME)/.codebuddy/projects -name '*.jsonl' 2>/dev/null | while read src; do \
		session=$$(basename "$$src" .jsonl); \
	project=$$(echo "$$src" | sed 's|.*/projects/||' | cut -d/ -f1 | sed 's|^-\?[^-]*-home-[^-]*-project-\?||;s|^Users-[^-]*-Coding-projects-\(active-\)\?||;s|^-||'); \
	project=$${project:-project}; \
	tgt=$(LOGS)/codebuddy/$$project/$$session; \
		[ -f "$$tgt.jsonl" ] && [ "$$tgt.jsonl" -nt "$$src" ] && continue; \
		mkdir -p $$(dirname "$$tgt"); \
		$(CONVERTER) -f codebuddy "$$src" "$$tgt.md" && \
		$(CONVERTER) -f codebuddy -t jsonl "$$src" "$$tgt.jsonl" && \
		echo "OK $$tgt" >&2; \
	done
	@# --- Codex ---
	@find $(HOME)/.codex/sessions -name '*.jsonl' 2>/dev/null | while read src; do \
		session=$$(basename "$$src" .jsonl); \
		tgt=$(LOGS)/codex/default/$$session; \
		[ -f "$$tgt.jsonl" ] && [ "$$tgt.jsonl" -nt "$$src" ] && continue; \
		mkdir -p $$(dirname "$$tgt"); \
		$(CONVERTER) -f codex "$$src" "$$tgt.md" && \
		$(CONVERTER) -f codex -t jsonl "$$src" "$$tgt.jsonl" && \
		echo "OK $$tgt" >&2; \
	done
	@# --- Cursor Agent ---
	@find $(HOME)/.cursor/projects -path '*/agent-transcripts/*' -name '*.jsonl' 2>/dev/null | while read src; do \
		session=$$(basename "$$src" .jsonl); \
		project=$$(echo "$$src" | sed 's|.*/projects/||' | cut -d/ -f1 | sed 's|^-\?[^-]*-home-[^-]*-project-\?||;s|^-||'); \
		project=$${project:-project}; \
		tgt=$(LOGS)/cursor/$$project/$$session; \
		[ -f "$$tgt.jsonl" ] && [ "$$tgt.jsonl" -nt "$$src" ] && continue; \
		mkdir -p $$(dirname "$$tgt"); \
		$(CONVERTER) -f cursor "$$src" "$$tgt.md" && \
		$(CONVERTER) -f cursor -t jsonl "$$src" "$$tgt.jsonl" && \
		echo "OK $$tgt" >&2; \
	done
report:
	@python3 ai_report.py report --logs $(LOGS)

push:
	@python3 ai_report.py push --logs $(LOGS)

soul:
	@python3 ai_report.py soul --logs $(LOGS) --soul $(LOGS)/SOUL.md

dream:
	@python3 ai_report.py dream --logs $(LOGS) --soul $(LOGS)/SOUL.md --memory $(LOGS)/MEMORY.md

distill:
	@python3 ai_report.py distill --logs $(LOGS) --soul $(LOGS)/SOUL.md --memory $(LOGS)/MEMORY.md --lessons $(LOGS)/LESSONS.md

lessons:
	@python3 ai_report.py lessons --logs $(LOGS) --lessons $(LOGS)/LESSONS.md

gene-health:
	@python3 ai_report.py gene-health --genes-dir $(LOGS)/genes

# STRICT=1 时 findings 非空则 exit 1（CI/本地强制）。默认软报告：
# cron 链用 { ... }; 连接，硬失败会中断上游；且 findings 指"需要动手的
# 待办"，不含"没来用系统"（无 session / Gene 陈旧是信息，不是故障）。
STRICT ?=
daily:
	@python3 ai_report.py daily --logs $(LOGS) $(if $(STRICT),--strict)

# Autonomy baseline. Deliberately NOT in install-cron: ~0.9 events/day means a
# daily run would be an empty section you learn to skip. Run it when you want a
# reading, and diff the JSON against the previous run.
interventions:
	@python3 ai_report.py interventions --logs $(LOGS)

sync-memory:
	@python3 ai_report.py sync-memory --logs $(LOGS)

pull-memory:
	@test -d '$(LOGS)/.git' || { echo "ERROR: $(LOGS) is not a git repository" >&2; exit 1; }
	@if ! git -C '$(LOGS)' diff --quiet || ! git -C '$(LOGS)' diff --cached --quiet; then \
		echo "ERROR: ai-memory has tracked local changes; sync or resolve them before pulling" >&2; \
		exit 1; \
	fi
	@git -C '$(LOGS)' pull --rebase --quiet

# Collectors may run on many machines: gather uniquely named raw sessions only.
collector:
	@$(MAKE) pull-memory
	@$(MAKE) harvest
	@$(MAKE) sync-memory

# Exactly one leader generates shared derived knowledge. Raw logs are synced
# first; independently completed derived stages are synced even if a later one
# fails, preserving the existing fail-visible cron behavior.
leader:
	@$(MAKE) pull-memory
	@$(MAKE) harvest
	@$(MAKE) sync-memory
	@pipeline_rc=0; \
	{ $(MAKE) report && $(MAKE) push && $(MAKE) soul && $(MAKE) dream && \
	  $(MAKE) lessons && $(MAKE) distill && $(MAKE) gene-health && $(MAKE) daily; \
	} || pipeline_rc=$$?; \
	$(MAKE) sync-memory || exit $$?; \
	exit $$pipeline_rc

CRON_ROLE ?= leader
CRON_HOUR ?= $(if $(filter collector,$(CRON_ROLE)),7,8)
CRON_MINUTE ?= 47

install-cron:
	@cron_role='$(CRON_ROLE)'; cron_hour='$(CRON_HOUR)'; cron_minute='$(CRON_MINUTE)'; \
	case "$$cron_role" in leader|collector) ;; *) echo "ERROR: CRON_ROLE must be leader or collector" >&2; exit 2 ;; esac; \
	case "$$cron_hour" in ''|*[!0-9]*) echo "ERROR: CRON_HOUR must be 0-23" >&2; exit 2 ;; esac; \
	case "$$cron_minute" in ''|*[!0-9]*) echo "ERROR: CRON_MINUTE must be 0-59" >&2; exit 2 ;; esac; \
	[ "$$cron_hour" -le 23 ] || { echo "ERROR: CRON_HOUR must be 0-23" >&2; exit 2; }; \
	[ "$$cron_minute" -le 59 ] || { echo "ERROR: CRON_MINUTE must be 0-59" >&2; exit 2; }; \
	runtime_path="/usr/local/bin:/usr/bin:/bin"; \
	codex_bin=""; \
	for tool in codex python3 git make; do \
		tool_bin=$$(command -v "$$tool" 2>/dev/null || true); \
		[ "$$tool" != codex ] || codex_bin="$$tool_bin"; \
		[ -z "$$tool_bin" ] && continue; \
		tool_dir=$$(dirname "$$tool_bin"); \
		case ":$$runtime_path:" in *":$$tool_dir:"*) ;; *) runtime_path="$$tool_dir:$$runtime_path" ;; esac; \
	done; \
	if ! (crontab -l 2>/dev/null | grep -v 'ai-distillery-cron'; \
	 printf '%s %s * * * export PATH=%s; cd '\''%s'\'' && make %s >> /tmp/ai-report.log 2>&1 # ai-distillery-cron\n' \
	 "$$cron_minute" "$$cron_hour" "$$runtime_path" '$(CURDIR)' "$$cron_role") | crontab -; \
	then \
		echo "ERROR: failed to install cron" >&2; \
		exit 1; \
	fi; \
	if [ -n "$$codex_bin" ]; then \
		echo "Cron installed: $$cron_role at $$cron_hour:$$cron_minute, tool PATH resolved (codex: $$codex_bin); logs: /tmp/ai-report.log"; \
	else \
		echo "Cron installed: $$cron_role at $$cron_hour:$$cron_minute, tool PATH resolved; codex not found, so configure LLM_API_KEY fallback; logs: /tmp/ai-report.log"; \
	fi

uninstall-cron:
	@crontab -l 2>/dev/null | grep -v 'ai-distillery-cron' | crontab -
	@echo "Cron removed"

backfill-soul:
	@echo "Backfilling SOUL.md from historical sessions (top 8 dates by session count)..."
	@python3 -c "\
import sys; sys.path.insert(0, '.'); \
from pathlib import Path; from collections import Counter; \
from ai_report import find_sessions, session_days; \
from datetime import date; \
logs = Path('ai-memory'); \
day_counts = Counter(); \
[day_counts.__setitem__(d, day_counts.get(d, 0) + 1) \
    for p in logs.rglob('*.jsonl') if 'reports' not in p.parts \
    for d in session_days(p)]; \
top_days = sorted(day_counts.items(), key=lambda x: -x[1])[:8]; \
print(f'Top {len(top_days)} dates by session count:'); \
[print(f'  {d}: {n} sessions') for d, n in top_days]; \
open('/tmp/backfill-dates.txt','w').write('\n'.join(str(d) for d,_ in top_days))"
	@while IFS= read -r d; do \
		echo "--- Soul extracting: $$d ---"; \
		python3 ai_report.py soul --date "$$d" --logs $(LOGS) --soul $(LOGS)/SOUL.md || true; \
		sleep 2; \
	done < /tmp/backfill-dates.txt
	@echo "Backfill complete. Run 'make dream' to consolidate."

setup:
	@echo "=== ai-distillery setup ==="
	@echo ""
	@python3 --version || (echo "ERROR: python3 not found" && exit 1)
	@python3 -c "import sys; assert sys.version_info >= (3, 10), f'Need Python 3.10+, got {sys.version}'" || exit 1
	@echo "✓ Python OK"
	@echo ""
	@if [ ! -f .env ]; then \
		echo "Creating .env template..."; \
		printf '# ai-distillery configuration\nLLM_API_KEY=your-api-key-here\n# LLM_BASE_URL=https://api.openai.com/v1\n# LLM_MODEL_NAME=deepseek-v4-flash\n# WECOM_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx\n' > .env; \
		echo "✓ .env created — EDIT IT with your API key before continuing"; \
		echo ""; \
		exit 1; \
	else \
		echo "✓ .env exists"; \
	fi
	@echo ""
	@if [ ! -d ai-memory/.git ]; then \
		echo "WARNING: ai-memory/ is not a git repository."; \
		echo "  To connect to your ai-memory repo:"; \
		echo "    git clone <your-ai-memory-repo-url> ai-memory"; \
		echo "  Or to start fresh:"; \
		echo "    mkdir -p ai-memory && cd ai-memory && git init"; \
		echo ""; \
	else \
		echo "✓ ai-memory/ is a git repo"; \
	fi
	@echo ""
	@python3 -c "from ai_report import main; from ai_prompts import SOUL_SYSTEM; print('✓ Imports OK')"
	@echo ""
	@echo "Installing cron job..."
	@$(MAKE) install-cron
	@echo ""
	@echo "Running initial harvest..."
	@$(MAKE) harvest 2>/dev/null || true
	@echo ""
	@echo "=== Setup complete ==="
	@echo "Next steps:"
	@echo "  1. Edit .env with your LLM API key"
	@echo "  2. Run 'make soul' to test extraction"
	@echo "  3. Run 'make backfill-soul' to process historical data"
	@echo "  4. Cron will run daily at 08:47"
