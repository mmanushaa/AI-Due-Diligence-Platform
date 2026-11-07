"""What every CrewAI agent stage shares: the model adapter and the tools.

The security crew (step 2.8) and the architecture crew (step 2.9) reach the model
the same way, and it took three attempts to get that way to work at all, so it
lives here once:

    GatewayLLM        the crew's model, forwarded to the audit's budgeted client
    crew_llm          the same adapter in the ``BaseLLM`` shape CrewAI validates
    messages_for_gateway
                      CrewAI's conversation in the shape the provider expects,
                      tool calls and tool results included
    gateway_tools     the tool schemas, minus the key Cohere answers with a 422
    read_only_tools   the three retrieval tools, recording what was called

Nothing here decides anything about an audit: a stage supplies its own prompt,
its own guardrails and its own outcome.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from app.agents.baseline_single import MAX_REPLY_TOKENS
from app.agents.tools import DataBlock, RepositoryTools, render_blocks
from app.db.enums import AgentName
from app.llm.client import LLMClient

logger = logging.getLogger(__name__)


class GatewayLLM:
    """The crew's model, forwarded to the audit's budgeted client.

    Written as a plain adapter rather than a CrewAI ``LLM`` subclass: CrewAI only
    needs an object with ``call`` and ``supports_function_calling``, and duck
    typing keeps its pydantic internals out of the stage's business.
    """

    def __init__(self, client: LLMClient, *, agent: AgentName) -> None:
        self._client = client
        self._agent = agent
        self.model = client.settings.llm_default_model
        self.calls = 0

    def call(
        self,
        messages: Any,
        tools: Any = None,
        callbacks: Any = None,
        available_functions: Any = None,
        from_task: Any = None,
        from_agent: Any = None,
        response_model: Any = None,
    ) -> Any:
        """One completion, budgeted and logged by :class:`LLMClient`.

        Returns the text of the answer, or the tool calls when the model asked for
        tools instead of answering. The tool calls go back in the OpenAI shape
        CrewAI already understands, so its native tool loop can run them.
        """
        result = self._client.complete(
            self._agent,
            messages_for_gateway(messages),
            max_tokens=MAX_REPLY_TOKENS,
            tools=gateway_tools(tools),
        )
        self.calls += 1
        if result.tool_calls:
            return [dict(call) for call in result.tool_calls]
        return result.text

    def supports_function_calling(self) -> bool:
        """Native tool calls: the provider's own tool format, not ReAct text."""
        return True

    def supports_stop_words(self) -> bool:
        return False

    def get_context_window_size(self) -> int:  # pragma: no cover - CrewAI bookkeeping
        return 128_000


def crew_llm(llm: GatewayLLM) -> Any:
    """Wrap the adapter in the ``BaseLLM`` CrewAI validates against.

    CrewAI 1.x only accepts a model name or a ``BaseLLM`` instance for an agent's
    ``llm``, so the duck-typed adapter is hidden behind a subclass that adds no
    behaviour of its own: every call still goes through :class:`GatewayLLM` and
    therefore through the audit's budgeted client. The class is defined here, at
    call time, so importing :mod:`app.agents` never imports CrewAI.
    """
    from crewai.llms.base_llm import BaseLLM

    class GatewayBaseLLM(BaseLLM):
        """A ``BaseLLM`` whose every completion is the gateway's completion."""

        llm_type: str = "gateway"

        def call(
            self,
            messages: Any,
            tools: Any = None,
            callbacks: Any = None,
            available_functions: Any = None,
            from_task: Any = None,
            from_agent: Any = None,
            response_model: Any = None,
        ) -> Any:
            """Forward to the adapter, which budgets and logs the call."""
            return llm.call(messages=messages, tools=tools)

        def supports_function_calling(self) -> bool:
            """Native tool calls, forwarded to the provider as OpenAI tools."""
            return True

        def supports_stop_words(self) -> bool:
            return False

    return GatewayBaseLLM(model=llm.model, context_window_size=llm.get_context_window_size())


