#!/usr/bin/env python3
"""
ai-distillery / ai-log-converter - v2.1.0
"Talk is cheap. Show me the code."

Convert AI conversation logs (Claude, Gemini, CodeBuddy, Codex) to readable formats.

Features:
- Multi-format output: Markdown (md), Plain Text (txt), Normalized JSONL (jsonl)
- Role-based extraction: user, assistant, or all.
- Timestamps: preserved per-message when available.
- Slop-Score: Reasoning-to-content ratio analysis (opt-in via --slop).
- Zero dependencies. Unix pipe friendly.
"""

__version__ = "2.2.0"

import argparse
import json
import re
import sys
from typing import Generator, Iterable, Optional

class Harness:
    def __init__(self, role_filter="all", no_thoughts=False, slop=False):
        self.role_filter = role_filter
        self.no_thoughts = no_thoughts
        self.slop = slop

    @staticmethod
    def clean(text: str) -> str:
        """Surgical text cleaning."""
        if not text: return ""
        patterns = [
            (r'<local-command-caveat>.*?</local-command-caveat>', ""),
            (r'<thinking>(.*?)</thinking>', r"[thought] \1"),
            (r'<local-command-stdout>(.*?)</local-command-stdout>', r"\1"),
            (r'\n{3,}', "\n\n")
        ]
        for p, r in patterns:
            text = re.sub(p, r, text, flags=re.DOTALL)
        return text.strip()

    def calculate_slop(self, msg: dict) -> float:
        """Ratio of reasoning/thoughts to actual output."""
        thought_len = sum(len(b['text']) for b in msg['content'] if b['type'] == 'thought')
        content_len = sum(len(b['text']) for b in msg['content'] if b['type'] == 'text')
        if content_len == 0: return 1.0 if thought_len > 0 else 0.0
        return round(thought_len / (thought_len + content_len), 3)

    def pipeline(self, source: Iterable[dict], mapper_func) -> Generator[dict, None, None]:
        for entry in source:
            for msg in mapper_func(entry):
                # Standardize
                msg['content'] = [b for b in msg['content'] if b.get('text') or b.get('input') or b.get('content')]

                # Thoughts
                new_content = []
                for b in msg['content']:
                    if b['type'] == 'text' and any(x in b['text'] for x in ('[thought]', '[thinking]')):
                        text = b['text'].replace('[thought] ', '').replace('[thinking] ', '')
                        new_content.append({'type': 'thought', 'text': text})
                    else:
                        new_content.append(b)
                msg['content'] = new_content

                # Meta: preserve mapper-provided meta (e.g. timestamp), then optionally add slop
                msg['meta'] = msg.get('meta', {})
                if self.slop:
                    msg['meta']['slop'] = self.calculate_slop(msg)

                # Filter
                role = msg['role'].lower()
                if self.role_filter != "all":
                    if self.role_filter == "user" and "user" not in role: continue
                    if self.role_filter == "assistant" and "assistant" not in role: continue

                if self.no_thoughts:
                    msg['content'] = [b for b in msg['content'] if b['type'] != 'thought']
                    if not msg['content']: continue

                yield msg

# === Mappers ===

def map_claude(entry: dict) -> Generator[dict, None, None]:
    if entry.get("type") not in ("user", "assistant", "progress") or entry.get("isMeta"): return
    agent_id = entry.get("subagentId") or entry.get("agentId") or entry.get("data", {}).get("agentId")
    data = entry.get("message") or entry.get("data", {}).get("message", {}).get("message") or {}
    if not data and entry.get("type") == "progress":
        prompt = entry.get("data", {}).get("prompt")
        if prompt: data = {"role": "user", "content": prompt}
    if not data: return
    role = data.get("role", "assistant")
    if agent_id and "user" not in role: role = f"assistant ({agent_id[:8]})"
    content = data.get("content", [])
    if isinstance(content, str): content = [{"type": "text", "text": content}]
    res = []
    for b in content:
        t = b.get("type")
        if t == "text": res.append({"type": "text", "text": Harness.clean(b.get("text", ""))})
        elif t == "tool_use": res.append({"type": "tool_call", "name": b.get("name"), "input": b.get("input")})
        elif t == "tool_result": res.append({"type": "tool_result", "content": b.get("content")})
    if res: yield {"role": role, "content": res, "meta": {"timestamp": entry.get("timestamp")}}

