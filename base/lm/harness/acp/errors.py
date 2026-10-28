from __future__ import annotations


class AcpError(RuntimeError): ...


class AcpClosedError(AcpError): ...


class AcpTimeoutError(AcpError): ...


class AcpRpcError(AcpError):
    def __init__(self, method: str, code: int, message: str, data: object = None) -> None:
        super().__init__(f"{method} failed with JSON-RPC error {code}: {message}")
        self.method = method
        self.code = code
        self.message = message
        self.data = data
