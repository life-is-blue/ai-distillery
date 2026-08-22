#!/usr/bin/env python3
"""
ai_report.py — LLM-powered daily report + soul model builder + distillation pipeline.

Subcommands:
  report       Daily work report with precise stats
  push         Post latest report to WeCom group
  soul         Full-context observation extraction → SOUL.md
  lessons      Extract lessons learned → LESSONS.md
  distill      Distill SOUL + LESSONS → MEMORY.md rules
  dream        Consolidate SOUL.md: merge duplicates, prune stale entries
  gene-health  Compute Gene freshness, rebuild registry
  interventions Mine user intervention points → autonomy baseline (mechanical)
  sync-memory  Commit and push ai-memory/ to remote

Config via .env (auto-loaded):
  LLM_API_KEY           API key (required for report/soul/lessons/distill)
  LLM_BASE_URL          OpenAI-compatible endpoint (default: https://api.openai.com/v1)
  LLM_MODEL_NAME        Model name (default: deepseek-v4-flash)
  LLM_MAX_TOKENS        Max tokens for LLM response (default: 2000)
  WECOM_WEBHOOK_URL     WeCom group robot webhook (optional, for push)
  AI_LOGS_DIR           Memory directory (default: ./ai-memory)
"""
import argparse, hashlib, json, os, re, subprocess, sys
from collections import deque
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from ai_engine import load_dotenv, call_engine, _codex_available
from ai_prompts import (
    REPORT_SYSTEM, SOUL_SYSTEM, DISTILL_SYSTEM, GROUNDING_SYSTEM,
    LESSONS_SYSTEM, SOUL_SKELETON, LESSONS_SKELETON, MEMORY_SKELETON,
    SOUL_DEDUP_SYSTEM, MEMORY_DEDUP_SYSTEM, AGENTS_SYSTEM, GENE_REVIEW_SYSTEM,
    INTERACTION_SYSTEM,
)


load_dotenv()


# ---------------------------------------------------------------------------
# LLM output parsing helpers
# ---------------------------------------------------------------------------

def _parse_skipped_section(raw: str) -> tuple:
    """Split LLM output into (main_content, skipped_items).

    The ## Skipped section at the end of lessons/distill output is metadata
    for the skip-buffer — it should NOT be written to LESSONS.md or MEMORY.md.
    Returns (main_text, list_of_dicts) where each dict has at least a "reason" key.
    """
    if not raw:
        return raw, []
    # Find ## Skipped section (must be a top-level ## heading)
    skip_match = re.search(r'(?:^|\n)## Skipped\n', raw)
    if not skip_match:
        return raw, []
    main_part = raw[:skip_match.start()].rstrip()
    skip_part = raw[skip_match.end():].strip()
    if not skip_part or skip_part.lower() == "none":
        return main_part, []
    items = []
    for line in skip_part.splitlines():
        line = line.strip()
        if not line or line.lower() == "none":
            continue
        # Strip leading "- "
        if line.startswith("- "):
            line = line[2:]
        # Try "slug: reason" format
        if ": " in line:
            slug, reason = line.split(": ", 1)
            items.append({"slug": slug.strip(), "reason": reason.strip()})
        else:
            items.append({"reason": line})
    return main_part, items


# ---------------------------------------------------------------------------
# Skip-buffer helpers: shared by cmd_lessons, cmd_distill, cmd_daily
# ---------------------------------------------------------------------------

def _skip_buffer_path(logs_dir: Path) -> Path:
    return logs_dir / ".skip-buffer.jsonl"


def append_skip(logs_dir: Path, source: str, items: list):
    """Append skip entries to buffer. source: 'lessons'|'distill'|'gene'."""
    if not items:
        return
    path = _skip_buffer_path(logs_dir)
    today = str(date.today())
    with path.open("a", encoding="utf-8") as f:
        for item in items:
            entry = {"date": today, "source": source, **item}
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_and_clear_skip_buffer(logs_dir: Path) -> list:
    """Read all skip entries for today, then clear the file."""
    path = _skip_buffer_path(logs_dir)
    if not path.exists():
        return []
    today = str(date.today())
    all_lines = path.read_text(encoding="utf-8").splitlines()
    entries = []
    other_lines = []
    for line in all_lines:
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("date") == today:
            entries.append(e)
        else:
            other_lines.append(line)
    # Clear today's entries; keep other days (rare)
    if other_lines:
        path.write_text("\n".join(other_lines) + "\n", encoding="utf-8")
    elif path.exists():
        path.unlink()
    return entries


def _ts_to_date(ts) -> date | None:
    """Parse meta.timestamp (int millis/seconds or ISO string) to a local-time date."""
    if ts is None or isinstance(ts, bool):
        return None
    try:
        if isinstance(ts, (int, float)):
            return datetime.fromtimestamp(ts / 1000 if ts >= 1e12 else ts).date()
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone()  # convert UTC/aware → local before taking date
        return dt.date()
    except (ValueError, OSError, OverflowError):
        return None


def session_days(path: Path) -> set[date]:
    """Every local date with at least one message. Mtime fallback if no timestamps found."""
    days: set[date] = set()
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                d = _ts_to_date((obj.get("meta") or {}).get("timestamp"))
                if d:
                    days.add(d)
    except OSError:
        pass
    if not days:
        try:
            days.add(datetime.fromtimestamp(path.stat().st_mtime).date())
        except OSError:
            pass
    return days


def _report_month_dir(reports_dir: Path, d: date) -> Path:
    """reports/ is partitioned reports/YYYY/MM/ to keep any one directory small."""
    return reports_dir / f"{d:%Y}" / f"{d:%m}"


def find_sessions(logs_dir: Path, target_date: date = None) -> list[Path]:
    results = []
    for p in sorted(logs_dir.rglob("*.jsonl")):
        if "reports" in p.parts:
            continue
        # Real sessions are always logs_dir/tool/project/session.jsonl (3 path parts).
        # Root-level jsonl files (e.g. .skip-buffer.jsonl) aren't sessions.
        if len(p.relative_to(logs_dir).parts) < 3:
            continue
        if target_date and target_date not in session_days(p):
            continue
        results.append(p)
    return results


def extract_turns(path: Path, max_chars: int = 2000, target_date: date = None, tail: bool = False) -> str:
    turns, total = [], 0
    try:
        with open(path, encoding="utf-8") as f:
            all_entries = []
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                role = obj.get("role", "")
                if role not in ("user", "assistant"):
                    continue
                if target_date is not None:
                    d = _ts_to_date((obj.get("meta") or {}).get("timestamp"))
                    if d != target_date:
                        continue
                content = obj.get("content", "")
                if isinstance(content, list):
                    content = " ".join(i.get("text", "") for i in content if i.get("type") == "text")
                if not isinstance(content, str):  # defensive: handles null/int in malformed JSONL
                    continue
                # User turns get full 500 chars; assistant truncated to 200
                # User intent is the gold signal for both reports and soul modeling
                limit = 500 if role == "user" else 200
                entry = f"[{role}] {content[:limit]}"
                if tail:
                    all_entries.append(entry)
                else:
                    total += len(entry)
                    turns.append(entry)
                    if total > max_chars:
                        break
    except OSError:
        return ""
    if tail:
        # Take last N entries that fit within max_chars (bug fixes tend to be at tail)
        result, total = [], 0
        for entry in reversed(all_entries):
            total += len(entry)
            if total > max_chars:
                break
            result.append(entry)
        return "\n".join(reversed(result))
    return "\n".join(turns)



def extract_interaction_turns(path: Path, max_chars: int = 6000, target_date: date = None) -> str:
    """Extract turns PRESERVING adjacency, for interaction-pattern extraction.

    Unlike extract_turns() (which flattens to isolated [user]/[assistant] lines
    for report/soul purposes), this keeps the conversational structure:
    [user] → [assistant] → [user] — because question/counter-question patterns
    only exist in the adjacency between turns, not in isolated messages.

    Budget allocation is INVERTED vs extract_turns(): assistant turns are
    truncated harder (120 chars — just enough context to see what the user is
    responding to), while user turns get the full budget (800 chars). Here the
    user message IS the payload — a counter-question lives inside it — whereas
    in report/soul the assistant summary was the payload.

    Tool-only turns are dropped from the transcript (they carry no linguistic
    signal) but still COUNT toward adjacency, so a [user] question that follows
    a tool_call is preserved in position.
    """
    turns, total = [], 0
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                role = obj.get("role", "")
                if role not in ("user", "assistant"):
                    continue
                if target_date is not None:
                    d = _ts_to_date((obj.get("meta") or {}).get("timestamp"))
                    if d != target_date:
                        continue
                content = obj.get("content", "")
                if isinstance(content, list):
                    texts = [i.get("text", "") for i in content if i.get("type") == "text"]
                    content = " ".join(texts)
                if not isinstance(content, str):
                    continue
                content = content.strip()
                if not content:
                    continue
                if role == "user":
                    limit = 800
                    entry = f"[user] {content[:limit]}"
                else:
                    limit = 120
                    entry = f"[assistant] {content[:limit]}"
                total += len(entry)
                if total > max_chars:
                    break
                turns.append(entry)
    except OSError:
        return ""
    return "\n".join(turns)


# ---------------------------------------------------------------------------
# Intervention mining (mechanical, no LLM)
#
# Measures where the user actually takes over from the agent, so the autonomy
# envelope can be derived from data instead of guessed. Every marker below was
# verified against the real corpus — see the notes on each for why it is shaped
# this way. Do not "simplify" these into a fuzzy keyword list; that was tried
# and measured, and it matched prose ("It rejects mixed content"), stack traces
# (processTicksAndRejections) and pasted git output far more often than real
# interventions.
# ---------------------------------------------------------------------------

# Per-tool hard markers. Full phrases only — the bare word "rejected" has ~538
# corpus hits that are overwhelmingly `API Error: Request rejected (429)` and
# `[remote rejected] main -> main`, i.e. infrastructure, not governance.
#
# NOTE the rejection marker lives in a tool_result block, and in codebuddy it
# arrives under role="tool" rather than role="user". Filtering on role="user"
# or reading only type=="text" blocks silently drops all 18 codebuddy hits —
# and codebuddy is 299 of the 660 sessions.
_CLAUDE_MARKERS = (
    ("interrupted", "[Request interrupted by user]"),
    ("tool_rejected", "The user doesn't want to proceed with this tool use"),
)
_HARD_MARKERS = {
    "claude": _CLAUDE_MARKERS,
    "tclaude": _CLAUDE_MARKERS,
    "codebuddy": _CLAUDE_MARKERS,
    "codex": (("turn_aborted", "<turn_aborted>"),),
    # cursor/gemini have no known marker. An honest zero beats a fabricated
    # number — cmd_interventions reports them as uncovered.
    "cursor": (),
    "gemini": (),
}

# Positive approval markers. Claude Code's plan flow emits a *rejection*
# tool_result and then the approval re-prompt, so an ExitPlanMode "interrupt"
# is usually an approval wearing a rejection costume. Checking only the
# adjacent message misclassifies these; look forward for these markers instead.
_APPROVE_MARKER = "User has approved your plan"
_IMPL_MARKER = "Implement the following plan"

# The rejection tool_result often embeds what the user said instead. This is
# the highest-quality directive signal in the corpus (32 events).
_INLINE_DIRECTIVE = "To tell you how to proceed, the user said:"

# Harness plumbing that is not user intent. Without this filter these get
# classified as directives (e.g. "The user wants to clarify these questions"
# is Claude Code's own AskUserQuestion re-prompt).
_SYNTHETIC_PREFIXES = (
    "<command-name>", "<command-message>", "<command-args>", "<local-command",
    "<system-reminder", "<task-notification>", "<user-prompt-submit-hook>",
    "<turn_aborted>", "# agents.md instructions",
    "caveat:", "this session is being continued", "copied to clipboard",
    "the user wants to clarify these questions", "[request interrupted",
    "api error:", "set model to", "ran git ", "worked for ",
)

# Attribution window: how far back to look for the tool_call being interrupted.
# 8 resolves all but ~19 events, and those are genuinely tool-less (plain text
# turn + Esc). Widening starts attributing an interrupt to an unrelated tool
# many turns back, which is worse than an honest "(none)".
_ATTRIB_WINDOW = 8
_DIRECTIVE_WINDOW = 20   # forward messages to search for what the user said
_APPROVE_WINDOW = 30     # forward messages to search for the approval marker

# Classification keyword sets, derived from the corpus rather than invented.
#
# The push-forward class is the counter-intuitive finding: the largest single
# action bucket (Bash) is dominated by the user shoving the agent forward
# ("不用核实,我已经配置了,执行先") rather than restraining it. The envelope
# implication is the opposite of what "intervention" suggests.
_PUSH_RE = re.compile(
    r"不用(核实|验证|检查|测试|管|问|了)|我已经(配置|改好|设置|装|部署|授权|提交)"
    r"|直接(执行|干|上|推|跑|技能|使用)|执行先|(推送|提交)吧|我授权|授权了|别问|干吧"
    r"|按流程处理|没问题就|就这样吧|(请|你)?继续|开始(吧|合并)|确认[.。]?$|同意"
    r"|本地切换先|别想复杂了"
)
_STOP_RE = re.compile(
    r"不对|不是这样|方向(不对|错)|停下|先别|回退|回滚|撤销|revert"
    r"|错了|搞错|坏品味|搞复杂|过度设计"
)
# Politeness guard: 要不要/好不好 are *suggestions* ("要不要也优化一下"), the
# opposite polarity of a correction. Measured: 11 of 104 naive soft-tier hits
# were these, i.e. a sign error.
_POLITE_RE = re.compile(r"要不要|好不好|是不是可以|能不能")
_QUESTION_RE = re.compile(r"为什么|是不是|你觉得|怎么(样|办|知道)|[?？]")

# Endorsement prefix. Measured on 36 plan_rejected events: 8 open with
# "方向没问题" / "同意方案 A 的方向", i.e. the plan WAS approved and the user is
# appending a delegation. Counting those as rejections inflated the plan
# rejection rate to 64% when the true figure is ~9%. A rejection marker plus an
# endorsement is an approval, not a veto.
_ENDORSE_RE = re.compile(
    r"(方向|方案|逻辑|思路)(没(问题|错)|ok|OK|可以)|^同意|没问题就|方向 ?ok"
)

# Hand-labelled taxonomy for what would otherwise be one opaque catch-all.
# Derived by reading all 66 catch-all samples and having the user rule on each
# group; the `counted` flag is the user's own call on whether better agent
# behaviour would have removed the need to speak at all.
#
# Order matters — first match wins, most specific first. Kept as data rather
# than branching logic so a future relabelling is a table edit, not a rewrite.
_TAXONOMY = (
    # (class, counts_as_intervention, pattern)
    ("delegate_directed", True, re.compile(
        r"让 ?codex|codebuddy|派遣|explore agent|gemini-frontend"
        r"|你负责规划|你(来)?(负责)?(进行)?验收")),
    ("plan_first", True, re.compile(
        r"先规划|谋定而后动|真的理解|第一性原理|苏格拉底|审视看看计划"
        r"|不用硬凑|输出整体计划|规划一下|下一步该做什么")),
    ("simpler_path", True, re.compile(
        r"更简单|不用这么麻烦|更合适|算了[,，]|不要着急|先跑起来|效率高很多"
        r"|直接拉取|不用删了|我来处理")),
    ("defect_report", True, re.compile(
        r"不清晰|过于杂乱|还是慢|又出现|重复的问题|方向 ?ok|保持好品味|乱七八糟"
        r"|质量问题|不明确的地方|重复表头|阻塞点|怎么回事|还没搞定|不是已经完成")),
    ("context_supplied", True, re.compile(
        r"我刚刚下载|另外一个项目|都登录完了|使用: ?http|访问 localhost"
        r"|加入 git-library|放到项目根目录|你的同事在本地|我本地也配置|我删了")),
    # Below: the user ruled these NORMAL COLLABORATION. Asking for status is how
    # they choose to work, and handing over a new task is not a correction —
    # counting either would turn "reduce interventions" into "make the user
    # speak less", which is the wrong objective.
    ("progress_query", False, re.compile(r"进度到哪里|总结我们完成|报告当前|现在进度")),
)

# Short acknowledgements and harness echo that carry no directive content.
_EMPTY_REPLY_RE = re.compile(
    r"^(你好|可以|可以的|好的|收到|嗯|ok|OK)[.。!！]?$|^\[?\x1b?\[?2m|Compacted \(ctrl"
    r"|^Switch model to|^No plan file found|^<environment_context>"
    r"|^The deployment was blocked|^Verify \d"
)


def _msg_parts(obj: dict) -> tuple[str, str, list[str]]:
    """Split one normalized message into (text, tool_result_text, tool_call_names).

    Handles `content` as either a list of blocks or a bare string. Reads BOTH
    text and tool_result blocks — the rejection markers live in tool_result.
    """
    content = obj.get("content", "")
    if isinstance(content, str):
        return content, "", []
    if not isinstance(content, list):
        return "", "", []
    texts, results, tools = [], [], []
    for b in content:
        if not isinstance(b, dict):
            continue
        btype = b.get("type")
        if btype == "text":
            t = b.get("text")
            if isinstance(t, str):
                texts.append(t)
        elif btype == "tool_result":
            c = b.get("content")
            results.append(c if isinstance(c, str) else json.dumps(c, ensure_ascii=False))
        elif btype == "tool_call":
            name = b.get("name")
            if isinstance(name, str) and name:
                tools.append(name)
    return " ".join(texts), " ".join(results), tools


