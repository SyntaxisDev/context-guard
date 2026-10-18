"""Bounded command-plan execution; provider models never receive execution tools.

Configured exact commands are trusted project programs, not an OS sandbox.
All other commands must satisfy the narrow Git grammar below.
"""
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from core import handoffs, processes
from core.state import file_lock, read_json, write_json

PLAN_SCHEMA = {'type': 'object', 'properties': {'commands': {'type': 'array',
    'maxItems': 8, 'items': {'type': 'array', 'minItems': 1,
                           'items': {'type': 'string'}}}},
    'required': ['commands'], 'additionalProperties': False}
SHELL_CHARS = re.compile(r'[\x00\n\r`$|;&<>]')
READ_GIT = {'status': {'--short', '--branch', '--porcelain', '--porcelain=v1'},
            'diff': {'--stat', '--name-only', '--cached', '--staged', '--check'},
            'log': {'--oneline', '--decorate', '-1', '-5', '-10'}}


def valid_path(value: str, workspace: Path) -> bool:
    if not value or value.startswith('-') or Path(value).is_absolute():
        return False
    try:
        (workspace / value).resolve().relative_to(workspace)
    except (ValueError, OSError):
        return False
    return True


def authorized_action(task: str, action: str) -> bool:
    if re.search(r"\b(not|never|don't|do not|no)\b", task, re.I):
        return False
    # Quoted descriptions cannot introduce another mutation clause.
    plain = re.sub(r"\"[^\"\n]*\"|'[^'\n]*'|`[^`\n]*`", '', task).strip()
    polite = r'(?:(?:please|can you|could you|would you)\s+)*'
    clauses = re.split(r'\b(?:and\s+then|and|then)\b', plain, flags=re.I)
    verbs = []
    for clause in clauses:
        match = re.match(r'^\s*' + polite + r'(stage|add|commit|push)\b', clause, re.I)
        if match is None:
            return False
        verbs.append(match[1].lower())
    accepted = {'add': {'stage', 'add', 'commit'}, 'commit': {'commit'}, 'push': {'push'}}[action]
    return bool(accepted.intersection(verbs))


def execution_environment() -> dict:
    redirects = {'GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_INDEX_FILE',
                 'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_NAMESPACE',
                 'GIT_CEILING_DIRECTORIES', 'GIT_DISCOVERY_ACROSS_FILESYSTEM', 'GIT_EXTERNAL_DIFF',
                 'GIT_DIFF_OPTS', 'GIT_EXEC_PATH'}
    env = {key: value for key, value in os.environ.items()
           if key not in redirects and not key.startswith('GIT_CONFIG')}
    env.update(CONTEXT_GUARD_INTERNAL='1', GIT_TERMINAL_PROMPT='0')
    return env


def git_allowed(args: list, task: str, workspace: Path) -> bool:
    if not args:
        return False
    action, rest = args[0], args[1:]
    if action in READ_GIT:
        return all(arg in READ_GIT[action] for arg in rest)
    if action == 'branch':
        return rest == ['--show-current']
    if action == 'add' and authorized_action(task, 'add'):
        paths = rest[1:] if rest and rest[0] == '--' else rest
        return bool(paths) and all(valid_path(path, workspace) for path in paths)
    if action == 'commit' and authorized_action(task, 'commit'):
        return len(rest) == 2 and rest[0] == '-m' and bool(rest[1]) and not rest[1].startswith('-')
    if action == 'push' and authorized_action(task, 'push'):
        return not rest  # Never permit URLs, force, transport helpers or arbitrary options.
    return False


def validate_command(argv: list, task: str, workspace: Path, commands: list) -> list:
    if not isinstance(argv, list) or not argv or len(argv) > 40:
        raise ValueError('invalid argv')
    if any(not isinstance(arg, str) or not arg or len(arg) > 4096 or SHELL_CHARS.search(arg) for arg in argv):
        raise ValueError('invalid command argument')
    if Path(argv[0]).name in {'sh', 'bash', 'zsh', 'fish', 'dash', 'env'}:
        raise ValueError('shell execution forbidden')
    if any(arg in {'-c', '-e', '--eval', '--command'} for arg in argv[1:]):
        raise ValueError('inline code forbidden')
    if argv[0] == 'git':
        if not git_allowed(argv[1:], task, workspace):
            raise ValueError('git command not authorized')
        executable = shutil.which('git')
    elif argv in commands:
        # Exact command templates are explicit user configuration, never model additions.
        executable = shutil.which(argv[0]) if '/' not in argv[0] else None
    else:
        raise ValueError('command not allowed')
    if not executable:
        raise ValueError('executable unavailable')
    if argv[0] == 'git':
        extras = ['--no-ext-diff', '--no-textconv'] if argv[1] == 'diff' else []
        return [executable, '--no-pager', '-c', 'core.fsmonitor=false', argv[1], *extras, *argv[2:]]
    return [executable, *argv[1:]]


