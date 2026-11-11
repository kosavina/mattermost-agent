"""The graph and the loop with a fake backend: every outcome, retries, crashes, approval."""

import io
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from helpers import captured_logs, make_post, squeezed
from langgraph.types import RetryPolicy

from mattermost_agent.agents import AgentError, AgentResult, AgentTask
from mattermost_agent.processor.checkpoints import (
    FILE_NAME as CHECKPOINTS,
)
from mattermost_agent.processor.checkpoints import (
    open_checkpointer,
    thread_ids,
)
from mattermost_agent.processor.cli import decide_draft, format_drafts
from mattermost_agent.processor.graph import ProcessorRuntime
from mattermost_agent.processor.loop import Processor, Stop
from mattermost_agent.processor.memory import memory_versions, read_memory
from mattermost_agent.processor.scenarios import Scenario
from mattermost_agent.processor.settings import Roots
from mattermost_agent.rules import Bursts, Rule, RuleSet
from mattermost_agent.store import (
    DRAFT_RETENTION_SECONDS,
    FILE_NAME,
    Job,
    Store,
    read_drafts,
    read_jobs,
)

NOW = 1_700_000_000.0
CHANNEL = "k3d7w1q9m2n4p6r8t0v2x4z6a8"
POST_CANARY = "post-canary-ZEBRAQUARTZ"
MEMORY_CANARY = "memory-canary-OCTOPUSPRIME"
DRAFT_CANARY = "draft-canary-GIRAFFEMOON"
RULE = Rule("dm", "intake", True, frozenset(), frozenset())
BURSTS = Bursts(90, 600, 1)


class Crash(BaseException):
    """Stands in for a kill while the agent runs: not an Exception, so nothing catches it."""


@dataclass
class FakeBackend:
    answers: list[object]
    name: str = "fake"
    tasks: list[AgentTask] = field(default_factory=list)

    def command(self, task: AgentTask, run_dir: Path) -> tuple[list[str], dict[str, str]]:
        return (["fake"], {})

    def run(self, task: AgentTask) -> AgentResult:
        self.tasks.append(task)
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        assert isinstance(answer, dict)
        return AgentResult(output=answer, backend=self.name, duration_s=0.5, denials=1)

    def check(self) -> None:
        pass


def answer(decision: str, memory: str | None = None, draft: str | None = None) -> dict[str, object]:
    return {"decision": decision, "memory": memory, "draft": draft}


@dataclass
class World:
    root: Path
    store: Store
    runtime: ProcessorRuntime
    processor: Processor
    fake: FakeBackend
    env: dict[str, str]
    now: float = NOW

    def clock(self) -> float:
        return self.now

    def job(self, message: str = POST_CANARY, post_id: str = "a") -> Job:
        post = make_post(id=post_id, channel_id=CHANNEL, message=message)
        outcome = self.store.claim(
            post,
            kind="dm",
            rule=RULE,
            place="@ivan",
            sender="ivan",
            live=True,
            now=self.now,
            cap=10,
            bursts=BURSTS,
        )
        assert outcome.job is not None
        return outcome.job

    def jobs(self) -> list[tuple[int, str, str | None, str | None, str | None]]:
        return [
            (j.id, j.status, j.decision, j.fail_reason, j.approval)
            for j in read_jobs(self.store.path)
        ]

    def memory(self) -> str | None:
        return read_memory(self.runtime.roots.conversations / CHANNEL)

    def checkpoint_bytes(self) -> bytes:
        folder = self.store.path.parent
        return b"".join(f.read_bytes() for f in folder.iterdir() if f.name.startswith(CHECKPOINTS))


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    root = tmp_path / "agent"
    for name in ("scenarios/intake", "projects/sales", "conversations", "state"):
        (root / name).mkdir(parents=True)
    (root / "scenarios" / "intake" / "AGENTS.md").write_text("# Intake\nReply in English.\n")
    (root / "scenarios" / "scenarios.yaml").write_text("scenarios: {intake: {folder: intake}}\n")
    (root / "rules.yaml").write_text(
        "rules: [{name: dm, dm: true, scenario: intake}]\n"
        "limits: {jobs_per_hour: 20}\n"
        "bursts: {quiet_seconds: 90, max_wait_seconds: 600, max_posts: 1}\n"
    )
    store = Store(root / "state" / FILE_NAME)
    checkpointer = open_checkpointer(root / "state")
    fake = FakeBackend([])
    roots = Roots(root / "scenarios", root / "projects", root / "conversations")
    runtime = ProcessorRuntime(
        store=store,
        checkpointer=checkpointer,
        rules=RuleSet((RULE,), 20, BURSTS),
        scenarios={"intake": Scenario("intake", "intake", None)},
        roots=roots,
        backends={"fake": fake},
        default_backend="fake",
        timeout_s=5.0,
        retry_policy=RetryPolicy(
            max_attempts=3, retry_on=(AgentError,), initial_interval=0, jitter=False
        ),
    )
    env = {
        "STATE_DIR": str(root / "state"),
        "AGENT_ROOT": str(root),
        "RULES_FILE": str(root / "rules.yaml"),
        "HOME": "/Users/example",
        "USER": "example",
    }
    world = World(root, store, runtime, Processor(runtime, poll_s=0.01), fake, env)
    runtime.clock = world.clock
    yield world
    checkpointer.conn.close()


