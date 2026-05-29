from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


MARKER_RE = re.compile(r"(?i)(?<![\w-])#pop(?![\w-])")
RESPONSE_MARKER_RE = re.compile(r"<!--\s*pop-agent:source-comment-id=(\d+)\s*-->")

DEFAULT_OWNER = "jmtdev0"
DEFAULT_MAX_TASKS = 3
DEFAULT_STATE_FILE = Path("/var/lib/pop-agent/state.json")
DEFAULT_WORK_DIR = Path("/var/lib/pop-agent/work")
DEFAULT_LOG_DIR = Path("/var/log/pop-agent")
DEFAULT_CODEX_MODEL = "gpt-5.5"
DEFAULT_CODEX_EFFORT = "high"
DEFAULT_CODEX_SANDBOX = "workspace-write"
DEFAULT_CODEX_NETWORK_ACCESS = True
DEFAULT_CODEX_TIMEOUT_SEC = 3600
DEFAULT_CHECK_TIMEOUT_SEC = 900
DEFAULT_NETLIFY_WAIT_SEC = 600
DEFAULT_NETLIFY_API_BASE = "https://api.netlify.com/api/v1"
DEFAULT_NETLIFY_TRIGGER_DELAY_SEC = 30
CODEX_SECRET_ENV_NAMES = {
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GITHUB_PAT",
    "NETLIFY_AUTH_TOKEN",
    "NPM_TOKEN",
    "SSH_AUTH_SOCK",
}


class AgentError(RuntimeError):
    pass


class CommandError(AgentError):
    def __init__(self, command: list[str], returncode: int, stdout: str, stderr: str):
        self.command = command
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(f"command failed ({returncode}): {format_command(command)}")


@dataclasses.dataclass(frozen=True)
class Task:
    repo: str
    default_branch: str
    ssh_url: str
    issue_number: int
    issue_title: str
    comment_id: int
    comment_url: str
    comment_created_at: str
    comment_updated_at: str
    body: str

    @property
    def spec(self) -> str:
        return clean_pop_marker(self.body).strip()


@dataclasses.dataclass
class CheckResult:
    name: str
    command: list[str]
    returncode: int
    duration_sec: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class RunLogger:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, message: str = "") -> None:
        timestamp = utc_now()
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{timestamp}] {message}\n")

    def section(self, title: str) -> None:
        self.write("")
        self.write(f"== {title} ==")


class State:
    def __init__(self, path: Path):
        self.path = path
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": 1, "processed": {}}
        with self.path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        data.setdefault("schema_version", 1)
        data.setdefault("processed", {})
        return data

    def is_processed(self, comment_id: int) -> bool:
        return str(comment_id) in self.data["processed"]

    def mark(self, comment_id: int, record: dict[str, Any]) -> None:
        record = dict(record)
        record.setdefault("processed_at", utc_now())
        self.data["processed"][str(comment_id)] = record
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(self.data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        tmp.replace(self.path)


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def format_command(command: list[str]) -> str:
    return " ".join(sh_quote(part) for part in command)


def sh_quote(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./:=@%+-]+", value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


def env_flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"", "0", "false", "no", "off"}


def run_command(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    timeout: int | None = None,
    logger: RunLogger | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    started = time.monotonic()
    if logger:
        where = f" cwd={cwd}" if cwd else ""
        logger.write(f"$ {format_command(command)}{where}")
    result = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        env=env,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )
    elapsed = time.monotonic() - started
    if logger:
        logger.write(f"exit={result.returncode} elapsed={elapsed:.1f}s")
        if result.stdout.strip():
            logger.write("stdout:\n" + result.stdout.rstrip())
        if result.stderr.strip():
            logger.write("stderr:\n" + result.stderr.rstrip())
    if check and result.returncode != 0:
        raise CommandError(command, result.returncode, result.stdout, result.stderr)
    return result


def gh_json(args: list[str], *, logger: RunLogger | None = None) -> Any:
    result = run_command(["gh", "api", *args], logger=logger)
    if not result.stdout.strip():
        return None
    return json.loads(result.stdout)


