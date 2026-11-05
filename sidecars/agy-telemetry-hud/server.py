#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Real-time Sidecar Server & Native macOS Overlay Manager for Antigravity Telemetry HUD.
Serves the Overview-aligned right-side Token Usage Inspector Card on 127.0.0.1:18765
and manages the native borderless macOS overlay window (hud_overlay_bin).
"""

import atexit
import glob
import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
CANDIDATE_SCRIPT_DIRS = [
    os.path.abspath(os.path.join(HERE, "..", "..", "scripts")),
    os.path.expanduser("~/.gemini/config/plugins/agy-telemetry-hud/scripts"),
    os.path.abspath(os.path.join(HERE, "scripts")),
]
for d in CANDIDATE_SCRIPT_DIRS:
    if os.path.isdir(d) and d not in sys.path:
        sys.path.insert(0, d)

from telemetry_core import (
    BRAIN_DIR,
    get_all_conversations_overview,
    load_live_state,
    parse_conversation_transcript,
    save_live_state,
)
from render_hud_artifact import write_hud_artifact

OVERLAY_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Antigravity Telemetry HUD</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    :root {
      --bg-card: rgba(255, 255, 255, 0.98);
      --bg-sub: #f3f4f6;
      --fg-main: #1f2937;
      --fg-muted: #6b7280;
      --border-col: #ebedf0;
      --blue-bg: #eef4ff;
      --blue-fg: #2563eb;
      --green-bg: #ecfdf5;
      --green-fg: #059669;
      --purple-bg: #f5f3ff;
      --purple-fg: #7c3aed;
      --shadow: 0 2px 12px rgba(15, 23, 42, 0.06), 0 1px 2px rgba(15, 23, 42, 0.04);
    }
    @media (prefers-color-scheme: dark) {
      :root {
        --bg-card: rgba(24, 28, 38, 0.97);
        --bg-sub: #1f2937;
        --fg-main: #f3f4f6;
        --fg-muted: #9ca3af;
        --border-col: rgba(55, 65, 81, 0.85);
        --blue-bg: rgba(37, 99, 235, 0.18);
        --blue-fg: #60a5fa;
        --green-bg: rgba(16, 185, 129, 0.18);
        --green-fg: #34d399;
        --purple-bg: rgba(124, 58, 237, 0.18);
        --purple-fg: #a78bfa;
        --shadow: 0 4px 18px rgba(0, 0, 0, 0.4);
      }
    }
    html, body {
      width: 100%;
      height: 100%;
      background: transparent;
      color: var(--fg-main);
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC", sans-serif;
      font-size: 11px;
      overflow: hidden;
      user-select: none;
      -webkit-user-select: none;
      padding: 3px;
    }
    .mono { font-family: "SF Mono", Menlo, Monaco, Consolas, monospace; }
    .hidden { display: none !important; }

    #card-view {
      width: 100%;
      height: 100%;
      background: var(--bg-card);
      border: 1px solid var(--border-col);
      border-radius: 12px;
      box-shadow: var(--shadow);
      padding: 10px 11px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      cursor: grab;
    }
    #card-view:active { cursor: grabbing; }

    .card-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding-bottom: 5px;
      border-bottom: 1px solid var(--border-col);
    }
    .card-title {
      font-size: 12px;
      font-weight: 600;
      color: var(--fg-muted);
      display: flex;
      align-items: center;
      gap: 5px;
    }
    .dot {
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: #9ca3af;
      flex-shrink: 0;
    }
    .dot.thinking {
      background: #10b981;
      box-shadow: 0 0 0 2.5px rgba(16, 185, 129, 0.25);
    }
    .dot.tool {
      background: #3b82f6;
      box-shadow: 0 0 0 2.5px rgba(59, 130, 246, 0.25);
    }
    .pill-badge {
      padding: 1px 5px;
      border-radius: 4px;
      background: var(--purple-bg);
      color: var(--purple-fg);
      font-weight: 600;
      font-size: 10px;
      flex-shrink: 0;
    }
    .meta-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 4px;
      font-size: 11px;
      padding: 2px 0;
    }
    .session-select {
      background: var(--bg-sub);
      color: var(--fg-main);
      border: 1px solid var(--border-col);
      border-radius: 5px;
      font-size: 10px;
      padding: 1px 4px;
      max-width: 118px;
      outline: none;
      cursor: pointer;
    }
    /* 3 Metric Boxes (Blue / Green / Purple) */
    .tri-grid {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 5px;
    }
    .metric-box {
      border-radius: 7px;
      padding: 5px 6px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      min-height: 44px;
    }
    .metric-box.blue { background: var(--blue-bg); }
    .metric-box.green { background: var(--green-bg); }
    .metric-box.purple { background: var(--purple-bg); }
    .metric-label { font-size: 10px; font-weight: 600; white-space: nowrap; }
    .metric-box.blue .metric-label { color: var(--blue-fg); }
    .metric-box.green .metric-label { color: var(--green-fg); }
    .metric-box.purple .metric-label { color: var(--purple-fg); }
    .metric-val {
      font-size: 12px;
      font-weight: 700;
      text-align: right;
      margin-top: 4px;
      white-space: nowrap;
    }

    /* Cumulative Section */
    .cum-section {
      padding: 5px 0;
      border-top: 1px solid var(--border-col);
      border-bottom: 1px solid var(--border-col);
      display: flex;
      flex-direction: column;
      gap: 5px;
    }
    .row-between {
      display: flex;
      align-items: center;
      justify-content: space-between;
      font-size: 10px;
    }
    .bar-row {
      display: flex;
      align-items: center;
      gap: 6px;
      font-size: 10px;
    }
    .bar-label {
      width: 28px;
      color: var(--fg-muted);
      flex-shrink: 0;
    }
    .bar-track {
      flex: 1;
      height: 6px;
      background: var(--bg-sub);
      border-radius: 99px;
      overflow: hidden;
      display: flex;
    }
    .bar-val {
      min-width: 40px;
      text-align: right;
      font-weight: 700;
      flex-shrink: 0;
      white-space: nowrap;
    }

    /* Segmented Tabs (上下文窗口 | Agent 调用) */
    .seg-tabs {
      background: var(--bg-sub);
      border-radius: 6px;
      padding: 2px;
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 3px;
    }
    .seg-btn {
      border: none;
      background: transparent;
      color: var(--fg-muted);
      font-size: 10px;
      font-weight: 600;
      padding: 3px 0;
      border-radius: 5px;
      cursor: pointer;
    }
    .seg-btn.active {
      background: var(--bg-card);
      color: var(--fg-main);
      box-shadow: 0 1px 2px rgba(0,0,0,0.06);
    }

    /* Context Window Section */
    .ctx-big-row {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      margin-top: 1px;
    }
    .ctx-cur-num {
      font-size: 16px;
      font-weight: 800;
      letter-spacing: -0.4px;
    }
    .ctx-lim-num {
      font-size: 10px;
      color: var(--fg-muted);
      margin-left: 1px;
    }
    .ctx-pct-num {
      font-size: 11px;
      font-weight: 700;
    }
    .legend-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 4px 6px;
      margin-top: 5px;
      font-size: 10px;
    }
    .legend-item {
      display: flex;
      align-items: center;
      justify-content: space-between;
      min-width: 0;
    }
    .legend-left {
      display: flex;
      align-items: center;
      gap: 3px;
      color: var(--fg-muted);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .ldot {
      width: 5px;
      height: 5px;
      border-radius: 50%;
      flex-shrink: 0;
    }
    .card-footer {
      padding-top: 4px;
      border-top: 1px solid var(--border-col);
      display: flex;
      align-items: center;
      justify-content: space-between;
      font-size: 10px;
      color: var(--fg-muted);
    }
    .link-btn {
      cursor: pointer;
      color: var(--fg-muted);
      border: none;
      background: transparent;
      font-size: 10px;
      padding: 1px 3px;
      border-radius: 4px;
    }
    .link-btn:hover { color: var(--fg-main); background: var(--bg-sub); }
  </style>
</head>
<body>
  <div id="card-view" title="按住面板任意空白处即可上下/左右拖动位置">
    <!-- Top Title Bar -->
    <div class="card-header">
      <div class="card-title">
        <span id="card-dot" class="dot"></span>
        <span>Token Usage</span>
        <span id="card-live-tag" class="mono" style="font-size:10px;font-weight:500;"></span>
      </div>
      <button id="btn-reset-pos" class="link-btn" title="恢复到 Overview 正下方默认对齐位置">↺</button>
    </div>

    <!-- Model + Effort + Session Switcher -->
    <div class="meta-row">
      <div style="display:flex;align-items:center;gap:4px;min-width:0;overflow:hidden;">
        <span style="font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;" id="card-model">—</span>
        <span id="card-effort" class="pill-badge">—</span>
      </div>
      <select id="session-select" class="session-select" title="切换会话 (默认自动跟随当前执行任务)"></select>
    </div>

    <!-- 3 Colored Metric Boxes: 实时 TPS | 平均 TPS | 单步耗时 -->
    <div class="tri-grid">
      <div class="metric-box blue">
        <div class="metric-label">实时 TPS</div>
        <div id="m-tps-cur" class="metric-val mono">—</div>
      </div>
      <div class="metric-box green">
        <div class="metric-label">平均 TPS</div>
        <div id="m-tps-avg" class="metric-val mono">—</div>
      </div>
      <div class="metric-box purple">
        <div id="m-lat-label" class="metric-label">本轮耗时</div>
        <div id="m-lat-val" class="metric-val mono">—</div>
      </div>
    </div>

    <!-- 会话累计 & 缓存命中率 -->
    <div class="cum-section">
      <div class="row-between">
        <span style="color:var(--fg-muted);font-weight:600;">会话累计</span>
        <span style="color:var(--fg-muted);">缓存命中 <strong id="c-hit-rate" class="mono" style="color:var(--fg-main)">—%</strong></span>
      </div>
      <!-- Token Bar -->
      <div class="bar-row">
        <span class="bar-label">Token</span>
        <div class="bar-track" title="青色=缓存读取 · 橙色=新增输入 · 蓝色=输出">
          <div id="bar-tok-cache" style="background:#0d9488;width:80%"></div>
          <div id="bar-tok-in" style="background:#d97706;width:15%"></div>
          <div id="bar-tok-out" style="background:#3b82f6;width:5%"></div>
        </div>
        <span id="c-tok-tot" class="bar-val mono">—</span>
      </div>
      <!-- Duration Bar -->
      <div class="bar-row">
        <span class="bar-label">耗时</span>
        <div class="bar-track" title="青色=模型思考耗时 · 橙色=工具执行耗时">
          <div id="bar-dur-llm" style="background:#0d9488;width:75%"></div>
          <div id="bar-dur-tool" style="background:#d97706;width:25%"></div>
        </div>
        <span id="c-dur-tot" class="bar-val mono">—</span>
      </div>
    </div>

    <!-- Segmented Tabs: 上下文窗口 | Agent 调用 -->
    <div class="seg-tabs">
      <button id="tab-btn-ctx" class="seg-btn active">上下文窗口</button>
      <button id="tab-btn-agent" class="seg-btn">Agent 调用</button>
    </div>

    <!-- Tab 1: 上下文窗口 -->
    <div id="tab-pane-ctx">
      <div style="font-size:10px;font-weight:700;color:var(--fg-muted);">当前上下文</div>
      <div class="ctx-big-row">
        <div>
          <span id="ctx-cur" class="ctx-cur-num mono">—</span>
          <span id="ctx-lim" class="ctx-lim-num mono">/ —</span>
        </div>
        <span id="ctx-pct" class="ctx-pct-num mono">—%</span>
      </div>

      <div class="row-between" style="margin-top:4px;margin-bottom:3px;color:var(--fg-muted);">
        <span>窗口分布</span>
        <span id="ctx-warn-tag" class="mono">距200k —</span>
      </div>

      <!-- 4-color segmented bar -->
      <div class="bar-track" style="height:6px;">
        <div id="seg-sys" style="background:#2563eb;width:15%"></div>
        <div id="seg-msg" style="background:#10b981;width:20%"></div>
        <div id="seg-tool" style="background:#d97706;width:55%"></div>
        <div id="seg-think" style="background:#8b5cf6;width:10%"></div>
      </div>

      <div class="legend-grid">
        <div class="legend-item">
          <span class="legend-left"><span class="ldot" style="background:#2563eb"></span>系统提示</span>
          <strong id="leg-sys" class="mono">—</strong>
        </div>
        <div class="legend-item">
          <span class="legend-left"><span class="ldot" style="background:#10b981"></span>对话消息</span>
          <strong id="leg-msg" class="mono">—</strong>
        </div>
        <div class="legend-item">
          <span class="legend-left"><span class="ldot" style="background:#d97706"></span>工具调用</span>
          <strong id="leg-tool" class="mono">—</strong>
        </div>
        <div class="legend-item">
          <span class="legend-left"><span class="ldot" style="background:#8b5cf6"></span>思考/其它</span>
          <strong id="leg-think" class="mono">—</strong>
        </div>
      </div>
    </div>

    <!-- Tab 2: Agent 调用 -->
    <div id="tab-pane-agent" class="hidden" style="display:flex;flex-direction:column;gap:4px;">
      <div class="row-between">
        <span style="font-weight:700;" id="ag-turn-title">本轮对话 (#—)</span>
        <span class="mono" style="color:var(--fg-muted);" id="ag-turn-stats">—</span>
      </div>
      <div id="ag-tools-list" style="display:flex;flex-direction:column;gap:3px;margin-top:2px;"></div>
      <div id="ag-cache-alert" class="mono" style="font-size:10px;color:#d97706;margin-top:2px;"></div>
    </div>

    <!-- Footer: Today Total -->
    <div class="card-footer">
      <span id="footer-today" class="mono">今日累计: —</span>
      <span style="font-size:9px;opacity:0.75;">可按住拖拽</span>
    </div>
  </div>

  <script>
    let selectedCid = '';
    const cardView = document.getElementById('card-view');
    const selectEl = document.getElementById('session-select');

    function postBridge(payload) {
      try {
        if (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.hudBridge) {
          window.webkit.messageHandlers.hudBridge.postMessage(payload);
        }
      } catch (_) {}
    }

    document.getElementById('btn-reset-pos').addEventListener('click', (e) => {
      e.stopPropagation();
      postBridge({ action: 'resetPosition' });
    });

    // Tab switching
    const btnCtx = document.getElementById('tab-btn-ctx');
    const btnAgent = document.getElementById('tab-btn-agent');
    const paneCtx = document.getElementById('tab-pane-ctx');
    const paneAgent = document.getElementById('tab-pane-agent');

    btnCtx.addEventListener('click', (e) => {
      e.stopPropagation();
      btnCtx.classList.add('active');
      btnAgent.classList.remove('active');
      paneCtx.classList.remove('hidden');
      paneAgent.classList.add('hidden');
    });
    btnAgent.addEventListener('click', (e) => {
      e.stopPropagation();
      btnAgent.classList.add('active');
      btnCtx.classList.remove('active');
      paneAgent.classList.remove('hidden');
      paneCtx.classList.add('hidden');
    });

    selectEl.addEventListener('change', () => {
      selectedCid = selectEl.value;
      fetchState();
    });

    // Native 60fps Dragging: pressing anywhere on the card (except buttons/select) starts native macOS mouse tracking
    cardView.addEventListener('mousedown', (e) => {
      const tag = (e.target && e.target.tagName) ? e.target.tagName.toUpperCase() : '';
      if (tag === 'BUTTON' || tag === 'SELECT' || tag === 'OPTION') return;
      postBridge({ action: 'startDrag' });
    });

    function fmtTok(n) {
      n = Number(n || 0);
      if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
      if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
      return String(n);
    }

    function fmtDur(sec) {
      sec = Math.max(0, Math.round(Number(sec || 0)));
      const h = Math.floor(sec / 3600);
      const m = Math.floor((sec % 3600) / 60);
      const s = sec % 60;
      if (h > 0) return `${h}小时${m}分${s}秒`;
      if (m > 0) return `${m}分${s}秒`;
      return `${s}秒`;
    }

    let lastSyncedCid = '';
    window.onDisplayedCidChanged = function() {
      fetchState();
    };
    function render(data) {
      const sessions = data.sessions || [];
      const s = data.active;
      if (!s) return;

      // Update session selector
      const sig = `${s.conversation_id}#` + sessions.map(x => `${x.conversation_id}:${x.status}:${x.planner_steps}`).join('|');
      if (selectEl.dataset.sig !== sig) {
        selectEl.dataset.sig = sig;
        selectEl.innerHTML = '';
        const autoOpt = document.createElement('option');
        autoOpt.value = '';
        autoOpt.textContent = `自动 (${s.short_id.slice(0, 6)})`;
        selectEl.appendChild(autoOpt);
        sessions.forEach(item => {
          const opt = document.createElement('option');
          opt.value = item.conversation_id;
          const st = item.status === 'THINKING' ? '🟢' : (item.status === 'TOOL_RUNNING' ? '🔵' : '⚪');
          opt.textContent = `${st} ${item.short_id.slice(0, 6)} · ${item.title}`;
          selectEl.appendChild(opt);
        });
        selectEl.value = selectedCid;
      }

      const dotCls = s.live.status === 'THINKING' ? 'dot thinking' : (s.live.status === 'TOOL_RUNNING' ? 'dot tool' : 'dot');
      document.getElementById('card-dot').className = dotCls;

      // Shorten model name slightly if needed so it fits 236px cleanly
      const shortModel = (s.model.name || '').replace(/^Gemini\s+/i, '');
      document.getElementById('card-model').textContent = shortModel || s.model.name;
      document.getElementById('card-effort').textContent = s.model.effort;
      const liveTag = document.getElementById('card-live-tag');
      if (s.live.status === 'THINKING') {
        liveTag.textContent = `· 思考 ${fmtDur(s.live.elapsed_s)}`;
        liveTag.style.color = 'var(--green-fg)';
      } else if (s.live.status === 'TOOL_RUNNING') {
        liveTag.textContent = `· 工具 ${fmtDur(s.live.elapsed_s)}`;
        liveTag.style.color = 'var(--blue-fg)';
      } else {
        liveTag.textContent = `· 第${s.turn_count}轮`;
        liveTag.style.color = 'var(--fg-muted)';
      }

      document.getElementById('m-tps-cur').textContent = (s.live.status === 'IDLE') ? 0 : s.tps.latest;
      document.getElementById('m-tps-avg').textContent = s.tps.session_avg;
      document.getElementById('m-lat-label').textContent = '本轮耗时';
      const turnSec = Math.round(s.timing.turn_duration_s ?? 0);
      const latValEl = document.getElementById('m-lat-val');
      latValEl.textContent = fmtDur(turnSec);
      latValEl.style.fontSize = turnSec >= 3600 ? '10.5px' : '12px';

      // Cumulative section
      document.getElementById('c-hit-rate').textContent = `${s.cache.session_hit_rate_pct}%`;
      document.getElementById('c-tok-tot').textContent = fmtTok(s.tokens.session_grand_total);
      const totTok = Math.max(1, s.tokens.session_grand_total);
      document.getElementById('bar-tok-cache').style.width = `${(s.tokens.session_cache_read / totTok) * 100}%`;
      document.getElementById('bar-tok-in').style.width = `${(s.tokens.session_input / totTok) * 100}%`;
      document.getElementById('bar-tok-out').style.width = `${Math.max(2, (s.tokens.session_output / totTok) * 100)}%`;

      const totDur = Math.round(s.timing.session_llm_s + s.timing.session_tool_s);
      document.getElementById('c-dur-tot').textContent = fmtDur(totDur);
      document.getElementById('bar-dur-llm').style.width = `${s.timing.llm_share_pct}%`;
      document.getElementById('bar-dur-tool').style.width = `${Math.max(0, 100 - s.timing.llm_share_pct)}%`;

      // Context Window Tab
      document.getElementById('ctx-cur').textContent = fmtTok(s.context.current_tokens);
      document.getElementById('ctx-lim').textContent = `/ ${fmtTok(s.context.limit_tokens)}`;
      document.getElementById('ctx-pct').textContent = `${s.context.used_pct}%`;
      const warnEl = document.getElementById('ctx-warn-tag');
      if (s.context.exceeds_200k) {
        warnEl.textContent = `⚠ 超200k (+${fmtTok(s.context.current_tokens - 200000)})`;
        warnEl.style.color = '#d97706';
      } else {
        warnEl.textContent = `距200k剩 ${fmtTok(s.context.remaining_to_200k)}`;
        warnEl.style.color = 'var(--fg-muted)';
      }

      const bd = s.context.breakdown || {};
      document.getElementById('seg-sys').style.width = `${bd.system_pct || 15}%`;
      document.getElementById('seg-msg').style.width = `${bd.messages_pct || 20}%`;
      document.getElementById('seg-tool').style.width = `${bd.tools_pct || 55}%`;
      document.getElementById('seg-think').style.width = `${bd.thinking_pct || 10}%`;

      document.getElementById('leg-sys').textContent = fmtTok(bd.system_tokens);
      document.getElementById('leg-msg').textContent = fmtTok(bd.messages_tokens);
      document.getElementById('leg-tool').textContent = fmtTok(bd.tools_tokens);
      document.getElementById('leg-think').textContent = fmtTok(bd.thinking_tokens);

      // Agent Tab
      document.getElementById('ag-turn-title').textContent = `第${s.turn_count}轮 (${s.steps.turn_planner_steps}步)`;
      document.getElementById('ag-turn-stats').textContent = `均值 ${s.tps.turn_avg} tok/s`;
      const toolsEl = document.getElementById('ag-tools-list');
      toolsEl.innerHTML = '';
      const topTools = (s.steps.top_tools || []).slice(0, 4);
      const maxCnt = Math.max(1, ...topTools.map(t => t.count));
      topTools.forEach(t => {
        const row = document.createElement('div');
        row.className = 'bar-row';
        row.innerHTML = `
          <span class="mono" style="width:78px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--fg-muted)">${t.name}</span>
          <div class="bar-track" style="height:5px;"><div style="background:#3b82f6;width:${Math.round((t.count/maxCnt)*100)}%"></div></div>
          <span class="mono" style="width:24px;text-align:right;font-weight:700;">×${t.count}</span>
        `;
        toolsEl.appendChild(row);
      });
      const cbAlert = document.getElementById('ag-cache-alert');
      if (s.cache.breaks_count > 0) {
        const lastCb = s.cache.breaks[s.cache.breaks.length - 1];
        cbAlert.textContent = `⚡ Step #${lastCb.step_index} 缓存击穿 (${lastCb.prev_hit_rate}%→${lastCb.new_hit_rate}%)`;
      } else {
        cbAlert.textContent = `✅ 缓存连续命中，无击穿`;
        cbAlert.style.color = 'var(--green-fg)';
      }

      if (data.today) {
        document.getElementById('footer-today').textContent =
          `今日(${data.today.sessions_count}会话): ${fmtTok(data.today.grand_total_tokens)}`;
      }
    }

    async function fetchState() {
      try {
        const qs = selectedCid ? `?cid=${encodeURIComponent(selectedCid)}` : '';
        const res = await fetch(`/api/state${qs}`, { cache: 'no-store' });
        if (res.ok) {
          const data = await res.json();
          render(data);
        }
      } catch (_) {}
    }

    fetchState();
    setInterval(fetchState, 500);
  </script>
</body>
</html>
"""


class TelemetryHTTPHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def _send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")

    def do_OPTIONS(self):
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self):
        parsed_url = urlparse(self.path)
        path = parsed_url.path or "/"
        qs = parse_qs(parsed_url.query)

        if path.endswith("/healthz"):
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(body)
            return

        if path.endswith("/api/state"):
            overview = get_all_conversations_overview(limit_sessions=25)
            req_cid = (qs.get("cid") or [None])[0]
            if req_cid:
                tpath = os.path.join(
                    BRAIN_DIR, req_cid, ".system_generated", "logs", "transcript.jsonl"
                )
                if os.path.exists(tpath):
                    live_state = load_live_state()
                    cstate = live_state.get("conversations", {}).get(req_cid, {})
                    custom_active = parse_conversation_transcript(tpath, cstate)
                    if custom_active:
                        overview["active"] = custom_active
                        overview["active_conversation_id"] = req_cid

            body = json.dumps(overview, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(body)
            return

        body = OVERLAY_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(body)


def background_watcher_loop():
    last_mtimes = {}
    last_artifact_sync = {}

    while True:
        try:
            pattern = os.path.join(
                BRAIN_DIR, "*", ".system_generated", "logs", "transcript.jsonl"
            )
            files = glob.glob(pattern)
            files.sort(
                key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0,
                reverse=True,
            )
            now = time.time()
            for fpath in files[:3]:
                try:
                    mtime = os.path.getmtime(fpath)
                except OSError:
                    continue
                if now - mtime > 600:
                    continue

                prev_mtime = last_mtimes.get(fpath)
                if prev_mtime is not None and mtime == prev_mtime:
                    continue
                last_mtimes[fpath] = mtime

                cid = os.path.basename(
                    os.path.dirname(os.path.dirname(os.path.dirname(fpath)))
                )
                last_line = None
                with open(fpath, "rb") as f:
                    f.seek(0, os.SEEK_END)
                    fsize = f.tell()
                    seek_pos = max(0, fsize - 65536)
                    f.seek(seek_pos)
                    lines = f.read().decode("utf-8", errors="replace").splitlines()
                    for ln in reversed(lines):
                        if ln.strip():
                            last_line = ln.strip()
                            break

                if last_line:
                    try:
                        step_obj = json.loads(last_line)
                        if step_obj.get("type") == "PLANNER_RESPONSE":
                            sidx = str(step_obj.get("step_index", 0))
                            state = load_live_state()
                            cstate = state.setdefault("conversations", {}).setdefault(
                                cid, {"step_end_times": {}}
                            )
                            end_map = cstate.setdefault("step_end_times", {})
                            if sidx not in end_map:
                                end_map[sidx] = mtime
                                save_live_state(state)
                    except Exception:
                        pass

                art_dir = os.path.join(BRAIN_DIR, cid)
                if now - last_artifact_sync.get(cid, 0) >= 2.0:
                    existing = [
                        fn
                        for fn in ("telemetry_hud.html", "telemetry_hud_preview.html")
                        if os.path.exists(os.path.join(art_dir, fn))
                    ]
                    if existing:
                        write_hud_artifact(cid, art_dir, filenames=tuple(existing))
                        last_artifact_sync[cid] = now
        except Exception:
            pass
        time.sleep(0.5)


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


_OVERLAY_PROC = None


def cleanup_overlay():
    global _OVERLAY_PROC
    if _OVERLAY_PROC is not None:
        try:
            _OVERLAY_PROC.terminate()
        except Exception:
            pass
        _OVERLAY_PROC = None


def manage_macos_overlay(port):
    global _OVERLAY_PROC
    if sys.platform != "darwin":
        return
    bin_path = os.path.join(HERE, "hud_overlay_bin")
    swift_path = os.path.join(HERE, "hud_overlay.swift")
    if not os.path.exists(bin_path) and os.path.exists(swift_path):
        try:
            subprocess.run(
                ["/usr/bin/swiftc", swift_path, "-o", bin_path],
                check=True,
                timeout=30,
            )
        except Exception as e:
            print(f"[Telemetry HUD] Failed to compile hud_overlay.swift: {e}", flush=True)
            return

    if not os.path.exists(bin_path):
        return

    atexit.register(cleanup_overlay)

    while True:
        try:
            env = os.environ.copy()
            env["HUD_PORT"] = str(port)
            _OVERLAY_PROC = subprocess.Popen([bin_path], env=env)
            _OVERLAY_PROC.wait()
        except Exception:
            pass
        time.sleep(3.0)


def serve_on_port(port, quiet_if_in_use=False):
    try:
        server = ReusableThreadingHTTPServer(("127.0.0.1", port), TelemetryHTTPHandler)
    except OSError:
        if quiet_if_in_use:
            return
        raise
    print(f"[Telemetry HUD] Listening on http://127.0.0.1:{port}", flush=True)
    server.serve_forever()


def main():
    signal.signal(signal.SIGTERM, lambda *_: (cleanup_overlay(), sys.exit(0)))
    watcher = threading.Thread(target=background_watcher_loop, daemon=True)
    watcher.start()

    fixed_port = 18765
    overlay_thread = threading.Thread(
        target=manage_macos_overlay, args=(fixed_port,), daemon=True
    )
    overlay_thread.start()

    sidecar_port = int(os.environ.get("ANTIGRAVITY_SIDECAR_WEB_PORT", "0") or "0")
    if sidecar_port and sidecar_port != fixed_port:
        t = threading.Thread(
            target=serve_on_port, args=(fixed_port, True), daemon=True
        )
        t.start()
        serve_on_port(sidecar_port)
    else:
        serve_on_port(fixed_port)


if __name__ == "__main__":
    main()
