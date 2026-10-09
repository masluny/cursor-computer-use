#!/bin/zsh
# Installs the computer-use MCP server and the /grok-computer-use skill into Cursor (global, all projects).
set -euo pipefail
ROOT="${0:A:h}"
cd "$ROOT"

command -v uv >/dev/null || { echo "uv is required: https://docs.astral.sh/uv/getting-started/installation/"; exit 1; }
[[ -x .venv/bin/python ]] || uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -q -e .

mkdir -p ~/.cursor/skills/grok-computer-use
cp skill/grok-computer-use/SKILL.md ~/.cursor/skills/grok-computer-use/SKILL.md
echo "Skill installed: ~/.cursor/skills/grok-computer-use/SKILL.md"

.venv/bin/python - "$ROOT" <<'PY'
import json, os, shutil, sys

root = sys.argv[1]
path = os.path.expanduser("~/.cursor/mcp.json")
cfg = {}
if os.path.exists(path):
    with open(path) as f:
        cfg = json.load(f)  # invalid JSON stops the install rather than overwriting the file
    shutil.copy(path, path + ".bak")
cfg.setdefault("mcpServers", {})["computer-use"] = {
    "type": "stdio",
    "command": os.path.join(root, ".venv", "bin", "python"),
    "args": ["-m", "cu_mcp"],
}
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
print(f"MCP server 'computer-use' registered in {path}")
PY

cat <<'EOF'

Next:
  1. System Settings > Privacy & Security: enable Cursor under Accessibility
     and under Screen & System Audio Recording.
  2. Quit Cursor completely (cmd+q) and reopen it.
  3. In Cursor's terminal run:  .venv/bin/cursor-computer-use --check
  4. In Agent chat pick a vision model (e.g. Grok 4.7) and type:  /grok-computer-use <task>
EOF