def gh_paginated(endpoint: str, *, logger: RunLogger | None = None) -> list[Any]:
    result = run_command(["gh", "api", "--paginate", "--slurp", endpoint], logger=logger)
    if not result.stdout.strip():
        return []
    pages = json.loads(result.stdout)
    items: list[Any] = []
    for page in pages:
        if isinstance(page, list):
            items.extend(page)
        else:
            items.append(page)
    return items


def has_pop_marker(body: str) -> bool:
    return MARKER_RE.search(body or "") is not None


def clean_pop_marker(body: str) -> str:
    return MARKER_RE.sub("", body or "").strip()


def extract_response_markers(comments: list[dict[str, Any]]) -> set[int]:
    processed: set[int] = set()
    for comment in comments:
        body = comment.get("body") or ""
        for match in RESPONSE_MARKER_RE.finditer(body):
            processed.add(int(match.group(1)))
    return processed


def issue_number_from_url(issue_url: str) -> int:
    match = re.search(r"/issues/(\d+)$", issue_url or "")
    if not match:
        raise AgentError(f"could not parse issue number from {issue_url!r}")
    return int(match.group(1))


def discover_tasks(owner: str, state: State, *, logger: RunLogger) -> list[Task]:
    repos = gh_paginated(
        "/user/repos?affiliation=owner&sort=updated&direction=desc&per_page=100",
        logger=logger,
    )
    tasks: list[Task] = []
    for repo in repos:
        full_name = repo.get("full_name", "")
        if not full_name.startswith(f"{owner}/"):
            continue
        if repo.get("archived") or repo.get("disabled"):
            continue
        comments = gh_paginated(f"/repos/{full_name}/issues/comments?per_page=100", logger=logger)
        already_answered = extract_response_markers(comments)
        for comment in comments:
            comment_id = int(comment["id"])
            body = comment.get("body") or ""
            if RESPONSE_MARKER_RE.search(body):
                continue
            if comment_id in already_answered or state.is_processed(comment_id):
                continue
            if (comment.get("user") or {}).get("login") != owner:
                continue
            if not has_pop_marker(body):
                continue
            issue_number = issue_number_from_url(comment.get("issue_url", ""))
            issue = gh_json([f"/repos/{full_name}/issues/{issue_number}"], logger=logger)
            tasks.append(
                Task(
                    repo=full_name,
                    default_branch=repo.get("default_branch") or "main",
                    ssh_url=repo.get("ssh_url") or f"git@github.com:{full_name}.git",
                    issue_number=issue_number,
                    issue_title=(issue or {}).get("title") or "",
                    comment_id=comment_id,
                    comment_url=comment.get("html_url") or "",
                    comment_created_at=comment.get("created_at") or "",
                    comment_updated_at=comment.get("updated_at") or "",
                    body=body,
                )
            )
    return sorted(tasks, key=lambda task: (task.comment_created_at, task.repo, task.comment_id))


def safe_workdir(base: Path, task: Task) -> Path:
    repo_slug = task.repo.replace("/", "__")
    return base / f"{repo_slug}__comment_{task.comment_id}"


def clone_repo(task: Task, workdir: Path, *, logger: RunLogger) -> None:
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.parent.mkdir(parents=True, exist_ok=True)
    run_command(
        [
            "git",
            "clone",
            "--depth",
            "1",
            "--branch",
            task.default_branch,
            task.ssh_url,
            str(workdir),
        ],
        logger=logger,
        timeout=600,
    )
    run_command(["git", "config", "user.name", "pop-agent[bot]"], cwd=workdir, logger=logger)
    run_command(
        ["git", "config", "user.email", "pop-agent[bot]@users.noreply.github.com"],
        cwd=workdir,
        logger=logger,
    )


