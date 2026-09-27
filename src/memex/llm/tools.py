"""Contrato de chat con HERRAMIENTAS y STREAMING, provider-agnóstico (hermano de `client.py`).

`LLMClient.complete` alcanza para los lotes (una pregunta, una respuesta). Un asistente
conversacional necesita dos cosas más: que el modelo pueda llamar herramientas (y recibir su
resultado en la misma conversación) y que el texto de la respuesta llegue por partes mientras se
genera, para mostrarlo en el chat antes de que termine. Este módulo define esa interfaz; los
proveedores OpenAI-compatible (DeepSeek, OpenAI) la implementan con `stream_tools`.

Los tipos son deliberadamente chicos y sin vendor: `ToolSpec` es la descripción JSON-Schema de una
herramienta, `ToolCall` un pedido del modelo, `AgentMessage` un turno (incluye `tool` para devolver
resultados) y los eventos del stream son `TextDelta`, `ToolCallsEvent` y `StreamDone`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal, Protocol, runtime_checkable

from memex.llm.client import LLMUsage

AgentRole = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True)
class ToolSpec:
    """Una herramienta ofrecida al modelo (forma `function` de OpenAI)."""

    name: str
    description: str
    parameters: Mapping[str, Any]

    def as_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": dict(self.parameters),
            },
        }


@dataclass(frozen=True)
class ToolCall:
    """Un pedido del modelo de ejecutar una herramienta. `arguments` ya parseado (dict); si el
    modelo mandó JSON inválido, `arguments` trae `{"_raw": "<texto>"}` para que el caller decida."""

    id: str
    name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True)
class AgentMessage:
    """Un turno de la conversación con herramientas. Un `assistant` puede traer `tool_calls`
    (y content vacío); un `tool` trae el resultado de UNA llamada (`tool_call_id`)."""

    role: AgentRole
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    #: Nombre de la herramienta (algunos proveedores lo piden en el mensaje `tool`).
    name: str | None = None

    def as_openai(self) -> dict[str, Any]:
        msg: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.role == "assistant" and self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": _dumps(c.arguments)},
                }
                for c in self.tool_calls
            ]
            if not self.content:
                msg["content"] = None
        if self.role == "tool":
            msg["tool_call_id"] = self.tool_call_id or ""
            if self.name:
                msg["name"] = self.name
        return msg


def _dumps(args: Mapping[str, Any]) -> str:
    import json

    if "_raw" in args and len(args) == 1:
        return str(args["_raw"])
    return json.dumps(dict(args), ensure_ascii=False)


@dataclass(frozen=True)
class TextDelta:
    """Un pedazo del texto de la respuesta, en orden."""

    text: str


@dataclass(frozen=True)
class ToolCallsEvent:
    """El modelo terminó el turno pidiendo herramientas (una o varias, completas)."""

    calls: tuple[ToolCall, ...]


@dataclass(frozen=True)
class StreamDone:
    """Fin del stream: uso, costo y motivo de corte. Siempre es el ÚLTIMO evento."""

    model: str
    usage: LLMUsage
    cost_usd: Decimal
    latency_ms: int
    finish_reason: str | None = None
    #: Texto completo acumulado (comodidad para el caller que no lo fue juntando).
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = field(default_factory=tuple)


StreamEvent = TextDelta | ToolCallsEvent | StreamDone


@runtime_checkable
class ToolStreamClient(Protocol):
    """Proveedor capaz de chat con herramientas en streaming."""

    def stream_tools(
        self,
        messages: Sequence[AgentMessage],
        *,
        tools: Sequence[ToolSpec] = (),
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamEvent]: ...


__all__ = [
    "AgentMessage",
    "AgentRole",
    "StreamDone",
    "StreamEvent",
    "TextDelta",
    "ToolCall",
    "ToolCallsEvent",
    "ToolSpec",
    "ToolStreamClient",
]
