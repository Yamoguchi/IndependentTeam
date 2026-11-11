"""Bind one openJiuwen ReAct session to the task context and tools."""

import asyncio
from contextlib import AsyncExitStack, nullcontext
from functools import partial
from pathlib import Path

from contest_harness.agent.generation import GenerationPolicy, valid_arguments
from contest_harness.planning.feasibility import SIGNALS
from contest_harness.settings import COMPACT_MODES
from contest_harness.workspace.output import ToolOutputs
from contest_harness.workspace.paths import InvalidToolCall, safe_path
from contest_harness.workspace.tools import use_tool


async def run_attempt(
    model, task_dir, workspace, task, current, trace, mode, generation_limits, direct_publish, coding,
    answer_format, reference, definitions, system, query, progress, publish, calls_before, step_limit, capture_handoff,
    recognize_signal=False,
):
    from openjiuwen.core.foundation.tool import LocalFunction, ToolCard
    from openjiuwen.core.single_agent import AgentCard, ReActAgent, ReActAgentConfig

    from contest_harness.agent.rail import TaskRail

    compact = mode in COMPACT_MODES
    allowed = {item["name"]: set(item["input_params"]["required"]) for item in definitions}
    generation = None
    if mode == "B+C+D":
        from contest_harness.agent.model_profiles import PROFILES

        cap = PROFILES[model.model_config.model_name][0]
        limits = {name: min(cap, limit) for name, limit in generation_limits.items()}
        generation = GenerationPolicy(
            task["task_type"], allowed, limits, terminal_signals=SIGNALS if recognize_signal else (),
        )
    state = None
    context_config = {}
    if mode == "B" or compact or capture_handoff:
        from openjiuwen.core.context_engine import ContextEngineConfig

        from contest_harness.agent.context import HISTORY_MESSAGES, TaskContext

        state = TaskContext(query, coding is not None, trace)
        progress.context = state
        if mode == "B" or compact:
            context_config["context_engine_config"] = ContextEngineConfig(max_context_message_num=HISTORY_MESSAGES)
    outputs = None

    async def execute(name, /, **arguments):
        if progress.submitted:
            return "Answer already saved."
        if not valid_arguments(allowed.get(name), arguments):
            raise InvalidToolCall("Use an available tool with its declared string arguments.")
        if name == "submit_answer":
            return await publish(arguments["answer"], "submit_answer")
        if name == "write_file" and coding and workspace != task_dir and Path(arguments["path"]) == Path("answer.txt"):
            # The payload of this call is the handoff. A file left in workspace is never read.
            return await publish(arguments["content"], "workspace")
        if name == "output_details" and outputs is not None:
            return outputs.details(arguments["id"], arguments["range"])
        if name in {"run_python", "run_python_file"}:
            output = await asyncio.to_thread(use_tool, name, arguments, workspace, coding, compact=compact)
        else:
            output = use_tool(name, arguments, workspace, coding, compact=compact)
        if direct_publish and rail.single_tool_turn:
            candidate = None
            if (coding and name == "run_python_file" and answer_format.kind == "literal"
                    and safe_path(workspace, arguments["path"]).relative_to(workspace) in coding.tests
                    and output.startswith("Exit code 0\n")):
                candidate = answer_format.literal
            elif reference and (
                name == "run_python" and output == f"Exit code 0\n{reference[0]}\n"
                or name == "read_file" and reference[1] == "labelled_field"
            ):
                candidate = reference[0]
            if candidate is not None:
                await publish(candidate, "direct")
        if outputs is not None and name != "write_file":
            return outputs.compact(name, output)
        return output

    agent = ReActAgent(AgentCard(name="contest_agent"))
    agent.configure(
        ReActAgentConfig(
            model_name=model.model_config.model_name,
            prompt_template=[{"role": "system", "content": system}],
            max_iterations=step_limit,
            parallel_tool_calls=False,
            **context_config,
        )
    )
    if context_config:
        agent.context_engine.register_window_mutator(state.select_window)
    agent.set_llm(model)
    rail = TaskRail(workspace, progress, trace, state, generation, allowed)
    try:
        async with AsyncExitStack() as cleanup:
            cleanup.push_async_callback(agent.clear_session, agent.card.id)
            await agent.register_rail(rail)
            cleanup.push_async_callback(agent.unregister_rail, rail)
            cleanup.callback(agent.ability_manager.teardown_tools)
            if compact:
                outputs = ToolOutputs(trace)
                cleanup.callback(outputs.close)
            for definition in definitions:
                card = ToolCard(**definition, parallel_safe=False)
                agent.ability_manager.add_ability(
                    card, LocalFunction(card, partial(execute, card.name))
                )
            policy = (
                model.generation(generation, allow_terminal_signal=recognize_signal)
                if generation is not None or recognize_signal else nullcontext()
            )
            with policy:
                result = await agent.invoke(
                    {"query": query, "conversation_id": agent.card.id}
                )
    except BaseException:
        if state is not None and not progress.submitted:
            state.status = "interrupted"
        raise

    if progress.submitted:
        return "submitted"
    if recognize_signal and result.get("result_type") == "answer":
        output = result.get("output")
        if isinstance(output, str) and output.strip() in SIGNALS:
            progress.signal = output.strip()
            return "no_answer"
    status = (
        "generation_failed" if generation is not None and generation.failed else
        "step_limit" if current.usage.calls - calls_before - (generation.recoveries if generation else 0) >= step_limit else
        "no_answer"
    )
    if state is not None:
        state.status = status
    return status