def codex_prompt(task: Task) -> str:
    return textwrap.dedent(
        f"""
        You are running inside a clean clone of {task.repo}.

        Source GitHub comment:
        {task.comment_url}

        Issue:
        #{task.issue_number} {task.issue_title}

        Task specification from the user's #pop comment:
        ---
        {task.spec}
        ---

        Implement the requested changes in this repository.

        Hard constraints:
        - Do not push to GitHub.
        - Do not create or reply to GitHub comments.
        - Do not deploy to Netlify.
        - Do not read, print, or create secrets.
        - Keep changes focused on the requested task.
        - Follow the repository's existing style and tooling.
        - Run relevant local checks when practical, but the orchestrator will also run discovered checks after you finish.

        Final response format:
        - Short summary of what changed.
        - Checks you ran, if any.
        - Any important caveats.
        """
    ).strip()


def codex_command(args: argparse.Namespace, output_file: Path) -> list[str]:
    cmd = [
        args.codex_cli,
        "--ask-for-approval",
        "never",
        "exec",
        "--model",
        args.codex_model,
        "-c",
        f'model_reasoning_effort="{args.codex_effort}"',
        "--sandbox",
        args.codex_sandbox,
        "--output-last-message",
        str(output_file),
        "-",
    ]
    if args.codex_network_access and args.codex_sandbox == "workspace-write":
        cmd[cmd.index("--sandbox"):cmd.index("--sandbox")] = [
            "-c",
            "sandbox_workspace_write.network_access=true",
        ]
    return cmd


def codex_environment(runtime_dir: Path) -> dict[str, str]:
    empty_gh_config = runtime_dir / "empty-gh-config"
    empty_gh_config.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    for name in CODEX_SECRET_ENV_NAMES:
        env.pop(name, None)
    env["GH_CONFIG_DIR"] = str(empty_gh_config)
    env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes -o IdentitiesOnly=yes -i /var/lib/pop-agent/no-such-key"
    env["HOME"] = os.environ.get("HOME", "/root")
    env["LANG"] = os.environ.get("LANG", "en_US.UTF-8")
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run_codex(task: Task, workdir: Path, runtime_dir: Path, args: argparse.Namespace, *, logger: RunLogger) -> Path:
    prompt = codex_prompt(task)
    output_file = runtime_dir / "codex_outputs" / f"{task.comment_id}.md"
    output_file.parent.mkdir(parents=True, exist_ok=True)
    env = codex_environment(runtime_dir)
    cmd = codex_command(args, output_file)
    run_command(cmd, cwd=workdir, env=env, input_text=prompt, timeout=args.codex_timeout, logger=logger)
    return output_file


def git_has_changes(workdir: Path, *, logger: RunLogger) -> bool:
    result = run_command(["git", "status", "--porcelain"], cwd=workdir, logger=logger)
    return bool(result.stdout.strip())


def package_manager_install_command(workdir: Path) -> list[str] | None:
    if (workdir / "package-lock.json").exists():
        return ["npm", "ci"]
    if (workdir / "pnpm-lock.yaml").exists():
        return ["corepack", "pnpm", "install", "--frozen-lockfile"]
    if (workdir / "yarn.lock").exists():
        return ["corepack", "yarn", "install", "--immutable"]
    if (workdir / "package.json").exists():
        return ["npm", "install", "--no-package-lock"]
    return None