def execute_plan(plan: dict, job: dict, config: dict, log: Path) -> dict:
    commands = plan.get('commands') if isinstance(plan, dict) else None
    limit = min(config.get('max_commands', 8), 8)
    if not isinstance(commands, list) or not commands or len(commands) > limit:
        raise ValueError('invalid command plan')
    workspace = Path(job['workspace']).resolve(strict=True)
    if not workspace.is_dir():
        raise ValueError('workspace unavailable')
    # Validate the entire plan before executing even its first command.
    approved = [validate_command(argv, job['prompt'], workspace, config.get('commands', [])) for argv in commands]
    results, started = [], time.monotonic()
    total = min(config.get('timeout_seconds', 300), 1800)
    environment = execution_environment()
    with log.open('a') as output:
        for original, argv in zip(commands, approved):
            remaining = total - (time.monotonic() - started)
            if remaining <= 0:
                return {'status': 'timeout', 'commands': results}
            output.write(json.dumps({'argv': original}) + '\n')
            output.flush()
            try:
                completed = processes.run(argv, cwd=workspace, env=environment, stdin=subprocess.DEVNULL,
                                           stdout=output, stderr=output, timeout=remaining, shell=False)
            except subprocess.TimeoutExpired:
                return {'status': 'timeout', 'commands': results}
            results.append({'argv': original, 'exit_code': completed.returncode})
            if completed.returncode:
                return {'status': 'failed', 'commands': results}
    return {'status': 'done', 'commands': results}


STAGED, SCHEDULED, CANCELLED = 'staged', 'scheduled', 'cancelled'
CONFIRM_SECONDS = 15
CONFIRM_POLL_SECONDS = 0.1
# Jobs this hook process staged; they run only once its blocking response is out (confirm_staged).
STAGED_JOBS: list[Path] = []


def confirm_staged() -> None:
    """Release this process's staged jobs to their workers; call only after the response is flushed."""
    while STAGED_JOBS:
        path = STAGED_JOBS.pop()
        try:
            with file_lock(path.with_suffix('.lock')):
                job = read_json(path)
                if job.get('status') == STAGED:
                    write_json(path, dict(job, status=SCHEDULED))
        except OSError:
            continue