def gateway_tools(tools: Any) -> list[dict[str, Any]] | None:
    """CrewAI's OpenAI tool schemas, minus the keys the provider rejects.

    CrewAI marks every function ``strict: true``. Cohere answers that with a
    ``422 unknown field`` and the whole crew dies before its first call, so the
    flag is dropped here rather than sent and refused. The schemas themselves are
    passed through untouched: they are what the model selects a tool by.
    """
    cleaned: list[dict[str, Any]] = []
    for tool in tools or ():
        if not isinstance(tool, dict):
            continue
        item = dict(tool)
        function = item.get("function")
        if isinstance(function, dict) and "strict" in function:
            item["function"] = {key: value for key, value in function.items() if key != "strict"}
        cleaned.append(item)
    return cleaned or None


def messages_for_gateway(messages: Any) -> list[dict[str, Any]]:
    """CrewAI's messages in the shape the OpenAI-compatible gateway expects.

    Conversation turns that carry tool calls or a tool result must keep those
    fields: a result without its ``tool_call_id`` is a message the provider cannot
    match to the call that asked for it.
    """
    payload: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        item: dict[str, Any] = {
            "role": str(message.get("role", "user")),
            "content": content
            if isinstance(content, str)
            else ("" if content is None else json.dumps(content)),
        }
        if message.get("tool_calls"):
            item["tool_calls"] = list(message["tool_calls"])
        if message.get("tool_call_id"):
            item["tool_call_id"] = str(message["tool_call_id"])
        payload.append(item)
    return payload


def read_only_tools(repository_tools: RepositoryTools, calls: list[str] | None = None) -> list[Any]:
    """The three retrieval tools, and nothing that could change the machine.

    ``calls`` collects the name of every tool the model actually invoked, which is
    how a stage tells an investigation apart from an answer written from memory.
    There is no shell tool, no file writer and no network path here, so the worst
    a confused or injected model can do is ask for context it already had.
    """
    from crewai.tools import tool

    record = (calls if calls is not None else []).append

    @tool("retrieve_context")
    def retrieve_context_tool(query: str) -> str:
        """Search this audit's code index for code relevant to a question.

        Pass a plain-language question ("where is an order read by id?"), never a
        file path or a line range: this searches, it does not open a file. Returns
        delimited, line-numbered blocks of repository text. Read-only.
        """
        record("retrieve_context")
        blocks: tuple[DataBlock, ...] = repository_tools.retrieve_context(query, k=6)
        logger.debug(
            "retrieve_context(%r) -> %s",
            query,
            [block.file_path for block in blocks] or "nothing",
        )
        return render_blocks(blocks) or "no indexed code matched that query"

    @tool("get_symbol")
    def get_symbol_tool(name: str) -> str:
        """Return the chunk that declares or references a symbol by name. Read-only."""
        record("get_symbol")
        blocks = repository_tools.get_symbol(name)
        logger.debug("get_symbol(%r) -> %s", name, [block.file_path for block in blocks] or "nothing")
        return render_blocks(blocks) or f"no symbol named {name!r} was found"

    @tool("read_finding")
    def read_finding_tool(finding_id: str) -> str:
        """Read one stored finding and its evidence, by the id in the findings list."""
        record("read_finding")
        try:
            view = repository_tools.read_finding(uuid.UUID(str(finding_id).strip()))
        except ValueError:
            return f"{finding_id!r} is not a finding id"
        if view is None:
            return f"no finding {finding_id!r} in this audit"
        logger.debug("read_finding(%s) -> %s", finding_id, view.file_path)
        return (
            f"{view.render()}\n"
            f'<<<DATA file="{view.file_path}" lines="{view.line_start}-{view.line_end}" '
            f'reason="evidence" untrusted="true">>>\n'
            f"{view.snippet}\n<<<END_DATA>>>"
        )

    return [retrieve_context_tool, get_symbol_tool, read_finding_tool]
