#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Antigravity CLI (agy) Status Line Script.
Configured via ~/.gemini/antigravity-cli/settings.json -> statusLine.
Reads CLI state JSON from stdin, enriches with real-time step/avg TPS & cache hit rate
from transcript_path, and prints a 2-line ANSI status bar below the prompt box.
"""

import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from telemetry_core import parse_conversation_transcript

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
MAGENTA = "\033[35m"
BLUE = "\033[34m"
RED = "\033[31m"


def fmt_k(n):
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def make_bar(pct, width=10):
    pct = max(0.0, min(100.0, float(pct or 0.0)))
    filled = int(round((pct / 100.0) * width))
    color = RED if pct >= 80 else (YELLOW if pct >= 20 else GREEN)
    return f"{color}{'█' * filled}{DIM}{'░' * (width - filled)}{RESET}"


def main():
    raw = sys.stdin.read()
    if not raw.strip():
        return
    payload = json.loads(raw)

    model_obj = payload.get("model") or {}
    model_name = model_obj.get("display_name") or model_obj.get("id") or "Auto"
    effort = (model_obj.get("effort") or "").upper()
    if effort and f"({effort.lower()})" not in model_name.lower():
        model_tag = f"{model_name} [{effort}]"
    else:
        model_tag = model_name

    agent_state = payload.get("agent_state") or "idle"
    state_icons = {
        "working": f"{GREEN}● 思考中{RESET}",
        "tool_use": f"{CYAN}⚙ 工具执行中{RESET}",
        "reviewing": f"{YELLOW}👁 待审阅{RESET}",
        "error": f"{RED}✖ 错误{RESET}",
        "idle": f"{DIM}○ 空闲{RESET}",
    }
    state_str = state_icons.get(agent_state, f"{CYAN}● {agent_state}{RESET}")

    cw = payload.get("context_window") or {}
    ctx_limit = int(cw.get("context_window_size") or 1048576)
    used_pct = float(cw.get("used_percentage") or 0.0)
    cur_usage = cw.get("current_usage") or {}
    cur_in = int(cur_usage.get("input_tokens") or 0) + int(
        cur_usage.get("cache_read_input_tokens") or 0
    )

    # Enrich with transcript.jsonl TPS & Cache Hit Rate
    tpath = payload.get("transcript_path")
    tps_latest = "—"
    tps_avg = "—"
    cache_hit = "—"
    grand_tot = int(cw.get("total_input_tokens") or 0) + int(
        cw.get("total_output_tokens") or 0
    )

    if tpath and os.path.exists(tpath):
        parsed = parse_conversation_transcript(tpath)
        if parsed:
            tps_latest = f"{parsed['tps']['latest']}"
            tps_avg = f"{parsed['tps']['session_avg']}"
            cache_hit = f"{parsed['cache']['current_hit_rate_pct']}%"
            grand_tot = parsed["tokens"]["session_grand_total"]
            if cur_in == 0:
                cur_in = parsed["context"]["current_tokens"]
                used_pct = parsed["context"]["used_pct"]

    bar = make_bar(used_pct, width=10)
    exceed_warn = f" {YELLOW}⚠>200k{RESET}" if payload.get("exceeds_200k_tokens") or cur_in >= 200000 else ""

    # Line 1: State | Model & Effort | Real-time & Avg TPS | Context Window Bar
    line1 = (
        f"{state_str} {BOLD}{MAGENTA}{model_tag}{RESET} │ "
        f"⚡ TPS: {BOLD}{GREEN}{tps_latest}{RESET}{DIM}/{tps_avg} tok/s{RESET} │ "
        f"📏 上下文: {bar} {BOLD}{fmt_k(cur_in)}{RESET}{DIM}/{fmt_k(ctx_limit)} ({used_pct:.1f}%){RESET}{exceed_warn}"
    )

    # Line 2: Cumulative Tokens | Cache Hit | Subagents/Tasks | Quota & Cost
    extras = [
        f"∑ 累计: {BOLD}{fmt_k(grand_tot)}{RESET} tok",
        f"🎯 缓存命中: {BOLD}{CYAN}{cache_hit}{RESET}",
    ]

    subagents = payload.get("subagents") or []
    running_subs = sum(1 for s in subagents if s.get("status") == "running")
    if subagents:
        extras.append(f"🤖 子Agent: {running_subs}/{len(subagents)}")

    task_count = payload.get("task_count") or 0
    if task_count:
        extras.append(f"⏳ 后台任务: {task_count}")

    quota = payload.get("quota") or {}
    for qk, qv in quota.items():
        if isinstance(qv, dict) and "remaining_fraction" in qv:
            rem = float(qv["remaining_fraction"]) * 100.0
            extras.append(f"🔋 配额({qk}): {rem:.0f}%")
            break

    cost = payload.get("cost") or {}
    if "total_usd" in cost and cost["total_usd"] is not None:
        extras.append(f"💰 ${float(cost['total_usd']):.3f}")

    line2 = " │ ".join(extras)
    sys.stdout.write(line1 + "\n" + line2 + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
