#!/usr/bin/env python3
"""Post-install configuration for Claude Code devcontainer.

Runs on container creation to set up:
- Onboarding bypass (when CLAUDE_CODE_OAUTH_TOKEN is set)
- Claude user config (theme, remote control, workspace trust)
- Claude settings (bypassPermissions, hooks, statusline, deny rules)
- Default Claude commands and statusline script
- Tmux configuration (200k history, mouse support)
- Directory ownership fixes for mounted volumes
"""

import contextlib
import json
import os
import subprocess
import sys
from pathlib import Path


def setup_onboarding_bypass():
    """Bypass the interactive onboarding wizard when CLAUDE_CODE_OAUTH_TOKEN is set.

    Runs `claude -p` to seed ~/.claude.json with auth state. The subprocess
    writes the config file during startup before the API call completes, so
    a timeout is expected and acceptable. After the subprocess finishes (or
    times out), we check whether ~/.claude.json was populated and only then
    set hasCompletedOnboarding.

    Workaround for https://github.com/anthropics/claude-code/issues/8938.
    """
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    if not token:
        print(
            "[post_install] No CLAUDE_CODE_OAUTH_TOKEN set, skipping onboarding bypass",
            file=sys.stderr,
        )
        return

    # When `CLAUDE_CONFIG_DIR` is set, as is done in `devcontainer.json`, `claude` unexpectedly
    # looks for `.claude.json` in *that* folder, instead of in `~`, contradicting the documentation.
    #  See https://github.com/anthropics/claude-code/issues/3833#issuecomment-3694918874
    claude_json_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home()))
    claude_json = claude_json_dir / ".claude.json"

    print("[post_install] Running claude -p to populate auth state...", file=sys.stderr)
    try:
        result = subprocess.run(
            ["claude", "-p", "ok"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            print(
                f"[post_install] claude -p exited {result.returncode}: "
                f"{result.stderr.strip()}",
                file=sys.stderr,
            )
    except subprocess.TimeoutExpired:
        print(
            "[post_install] claude -p timed out (expected on cold start)",
            file=sys.stderr,
        )
    except (FileNotFoundError, OSError) as e:
        print(
            f"[post_install] Warning: could not run claude ({e}) — "
            "onboarding bypass skipped",
            file=sys.stderr,
        )
        return

    if not claude_json.exists():
        print(
            f"[post_install] Warning: {claude_json} not created by claude -p — "
            "onboarding bypass skipped",
            file=sys.stderr,
        )
        return

    config: dict = {}
    try:
        config = json.loads(claude_json.read_text())
    except json.JSONDecodeError as e:
        print(
            f"[post_install] Warning: {claude_json} has invalid JSON ({e}), "
            "starting fresh",
            file=sys.stderr,
        )

    config["hasCompletedOnboarding"] = True

    claude_json.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    print(
        f"[post_install] Onboarding bypass configured: {claude_json}", file=sys.stderr
    )


def setup_claude_user_config():
    """Pre-configure Claude Code user preferences in .claude.json.

    Sets theme, workspace trust, and remote-control acceptance so those
    dialogs don't interrupt the first session. The bypass-permissions warning
    is intentionally left intact — it's the container's one remaining safety
    acknowledgement.
    """
    claude_json_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home()))
    claude_json_dir.mkdir(parents=True, exist_ok=True)
    claude_json = claude_json_dir / ".claude.json"

    config: dict = {}
    if claude_json.exists():
        with contextlib.suppress(json.JSONDecodeError):
            config = json.loads(claude_json.read_text())

    changed = False

    # Dark theme (matches most terminal setups; also happens to be the compiled-in default)
    if config.get("theme") != "dark":
        config["theme"] = "dark"
        changed = True

    # Skip "Enable Remote Control? (y/n)" prompt when --remote-control is passed
    if not config.get("remoteDialogSeen"):
        config["remoteDialogSeen"] = True
        changed = True

    # Pre-accept the workspace trust dialog for /workspace
    projects = config.setdefault("projects", {})
    workspace = projects.setdefault("/workspace", {})
    if not workspace.get("hasTrustDialogAccepted"):
        workspace["hasTrustDialogAccepted"] = True
        changed = True

    if changed:
        claude_json.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        print(f"[post_install] Claude user config updated: {claude_json}", file=sys.stderr)
    else:
        print("[post_install] Claude user config already configured", file=sys.stderr)