class Runner:
    """Persist independent job reports and own detached worker scheduling.

    Scheduling is two-phase so a job never runs beside a prompt that went through anyway: the
    hook stages the job, and only after printing its blocking response marks it scheduled. A
    worker whose job stays staged for `confirm_seconds` cancels it.
    """
    def __init__(self, directory: Path, config: dict, report_dir: Path | None = None,
                 confirm_seconds: float = CONFIRM_SECONDS):
        self.directory, self.config = Path(directory) / 'runner', config
        self.report_dir, self.confirm_seconds = report_dir, confirm_seconds

    def schedule(self, prompt: str, workspace: str, command: list, env=None, metadata=None) -> str:
        if not self.config.get('enabled') or not workspace or not Path(workspace).is_dir():
            return ''
        self.directory.mkdir(parents=True, exist_ok=True)
        with file_lock(self.directory / 'schedule.lock'):
            stamp = read_json(self.directory / 'last.json')
            when = stamp.get('ts', 0)
            when = when if type(when) in (int, float) else 0
            if time.time() - when < self.config.get('cooldown_seconds', 60):
                return ''
            key = uuid.uuid4().hex
            path = self.directory / (key + '.json')
            write_json(path, {'status': STAGED, 'prompt': prompt, 'workspace': str(Path(workspace).resolve()),
                              'created_at': time.time(), 'log': str(path.with_suffix('.log')), **(metadata or {})})
            try:
                with path.with_suffix('.log').open('a') as output:
                    subprocess.Popen([*command, '--worker', str(path)], env=env, stdin=subprocess.DEVNULL,
                                     stdout=output, stderr=output, start_new_session=True)
            except OSError:
                path.unlink(missing_ok=True)
                return ''
            write_json(self.directory / 'last.json', {'ts': time.time()})
            STAGED_JOBS.append(path)
            return str(path)

    def claim(self, path: Path) -> dict | None:
        """Wait for the hook to confirm a staged job, then mark it running; cancel it on timeout."""
        expires = time.monotonic() + self.confirm_seconds
        while True:
            with file_lock(path.with_suffix('.lock')):
                job = read_json(path)
                status = job.get('status')
                if status == SCHEDULED:
                    job.update(status='running', started_at=time.time())
                    write_json(path, job)
                    return job
                if status != STAGED:
                    return None
                if time.monotonic() >= expires:
                    write_json(path, dict(job, status=CANCELLED, finished_at=time.time(),
                                          error='the hook response was not delivered'))
                    return None
            time.sleep(CONFIRM_POLL_SECONDS)

    def work(self, path: Path, complete_plan, model: str) -> int:
        path = Path(path).resolve()
        if path.parent != self.directory.resolve() or not re.fullmatch(r'[a-f0-9]{32}\.json', path.name):
            raise ValueError('invalid job path')
        job = self.claim(path)
        if job is None:
            return 1
        with path.with_suffix('.log').open('a') as output:
            output.write(json.dumps({'status': 'running'}) + '\n')
        try:
            for field in ('prompt', 'workspace'):
                if not isinstance(job.get(field), str) or not job[field] or '\0' in job[field]:
                    raise ValueError('invalid job ' + field)
                try:
                    job[field].encode('utf-8')
                except UnicodeEncodeError as exc:
                    raise ValueError('invalid job ' + field) from exc
            prompt = ('Return a JSON command plan for this explicitly requested mechanical task. '
                      'No tools, no scripts, no shell. Only narrow git status/diff/log/add/commit -m/push '
                      'or exact configured commands: ' + json.dumps(self.config.get('commands', [])) +
                      '. If insufficient context or unsupported task, return {"commands": []}. '
                      'Workspace: ' + job['workspace'] + '\nTask: ' + job['prompt'])
            plan = complete_plan(model, prompt, self.config.get('plan_timeout_seconds', 60))
            result = execute_plan(plan, job, self.config, path.with_suffix('.log'))
        except (ValueError, OSError) as exc:
            result = {'status': 'failed', 'error': str(exc)}
        job.update(result, finished_at=time.time())
        with file_lock(path.with_suffix('.lock')):
            latest = read_json(path)
            if latest.get('published'):
                job['published'] = True
            write_json(path, job)
            self.publish(path, job)
        return 0 if job['status'] == 'done' else 1

    def publish(self, path: Path, job: dict) -> None:
        if self.report_dir is None or job.get('published'):
            return
        report = dict(job, id='native-' + path.stem,
                      status='timed out' if job['status'] == 'timeout' else job['status'],
                      report=job.get('error') or json.dumps(job.get('commands', [])))
        handoffs.publish_pending(report, self.report_dir,
                                {'job': str(path), 'output': str(path.with_suffix('.log'))})
        job['published'] = True
        write_json(path, job)

    def reports(self, workspace=None) -> str:
        """Publish finished jobs to the handoff store's pending/ and return the pending context."""
        self.publish_finished(workspace)
        if self.report_dir is None:
            return ''
        return handoffs.pending_context([workspace] if workspace else [], self.report_dir)

    def publish_finished(self, workspace=None) -> None:
        if not self.directory.is_dir():
            return
        for path in sorted(self.directory.glob('*.json')):
            if path.name == 'last.json':
                continue
            with file_lock(path.with_suffix('.lock')):
                job = read_json(path)
                if workspace and job.get('workspace') != str(Path(workspace).resolve()):
                    continue
                status = job.get('status')
                created = job.get('started_at', job.get('created_at', 0))
                created = created if type(created) in (int, float) else 0
                if status in ('running', SCHEDULED) and time.time() - created > 3600:
                    job.update(status='failed', error='worker did not finish before its deadline')
                    status = 'failed'
                    write_json(path, job)
                if status in ('done', 'failed', 'timeout'):
                    self.publish(path, job)