def _is_synthetic(text: str) -> bool:
    """True if text is harness plumbing rather than something the user typed."""
    low = text.strip().lower()
    if not low:
        return True
    return low.startswith(_SYNTHETIC_PREFIXES)


def _classify_directive(directive: str) -> str:
    """Map a recovered user directive to a decision class.

    Order: empty -> push-forward -> stop -> hand-labelled taxonomy -> question
    -> catch-all. Push and stop come first because they are the two classes with
    a clear envelope implication; the taxonomy then resolves what used to be an
    opaque 32% catch-all.
    """
    if not directive:
        return "unresolved"
    if _EMPTY_REPLY_RE.search(directive.strip()):
        return "noise_reply"
    if _PUSH_RE.search(directive):
        return "over_verification"
    if _STOP_RE.search(directive) and not _POLITE_RE.search(directive):
        return "wrong_direction"
    for klass, _counted, pattern in _TAXONOMY:
        if pattern.search(directive):
            return klass
    if _QUESTION_RE.search(directive):
        return "counter_question"
    return "new_task"


def scan_session_interventions(path: Path, tool: str) -> list[dict]:
    """Stream one session, returning one record per intervention event.

    Single pass with a bounded lookback deque and a pending-resolution queue —
    the largest session is 7.2MB/3775 lines, so the file is never materialized.
    Each pending event resolves forward for its directive and for the approval
    marker, then finalizes once its windows expire.
    """
    markers = _HARD_MARKERS.get(tool, ())
    if not markers:
        return []

    lookback: deque = deque(maxlen=_ATTRIB_WINDOW)
    pending: list[dict] = []
    done: list[dict] = []
    prev_was_marker = False
    idx = -1

    def _finalize(rec: dict) -> None:
        # The IMPL marker is checked globally, not just under ExitPlanMode:
        # measured, it also follows Edit and no-tool actions, and it always
        # means the plan was approved.
        if rec["plan_approved"] or _IMPL_MARKER in rec["directive"]:
            rec["klass"] = "plan_approved"
        elif rec["infra"]:
            rec["klass"] = "infra_noise"
        elif rec["action"] == "ExitPlanMode" and _ENDORSE_RE.search(rec["directive"]):
            # "方向没问题, 让 codex 评审下计划" — the plan passed; what follows is a
            # delegation, not a veto. Route to the taxonomy so the delegation
            # itself is what gets counted.
            rec["klass"] = _classify_directive(rec["directive"])
            if rec["klass"] in ("new_task", "counter_question"):
                rec["klass"] = "plan_endorsed"
        elif rec["action"] == "ExitPlanMode":
            # A plan interrupted without endorsement counts as incomplete, and
            # that INCLUDES design questions ("是不是这个更适合放 tags") — per the
            # user's own ruling, those are things the plan should have settled
            # before asking for approval. Only carve out the cases that say
            # nothing about plan quality: pure delegation, pure context drops,
            # and events whose directive was never recoverable.
            sub = _classify_directive(rec["directive"])
            rec["klass"] = (sub if sub in ("delegate_directed", "context_supplied",
                                           "unresolved", "noise_reply")
                            else "plan_rejected")
        else:
            rec["klass"] = _classify_directive(rec["directive"])
        done.append(rec)

    try:
        fh = open(path, encoding="utf-8")
    except OSError:
        return []
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            idx += 1
            role = obj.get("role", "") or ""
            text, results, tools = _msg_parts(obj)
            blob = f"{text}\n{results}"

            hit = next((label for label, needle in markers if needle in blob), None)

            # Resolve pending events against this message before anything else.
            # A message bearing a marker never supplies a directive — for codex
            # the marker sits in the user text itself, so it would otherwise
            # become its own directive.
            still_pending = []
            for rec in pending:
                rec["seen"] += 1
                # Approval only counts while the event is still unanswered. The
                # forward window is wide enough to catch a *later, unrelated*
                # plan approval, which would wrongly excuse this interruption.
                if _APPROVE_MARKER in blob and not rec["directive"]:
                    rec["plan_approved"] = True
                if (not rec["directive"] and role == "user" and not hit
                        and rec["seen"] <= _DIRECTIVE_WINDOW
                        and text.strip() and not _is_synthetic(text)):
                    rec["directive"] = text.strip()
                if rec["seen"] >= _APPROVE_WINDOW:
                    _finalize(rec)
                else:
                    still_pending.append(rec)
            pending = still_pending

            if hit:
                # Claude emits the rejection tool_result and the interrupt text
                # back to back; collapse the pair into one event.
                if not prev_was_marker:
                    action = "(none)"
                    for prev_role, prev_tools, _ in reversed(lookback):
                        if prev_role.startswith("assistant") and prev_tools:
                            action = prev_tools[0]
                            break
                    infra = any(
                        pt.lstrip().startswith("API Error")
                        for pr, _, pt in reversed(lookback)
                        if pr.startswith("assistant")
                    )
                    directive = ""
                    if _INLINE_DIRECTIVE in results:
                        inline = results.split(_INLINE_DIRECTIVE, 1)[1].strip()
                        if inline and not _is_synthetic(inline):
                            directive = inline
                    pending.append({
                        "marker": hit, "action": action, "directive": directive,
                        "infra": infra, "plan_approved": False, "seen": 0,
                        "msg": idx, "tool": tool,
                    })
                prev_was_marker = True
            else:
                # Only assistant/tool activity breaks a marker run; a bare
                # continuation shouldn't merge two genuinely separate events.
                prev_was_marker = False

            lookback.append((role, tools, text))

    for rec in pending:
        _finalize(rec)
    return done


def quality_gate(observations: str) -> str:
    """Filter out low-signal observation bullets. Returns empty string if nothing survives."""
    REJECT_PATTERNS = [
        r"数据不足", r"无实质性", r"无法提取", r"样本有限",
        r"仅包含.*?/clear", r"仅包含.*?/resume", r"无实质性交互",
        r"需要更多.*?消息才能构建", r"(?:推测|初步判断|大概率)(?:使用|为|是)",
    ]
    lines = observations.strip().splitlines()
    kept = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            kept.append(line)
            continue
        if any(re.search(p, stripped) for p in REJECT_PATTERNS):
            continue
        # Bullets with <8 chars of actual content after stripping bold markers are noise
        text = re.sub(r"\*\*.*?\*\*[：:]?\s*", "", stripped.lstrip("- "))
        if len(text) < 8:
            continue
        kept.append(line)
    # If no bullet points survived, return empty
    if not any(l.strip().startswith("-") for l in kept):
        return ""
    return "\n".join(kept).strip()


def grounding_check(observations: str, user_turns: str) -> str:
    """Verify structured SOUL observations against user messages.

    Operates at ENTRY level (not line level):
    - Parses observations into (section, entry) pairs using _parse_soul_sections.
    - Each entry may span multiple lines (e.g., Preferences with Why/How).
    - How: fields are skipped — they are AI instructions, not user claims.
    - Preferences entries whose How: field is < 15 chars are rejected (quality gate).
    - Sends the grounding-relevant claim for each entry to the LLM.
    - Returns full entries (including How:, pk tags, new: tags) for GROUNDED ones.
    - Preserves section headers in output so _merge_soul_entry can parse it.

    Backward compatible: if observations has no ## section headers, falls back
    to the original line-by-line behaviour.
    """
    if not observations.strip() or not user_turns.strip():
        return ""

    # LLM fallback (small context) can't handle unbounded user_turns —
    # grounding requires observations + user_turns in one atomic call.
    # Codex exec (128K) handles full context; fallback truncates to 20K.
    if not _codex_available() and len(user_turns) > 20000:
        print(f"Grounding: user_turns truncated from {len(user_turns)} to 20000 (LLM fallback)", file=sys.stderr)
        user_turns = user_turns[:20000]

    # ── Backward-compat: old flat-bullet format (no ## section headers) ──
    if not re.search(r"^## ", observations, re.M):
        return _grounding_check_legacy(observations, user_turns)

    # ── Structured format: entry-aware grounding ──────────────────────────
    _PK_RE = re.compile(r'\s*<!--\s*pk:\s*[\w-]+\s*-->')
    SECTION_ORDER = ("Identity", "Preferences", "Patterns", "Context", "Interaction")

    def _extract_claim(section_name: str, entry: str) -> str | None:
        """Return grounding claim for entry, or None if it fails quality gate."""
        entry_lines = entry.split("\n")

        # Quality gate: Preferences.How: must be >= 15 chars
        if section_name == "Preferences":
            how_line = next(
                (l for l in entry_lines if re.match(r'\s+How:', l)), None
            )
            if how_line:
                how_text = re.sub(r'^\s*How:\s*', "", how_line).strip()
                if len(how_text) < 15:
                    return None  # too vague — quality gate rejects

        # Build claim: strip How: lines and pk tags
        claim_lines = []
        for line in entry_lines:
            if re.match(r'\s+How:', line):
                continue  # How: is an AI instruction, not a user claim
            clean = _PK_RE.sub("", line)
            claim_lines.append(clean)
        return "\n".join(claim_lines).strip()

    # Collect (section, entry, claim) triples — entries with None claims are
    # already rejected by the quality gate and excluded from grounding call.
    triples: list[tuple[str, str, str]] = []  # (section, full_entry, claim)
    gated_out: list[tuple[str, str]] = []     # (section, full_entry) quality-gate rejects

    parsed = _parse_soul_sections(observations)
    for section in SECTION_ORDER:
        for entry in parsed.get(section, []):
            claim = _extract_claim(section, entry)
            if claim is None:
                how_preview = entry.split("\n")[0][:60]
                print(f"Grounding: How: quality gate rejected: {how_preview}", file=sys.stderr)
                gated_out.append((section, entry))
            else:
                triples.append((section, entry, claim))

    if not triples:
        return ""

    # Build numbered bullet list of claims for the LLM
    numbered_claims = "\n".join(
        f"{i + 1}. {claim}" for i, (_, _, claim) in enumerate(triples)
    )
    prompt = f"## 观察\n\n{numbered_claims}\n\n## 用户原始消息\n\n{user_turns}"
    verdict = call_engine(prompt, GROUNDING_SYSTEM)
    if not verdict:
        print("Grounding: LLM returned empty response (possible API refusal)", file=sys.stderr)
        return ""

    # Parse verdicts — LLM echoes bullets; match back by index or text proximity.
    # Strategy: build an ordered list of (index, is_grounded) from verdict lines.
    # Index is inferred by matching the bullet text against the numbered claims.
    grounded_indices: set[int] = set()
    claim_texts = [claim for _, _, claim in triples]

    for vline in verdict.strip().splitlines():
        vline = vline.strip()
        if not vline:
            continue
        if vline.startswith("GROUNDED:"):
            raw_bullet = vline[len("GROUNDED:"):].strip().lstrip("- ").lstrip()
            # Try numeric prefix first: "GROUNDED: 3. ..." → index 2
            num_m = re.match(r'^(\d+)[.)\s]', raw_bullet)
            if num_m:
                idx = int(num_m.group(1)) - 1
                if 0 <= idx < len(triples):
                    grounded_indices.add(idx)
                    continue
            # Fallback: substring match against claim texts
            for idx, claim in enumerate(claim_texts):
                first_claim_line = claim.split("\n")[0]
                clean_bullet = _PK_RE.sub("", raw_bullet).strip()
                if clean_bullet in first_claim_line or first_claim_line[:40] in clean_bullet:
                    grounded_indices.add(idx)
                    break
            else:
                # Last resort: bigram Jaccard similarity ≥ 0.45
                bullet_tokens = _tokenize_bigram(raw_bullet)
                best_score, best_idx = 0.0, -1
                for idx, claim in enumerate(claim_texts):
                    score = _jaccard(bullet_tokens, _tokenize_bigram(claim))
                    if score > best_score:
                        best_score, best_idx = score, idx
                if best_score >= 0.45 and best_idx >= 0:
                    grounded_indices.add(best_idx)
        elif vline.startswith("FABRICATED:"):
            print(f"Grounding rejected: {vline[:100]}", file=sys.stderr)
        else:
            print(f"Grounding: unparseable judge line: {vline[:80]}", file=sys.stderr)

    if not grounded_indices:
        return ""

    # Reassemble output: section headers + GROUNDED entries only
    output_sections: dict[str, list[str]] = {s: [] for s in SECTION_ORDER}
    for idx, (section, full_entry, _) in enumerate(triples):
        if idx in grounded_indices:
            output_sections[section].append(full_entry)

    parts: list[str] = []
    for section in SECTION_ORDER:
        entries = output_sections[section]
        if not entries:
            continue
        parts.append(f"## {section}")
        for entry in entries:
            parts.append(entry)
        parts.append("")  # blank line between sections

    return "\n".join(parts).strip()


def _grounding_check_legacy(observations: str, user_turns: str) -> str:
    """Original line-by-line grounding for old flat-bullet SOUL format.
    Called by grounding_check() when no ## section headers are detected."""
    # Strip pk tags before sending to grounding LLM — LLMs unreliably preserve HTML comments
    pk_re = re.compile(r'\s*<!--\s*pk:\s*[\w-]+\s*-->')
    pk_map = {}  # normalized bullet text → pk tag
    clean_lines = []
    for line in observations.strip().splitlines():
        pk_match = re.search(r'(<!--\s*pk:\s*[\w-]+\s*-->)', line)
        if pk_match and line.strip().startswith("-"):
            clean_text = pk_re.sub('', line).strip()
            norm_key = re.sub(r'\*\*.*?\*\*[：:]?\s*', '', clean_text.lstrip("- ")).strip()
            pk_map[norm_key] = pk_match.group(1)
            clean_lines.append(clean_text)
        else:
            clean_lines.append(line)
    clean_obs = "\n".join(clean_lines)

    prompt = f"## 观察\n\n{clean_obs}\n\n## 用户原始消息\n\n{user_turns}"
    verdict = call_engine(prompt, GROUNDING_SYSTEM)
    if not verdict:
        print("Grounding: LLM returned empty response (possible API refusal)", file=sys.stderr)
        return ""
    kept = []
    for line in verdict.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("GROUNDED:"):
            bullet = line[len("GROUNDED:"):].strip()
            if bullet:
                bullet = bullet.lstrip("-").lstrip()
                kept.append(f"- {bullet}")
        elif line.startswith("FABRICATED:"):
            print(f"Grounding rejected: {line[:100]}", file=sys.stderr)
        else:
            print(f"Grounding: unparseable judge line: {line[:80]}", file=sys.stderr)

    # Re-attach pk tags to surviving bullets
    for i, kept_line in enumerate(kept):
        bullet_text = re.sub(r'\*\*.*?\*\*[：:]?\s*', '', kept_line.lstrip("- ")).strip()
        for orig_norm, pk_tag in pk_map.items():
            if orig_norm in bullet_text or bullet_text in orig_norm:
                kept[i] = f"{kept_line} {pk_tag}"
                break

    if not kept:
        return ""
    return "\n".join(kept)


def priority_gate(observations: str) -> str:
    """Mechanically drop Preferences/Patterns/Context entries tagged priority<50.

    Identity entries pass through untouched (not priority-scored). Entries
    missing a priority tag also pass through (legacy/lenient default).
    Operates on the same ## section-header structure grounding_check produces.
    """
    if "## " not in observations:
        return observations
    SECTION_ORDER = ("Identity", "Preferences", "Patterns", "Context", "Interaction")
    sections = _parse_soul_sections(observations)
    priority_re = re.compile(r'<!--\s*priority:\s*(\d+)\s*-->')

    output_sections: dict[str, list[str]] = {s: [] for s in SECTION_ORDER}
    dropped = 0
    for section in SECTION_ORDER:
        for entry in sections.get(section, []):
            if section != "Identity":
                m = priority_re.search(entry)
                if m and int(m.group(1)) < 50:
                    dropped += 1
                    continue
            output_sections[section].append(entry)

    if dropped:
        print(f"Priority gate: dropped {dropped} entries (priority<50)", file=sys.stderr)

    parts: list[str] = []
    for section in SECTION_ORDER:
        entries = output_sections[section]
        if not entries:
            continue
        parts.append(f"## {section}")
        for entry in entries:
            parts.append(entry)
        parts.append("")

    return "\n".join(parts).strip()


def observe_with_chunking(chunks: list[str]) -> str:
    """LLM observe — call_engine handles context limits internally."""
    combined = "\n\n---\n\n".join(chunks)
    return call_engine(combined, SOUL_SYSTEM)


def _get_required_fields(entry_text: str) -> list[str]:
    """Determine required fields based on type: tag in entry."""
    m = re.search(r'\|\s*type:\s*([\w-]+)', entry_text)
    entry_type = m.group(1) if m else "trap"
    if entry_type == "correction":
        return ["**误**", "**正**", "**因**"]
    if entry_type == "method":
        return ["**法**", "**步**", "**用**"]
    # trap / toolchain / arch / unknown → original fields
    return ["**坑**", "**因**", "**法**"]


