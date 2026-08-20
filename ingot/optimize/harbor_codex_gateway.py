"""Harbor Codex adapter variant for the local LiteLLM compatibility gateway."""
from __future__ import annotations

from harbor.agents.installed.codex import Codex

from .harbor_gateway import codex_gateway_setup_command


class GatewayCodex(Codex):
    """Use HTTP Responses without the native-only reasoning parameter."""
    # Harbor's parent Codex adapter defaults this flag to "high".  LiteLLM custom_openai
    # correctly rejects it for the local Qwen endpoint, while its remaining CLI flags still carry
    # the normal tool-call behavior.
    CLI_FLAGS = [flag for flag in Codex.CLI_FLAGS if flag.kwarg != "reasoning_effort"]

    async def run(self, instruction, environment, context):  # type: ignore[no-untyped-def]
        await self.exec_as_agent(
            environment,
            command=codex_gateway_setup_command(),
            env={"CODEX_HOME": self._REMOTE_CODEX_HOME.as_posix()},
        )
        await super().run(instruction, environment, context)
