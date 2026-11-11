"""The `process` command: take ready jobs one at a time, run the graph, record the outcome.

One worker. A job is taken with a compare-and-set, run to its end or to its approval pause, and
its status written from what the graph returned: an interrupt means `waiting_approval`, a final
state `done`, an exception `failed` with a reason. A finished job's checkpoint thread is deleted
at once. At startup, jobs left in `running` by a crash are continued with invoke(None). Hourly,
drafts older than three days expire and checkpoint threads whose job is gone are deleted.

Only one processor may run per state folder: `processor.lock` there is held with flock for the
whole run, so that a second one cannot resume the jobs the first still owns or clear its tmp/.

Ctrl+C or SIGTERM kills the agent run in progress with its process group and stops the loop; the
job stays `running` and is continued at the next start.
"""

import fcntl
import os
import signal
import time
from collections.abc import Mapping
from pathlib import Path
from types import FrameType

from mattermost_agent.agents import AgentError, BackendCheckError
from mattermost_agent.agents.locked_run import clear_stale_tmp, kill_running
from mattermost_agent.logs import configure_logging, log_job_done, log_lifecycle
from mattermost_agent.processor.checkpoints import forget_thread, open_checkpointer, thread_ids
from mattermost_agent.processor.folders import JobSetupError
from mattermost_agent.processor.graph import JobVanished, ProcessorRuntime, build_graph, thread
from mattermost_agent.processor.scenarios import ScenariosError, load_scenarios, scenario_dir
from mattermost_agent.processor.settings import ProcessorSettings, load_processor_settings
from mattermost_agent.rules import RulesError, load_rules
from mattermost_agent.settings import SettingsError
from mattermost_agent.store import FILE_NAME, FailReason, Store, StoreError

HOUSEKEEPING_SECONDS = 3600.0
LOCK_FILE = "processor.lock"
# The lock's descriptor, kept open so that the lock lasts as long as the process.
_lock: int | None = None
AGENT_FAIL_REASONS: dict[str, FailReason] = {
    "exit": "agent_failed",
    "denied_startup": "agent_failed",
    "timeout": "agent_timeout",
    "invalid_json": "invalid_output",
    "invalid_output": "invalid_output",
}


def hold_lock(state_dir: Path) -> None:
    """Take the processor's lock for this process, or raise SettingsError if another holds it."""
    global _lock
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(state_dir / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        raise SettingsError("another processor is running on this STATE_DIR") from None
    _lock = descriptor


def release_lock() -> None:
    """Drop the lock (tests run more than one processor in a process; exit does it otherwise)."""
    global _lock
    if _lock is not None:
        os.close(_lock)
        _lock = None


class Stop(BaseException):
    """A signal asked the loop to stop. Not an Exception: nothing on the way may swallow it."""


def _stop(signum: int, frame: FrameType | None) -> None:
    kill_running()
    raise Stop


class Processor:
    def __init__(self, runtime: ProcessorRuntime, poll_s: float) -> None:
        self.runtime = runtime
        self.poll_s = poll_s
        self.graph = build_graph(runtime)
        self.last_housekeeping: float | None = None

    def run_job(self, job_id: int, resume: bool = False) -> None:
        """Invoke the graph for one job (from the start, or continuing after a crash) and record."""
        store, now = self.runtime.store, self.runtime.clock
        try:
            result = self.graph.invoke(None if resume else {"job_id": job_id}, thread(job_id))
        except AgentError as error:
            self._fail(job_id, AGENT_FAIL_REASONS[error.reason])
            return
        except JobSetupError as error:
            self._fail(job_id, error.reason)
            return
        except JobVanished:
            forget_thread(self.runtime.checkpointer, job_id)
            return
        except Exception as error:
            log_lifecycle("internal_error", error=error)
            self._fail(job_id, "agent_failed")
            return
        backend, decision = result.get("backend"), result.get("decision")
        if "__interrupt__" in result:
            store.set_waiting(
                job_id,
                backend=str(backend),
                decision=str(decision),
                draft=str(result.get("draft") or ""),
                now=now(),
            )
            log_job_done(job_id=job_id, status="waiting_approval", fail_reason=None)
            return
        store.set_done(job_id, now=now(), backend=backend, decision=decision)
        forget_thread(self.runtime.checkpointer, job_id)
        log_job_done(job_id=job_id, status="done", fail_reason=None)

    def _fail(self, job_id: int, reason: FailReason) -> None:
        self.runtime.store.set_failed(job_id, reason=reason, now=self.runtime.clock())
        forget_thread(self.runtime.checkpointer, job_id)
        log_job_done(job_id=job_id, status="failed", fail_reason=reason)

    def housekeeping(self) -> None:
        """Expire old drafts and delete checkpoint threads whose job the listener pruned."""
        now = self.runtime.clock()
        if (
            self.last_housekeeping is not None
            and now - self.last_housekeeping < HOUSEKEEPING_SECONDS
        ):
            return
        self.last_housekeeping = now
        expired = self.runtime.store.expire_drafts(now)
        for job in expired:
            forget_thread(self.runtime.checkpointer, job.id)
            log_job_done(job_id=job.id, status="failed", fail_reason="expired")
        if expired:
            log_lifecycle("drafts_expired", count=len(expired))
        orphans = {
            int(thread_id)
            for thread_id in thread_ids(self.runtime.checkpointer)
            if thread_id.isascii() and thread_id.isdigit()
        } - self.runtime.store.job_ids()
        for job_id in orphans:
            forget_thread(self.runtime.checkpointer, job_id)
        if orphans:
            log_lifecycle("threads_pruned", count=len(orphans))

    def resume_running(self) -> None:
        """Continue every job a crash left in `running`: a done step is not run again."""
        for job in self.runtime.store.jobs_in("running"):
            self.run_job(job.id, resume=True)

    def run_forever(self) -> None:
        self.resume_running()
        while True:
            self.housekeeping()
            job = self.runtime.store.take_ready(self.runtime.clock())
            if job is None:
                time.sleep(self.poll_s)
                continue
            self.run_job(job.id)


def start(settings: ProcessorSettings) -> Processor:
    """Load everything the processor needs and check the backends, or raise the first error."""
    rules = load_rules(settings.rules_file)
    scenarios = load_scenarios(settings.scenarios_file)
    for scenario in scenarios.values():
        scenario_dir(settings.roots, scenario)
    backends = settings.backends()
    used = {settings.backend, *(s.backend for s in scenarios.values() if s.backend)}
    for name in sorted(used):
        backends[name].check()
    store = Store(settings.state_dir / FILE_NAME)
    checkpointer = open_checkpointer(settings.state_dir)
    runtime = ProcessorRuntime(
        store=store,
        checkpointer=checkpointer,
        rules=rules,
        scenarios=scenarios,
        roots=settings.roots,
        backends=backends,
        default_backend=settings.backend,
        timeout_s=settings.timeout_s,
    )
    return Processor(runtime, settings.poll_s)


def main(env: Mapping[str, str]) -> int:
    """Run the processor until a signal stops it. Return the exit code."""
    try:
        settings = load_processor_settings(env)
        configure_logging(settings.log_level)
        os.umask(0o077)
        hold_lock(settings.state_dir)
        clear_stale_tmp(settings.state_dir)
        processor = start(settings)
    except (SettingsError, RulesError, ScenariosError, BackendCheckError, StoreError) as error:
        raise SystemExit(f"mattermost-agent: {error}") from None
    for number in (signal.SIGINT, signal.SIGTERM):
        signal.signal(number, _stop)
    log_lifecycle("startup")
    try:
        processor.run_forever()
    except Stop:
        log_lifecycle("shutdown", reason="signal")
    except StoreError as error:
        log_lifecycle("fatal", reason="store_error", error=error.__cause__)
        raise SystemExit(f"mattermost-agent: {error}") from None
    except Exception as error:
        log_lifecycle("fatal", error=error)
        raise SystemExit(f"mattermost-agent: stopped by {type(error).__name__}") from None
    finally:
        for number in (signal.SIGINT, signal.SIGTERM):
            signal.signal(number, signal.SIG_DFL)
    log_lifecycle("stopped")
    return 0
