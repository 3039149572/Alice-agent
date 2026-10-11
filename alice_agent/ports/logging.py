from __future__ import annotations

from typing import Any, Protocol


class LoggerPort(Protocol):
    def debug(self, message: str, *args: Any) -> None: ...

    def info(self, message: str, *args: Any) -> None: ...

    def warning(self, message: str, *args: Any) -> None: ...

    def error(self, message: str, *args: Any) -> None: ...

    def exception(self, message: str, *args: Any) -> None: ...


class NullLogger:
    def debug(self, message: str, *args: Any) -> None:
        pass

    def info(self, message: str, *args: Any) -> None:
        pass

    def warning(self, message: str, *args: Any) -> None:
        pass

    def error(self, message: str, *args: Any) -> None:
        pass

    def exception(self, message: str, *args: Any) -> None:
        pass
