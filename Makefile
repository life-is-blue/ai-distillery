.PHONY: test clean harvest report push soul distill lessons gene-health daily sync-memory install-cron uninstall-cron

LOGS     := $(CURDIR)/ai-logs
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
			project=$$(echo "$$src" | sed 's|.*/projects/||' | cut -d/ -f1 | sed 's/^-\?[^-]*-home-[^-]*-project-\?//;s/^-//'); \
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
	project=$$(echo "$$src" | sed 's|.*/projects/||' | cut -d/ -f1 | sed 's/^-\?[^-]*-home-[^-]*-project-\?//;s/^-//'); \
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

distill:
	@python3 ai_report.py distill --logs $(LOGS) --soul $(LOGS)/SOUL.md --memory $(LOGS)/MEMORY.md --lessons $(LOGS)/LESSONS.md

lessons:
	@python3 ai_report.py lessons --logs $(LOGS) --lessons $(LOGS)/LESSONS.md

gene-health:
	@python3 ai_report.py gene-health --genes-dir $(LOGS)/.genes

daily:
	@python3 ai_report.py daily --logs $(LOGS)

sync-memory:
	@python3 ai_report.py sync-memory --logs $(LOGS)

install-cron:
	@(crontab -l 2>/dev/null | grep -v 'ai-distillery-cron'; echo "47 8 * * * cd $(CURDIR) && make harvest && make report && make push && make soul && make lessons && make distill && make gene-health && make daily && make sync-memory >> /tmp/ai-report.log 2>&1 # ai-distillery-cron") | crontab -
	@echo "Cron installed: daily harvest+report+push+soul+lessons+distill+gene-health+daily+sync-memory at 08:47"

uninstall-cron:
	@crontab -l 2>/dev/null | grep -v 'ai-distillery-cron' | crontab -
	@echo "Cron removed"
