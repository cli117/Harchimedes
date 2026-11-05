#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lifecycle Hook Handler for Antigravity Telemetry HUD.
Invoked on SessionStart, PreInvocation, PostInvocation, PostToolUse, and Stop.
Always outputs {} to stdout and exits 0 within <100ms so it never blocks the agent loop.
"""

import json
import os
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)


def handle_hook():
    event_name = sys.argv[1] if len(sys.argv) > 1 else "Unknown"
    raw_in = sys.stdin.read()
    if not raw_in.strip():
        return

    payload = json.loads(raw_in)
    conv_id = payload.get("conversationId")
    if not conv_id:
        return

    from telemetry_core import (
        get_displayed_conversation_id,
        load_live_state,
        save_live_state,
    )
    from render_hud_artifact import write_hud_artifact

    now = time.time()
    state = load_live_state()
    convs = state.setdefault("conversations", {})
    cstate = convs.setdefault(conv_id, {"step_end_times": {}})

    model_name = payload.get("modelName")
    if model_name and model_name not in ("auto", "none", ""):
        cstate["model_name"] = model_name

    transcript_path = payload.get("transcriptPath")
    if transcript_path:
        cstate["transcript_path"] = transcript_path

    artifact_dir = payload.get("artifactDirectoryPath")
    if artifact_dir:
        cstate["artifact_dir"] = artifact_dir

    if event_name == "PreInvocation":
        cstate["status"] = "THINKING"
        cstate["phase_started_at"] = now
    elif event_name == "PostToolUse":
        cstate["status"] = "THINKING"
        cstate["phase_started_at"] = now
        step_idx = payload.get("stepIdx")
        if step_idx is not None:
            cstate.setdefault("step_end_times", {})[str(step_idx)] = now
    elif event_name == "PostInvocation":
        cstate["status"] = "TOOL_RUNNING"
        cstate["phase_started_at"] = now
    elif event_name == "Stop":
        cstate["status"] = "IDLE"
        cstate["termination_reason"] = payload.get("terminationReason", "")
    elif event_name == "SessionStart":
        cstate["status"] = "IDLE"
        cstate["phase_started_at"] = now

    cstate["updated_at"] = now
    cstate["last_event"] = event_name
    displayed_cid = get_displayed_conversation_id()
    if displayed_cid:
        state["active_conversation_id"] = displayed_cid
    elif not state.get("active_conversation_id"):
        state["active_conversation_id"] = conv_id
    save_live_state(state)

    # Refresh existing HUD HTML artifacts in the conversation's artifact directory
    if artifact_dir and os.path.isdir(artifact_dir):
        existing = [
            f
            for f in ("telemetry_hud.html", "telemetry_hud_preview.html")
            if os.path.exists(os.path.join(artifact_dir, f))
        ]
        if existing:
            write_hud_artifact(conv_id, artifact_dir, filenames=tuple(existing))


if __name__ == "__main__":
    try:
        handle_hook()
    except Exception:
        pass
    sys.stdout.write("{}\n")
    sys.stdout.flush()