def discovered_node_scripts(workdir: Path) -> list[str]:
    package_json = workdir / "package.json"
    if not package_json.exists():
        return []
    try:
        data = json.loads(package_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    scripts = data.get("scripts") or {}
    return [name for name in ["lint", "test", "build"] if name in scripts]


def project_files(workdir: Path) -> list[str]:
    result = run_command(["git", "ls-files"], cwd=workdir, check=False)
    if result.returncode == 0:
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    ignored_roots = {".git", "node_modules", "dist", "build", ".venv", "venv"}
    files: list[str] = []
    for path in workdir.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(workdir).as_posix()
        if any(part in ignored_roots for part in rel.split("/")):
            continue
        files.append(rel)
    return files


def is_python_test_file(rel_path: str) -> bool:
    parts = rel_path.split("/")
    name = parts[-1]
    if not name.endswith(".py"):
        return False
    return "tests" in parts[:-1] or name.startswith("test_") or name.endswith("_test.py")


def python_checks_discovered(workdir: Path) -> bool:
    return any(is_python_test_file(rel_path) for rel_path in project_files(workdir))


def run_check(
    name: str,
    command: list[str],
    workdir: Path,
    *,
    env: dict[str, str],
    timeout: int,
    logger: RunLogger,
) -> CheckResult:
    started = time.monotonic()
    result = run_command(command, cwd=workdir, env=env, timeout=timeout, logger=logger, check=False)
    return CheckResult(name=name, command=command, returncode=result.returncode, duration_sec=time.monotonic() - started)


def run_discovered_checks(task: Task, workdir: Path, runtime_dir: Path, args: argparse.Namespace, *, logger: RunLogger) -> list[CheckResult]:
    results: list[CheckResult] = []
    env = dict(os.environ)
    env["CI"] = "true"

    node_scripts = discovered_node_scripts(workdir)
    install_cmd = package_manager_install_command(workdir)
    if node_scripts and install_cmd:
        results.append(run_check("node dependency install", install_cmd, workdir, env=env, timeout=args.check_timeout, logger=logger))
        if not results[-1].ok:
            return results
        for script in node_scripts:
            results.append(
                run_check(
                    f"npm run {script}",
                    ["npm", "run", script],
                    workdir,
                    env=env,
                    timeout=args.check_timeout,
                    logger=logger,
                )
            )
            if not results[-1].ok:
                return results

    if python_checks_discovered(workdir):
        venv_dir = runtime_dir / "venvs" / f"{task.repo.replace('/', '__')}__{task.comment_id}"
        if venv_dir.exists():
            shutil.rmtree(venv_dir)
        run_command(["python3", "-m", "venv", str(venv_dir)], logger=logger, timeout=300)
        py = venv_dir / "bin" / "python"
        run_command([str(py), "-m", "pip", "install", "-U", "pip"], logger=logger, timeout=args.check_timeout)
        if (workdir / "requirements.txt").exists():
            results.append(
                run_check(
                    "python dependencies",
                    [str(py), "-m", "pip", "install", "-r", "requirements.txt"],
                    workdir,
                    env=env,
                    timeout=args.check_timeout,
                    logger=logger,
                )
            )
            if not results[-1].ok:
                return results
        results.append(
            run_check(
                "pytest",
                [str(py), "-m", "pip", "install", "pytest"],
                workdir,
                env=env,
                timeout=args.check_timeout,
                logger=logger,
            )
        )
        if not results[-1].ok:
            return results
        results.append(
            run_check("python -m pytest", [str(py), "-m", "pytest"], workdir, env=env, timeout=args.check_timeout, logger=logger)
        )
    return results


def cleanup_untracked_artifacts(workdir: Path, *, logger: RunLogger) -> None:
    candidates = [
        "node_modules",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".parcel-cache",
        ".next/cache",
        "coverage",
        "__pycache__",
    ]
    tracked = run_command(["git", "ls-files"], cwd=workdir, logger=logger).stdout.splitlines()
    tracked_set = set(tracked)
    for rel in candidates:
        path = workdir / rel
        if not path.exists():
            continue
        if any(item == rel or item.startswith(rel.rstrip("/") + "/") for item in tracked_set):
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()


def commit_and_push(task: Task, workdir: Path, *, logger: RunLogger) -> str:
    run_command(["git", "add", "-A"], cwd=workdir, logger=logger)
    if run_command(["git", "diff", "--cached", "--quiet"], cwd=workdir, logger=logger, check=False).returncode == 0:
        raise AgentError("no staged changes after cleanup")
    message = f"pop: resolve #pop comment {task.comment_id}"
    run_command(["git", "commit", "-m", message], cwd=workdir, logger=logger, timeout=300)
    sha = run_command(["git", "rev-parse", "HEAD"], cwd=workdir, logger=logger).stdout.strip()
    run_command(["git", "push", "origin", f"HEAD:{task.default_branch}"], cwd=workdir, logger=logger, timeout=600)
    return sha


def check_summaries(results: list[CheckResult]) -> list[str]:
    if not results:
        return ["no automated checks discovered"]
    lines = []
    for result in results:
        status = "passed" if result.ok else f"failed ({result.returncode})"
        lines.append(f"{format_command(result.command)}: {status}")
    return lines


def checks_ok(results: list[CheckResult]) -> bool:
    return all(result.ok for result in results)


def netlify_api_request(
    method: str,
    path: str,
    *,
    token: str,
    data: dict[str, Any] | None = None,
    logger: RunLogger,
) -> Any:
    base = os.environ.get("POP_AGENT_NETLIFY_API_BASE", DEFAULT_NETLIFY_API_BASE).rstrip("/")
    url = f"{base}/{path.lstrip('/')}"
    payload = json.dumps(data or {}).encode("utf-8") if data is not None else None
    request = urllib.request.Request(
        url,
        data=payload,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "pop-agent",
        },
    )
    logger.write(f"$ netlify api {method} {path}")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise AgentError(f"Netlify API {method} {path} failed ({exc.code}): {body[:500]}") from exc
    if not body.strip():
        return None
    return json.loads(body)


