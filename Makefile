.PHONY: test clean harvest report push soul dream distill lessons gene-health daily sync-memory install-cron uninstall-cron backfill-soul setup

LOGS     := $(CURDIR)/ai-memory
CONVERTER := python3 ai_log_converter.py

export AI_LOGS_DIR := $(LOGS)

test:
	python3 tests/test_conversion.py

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

report:
	@python3 ai_report.py report --logs $(LOGS)

push:
	@python3 ai_report.py push --logs $(LOGS)

soul:
	@python3 ai_report.py soul --logs $(LOGS) --soul $(LOGS)/SOUL.md

dream:
	@python3 ai_report.py dream --soul $(LOGS)/SOUL.md

distill:
	@python3 ai_report.py distill --logs $(LOGS) --soul $(LOGS)/SOUL.md --memory $(LOGS)/MEMORY.md --lessons $(LOGS)/LESSONS.md

lessons:
	@python3 ai_report.py lessons --logs $(LOGS) --lessons $(LOGS)/LESSONS.md

gene-health:
	@python3 ai_report.py gene-health --genes-dir $(LOGS)/genes

daily:
	@python3 ai_report.py daily --logs $(LOGS)

sync-memory:
	@python3 ai_report.py sync-memory --logs $(LOGS)

install-cron:
	@(crontab -l 2>/dev/null | grep -v 'ai-distillery-cron'; echo "47 8 * * * cd $(CURDIR) && cd $(LOGS) && git pull --rebase --quiet 2>/dev/null; cd $(CURDIR) && make harvest && make report && make push && make soul && make dream && make lessons && make distill && make gene-health && make daily && make sync-memory >> /tmp/ai-report.log 2>&1 # ai-distillery-cron") | crontab -
	@echo "Cron installed: daily pull+harvest+report+push+soul+dream+lessons+distill+gene-health+daily+sync-memory at 08:47"

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
		printf '# ai-distillery configuration\nLLM_API_KEY=your-api-key-here\n# LLM_BASE_URL=https://api.openai.com/v1\n# LLM_MODEL_NAME=gpt-4o-mini\n# WECOM_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx\n' > .env; \
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