def map_gemini(entry: dict) -> Generator[dict, None, None]:
    msgs = entry.get("messages", [entry]) if isinstance(entry.get("messages"), list) else [entry]
    for m in msgs:
        if m.get("type") == "info": continue
        role = m.get("type", "assistant").replace("gemini", "assistant").replace("model", "assistant")
        res = []
        parts = m.get("parts", [])
        if not parts and "content" in m: parts = m["content"] if isinstance(m["content"], list) else [{"text": m["content"]}]
        for p in parts:
            if isinstance(p, str): res.append({"type": "text", "text": Harness.clean(p)})
            elif "text" in p: res.append({"type": "text", "text": Harness.clean(p["text"])})
            elif "functionCall" in p: res.append({"type": "tool_call", "name": p["functionCall"].get("name"), "input": p["functionCall"].get("args")})
            elif "functionResponse" in p: res.append({"type": "tool_result", "name": p["functionResponse"].get("name"), "content": p["functionResponse"].get("response")})
        for t in m.get("thoughts", []):
            txt = t.get("text") or t.get("description")
            if txt: res.append({"type": "thought", "text": txt})
        if res: yield {"role": role, "content": res, "meta": {"timestamp": m.get("timestamp")}}

def map_codebuddy(entry: dict) -> Generator[dict, None, None]:
    etype = entry.get("type", "")
    ts = entry.get("timestamp") or entry.get("created_at")
    if etype == "message":
        res = []
        for b in entry.get("content", []):
            if b.get("type") in ("input_text", "output_text"):
                text = Harness.clean(b.get("text", ""))
                if text: res.append({"type": "text", "text": text})
        if res: yield {"role": entry.get("role", "unknown"), "content": res, "meta": {"timestamp": ts}}
    elif etype == "function_call":
        args = entry.get("arguments", "{}")
        yield {"role": "assistant", "content": [{"type": "tool_call", "name": entry.get("name"), "input": json.loads(args) if isinstance(args, str) else args}], "meta": {"timestamp": ts}}
    elif etype in ("function_call_result", "function_call_output"):
        output = entry.get("output", {})
        text = output.get("text", "") if isinstance(output, dict) else str(output)
        yield {"role": "tool", "content": [{"type": "tool_result", "name": entry.get("name"), "content": text}], "meta": {"timestamp": ts}}

def map_codex(entry: dict) -> Generator[dict, None, None]:
    if entry.get("type") != "response_item": return
    p = entry.get("payload", {})
    ptype = p.get("type", "")
    ts = entry.get("timestamp") or entry.get("created_at")
    if ptype == "message":
        role = p.get("role", "unknown")
        if role in ("developer", "system"): return
        res = []
        for b in p.get("content", []):
            if b.get("type") in ("input_text", "output_text"):
                text = Harness.clean(b.get("text", ""))
                if text: res.append({"type": "text", "text": text})
        if res: yield {"role": role, "content": res, "meta": {"timestamp": ts}}
    elif ptype == "function_call":
        args = p.get("arguments", "{}")
        yield {"role": "assistant", "content": [{"type": "tool_call", "name": p.get("name"), "input": json.loads(args) if isinstance(args, str) else args}], "meta": {"timestamp": ts}}
    elif ptype == "function_call_output":
        yield {"role": "tool", "content": [{"type": "tool_result", "name": p.get("name"), "content": p.get("output", "")}], "meta": {"timestamp": ts}}

MAPPER_REGISTRY = {"claude": map_claude, "gemini": map_gemini, "codebuddy": map_codebuddy, "codex": map_codex}
DETECT_PEEK_LIMIT = 50

# === IO and Detect ===

def is_metadata_entry(sample: dict) -> bool:
    if sample.get("isMeta") or sample.get("isSummary"):
        return True

    stype = sample.get("type")
    if stype in ("info", "session_meta", "event_msg", "file-history-snapshot", "topic"):
        return True

    if stype == "response_item":
        payload = sample.get("payload", {})
        ptype = payload.get("type")
        if ptype == "reasoning":
            return True
        if ptype == "message" and payload.get("role") in ("system", "developer"):
            return True

    return False