def paginated_netlify_api(path: str, *, token: str, logger: RunLogger) -> list[Any]:
    items: list[Any] = []
    separator = "&" if "?" in path else "?"
    for page in range(1, 21):
        page_path = f"{path}{separator}page={page}&per_page=100"
        data = netlify_api_request("GET", page_path, token=token, logger=logger)
        if not isinstance(data, list):
            return items
        items.extend(data)
        if len(data) < 100:
            return items
    return items


def netlify_site_matches(task: Task, site: dict[str, Any]) -> bool:
    settings = site.get("build_settings") or {}
    repo_path = settings.get("repo_path")
    repo_url = settings.get("repo_url") or ""
    repo_branch = settings.get("repo_branch")
    normalized_url = repo_url.removesuffix(".git").removeprefix("https://github.com/").removeprefix("git@github.com:")
    return (repo_path == task.repo or normalized_url == task.repo) and repo_branch in {None, "", task.default_branch}


def find_netlify_site(task: Task, *, token: str, logger: RunLogger) -> dict[str, Any] | None:
    sites = paginated_netlify_api("/sites", token=token, logger=logger)
    for site in sites:
        if isinstance(site, dict) and netlify_site_matches(task, site):
            logger.write(f"netlify site matched: {site.get('name')} ({site.get('id')})")
            return site
    logger.write(f"netlify site not found for {task.repo} branch {task.default_branch}")
    return None


def deploy_matches_sha(deploy: dict[str, Any], sha: str) -> bool:
    commit_ref = str(deploy.get("commit_ref") or "")
    commit_url = str(deploy.get("commit_url") or "")
    return commit_ref == sha or commit_url.rstrip("/").endswith(sha)


def find_netlify_deploy(site_id: str, sha: str, *, token: str, logger: RunLogger) -> dict[str, Any] | None:
    deploys = paginated_netlify_api(f"/sites/{site_id}/deploys", token=token, logger=logger)
    for deploy in deploys:
        if isinstance(deploy, dict) and deploy_matches_sha(deploy, sha):
            logger.write(f"netlify deploy matched: {deploy.get('id')} state={deploy.get('state')}")
            return deploy
    return None


def trigger_netlify_build(site_id: str, *, token: str, logger: RunLogger) -> None:
    netlify_api_request("POST", f"/sites/{site_id}/builds", token=token, data={}, logger=logger)
    logger.write(f"netlify build triggered for site {site_id}")