def test_ignore(world: World) -> None:
    job = world.job()
    world.fake.answers = [answer("ignore")]
    with captured_logs() as log:
        world.processor.run_job(job.id)
    assert world.jobs() == [(job.id, "done", "ignore", None, None)]
    assert world.memory() is None
    assert thread_ids(world.runtime.checkpointer) == set()
    lines = squeezed(log.getvalue())
    assert f'event=agent_run job={job.id} backend=fake scenario="intake" decision=ignore' in lines
    assert "attempt=1 denials=1" in lines
    assert f"event=job_done job={job.id} status=done fail_reason=null" in lines


def test_the_agent_sees_the_right_folders_and_prompt(world: World) -> None:
    job = world.job()
    world.fake.answers = [answer("ignore")]
    world.processor.run_job(job.id)
    task = world.fake.tasks[0]
    assert task.scenario_dir == world.runtime.roots.scenarios / "intake"
    assert task.read_dirs == (world.runtime.roots.conversations / CHANNEL,)
    assert task.instructions == "# Intake\nReply in English.\n"
    assert POST_CANARY in task.prompt and "ivan: " in task.prompt
    assert task.timeout_s == 5.0


def test_remember(world: World) -> None:
    job = world.job()
    world.fake.answers = [answer("remember", memory=MEMORY_CANARY)]
    with captured_logs() as log:
        world.processor.run_job(job.id)
    assert world.jobs() == [(job.id, "done", "remember", None, None)]
    assert world.memory() == MEMORY_CANARY
    folder = world.runtime.roots.conversations / CHANNEL
    assert (folder.stat().st_mode & 0o777) == 0o700
    assert ((folder / "memory.md").stat().st_mode & 0o777) == 0o600
    assert (
        f'event=memory_saved job={job.id} channel="{CHANNEL}" chars={len(MEMORY_CANARY)} version=1'
        in squeezed(log.getvalue())
    )
    assert thread_ids(world.runtime.checkpointer) == set()


def test_the_next_run_sees_the_memory_and_the_same_text_adds_no_version(world: World) -> None:
    first = world.job(post_id="a")
    world.fake.answers = [answer("remember", memory=MEMORY_CANARY)]
    world.processor.run_job(first.id)
    second = world.job(post_id="b")
    world.fake.answers = [answer("remember", memory=MEMORY_CANARY)]
    with captured_logs() as log:
        world.processor.run_job(second.id)
    assert MEMORY_CANARY in world.fake.tasks[1].prompt
    assert memory_versions(world.runtime.roots.conversations / CHANNEL) == 0
    assert "memory_saved" not in log.getvalue()
    third = world.job(post_id="c")
    world.fake.answers = [answer("remember", memory="newer")]
    world.processor.run_job(third.id)
    assert memory_versions(world.runtime.roots.conversations / CHANNEL) == 1


def test_act_waits_for_approval(world: World) -> None:
    job = world.job()
    world.fake.answers = [answer("act", memory=MEMORY_CANARY, draft=DRAFT_CANARY)]
    with captured_logs() as log:
        world.processor.run_job(job.id)
    assert world.jobs() == [(job.id, "waiting_approval", "act", None, None)]
    assert world.memory() == MEMORY_CANARY
    drafts = read_drafts(world.store.path)
    assert [d.text for d in drafts] == [DRAFT_CANARY]
    assert thread_ids(world.runtime.checkpointer) == {str(job.id)}
    assert f"event=job_done job={job.id} status=waiting_approval" in squeezed(log.getvalue())
    listing = format_drafts(drafts, now=NOW + 3700)
    assert f"--- job {job.id} place=@ivan backend=fake waiting=1h 01m" in squeezed(listing)
    assert DRAFT_CANARY in listing


@pytest.mark.parametrize("approved", [True, False])
def test_approve_and_reject_finish_the_job(
    world: World, approved: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    job = world.job()
    world.fake.answers = [answer("act", draft=DRAFT_CANARY)]
    world.processor.run_job(job.id)
    assert decide_draft(world.env, job.id, approved=approved) == 0
    expected = "approved" if approved else "rejected"
    assert world.jobs() == [(job.id, "done", "act", None, expected)]
    assert thread_ids(world.runtime.checkpointer) == set()
    assert read_drafts(world.store.path) == []
    assert expected in capsys.readouterr().out
    assert len(world.fake.tasks) == 1  # the agent did not run again
    with pytest.raises(SystemExit, match="not waiting for approval"):
        decide_draft(world.env, job.id, approved=True)


def test_approve_without_a_checkpoint_never_runs_the_agent(world: World) -> None:
    """A waiting row whose thread is gone must not make the approve command start the graph."""
    job = world.job()
    world.fake.answers = [answer("act", draft=DRAFT_CANARY), answer("ignore")]
    world.processor.run_job(job.id)
    world.runtime.checkpointer.delete_thread(str(job.id))
    with pytest.raises(SystemExit, match="could not be resumed"):
        decide_draft(world.env, job.id, approved=True)
    assert world.jobs() == [(job.id, "waiting_approval", "act", None, None)]
    assert len(world.fake.tasks) == 1


def test_retries_then_success(world: World) -> None:
    job = world.job()
    world.fake.answers = [AgentError("exit", 3), AgentError("timeout"), answer("ignore")]
    with captured_logs() as log:
        world.processor.run_job(job.id)
    assert world.jobs() == [(job.id, "done", "ignore", None, None)]
    lines = squeezed(log.getvalue())
    assert f"event=agent_error job={job.id} backend=fake reason=exit exit_code=3 attempt=1" in lines
    assert "reason=timeout exit_code=null attempt=2" in lines
    assert "decision=ignore" in lines and "attempt=3" in lines


@pytest.mark.parametrize(
    ("errors", "reason"),
    [
        ([AgentError("timeout")] * 3, "agent_timeout"),
        ([AgentError("exit", 1)] * 3, "agent_failed"),
        ([AgentError("denied_startup", 127)] * 3, "agent_failed"),
        ([AgentError("invalid_json")] * 3, "invalid_output"),
        ([answer("act", memory=MEMORY_CANARY)] * 3, "invalid_output"),  # act without a draft
        ([answer("remember", memory="x" * 4001)] * 3, "invalid_output"),
    ],
)
def test_three_failures_fail_the_job(world: World, errors: list[object], reason: str) -> None:
    job = world.job()
    world.fake.answers = list(errors)
    with captured_logs() as log:
        world.processor.run_job(job.id)
    assert world.jobs() == [(job.id, "failed", None, reason, None)]
    assert len(world.fake.tasks) == 3
    assert thread_ids(world.runtime.checkpointer) == set()
    assert f"status=failed fail_reason={reason}" in squeezed(log.getvalue())
    assert MEMORY_CANARY not in log.getvalue()


def test_a_setup_problem_fails_at_once_without_running_the_agent(world: World) -> None:
    job = world.job()
    world.runtime.scenarios = {}
    world.processor.run_job(job.id)
    assert world.jobs() == [(job.id, "failed", None, "unknown_scenario", None)]
    assert world.fake.tasks == []
    world.runtime.scenarios = {"intake": Scenario("intake", "intake", None)}
    (world.root / "scenarios" / "intake" / "CLAUDE.md").write_text("x")
    other = world.job(post_id="b")
    world.processor.run_job(other.id)
    assert world.jobs()[0] == (other.id, "failed", None, "bad_folder", None)


def test_a_crash_during_decide_reruns_it_once_on_the_next_start(world: World) -> None:
    job = world.job()
    world.store.take_ready(NOW)
    world.fake.answers = [Crash(), answer("ignore")]
    with pytest.raises(Crash):
        world.processor.run_job(job.id)
    assert world.jobs() == [(job.id, "running", None, None, None)]
    fresh = Processor(world.runtime, poll_s=0.01)
    fresh.resume_running()
    assert world.jobs() == [(job.id, "done", "ignore", None, None)]
    assert len(world.fake.tasks) == 2


def test_a_stop_signal_during_a_run_leaves_the_job_running(world: World) -> None:
    """The signal handler raises Stop inside the agent run; the loop must end, not fail the job."""
    job = world.job()
    world.store.take_ready(NOW)
    world.fake.answers = [Stop()]
    with pytest.raises(Stop), captured_logs() as log:
        world.processor.run_job(job.id)
    assert world.jobs() == [(job.id, "running", None, None, None)]
    assert "internal_error" not in log.getvalue()


def test_a_crash_after_the_interrupt_resumes_to_waiting(world: World) -> None:
    job = world.job()
    world.fake.answers = [answer("act", draft=DRAFT_CANARY)]
    world.processor.run_job(job.id)
    # The loop died before it could write waiting_approval: the row still says running.
    world.store.take_waiting(job.id, NOW)
    Processor(world.runtime, poll_s=0.01).resume_running()
    assert world.jobs() == [(job.id, "waiting_approval", "act", None, None)]
    assert len(world.fake.tasks) == 1


def test_post_text_never_enters_the_checkpoint_file(world: World) -> None:
    job = world.job()
    world.fake.answers = [answer("act", memory=MEMORY_CANARY, draft=DRAFT_CANARY)]
    world.processor.run_job(job.id)
    data = world.checkpoint_bytes()
    assert POST_CANARY.encode() not in data
    assert DRAFT_CANARY.encode() in data  # the draft is allowed at rest, and leaves with the job
    decide_draft(world.env, job.id, approved=True)
    assert DRAFT_CANARY.encode() not in world.checkpoint_bytes()


def test_no_text_reaches_the_log(world: World) -> None:
    job = world.job()
    world.fake.answers = [
        answer("act", memory=MEMORY_CANARY),  # invalid: retried, pydantic quotes the input
        answer("act", memory=MEMORY_CANARY, draft=DRAFT_CANARY),
    ]
    with captured_logs("DEBUG") as log:
        world.processor.run_job(job.id)
        decide_draft(world.env, job.id, approved=True)
    for canary in (POST_CANARY, MEMORY_CANARY, DRAFT_CANARY, "ivan", "Intake"):
        assert canary not in log.getvalue()


def test_drafts_expire_after_three_days(world: World) -> None:
    job = world.job()
    world.fake.answers = [answer("act", draft=DRAFT_CANARY)]
    world.processor.run_job(job.id)
    world.now = NOW + DRAFT_RETENTION_SECONDS + 1
    with captured_logs() as log:
        world.processor.housekeeping()
    assert world.jobs() == [(job.id, "failed", "act", "expired", None)]
    assert thread_ids(world.runtime.checkpointer) == set()
    lines = squeezed(log.getvalue())
    assert f"event=job_done job={job.id} status=failed fail_reason=expired" in lines
    assert "event=drafts_expired" in lines and "count=1" in lines


def test_housekeeping_runs_hourly_and_prunes_orphan_threads(world: World) -> None:
    job = world.job()
    world.fake.answers = [answer("act", draft=DRAFT_CANARY)]
    world.processor.run_job(job.id)
    world.processor.housekeeping()
    connection = sqlite3.connect(world.store.path)
    connection.execute("DELETE FROM job_posts")
    connection.execute("DELETE FROM jobs")
    connection.commit()
    connection.close()
    world.processor.housekeeping()  # too soon: nothing happens
    assert thread_ids(world.runtime.checkpointer) == {str(job.id)}
    world.now = NOW + 3601
    with captured_logs() as log:
        world.processor.housekeeping()
    assert thread_ids(world.runtime.checkpointer) == set()
    assert "event=threads_pruned" in squeezed(log.getvalue())


def test_a_job_pruned_while_running_leaves_no_thread(world: World) -> None:
    job = world.job()
    world.store.take_ready(NOW)
    connection = sqlite3.connect(world.store.path)
    connection.execute("DELETE FROM job_posts")
    connection.execute("DELETE FROM jobs")
    connection.commit()
    connection.close()
    world.processor.run_job(job.id, resume=True)
    assert thread_ids(world.runtime.checkpointer) == set()
    assert world.fake.tasks == []


def test_run_forever_takes_jobs_oldest_first(world: World) -> None:
    first = world.job(post_id="a")
    world.now += 1
    second = world.job(post_id="b")
    world.now += 1
    third = world.job(post_id="c")
    world.fake.answers = [answer("ignore"), answer("remember", memory="m"), Crash()]
    with pytest.raises(Crash):
        world.processor.run_forever()  # the third job's run is "killed", which ends the loop
    state = {j.id: (j.status, j.decision) for j in read_jobs(world.store.path)}
    assert state == {
        first.id: ("done", "ignore"),
        second.id: ("done", "remember"),
        third.id: ("running", None),
    }


def test_log_stream_is_text_free_even_with_a_drafts_listing(world: World) -> None:
    job = world.job()
    world.fake.answers = [answer("act", draft=DRAFT_CANARY)]
    with captured_logs() as log:
        world.processor.run_job(job.id)
        format_drafts(read_drafts(world.store.path), now=NOW)
    assert DRAFT_CANARY not in log.getvalue()
    assert isinstance(log, io.StringIO)
