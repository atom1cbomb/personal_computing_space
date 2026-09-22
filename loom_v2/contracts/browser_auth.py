"""Transient browser authentication messages, never command receipts."""

from typing import Literal

from pydantic import Field, model_validator

from .types import ContractModel


class BrowserAuthContext(ContractModel):
    session_id: str = Field(pattern=r"^[A-Za-z0-9_-]{32,128}$")
    callback_uri: Literal["http://localhost:3000/login-callback"]
    expires_at: float = Field(allow_inf_nan=False)


class BrowserAuthMessage(ContractModel):
    action: Literal["open", "callback"]
    session_id: str = Field(pattern=r"^[A-Za-z0-9_-]{32,128}$")
    query: dict[str, list[str]] = Field(default_factory=dict, max_length=32)

    @model_validator(mode="after")
    def validate_query(self):
        if self.action == "open" and self.query:
            raise ValueError("browser_callback_invalid")
        if sum(len(values) for values in self.query.values()) > 32:
            raise ValueError("browser_callback_invalid")
        if any(len(key) > 128 or len(value) > 32 or any(len(item) > 4096 for item in value)
               for key, value in self.query.items()):
            raise ValueError("browser_callback_invalid")
        if sum(len(key.encode()) + sum(len(item.encode()) for item in value)
               for key, value in self.query.items()) > 16384:
            raise ValueError("browser_callback_invalid")
        return self
