"""Observe SDK model and tool calls and stop after a saved answer."""

import json
from time import monotonic

from openjiuwen.core.foundation.llm import ToolMessage
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    AgentRail,
    ModelCallInputs,
    ToolCallInputs,
)

from contest_harness.agent.generation import valid_arguments
from contest_harness.json_codec import strict_json_loads
from contest_harness.telemetry.trace import describe, error_fields
from contest_harness.workspace.paths import safe_path
from contest_harness.workspace.tools import tool_status


class TaskRail(AgentRail):

    def __init__(self, workspace, progress, trace, state, generation, allowed):
        self.workspace = workspace
        self.progress = progress
        self.trace = trace
        self.state = state
        self.generation = generation
        self.allowed = allowed
        self.single_tool_turn = False

    def valid_tool_input(self, inputs):
        try:
            args = strict_json_loads(inputs.tool_args) if isinstance(inputs.tool_args, str) else inputs.tool_args
        except (ValueError, TypeError, RecursionError):
            return False
        return valid_arguments(self.allowed.get(inputs.tool_name), args)

    async def after_model_call(self, ctx: AgentCallbackContext):
        if isinstance(ctx.inputs, ModelCallInputs) and ctx.inputs.response is not None:
            self.single_tool_turn = len(ctx.inputs.response.tool_calls or ()) == 1
            for call in ctx.inputs.response.tool_calls or ():
                if call.name not in self.allowed:
                    self.trace.tool_event(
                        "tool.unavailable", tool=call.name, tool_call_id=call.id, reason="schema_not_offered",
                    )
            if self.state is not None:
                self.state.observe_response(ctx.inputs.response)

    async def before_tool_call(self, ctx: AgentCallbackContext):
        inputs = ctx.inputs
        if not isinstance(inputs, ToolCallInputs):
            return
        if self.trace.enabled:
            self.trace.tool_event(
                "tool.started",
                tool=inputs.tool_name,
                tool_call_id=getattr(inputs.tool_call, "id", None),
                arguments=describe(inputs.tool_args),
                after_answer=self.progress.submitted,
            )
        if not self.valid_tool_input(inputs):
            # The SDK may repair JSON before dispatch. Pass invalid arguments that
            # cannot be repaired into a valid call or reach a side effect.
            inputs.tool_args = "{}"

    async def after_tool_call(self, ctx: AgentCallbackContext):
        inputs = ctx.inputs
        if not isinstance(inputs, ToolCallInputs):
            return
        if ctx.exception is not None:
            error = error_fields(ctx.exception)
            if error["error_type"] == "ToolUnavailable":
                self.trace.tool_event(
                    "tool.unavailable", tool=inputs.tool_name,
                    tool_call_id=getattr(inputs.tool_call, "id", None), reason="task_type",
                )
            status = "invalid" if (
                error["error_type"] in {"InvalidToolCall", "ToolUnavailable"} or not self.valid_tool_input(inputs)
            ) else "error"
            if self.state is not None:
                message = getattr(ctx.exception, "tool_message", None) or ToolMessage(
                    content=f"Ability execution error: {ctx.exception}",
                    tool_call_id=inputs.tool_call.id,
                )
                self.state.observe_tool(inputs.tool_name, inputs.tool_args, message, status, None, executed=False)
                inputs.tool_msg = message
            self.record_tool(ctx, status, None, **error)
            return
        output = inputs.tool_msg.content if inputs.tool_msg is not None else None
        status = tool_status(inputs.tool_name, output) if isinstance(output, str) else "ok"
        if self.state is not None and inputs.tool_msg is not None:
            path = None
            if output not in {"Answer already saved.", "Answer saved."}:
                args = inputs.tool_args
                args = json.loads(args) if isinstance(args, str) else args
                if isinstance(args, dict) and "path" in args:
                    path = safe_path(self.workspace, args["path"]).relative_to(self.workspace).as_posix()
            self.state.observe_tool(
                inputs.tool_name, inputs.tool_args, inputs.tool_msg, status, path,
                executed=output != "Answer already saved.",
            )
        self.record_tool(ctx, status, output)
        if self.progress.submitted:
            ctx.request_force_finish(
                {"output": "Answer saved.", "result_type": "answer"}
            )

    def record_tool(self, ctx, status, output, **error):
        inputs = ctx.inputs
        if self.generation is not None:
            self.generation.observe_tool(inputs.tool_name, status)
        if self.trace.enabled:
            self.trace.tool_event(
                "tool.finished",
                tool=inputs.tool_name,
                tool_call_id=getattr(inputs.tool_call, "id", None),
                status=status,
                status_source="harness",
                duration_ms=round((monotonic() - ctx.invoke_start_time) * 1000, 3),
                result=describe(output) if output is not None else None,
                **error,
            )
