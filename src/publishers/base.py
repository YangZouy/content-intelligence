from __future__ import annotations
from typing import Any, Dict, List, Protocol, runtime_checkable
from src.state import BrandConfig, PublishResultItem

@runtime_checkable
class Publisher(Protocol):

    @property
    def platform(self) -> str:
        ...

    def validate(
        self,
        content: str,
        brand: BrandConfig,
    ) -> tuple[bool, List[str]]:
        ...

    def publish(
        self,
        content: str,
        brand: BrandConfig,
    ) -> PublishResultItem:
        ...