def netlify_deploy_url(deploy: dict[str, Any]) -> str:
    links = deploy.get("links") or {}
    for key in ["permalink", "alias"]:
        value = links.get(key)
        if value:
            return str(value)
    for key in ["deploy_ssl_url", "ssl_url", "deploy_url", "admin_url"]:
        value = deploy.get(key)
        if value:
            return str(value)
    return ""


def describe_netlify_deploy(deploy: dict[str, Any]) -> str:
    state = str(deploy.get("state") or "unknown")
    url = netlify_deploy_url(deploy)
    return f"{state}: {url}" if url else state


def terminal_netlify_state(deploy: dict[str, Any]) -> bool:
    return str(deploy.get("state") or "").lower() in {"ready", "error", "failed", "canceled"}


def netlify_status_via_api(task: Task, sha: str, wait_sec: int, *, logger: RunLogger) -> str | None:
    token = os.environ.get("NETLIFY_AUTH_TOKEN")
    if not token:
        logger.write("netlify api token not configured")
        return None

    site = find_netlify_site(task, token=token, logger=logger)
    if not site:
        return "not configured"

    site_id = str(site.get("id") or site.get("site_id") or "")
    if not site_id:
        logger.write("netlify matched site without id")
        return "not configured"

    deadline = time.monotonic() + wait_sec
    trigger_delay = int(os.environ.get("POP_AGENT_NETLIFY_TRIGGER_DELAY_SEC", DEFAULT_NETLIFY_TRIGGER_DELAY_SEC))
    trigger_after = time.monotonic() + trigger_delay
    triggered = False
    last = "not detected"

    while True:
        deploy = find_netlify_deploy(site_id, sha, token=token, logger=logger)
        if deploy:
            last = describe_netlify_deploy(deploy)
            if terminal_netlify_state(deploy):
                return last

        if not triggered and time.monotonic() >= trigger_after:
            try:
                trigger_netlify_build(site_id, token=token, logger=logger)
                triggered = True
                last = "build triggered"
            except Exception as exc:
                logger.write(f"netlify build trigger failed: {exc}")
                triggered = True
                last = f"trigger failed: {exc}"

        if time.monotonic() >= deadline:
            return last
        time.sleep(20)


def netlify_status(task: Task, sha: str, wait_sec: int, *, logger: RunLogger) -> str:
    try:
        api_status = netlify_status_via_api(task, sha, wait_sec, logger=logger)
    except Exception as exc:
        logger.write(f"netlify api status lookup failed: {exc}")
        api_status = None
    if api_status is not None:
        return api_status

    deadline = time.monotonic() + wait_sec
    last = "not detected"
    while True:
        detected = detect_netlify_once(task, sha, logger=logger)
        if detected != "not detected":
            return detected
        if time.monotonic() >= deadline:
            return last
        time.sleep(20)


def detect_netlify_once(task: Task, sha: str, *, logger: RunLogger) -> str:
    try:
        statuses = gh_json([f"/repos/{task.repo}/commits/{sha}/status"], logger=logger) or {}
        for status in statuses.get("statuses", []):
            text = " ".join(str(status.get(key, "")) for key in ["context", "description", "target_url"]).lower()
            if "netlify" in text:
                target = status.get("target_url") or ""
                state = status.get("state") or "unknown"
                return f"{state}: {target}" if target else state
    except Exception as exc:
        logger.write(f"netlify status lookup via statuses failed: {exc}")
    try:
        runs = gh_json(["-H", "Accept: application/vnd.github+json", f"/repos/{task.repo}/commits/{sha}/check-runs"], logger=logger) or {}
        for run in runs.get("check_runs", []):
            app = run.get("app") or {}
            text = " ".join(str(value) for value in [run.get("name"), app.get("name"), app.get("slug"), run.get("html_url")]).lower()
            if "netlify" in text:
                status = run.get("conclusion") or run.get("status") or "unknown"
                url = run.get("html_url") or ""
                return f"{status}: {url}" if url else status
    except Exception as exc:
        logger.write(f"netlify status lookup via check-runs failed: {exc}")
    return "not detected"


