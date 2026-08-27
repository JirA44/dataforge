"""Domain exceptions translated by the HTTP layer."""


class DataForgeError(Exception):
    """Base error with a stable machine-readable code."""

    code = "DATAFORGE_ERROR"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class NotFoundError(DataForgeError):
    code = "NOT_FOUND"


class ConflictError(DataForgeError):
    code = "CONFLICT"


class ValidationError(DataForgeError):
    code = "VALIDATION_ERROR"


class IntegrityError(DataForgeError):
    code = "INTEGRITY_ERROR"

