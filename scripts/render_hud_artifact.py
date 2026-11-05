#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Generates or updates the interactive HTML Telemetry HUD artifact for a conversation.
Works both inline (<agent-embed>) and in the right-hand Artifacts / Auxiliary pane.
"""

import json
import os
import sys
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from telemetry_core import (
    BRAIN_DIR,
    load_live_state,
    parse_conversation_transcript,
    get_all_conversations_overview,
)


def fmt_k(n):
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def build_hud_html(data, overview=None):
    payload_json = json.dumps(
        {"session": data, "overview": overview}, ensure_ascii=False
    )
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Antigravity Telemetry HUD</title>
  <script src="https://www.gstatic.com/antigravity/web/dev/tailwindcss.min.js"></script>
</head>
<body class="bg-transparent text-[var(--foreground)] antialiased p-2.5 select-none">
  <div id="hud-root" class="bg-[var(--card)] text-[var(--foreground)] border border-[var(--border)] rounded-xl p-3.5 shadow-sm space-y-2.5">
    <!-- Top Header -->
    <div class="flex flex-wrap items-center justify-between gap-2 border-b border-[var(--border)] pb-2">
      <div class="flex items-center gap-2 flex-wrap">
        <span id="status-dot" class="inline-flex h-2.5 w-2.5 rounded-full bg-emerald-500"></span>
        <span id="model-name" class="font-semibold text-sm">—</span>
        <span id="model-effort" class="px-2 py-0.5 text-xs font-medium rounded-md bg-[var(--secondary)] text-[var(--secondary-foreground)] border border-[var(--border)]">
          Effort: —
        </span>
        <span id="status-badge" class="px-2 py-0.5 text-xs font-mono rounded-md bg-emerald-500/15 text-emerald-600 dark:text-emerald-400">
          —
        </span>
      </div>
      <div class="flex items-center gap-2 text-xs">
        <span id="cache-break-pill" class="hidden px-2 py-0.5 rounded bg-amber-500/15 text-amber-600 dark:text-amber-400 font-medium"></span>
        <span id="conv-meta" class="text-xs text-[var(--muted-foreground)] font-mono">—</span>
      </div>
    </div>

    <!-- 4 Core Cards -->
    <div class="grid grid-cols-2 sm:grid-cols-4 gap-2">
      <!-- Card 1: Real-time & Avg TPS -->
      <div class="p-2.5 rounded-lg bg-[var(--background)] border border-[var(--border)]">
        <div class="flex items-center justify-between text-xs text-[var(--muted-foreground)]">
          <span>实时 TPS / 会话平均</span>
          <span id="latest-step-tag" class="font-mono">Step —</span>
        </div>
        <div class="mt-1 flex items-baseline gap-1.5">
          <span id="tps-latest" class="text-lg font-bold font-mono text-emerald-600 dark:text-emerald-400">—</span>
          <span id="tps-avg" class="text-xs text-[var(--muted-foreground)] font-mono">/ — tok/s</span>
        </div>
        <div id="tps-sub" class="mt-0.5 text-xs text-[var(--muted-foreground)] font-mono truncate">
          本轮均值 — · 峰值 —
        </div>
      </div>

      <!-- Card 2: Context Window & Limit -->
      <div class="p-2.5 rounded-lg bg-[var(--background)] border border-[var(--border)]">
        <div class="flex items-center justify-between text-xs text-[var(--muted-foreground)]">
          <span>当前上下文 / 上限</span>
          <span id="ctx-pct" class="font-mono">—%</span>
        </div>
        <div class="mt-1 flex items-baseline gap-1">
          <span id="ctx-cur" class="text-lg font-bold font-mono">—</span>
          <span id="ctx-limit" class="text-xs text-[var(--muted-foreground)] font-mono">/ —</span>
        </div>
        <div class="mt-1.5 relative w-full h-1.5 bg-[var(--border)] rounded-full overflow-hidden">
          <div id="ctx-bar" class="h-full bg-blue-500 rounded-full transition-all" style="width: 0%"></div>
        </div>
        <div id="ctx-warn" class="mt-0.5 text-xs text-[var(--muted-foreground)] font-mono truncate">
          距 200k 警戒线: —
        </div>
      </div>

      <!-- Card 3: Current Turn vs Session Tokens -->
      <div class="p-2.5 rounded-lg bg-[var(--background)] border border-[var(--border)]">
        <div class="flex items-center justify-between text-xs text-[var(--muted-foreground)]">
          <span>会话累计总 Token</span>
          <span id="turn-badge" class="font-mono">第 — 轮</span>
        </div>
        <div class="mt-1 flex items-baseline gap-1">
          <span id="tok-grand" class="text-lg font-bold font-mono">—</span>
          <span id="tok-turn" class="text-xs text-[var(--muted-foreground)] font-mono">(本轮 —)</span>
        </div>
        <div id="tok-breakdown" class="mt-0.5 text-xs text-[var(--muted-foreground)] font-mono truncate">
          In — · Cache — · Out —
        </div>
      </div>

      <!-- Card 4: Cache Hit Rate & Time Split -->
      <div class="p-2.5 rounded-lg bg-[var(--background)] border border-[var(--border)]">
        <div class="flex items-center justify-between text-xs text-[var(--muted-foreground)]">
          <span>Prompt 缓存命中率</span>
          <span id="time-split" class="font-mono">推理占比 —%</span>
        </div>
        <div class="mt-1 flex items-baseline gap-1.5">
          <span id="cache-cur" class="text-lg font-bold font-mono text-sky-600 dark:text-sky-400">—%</span>
          <span id="cache-avg" class="text-xs text-[var(--muted-foreground)] font-mono">(累计 —%)</span>
        </div>
        <div id="timing-sub" class="mt-0.5 text-xs text-[var(--muted-foreground)] font-mono truncate">
          思考 —s · 工具 —s (— 次)
        </div>
      </div>
    </div>

    <!-- Step Sparkline Bar Chart -->
    <div>
      <div class="flex items-center justify-between text-xs text-[var(--muted-foreground)] mb-1">
        <span id="chart-title">步级推理 TPS 轨迹（绿=正常，黄=Cache 击穿，蓝=本轮步骤）</span>
        <span id="today-summary" class="font-mono">—</span>
      </div>
      <div id="bars" class="h-12 pt-1.5 px-1.5 bg-[var(--background)] rounded-lg border border-[var(--border)] flex items-end gap-1"></div>
      <div id="tooltip" class="mt-1 text-xs font-mono text-[var(--muted-foreground)] truncate">
        👉 悬停柱状图查看单步 Token、耗时与缓存命中率
      </div>
    </div>
  </div>

  <script>
    let STATE = {payload_json};

    function fmtTok(n) {{
      n = Number(n || 0);
      if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M';
      if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
      return String(n);
    }}

    function render(payload) {{
      const s = payload.session;
      if (!s) return;
      const ov = payload.overview;

      document.getElementById('model-name').textContent = s.model.name;
      document.getElementById('model-effort').textContent = 'Effort: ' + s.model.effort;
      document.getElementById('conv-meta').textContent = `${{s.short_id}} · ${{s.steps.total_planner_steps}} 步`;

      const dot = document.getElementById('status-dot');
      const badge = document.getElementById('status-badge');
      badge.textContent = s.live.label;
      if (s.live.status === 'THINKING') {{
        dot.className = 'inline-flex h-2.5 w-2.5 rounded-full bg-emerald-500 animate-ping';
        badge.className = 'px-2 py-0.5 text-xs font-mono rounded-md bg-emerald-500/15 text-emerald-600 dark:text-emerald-400';
      }} else if (s.live.status === 'TOOL_RUNNING') {{
        dot.className = 'inline-flex h-2.5 w-2.5 rounded-full bg-sky-500 animate-pulse';
        badge.className = 'px-2 py-0.5 text-xs font-mono rounded-md bg-sky-500/15 text-sky-600 dark:text-sky-400';
      }} else {{
        dot.className = 'inline-flex h-2.5 w-2.5 rounded-full bg-slate-400';
        badge.className = 'px-2 py-0.5 text-xs font-mono rounded-md bg-[var(--secondary)] text-[var(--muted-foreground)]';
      }}

      const cbPill = document.getElementById('cache-break-pill');
      if (s.cache.breaks_count > 0) {{
        const lastCb = s.cache.breaks[s.cache.breaks.length - 1];
        cbPill.textContent = `⚡ Step #${{lastCb.step_index}} Cache Break (${{lastCb.prev_hit_rate}}%→${{lastCb.new_hit_rate}}%)`;
        cbPill.classList.remove('hidden');
      }} else {{
        cbPill.classList.add('hidden');
      }}

      document.getElementById('latest-step-tag').textContent = `Step #${{s.tps.latest_step_index}}`;
      document.getElementById('tps-latest').textContent = s.tps.latest;
      document.getElementById('tps-avg').textContent = `/ ${{s.tps.session_avg}} tok/s`;
      document.getElementById('tps-sub').textContent = `本轮 ${{s.tps.turn_avg}} · 峰值 ${{s.tps.peak}} · 谷值 ${{s.tps.min}}`;

      document.getElementById('ctx-pct').textContent = `${{s.context.used_pct}}%`;
      document.getElementById('ctx-cur').textContent = fmtTok(s.context.current_tokens);
      document.getElementById('ctx-limit').textContent = `/ ${{fmtTok(s.context.limit_tokens)}}`;
      const bar = document.getElementById('ctx-bar');
      bar.style.width = Math.min(100, Math.max(2, s.context.used_pct)) + '%';
      bar.className = 'h-full rounded-full transition-all ' + (s.context.exceeds_200k ? 'bg-amber-500' : 'bg-blue-500');
      document.getElementById('ctx-warn').textContent = s.context.exceeds_200k
        ? `⚠️ 已超 200k 警戒线 (建议 /compact)`
        : `距 200k 警戒线还剩 ${{fmtTok(s.context.remaining_to_200k)}}`;

      document.getElementById('turn-badge').textContent = `第 ${{s.turn_count}} 轮 (${{s.steps.turn_planner_steps}}步)`;
      document.getElementById('tok-grand').textContent = fmtTok(s.tokens.session_grand_total);
      document.getElementById('tok-turn').textContent = `(本轮 ${{fmtTok(s.tokens.turn_total)}})`;
      document.getElementById('tok-breakdown').textContent =
        `In ${{fmtTok(s.tokens.session_input)}} · Cache ${{fmtTok(s.tokens.session_cache_read)}} · Out ${{fmtTok(s.tokens.session_output)}}`;

      document.getElementById('time-split').textContent = `思考占比 ${{s.timing.llm_share_pct}}%`;
      document.getElementById('cache-cur').textContent = `${{s.cache.current_hit_rate_pct}}%`;
      document.getElementById('cache-avg').textContent = `(累计 ${{s.cache.session_hit_rate_pct}}%)`;
      document.getElementById('timing-sub').textContent =
        `思考 ${{s.timing.session_llm_s}}s · 工具 ${{s.timing.session_tool_s}}s (${{s.steps.total_tool_calls}}次)`;

      if (ov && ov.today) {{
        document.getElementById('today-summary').textContent =
          `今日全量(${{ov.today.sessions_count}}会话): ${{fmtTok(ov.today.grand_total_tokens)}} tok`;
      }}

      const barsEl = document.getElementById('bars');
      const tipEl = document.getElementById('tooltip');
      barsEl.innerHTML = '';
      const hist = s.steps.history || [];
      const maxTps = Math.max(60, ...hist.map(h => h.tps || 0));
      hist.forEach(h => {{
        const pct = Math.max(14, Math.round(((h.tps || 10) / maxTps) * 100));
        const b = document.createElement('div');
        b.style.height = pct + '%';
        b.style.flex = '1 1 0%';
        const color = h.is_cache_break
          ? 'bg-amber-500 hover:bg-amber-400'
          : (h.in_current_turn ? 'bg-emerald-500 hover:bg-emerald-400' : 'bg-slate-400/60 hover:bg-slate-400');
        b.className = 'rounded-t transition-all cursor-pointer ' + color;
        b.onmouseenter = () => {{
          tipEl.textContent = `Step #${{h.step_index}} | TPS: ${{h.tps ?? '—'}} tok/s (Out: ${{h.out_tok}} tok, 思考: ${{h.dur_s ?? '—'}}s) | 上下文: ${{fmtTok(h.ctx_tok)}} | 缓存命中: ${{h.cache_hit_rate}}%` + (h.is_cache_break ? ' ⚡Cache Break' : '');
        }};
        barsEl.appendChild(b);
      }});
    }}

    render(STATE);

    // Optional live polling from local Sidecar server if reachable
    setInterval(async () => {{
      try {{
        const cid = STATE.session ? STATE.session.conversation_id : '';
        const res = await fetch(`http://127.0.0.1:18765/api/state?cid=${{encodeURIComponent(cid)}}`, {{ mode: 'cors' }});
        if (res.ok) {{
          const latest = await res.json();
          if (latest && latest.active) {{
            STATE = {{ session: latest.active, overview: latest }};
            render(STATE);
          }}
        }}
      }} catch (_) {{}}
    }}, 1500);
  </script>
</body>
</html>
"""