def parse_lesson_entries(raw: str, target_date) -> list[dict]:
    """Parse LLM output into structured lesson entries.
    Each entry must have ## slug header + type-appropriate field triple."""
    entries = []
    parts = re.split(r'(?=^## [\w-]+$)', raw.strip(), flags=re.M)
    for part in parts:
        part = part.strip()
        if not part.startswith("## "):
            continue
        m = re.match(r'^## ([\w-]+)\s*$', part.splitlines()[0])
        if not m:
            continue
        slug = m.group(1)
        # Type-aware triple validation
        missing = [f for f in _get_required_fields(part) if f not in part]
        if missing:
            print(f"Lessons: skipping {slug}, missing: {', '.join(missing)}", file=sys.stderr)
            continue
        # Fix date line: match "> anything | pk:" pattern precisely
        text = re.sub(
            r'^>\s*\d{4}-\d{2}-\d{2}\s*\|',
            f'> {target_date} |',
            part, count=1, flags=re.M
        )
        entries.append({"slug": slug, "text": text})
    return entries


def lessons_quality_gate(entries: list[dict]) -> list[dict]:
    """Mechanical filter for lesson entries — reject speculative or vague content."""
    REJECT_PATTERNS = [
        r"(?:推测|可能|大概|也许|似乎)(?:是|为|存在|导致)",
        r"(?:暂未验证|待确认|不确定)",
    ]
    kept = []
    for entry in entries:
        text = entry["text"]
        if any(re.search(p, text) for p in REJECT_PATTERNS):
            print(f"Lessons quality gate rejected: {entry['slug']}", file=sys.stderr)
            continue
        # Mechanical priority cutoff (missing tag = keep, lenient default)
        pri_m = re.search(r'\|\s*priority:\s*(\d+)', text)
        if pri_m and int(pri_m.group(1)) < 50:
            print(f"Lessons quality gate rejected (priority<50): {entry['slug']}", file=sys.stderr)
            continue
        # correction without 因 is noise — root cause is the core value of a correction
        if 'type: correction' in text:
            if '**因**' not in text:
                print(f"Lessons quality gate rejected (correction without 因): {entry['slug']}", file=sys.stderr)
                continue
        kept.append(entry)
    return kept



def cmd_lessons(args):
    """Extract lessons learned from sessions into LESSONS.md."""
    logs_dir = Path(args.logs)
    lessons_path = Path(args.lessons)
    target_date = args.date or (date.today() - timedelta(days=1))

    sessions = find_sessions(logs_dir, target_date)
    if not sessions:
        print(f"No sessions for {target_date}", file=sys.stderr); return

    # Collect full day content — call_engine handles context limits
    chunks = []
    for s in sessions:
        excerpt = extract_turns(s, max_chars=200000, target_date=target_date)
        if excerpt:
            chunks.append(excerpt)
    if not chunks:
        print(f"No extractable content for {target_date}", file=sys.stderr); return

    combined = "\n\n---\n\n".join(chunks)
    system = LESSONS_SYSTEM.format(date=target_date)
    print(f"Lessons: {len(combined)//1024}KB input", file=sys.stderr)
    raw = call_engine(combined, system)

    if not raw or raw.strip() == "NONE":
        print(f"No lessons for {target_date}", file=sys.stderr); return

    # Parse and strip ## Skipped section (metadata only — not written to LESSONS.md)
    raw, skipped_lessons = _parse_skipped_section(raw)
    if skipped_lessons:
        append_skip(logs_dir, 'lessons', skipped_lessons)
        print(f"Lessons: {len(skipped_lessons)} skipped (forwarded to skip-buffer)", file=sys.stderr)

    entries = parse_lesson_entries(raw, target_date)
    entries = lessons_quality_gate(entries)
    if not entries:
        print(f"No valid lesson entries for {target_date}", file=sys.stderr); return

    # Dedup: skip entries whose slug already exists
    existing_slugs = set()
    if lessons_path.exists():
        for m in re.finditer(r'^## ([\w-]+)$', lessons_path.read_text(encoding="utf-8"), re.M):
            existing_slugs.add(m.group(1))

    new_entries = [e for e in entries if e["slug"] not in existing_slugs]
    if not new_entries:
        print(f"All lessons for {target_date} already exist", file=sys.stderr); return

    # Write
    if not lessons_path.exists():
        lessons_path.write_text(LESSONS_SKELETON.format(date=target_date, count=0), encoding="utf-8")
    content = lessons_path.read_text(encoding="utf-8")

    for entry in new_entries:
        # Insert absorbed:false marker after ## slug line
        text = re.sub(r'^(## [\w-]+)\n', r'\1\n<!-- absorbed: false -->\n', entry["text"], count=1, flags=re.M)
        content += f"\n{text}\n"

    # Update metadata
    entry_count = len(re.findall(r'^## [\w-]+$', content, re.M))
    content = re.sub(r'Entries: \d+', f'Entries: {entry_count}', content)
    content = re.sub(r'Last updated: \S+', f'Last updated: {target_date}', content)

    lessons_path.write_text(content, encoding="utf-8")
    print(f"OK {lessons_path} (+{len(new_entries)} lessons for {target_date})", file=sys.stderr)


def cmd_report(args):
    logs_dir = Path(args.logs)
    target_date = args.date or (date.today() - timedelta(days=1))
    sessions = find_sessions(logs_dir, target_date)
    reports_dir = logs_dir / "reports"
    month_dir = _report_month_dir(reports_dir, target_date)
    month_dir.mkdir(parents=True, exist_ok=True)
    out_path = month_dir / f"{target_date}.md"
    if not sessions:
        out_path.write_text(f"# {target_date}\n\n无 AI 会话记录。\n", encoding="utf-8")
        print(f"OK {out_path}", file=sys.stderr); return
    # Compute structured stats from session paths
    tool_counts, project_counts = {}, {}
    for s in sessions:
        try:
            rel = s.relative_to(logs_dir).parts
            tool, project = rel[0], rel[1] if len(rel) > 1 else "unknown"
            tool_counts[tool] = tool_counts.get(tool, 0) + 1
            project_counts[project] = project_counts.get(project, 0) + 1
        except (ValueError, IndexError):
            pass
    stats = f"## 精确统计（请直接引用，不要估算）\n\n"
    stats += f"**总 session 数: {len(sessions)}**\n\n"
    stats += "| 工具 | session 数 |\n|------|----------|\n"
    for t, c in sorted(tool_counts.items(), key=lambda x: -x[1]):
        stats += f"| {t} | {c} |\n"
    stats += "\n| 项目 | session 数 |\n|------|----------|\n"
    for p, c in sorted(project_counts.items(), key=lambda x: -x[1]):
        stats += f"| {p} | {c} |\n"

    parts = []
    for s in sessions:
        try:
            tool = s.relative_to(logs_dir).parts[0]
        except (ValueError, IndexError):
            tool = "unknown"
        parts.append(f"## {tool}: {s.stem}\n{extract_turns(s, max_chars=4000, target_date=target_date)}")
    result = call_engine(f"Date: {target_date}\n\n{stats}\n\n## 会话详情\n\n" + "\n\n".join(parts), REPORT_SYSTEM)
    out_path.write_text(f"# {target_date}\n\n{result}\n", encoding="utf-8")
    print(f"OK {out_path}", file=sys.stderr)


def cmd_soul(args):
    logs_dir, soul_path = Path(args.logs), Path(args.soul)
    target_date = args.date  # None means "today's batch mode" (existing behavior)

    if target_date:
        # Date-specific mode: extract observations for one specific day
        sessions = find_sessions(logs_dir, target_date)
        if not sessions:
            print(f"No sessions for {target_date}", file=sys.stderr); return
        chunks = []
        for s in sessions:
            excerpt = extract_turns(s, max_chars=200000, target_date=target_date)
            if excerpt:
                chunks.append(excerpt)
        if not chunks:
            print(f"No extractable content for {target_date}", file=sys.stderr); return
        observations = observe_with_chunking(chunks)
        observations = quality_gate(observations)
        if not observations:
            print(f"Observations for {target_date} rejected by quality gate", file=sys.stderr); return
        # Layer 2: LLM grounding check — collect user turns with larger budget
        # Use 8000 mixed budget so ~4000 user-only chars survive filtering
        user_turns_text = ""
        for s in sessions:
            turns = extract_turns(s, max_chars=200000, target_date=target_date)
            user_turns_text += "\n".join(l for l in turns.splitlines() if l.startswith("[user]")) + "\n"
        observations = grounding_check(observations, user_turns_text)
        if not observations:
            print(f"Observations for {target_date} rejected by grounding check", file=sys.stderr); return
        observations = priority_gate(observations)
        if not observations:
            print(f"Observations for {target_date} rejected by priority gate", file=sys.stderr); return
        entry_date = target_date
    else:
        # Existing batch mode (unchanged logic)
        today = date.today()
        if args.since:
            since_date = args.since
        elif soul_path.exists():
            since_date = datetime.fromtimestamp(soul_path.stat().st_mtime).date()
        else:
            since_date = date(2020, 1, 1)
        sessions = [s for s in find_sessions(logs_dir)
                    if max(session_days(s), default=date.min) >= since_date]
        if not sessions:
            print(f"No new sessions since {since_date}", file=sys.stderr); return
        chunks, total = [], 0
        for s in sessions:
            excerpt = extract_turns(s, max_chars=200000)
            if not excerpt:
                continue
            chunks.append(excerpt)
            total += len(excerpt)
            if total > 500000:  # soft cap for map-reduce across sessions
                break
        if not chunks:
            print("No extractable content from sessions", file=sys.stderr); return
        observations = observe_with_chunking(chunks)
        observations = quality_gate(observations)
        if not observations:
            print("Observations rejected by quality gate", file=sys.stderr); return
        # Layer 2: LLM grounding check — collect user turns with larger budget
        user_turns_text = ""
        for s in sessions:
            turns = extract_turns(s, max_chars=200000)
            user_turns_text += "\n".join(l for l in turns.splitlines() if l.startswith("[user]")) + "\n"
        observations = grounding_check(observations, user_turns_text)
        if not observations:
            print("Observations rejected by grounding check", file=sys.stderr); return
        observations = priority_gate(observations)
        if not observations:
            print("Observations rejected by priority gate", file=sys.stderr); return
        entry_date = today

    # Count actual jsonl files on disk
    file_count = len(find_sessions(logs_dir))

    if not soul_path.exists():
        soul_path.write_text(SOUL_SKELETON.format(date=entry_date, count=file_count), encoding="utf-8")
    content = soul_path.read_text(encoding="utf-8")

    # Update metadata
    content = re.sub(r"Sessions:.*", f"Sessions: {file_count} files", content)
    content = re.sub(r"Last updated:.*", f"Last updated: {entry_date}", content)
    # Legacy format cleanup: remove "Sessions processed: N" if present
    content = re.sub(r"> Sessions processed: \d+\n", "", content)

    # Merge new observations into 5-section structure
    content = _merge_soul_entry(content, observations, str(entry_date))

    # ── Interaction pass ────────────────────────────────────────────────
    # Adjacency-preserving extraction of questioning / counter-questioning /
    # alignment patterns (for harness & loop engineering). Reuses `sessions`
    # selected above; only the excerpting differs — extract_interaction_turns
    # inverts the budget toward user turns and keeps turn adjacency, which is
    # where question/counter-question structure lives.
    interaction_chunks = []
    for s in sessions:
        excerpt = extract_interaction_turns(s, max_chars=6000, target_date=target_date)
        if excerpt:
            interaction_chunks.append(excerpt)
    if interaction_chunks:
        interaction_raw = call_engine("\n\n---\n\n".join(interaction_chunks), INTERACTION_SYSTEM)
        if interaction_raw and interaction_raw.strip() != "NONE":
            interaction_raw = quality_gate(interaction_raw)
            if interaction_raw:
                user_turns_text = "\n".join(
                    l for chunk in interaction_chunks
                    for l in chunk.splitlines() if l.startswith("[user]")
                ) + "\n"
                interaction_raw = grounding_check(interaction_raw, user_turns_text)
                if interaction_raw:
                    interaction_raw = priority_gate(interaction_raw)
                    if interaction_raw:
                        content = _merge_soul_entry(content, interaction_raw, str(entry_date))

    soul_path.write_text(content, encoding="utf-8")
    print(f"OK {soul_path} ({entry_date}, +{len(sessions)} sessions)", file=sys.stderr)


def extract_unabsorbed(soul_path: Path) -> list[tuple[str, str]]:
    """Parse SOUL.md, return [(date_str, observation_text)] for unabsorbed entries."""
    if not soul_path.exists():
        return []
    content = soul_path.read_text(encoding="utf-8")
    entries = re.split(r'(?=\n### \d{4}-\d{2}-\d{2}\n)', content)
    result = []
    for entry in entries:
        m = re.match(r'\n### (\d{4}-\d{2}-\d{2})\n', entry)
        if not m:
            continue
        if "<!-- absorbed: true -->" in entry:
            continue
        date_str = m.group(1)
        # Strip the header and absorbed marker
        text = re.sub(r'^.*?-->\s*', '', entry[m.end():], count=1, flags=re.DOTALL).strip()
        if not text:
            text = entry[m.end():].strip()
        if text:
            result.append((date_str, text))
    return result


def extract_pattern_counts(soul_path: Path, lessons_path: Path | None = None) -> dict[str, int]:
    """Parse SOUL.md + LESSONS.md, count unique dates per pattern-key.

    Returns dict mapping pattern-key → number of distinct dates it appeared on.
    This mechanical count replaces unreliable LLM self-counting.
    """
    pk_dates: dict[str, set[str]] = {}
    pk_re = re.compile(r'<!--\s*pk:\s*([\w-]+)\s*-->')

    # --- SOUL.md: date sections with pk-tagged bullets (legacy format) ---
    if soul_path.exists():
        content = soul_path.read_text(encoding="utf-8")
        entries = re.split(r'(?=\n### \d{4}-\d{2}-\d{2}\n)', content)
        for entry in entries:
            m = re.match(r'\n### (\d{4}-\d{2}-\d{2})\n', entry)
            if not m:
                continue
            date_str = m.group(1)
            for pk_match in pk_re.finditer(entry):
                key = pk_match.group(1)
                pk_dates.setdefault(key, set()).add(date_str)
        # --- SOUL.md: new 4-section format with inline <!-- new: DATE --> tags ---
        for line in content.splitlines():
            pk_m = re.search(r'<!--\s*pk:\s*([\w-]+)\s*-->', line)
            if not pk_m:
                continue
            date_tag = (re.search(r'<!--\s*new:\s*(\d{4}-\d{2}-\d{2})\s*-->', line)
                        or re.search(r'<!--\s*absorbed:\s*(\d{4}-\d{2}-\d{2})\s*-->', line))
            if date_tag:
                pk_dates.setdefault(pk_m.group(1), set()).add(date_tag.group(1))

    # --- LESSONS.md: each entry has `> YYYY-MM-DD | pk: xxx` ---
    if lessons_path and lessons_path.exists():
        content = lessons_path.read_text(encoding="utf-8")
        lesson_entries = re.split(r'(?=^## [\w-])', content, flags=re.M)
        date_pk_re = re.compile(r'>\s*(\d{4}-\d{2}-\d{2})\s*\|\s*pk:\s*([\w-]+)')
        for entry in lesson_entries:
            m = date_pk_re.search(entry)
            if m:
                pk_dates.setdefault(m.group(2), set()).add(m.group(1))

    return {k: len(v) for k, v in sorted(pk_dates.items(), key=lambda x: -len(x[1]))}


def mark_absorbed(soul_path: Path, dates: list[str]):
    """Mark observation entries as absorbed in SOUL.md."""
    if not soul_path.exists():
        return
    content = soul_path.read_text(encoding="utf-8")
    for d in dates:
        content = content.replace(
            f"### {d}\n<!-- absorbed: false -->",
            f"### {d}\n<!-- absorbed: true -->"
        )
    soul_path.write_text(content, encoding="utf-8")


def prune_old(soul_path: Path, keep_days: int = 30):
    """Remove absorbed entries older than keep_days from SOUL.md."""
    if not soul_path.exists():
        return
    content = soul_path.read_text(encoding="utf-8")
    cutoff = date.today() - timedelta(days=keep_days)
    segments = re.split(r'(?=\n### \d{4}-\d{2}-\d{2}\n)', content)
    kept = []
    pruned = 0
    for seg in segments:
        m = re.match(r'\n### (\d{4}-\d{2}-\d{2})\n', seg)
        if not m:
            kept.append(seg)
            continue
        entry_date = date.fromisoformat(m.group(1))
        if entry_date < cutoff and "<!-- absorbed: true -->" in seg:
            pruned += 1
            continue
        kept.append(seg)
    if pruned:
        soul_path.write_text("".join(kept), encoding="utf-8")
        print(f"Pruned {pruned} old absorbed entries from SOUL.md", file=sys.stderr)


