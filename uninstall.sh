#!/usr/bin/env bash
set -euo pipefail

echo "🧹 Uninstalling Antigravity Telemetry HUD..."

if [[ -n "${ANTIGRAVITY_LS_ADDRESS:-}" && -n "${ANTIGRAVITY_CSRF_TOKEN:-}" ]]; then
  curl -sS -X POST \
    "http://${ANTIGRAVITY_LS_ADDRESS}/exa.language_server_pb.LanguageServerService/JetboxWriteState" \
    -H "Content-Type: application/json" \
    -H "x-codeium-csrf-token: ${ANTIGRAVITY_CSRF_TOKEN}" \
    -d '{"userConfig":{"plugins":{"agy-telemetry-hud":{"enabled":false}},"sidecars":{"agy-telemetry-hud":{"enabled":false}}}}' >/dev/null || true
fi

pkill -f "hud_overlay_bin" || true
rm -rf "${HOME}/.gemini/config/plugins/agy-telemetry-hud"
rm -rf "${HOME}/.gemini/config/sidecars/agy-telemetry-hud"
rm -rf "${HOME}/.gemini/config/skills/hud"

python3 - << 'PYEOF'
import json, os

hooks_file = os.path.expanduser("~/.gemini/config/hooks.json")
if os.path.exists(hooks_file):
    try:
        with open(hooks_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if "agy-telemetry-hud" in data:
            del data["agy-telemetry-hud"]
            with open(hooks_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

cli_settings = os.path.expanduser("~/.gemini/antigravity-cli/settings.json")
if os.path.exists(cli_settings):
    try:
        with open(cli_settings, "r", encoding="utf-8") as f:
            data = json.load(f)
        sl = data.get("statusLine", {})
        if "agy-telemetry-hud" in sl.get("command", ""):
            del data["statusLine"]
            with open(cli_settings, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
PYEOF

echo "✅ Uninstalled Antigravity Telemetry HUD."
