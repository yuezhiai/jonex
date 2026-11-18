class DomainError(Exception):
    """统一领域错误;.code 映射到 §7.4 错误 envelope 的稳定错误码。"""

    def __init__(self, code: str, message: str, http_status: int = 500):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


class NotFoundError(DomainError):
    def __init__(self, message: str):
        super().__init__("not_found", message, 404)


class ConflictError(DomainError):
    def __init__(self, message: str):
        super().__init__("conflict", message, 409)


class ValidationError(DomainError):
    def __init__(self, message: str):
        super().__init__("validation_failed", message, 422)


class CompileModelUnavailable(RuntimeError):
    """The configured model or gateway credentials rejected the compile."""
