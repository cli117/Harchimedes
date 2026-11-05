# Harchimedes (`agy-telemetry-hud`)

哈基米德，你的哈基米伴侣 —— 为 **Antigravity 桌面客户端 (App)** 与 **终端 CLI (`agy`)** 提供实时的模型与思考强度、实时/平均 TPS、本轮耗时、上下文窗口水位与四维构成、会话累计 Token 吞吐及 Prompt 缓存命中率监控。

## ✨ 核心特性

1. **核心指标全覆盖（基于底层真实账单 `transcript.jsonl`、SQLite 状态库与生命周期 Hook）**：
   - **当前模型 & 思考强度**：自动识别当前会话模型与思考等级（如 `4 Argon High`、`3.8 Flash Medium`、`Claude Opus 5.5 Low`）。
   - **实时 TPS & 平均 TPS**：剔除工具执行耗时，实时展示模型推理 TPS 与会话平均 TPS；任务完成或手动点击停止（Stop）时实时 TPS 自动归零。
   - **本轮耗时**：统计当前对话轮次完整耗时（中途补充引导信息不重新计时），采用 `1s` 整数粒度并自动向上进位（`x秒` / `x分x秒` / `x小时x分x秒`）。
   - **当前上下文 & 四维构成拆解**：实时显示当前上下文 Token 水位、模型硬上限（如 `1.0M` / `200.0K`）、`200k` 警戒水位线，以及 **系统提示 / 对话消息 / 工具调用 / 思考/其它** 四段色彩占比。
   - **会话累计 Token & 耗时进度条**：展示缓存读取（`Cache`）、新增输入（`In`）、输出（`Out`）分段条与思考/工具耗时占比。
2. **与原生 `Overview` 像素级锁定与同步显隐**：
   - **自动锁定在原生 `Overview` 正下方**：通过窗口像素特征检测实时定位 `Overview` 卡片下边缘，无论右侧分屏开关、切换会话或 `Overview` 列表高度变化，始终紧贴在 `Overview` 正下方 `8pt`。
   - **同步自动隐藏**：在 `+ New Conversation` 空白页、窗口缩窄导致 `Overview` 隐藏、或点开大图预览（`media-modal` 遮罩）时自动隐藏，绝不遮挡内容。
   - **支持 60fps 原生按住拖拽与一键归位**：按住卡片任意空白处即可拖动并记忆相对 `Overview` 偏移；点击卡片右上角 `↺` 即可一键归位。
3. **随 Antigravity 自动启动 & 100% 兼容客户端升级**：
   - 基于 Antigravity 原生 Sidecar 守护机制（`restart_policy: always`），随客户端启动自动拉起、随客户端退出自动清理。
   - 纯 Python 3 标准库 + macOS 原生 Swift 贴窗组件（无需 `pip install` 任何第三方包），全量安装在 `~/.gemini/config/` 与 `~/.gemini/antigravity-cli/` 下，升级客户端绝不丢失。

---

## 🚀 一键安装

```bash
git clone https://github.com/cli117/Harchimedes.git
cd Harchimedes
bash ./install.sh
```

> 首次启动时，macOS 会提示授权「屏幕与系统音频录制」权限（用于检测 Antigravity 窗口内 `Overview` 卡片的实时位置与图片预览弹窗遮挡），授权后重启一次 Antigravity 即可。

如需卸载：

```bash
bash ./uninstall.sh
```
