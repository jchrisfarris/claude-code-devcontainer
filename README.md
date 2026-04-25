# Claude Code Devcontainer

A sandboxed environment for running Claude Code in fully autonomous mode — with explicit, user-controlled constraints on what it can actually do.

Forked from [Trail of Bits](https://github.com/trailofbits/claude-code-devcontainer), with their security-focused foundations kept intact and extended with AWS credential scoping.

## The Idea

Claude running in `--dangerously-skip-permissions` mode is productive but risky on a host machine. This repo solves that with two independent layers of constraint:

**1. Filesystem isolation** — Claude runs in a Docker devcontainer. It cannot touch your host files, install system packages, or affect other projects. The container is the sandbox.

**2. AWS permission scoping** — When Claude needs cloud access, you inject temporary credentials scoped to a session policy. Claude starts read-only. When it hits a permission error and needs more, it edits the session policy file and asks you to re-run one command. You stay in control of what it can do in AWS without approving every individual action.

Together these let Claude operate autonomously on real tasks — including `terraform apply` and AWS deployments — while you retain meaningful oversight without becoming a bottleneck.

## Prerequisites

- **Docker runtime** (one of):
  - [Docker Desktop](https://docker.com/products/docker-desktop)

- **For terminal workflows** (one-time install):

  ```bash
  npm install -g @devcontainers/cli
  git clone https://github.com/YOUR-USERNAME/claude-code-devcontainer ~/.claude-devcontainer
  ~/.claude-devcontainer/install.sh self-install
  ```

- **For AWS credential injection:** `uv` must be installed on the host (`brew install uv`)

## Quick Start

Choose the pattern that fits your workflow:

### Pattern A: Per-Project Container

Each project gets its own isolated container. Best for one-off reviews or untrusted repos.

```bash
git clone <repo-url>
cd repo
devc .          # Install devcontainer template + start container
devc shell      # Open shell in container
```

### Pattern B: Shared Workspace Container (preferred)

A parent directory holds the devcontainer config; clone multiple repos inside. Best for multiple repos, of when you have files that doen't belong in git.

```bash
mkdir -p ~/sandbox/my-project && cd ~/sandbox/my-project
devc .
devc shell

# Inside container:
git clone <repo-1>
git clone <repo-2>
cd repo-1 && claude
```

## Token-Based Auth (Headless)

To skip the interactive login wizard:

```bash
claude setup-token                        # run on host, one-time
export CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...
devc rebuild                              # rebuilds with token injected
```

The token is forwarded into the container. On each container creation, `post_install.py` runs a one-shot auth handshake so `claude` starts without the login wizard.

This works around Claude Code's interactive onboarding wizard always showing in containers, even with valid credentials ([#8938](https://github.com/anthropics/claude-code/issues/8938)).

If you don't set a token, the interactive login flow works as before.

## AWS Credentials

Claude starts with read-only AWS access. It can request additional permissions by editing a policy file; you grant them by re-running one command.

### One-time setup per AWS account

```bash
devc aws-setup-role --profile my-profile
```

This creates an IAM role called `ClaudeDevContainer` in the account associated with `my-profile`, with `AdministratorAccess` attached. The session policy (below) is what actually constrains Claude — the role's broad permissions are the ceiling, not the floor.

For IAM Identity Center (SSO) users: the role is assumable by any principal in the account, so your SSO role can assume it without per-user trust policy configuration.

### Inject credentials before a session

```bash
devc aws-creds --profile my-profile
```

This:
1. Assumes `ClaudeDevContainer` with a read-only session policy
2. Injects the temporary credentials into the running container as profile `my-profile`
3. Writes the active session policy to `/workspace/session-policy.json`

Credentials expire after 6 hours. Re-run the command to refresh.

### How Claude requests more permissions

When Claude hits an `AccessDenied` error, it edits `/workspace/session-policy.json` to add what it needs. For example, to run `terraform apply` it might add:

```json
{
  "Effect": "Allow",
  "Action": ["ec2:*", "s3:*", "iam:PassRole"],
  "Resource": "*"
}
```

Then it asks you to re-run `devc aws-creds --profile my-profile`. The next session picks up the updated policy. You review the diff before running — that's the approval gate.

The default session policy (read-only) lives in `aws-config/session-policy.json` in this repo and is used on first run. After that, the container's copy is used on re-runs.

### Multiple AWS accounts

Run `devc aws-creds` multiple times with different profiles — credentials accumulate as named profiles in the container's `~/.aws/credentials`:

```bash
devc aws-creds --profile dev
devc aws-creds --profile staging
```

Inside the container, use `AWS_PROFILE=dev` or `--profile dev` to select. Credentials do not survive `devc rebuild` by design.

## CLI Reference

```
devc .              Install devcontainer template + start container
devc up             Start the devcontainer
devc rebuild        Rebuild container (preserves persistent volumes)
devc destroy [-f]   Remove container, volumes, and image
devc down           Stop the container
devc shell          Open zsh shell in container
devc exec CMD       Execute command inside the container
devc upgrade        Upgrade Claude Code in the container
devc mount SRC DST  Add a bind mount (host → container)
devc sync [NAME]    Sync Claude Code sessions to host (for /insights)
devc cp CONT HOST   Copy files from container to host
devc aws-creds      Inject scoped AWS credentials into container
devc aws-setup-role Create the ClaudeDevContainer IAM role
devc self-install   Install devc to ~/.local/bin
devc update         Update devc to latest version
devc claude         Run claude --dangerously-skip-permissions in container
```

> Use `devc destroy` to clean up Docker resources. Removing containers manually (e.g. `docker rm`) leaves orphaned volumes that `devc destroy` won't find.

## Session Sync for `/insights`

Claude Code's `/insights` reads from `~/.claude/projects/` on the host. Sessions inside devcontainer volumes are invisible to it. `devc sync` copies them:

```bash
devc sync              # Sync all devcontainers
devc sync my-project   # Filter by name (substring match)
```

Devcontainers are auto-discovered via Docker labels — no need to know container names or IDs. The sync is incremental, so it's safe to run repeatedly.

## File Sharing

### VS Code / Cursor

Drag files from your host into the VS Code Explorer panel — they are copied into `/workspace/` automatically. No configuration needed.

### Terminal: `devc mount`

To make a host directory available inside the container:

```bash
devc mount ~/drop /drop           # Read-write
devc mount ~/secrets /secrets --readonly
```

This adds a bind mount to `devcontainer.json` and recreates the container. Existing mounts are preserved across `devc template` updates.

> Avoid mounting large host directories. Every mounted path is writable from inside the container unless `--readonly` is specified.



## Container Details

| Component | Details |
|-----------|---------|
| Base | Ubuntu 24.04, Node.js 22, Python 3.13 + uv, zsh |
| User | `vscode` (passwordless sudo), working dir `/workspace` |
| Tools | `rg`, `fd`, `ast-grep`, `tmux`, `fzf`, `delta`, `iptables`, `terraform`, AWS CLI |
| Persistent volumes | Shell history (`/commandhistory`), Claude config (`~/.claude`), GitHub CLI auth (`~/.config/gh`) |
| Host mounts | `~/.gitconfig` (read-only), `.devcontainer/` (read-only) |
| AWS credentials | Not persisted — lost on `devc rebuild` (intentional) |

## Troubleshooting

**`devcontainer CLI not found`**
```bash
npm install -g @devcontainers/cli
```

**`uv: command not found` when running `devc aws-creds`**
```bash
brew install uv
```

**SSO credentials expired**
```bash
aws sso login --profile my-profile
devc aws-creds --profile my-profile
```

**Container won't start**
1. Check Docker is running
2. `devc rebuild`
3. `docker logs $(docker ps -lq)`

**GitHub CLI auth not persisting**
```bash
sudo chown -R $(id -u):$(id -g) ~/.config/gh
```