def setup_claude_settings():
    """Configure Claude Code with bypassPermissions and safety defaults."""
    claude_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
    claude_dir.mkdir(parents=True, exist_ok=True)

    settings_file = claude_dir / "settings.json"

    # Load existing settings or start fresh
    settings = {}
    if settings_file.exists():
        with contextlib.suppress(json.JSONDecodeError):
            settings = json.loads(settings_file.read_text())

    # Always enforce bypassPermissions (container is the security boundary)
    if "permissions" not in settings:
        settings["permissions"] = {}
    settings["permissions"]["defaultMode"] = "bypassPermissions"

    # Deny rules act as hard blocks even in bypassPermissions mode
    if "deny" not in settings["permissions"]:
        settings["permissions"]["deny"] = [
            "Bash(rm -rf *)",
            "Bash(rm -fr *)",
            "Bash(mkfs *)",
            "Bash(dd *)",
            "Bash(wget *|bash*)",
            "Bash(wget *| bash*)",
            "Bash(git push --force*)",
            "Bash(git push *--force*)",
            "Bash(git reset --hard*)",
            "Edit(~/.bashrc)",
            "Edit(~/.zshrc)",
        ]

    # Set other defaults only if not already configured (preserves user customizations)
    defaults: dict = {

        "skipDangerousModePermissionPrompt": True,
        "theme": "dark",
        "cleanupPeriodDays": 365,
        "enableAllProjectMcpServers": False,
        "alwaysThinkingEnabled": True,
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {
                            "type": "command",
                            "command": (
                                "CMD=$(jq -r '.tool_input.command'); "
                                "if echo \"$CMD\" | grep -qiE "
                                "'(^|;[[:space:]]*|&&[[:space:]]*|[|][|][[:space:]]*|[|][[:space:]]*)rm[[:space:]]' "
                                "&& echo \"$CMD\" | grep -qiE '(^|[[:space:]])-[a-zA-Z]*[rR]|--recursive' "
                                "&& echo \"$CMD\" | grep -qiE '(^|[[:space:]])-[a-zA-Z]*[fF]|--force'; "
                                "then echo 'BLOCKED: Use trash instead of rm -rf' >&2; exit 2; fi"
                            ),
                        },
                        {
                            "type": "command",
                            "command": (
                                "CMD=$(jq -r '.tool_input.command'); "
                                "if echo \"$CMD\" | grep -qE 'git[[:space:]]+push.*(main|master)'; "
                                "then echo 'BLOCKED: Use feature branches, not direct push to main' >&2; exit 2; fi"
                            ),
                        },
                    ],
                }
            ]
        },
        "statusLine": {
            "type": "command",
            "command": "~/.claude/statusline.sh",
        },
    }

    for key, value in defaults.items():
        settings.setdefault(key, value)

    settings_file.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    print(
        f"[post_install] Claude settings configured: {settings_file}", file=sys.stderr
    )


def setup_claude_defaults():
    """Install default Claude scripts from /opt/claude-defaults/.

    Commands are served via a bind mount (.devcontainer/commands → ~/.claude/commands)
    so they stay live-updated from the host without a container rebuild.
    """
    claude_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
    defaults_dir = Path("/opt/claude-defaults")

    if not defaults_dir.exists():
        print("[post_install] No /opt/claude-defaults found, skipping", file=sys.stderr)
        return

    # Install statusline script (only if not already customized)
    statusline_src = defaults_dir / "statusline.sh"
    statusline_dst = claude_dir / "statusline.sh"
    if statusline_src.exists() and not statusline_dst.exists():
        statusline_dst.write_bytes(statusline_src.read_bytes())
        statusline_dst.chmod(0o755)
        print(f"[post_install] Installed statusline: {statusline_dst}", file=sys.stderr)


def setup_tmux_config():
    """Configure tmux with 200k history, mouse support, and vi keys."""
    tmux_conf = Path.home() / ".tmux.conf"

    if tmux_conf.exists():
        print("[post_install] Tmux config exists, skipping", file=sys.stderr)
        return

    config = """\
# 200k line scrollback history
set-option -g history-limit 200000

# Enable mouse support
set -g mouse on

# Use vi keys in copy mode
setw -g mode-keys vi

# Start windows and panes at 1, not 0
set -g base-index 1
setw -g pane-base-index 1

# Renumber windows when one is closed
set -g renumber-windows on

# Faster escape time for vim
set -sg escape-time 10

# True color support
set -g default-terminal "tmux-256color"
set -ag terminal-overrides ",xterm-256color:RGB"

# Terminal features (ghostty, cursor shape in vim)
set -as terminal-features ",xterm-ghostty:RGB"
set -as terminal-features ",xterm*:RGB"
set -ga terminal-overrides ",xterm*:colors=256"
set -ga terminal-overrides '*:Ss=\\E[%p1%d q:Se=\\E[ q'

# Status bar
set -g status-style 'bg=#333333 fg=#ffffff'
set -g status-left '[#S] '
set -g status-right '%Y-%m-%d %H:%M'
"""
    tmux_conf.write_text(config, encoding="utf-8")
    print(f"[post_install] Tmux configured: {tmux_conf}", file=sys.stderr)


def fix_directory_ownership():
    """Fix ownership of mounted volumes that may have root ownership."""
    uid = os.getuid()
    gid = os.getgid()

    dirs_to_fix = [
        Path.home() / ".claude",
        Path("/commandhistory"),
        Path.home() / ".config" / "gh",
    ]

    for dir_path in dirs_to_fix:
        if dir_path.exists():
            try:
                # Use sudo to fix ownership if needed
                stat_info = dir_path.stat()
                if stat_info.st_uid != uid:
                    subprocess.run(
                        ["sudo", "chown", "-R", f"{uid}:{gid}", str(dir_path)],
                        check=True,
                        capture_output=True,
                    )
                    print(
                        f"[post_install] Fixed ownership: {dir_path}", file=sys.stderr
                    )
            except (PermissionError, subprocess.CalledProcessError) as e:
                print(
                    f"[post_install] Warning: Could not fix ownership of {dir_path}: {e}",
                    file=sys.stderr,
                )


def setup_global_gitignore():
    """Set up global gitignore and local git config.

    Since ~/.gitconfig is mounted read-only from host, we create a local
    config file that includes the host config and adds container-specific
    settings like core.excludesfile and delta configuration.

    GIT_CONFIG_GLOBAL env var (set in devcontainer.json) points git to this
    local config as the "global" config.
    """
    home = Path.home()
    gitignore = home / ".gitignore_global"
    local_gitconfig = home / ".gitconfig.local"
    host_gitconfig = home / ".gitconfig"

    # Create global gitignore with common patterns
    patterns = """\
# Claude Code
.claude/

# macOS
.DS_Store
.AppleDouble
.LSOverride
._*

# Python
*.pyc
*.pyo
__pycache__/
*.egg-info/
.eggs/
*.egg
.venv/
venv/
.mypy_cache/
.ruff_cache/

# Node
node_modules/
.npm/

# Editors
*.swp
*.swo
*~
.idea/
.vscode/
*.sublime-*

# Misc
*.log
.env.local
.env.*.local
"""
    gitignore.write_text(patterns, encoding="utf-8")
    print(f"[post_install] Global gitignore created: {gitignore}", file=sys.stderr)

    # Create local git config that includes host config and sets excludesfile + delta
    # Delta config is included here so it works even if host doesn't have it configured
    local_config = f"""\
# Container-local git config
# Includes host config (mounted read-only) and adds container settings

[include]
    path = {host_gitconfig}

[core]
    excludesfile = {gitignore}
    pager = delta

[interactive]
    diffFilter = delta --color-only

[delta]
    navigate = true
    light = false
    line-numbers = true
    side-by-side = false

[merge]
    conflictstyle = diff3

[diff]
    colorMoved = default

[gpg "ssh"]
    program = /usr/bin/ssh-keygen
"""
    local_gitconfig.write_text(local_config, encoding="utf-8")
    print(
        f"[post_install] Local git config created: {local_gitconfig}", file=sys.stderr
    )


def main():
    """Run all post-install configuration."""
    print("[post_install] Starting post-install configuration...", file=sys.stderr)

    setup_onboarding_bypass()
    fix_directory_ownership()
    # setup_claude_user_config()
    setup_claude_defaults()
    setup_claude_settings()
    setup_tmux_config()
    setup_global_gitignore()

    print("[post_install] Configuration complete!", file=sys.stderr)


if __name__ == "__main__":
    main()