def post_issue_comment(task: Task, body: str, *, logger: RunLogger) -> str:
    payload = json.dumps({"body": body})
    result = run_command(
        ["gh", "api", "-X", "POST", f"/repos/{task.repo}/issues/{task.issue_number}/comments", "--input", "-"],
        input_text=payload,
        logger=logger,
    )
    data = json.loads(result.stdout)
    return data.get("html_url", "")


def success_comment(task: Task, sha: str, checks: list[CheckResult], netlify: str) -> str:
    short_sha = sha[:12]
    check_lines = "\n".join(f"- `{line}`" for line in check_summaries(checks))
    return textwrap.dedent(
        f"""
        <!-- pop-agent:source-comment-id={task.comment_id} -->
        Done. Processed this `#pop` comment and pushed the changes.

        - Repo: `{task.repo}`
        - Commit: `{short_sha}`
        - Netlify: {netlify}

        Checks:
        {check_lines}
        """
    ).strip()


def failure_comment(task: Task, reason: str, checks: list[CheckResult] | None = None) -> str:
    check_block = ""
    if checks is not None:
        check_lines = "\n".join(f"- `{line}`" for line in check_summaries(checks))
        check_block = f"\n\nChecks:\n{check_lines}"
    safe_reason = reason.strip()[:1500]
    return textwrap.dedent(
        f"""
        <!-- pop-agent:source-comment-id={task.comment_id} -->
        I could not safely complete this `#pop` task, so I did not push changes.

        Reason:
        {safe_reason}{check_block}
        """
    ).strip()


def no_changes_comment(task: Task, checks: list[CheckResult]) -> str:
    check_lines = "\n".join(f"- `{line}`" for line in check_summaries(checks))
    return textwrap.dedent(
        f"""
        <!-- pop-agent:source-comment-id={task.comment_id} -->
        I processed this `#pop` task, but there were no repository changes to push.

        Checks:
        {check_lines}
        """
    ).strip()


def process_task(task: Task, state: State, args: argparse.Namespace, *, logger: RunLogger) -> None:
    logger.section(f"task {task.repo} comment {task.comment_id}")
    workdir = safe_workdir(args.work_dir, task)
    checks: list[CheckResult] = []
    if args.dry_run:
        logger.write(f"dry-run pending task: {task.comment_url}")
        return
    try:
        clone_repo(task, workdir, logger=logger)
        run_codex(task, workdir, args.runtime_dir, args, logger=logger)
        cleanup_untracked_artifacts(workdir, logger=logger)
        if not git_has_changes(workdir, logger=logger):
            body = no_changes_comment(task, checks)
            response_url = post_issue_comment(task, body, logger=logger)
            state.mark(task.comment_id, base_record(task, "no_changes", response_url=response_url))
            return
        checks = run_discovered_checks(task, workdir, args.runtime_dir, args, logger=logger)
        cleanup_untracked_artifacts(workdir, logger=logger)
        if not checks_ok(checks):
            body = failure_comment(task, "A discovered check failed.", checks)
            response_url = post_issue_comment(task, body, logger=logger)
            state.mark(task.comment_id, base_record(task, "failed", response_url=response_url, error="check failed"))
            return
        sha = commit_and_push(task, workdir, logger=logger)
        netlify = netlify_status(task, sha, args.netlify_wait, logger=logger)
        body = success_comment(task, sha, checks, netlify)
        response_url = post_issue_comment(task, body, logger=logger)
        state.mark(task.comment_id, base_record(task, "success", sha=sha, response_url=response_url, netlify=netlify))
    except Exception as exc:
        logger.write(f"task failed: {exc!r}")
        try:
            response_url = post_issue_comment(task, failure_comment(task, str(exc), checks), logger=logger)
        except Exception as comment_exc:
            logger.write(f"failed to post failure comment: {comment_exc!r}")
            response_url = ""
        state.mark(task.comment_id, base_record(task, "failed", response_url=response_url, error=str(exc)[:1000]))


