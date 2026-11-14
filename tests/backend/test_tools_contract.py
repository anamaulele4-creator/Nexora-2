from __future__ import annotations

import uuid

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from connector_hub.registry import ToolRegistry
from connectors.base import (
    ApprovalCategory,
    BaseConnector,
    ConnectionResult,
    ConnectorContext,
    ConnectorError,
    HealthReport,
    HealthStatus,
    RateLimit,
    RetryPolicy,
    RiskLevel,
    ToolSpec,
    VerificationStrategy,
)


class EchoIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str


class EchoOut(BaseModel):
    text: str


class SendIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to: str


class SendOut(BaseModel):
    id: str


ECHO = ToolSpec(
    name="demo.echo", description="Echo the given text back (test tool).", parameters=EchoIn, response=EchoOut,
    permissions=("demo.read",), risk=RiskLevel.low, requires_approval=False, idempotent=True,
    rate_limit=RateLimit(max_calls=10, per_seconds=60), retry=RetryPolicy(max_attempts=3),
    verification=VerificationStrategy.response_schema,
)
SEND = ToolSpec(
    name="demo.send", description="Send something visible to other people (test tool).", parameters=SendIn,
    response=SendOut, permissions=("demo.send",), risk=RiskLevel.high, requires_approval=True,
    approval_category=ApprovalCategory.send_message, rate_limit=RateLimit(max_calls=5, per_seconds=60),
    verification=VerificationStrategy.read_back,
)


class DemoConnector(BaseConnector):
    key = "demo"
    tools = (ECHO, SEND)

    async def connect(self, ctx, credentials):
        return ConnectionResult("acc-1", "Demo account")

    async def disconnect(self, ctx):
        return None

    async def health_check(self, ctx):
        return HealthReport(HealthStatus.healthy)

    async def _run(self, spec, ctx, params):
        if spec.name == "demo.echo":
            if params.text == "boom":
                raise ConnectorError("upstream_5xx", "fail", retryable=True)
            if params.text == "bad-response":
                return {"unexpected": 1}
            return EchoOut(text=params.text)
        return SendOut(id="msg-1")


def ctx(perms=("demo.read", "demo.send"), approval=None):
    return ConnectorContext(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), frozenset(perms), approval_id=approval)


def test_high_risk_without_approval_is_rejected():
    with pytest.raises(ValidationError, match="aprovação"):
        ToolSpec(name="demo.pay", description="Pay an invoice right now.", parameters=EchoIn, response=EchoOut,
                 permissions=("demo.pay",), risk=RiskLevel.critical, requires_approval=False,
                 rate_limit=RateLimit(max_calls=1, per_seconds=60), verification=VerificationStrategy.read_back)


def test_invalid_names_and_loose_parameters_rejected():
    with pytest.raises(ValidationError, match="conector"):
        ToolSpec(**(ECHO.model_dump() | {"name": "Echo"}))

    class Loose(BaseModel):
        text: str

    with pytest.raises(ValidationError, match="forbid"):
        ToolSpec(**(ECHO.model_dump() | {"parameters": Loose}))


def test_side_effect_tools_cannot_auto_retry_unless_idempotent():
    with pytest.raises(ValidationError, match="repetir"):
        ToolSpec(**(SEND.model_dump() | {"retry": RetryPolicy(max_attempts=3)}))


def test_openai_tool_schema():
    tool = ECHO.to_openai_tool()
    assert tool["type"] == "function"
    assert tool["function"]["name"] == "demo__echo"
    params = tool["function"]["parameters"]
    assert params["additionalProperties"] is False and params["required"] == ["text"]
    assert ECHO.manifest()["risk"] == "low"


def test_retry_backoff():
    p = RetryPolicy(max_attempts=4, backoff_seconds=1, backoff_multiplier=2)
    assert [p.delay_for(i) for i in (1, 2, 3, 4)] == [0, 1, 2, 4]


async def test_execute_enforces_permissions_approval_and_schemas():
    c = DemoConnector()
    r = await c.execute("demo.echo", ctx(perms=()), {"text": "hi"})
    assert r.error_code == "permission_denied"
    r = await c.execute("demo.echo", ctx(), {"text": "hi", "extra": 1})
    assert r.error_code == "invalid_arguments"
    r = await c.execute("demo.echo", ctx(), {"text": "hi"})
    assert r.ok and r.data == {"text": "hi"}
    r = await c.execute("demo.echo", ctx(), {"text": "boom"})
    assert not r.ok and r.retryable
    r = await c.execute("demo.echo", ctx(), {"text": "bad-response"})
    assert r.error_code == "invalid_response"
    r = await c.execute("demo.send", ctx(), {"to": "x"})
    assert r.error_code == "approval_required"
    r = await c.execute("demo.send", ctx(approval=uuid.uuid4()), {"to": "x"})
    assert r.ok


def test_registry_only_resolves_registered_tools():
    reg = ToolRegistry()
    reg.register(DemoConnector())
    assert reg.names() == ["demo.echo", "demo.send"]
    assert reg.resolve("demo__echo")[1] is ECHO
    with pytest.raises(ConnectorError):
        reg.resolve("os.system")
    with pytest.raises(ValueError):
        reg.register(DemoConnector())
    assert [t["function"]["name"] for t in reg.openai_tools({"demo.read"})] == ["demo__echo"]


def test_connector_rejects_foreign_tools():
    with pytest.raises(TypeError):
        class Wrong(DemoConnector):  # noqa: F841
            key = "other"
