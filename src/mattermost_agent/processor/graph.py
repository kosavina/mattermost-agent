"""The processor's flow for one job, as a LangGraph graph with a checkpoint per step.

    START -> decide --(ignore)----------------------------> END
                    --(remember)--> save_memory -----------> END
                    --(act)-------> save_memory -> approve -> END  (approve pauses for Kirill)

`decide` runs the agent; it is the only node that reads the burst, and the burst never enters the
state (the checkpoint file holds the memory and the draft, never a post). `save_memory` is our
code writing what the agent proposed. `approve` interrupts; `Command(resume=True|False)` from the
approve or reject command continues it. The job's status in the jobs table is written by the
loop that invokes the graph, from what invoke() returns; the nodes write files and the log.
"""

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import RetryPolicy, interrupt

from mattermost_agent.agents import AgentBackend, AgentError, AgentTask
from mattermost_agent.agents.prompt import build_prompt
from mattermost_agent.agents.schema import validate_answer
from mattermost_agent.logs import log_agent_error, log_agent_run, log_memory_saved
from mattermost_agent.processor import memory as memory_files
from mattermost_agent.processor.folders import conversation_dir, resolve
from mattermost_agent.processor.scenarios import Scenario
from mattermost_agent.processor.settings import Roots
from mattermost_agent.rules import RuleSet
from mattermost_agent.store import Store

MAX_ATTEMPTS = 3
UNKNOWN_SENDER = "unknown"


@dataclass
class State:
    """What one job's run has decided so far. No post text, ever."""

    job_id: int
    channel_id: str = ""
    backend: str | None = None
    decision: str | None = None
    memory: str | None = None
    draft: str | None = None
    approved: bool | None = None
    denials: int = 0


@dataclass
class ProcessorRuntime:
    """Everything the graph talks to, so that every test can run with a fake backend."""

    store: Store
    checkpointer: SqliteSaver
    rules: RuleSet
    scenarios: Mapping[str, Scenario]
    roots: Roots
    backends: Mapping[str, AgentBackend]
    default_backend: str
    timeout_s: float
    clock: Callable[[], float] = time.time
    retry_policy: RetryPolicy = field(
        default_factory=lambda: RetryPolicy(max_attempts=MAX_ATTEMPTS, retry_on=(AgentError,))
    )
    attempts: dict[int, int] = field(default_factory=dict)


class JobVanished(Exception):
    """The job's row is gone (the listener pruned it while it ran)."""


type Node = Callable[..., dict[str, object]]


def _decide(runtime: ProcessorRuntime) -> Node:
    def decide(state: State) -> dict[str, object]:
        job = runtime.store.job(state.job_id)
        if job is None:
            raise JobVanished
        resolved = resolve(
            scenario=job.scenario,
            rule=job.rule,
            channel_id=job.channel_id,
            rules=runtime.rules,
            scenarios=runtime.scenarios,
            roots=runtime.roots,
            default_backend=runtime.default_backend,
        )
        backend = runtime.backends[resolved.backend]
        task = AgentTask(
            scenario_dir=resolved.scenario_dir,
            read_dirs=resolved.read_dirs,
            instructions=resolved.instructions,
            prompt=build_prompt(
                runtime.store.posts(job.id),
                memory_files.read_memory(resolved.conversation_dir),
                job.sender or UNKNOWN_SENDER,
            ),
            timeout_s=runtime.timeout_s,
        )
        attempt = runtime.attempts.get(job.id, 0) + 1
        runtime.attempts[job.id] = attempt
        try:
            result = backend.run(task)
            answer = validate_answer(result.output)
        except AgentError as error:
            log_agent_error(
                job_id=job.id,
                backend=backend.name,
                reason=error.reason,
                exit_code=error.exit_code,
                attempt=attempt,
            )
            raise
        log_agent_run(
            job_id=job.id,
            backend=backend.name,
            scenario=job.scenario,
            decision=answer.decision,
            duration_s=result.duration_s,
            attempt=attempt,
            denials=result.denials,
        )
        return {
            "channel_id": job.channel_id,
            "backend": backend.name,
            "decision": answer.decision,
            "memory": answer.memory,
            "draft": answer.draft,
            "denials": result.denials,
        }

    return decide


def _save_memory(runtime: ProcessorRuntime) -> Node:
    def save_memory(state: State) -> dict[str, object]:
        if state.memory is None:
            return {}
        folder = conversation_dir(runtime.roots, state.channel_id)
        version = memory_files.save_memory(folder, state.memory)
        if version is not None:
            log_memory_saved(
                job_id=state.job_id,
                channel_id=state.channel_id,
                chars=len(state.memory),
                version=version,
            )
        return {}

    return save_memory


def approve(state: State) -> dict[str, object]:
    """Pause until the approve or reject command answers. Nothing is sent in this milestone."""
    return {"approved": bool(interrupt({"job": state.job_id}))}


def _after_decide(state: State) -> str:
    return END if state.decision == "ignore" else "save_memory"


def _after_memory(state: State) -> str:
    return "approve" if state.decision == "act" else END


def build_graph(runtime: ProcessorRuntime) -> CompiledStateGraph:
    """Compile the graph against the runtime's checkpointer."""
    builder = StateGraph(State)
    builder.add_node("decide", _decide(runtime), retry_policy=runtime.retry_policy)
    builder.add_node("save_memory", _save_memory(runtime))
    builder.add_node("approve", approve)
    builder.add_edge(START, "decide")
    builder.add_conditional_edges("decide", _after_decide, ["save_memory", END])
    builder.add_conditional_edges("save_memory", _after_memory, ["approve", END])
    builder.add_edge("approve", END)
    return builder.compile(checkpointer=runtime.checkpointer)


def thread(job_id: int) -> RunnableConfig:
    """The invoke config for a job: its id is the LangGraph thread id."""
    return {"configurable": {"thread_id": str(job_id)}}