def extract_unabsorbed_lessons(lessons_path: Path) -> list[tuple[str, str]]:
    """Parse LESSONS.md, return unabsorbed entries as (date_str, text) tuples.
    Entries without any absorbed marker are treated as unabsorbed (backward compat)."""
    if not lessons_path.exists():
        return []
    content = lessons_path.read_text(encoding="utf-8")
    entries = re.split(r'(?=^## [\w-]+$)', content, flags=re.M)
    result = []
    for entry in entries:
        entry = entry.strip()
        if not entry.startswith("## "):
            continue
        if "<!-- absorbed: true -->" in entry or "<!-- rejected:" in entry or "<!-- needs-review -->" in entry:
            continue
        m = re.search(r'>\s*(\d{4}-\d{2}-\d{2})\s*\|', entry)
        date_str = m.group(1) if m else "unknown"
        result.append((date_str, entry))
    return result


def mark_absorbed_lessons(lessons_path: Path, slugs: list[str]):
    """Mark lesson entries as absorbed in LESSONS.md.
    Handles both new format (replace absorbed:false→true) and legacy (insert marker)."""
    if not lessons_path.exists() or not slugs:
        return
    content = lessons_path.read_text(encoding="utf-8")
    for slug in slugs:
        escaped = re.escape(slug)
        # Try replacing absorbed:false → true (new format)
        new_content = re.sub(
            rf'^(## {escaped}\n)<!-- absorbed: false -->',
            rf'\1<!-- absorbed: true -->',
            content, count=1, flags=re.M
        )
        if new_content != content:
            content = new_content
            continue
        # Legacy: no marker at all → insert absorbed:true after ## slug line
        new_content = re.sub(
            rf'^(## {escaped}\n)(?!<!-- )',
            rf'\1<!-- absorbed: true -->\n',
            content, count=1, flags=re.M
        )
        content = new_content
    lessons_path.write_text(content, encoding="utf-8")


def prune_old_lessons(lessons_path: Path, keep_days: int = 90):
    """Remove absorbed lesson entries older than keep_days."""
    if not lessons_path.exists():
        return
    content = lessons_path.read_text(encoding="utf-8")
    cutoff = date.today() - timedelta(days=keep_days)
    entries = re.split(r'(?=^## [\w-]+$)', content, flags=re.M)
    kept, pruned = [], 0
    for entry in entries:
        if not entry.strip().startswith("## "):
            kept.append(entry)
            continue
        m = re.search(r'>\s*(\d{4}-\d{2}-\d{2})\s*\|', entry)
        if m and "<!-- absorbed: true -->" in entry:
            entry_date = date.fromisoformat(m.group(1))
            if entry_date < cutoff:
                pruned += 1
                continue
        kept.append(entry)
    if pruned:
        new_content = "".join(kept)
        # Update entry count
        entry_count = len(re.findall(r'^## [\w-]+$', new_content, re.M))
        new_content = re.sub(r'Entries: \d+', f'Entries: {entry_count}', new_content)
        lessons_path.write_text(new_content, encoding="utf-8")
        print(f"Pruned {pruned} old absorbed entries from LESSONS.md", file=sys.stderr)


def review_agent_entries(lessons_path: Path):
    """Review <!-- needs-review --> entries written by agent: apply quality gate,
    promote to absorbed:false or mark as rejected."""
    if not lessons_path.exists():
        return
    content = lessons_path.read_text(encoding="utf-8")
    if "<!-- needs-review -->" not in content:
        return
    entries = re.split(r'(?=^## [\w-]+$)', content, flags=re.M)
    reviewed = 0
    for i, entry in enumerate(entries):
        if "<!-- needs-review -->" not in entry:
            continue
        # Extract slug for logging
        slug_m = re.match(r'^## ([\w-]+)', entry.strip())
        slug = slug_m.group(1) if slug_m else "unknown"
        # Apply quality gate
        dummy = [{"slug": slug, "text": entry}]
        kept = lessons_quality_gate(dummy)
        if kept:
            entries[i] = entry.replace("<!-- needs-review -->", "<!-- absorbed: false -->")
            print(f"Lessons review: {slug} → approved", file=sys.stderr)
        else:
            today_str = date.today().isoformat()
            entries[i] = entry.replace("<!-- needs-review -->", f"<!-- rejected: {today_str} -->")
            print(f"Lessons review: {slug} → rejected", file=sys.stderr)
        reviewed += 1
    if reviewed:
        content = "".join(entries)
        lessons_path.write_text(content, encoding="utf-8")
        print(f"Reviewed {reviewed} agent-written entries in LESSONS.md", file=sys.stderr)


def parse_distill_ops(raw: str) -> list[tuple[str, str, str]]:
    """Parse LLM structured diff → [(op, section, content)]. Invalid lines skipped."""
    ops = []
    for line in raw.strip().splitlines():
        line = line.strip()
        if not line or line == "NOP":
            continue
        m = re.match(r'^(ADD|STRENGTHEN|WEAKEN|REMOVE)\s+(MUST|MUST_NOT|PREFER|CONTEXT):\s*(.+)$', line)
        if m:
            ops.append((m.group(1), m.group(2), m.group(3)))
        elif line not in ("", "NOP"):
            print(f"Distill: unparseable line: {line[:80]}", file=sys.stderr)
    return ops


DISTILL_MIN_ENTRIES = 7  # minimum unabsorbed entries before auto-triggering distill


def _section_bounds(content: str, header: str) -> tuple[int, int] | None:
    """Return (start, end) line indices for a section's bullet area (exclusive of header)."""
    lines = content.splitlines()
    start = None
    for i, l in enumerate(lines):
        if l.strip() == f"## {header}":
            start = i + 1
        elif start is not None and l.startswith("## "):
            return (start, i)
    if start is not None:
        return (start, len(lines))
    return None


def apply_ops(content: str, ops: list[tuple[str, str, str]]) -> str:
    """Apply structured diff operations to MEMORY.md content, scoped to target sections."""
    if not content.strip():
        content = MEMORY_SKELETON.format(date=date.today(), version=0)

    for op, section, payload in ops:
        header = "MUST NOT" if section == "MUST_NOT" else section
        section_re = rf'(## {re.escape(header)}\n)(.*?)(?=\n## |\Z)'

        if op == "ADD":
            def add_rule(m, _payload=payload):
                return m.group(1) + m.group(2).rstrip('\n') + f"\n- {_payload}\n"
            content = re.sub(section_re, add_rule, content, count=1, flags=re.DOTALL)

        elif op == "REMOVE":
            bounds = _section_bounds(content, header)
            if bounds:
                lines = content.splitlines()
                s, e = bounds
                kept = [l for l in lines[s:e] if not (l.lstrip().startswith("- ") and payload in l)]
                content = "\n".join(lines[:s] + kept + lines[e:]) + "\n"
            else:
                print(f"Distill: REMOVE section not found: {header}", file=sys.stderr)

        elif op == "STRENGTHEN":
            if "→" in payload:
                old_hint, new_rule = payload.split("→", 1)
                old_hint, new_rule = old_hint.strip(), new_rule.strip()
                bounds = _section_bounds(content, header)
                if bounds:
                    lines = content.splitlines()
                    s, e = bounds
                    replaced = False
                    for i in range(s, e):
                        if old_hint in lines[i]:
                            lines[i] = f"- {new_rule}"
                            replaced = True
                            break
                    if replaced:
                        content = "\n".join(lines) + "\n"
                    else:
                        print(f"Distill: STRENGTHEN target not found in {header}: {old_hint[:60]}", file=sys.stderr)

        elif op == "WEAKEN":
            bounds = _section_bounds(content, header)
            removed_line = None
            if bounds:
                lines = content.splitlines()
                s, e = bounds
                for i in range(s, e):
                    if lines[i].lstrip().startswith("- ") and payload in lines[i]:
                        removed_line = lines[i].lstrip("- ").strip()
                        lines.pop(i)
                        content = "\n".join(lines) + "\n"
                        break
            if removed_line:
                prefer_re = r'(## PREFER\n)(.*?)(?=\n## |\Z)'
                def add_weakened(m, _rl=removed_line):
                    return m.group(1) + m.group(2).rstrip('\n') + f"\n- {_rl} (待观察)\n"
                content = re.sub(prefer_re, add_weakened, content, count=1, flags=re.DOTALL)

    # Update metadata
    content = re.sub(r'Updated: \S+', f'Updated: {date.today()}', content)
    if (m := re.search(r'Version: (\d+)', content)):
        content = re.sub(r'Version: \d+', f'Version: {int(m.group(1)) + 1}', content)

    return content


def _auto_create_gene(genes_dir: Path, pk: str, area: str, pk_entries_text: list[str], today: date):
    """Auto-create Gene scaffold from LESSONS.md entries sharing a pk.

    If a type:method entry exists for this pk, extract approach from its **步** field.
    Otherwise create a scaffold with <!-- needs-review --> marker.
    """
    gene_dir = genes_dir / pk
    if gene_dir.exists():
        return  # idempotent

    # Check for method entries
    method_approach = None
    method_desc = None
    for text in pk_entries_text:
        if 'type: method' in text:
            fa_m = re.search(r'\*\*法\*\*[：:]\s*(.+)', text)
            bu_m = re.search(r'\*\*步\*\*[：:]\s*(.+?)(?=\n\*\*|\Z)', text, re.S)
            if fa_m:
                method_desc = fa_m.group(1).strip()
            if bu_m:
                method_approach = bu_m.group(1).strip()

    description = method_desc or f"<!-- TODO: needs human review — auto-scaffolded from {len(pk_entries_text)} entries -->"
    needs_review = method_desc is None

    # Create directories
    gene_dir.mkdir(parents=True, exist_ok=True)
    (gene_dir / "variants").mkdir(exist_ok=True)

    # Write gene.yaml (atomic)
    gene_yaml = (
        f"gene_id: GEN-{today.strftime('%Y%m%d')}-{pk[:3]}\n"
        f"name: {pk}\n"
        f"description: {description}\n"
        f"created: {today.isoformat()}\n"
        f"source_type: learning\n"
        f"context_tags: {area}\n"
        f"applicable_areas: {area}\n"
        f"usage_count: 0\n"
        f"decay_window_days: 90\n"
    )
    tmp = (gene_dir / "gene.yaml.tmp")
    tmp.write_text(gene_yaml, encoding="utf-8")
    os.replace(tmp, gene_dir / "gene.yaml")

    # Write variants/v1.yaml (atomic)
    approach = method_approach or "\n".join(f"  {i+1}. (from {slug})" for i, slug in enumerate(
        re.findall(r'^## ([\w-]+)', '\n'.join(pk_entries_text), re.M)[:5]
    ))
    v1_yaml = (
        f"version: 1\n"
        f"created: {today.isoformat()}\n"
        f"approach: |\n"
        f"  {approach}\n"
    )
    tmp_v = (gene_dir / "variants" / "v1.yaml.tmp")
    tmp_v.write_text(v1_yaml, encoding="utf-8")
    os.replace(tmp_v, gene_dir / "variants" / "v1.yaml")

    marker = " [needs-review]" if needs_review else ""
    print(f"Gene auto-created: {pk} (area={area}){marker}", file=sys.stderr)


def cmd_distill(args):
    """Distill SOUL.md observations + LESSONS.md lessons into MEMORY.md rules."""
    soul_path = Path(args.soul)
    memory_path = Path(args.memory)
    lessons_path = Path(args.lessons)

    # Phase 0: review agent-written entries (<!-- needs-review --> → quality gate)
    review_agent_entries(lessons_path)

    # Phase 1: extract unabsorbed from both sources
    unabsorbed_soul = extract_unabsorbed_soul(soul_path)
    unabsorbed_lessons = extract_unabsorbed_lessons(lessons_path)
    all_unabsorbed = unabsorbed_soul + unabsorbed_lessons

    if not all_unabsorbed:
        print("Distill: no unabsorbed entries in SOUL.md or LESSONS.md", file=sys.stderr); return
    # Threshold applies to combined count (SOUL + LESSONS)
    if len(all_unabsorbed) < DISTILL_MIN_ENTRIES and not args.force:
        print(f"Distill: only {len(all_unabsorbed)} unabsorbed entries (need {DISTILL_MIN_ENTRIES}+). Use --force to override.", file=sys.stderr)
        return

    # Phase 1.5: mechanical pattern-key counting across ALL entries (absorbed + unabsorbed)
    pattern_counts = extract_pattern_counts(soul_path, lessons_path)
    pattern_section = ""
    if pattern_counts:
        # Include pk with count (days × occurrences) for richer signal
        strong_pks = {k: v for k, v in pattern_counts.items() if v >= 2}
        if strong_pks:
            lines = [f"  {k}: {v}天" for k, v in strong_pks.items()]
            pattern_section = (
                "\n\n## Pattern-Key 出现天数（机械统计，地面真值）\n\n"
                + "\n".join(lines)
            )
        print(f"Distill: {len(pattern_counts)} pattern-keys ({len(strong_pks)} with ≥2 days), top: "
              + ", ".join(f"{k}={v}" for k, v in list(pattern_counts.items())[:5]),
              file=sys.stderr)

    # Phase 2: read current MEMORY.md
    current_memory = memory_path.read_text(encoding="utf-8") if memory_path.exists() else ""

    # Phase 3: LLM → structured diff (call_engine handles context limits)
    obs_parts = []
    if unabsorbed_soul:
        obs_parts.append("### 行为观察（来自 SOUL.md）\n\n" +
                         "\n\n".join(f"#### [{section}]\n{entry}" for section, entry in unabsorbed_soul))
    if unabsorbed_lessons:
        obs_parts.append("### 经验教训（来自 LESSONS.md）\n\n" +
                         "\n\n".join(f"#### {d}\n{t}" for d, t in unabsorbed_lessons))
    obs_text = "\n\n".join(obs_parts)
    prompt = f"## Current MEMORY.md\n\n{current_memory}\n\n## New Input\n\n{obs_text}{pattern_section}"
    print(f"Distill: {len(all_unabsorbed)} entries ({len(prompt)//1024}KB prompt)", file=sys.stderr)
    raw_diff = call_engine(prompt, DISTILL_SYSTEM)

    # Parse and strip ## Skipped section from distill output
    if raw_diff:
        raw_diff, skipped_distill = _parse_skipped_section(raw_diff)
        if skipped_distill:
            append_skip(Path(args.logs), 'distill', skipped_distill)
            print(f"Distill: {len(skipped_distill)} candidates skipped (forwarded to skip-buffer)", file=sys.stderr)

    # Phase 4: parse and apply
    ops = parse_distill_ops(raw_diff)
    if not ops:
        print("Distill: NOP, marking as absorbed", file=sys.stderr)
    else:
        new_memory = apply_ops(current_memory, ops)
        memory_path.write_text(new_memory, encoding="utf-8")

    # Phase 5: mark absorbed + prune old
    _mark_absorbed_soul_entries(soul_path, [t for _, t in unabsorbed_soul])
    prune_old(soul_path, keep_days=30)  # no-op for new format, safe for old
    _mark_lessons_absorbed(lessons_path, unabsorbed_lessons)
    prune_old_lessons(lessons_path, keep_days=90)

    total = len(unabsorbed_soul) + len(unabsorbed_lessons)
    if ops:
        print(f"OK {memory_path} ({len(ops)} ops, {total} entries absorbed: "
              f"{len(unabsorbed_soul)} soul + {len(unabsorbed_lessons)} lessons)", file=sys.stderr)
    else:
        print(f"Distill: {total} entries marked absorbed (NOP)", file=sys.stderr)

    # Phase 6: Gene candidate review (LLM-gated, multi-condition AND)
    if pattern_counts:
        genes_dir = Path(args.logs) / "genes"
        logs_dir = Path(args.logs)

        # Build structured lesson_entries for this phase
        lesson_entries = []
        if lessons_path.exists():
            lc = lessons_path.read_text(encoding="utf-8")
            for entry_text in re.split(r'(?=^## [\w-])', lc, flags=re.M):
                pk_m = re.search(r'pk:\s*([\w-]+)', entry_text)
                slug_m = re.match(r'^## ([\w-]+)', entry_text.strip())
                type_m = re.search(r'type:\s*([\w-]+)', entry_text)
                area_m = re.search(r'area:\s*([\w-]+)', entry_text)
                if pk_m:
                    lesson_entries.append({
                        'pk': pk_m.group(1),
                        'slug': slug_m.group(1) if slug_m else '',
                        'type': type_m.group(1) if type_m else 'trap',
                        'area': area_m.group(1) if area_m else 'unknown',
                        'text': entry_text.strip(),
                    })

        candidates = []
        for pk, days in pattern_counts.items():
            if days < 2:
                continue
            pk_lessons = [e for e in lesson_entries if e.get('pk') == pk]
            candidates.append({
                "pk": pk,
                "days": days,
                "lessons": [{"slug": l.get('slug'), "type": l.get('type', 'trap'),
                              "preview": l.get('text', '')[:200]} for l in pk_lessons[:3]]
            })

        if candidates:
            existing_genes = (
                [p.name for p in genes_dir.iterdir()
                 if p.is_dir() and not p.name.startswith('.')]
                if genes_dir.exists() else []
            )
            mem_keywords = re.findall(r'\b[a-z]{4,}\b', memory_path.read_text(encoding="utf-8").lower()
                                      if memory_path.exists() else "")[:200]

            review_input = json.dumps({
                "candidates": candidates,
                "existing_genes": existing_genes,
                "memory_keywords": list(set(mem_keywords)),
            }, ensure_ascii=False)

            review_output = call_engine(review_input, GENE_REVIEW_SYSTEM)

            skip_items = []
            for line in (review_output or "").splitlines():
                line = line.strip()
                if not line.startswith('{'):
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                pk = d.get("pk")
                if not pk:
                    continue
                action = d.get("action", "skip")
                confidence = d.get("confidence", "")
                reason = d.get("reason", "")

                if action == "create" and confidence == "high":
                    pk_lessons = [e for e in lesson_entries if e.get('pk') == pk]
                    area = pk_lessons[0].get('area', 'general') if pk_lessons else 'general'
                    pk_texts = [e['text'] for e in pk_lessons]
                    _auto_create_gene(genes_dir, pk, area, pk_texts, date.today())
                    print(f"Gene auto-created: {pk} (high confidence) — {reason}", file=sys.stderr)
                elif action == "create" and confidence == "medium":
                    print(f"Gene candidate (medium, needs human review): {pk} — {reason}", file=sys.stderr)
                    skip_items.append({"pk": pk, "decision": "needs-review", "reason": reason})
                else:
                    print(f"Gene skip: {pk} — {reason}", file=sys.stderr)
                    skip_items.append({"pk": pk, "decision": "skip", "reason": reason})

            if skip_items:
                append_skip(logs_dir, 'gene', skip_items)

    # Phase 7: MEMORY.md health check (mechanical)
    if memory_path.exists():
        mem_counts = _count_memory_rules(memory_path)
        total_rules = sum(mem_counts.values())
        warnings = []

        if total_rules > 100:
            warnings.append(f"MEMORY.md 规模膨胀: {total_rules} 条规则 (>100)")

        prefer_count = mem_counts.get("PREFER", 0)
        must_count = mem_counts.get("MUST", 0)
        if prefer_count > max(15, int(must_count * 1.5)):
            warnings.append(f"PREFER ({prefer_count}) 显著多于 MUST ({must_count})，考虑升级强信号")

        unabsorbed_count = len(extract_unabsorbed_lessons(lessons_path))
        if unabsorbed_count > DISTILL_MIN_ENTRIES * 3:
            warnings.append(f"LESSONS 积压 {unabsorbed_count} 条未吸收")

        context_count = mem_counts.get("CONTEXT", 0)
        if context_count < 2 and total_rules > 20:
            warnings.append(f"CONTEXT section 仅 {context_count} 条，考虑补充环境约束")

        for w in warnings:
            print(f"Distill health: {w}", file=sys.stderr)


