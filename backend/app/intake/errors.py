"""Errors raised by intake. Each carries the status code the API should use."""


class IntakeError(Exception):
    """A rejected repository, with a message safe to show the user."""

    status_code = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class UploadTooLarge(IntakeError):
    status_code = 413
