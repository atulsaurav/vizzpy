from __future__ import annotations
from dataclasses import dataclass
from typing import Optional


@dataclass
class FuncSpan:
    qualified_name: str   # e.g. "services.order.OrderService.process"
    display_name: str     # e.g. "OrderService.process" or "OrderService" for __init__
    module_name: str      # e.g. "services.order"
    start: int
    end: int
    docstring: Optional[str] = None


class FuncScope:
    """Registry of all function/method definitions within a single module."""

    def __init__(self, module_name: str):
        self.module_name = module_name
        self._spans: list[FuncSpan] = []
        self._names: set[str] = set()

    def add(self, span: FuncSpan) -> None:
        self._spans.append(span)
        self._names.add(span.qualified_name)

    @property
    def names(self) -> set[str]:
        return self._names

    @property
    def spans(self) -> list[FuncSpan]:
        return list(self._spans)
