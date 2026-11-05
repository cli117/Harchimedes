#!/usr/bin/env bash
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="${HOME}/.gemini/config"
PLUGIN_DIR="${CONFIG_DIR}/plugins/agy-telemetry-hud"
SIDECAR_DIR="${CONFIG_DIR}/sidecars/agy-telemetry-hud"
SKILL_LINK_DIR="${CONFIG_DIR}/skills"
CLI_DIR="${HOME}/.gemini/antigravity-cli"

echo "🚀 Installing Antigravity Telemetry HUD (Desktop Overlay + CLI)..."

# 0. Compile native macOS floating HUD overlay binary (if on macOS)
if [[ "$(uname -s)" == "Darwin" && -x "/usr/bin/swiftc" ]]; then
  echo "  🔨 Compiling native macOS HUD overlay window..."
  /usr/bin/swiftc "${SRC_DIR}/sidecars/agy-telemetry-hud/hud_overlay.swift" \
    -o "${SRC_DIR}/sidecars/agy-telemetry-hud/hud_overlay_bin"
  chmod +x "${SRC_DIR}/sidecars/agy-telemetry-hud/hud_overlay_bin"
fi

# 1. Copy plugin bundle into ~/.gemini/config/plugins/agy-telemetry-hud
mkdir -p "${PLUGIN_DIR}"
rm -rf "${PLUGIN_DIR}/sidecars"
cp -R "${SRC_DIR}/plugin.json" "${SRC_DIR}/hooks.json" "${SRC_DIR}/assets" "${SRC_DIR}/scripts" "${SRC_DIR}/skills" "${PLUGIN_DIR}/"
chmod +x "${PLUGIN_DIR}/scripts/"*.py

# 2. Install Sidecar into ~/.gemini/config/sidecars/agy-telemetry-hud
mkdir -p "${SIDECAR_DIR}"
pkill -f "hud_overlay_bin" 2>/dev/null || true
cp -R "${SRC_DIR}/sidecars/agy-telemetry-hud/"* "${SIDECAR_DIR}/"
chmod +x "${SIDECAR_DIR}/server.py"
if [[ -f "${SIDECAR_DIR}/hud_overlay_bin" ]]; then
  chmod +x "${SIDECAR_DIR}/hud_overlay_bin"
fi

# 3. Install /hud skill into ~/.gemini/config/skills/hud for immediate discovery
mkdir -p "${SKILL_LINK_DIR}"
rm -rf "${SKILL_LINK_DIR}/hud"
cp -R "${SRC_DIR}/skills/hud" "${SKILL_LINK_DIR}/hud"

# 4. Merge lifecycle hooks into ~/.gemini/config/hooks.json (preserving existing hooks)
python3 - << 'PYEOF'
import json, os

config_dir = os.path.expanduser("~/.gemini/config")
hooks_file = os.path.join(config_dir, "hooks.json")
plugin_hooks_file = os.path.join(config_dir, "plugins", "agy-telemetry-hud", "hooks.json")

with open(plugin_hooks_file, "r", encoding="utf-8") as f:
    new_hooks = json.load(f)

existing = {}
if os.path.exists(hooks_file):
    try:
        with open(hooks_file, "r", encoding="utf-8") as f:
            existing = json.load(f)
    except Exception:
        existing = {}

existing.update(new_hooks)
tmp = hooks_file + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(existing, f, ensure_ascii=False, indent=2)
os.replace(tmp, hooks_file)
print("  ✅ Lifecycle hooks registered in ~/.gemini/config/hooks.json")
PYEOF

# 5. Configure Antigravity CLI (agy) statusLine in ~/.gemini/antigravity-cli/settings.json
mkdir -p "${CLI_DIR}"
python3 - << 'PYEOF'
import json, os

cli_settings = os.path.expanduser("~/.gemini/antigravity-cli/settings.json")
data = {}
if os.path.exists(cli_settings):
    try:
        with open(cli_settings, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}

data["statusLine"] = {
    "type": "command",
    "command": "python3 ~/.gemini/config/plugins/agy-telemetry-hud/scripts/cli_statusline.py",
    "enabled": True,
    "padding": 0,
    "stack_with_default": False
}

tmp = cli_settings + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
os.replace(tmp, cli_settings)
print("  ✅ CLI statusLine configured in ~/.gemini/antigravity-cli/settings.json")
PYEOF

# 6. Enable plugin & sidecar via LanguageServer RPC (if inside Antigravity) and ~/.gemini/config/config.json
if [[ -n "${ANTIGRAVITY_LS_ADDRESS:-}" && -n "${ANTIGRAVITY_CSRF_TOKEN:-}" ]]; then
  curl -sS -X POST \
    "http://${ANTIGRAVITY_LS_ADDRESS}/exa.language_server_pb.LanguageServerService/JetboxWriteState" \
    -H "Content-Type: application/json" \
    -H "x-codeium-csrf-token: ${ANTIGRAVITY_CSRF_TOKEN}" \
    -d '{"userConfig":{"plugins":{"agy-telemetry-hud":{"enabled":false}},"sidecars":{"agy-telemetry-hud":{"enabled":false}}}}' >/dev/null || true
  sleep 1
  curl -sS -X POST \
    "http://${ANTIGRAVITY_LS_ADDRESS}/exa.language_server_pb.LanguageServerService/JetboxWriteState" \
    -H "Content-Type: application/json" \
    -H "x-codeium-csrf-token: ${ANTIGRAVITY_CSRF_TOKEN}" \
    -d '{"userConfig":{"plugins":{"agy-telemetry-hud":{"enabled":true}},"sidecars":{"agy-telemetry-hud":{"enabled":true}}}}' >/dev/null || true
  echo "  ✅ Live-enabled plugin and native HUD overlay via LanguageServer RPC"
else
  python3 - << 'PYEOF'
import json, os

cfg_path = os.path.expanduser("~/.gemini/config/config.json")
cfg = {}
if os.path.exists(cfg_path):
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}

cfg.setdefault("plugins", {})["agy-telemetry-hud"] = {"enabled": True}
cfg.setdefault("sidecars", {})["agy-telemetry-hud"] = {"enabled": True}

tmp = cfg_path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
os.chmod(tmp, 0o600)
os.replace(tmp, cfg_path)
print("  ✅ Enabled plugin and sidecar in ~/.gemini/config/config.json")
PYEOF
fi

echo ""
echo "🎉 Installation complete! Everything lives under ~/.gemini/config/ and survives all Antigravity upgrades."
echo "   - Desktop App Native HUD: Anchored directly below Overview on the far right edge (auto-hides when window is narrowed, drag anywhere to reposition)"
echo "   - Terminal CLI (agy): Automatic 2-line status bar at bottom of prompt"