def _mark_lessons_absorbed(lessons_path: Path, unabsorbed_lessons: list[tuple[str, str]]):
    """Extract slugs from unabsorbed lessons and mark them absorbed."""
    slugs = []
    for _, text in unabsorbed_lessons:
        m = re.match(r'^## ([\w-]+)', text.strip())
        if m:
            slugs.append(m.group(1))
    mark_absorbed_lessons(lessons_path, slugs)


def _parse_gene_yaml(filepath: Path) -> dict | None:
    """Parse gene.yaml — flat top-level scalar fields only, no PyYAML.

    Skips comment lines, indented lines (block scalar content), and
    multiline block markers (value == '|'). Returns dict of scalar
    fields or None if file is empty/invalid.
    """
    if not filepath.is_file():
        return None
    result = {}
    for raw in filepath.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        # Skip comments and empty lines
        if not stripped or stripped.startswith("#"):
            continue
        # Indented line = block scalar content, skip
        if raw[0:1].isspace():
            continue
        # Top-level key: value
        if ":" in stripped:
            key, _, value = stripped.partition(":")
            value = value.strip().strip('"').strip("'")
            if value == "|":
                continue  # block scalar marker — skip, content lines are indented
            result[key.strip()] = value
    return result or None


def cmd_gene_health(args):
    """Compute Gene freshness scores and output health report."""
    genes_dir = Path(args.genes_dir)
    if not genes_dir.is_dir():
        print(f"No genes directory at {genes_dir}", file=sys.stderr)
        return

    today = date.today()
    genes = []
    for entry in sorted(genes_dir.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        gene_yaml = entry / "gene.yaml"
        gene = _parse_gene_yaml(gene_yaml)
        if not gene:
            continue
        gene["_name"] = entry.name
        gene["_path"] = str(gene_yaml)
        genes.append(gene)

    if not genes:
        print("No genes found", file=sys.stderr)
        return

    active, stale, degraded = [], [], []
    registry_entries = []
    for g in genes:
        last_used = g.get("last_used", "")
        decay_window = int(g.get("decay_window_days") or 90)
        if last_used:
            try:
                lu_date = date.fromisoformat(last_used[:10])
                days_since = (today - lu_date).days
            except ValueError:
                days_since = decay_window
        else:
            # Never used — check created date
            created = g.get("created", "")
            if created:
                try:
                    cr_date = date.fromisoformat(created[:10])
                    days_since = (today - cr_date).days
                except ValueError:
                    days_since = decay_window
            else:
                days_since = decay_window

        freshness = max(0.0, round(1.0 - days_since / decay_window, 3))
        status = "active" if freshness > 0.5 else "stale" if freshness > 0.2 else "degraded"
        g["_freshness"] = freshness
        g["_status"] = status

        # Update gene.yaml in place
        path = Path(g["_path"])
        content = path.read_text(encoding="utf-8")
        for field, val in [("freshness_score", freshness), ("decay_status", status)]:
            if re.search(rf"^{field}:", content, flags=re.M):
                content = re.sub(rf"^{field}:.*$", f"{field}: {val}", content, flags=re.M)
            else:
                content = content.rstrip("\n") + f"\n{field}: {val}\n"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)

        {"active": active, "stale": stale, "degraded": degraded}[status].append(g)
        registry_entries.append({
            "gene_id": g.get("gene_id", ""),
            "name": g["_name"],
            "path": g["_name"],
            "created": g.get("created", ""),
            "decay_status": status,
            "freshness_score": freshness,
        })

    # Rebuild registry.json from gene.yaml (SSOT: gene.yaml, registry is derived index)
    registry_path = genes_dir / "registry.json"
    tmp_registry = registry_path.with_suffix(".tmp")
    tmp_registry.write_text(json.dumps({"genes": registry_entries}, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp_registry, registry_path)

    print(f"Gene Health: {len(active)} active, {len(stale)} stale, {len(degraded)} degraded", file=sys.stderr)
    for g in stale:
        print(f"  STALE: {g['_name']} (freshness={g['_freshness']}, last_used={g.get('last_used', 'never')})", file=sys.stderr)
    for g in degraded:
        print(f"  DEGRADED: {g['_name']} (freshness={g['_freshness']}, last_used={g.get('last_used', 'never')})", file=sys.stderr)


def cmd_push(args):
    """Push latest report to WeCom group webhook."""
    webhook = os.environ.get("WECOM_WEBHOOK_URL")
    if not webhook:
        print("WECOM_WEBHOOK_URL not set, skip push", file=sys.stderr); return
    reports_dir = Path(args.logs) / "reports"
    # Find the most recently modified work report (YYYY-MM-DD.md only, exclude daily-health-*)
    # reports/ is partitioned reports/YYYY/MM/, so recurse rather than glob one level.
    reports = sorted(
        [p for p in reports_dir.rglob("*.md") if re.match(r'\d{4}-\d{2}-\d{2}\.md$', p.name)],
        key=lambda p: p.stat().st_mtime, reverse=True
    )
    if not reports:
        print("No reports found", file=sys.stderr); return
    report_text = reports[0].read_text(encoding="utf-8")
    # WeCom markdown limit is 4096 bytes
    if len(report_text.encode("utf-8")) > 4000:
        report_text = report_text[:3500] + "\n\n...\n\n> 完整日报见服务器"
    body = json.dumps({"msgtype": "markdown", "markdown": {"content": report_text}}).encode()
    req = Request(webhook, data=body, headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=10):
            pass
        print(f"Pushed {reports[0].name} to WeCom", file=sys.stderr)
    except Exception as e:
        print(f"Push failed: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# cmd_daily — pure mechanical health report (no LLM)
# ---------------------------------------------------------------------------

def _tokenize_bigram(text: str) -> set[str]:
    """CJK bigram + Latin word tokenizer for Jaccard similarity."""
    # Extract Latin words and CJK characters
    tokens = re.findall(r'[一-鿿]|[a-zA-Z0-9_]+', text.lower())
    # Build bigrams from adjacent CJK characters
    result = set()
    cjk_buf = []
    for tok in tokens:
        if len(tok) == 1 and '一' <= tok <= '鿿':
            cjk_buf.append(tok)
        else:
            # Flush CJK buffer as bigrams
            for i in range(len(cjk_buf) - 1):
                result.add(cjk_buf[i] + cjk_buf[i + 1])
            cjk_buf = []
            if len(tok) > 1:
                result.add(tok)
    # Flush remaining CJK
    for i in range(len(cjk_buf) - 1):
        result.add(cjk_buf[i] + cjk_buf[i + 1])
    return result


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ── SOUL 4-section helpers ─────────────────────────────────────────────────

def _parse_section_entries(section_name: str, body: str) -> list[str]:
    """Extract individual entries from a section body string.

    For Preferences, each entry spans the PREFER/REJECT line plus indented
    Why:/How: continuation lines.  All other sections are single-line bullets.
    """
    entries: list[str] = []
    current_lines: list[str] = []

    for line in body.splitlines():
        if line.startswith("- "):
            if current_lines:
                entries.append("\n".join(current_lines))
            current_lines = [line]
        elif current_lines and section_name == "Preferences" and (
            line.startswith("  ") or line.startswith("\t")
        ):
            # indented continuation for multi-line Preferences entry
            current_lines.append(line)
        elif line.strip() and current_lines:
            # non-bullet non-empty non-continuation → end current entry
            entries.append("\n".join(current_lines))
            current_lines = []
        # blank lines and section metadata lines are skipped

    if current_lines:
        entries.append("\n".join(current_lines))

    return entries


def _parse_soul_sections(content: str) -> dict[str, list[str]]:
    """Parse SOUL.md content into {section_name: [entry_str, ...]} dict.

    Handles the 4 canonical sections: Identity, Preferences, Patterns, Context.
    Unknown sections and header content are ignored.
    """
    result: dict[str, list[str]] = {
        "Identity": [], "Preferences": [], "Patterns": [], "Context": [], "Interaction": []
    }
    parts = re.split(r"(?=^## )", content, flags=re.M)
    for part in parts:
        m = re.match(r"^## (Identity|Preferences|Patterns|Context|Interaction)\s*\n", part)
        if not m:
            continue
        section_name = m.group(1)
        body = part[m.end():]
        result[section_name] = _parse_section_entries(section_name, body)
    return result


def _pref_key(first_line: str) -> str:
    """Dedup key for a Preferences entry: text after PREFER/REJECT up to
    first comma (ASCII or CJK), lowercased, backticks/asterisks removed."""
    # strip HTML comments
    line = re.sub(r"\s*<!--.*?-->", "", first_line, flags=re.DOTALL).strip()
    m = re.match(r"^-\s*(?:PREFER|REJECT)\s+(.+)", line)
    if not m:
        return line.lower().replace("`", "")[:50]
    text = m.group(1)
    # stop at first ASCII or CJK comma
    text = re.split(r"[,，]", text, maxsplit=1)[0]
    return text.strip().lower().replace("`", "").replace("*", "")[:50]


def _update_soul_section(
    soul_content: str,
    section_name: str,
    to_replace: dict[str, str],
    to_add: list[str],
) -> str:
    """Apply replacements and additions to a named section in soul_content.

    Replacements are done by exact-string substitution on the full content.
    New entries are appended before the next ## section header (or EOF).
    """
    # Apply replacements first (entries are unique strings within the file)
    for old_entry, new_entry in to_replace.items():
        # Try exact match first; fall back to first-line match
        if old_entry in soul_content:
            soul_content = soul_content.replace(old_entry, new_entry, 1)
        else:
            old_first = old_entry.split("\n")[0]
            new_first = new_entry.split("\n")[0]
            if old_first in soul_content:
                soul_content = soul_content.replace(old_first, new_first, 1)

    if not to_add:
        return soul_content

    # Locate section header
    header_re = re.compile(rf"^## {re.escape(section_name)}\s*$", re.M)
    m_hdr = header_re.search(soul_content)
    if not m_hdr:
        return soul_content

    body_start = m_hdr.end()

    # Locate next ## section (to know where the body ends)
    next_re = re.compile(r"^## ", re.M)
    m_next = next_re.search(soul_content, body_start)
    insert_pos = m_next.start() if m_next else len(soul_content)

    block = ""
    for entry in to_add:
        block += entry.rstrip("\n") + "\n"

    before = soul_content[:insert_pos].rstrip("\n") + "\n"
    after = soul_content[insert_pos:]
    separator = "\n" if m_next else ""  # blank line before the next ## header, none at EOF
    return before + block + separator + after


def _merge_soul_entry(
    soul_content: str, new_observations: str, entry_date: str
) -> str:
    """Append new LLM observations into soul_content (pure append, no dedup).

    Each entry is tagged with <!-- new: entry_date -->.
    Dedup and consolidation are dream's responsibility.
    """
    new_sections = _parse_soul_sections(new_observations)

    for section_name in ("Identity", "Preferences", "Patterns", "Context", "Interaction"):
        new_entries = new_sections.get(section_name, [])
        if not new_entries:
            continue

        # Ensure section exists in soul_content (safety net for migration)
        if f"## {section_name}" not in soul_content:
            soul_content = soul_content.rstrip("\n") + f"\n\n## {section_name}\n"

        to_add: list[str] = []
        for new_entry in new_entries:
            lines = new_entry.split("\n")
            first_line = lines[0]
            if "<!-- new:" not in first_line and "<!-- absorbed:" not in first_line:
                lines[0] = first_line + f" <!-- new: {entry_date} -->"
            to_add.append("\n".join(lines))

        if to_add:
            soul_content = _update_soul_section(soul_content, section_name, {}, to_add)

    return soul_content

def _rebuild_soul(original_content: str, sections_entries: dict[str, list[str]]) -> str:
    """Rebuild SOUL.md preserving the header (metadata block) and replacing
    the 4 sections with the supplied entries."""
    section_names = ("Identity", "Preferences", "Patterns", "Context", "Interaction")
    # Everything before the first canonical ## section is the header
    first_section_re = re.compile(
        r"^## (?:Identity|Preferences|Patterns|Context|Interaction)", re.M
    )
    m_first = first_section_re.search(original_content)
    if m_first:
        line_start = original_content.rfind("\n", 0, m_first.start()) + 1
        header = original_content[:line_start]
    else:
        header = original_content

    result = header.rstrip("\n") + "\n"
    for name in section_names:
        result += f"\n## {name}\n\n"
        for entry in sections_entries.get(name, []):
            result += entry.rstrip("\n") + "\n"
    return result


# ── Unabsorbed soul entries (new 4-section format) ─────────────────────────

def extract_unabsorbed_soul(soul_path: Path) -> list[tuple[str, str]]:
    """Parse SOUL.md (new 4-section format), return (section_name, entry_text)
    for entries tagged with <!-- new: YYYY-MM-DD --> but NOT <!-- absorbed: -->.
    """
    if not soul_path.exists():
        return []
    content = soul_path.read_text(encoding="utf-8")
    sections = _parse_soul_sections(content)
    result: list[tuple[str, str]] = []
    for section_name, entries in sections.items():
        for entry in entries:
            if "<!-- new:" in entry and "<!-- absorbed:" not in entry:
                result.append((section_name, entry))
    return result


def _mark_absorbed_soul_entries(soul_path: Path, entry_texts: list[str]) -> None:
    """Change <!-- new: YYYY-MM-DD --> to <!-- absorbed: TODAY --> for each entry.

    Matching is done on the first line of each entry (more robust than full-text
    match for multi-line Preferences entries).
    """
    if not soul_path.exists() or not entry_texts:
        return
    today = date.today().isoformat()
    content = soul_path.read_text(encoding="utf-8")
    changed = False
    for entry in entry_texts:
        first_line = entry.split("\n")[0]
        if "<!-- new:" not in first_line:
            continue
        new_first_line = re.sub(
            r"\s*<!--\s*new:\s*\d{4}-\d{2}-\d{2}\s*-->",
            f" <!-- absorbed: {today} -->",
            first_line,
        )
        if new_first_line != first_line and first_line in content:
            content = content.replace(first_line, new_first_line, 1)
            changed = True
    if changed:
        soul_path.write_text(content, encoding="utf-8")

# ── cmd_dream: LLM-powered semantic consolidation ────────────────────────

def _strip_lifecycle_tags(text: str) -> str:
    """Remove id/new/absorbed HTML-comment tags (dream reassigns these on write)."""
    text = re.sub(r'\s*<!--\s*id:\s*[0-9a-f]+\s*-->', '', text)
    text = re.sub(r'\s*<!--\s*new:\s*\d{4}-\d{2}-\d{2}\s*-->', '', text)
    text = re.sub(r'\s*<!--\s*absorbed:\s*[^>]*?-->', '', text)
    return text


_ID_TAG_RE = re.compile(r'<!--\s*id:\s*([0-9a-f]{8})\s*-->')


def _entry_id(text: str) -> str:
    """Deterministic 8-hex-char id from an entry's content (sans lifecycle tags),
    so the same semantic entry always gets the same id across dream runs."""
    return hashlib.sha1(_strip_lifecycle_tags(text).strip().encode("utf-8")).hexdigest()[:8]


def _ensure_entry_id(entry: str) -> tuple[str, str]:
    """Return (id, entry_with_id_tag_on_first_line). Reuses an existing id tag if present."""
    m = _ID_TAG_RE.search(entry)
    if m:
        return m.group(1), entry
    eid = _entry_id(entry)
    lines = entry.split("\n")
    lines[0] = lines[0].rstrip() + f" <!-- id: {eid} -->"
    return eid, "\n".join(lines)


def _call_json_engine(payload: dict, system: str) -> list | None:
    """call_engine expecting a strict JSON array response.

    Always allow_chunking=False: candidate pools here are small (tens of
    entries, well under the char budget in practice), and a JSON array is a
    single logical unit — splitting the call would produce N independent
    arrays with no way to merge them meaningfully. Returns None (not []) on
    any failure so callers can distinguish "nothing to change" from "could
    not get a trustworthy answer, leave the file alone".
    """
    raw = call_engine(json.dumps(payload, ensure_ascii=False), system, allow_chunking=False)
    if not raw:
        return None
    raw = re.sub(r'^```(?:json)?\s*|\s*```\s*$', '', raw.strip())
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"Dream: JSON parse failed: {e}", file=sys.stderr)
        return None
    if not isinstance(data, list):
        print("Dream: JSON response is not a list, ignoring", file=sys.stderr)
        return None
    return data


def _apply_dedup_ops(id_map: dict[str, str], ops: list, unabsorbed_ids: set[str]) -> list[str]:
    """Apply validated remove/merge ops to an {id: tagged_entry_text} pool.

    Returns the final entry list (survivors + newly merged entries).
    Malformed ops (unknown id, id claimed by >1 op, missing content) are
    skipped with a stderr note rather than raising — LLM output is untrusted
    and a bad op must never take down the whole consolidation pass.
    """
    removed: set[str] = set()
    claimed: set[str] = set()
    merged_new: list[str] = []
    for op in ops:
        if not isinstance(op, dict):
            continue
        action = op.get("op")
        ids = [i for i in op.get("ids", []) if isinstance(i, str) and i in id_map and i not in claimed]
        if not ids:
            continue
        if action == "remove":
            removed.update(ids)
            claimed.update(ids)
        elif action == "merge":
            content = (op.get("content") or "").strip()
            if not content:
                print("Dream: merge op missing content, skipping", file=sys.stderr)
                continue
            removed.update(ids)
            claimed.update(ids)
            content = _strip_lifecycle_tags(content).strip()
            eid = _entry_id(content)
            lines = content.split("\n")
            tag = f" <!-- id: {eid} -->"
            if any(i in unabsorbed_ids for i in ids):
                tag += f" <!-- new: {date.today()} -->"
            lines[0] = lines[0].rstrip() + tag
            merged_new.append("\n".join(lines))
        else:
            print(f"Dream: unknown op '{action}', skipping", file=sys.stderr)

    survivors = [text for eid, text in id_map.items() if eid not in removed]
    return survivors + merged_new


def _dedup_pool(pool_entries: list[str], payload_extra: dict, system: str) -> list[str]:
    """Structured LLM dedup for one candidate pool (list of raw entry strings).

    Replaces the old whole-file-rewrite approach: the LLM never sees or
    produces a complete document, only per-entry remove/merge decisions
    against a small pool. A malformed/empty response leaves the pool
    untouched (ids get assigned regardless, so future runs converge) instead
    of silently destroying entries it never had a chance to consider.
    """
    if len(pool_entries) < 2:
        return pool_entries
    id_map: dict[str, str] = {}
    unabsorbed: set[str] = set()
    pool = []
    for entry in pool_entries:
        eid, tagged = _ensure_entry_id(entry)
        id_map[eid] = tagged
        if "<!-- new:" in tagged:
            unabsorbed.add(eid)
        pool.append({"id": eid, "content": _strip_lifecycle_tags(tagged).strip()})

    payload = {**payload_extra, "entries": pool}
    ops = _call_json_engine(payload, system)
    if ops is None:
        print("Dream: dedup call failed, pool left unchanged (ids assigned)", file=sys.stderr)
        return list(id_map.values())
    if not ops:
        return list(id_map.values())
    return _apply_dedup_ops(id_map, ops, unabsorbed)


_MEMORY_SECTIONS = ("MUST", "MUST NOT", "PREFER", "CONTEXT")


def _parse_memory_layers(content: str) -> dict[str, dict[str, list[str]]]:
    """Parse MEMORY.md into {section: {"Universal": [...], "Project-specific": [...]}}.

    Tolerates the flat legacy format (no ### sub-headers) by treating the
    whole section body as Universal, and a missing section entirely as empty
    (rather than raising) — this is also how a partially-corrupted MEMORY.md
    degrades gracefully instead of crashing the pipeline.
    """
    result: dict[str, dict[str, list[str]]] = {}
    for section in _MEMORY_SECTIONS:
        pattern = rf'^## {re.escape(section)}\s*\n(.*?)(?=^## |\Z)'
        m = re.search(pattern, content, re.S | re.M)
        body = m.group(1) if m else ""
        layers = {"Universal": [], "Project-specific": []}
        if "### Universal" in body or "### Project-specific" in body:
            for layer in ("Universal", "Project-specific"):
                sub_pattern = rf'^### {re.escape(layer)}[^\n]*\n(.*?)(?=^### |\Z)'
                sm = re.search(sub_pattern, body, re.S | re.M)
                if sm:
                    layers[layer] = [l.strip() for l in sm.group(1).splitlines() if l.strip().startswith("- ")]
        else:
            layers["Universal"] = [l.strip() for l in body.splitlines() if l.strip().startswith("- ")]
        result[section] = layers
    return result


def _rebuild_memory(header: str, layers: dict[str, dict[str, list[str]]]) -> str:
    """Rebuild MEMORY.md: given header text (metadata block only) and the
    4-section Universal/Project-specific layer dict, emit the full file."""
    result = header.rstrip("\n") + "\n"
    for section in _MEMORY_SECTIONS:
        result += f"\n## {section}\n\n### Universal\n"
        for e in layers[section]["Universal"]:
            result += e.rstrip("\n") + "\n"
        result += "\n### Project-specific (ai-distillery)\n"
        for e in layers[section]["Project-specific"]:
            result += e.rstrip("\n") + "\n"
    return result


def cmd_dream(args):
    """Consolidate SOUL.md + MEMORY.md via structured, ID-based LLM dedup.

    Each candidate pool (one SOUL section, or one MEMORY section+layer) gets
    a batch remove/merge JSON decision from the LLM — never a whole-file
    rewrite. This means a malformed/truncated/partial LLM response can only
    ever leave one small pool unchanged; it can no longer silently delete
    sections it was never asked about (see 2026-05-11 MEMORY.md incident:
    a truncated whole-file rewrite dropped MUST NOT/PREFER/CONTEXT entirely
    and passed validation because the check only looked for "## MUST").
    """
    soul_path = Path(args.soul)
    memory_path = Path(args.memory)
    logs_dir = Path(args.logs)

    # --- SOUL consolidation ---
    if soul_path.exists():
        soul_content = soul_path.read_text(encoding="utf-8")
        sections = _parse_soul_sections(soul_content)
        entry_count = sum(len(v) for v in sections.values())
        if entry_count <= 20:
            print(f"Dream: SOUL {entry_count} entries (≤20), skipping", file=sys.stderr)
        else:
            print(f"Dream: consolidating SOUL.md ({entry_count} entries) via structured dedup...", file=sys.stderr)
            new_sections = dict(sections)
            for section in ("Preferences", "Patterns", "Context", "Interaction"):
                before = sections.get(section, [])
                after = _dedup_pool(before, {"section": section}, SOUL_DEDUP_SYSTEM)
                new_sections[section] = after
                if len(after) != len(before):
                    print(f"Dream: SOUL.{section} {len(before)} → {len(after)} entries", file=sys.stderr)
            # Identity: mechanical cap only (low volume, not worth an LLM call)
            if len(new_sections.get("Identity", [])) > 3:
                new_sections["Identity"] = new_sections["Identity"][-3:]

            file_count = len(find_sessions(logs_dir))
            updated_header = re.sub(r"Sessions:.*", f"Sessions: {file_count} files", soul_content)
            updated_header = re.sub(r"Last updated:.*", f"Last updated: {date.today()}", updated_header)
            new_content = _rebuild_soul(updated_header, new_sections)
            soul_path.write_text(new_content, encoding="utf-8")
            new_total = sum(len(v) for v in new_sections.values())
            print(f"Dream: SOUL consolidated {entry_count} → {new_total} entries", file=sys.stderr)
    else:
        print("Dream: SOUL.md not found, skipping", file=sys.stderr)

    # --- MEMORY consolidation ---
    if memory_path.exists():
        mem_content = memory_path.read_text(encoding="utf-8")
        layers = _parse_memory_layers(mem_content)
        rule_count = sum(len(l["Universal"]) + len(l["Project-specific"]) for l in layers.values())
        if rule_count <= 60:
            print(f"Dream: MEMORY has {rule_count} rules (≤60), skipping", file=sys.stderr)
        else:
            print(f"Dream: consolidating MEMORY.md ({rule_count} rules) via structured dedup...", file=sys.stderr)
            new_layers = {s: dict(l) for s, l in layers.items()}
            for section in _MEMORY_SECTIONS:
                for layer_name in ("Universal", "Project-specific"):
                    before = layers[section][layer_name]
                    after = _dedup_pool(before, {"section": section, "layer": layer_name}, MEMORY_DEDUP_SYSTEM)
                    new_layers[section][layer_name] = after
                    if len(after) != len(before):
                        print(f"Dream: MEMORY.{section}.{layer_name} {len(before)} → {len(after)} rules", file=sys.stderr)

            version_m = re.search(r"Version: (\d+)", mem_content)
            version = int(version_m.group(1)) + 1 if version_m else 1
            header = (
                f"# MEMORY.md — Behavioral Rules\n"
                f"> Source: SOUL.md + dream | Updated: {date.today()} | "
                f"Version: {version} (dream-consolidated)\n"
                f"> Precedence: explicit user instruction > MEMORY.md > project CLAUDE.md\n\n"
            )
            new_content = _rebuild_memory(header, new_layers)
            memory_path.write_text(new_content, encoding="utf-8")
            new_total = sum(len(l["Universal"]) + len(l["Project-specific"]) for l in new_layers.values())
            print(f"Dream: MEMORY consolidated {rule_count} → {new_total} rules", file=sys.stderr)
    else:
        print("Dream: MEMORY.md not found, skipping", file=sys.stderr)

    # --- Generate AGENTS.md from SOUL + MEMORY.Universal ---
    # Holistic narrative synthesis (like TencentDB's L3 persona doc) — this one
    # is NOT a structured diff, by design (same as their persona-generation
    # prompt). It must never be chunked: allow_chunking=False, plus a
    # duplicate-document guard as a second line of defense (see the 2026-05-09
    # incident where a chunked call produced two complete AGENTS.md docs
    # concatenated together).
    if soul_path.exists() and memory_path.exists():
        soul_body_match = re.search(r'^---\s*\n(.+)', soul_path.read_text(), re.S | re.M)
        soul_body = soul_body_match.group(1) if soul_body_match else ""

        mem_content = memory_path.read_text()
        universal_blocks = []
        for section in _MEMORY_SECTIONS:
            pattern = rf'^## {re.escape(section)}\s*\n(.*?)(?=^## |\Z)'
            m = re.search(pattern, mem_content, re.S | re.M)
            if not m:
                continue
            section_body = m.group(1)
            uni_m = re.search(r'### Universal[^\n]*\n(.*?)(?=^### |\Z)', section_body, re.S | re.M)
            if uni_m and uni_m.group(1).strip():
                universal_blocks.append(f"## {section}\n{uni_m.group(1).strip()}")

        if soul_body and universal_blocks:
            combined = soul_body + "\n\n---\n\n# MEMORY (Universal only)\n\n" + "\n\n".join(universal_blocks)
            print("Dream: generating AGENTS.md from SOUL + MEMORY.Universal...", file=sys.stderr)
            agents_content = call_engine(combined, AGENTS_SYSTEM, allow_chunking=False)
            header_hits = len(re.findall(r'^# AGENTS\.md', agents_content, re.M)) if agents_content else 0
            if agents_content and agents_content.strip().startswith("#") and header_hits <= 1:
                agents_path = soul_path.parent / "AGENTS.md"
                agents_path.write_text(agents_content.strip() + "\n", encoding="utf-8")
                lines = len(agents_content.splitlines())
                chars = len(agents_content)
                over_budget = " [OVER 6000-char budget]" if chars > 6000 else ""
                print(f"Dream: AGENTS.md generated ({lines} lines, {chars} chars){over_budget}", file=sys.stderr)
            elif header_hits > 1:
                print(f"Dream: LLM returned {header_hits} concatenated AGENTS.md documents, rejecting write", file=sys.stderr)
            else:
                print("Dream: LLM returned invalid AGENTS.md, skipping", file=sys.stderr)


def _parse_all_lesson_pits(lessons_path: Path) -> list[tuple[str, str]]:
    """Extract (slug, pit_text) from ALL LESSONS.md entries (regardless of absorbed status)."""
    if not lessons_path.exists():
        return []
    content = lessons_path.read_text(encoding="utf-8")
    entries = re.split(r'(?=^## [\w-]+$)', content, flags=re.M)
    result = []
    for entry in entries:
        m = re.match(r'^## ([\w-]+)', entry.strip())
        if not m:
            continue
        slug = m.group(1)
        pit_m = re.search(r'\*\*坑\*\*[：:]\s*(.+)', entry)
        if pit_m:
            result.append((slug, pit_m.group(1).strip()))
    return result


def _count_memory_rules(memory_path: Path) -> dict[str, int]:
    """Count rules per section in MEMORY.md."""
    counts = {"MUST": 0, "MUST NOT": 0, "PREFER": 0, "CONTEXT": 0}
    if not memory_path.exists():
        return counts
    content = memory_path.read_text(encoding="utf-8")
    current_section = None
    for line in content.splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
            if section in counts:
                current_section = section
            else:
                current_section = None
        elif current_section and line.strip().startswith("- "):
            counts[current_section] += 1
    return counts


def _soul_touched_on_day(soul_content: str, day: date) -> bool:
    """True if soul produced observations on `day`.

    soul tags new entries `<!-- new: DAY -->`. But `make leader` runs soul and
    distill in the same chain, and distill rewrites `new:` → `absorbed: today`
    (`_mark_absorbed_soul_entries`). So by the time `daily` runs, the day's
    `new:` tags are gone. An `absorbed: DAY` tag means the entry was *both*
    produced and consumed that day — the normal case under the leader chain —
    so it must still count as "soul ran that day".
    """
    return (
        f"<!-- new: {day} -->" in soul_content
        or f"<!-- absorbed: {day} -->" in soul_content
    )


def _check_rule_freshness(memory_path: Path, soul_path: Path) -> list[tuple[str, str]]:
    """Check which MEMORY.md rules have recent evidence in SOUL.md (last 30 days).

    Two tiers, decided per rule in order:
      Tier 1 (exact join): rule carries a `<!-- pk: xxx -->` tag → the pk is the
        ground-truth join key. pk in recent SOUL pks → 'evidenced'; not → 'stale'.
        This is a REAL verdict — no text matching involved.
      Tier 2 (legacy word-split): rule has NO pk tag → fall back to the old
        heuristic (split pk into words, whole-word match against rule prose).
        Only pk-less rules reach here.

    The distinction matters: a pk-bearing rule whose pk is stale must be 'stale'
    even if its words coincidentally match another recent pk. Tier 1 decides
    first so that case can't leak into Tier 2.

    Returns [(rule_text, status)] where status is 'evidenced' or 'stale'.
    """
    if not memory_path.exists() or not soul_path.exists():
        return []
    # Collect pk tags from SOUL entries touched (new/absorbed) in the last 30 days.
    # SOUL.md's current format is the 4-section Identity/Preferences/Patterns/Context
    # layout (no per-date ### blocks), so entries are parsed via _parse_soul_sections
    # and dated via their <!-- new: --> / <!-- absorbed: --> lifecycle tags.
    cutoff = date.today() - timedelta(days=30)
    soul_content = soul_path.read_text(encoding="utf-8")
    recent_pks = set()
    for entries in _parse_soul_sections(soul_content).values():
        for entry in entries:
            entry_dates = []
            for d in re.findall(r'<!--\s*(?:new|absorbed):\s*(\d{4}-\d{2}-\d{2})\s*-->', entry):
                try:
                    entry_dates.append(date.fromisoformat(d))
                except ValueError:
                    continue
            if entry_dates and max(entry_dates) >= cutoff:
                for pk_m in re.finditer(r'<!--\s*pk:\s*([\w-]+)\s*-->', entry):
                    recent_pks.add(pk_m.group(1))

    # Check each rule against recent pks
    memory_content = memory_path.read_text(encoding="utf-8")
    results = []
    for line in memory_content.splitlines():
        line_s = line.strip()
        if not line_s.startswith("- "):
            continue
        rule_text = line_s[2:].strip()
        # Tier 1: exact pk join (ground truth). pk tag, if present, decides.
        pk_m = re.search(r'<!--\s*pk:\s*([\w-]+)\s*-->', rule_text)
        if pk_m:
            results.append((rule_text, "evidenced" if pk_m.group(1) in recent_pks else "stale"))
            continue
        # Tier 2: legacy word-split heuristic for pk-less rules only.
        # (plain substring matching false-positived "review" -> "reviewer",
        # "commit" -> "Commits"; \b prevents matching inside a longer word)
        found = False
        for pk in recent_pks:
            # pk is kebab-case like "plan-before-act"; check each word
            for word in pk.split("-"):
                if len(word) > 2 and re.search(rf"\b{re.escape(word)}\b", rule_text, re.I):
                    found = True
                    break
            if found:
                break
        results.append((rule_text, "evidenced" if found else "stale"))
    return results


def cmd_daily(args):
    """Generate daily health report — pure mechanical analysis, no LLM."""
    logs_dir = Path(args.logs)
    target_date = args.date or date.today()
    soul_path = logs_dir / "SOUL.md"
    lessons_path = logs_dir / "LESSONS.md"
    memory_path = logs_dir / "MEMORY.md"
    genes_dir = logs_dir / "genes"
    reports_dir = logs_dir / "reports"
    month_dir = _report_month_dir(reports_dir, target_date)
    month_dir.mkdir(parents=True, exist_ok=True)
    out_path = month_dir / f"daily-health-{target_date}.md"

    sections = []
    findings = []

    # --- Section 1: 知识库摘要 ---
    s1 = [f"## 1. 知识库摘要\n"]
    # SOUL.md
    soul_total = soul_unabsorbed = soul_today = 0
    if soul_path.exists():
        sc = soul_path.read_text(encoding="utf-8")
        soul_total = len(re.findall(r'^- ', sc, re.M))  # count bullet entries
        soul_unabsorbed = sc.count("<!-- new:")  # new entries not yet absorbed
        # Same leader-chain issue as _soul_touched_on_day: distill rewrites
        # new: → absorbed: same day, so "今日 +N" must count absorbed: today too.
        soul_today = len(re.findall(rf'<!-- (?:new|absorbed): {target_date} -->', sc))
    # LESSONS.md
    les_total = les_absorbed = les_unabsorbed = les_review = 0
    if lessons_path.exists():
        lc = lessons_path.read_text(encoding="utf-8")
        les_total = len(re.findall(r'^## [\w-]+$', lc, re.M))
        les_absorbed = lc.count("absorbed: true")
        les_review = lc.count("needs-review")
        les_unabsorbed = lc.count("absorbed: false")
    # MEMORY.md
    mem_counts = _count_memory_rules(memory_path)
    mem_total = sum(mem_counts.values())
    # Genes
    gene_active = gene_stale = gene_degraded = 0
    reg_path = genes_dir / "registry.json"
    if reg_path.exists():
        try:
            reg = json.loads(reg_path.read_text(encoding="utf-8"))
            for g in reg.get("genes", []):
                s = g.get("decay_status", "")
                if s == "active": gene_active += 1
                elif s == "stale": gene_stale += 1
                elif s == "degraded": gene_degraded += 1
        except (json.JSONDecodeError, KeyError):
            pass

    s1.append(f"| 知识库 | 总计 | 详情 |")
    s1.append(f"|--------|------|------|")
    s1.append(f"| SOUL.md | {soul_total} 条观察 | 今日 +{soul_today}, unabsorbed {soul_unabsorbed} |")
    s1.append(f"| LESSONS.md | {les_total} 条教训 | absorbed {les_absorbed}, unabsorbed {les_unabsorbed}, needs-review {les_review} |")
    s1.append(f"| MEMORY.md | {mem_total} 条规则 | MUST {mem_counts['MUST']}, MUST_NOT {mem_counts['MUST NOT']}, PREFER {mem_counts['PREFER']}, CONTEXT {mem_counts['CONTEXT']} |")
    s1.append(f"| genes/ | {gene_active + gene_stale + gene_degraded} 个 Gene | active {gene_active}, stale {gene_stale}, degraded {gene_degraded} |")
    sections.append("\n".join(s1))
    # Gene 的 stale/degraded 不入 findings：新鲜度低 = "方法论最近没用"，
    # 属于没来用系统，不是需要动手的故障（用户裁定）。第 5 节仍展示状态。
    if les_review:
        findings.append(f"审查 {les_review} 条 needs-review 教训")

    # --- Section 2: 提升候选 ---
    s2 = ["## 2. 提升候选\n"]
    pattern_counts = extract_pattern_counts(soul_path, lessons_path)
    candidates = [(pk, cnt) for pk, cnt in pattern_counts.items() if cnt >= 3]
    if candidates:
        for pk, cnt in candidates:
            s2.append(f"- `{pk}` ({cnt} 天) → 可提取为 Gene: `scripts/extract-gene.sh {pk}`")
        findings.append(f"评估 {len(candidates)} 个 Gene 晋升候选")
    else:
        s2.append("无候选（需 pk ≥ 3 天）")
    sections.append("\n".join(s2))

    # --- Section 3: 潜在重复检测 ---
    s3 = ["## 3. 潜在重复检测\n"]
    pits = _parse_all_lesson_pits(lessons_path)
    duplicates = []
    for i in range(len(pits)):
        tokens_i = _tokenize_bigram(pits[i][1])
        for j in range(i + 1, len(pits)):
            sim = _jaccard(tokens_i, _tokenize_bigram(pits[j][1]))
            if sim >= 0.5:
                duplicates.append((pits[i][0], pits[j][0], round(sim, 2)))
    if duplicates:
        for a, b, sim in duplicates:
            s3.append(f"- `{a}` ↔ `{b}` (相似度 {sim:.0%})")
        findings.append(f"检查 {len(duplicates)} 对潜在重复教训")
    else:
        s3.append("未发现重复")
    sections.append("\n".join(s3))

    # --- Section 4: LESSONS 分布统计 ---
    s4 = ["## 4. LESSONS 分布统计\n"]
    month_counts: dict[str, int] = {}
    if lessons_path.exists():
        for m in re.finditer(r'>\s*(\d{4}-\d{2})-\d{2}\s*\|', lessons_path.read_text(encoding="utf-8")):
            ym = m.group(1)
            month_counts[ym] = month_counts.get(ym, 0) + 1
    if month_counts:
        s4.append("| 月份 | 新增 |")
        s4.append("|------|------|")
        for ym in sorted(month_counts):
            s4.append(f"| {ym} | {month_counts[ym]} |")
    else:
        s4.append("暂无数据")
    # High-value: pk≥3 days AND unabsorbed
    hv = [(pk, cnt) for pk, cnt in pattern_counts.items()
          if cnt >= 3 and any(pk in t for _, t in extract_unabsorbed_lessons(lessons_path))]
    if hv:
        s4.append(f"\n**高价值未吸收**: {', '.join(f'`{pk}`({cnt}天)' for pk, cnt in hv)}")
    sections.append("\n".join(s4))

    # --- Section 5: Gene 健康 ---
    s5 = ["## 5. Gene 健康\n"]
    if reg_path.exists():
        try:
            reg = json.loads(reg_path.read_text(encoding="utf-8"))
            genes = reg.get("genes", [])
            if genes:
                s5.append("| Gene | 状态 | 新鲜度 |")
                s5.append("|------|------|--------|")
                for g in genes:
                    s5.append(f"| {g.get('name', '?')} | {g.get('decay_status', '?')} | {g.get('freshness_score', '?')} |")
            else:
                s5.append("无 Gene")
        except (json.JSONDecodeError, KeyError):
            s5.append("registry.json 解析失败")
    else:
        s5.append("无 genes/ 目录")
    sections.append("\n".join(s5))

    # --- Section 6: MEMORY.md 规则新鲜度 ---
    s6 = ["## 6. 规则新鲜度\n"]
    freshness = _check_rule_freshness(memory_path, soul_path)
    stale_rules = [(r, s) for r, s in freshness if s == "stale"]
    if freshness:
        evidenced_n = sum(1 for _, s in freshness if s == "evidenced")
        s6.append(f"- 有近期证据: {evidenced_n}/{len(freshness)}")
        s6.append(f"- 可能过时: {len(stale_rules)}/{len(freshness)}")
        if stale_rules:
            s6.append("\n**可能过时的规则**:")
            for r, _ in stale_rules[:5]:
                s6.append(f"- {r[:80]}...")
            if len(stale_rules) > 5:
                s6.append(f"- ...及其他 {len(stale_rules) - 5} 条")
            findings.append(f"审查 {len(stale_rules)} 条可能过时的规则")
    else:
        s6.append("MEMORY.md 为空或无规则")
    sections.append("\n".join(s6))

    # --- Section 7: 蒸馏链路健康 ---
    s7 = ["## 7. 蒸馏链路健康（最近 7 天）\n"]
    s7.append("| 日期 | Sessions | 日报 | SOUL | LESSONS |")
    s7.append("|------|----------|------|------|---------|")
    soul_content = soul_path.read_text(encoding="utf-8") if soul_path.exists() else ""
    lessons_content = lessons_path.read_text(encoding="utf-8") if lessons_path.exists() else ""
    for d in range(7):
        day = target_date - timedelta(days=d)
        n_sessions = len(find_sessions(logs_dir, day))
        has_report = (_report_month_dir(reports_dir, day) / f"{day}.md").exists()
        has_soul = _soul_touched_on_day(soul_content, day)
        n_lessons = len(re.findall(rf'>\s*{day}\s*\|', lessons_content))
        s7.append(f"| {day} | {n_sessions} | {'✓' if has_report else '—'} | {'✓' if has_soul else '—'} | +{n_lessons} |")
    sections.append("\n".join(s7))

    # --- Section 9: 可操作建议 ---
    s9 = ["## 9. 可操作建议\n"]
    recommendations = []

    # 9a: Unabsorbed Preferences in SOUL
    if soul_path.exists():
        soul_content = soul_path.read_text(encoding="utf-8")
        # Count entries with <!-- new: --> but not <!-- absorbed: -->
        new_entries = len(re.findall(r'<!-- new: \d{4}-\d{2}-\d{2} -->', soul_content))
        absorbed_entries = len(re.findall(r'<!-- absorbed: \d{4}-\d{2}-\d{2} -->', soul_content))
        unabsorbed = new_entries - absorbed_entries
        if unabsorbed > 0:
            recommendations.append(
                f"SOUL 有 {unabsorbed} 条未吸收条目，建议 `make distill` 转化为 MEMORY 规则"
            )

    # 9b: Pattern with >=3 days but no Gene
    if genes_dir.is_dir():
        for pk, cnt in pattern_counts.items():
            if cnt >= 3 and not (genes_dir / pk).exists():
                recommendations.append(f"pk `{pk}` 出现 {cnt} 天，建议提取为 Gene")

    # 9c: Identity section too sparse
    if soul_path.exists():
        identity_section = re.search(r'## Identity\n(.*?)(?=\n## |\Z)', soul_content, re.S)
        identity_count = len(re.findall(r'^- ', identity_section.group(1), re.M)) if identity_section else 0
        if identity_count < 2:
            recommendations.append(
                "SOUL Identity 不足 2 条——AI 不知道你是谁，"
                "建议下次会话后 `make soul` 补充身份画像"
            )

    # 9d: Preferences without How
    if soul_path.exists():
        prefs_section = re.search(r'## Preferences\n(.*?)(?=\n## |\Z)', soul_content, re.S)
        if prefs_section:
            pref_entries = re.findall(r'^- (?:PREFER|REJECT).*', prefs_section.group(1), re.M)
            prefs_no_how = sum(1 for _ in pref_entries) - soul_content.count("How:")
            if prefs_no_how > 0:
                recommendations.append(
                    f"{prefs_no_how} 条 Preferences 缺 How（不可操作），建议补充或 `make dream` 整合"
                )

    if recommendations:
        for r in recommendations:
            s9.append(f"- {r}")
            findings.append(r)
    # 健康由 ALL findings 决定，不是只看第 9 节这组建议。曾有 91 条 stale
    # 规则进入待办、第 9 节却输出"系统状态健康"——检测无后果等于零效果
    if not findings:
        s9.append("无建议 — 系统状态健康")
    else:
        s9.append(f"\n**健康由 {len(findings)} 条待处理发现决定** —— 见第 8 节")

    # 第 8 节清单必须在 findings 完全收集后渲染（第 9 节会往里追加），
    # 否则清单漏掉建议项、计数却包含它们，两处显示不一致。
    s8 = ["## 8. 待办事项\n"]
    if findings:
        for i, t in enumerate(findings, 1):
            s8.append(f"{i}. {t}")
    else:
        s8.append("无待办 — 一切正常")

    # Read and clear skip buffer for today
    skips = read_and_clear_skip_buffer(logs_dir)
    if skips:
        by_source: dict = {}
        for s in skips:
            by_source.setdefault(s['source'], []).append(s)
        s8.append("\n### 今日 Skip 摘要\n")
        for source in ('lessons', 'distill', 'gene'):
            items = by_source.get(source, [])
            if not items:
                continue
            s8.append(f"\n**{source.upper()}** ({len(items)} 项跳过):")
            for item in items[:5]:
                reason = item.get('reason', item.get('decision', '?'))
                label = item.get('slug') or item.get('pk') or item.get('summary', '?')
                s8.append(f"- `{label}`: {reason}")
            if len(items) > 5:
                s8.append(f"- ...及其他 {len(items)-5} 项")

    sections.append("\n".join(s8))
    sections.append("\n".join(s9))

    # Write report
    header = f"# Daily Health Report — {target_date}\n"
    content = header + "\n\n".join(sections) + "\n"
    out_path.write_text(content, encoding="utf-8")
    print(f"OK {out_path}", file=sys.stderr)

    # Enforcement closure (see research in plan): detection without consequence
    # is zero effect (Gray & Scholz 1991: penalized inspections -22% injuries,
    # penalty-free inspections no effect; Best/Shah/Waseem 2021: detected-but-
    # unconsequenced audits deter nothing). In --strict mode, open findings
    # make the command fail so CI/local calls can't be green while unhealthy.
    if _should_fail_daily(findings, args.strict):
        print(f"daily: {len(findings)} open findings — non-zero exit (--strict)",
              file=sys.stderr)
        sys.exit(1)


def _should_fail_daily(findings: list, strict: bool) -> bool:
    """Enforcement gate: fail only in --strict mode AND when findings are open.

    Extracted from cmd_daily so the closure is unit-testable without running
    the whole report pipeline. Default (cron) mode stays soft — see the cron
    chain, which runs `make daily` inside `{ ... }` before a `;`-joined
    sync-memory, so a hard fail there would halt the upstream chain.
    """
    return bool(strict and findings)


def _intervention_stats(records: list[dict], scan_meta: dict, samples_n: int) -> dict:
    """Aggregate intervention records into the JSON SSOT.

    JSON is the source of truth and the markdown is rendered from it, because
    the point of this artifact is to be re-run in three months and diffed —
    prose diffs are unreadable.
    """
    # Not interventions: plan approvals, infrastructure failures, unrecoverable
    # directives, content-free acknowledgements, and the two classes the user
    # ruled normal collaboration (asking for status, handing over a new task).
    EXCLUDED = ("plan_approved", "plan_endorsed", "infra_noise", "unresolved",
                "noise_reply", "new_task",
                *(k for k, counted, _ in _TAXONOMY if not counted))
    CONFIDENCE = {
        "plan_approved": "高（正向标记）", "plan_endorsed": "高（认可前缀）",
        "plan_rejected": "高（结构性）",
        "infra_noise": "高", "unresolved": "高（作为未知）", "noise_reply": "高",
        "over_verification": "中（关键词）", "wrong_direction": "中（关键词）",
        "delegate_directed": "中（人工标注）", "plan_first": "中（人工标注）",
        "simpler_path": "中（人工标注）", "defect_report": "中（人工标注）",
        "context_supplied": "中（人工标注）",
        "progress_query": "中（正常协作）", "new_task": "低（兜底）",
        "counter_question": "低（关键词）",
    }
    classes: dict[str, int] = {}
    matrix: dict[str, int] = {}
    per_tool: dict[str, dict] = {}
    per_month: dict[str, dict] = {}
    samples: dict[str, list] = {}
    # Plan outcomes are tallied per event, independent of which class the event
    # landed in: an endorsed plan usually gets routed to delegate_directed
    # (because the endorsement is followed by "让 codex 评审"), and would
    # otherwise vanish from the denominator and re-inflate the rejection rate.
    plan_passed = plan_vetoed = 0

    for r in records:
        k = r["klass"]
        classes[k] = classes.get(k, 0) + 1
        matrix[f"{k}|{r['action']}"] = matrix.get(f"{k}|{r['action']}", 0) + 1
        if k == "plan_rejected":
            plan_vetoed += 1
        elif r["action"] == "ExitPlanMode" or k == "plan_approved":
            # Any other ExitPlanMode outcome (endorsed, delegated, context) is a
            # pass. Per the user's ruling, design questions are NOT passes —
            # those stay in plan_rejected above.
            plan_passed += 1
        tool = r["tool"]
        t = per_tool.setdefault(tool, {"hard": 0, "excluded": 0})
        if k in EXCLUDED:
            t["excluded"] += 1
        else:
            t["hard"] += 1
            month = r.get("month") or "unknown"
            m = per_month.setdefault(month, {})
            m[tool] = m.get(tool, 0) + 1
        # The catch-all gets a bigger sample: hand-labelling it is how new
        # classes get discovered, which is how _TAXONOMY was built.
        cap = samples_n * 2 if k == "new_task" else samples_n
        bucket = samples.setdefault(k, [])
        if len(bucket) < cap:
            bucket.append({
                "provenance": f"{r['session']}:msg{r['msg']}",
                "action": r["action"], "marker": r["marker"],
                "directive": r["directive"][:200],
            })

    for tool, meta in scan_meta["tools"].items():
        per_tool.setdefault(tool, {"hard": 0, "excluded": 0}).update({
            "sessions": meta["sessions"],
            "sessions_hit": meta["sessions_hit"],
            "agent_actions": meta["agent_actions"],
            "markers_wired": meta["markers_wired"],
            "covered": bool(meta["markers_wired"]),
        })

    hard_total = sum(v["hard"] for v in per_tool.values())
    actions_total = sum(v.get("agent_actions", 0) for v in per_tool.values())
    for tool, v in per_tool.items():
        acts = v.get("agent_actions", 0)
        v["rate_per_100"] = round(100 * v["hard"] / acts, 2) if acts else None

    rate_trend = {}
    for month, tools in sorted(per_month.items()):
        rate_trend[month] = {
            t: n for t, n in sorted(tools.items())
        }

    # Endorsed plans count as passed: "方向没问题, 让 codex 评审" is a pass.
    pr, pa = plan_vetoed, plan_passed
    return {
        "schema_version": 1,
        "generated_from": "ai_report.py interventions",
        "range": scan_meta["range"],
        "scan": {
            "files_scanned": scan_meta["files_scanned"],
            "duplicates_dropped": scan_meta["duplicates_dropped"],
            "events": len(records),
            "hard_interventions": hard_total,
            "agent_actions": actions_total,
            "rate_per_100_actions": round(100 * hard_total / actions_total, 3) if actions_total else None,
        },
        "excluded_classes": list(EXCLUDED),
        "classes": dict(sorted(classes.items(), key=lambda x: -x[1])),
        "class_confidence": CONFIDENCE,
        "class_action_matrix": dict(sorted(matrix.items(), key=lambda x: -x[1])),
        "per_tool": per_tool,
        "monthly_counts": rate_trend,
        "plan_rejection_rate": round(pr / (pr + pa), 3) if (pr + pa) else None,
        "samples": samples,
    }


def _render_intervention_report(st: dict) -> str:
    """Render the human-readable baseline from the JSON SSOT."""
    rng = st["range"]
    sc = st["scan"]
    out = [f"# 干预点基线报告 — {rng['since'] or '起始'} ~ {rng['until'] or '至今'}\n"]
    out.append("> Auto-generated from interventions-*.json — do not edit\n")

    s = ["## 1. 覆盖与方法\n",
         "| 工具 | sessions | 命中 sessions | agent 动作 | 已接标记 | 覆盖 |",
         "|------|----------|---------------|-----------|---------|------|"]
    for tool, v in sorted(st["per_tool"].items()):
        cov = "是" if v.get("covered") else "**未覆盖**"
        s.append(f"| {tool} | {v.get('sessions', 0)} | {v.get('sessions_hit', 0)} "
                 f"| {v.get('agent_actions', 0)} | {v.get('markers_wired', 0)} | {cov} |")
    s.append(f"\n- 扫描文件 {sc['files_scanned']}，按内容去重丢弃 {sc['duplicates_dropped']} 个冗余副本")
    s.append("- cursor/gemini 无已知中断标记，其 0 值是**未覆盖**而非无干预")
    out.append("\n".join(s))

    excl = sc["events"] - sc["hard_interventions"]
    s = ["## 2. 硬层口径\n",
         f"- 原始事件 {sc['events']}",
         f"- 排除 {excl}（" + "、".join(
             f"{k} {st['classes'].get(k, 0)}" for k in st["excluded_classes"]) + "）",
         f"- **硬干预 {sc['hard_interventions']}**",
         f"- agent 动作总数 {sc['agent_actions']}，全局率 {sc['rate_per_100_actions']}/100 动作"]
    out.append("\n".join(s))

    s = ["## 3. 率趋势（按工具分列）\n",
         "全局单一数字会误导：工具构成变化和仪表化差异都会压低它。只看分列。\n",
         "| 月份 | " + " | ".join(sorted(st["per_tool"])) + " |",
         "|------|" + "|".join(["-----"] * len(st["per_tool"])) + "|"]
    tools = sorted(st["per_tool"])
    for month, counts in st["monthly_counts"].items():
        s.append(f"| {month} | " + " | ".join(str(counts.get(t, 0)) for t in tools) + " |")
    # Flag tools whose low count may be under-instrumentation rather than
    # autonomy: markers are wired but almost no session ever hits one.
    weak = [t for t, v in st["per_tool"].items()
            if v.get("covered") and v.get("sessions", 0) >= 50
            and v.get("sessions_hit", 0) / max(1, v.get("sessions", 1)) < 0.15]
    if weak:
        s.append("\n**口径说明**：" + "、".join(weak) +
                 " 虽已接入标记，但命中 session 占比 <15%，其低值可能部分来自仪表化不足，"
                 "不能直接读作自主度高。")
    out.append("\n".join(s))

    s = ["## 4. class 分布\n", "| class | 数量 | 占比 | 置信度 |", "|-------|------|------|--------|"]
    tot = sc["events"] or 1
    for k, n in st["classes"].items():
        s.append(f"| {k} | {n} | {100*n/tot:.0f}% | {st['class_confidence'].get(k, '?')} |")
    out.append("\n".join(s))

    s = ["## 5. class x action 矩阵\n",
         "下一阶段写 pre-authorized / must-stop 策略的直接依据。\n",
         "| class | action | 数量 |", "|-------|--------|------|"]
    for key, n in list(st["class_action_matrix"].items())[:20]:
        k, a = key.split("|", 1)
        s.append(f"| {k} | {a} | {n} |")
    out.append("\n".join(s))

    s = ["## 6. 逐字样本（精度审计面）\n",
         "读这一段。若某类样本不成立，当场否决该类，别让它流入下游。\n"]
    for k in st["classes"]:
        bucket = st["samples"].get(k) or []
        if not bucket:
            continue
        s.append(f"\n### {k} ({st['classes'][k]}，展示 {len(bucket)})\n")
        for smp in bucket:
            d = smp["directive"].replace("\n", " ").strip() or "(无)"
            s.append(f"- `[{smp['action']}]` {d}")
            s.append(f"  - {smp['provenance']}")
    out.append("\n".join(s))

    s = ["## 7. 已知局限\n",
         f"- 兜底类 `new_task` 占 {100*st['classes'].get('new_task',0)/tot:.0f}%"
         "——机械分不出来的残余，读第 6 段样本贴标签可扩充 _TAXONOMY",
         "- cursor/gemini 未覆盖；codebuddy 仪表化不足",
         "- cursor 全部消息无 timestamp，日期过滤依赖 mtime fallback",
         "- _TAXONOMY 五类（delegate_directed/plan_first/simpler_path/"
         "defect_report/context_supplied）为人工标注 + 关键词匹配，非结构性判定",
         "- progress_query / new_task 经用户裁定为正常协作，不计入分子——"
         "否则\"减少干预\"会退化成\"让用户少说话\"",
         "- 未做软纠正层：实测 97% 命中是泛用词且约 11% 符号相反，一票否决"]
    if st["plan_rejection_rate"] is not None:
        s.append(f"- 计划否决率 {st['plan_rejection_rate']:.0%}"
                 "（已剔除 plan_endorsed：带\"方向没问题\"前缀的是批准+委派，非否决）")
    out.append("\n".join(s))
    return "\n\n".join(out) + "\n"


def cmd_interventions(args):
    """Mine user intervention points from session logs — mechanical, no LLM.

    Produces the autonomy baseline: where the user actually takes over, what the
    agent was doing, and what the user said instead. Deliberately NOT wired into
    cron/make daily — ~0.9 events/day would be an empty section you learn to skip.
    """
    logs_dir = Path(args.logs)
    since, until = args.since, args.until
    samples_n = max(1, args.samples)

    seen_hashes: set[str] = set()
    records: list[dict] = []
    tools_meta: dict[str, dict] = {}
    files_scanned = 0
    duplicates = 0

    for p in find_sessions(logs_dir):
        tool = p.relative_to(logs_dir).parts[0]
        if args.tool and tool != args.tool:
            continue
        # Session-level date filtering: cursor has no per-message timestamps at
        # all, so per-message filtering would drop it entirely. session_days()
        # has the mtime fallback.
        if since or until:
            days = session_days(p)
            if not days:
                continue
            if since and max(days) < since:
                continue
            if until and min(days) > until:
                continue
        try:
            digest = hashlib.md5(p.read_bytes()).hexdigest()
        except OSError:
            continue
        if digest in seen_hashes:
            duplicates += 1
            continue
        seen_hashes.add(digest)
        files_scanned += 1

        meta = tools_meta.setdefault(tool, {
            "sessions": 0, "sessions_hit": 0, "agent_actions": 0,
            "markers_wired": len(_HARD_MARKERS.get(tool, ())),
        })
        meta["sessions"] += 1
        meta["agent_actions"] += _count_agent_actions(p)

        recs = scan_session_interventions(p, tool)
        if recs:
            meta["sessions_hit"] += 1
        rel = str(p.relative_to(logs_dir))
        month = ""
        days = session_days(p)
        if days:
            month = f"{max(days):%Y-%m}"
        for r in recs:
            r["session"] = rel
            r["month"] = month
            records.append(r)

    scan_meta = {
        "range": {"since": since.isoformat() if since else None,
                  "until": until.isoformat() if until else None},
        "files_scanned": files_scanned,
        "duplicates_dropped": duplicates,
        "tools": tools_meta,
    }
    stats = _intervention_stats(records, scan_meta, samples_n)

    stamp_date = until or date.today()
    reports_dir = logs_dir / "reports"
    month_dir = _report_month_dir(reports_dir, stamp_date)
    month_dir.mkdir(parents=True, exist_ok=True)
    slug = f"{since or 'all'}_{until or 'all'}"
    json_path = month_dir / f"interventions-{slug}.json"
    json_path.write_text(json.dumps(stats, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    written = [json_path]
    if not args.json_only:
        md_path = month_dir / f"interventions-{slug}.md"
        md_path.write_text(_render_intervention_report(stats), encoding="utf-8")
        written.append(md_path)
    for w in written:
        print(f"OK {w}", file=sys.stderr)


def _count_agent_actions(path: Path) -> int:
    """Count assistant messages carrying >=1 tool_call — the intervention denominator.

    Sessions span 290-4947 messages, so per-session rates are useless for trend;
    agent actions are the thing that actually gets interrupted.
    """
    n = 0
    try:
        fh = open(path, encoding="utf-8")
    except OSError:
        return 0
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not str(obj.get("role", "")).startswith("assistant"):
                continue
            if _msg_parts(obj)[2]:
                n += 1
    return n


def cmd_sync_memory(args):
    """Commit and push ai-memory/ (which IS the ai-memory repo) to remote.

    ai-memory/ is a git clone of the ai-memory repository. All cmd_* functions
    write directly into it. This command simply stages, commits, and pushes.
    No file copying — ai-memory/ is the SSOT.
    """
    logs_dir = Path(args.logs)
    git_dir = logs_dir / ".git"
    if not git_dir.is_dir():
        print(f"sync-memory: {logs_dir} is not a git repo (no .git/)", file=sys.stderr)
        sys.exit(1)

    def git_error_text(exc):
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else str(exc.stderr or "")
        stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else str(exc.stdout or "")
        return f"{stderr}\n{stdout}".strip()

    def is_non_fast_forward(exc):
        detail = git_error_text(exc).lower()
        return "non-fast-forward" in detail or "fetch first" in detail

    committed = False
    try:
        subprocess.run(["git", "add", "-A"], cwd=str(logs_dir), check=True,
                      capture_output=True, timeout=30)
        # Commit new files when present, but still push when clean: a previous
        # run may have committed successfully and then lost the network race.
        result = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=str(logs_dir),
                               capture_output=True, timeout=10)
        if result.returncode != 0:
            today_str = date.today().isoformat()
            subprocess.run(["git", "commit", "-m", f"chore: sync {today_str}"],
                          cwd=str(logs_dir), check=True, capture_output=True, timeout=30)
            committed = True

        for attempt in range(3):
            try:
                subprocess.run(["git", "push"], cwd=str(logs_dir), check=True,
                              capture_output=True, timeout=120)
                if committed:
                    print("OK sync-memory: committed and pushed to ai-memory", file=sys.stderr)
                else:
                    print("sync-memory: no new changes; pending commits pushed", file=sys.stderr)
                return
            except subprocess.CalledProcessError as push_error:
                if attempt == 2 or not is_non_fast_forward(push_error):
                    raise
                print(f"sync-memory: remote advanced, rebasing ({attempt + 1}/2)", file=sys.stderr)
                try:
                    subprocess.run(["git", "pull", "--rebase"], cwd=str(logs_dir), check=True,
                                  capture_output=True, timeout=120)
                except subprocess.CalledProcessError:
                    # Restore the pre-rebase local commit. Never guess at a
                    # semantic merge for shared SOUL/MEMORY/LESSONS state.
                    subprocess.run(["git", "rebase", "--abort"], cwd=str(logs_dir),
                                  capture_output=True, timeout=30)
                    raise
    except subprocess.CalledProcessError as e:
        err = git_error_text(e)
        print(f"sync-memory git error: {err[:200]}", file=sys.stderr); sys.exit(1)
    except subprocess.TimeoutExpired:
        print("sync-memory: git operation timed out", file=sys.stderr); sys.exit(1)


def main():
    p = argparse.ArgumentParser(description="AI log report & soul builder")
    sub = p.add_subparsers(dest="cmd", required=True)
    default_logs = os.environ.get("AI_LOGS_DIR", "./ai-memory")
    r = sub.add_parser("report")
    r.add_argument("--date", type=date.fromisoformat, default=None)
    r.add_argument("--logs", default=default_logs)
    s = sub.add_parser("soul")
    s.add_argument("--date", type=date.fromisoformat, default=None)
    s.add_argument("--since", type=date.fromisoformat, default=None)
    s.add_argument("--logs", default=default_logs)
    s.add_argument("--soul", default=str(Path(default_logs) / "SOUL.md"))
    pu = sub.add_parser("push")
    pu.add_argument("--logs", default=default_logs)
    d = sub.add_parser("distill")
    d.add_argument("--logs", default=default_logs)
    d.add_argument("--soul", default=str(Path(default_logs) / "SOUL.md"))
    d.add_argument("--memory", default=str(Path(default_logs) / "MEMORY.md"))
    d.add_argument("--lessons", default=str(Path(default_logs) / "LESSONS.md"))
    d.add_argument("--force", action="store_true", help="Distill even with <7 entries")
    le = sub.add_parser("lessons")
    le.add_argument("--date", type=date.fromisoformat, default=None)
    le.add_argument("--logs", default=default_logs)
    le.add_argument("--lessons", default=str(Path(default_logs) / "LESSONS.md"))
    gh = sub.add_parser("gene-health")
    gh.add_argument("--genes-dir", default=str(Path(default_logs) / "genes"))
    da = sub.add_parser("daily")
    da.add_argument("--logs", default=default_logs)
    da.add_argument("--date", type=date.fromisoformat, default=None)
    da.add_argument("--strict", action="store_true",
                    help="exit 1 if any open findings remain (CI/local enforcement)")
    sm = sub.add_parser("sync-memory")
    sm.add_argument("--logs", default=default_logs)
    dr = sub.add_parser("dream")
    dr.add_argument("--logs", default=default_logs)
    dr.add_argument("--soul", default=str(Path(default_logs) / "SOUL.md"))
    dr.add_argument("--memory", default=str(Path(default_logs) / "MEMORY.md"))
    iv = sub.add_parser("interventions")
    iv.add_argument("--logs", default=default_logs)
    iv.add_argument("--since", type=date.fromisoformat, default=None)
    iv.add_argument("--until", type=date.fromisoformat, default=None)
    iv.add_argument("--tool", default=None)
    iv.add_argument("--samples", type=int, default=8)
    iv.add_argument("--json-only", action="store_true")
    args = p.parse_args()
    {"report": cmd_report, "soul": cmd_soul, "push": cmd_push,
     "distill": cmd_distill, "lessons": cmd_lessons,
     "gene-health": cmd_gene_health, "daily": cmd_daily,
     "sync-memory": cmd_sync_memory, "interventions": cmd_interventions,
     "dream": cmd_dream}[args.cmd](args)


if __name__ == "__main__":
    main()
