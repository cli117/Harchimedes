---
name: hud
description: >-
  在当前对话中内嵌渲染或唤醒 Antigravity Telemetry HUD 实时监控看板（支持对话区右侧 Token Usage 面板模式与底部输入框旁胶囊条模式一键切换，展示当前模型、思考强度、实时/平均 TPS、当前上下文窗口水位与四维构成、会话累计 Token、Prompt 缓存命中率与击穿告警、耗时拆解及今日跨会话统计）。当用户输入 /hud，或询问当前对话的 Token 消耗、TPS 速度、上下文占用、使用模型或打开监控面板时使用。
---

# Antigravity Telemetry HUD (`/hud`)

在当前对话中确保窗内原生悬浮挂件（支持「红框同款 Token Usage 面板模式」与「输入框旁底部胶囊条模式」一键切换与自由拖拽）处于运行状态，并在对话流中生成内嵌 HUD 卡片。

## 执行步骤

1. **生成当前会话的实时 HUD HTML 构件**：
   使用 `run_command` 执行以下命令（传入当前会话的 `Conversation ID`）：
   ```bash
   python3 ~/.gemini/config/plugins/agy-telemetry-hud/scripts/render_hud_artifact.py "<conversation-id>"
   ```
   该脚本会解析当前会话的 `transcript.jsonl` 与实时 Hook 状态，在当前会话的 Artifact 目录下生成 `telemetry_hud.html`（及 `.metadata.json`）。

2. **在回复中内嵌卡片并说明窗内挂件操作**：
   在回复正文中输出：
   - 提示用户窗内挂件右上角可点击 `▁ 底部条` / `📊 面板` 在「右侧 Token Usage 面板模式」与「底部输入框旁胶囊条模式」之间一键切换，且按住任意空白处即可拖拽位置。
   - 对话框内嵌交互卡片：
     ```html
     <agent-embed src="file:///Users/<user>/.gemini/antigravity/brain/<conversation-id>/telemetry_hud.html"></agent-embed>
     ```
     *(请将路径替换为步骤 1 脚本实际输出的绝对路径)*
