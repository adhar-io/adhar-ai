from __future__ import annotations


class BackendNotConfigured(RuntimeError):
    """A tool was called but its backend URL/credential is not set.

    Raised (never swallowed into a fake result) so the model sees an honest
    error and can say so instead of hallucinating telemetry.
    """

    def __init__(self, backend: str, hint: str = "") -> None:
        msg = f"{backend} is not configured for this Adhar AI deployment"
        if hint:
            msg = f"{msg} ({hint})"
        super().__init__(msg)
        self.backend = backend


class WriteNotPermitted(PermissionError):
    """A write tool was called where policy forbids it."""