def base_record(task: Task, status: str, **extra: Any) -> dict[str, Any]:
    record = {
        "status": status,
        "repo": task.repo,
        "issue_number": task.issue_number,
        "comment_id": task.comment_id,
        "comment_url": task.comment_url,
        "comment_updated_at": task.comment_updated_at,
    }
    record.update(extra)
    return record


def run(args: argparse.Namespace) -> int:
    args.runtime_dir.mkdir(parents=True, exist_ok=True)
    args.work_dir.mkdir(parents=True, exist_ok=True)
    args.log_dir.mkdir(parents=True, exist_ok=True)
    log_path = args.log_dir / f"run-{dt.datetime.now(dt.UTC).strftime('%Y%m%dT%H%M%SZ')}.log"
    logger = RunLogger(log_path)
    logger.section("start")
    logger.write(f"dry_run={args.dry_run} owner={args.owner} max_tasks={args.max_tasks}")
    state = State(args.state_file)
    tasks = discover_tasks(args.owner, state, logger=logger)
    selected = tasks[: args.max_tasks]
    logger.write(f"pending={len(tasks)} selected={len(selected)}")
    for task in selected:
        process_task(task, state, args, logger=logger)
    logger.section("done")
    print(f"log: {log_path}")
    print(f"pending={len(tasks)} selected={len(selected)} dry_run={args.dry_run}")
    for task in selected:
        print(f"- {task.repo}#{task.issue_number} comment={task.comment_id} {task.comment_url}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pop-agent")
    subparsers = parser.add_subparsers(dest="command")
    run_parser = subparsers.add_parser("run", help="scan and process #pop comments")
    run_parser.add_argument("--owner", default=DEFAULT_OWNER)
    run_parser.add_argument("--max-tasks", type=int, default=DEFAULT_MAX_TASKS)
    run_parser.add_argument("--dry-run", action="store_true")
    run_parser.add_argument("--runtime-dir", type=Path, default=Path(os.environ.get("POP_AGENT_RUNTIME_DIR", "/var/lib/pop-agent")))
    run_parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    run_parser.add_argument("--work-dir", type=Path, default=DEFAULT_WORK_DIR)
    run_parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    run_parser.add_argument("--codex-cli", default=os.environ.get("POP_AGENT_CODEX_CLI", "codex"))
    run_parser.add_argument("--codex-model", default=os.environ.get("POP_AGENT_CODEX_MODEL", DEFAULT_CODEX_MODEL))
    run_parser.add_argument("--codex-effort", default=os.environ.get("POP_AGENT_CODEX_EFFORT", DEFAULT_CODEX_EFFORT))
    run_parser.add_argument(
        "--codex-sandbox",
        choices=["read-only", "workspace-write", "danger-full-access"],
        default=os.environ.get("POP_AGENT_CODEX_SANDBOX", DEFAULT_CODEX_SANDBOX),
    )
    run_parser.add_argument(
        "--codex-network-access",
        action=argparse.BooleanOptionalAction,
        default=env_flag("POP_AGENT_CODEX_NETWORK_ACCESS", DEFAULT_CODEX_NETWORK_ACCESS),
    )
    run_parser.add_argument("--codex-timeout", type=int, default=int(os.environ.get("POP_AGENT_CODEX_TIMEOUT_SEC", DEFAULT_CODEX_TIMEOUT_SEC)))
    run_parser.add_argument("--check-timeout", type=int, default=int(os.environ.get("POP_AGENT_CHECK_TIMEOUT_SEC", DEFAULT_CHECK_TIMEOUT_SEC)))
    run_parser.add_argument("--netlify-wait", type=int, default=int(os.environ.get("POP_AGENT_NETLIFY_WAIT_SEC", DEFAULT_NETLIFY_WAIT_SEC)))
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command in {None, "run"}:
        if args.command is None:
            args = parser.parse_args(["run", *(argv or [])])
        return run(args)
    parser.error(f"unknown command {args.command}")
    return 2
