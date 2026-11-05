#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Core telemetry parser and calculator for Antigravity Telemetry HUD.
Uses only Python 3 standard library for zero-dependency portability across macOS/Linux.
"""

import glob
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone, timedelta

HOME_DIR = os.path.expanduser("~")
BRAIN_DIR = os.path.join(HOME_DIR, ".gemini", "antigravity", "brain")
ANNOTATIONS_DIR = os.path.join(HOME_DIR, ".gemini", "antigravity", "annotations")
SUMMARIES_DB = os.path.join(HOME_DIR, ".gemini", "antigravity", "conversation_summaries.db")
CONVERSATIONS_DB_DIR = os.path.join(HOME_DIR, ".gemini", "antigravity", "conversations")
SIDECAR_DATA_DIR = os.path.join(
    HOME_DIR, ".gemini", "antigravity", "sidecar_data", "agy-telemetry-hud", "data"
)
LIVE_STATE_FILE = os.path.join(SIDECAR_DATA_DIR, "live_state.json")

# In-memory cache keyed by transcript path -> (mtime, size, parsed_summary)
_TRANSCRIPT_CACHE = {}
# In-memory cache keyed by annotation pbtxt path -> (mtime_ns, view_time_ns)
_ANNOTATION_CACHE = {}
VIEW_TIME_RE = re.compile(
    r"last_user_view_time:\s*\{\s*seconds:\s*(\d+)(?:\s*nanos:\s*(\d+))?"
)

MODEL_CONTEXT_LIMITS = [
    (r"gemini", 1048576),
    (r"claude", 200000),
    (r"gpt-oss", 128000),
]

EFFORT_RE = re.compile(
    r"^(.*?)\s*\((Low|Medium|High|Max|Fast|Thinking|Minimal|Ultra)\)\s*$",
    re.IGNORECASE,
)
SLUG_EFFORT_RE = re.compile(
    r"^(.*?)[-_](low|medium|high|max|fast|thinking|minimal|ultra)$",
    re.IGNORECASE,
)
SETTINGS_MODEL_RE = re.compile(
    r"Model Selection`\s*from\s*.*?\s*to\s*([^\n]+?)\.(?:\s+No need|\s*$)",
    re.IGNORECASE,
)
USER_REQ_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL)
TOOL_CREATED_RE = re.compile(r"Created At:\s*([0-9T:\.\-\+Z]+)")
TOOL_COMPLETED_RE = re.compile(r"Completed At:\s*([0-9T:\.\-\+Z]+)")


def parse_iso_ts(ts_str):
    if not ts_str:
        return None
    try:
        if ts_str.endswith("Z"):
            ts_str = ts_str[:-1] + "+00:00"
        return datetime.fromisoformat(ts_str).timestamp()
    except Exception:
        return None


def resolve_context_limit(model_str):
    low = (model_str or "").lower()
    for pat, limit in MODEL_CONTEXT_LIMITS:
        if re.search(pat, low):
            return limit
    return 1048576


def _prettify_model_slug(slug_str):
    s = slug_str.strip()
    if not s or " " in s:
        return s
    # Collapse hyphenated decimal versions like -5-5 or -3-8 into 5.5 / 3.8
    s = re.sub(r"-(\d+)-(\d+)(?=-|$)", r" \1.\2", s)
    parts = [p for p in re.split(r"[-_]+", s) if p]
    out = []
    for p in parts:
        low = p.lower()
        if low == "gpt":
            out.append("GPT")
        elif low == "oss":
            out.append("OSS")
        else:
            out.append(p[0].upper() + p[1:] if p else p)
    return " ".join(out)


def split_model_and_effort(raw_model_str, fallback_effort=None):
    raw = (raw_model_str or "").strip()
    if not raw or raw.lower() in ("none", "auto", "unknown"):
        return ("Auto / Default", fallback_effort or "Default", "Auto / Default")
    m = EFFORT_RE.match(raw)
    if m:
        base = m.group(1).strip()
        effort = m.group(2).strip().capitalize()
        return (base, effort, f"{base} ({effort})")
    ms = SLUG_EFFORT_RE.match(raw)
    if ms:
        base = _prettify_model_slug(ms.group(1))
        effort = ms.group(2).strip().capitalize()
        return (base, effort, f"{base} ({effort})")
    base = _prettify_model_slug(raw)
    effort = (fallback_effort or "Default").capitalize()
    return (base, effort, base if effort == "Default" else f"{base} ({effort})")


def load_live_state():
    try:
        if os.path.exists(LIVE_STATE_FILE):
            with open(LIVE_STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {"conversations": {}, "active_conversation_id": None}


def save_live_state(state):
    try:
        os.makedirs(SIDECAR_DATA_DIR, exist_ok=True)
        tmp_path = LIVE_STATE_FILE + f".tmp.{os.getpid()}"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp_path, LIVE_STATE_FILE)
    except Exception:
        pass


def get_db_conversation_run_state(conv_id):
    """
    Read the authoritative run status from Antigravity's SQLite databases
    (conversation_summaries.db and conversations/<cid>.db).
    When the user clicks Stop to cancel a running task, language_server skips the Stop hook
    but immediately updates conversation_summaries.status to CASCADE_RUN_STATUS_IDLE (not_fully_idle=0)
    and/or marks the step status=6 (CANCELED) in conversations/<cid>.db.
    """
    is_db_idle = None
    db_mod_ts = None
    try:
        if os.path.exists(SUMMARIES_DB):
            conn = sqlite3.connect(f"file:{SUMMARIES_DB}?mode=ro", uri=True, timeout=0.15)
            cur = conn.cursor()
            cur.execute(
                "SELECT status, not_fully_idle, last_modified_time FROM conversation_summaries WHERE conversation_id = ?",
                (conv_id,),
            )
            row = cur.fetchone()
            conn.close()
            if row:
                st_str, not_idle, mod_str = row[0] or "", bool(row[1]), row[2] or ""
                if st_str in ("CASCADE_RUN_STATUS_IDLE", "CASCADE_RUN_STATUS_CANCELING") or not not_idle:
                    is_db_idle = True
                elif st_str in ("CASCADE_RUN_STATUS_RUNNING", "CASCADE_RUN_STATUS_BUSY"):
                    is_db_idle = False
                if mod_str:
                    db_mod_ts = parse_iso_ts(str(mod_str).replace(" ", "T"))
    except Exception:
        pass

    try:
        cdb = os.path.join(CONVERSATIONS_DB_DIR, f"{conv_id}.db")
        if os.path.exists(cdb):
            conn2 = sqlite3.connect(f"file:{cdb}?mode=ro", uri=True, timeout=0.15)
            cur2 = conn2.cursor()
            cur2.execute("SELECT status FROM steps ORDER BY idx DESC LIMIT 1")
            srow = cur2.fetchone()
            conn2.close()
            if srow and srow[0] in (6, 7):  # 6 = CORTEX_STEP_STATUS_CANCELED, 7 = ERROR
                is_db_idle = True
    except Exception:
        pass

    return is_db_idle, db_mod_ts


def get_displayed_conversation_id():
    """
    Determine the conversation currently displayed in the Antigravity UI by reading
    ~/.gemini/antigravity/annotations/<cid>.pbtxt (written by conversationViewing/viewChanged).
    When switching from A -> B, Antigravity writes A.pbtxt then B.pbtxt with the same
    last_user_view_time, so sorting by (view_time_ns, st_mtime_ns) descending identifies B.
    """
    if not os.path.isdir(ANNOTATIONS_DIR):
        return None
    best_key = None
    best_cid = None
    try:
        for entry in os.scandir(ANNOTATIONS_DIR):
            if not entry.name.endswith(".pbtxt"):
                continue
            cid = entry.name[:-6]
            try:
                st = entry.stat()
                mtime_ns = st.st_mtime_ns
            except OSError:
                continue
            cached = _ANNOTATION_CACHE.get(entry.path)
            if cached and cached[0] == mtime_ns:
                view_ns = cached[1]
            else:
                view_ns = 0
                try:
                    with open(entry.path, "r", encoding="utf-8", errors="ignore") as f:
                        txt = f.read()
                    m = VIEW_TIME_RE.search(txt)
                    if m:
                        sec = int(m.group(1))
                        nanos = int(m.group(2) or 0)
                        view_ns = sec * 1_000_000_000 + nanos
                except Exception:
                    pass
                _ANNOTATION_CACHE[entry.path] = (mtime_ns, view_ns)
            key = (view_ns, mtime_ns)
            if best_key is None or key > best_key:
                best_key = key
                best_cid = cid
    except Exception:
        pass
    return best_cid


def extract_clean_title(text, max_len=42):
    if not text:
        return "未命名会话"
    m = USER_REQ_RE.search(text)
    body = m.group(1).strip() if m else text.strip()
    body = re.sub(r"<[^>]+>", "", body).strip()
    first_line = next((ln.strip() for ln in body.splitlines() if ln.strip()), "未命名会话")
    if len(first_line) > max_len:
        return first_line[:max_len] + "…"
    return first_line


def parse_conversation_transcript(transcript_path, hook_conv_state=None):
    """
    Parse a conversation's transcript.jsonl and return full telemetry metrics.
    Uses (mtime, size) caching for base step parsing, then overlays real-time elapsed timers.
    """
    if not os.path.exists(transcript_path):
        return None

    try:
        st = os.stat(transcript_path)
        mtime = st.st_mtime
        fsize = st.st_size
    except OSError:
        return None

    conv_id = os.path.basename(
        os.path.dirname(os.path.dirname(os.path.dirname(transcript_path)))
    )
    hook_conv_state = hook_conv_state or {}
    step_end_map = hook_conv_state.get("step_end_times", {})

    cached = _TRANSCRIPT_CACHE.get(transcript_path)
    if cached and cached[0] == mtime and cached[1] == fsize:
        base = cached[2]
    else:
        steps = []
        try:
            with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        steps.append(json.loads(line))
                    except Exception:
                        continue
        except Exception:
            return None

        if not steps:
            return None

        # 1. Identify model changes, title, and logical user turns
        #    (mid-turn supplementary user messages while a turn is active do NOT start a new turn or reset the turn timer)
        raw_model = None
        conv_title = "未命名会话"
        user_turn_indices = []
        turn_active = False

        for idx, s in enumerate(steps):
            stype = s.get("type")
            if stype == "USER_INPUT":
                content = s.get("content", "")
                if conv_title == "未命名会话" and content:
                    conv_title = extract_clean_title(content)
                for m in SETTINGS_MODEL_RE.finditer(content):
                    raw_model = m.group(1).strip()
                if s.get("source") == "USER_EXPLICIT":
                    ts = parse_iso_ts(s.get("created_at"))
                    if not turn_active or not user_turn_indices:
                        user_turn_indices.append(idx)
                        turn_active = True
                    else:
                        cur_start = user_turn_indices[-1]
                        has_planner = any(
                            steps[k].get("type") == "PLANNER_RESPONSE"
                            for k in range(cur_start, idx)
                        )
                        prev_ts = parse_iso_ts(steps[idx - 1].get("created_at")) or ts or 0
                        has_sys_restart = (
                            idx + 1 < len(steps)
                            and steps[idx + 1].get("type") == "SYSTEM_MESSAGE"
                        )
                        if not has_planner or has_sys_restart or (ts and prev_ts and (ts - prev_ts) > 180):
                            user_turn_indices[-1] = idx
            elif stype == "PLANNER_RESPONSE":
                if not (s.get("tool_calls") or []):
                    turn_active = False

        last_turn_start_pos = user_turn_indices[-1] if user_turn_indices else 0
        turn_start_ts = (
            parse_iso_ts(steps[last_turn_start_pos].get("created_at"))
            if steps and last_turn_start_pos < len(steps)
            else None
        ) or mtime
        current_turn_prompt = (
            extract_clean_title(steps[last_turn_start_pos].get("content", ""), 64)
            if steps and last_turn_start_pos < len(steps)
            else conv_title
        )

        # 2. Iterate through steps to compute per-step LLM time, tool time, tokens, and cache hit rate
        planner_steps = []
        tool_counts = {}
        total_tool_calls = 0
        total_tool_duration_s = 0.0
        turn_tool_duration_s = 0.0
        cache_breaks = []
        prev_cache_hit = None

        first_user_bytes = 0
        msg_bytes = 0
        tool_bytes = 0
        thinking_bytes = 0

        n_steps = len(steps)
        for i, s in enumerate(steps):
            stype = s.get("type")
            if stype == "USER_INPUT":
                b = len((s.get("content") or "").encode("utf-8", errors="ignore"))
                if i == 0:
                    first_user_bytes = b
                msg_bytes += b
            elif stype == "GENERIC":
                tool_bytes += len((s.get("content") or "").encode("utf-8", errors="ignore"))
            elif stype == "PLANNER_RESPONSE":
                msg_bytes += len((s.get("content") or "").encode("utf-8", errors="ignore"))
                thinking_bytes += len((s.get("thinking") or "").encode("utf-8", errors="ignore"))
                if s.get("tool_calls"):
                    try:
                        tool_bytes += len(json.dumps(s.get("tool_calls"), ensure_ascii=False).encode("utf-8"))
                    except Exception:
                        pass
                step_idx = s.get("step_index", i)
                t_start = parse_iso_ts(s.get("created_at"))
                t_end = None

                # Check hook recorded end time first
                if str(step_idx) in step_end_map:
                    t_end = float(step_end_map[str(step_idx)])

                # Next step timestamp in transcript.jsonl is when tool started (or next user input arrived)
                if t_end is None and i + 1 < n_steps:
                    next_s = steps[i + 1]
                    if next_s.get("type") == "GENERIC":
                        m_cr = TOOL_CREATED_RE.search(next_s.get("content", "")[:200])
                        if m_cr:
                            t_end = parse_iso_ts(m_cr.group(1))
                    if t_end is None:
                        t_end = parse_iso_ts(next_s.get("created_at"))

                # If this is the very last step in transcript.jsonl, file mtime is when it was flushed
                if t_end is None and i == n_steps - 1:
                    t_end = mtime

                dur_s = None
                if t_start is not None and t_end is not None:
                    dur_s = max(0.5, round(t_end - t_start, 2))
                    # Cap idle gap if next step was a user input hours later and no tool calls were made
                    if (
                        i + 1 < n_steps
                        and steps[i + 1].get("type") == "USER_INPUT"
                        and not s.get("tool_calls")
                        and dur_s > 180
                    ):
                        dur_s = None

                in_tok = int(s.get("input_tokens", 0) or 0)
                cache_tok = int(s.get("cache_read_tokens", 0) or 0)
                out_tok = int(s.get("output_tokens", 0) or 0)
                ctx_tok = in_tok + cache_tok
                tps = round(out_tok / dur_s, 2) if (dur_s and dur_s > 0 and out_tok > 0) else None
                cache_hit_rate = round((cache_tok / ctx_tok) * 100.0, 1) if ctx_tok > 0 else 0.0

                is_cache_break = False
                if (
                    prev_cache_hit is not None
                    and prev_cache_hit >= 60.0
                    and cache_hit_rate < prev_cache_hit - 35.0
                    and in_tok >= 20000
                ):
                    is_cache_break = True
                    cache_breaks.append(
                        {
                            "step_index": step_idx,
                            "prev_hit_rate": prev_cache_hit,
                            "new_hit_rate": cache_hit_rate,
                            "input_tokens": in_tok,
                        }
                    )
                if ctx_tok > 0:
                    prev_cache_hit = cache_hit_rate

                tcalls = s.get("tool_calls", []) or []
                tcall_names = []
                for tc in tcalls:
                    tname = tc.get("name", "unknown")
                    tcall_names.append(tname)
                    tool_counts[tname] = tool_counts.get(tname, 0) + 1
                    total_tool_calls += 1

                # Calculate tool execution window between i+1 (first GENERIC) and next PLANNER_RESPONSE/USER_INPUT
                step_tool_dur = 0.0
                if tcalls and i + 1 < n_steps:
                    j = i + 1
                    t_tool_start = None
                    t_tool_end = None
                    while j < n_steps and steps[j].get("type") == "GENERIC":
                        c_head = steps[j].get("content", "")[:240]
                        m1 = TOOL_CREATED_RE.search(c_head)
                        m2 = TOOL_COMPLETED_RE.search(c_head)
                        ts1 = parse_iso_ts(m1.group(1)) if m1 else parse_iso_ts(steps[j].get("created_at"))
                        ts2 = parse_iso_ts(m2.group(1)) if m2 else None
                        if ts1 and (t_tool_start is None or ts1 < t_tool_start):
                            t_tool_start = ts1
                        if ts2 and (t_tool_end is None or ts2 > t_tool_end):
                            t_tool_end = ts2
                        j += 1
                    if t_tool_end is None and j < n_steps:
                        t_tool_end = parse_iso_ts(steps[j].get("created_at"))
                    if t_tool_start and t_tool_end and t_tool_end >= t_tool_start:
                        step_tool_dur = round(min(600.0, t_tool_end - t_tool_start), 2)
                        total_tool_duration_s += step_tool_dur
                        if i >= last_turn_start_pos:
                            turn_tool_duration_s += step_tool_dur

                planner_steps.append(
                    {
                        "pos": i,
                        "in_current_turn": i >= last_turn_start_pos,
                        "step_index": step_idx,
                        "created_at": s.get("created_at"),
                        "in_tok": in_tok,
                        "cache_tok": cache_tok,
                        "out_tok": out_tok,
                        "ctx_tok": ctx_tok,
                        "dur_s": dur_s,
                        "tool_dur_s": step_tool_dur,
                        "tps": tps,
                        "cache_hit_rate": cache_hit_rate,
                        "is_cache_break": is_cache_break,
                        "tool_calls": tcall_names,
                    }
                )

        last_step = steps[-1]
        base = {
            "conversation_id": conv_id,
            "title": conv_title,
            "current_turn_prompt": current_turn_prompt,
            "turn_count": max(1, len(user_turn_indices)),
            "turn_start_ts": turn_start_ts,
            "total_raw_steps": n_steps,
            "raw_model": raw_model,
            "planner_steps": planner_steps,
            "total_tool_calls": total_tool_calls,
            "tool_counts": tool_counts,
            "total_tool_duration_s": round(total_tool_duration_s, 1),
            "turn_tool_duration_s": round(turn_tool_duration_s, 1),
            "cache_breaks": cache_breaks,
            "first_user_bytes": first_user_bytes,
            "msg_bytes": msg_bytes,
            "tool_bytes": tool_bytes,
            "thinking_bytes": thinking_bytes,
            "last_step_type": last_step.get("type"),
            "last_step_index": last_step.get("step_index", n_steps - 1),
            "last_step_created_ts": parse_iso_ts(last_step.get("created_at")),
            "last_step_tool_calls": [
                tc.get("name", "tool") for tc in (last_step.get("tool_calls") or [])
            ],
            "mtime": mtime,
        }
        _TRANSCRIPT_CACHE[transcript_path] = (mtime, fsize, base)

    # 3. Overlay real-time state & compute dynamic aggregations
    now = time.time()
    hook_model = hook_conv_state.get("model_name")
    _, hook_effort, _ = split_model_and_effort(hook_model) if hook_model else (None, None, None)
    if hook_model and (not base["raw_model"] or hook_conv_state.get("updated_at", 0) > base["mtime"] + 0.5):
        effective_raw_model = hook_model
    else:
        effective_raw_model = base["raw_model"] or hook_model or "Auto"
    model_name, effort, full_model = split_model_and_effort(
        effective_raw_model,
        fallback_effort=hook_effort if hook_effort != "Default" else None,
    )
    context_limit = resolve_context_limit(full_model)

    psteps = base["planner_steps"]
    turn_psteps = [p for p in psteps if p["in_current_turn"]]

    tot_in = sum(p["in_tok"] for p in psteps)
    tot_cache = sum(p["cache_tok"] for p in psteps)
    tot_out = sum(p["out_tok"] for p in psteps)
    grand_tot = tot_in + tot_cache + tot_out

    valid_dur_steps = [p for p in psteps if p["dur_s"] is not None and p["dur_s"] > 0]
    tot_llm_dur = round(sum(p["dur_s"] for p in valid_dur_steps), 1)
    tot_valid_out = sum(p["out_tok"] for p in valid_dur_steps)
    avg_tps = round(tot_valid_out / tot_llm_dur, 1) if tot_llm_dur > 0 else 0.0

    tps_values = [p["tps"] for p in psteps if p["tps"] is not None]
    peak_tps = round(max(tps_values), 1) if tps_values else 0.0
    min_tps = round(min(tps_values), 1) if tps_values else 0.0

    # Current turn aggregations
    turn_in = sum(p["in_tok"] for p in turn_psteps)
    turn_cache = sum(p["cache_tok"] for p in turn_psteps)
    turn_out = sum(p["out_tok"] for p in turn_psteps)
    turn_tot = turn_in + turn_cache + turn_out
    turn_valid_dur = [p for p in turn_psteps if p["dur_s"] is not None and p["dur_s"] > 0]
    turn_llm_dur = round(sum(p["dur_s"] for p in turn_valid_dur), 1)
    turn_valid_out = sum(p["out_tok"] for p in turn_valid_dur)
    turn_avg_tps = round(turn_valid_out / turn_llm_dur, 1) if turn_llm_dur > 0 else avg_tps

    latest = psteps[-1] if psteps else None
    cur_ctx = latest["ctx_tok"] if latest else 0
    cur_ctx_with_out = (latest["ctx_tok"] + latest["out_tok"]) if latest else 0
    cur_cache_hit = latest["cache_hit_rate"] if latest else 0.0
    session_cache_hit = (
        round((tot_cache / (tot_in + tot_cache)) * 100.0, 1)
        if (tot_in + tot_cache) > 0
        else 0.0
    )
    latest_tps = latest["tps"] if (latest and latest["tps"] is not None) else avg_tps
    latest_out = latest["out_tok"] if latest else 0
    latest_dur = latest["dur_s"] if latest else 0.0
    latest_step_idx = latest["step_index"] if latest else 0

    ctx_used_pct = round((cur_ctx / context_limit) * 100.0, 2) if context_limit > 0 else 0.0

    # 4-segment context window breakdown (System/Rules, Messages, Tool Calls, Thinking/Other)
    first_ctx = psteps[0]["ctx_tok"] if psteps else 0
    est_first_user_tok = min(first_ctx // 3, max(20, base.get("first_user_bytes", 0) // 3))
    sys_tokens = min(cur_ctx, max(0, first_ctx - est_first_user_tok))
    rem_ctx = max(0, cur_ctx - sys_tokens)
    b_msg = max(1, base.get("msg_bytes", 1))
    b_tool = max(0, base.get("tool_bytes", 0))
    b_think = max(0, base.get("thinking_bytes", 0))
    b_tot = max(1, b_msg + b_tool + b_think)
    msg_tokens = int(round(rem_ctx * (b_msg / b_tot)))
    tool_tokens = int(round(rem_ctx * (b_tool / b_tot)))
    think_tokens = max(0, rem_ctx - msg_tokens - tool_tokens)
    ctx_breakdown = {
        "system_tokens": sys_tokens,
        "system_pct": round((sys_tokens / cur_ctx) * 100.0, 1) if cur_ctx > 0 else 0.0,
        "messages_tokens": msg_tokens,
        "messages_pct": round((msg_tokens / cur_ctx) * 100.0, 1) if cur_ctx > 0 else 0.0,
        "tools_tokens": tool_tokens,
        "tools_pct": round((tool_tokens / cur_ctx) * 100.0, 1) if cur_ctx > 0 else 0.0,
        "thinking_tokens": think_tokens,
        "thinking_pct": round((think_tokens / cur_ctx) * 100.0, 1) if cur_ctx > 0 else 0.0,
    }

    # Determine real-time execution status (THINKING / TOOL_RUNNING / IDLE)
    age_since_mtime = max(0.0, now - base["mtime"])
    hook_status = hook_conv_state.get("status")
    hook_updated_at = float(hook_conv_state.get("updated_at", 0) or 0)
    hook_fresh = (now - hook_updated_at) < 180.0

    live_status = "IDLE"
    live_status_label = "已完成 / 等待输入"
    live_elapsed_s = 0.0
    active_tools = []

    ltype = base["last_step_type"]
    ltcalls = base["last_step_tool_calls"]
    is_final_planner_step = (ltype == "PLANNER_RESPONSE" and not ltcalls)
    is_db_idle, db_mod_ts = get_db_conversation_run_state(conv_id)

    if is_db_idle is True:
        live_status = "IDLE"
    elif hook_fresh and hook_status == "IDLE" and hook_updated_at >= base["mtime"] - 0.5:
        live_status = "IDLE"
    elif is_final_planner_step and base["mtime"] >= hook_updated_at - 0.2:
        live_status = "IDLE"
    elif hook_fresh and hook_status == "THINKING" and hook_updated_at >= base["mtime"] - 1.0:
        live_status = "THINKING"
        live_elapsed_s = int(round(max(1.0, now - float(hook_conv_state.get("phase_started_at", hook_updated_at)))))
        live_status_label = f"正在思考推理中 · 已耗时 {live_elapsed_s}s"
    elif age_since_mtime < 120.0:
        if ltype in ("USER_INPUT", "GENERIC"):
            live_status = "THINKING"
            live_elapsed_s = int(round(max(1.0, age_since_mtime)))
            live_status_label = f"正在思考推理中 · 已耗时 {live_elapsed_s}s"
        elif ltype == "PLANNER_RESPONSE" and ltcalls:
            live_status = "TOOL_RUNNING"
            active_tools = ltcalls
            live_elapsed_s = int(round(max(1.0, age_since_mtime)))
            live_status_label = f"正在执行工具 ({', '.join(ltcalls[:2])}) · 已耗时 {live_elapsed_s}s"

    realtime_tps = latest_tps if live_status != "IDLE" else 0

    t_start_turn = float(base.get("turn_start_ts") or base["mtime"])
    last_step_no = str(base.get("last_step_index", 0))
    t_end_turn = float(step_end_map.get(last_step_no) or base["mtime"])
    if db_mod_ts and db_mod_ts >= t_start_turn:
        t_end_turn = max(t_end_turn, float(db_mod_ts))
    if live_status != "IDLE":
        turn_duration_s = int(round(max(1.0, now - t_start_turn)))
    else:
        turn_duration_s = int(round(max(1.0, t_end_turn - t_start_turn)))

    tot_combined_time = tot_llm_dur + base["total_tool_duration_s"]
    llm_time_share_pct = (
        round((tot_llm_dur / tot_combined_time) * 100.0, 1)
        if tot_combined_time > 0
        else 100.0
    )

    top_tools = sorted(
        base["tool_counts"].items(), key=lambda kv: kv[1], reverse=True
    )[:6]

    return {
        "conversation_id": conv_id,
        "short_id": conv_id[:8],
        "title": base["title"],
        "current_turn_prompt": base["current_turn_prompt"],
        "turn_count": base["turn_count"],
        "model": {
            "name": model_name,
            "effort": effort,
            "full_name": full_model,
        },
        "live": {
            "status": live_status,
            "label": live_status_label,
            "elapsed_s": live_elapsed_s,
            "active_tools": active_tools,
            "last_updated_ago_s": round(age_since_mtime, 1),
            "mtime": base["mtime"],
        },
        "tps": {
            "latest": realtime_tps,
            "last_step": latest_tps,
            "latest_step_index": latest_step_idx,
            "latest_out_tokens": latest_out,
            "latest_dur_s": latest_dur,
            "turn_avg": turn_avg_tps,
            "session_avg": avg_tps,
            "peak": peak_tps,
            "min": min_tps,
        },
        "context": {
            "current_tokens": cur_ctx,
            "current_with_output_tokens": cur_ctx_with_out,
            "limit_tokens": context_limit,
            "used_pct": ctx_used_pct,
            "warn_threshold_tokens": 200000,
            "exceeds_200k": cur_ctx >= 200000,
            "remaining_to_200k": max(0, 200000 - cur_ctx),
            "remaining_to_limit": max(0, context_limit - cur_ctx),
            "breakdown": ctx_breakdown,
        },
        "tokens": {
            "turn_input": turn_in,
            "turn_cache_read": turn_cache,
            "turn_output": turn_out,
            "turn_total": turn_tot,
            "session_input": tot_in,
            "session_cache_read": tot_cache,
            "session_output": tot_out,
            "session_grand_total": grand_tot,
        },
        "cache": {
            "current_hit_rate_pct": cur_cache_hit,
            "session_hit_rate_pct": session_cache_hit,
            "breaks_count": len(base["cache_breaks"]),
            "breaks": base["cache_breaks"][-5:],
        },
        "timing": {
            "turn_duration_s": turn_duration_s,
            "session_llm_s": int(round(tot_llm_dur)),
            "session_tool_s": int(round(base["total_tool_duration_s"])),
            "turn_llm_s": int(round(turn_llm_dur)),
            "turn_tool_s": int(round(base["turn_tool_duration_s"])),
            "llm_share_pct": llm_time_share_pct,
        },
        "steps": {
            "total_planner_steps": len(psteps),
            "turn_planner_steps": len(turn_psteps),
            "total_raw_steps": base["total_raw_steps"],
            "total_tool_calls": base["total_tool_calls"],
            "top_tools": [{"name": k, "count": v} for k, v in top_tools],
            "history": psteps[-40:],
        },
    }


def get_all_conversations_overview(limit_sessions=25):
    """
    Scan brain directory for recent conversations and compute cross-session daily & 7-day totals.
    """
    live_state = load_live_state()
    conv_states = live_state.get("conversations", {})
    pattern = os.path.join(BRAIN_DIR, "*", ".system_generated", "logs", "transcript.jsonl")
    files = glob.glob(pattern)
    files.sort(key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0, reverse=True)

    now = time.time()
    local_now = datetime.now().astimezone()
    start_of_today = local_now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    start_of_7d = (local_now - timedelta(days=7)).timestamp()

    sessions = []
    today_stats = {
        "sessions_count": 0,
        "input_tokens": 0,
        "cache_read_tokens": 0,
        "output_tokens": 0,
        "grand_total_tokens": 0,
        "planner_steps": 0,
    }
    week_stats = {
        "sessions_count": 0,
        "input_tokens": 0,
        "cache_read_tokens": 0,
        "output_tokens": 0,
        "grand_total_tokens": 0,
        "planner_steps": 0,
    }

    for idx, fpath in enumerate(files):
        try:
            mtime = os.path.getmtime(fpath)
        except OSError:
            continue
        if mtime < start_of_7d and idx >= limit_sessions:
            continue

        cid = os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(fpath))))
        parsed = parse_conversation_transcript(fpath, conv_states.get(cid))
        if not parsed:
            continue

        if mtime >= start_of_today:
            today_stats["sessions_count"] += 1
            today_stats["input_tokens"] += parsed["tokens"]["session_input"]
            today_stats["cache_read_tokens"] += parsed["tokens"]["session_cache_read"]
            today_stats["output_tokens"] += parsed["tokens"]["session_output"]
            today_stats["grand_total_tokens"] += parsed["tokens"]["session_grand_total"]
            today_stats["planner_steps"] += parsed["steps"]["total_planner_steps"]

        if mtime >= start_of_7d:
            week_stats["sessions_count"] += 1
            week_stats["input_tokens"] += parsed["tokens"]["session_input"]
            week_stats["cache_read_tokens"] += parsed["tokens"]["session_cache_read"]
            week_stats["output_tokens"] += parsed["tokens"]["session_output"]
            week_stats["grand_total_tokens"] += parsed["tokens"]["session_grand_total"]
            week_stats["planner_steps"] += parsed["steps"]["total_planner_steps"]

        if len(sessions) < limit_sessions:
            sessions.append(parsed)

    # Determine primary active conversation: always follow the UI-displayed conversation first
    # so background running conversations never hijack the HUD or its Overview alignment.
    displayed_cid = get_displayed_conversation_id()
    active_cid = displayed_cid or live_state.get("active_conversation_id")
    active_session = None
    if active_cid:
        active_session = next((s for s in sessions if s["conversation_id"] == active_cid), None)
        if not active_session:
            tpath = os.path.join(BRAIN_DIR, active_cid, ".system_generated", "logs", "transcript.jsonl")
            if os.path.exists(tpath):
                active_session = parse_conversation_transcript(tpath, conv_states.get(active_cid))
                if active_session:
                    sessions.insert(0, active_session)
    if not active_session and sessions:
        active_session = sessions[0]

    return {
        "timestamp": now,
        "active_conversation_id": active_session["conversation_id"] if active_session else None,
        "active": active_session,
        "sessions": [
            {
                "conversation_id": s["conversation_id"],
                "short_id": s["short_id"],
                "title": s["title"],
                "model_full": s["model"]["full_name"],
                "status": s["live"]["status"],
                "status_label": s["live"]["label"],
                "last_updated_ago_s": s["live"]["last_updated_ago_s"],
                "context_tokens": s["context"]["current_tokens"],
                "context_used_pct": s["context"]["used_pct"],
                "latest_tps": s["tps"]["latest"],
                "session_avg_tps": s["tps"]["session_avg"],
                "grand_total_tokens": s["tokens"]["session_grand_total"],
                "planner_steps": s["steps"]["total_planner_steps"],
            }
            for s in sessions
        ],
        "today": today_stats,
        "last_7_days": week_stats,
    }


if __name__ == "__main__":
    import sys
    cid = sys.argv[1] if len(sys.argv) > 1 else None
    overview = get_all_conversations_overview()
    if cid:
        tpath = os.path.join(BRAIN_DIR, cid, ".system_generated", "logs", "transcript.jsonl")
        res = parse_conversation_transcript(tpath)
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(overview, ensure_ascii=False, indent=2))