def write_hud_artifact(conversation_id, artifact_dir=None, filenames=("telemetry_hud.html",)):
    if not conversation_id:
        return None
    if not artifact_dir:
        artifact_dir = os.path.join(BRAIN_DIR, conversation_id)
    transcript_path = os.path.join(
        BRAIN_DIR, conversation_id, ".system_generated", "logs", "transcript.jsonl"
    )
    if not os.path.exists(transcript_path):
        return None

    live_state = load_live_state()
    conv_state = live_state.get("conversations", {}).get(conversation_id, {})
    parsed = parse_conversation_transcript(transcript_path, conv_state)
    if not parsed:
        return None
    overview = get_all_conversations_overview(limit_sessions=10)
    html = build_hud_html(parsed, overview)

    os.makedirs(artifact_dir, exist_ok=True)
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    written = []
    for fname in filenames:
        fpath = os.path.join(artifact_dir, fname)
        tmp_path = fpath + f".tmp.{os.getpid()}"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(html)
        os.replace(tmp_path, fpath)

        meta_path = fpath + ".metadata.json"
        meta = {
            "summary": (
                f"Telemetry HUD [{parsed['model']['full_name']}] · "
                f"实时 TPS {parsed['tps']['latest']} / 平均 {parsed['tps']['session_avg']} tok/s · "
                f"上下文 {fmt_k(parsed['context']['current_tokens'])}/{fmt_k(parsed['context']['limit_tokens'])} ({parsed['context']['used_pct']}%) · "
                f"累计吞吐 {fmt_k(parsed['tokens']['session_grand_total'])} tokens"
            ),
            "updatedAt": now_iso,
            "userFacing": True,
        }
        tmp_meta = meta_path + f".tmp.{os.getpid()}"
        with open(tmp_meta, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        os.replace(tmp_meta, meta_path)
        written.append(fpath)

    return written[0] if written else None


if __name__ == "__main__":
    cid = sys.argv[1] if len(sys.argv) > 1 else None
    if not cid:
        ov = get_all_conversations_overview(limit_sessions=1)
        cid = ov.get("active_conversation_id")
    if cid:
        out = write_hud_artifact(cid)
        print(out or "")