def detect_format(samples: Iterable[dict]) -> Optional[str]:
    for sample in samples:
        if not isinstance(sample, dict) or is_metadata_entry(sample):
            continue

        if "type" in sample and ("messageId" in sample or "message" in sample or "snapshot" in sample):
            return "claude"
        if "messages" in sample or sample.get("type") in ("user", "model", "gemini"):
            return "gemini"
        if "type" in sample and "payload" in sample:
            return "codex"
        if "type" in sample and "role" in sample and "content" in sample:
            return "codebuddy"

    return None

def main():
    parser = argparse.ArgumentParser(description="ai-distillery: Convert AI conversation logs to readable formats")
    parser.add_argument("input", nargs="?", default="-")
    parser.add_argument("output", nargs="?", default="-")
    parser.add_argument("-f", "--format", choices=list(MAPPER_REGISTRY.keys()))
    parser.add_argument("-t", "--type", choices=["md", "txt", "jsonl"], default="md")
    parser.add_argument("-r", "--role", choices=["user", "assistant", "all"], default="all")
    parser.add_argument("--no-thoughts", action="store_true")
    parser.add_argument("--slop", action="store_true", help="Calculate and display Slop Score (reasoning/output ratio)")
    args = parser.parse_args()

    inf = sys.stdin if args.input == "-" else open(args.input, "r", encoding="utf-8")
    outf = sys.stdout if args.output == "-" else open(args.output, "w", encoding="utf-8")
    
    try:
        fmt, peek_buffer = args.format, []
        
        # 1. Read a sample to detect format
        first_line = inf.readline()
        if not first_line: return
        
        try:
            # Try parsing as JSONL first
            obj = json.loads(first_line)
            peek_buffer.append(obj)
            if not fmt:
                for _ in range(DETECT_PEEK_LIMIT):
                    fmt = detect_format(peek_buffer)
                    if fmt:
                        break
                    line = inf.readline()
                    if not line:
                        break
                    if not line.strip():
                        continue
                    try:
                        peek_buffer.append(json.loads(line))
                    except:
                        continue
            is_jsonl = True
        except:
            # It's either a multi-line JSON or garbage
            is_jsonl = False
            full_content = first_line + inf.read()
            try:
                data = json.loads(full_content)
                if not fmt:
                    fmt = detect_format(data if isinstance(data, list) else [data])
            except:
                print("Error: Invalid JSON.", file=sys.stderr); sys.exit(1)

        if not fmt: print("Error: Unknown format.", file=sys.stderr); sys.exit(1)
        harness = Harness(role_filter=args.role, no_thoughts=args.no_thoughts, slop=args.slop)
        
        # 2. Define data source
        def stream_source():
            if is_jsonl:
                yield from peek_buffer
                for line in inf:
                    if line.strip():
                        try: yield json.loads(line)
                        except: continue
            else:
                yield from (data if isinstance(data, list) else [data])

        msgs, count = harness.pipeline(stream_source(), MAPPER_REGISTRY[fmt]), 0
        for m in msgs:
            if args.type == "jsonl": outf.write(json.dumps(m, ensure_ascii=False) + "\n")
            elif args.type == "txt":
                text = " ".join(b['text'] for b in m['content'] if b['type'] == 'text')
                outf.write(f"[{m['role'].upper()}] {text}\n\n")
            else:
                meta = m.get('meta', {})
                ts = f" `{meta['timestamp']}`" if meta.get('timestamp') else ""
                slop = f" (Slop: {meta['slop']})" if meta.get('slop', 0) > 0 else ""
                outf.write(f"### {m['role'].capitalize()}{ts}{slop}\n\n")
                for b in m['content']:
                    if b['type'] == 'text': outf.write(f"{b['text']}\n\n")
                    elif b['type'] == 'thought': outf.write(f"> *Thought*: {b['text']}\n\n")
                    elif b['type'] == 'tool_call': outf.write(f"**Tool Call: `{b['name']}`**\n```json\n{json.dumps(b['input'], indent=2)}\n```\n\n")
                    elif b['type'] == 'tool_result': outf.write(f"**Tool Result**\n```json\n{json.dumps(b['content'], indent=2) if isinstance(b['content'], dict) else b['content']}\n```\n\n")
                outf.write("---\n\n")
            count += 1
    finally:
        if inf is not sys.stdin: inf.close()
        if outf is not sys.stdout: outf.close()

if __name__ == "__main__": main()
