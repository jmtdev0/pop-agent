# pop-agent

Nightly GitHub issue-comment agent for `#pop` tasks.

`pop-agent` scans repositories owned by `jmtdev0` for issue comments written by
`jmtdev0` that contain `#pop`. It processes each unique comment once, asks
Codex CLI to make the requested local code changes in a clean clone, runs
discovered checks, pushes directly to the repository default branch, and replies
to the original GitHub comment with the result.

The agent stores runtime state on the server. Do not commit credentials here.

## Server layout

- Source: `/opt/pop-agent`
- State: `/var/lib/pop-agent/state.json`
- Workspaces: `/var/lib/pop-agent/work`
- Logs: `/var/log/pop-agent`
- Timer: `pop-agent.timer`, daily at `02:00 UTC`

## Required server tools

- `gh`, authenticated as `jmtdev0`
- `git`, with GitHub SSH access
- `codex`, authenticated and available in `PATH`
- `node`/`npm` for JavaScript repositories
- `python3` for Python repositories

Netlify CLI is not required. To let the agent trigger and verify Netlify deploys
directly, store a token outside this repository:

```bash
sudo install -d -m 700 /etc/pop-agent
sudoedit /etc/pop-agent/pop-agent.env
```

```bash
NETLIFY_AUTH_TOKEN=...
```

The systemd service loads this optional file. If no token is configured, the
agent falls back to GitHub checks/statuses.

## Manual usage

```bash
python -m pop_agent run --dry-run
python -m pop_agent run --max-tasks 1
```

## Safety model

- Only comments authored by `jmtdev0` are considered.
- Each `comment_id` is processed once.
- Failed tasks are not retried; write a new `#pop` comment to try again.
- Codex is allowed to edit only the cloned target workspace.
- During the Codex step, GitHub credentials are hidden from `gh`, and Git SSH
  network operations are disabled. The orchestrator performs comments and pushes.
- If discovered checks fail, the agent does not push.
- Netlify credentials are read from `/etc/pop-agent/pop-agent.env`; never commit
  them to this repository.
