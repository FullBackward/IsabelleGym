"""Shared OpenAI-compatible chat client (provider-agnostic)."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator

from openai import APIError, AsyncOpenAI, Timeout
from openai.types.chat import ChatCompletion, ChatCompletionMessageToolCall

from .config import Config, load


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]

    def to_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass(frozen=True)
class RoundResult:
    assistant_text: str | None
    tool_calls: list[ChatCompletionMessageToolCall]
    usage: dict[str, int]
    latency_s: float


class ModelClient:
    def __init__(self, cfg: Config | None = None, timeout: float = 360.0):
        self.cfg = cfg or load()
        self.timeout = timeout
        self.client = AsyncOpenAI(
            base_url=self.cfg.model.base_url,
            api_key=self.cfg.model.api_key,
            timeout=timeout,
        )

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec] | list[dict[str, Any]] | None = None,
        system_prompt: str | None = None,
    ) -> RoundResult:
        """Send a chat request. Optionally prepend a system_prompt message."""
        t0 = time.time()
        messages_out = list(messages)
        if system_prompt:
            messages_out = [{"role": "system", "content": system_prompt}] + messages_out

        kwargs: dict[str, Any] = {
            "model": self.cfg.model.model_id,
            "temperature": self.cfg.model.temperature,
            "max_tokens": self.cfg.model.max_tokens,
            "messages": messages_out,
        }
        if tools:
            openai_tools = []
            for t in tools:
                if isinstance(t, ToolSpec):
                    openai_tools.append(t.to_openai())
                else:
                    openai_tools.append({
                        "type": "function",
                        "function": {
                            "name": t["name"],
                            "description": t.get("description", ""),
                            "parameters": t.get("parameters", {"type": "object", "properties": {}}),
                        },
                    })
            kwargs["tools"] = openai_tools
            kwargs["tool_choice"] = "auto"

        last_error: Exception | None = None
        for attempt in range(3):
            try:
                resp: ChatCompletion = await self.client.chat.completions.create(**kwargs)
                break
            except Timeout as e:
                last_error = e
                print(f"Model request timed out (attempt {attempt + 1}/3, timeout={self.timeout}s)")
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
                continue
            except APIError as e:
                last_error = e
                print(f"Model API error (attempt {attempt + 1}/3): {e}")
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
                continue
            except Exception as e:
                last_error = e
                print(f"Model request failed (attempt {attempt + 1}/3): {type(e).__name__}: {e}")
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
                continue
        else:
            raise RuntimeError(f"Model request failed after 3 attempts: {last_error}")

        latency_s = time.time() - t0
        choice = resp.choices[0]
        message = choice.message
        if resp.usage is None:
            if not hasattr(self, "_usage_warned"):
                print("Warning: provider did not return usage block; token counts will be 0")
                self._usage_warned = True
            usage = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0}
        else:
            cached = 0
            if getattr(resp.usage, "prompt_tokens_details", None):
                cached = getattr(resp.usage.prompt_tokens_details, "cached_tokens", 0) or 0
            usage = {
                "input_tokens": getattr(resp.usage, "prompt_tokens", 0) or 0,
                "output_tokens": getattr(resp.usage, "completion_tokens", 0) or 0,
                "cached_tokens": cached,
            }
        return RoundResult(
            assistant_text=message.content,
            tool_calls=list(message.tool_calls or []),
            usage=usage,
            latency_s=latency_s,
        )

    async def stream_text(
        self,
        messages: list[dict[str, Any]],
    ) -> AsyncIterator[str]:
        stream = await self.client.chat.completions.create(
            model=self.cfg.model.model_id,
            temperature=self.cfg.model.temperature,
            max_tokens=self.cfg.model.max_tokens,
            messages=messages,
            stream=True,
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta