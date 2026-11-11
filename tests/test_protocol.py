import asyncio
import base64
import hashlib
import json
import socket
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from mcp.server.fastmcp import FastMCP

from openjiuwen_challenge.protocol import Protocol
from openjiuwen_challenge.team import canonical, digest, load_team, validate_team
from openjiuwen_challenge.transport import RegistryError, connect

CONSENT = dict(
    text_version="nsu-privacy-registration-v1",
    policy_url="https://www.nsu.ru/n/privacy/",
    statement="Test consent statement",
    accepted_required=True,
)


@pytest.fixture
def team():
    value = json.loads((Path(__file__).parents[1] / "examples/team.example.json").read_text())
    value["consent"]["accepted"] = True
    value["team_name"] = "  Кириллица и пробелы  "
    return value


@asynccontextmanager
async def mock_registry(
    *,
    corrupt_fingerprint=False,
    overload_once=False,
    http_429_once=False,
    invalid_timestamp=False,
    wrong_heartbeat_version=False,
):
    """Actual MCP HTTP transport, independent server signature/digest verification."""
    server = FastMCP("test-registry", json_response=True)
    nonces = {}
    events = []
    owners = {}
    state = {}
    attempted_nonces = []

    @server.tool()
    def create_registration_challenge(
        action: str = "register_agent",
        name: str | None = None,
        registration_id: str | None = None,
        version: str | None = None,
    ) -> dict:
        nonce = uuid.uuid4().hex
        c = dict(nonce=nonce, expires_in=300, scheme="ssh-key-v1", namespace="agent-registry")
        if action == "register_agent":
            assert name is registration_id is version is None
            c.update(
                message_format="{nonce}:{name}:{version}:{sha256(harness_report)}",
                team_message_format="{nonce}:{name}:{version}:{sha256(harness_report)}:team-v1:{sha256(canonical_team_json)}",
                consent=CONSENT,
                open_enrollment=True,
            )
        else:
            message = dict(
                action=action,
                name=name,
                nonce=nonce,
                protocol="agent-registry-operation-v1",
                registration_id=registration_id,
                version=version,
            )
            # Noncanonical whitespace deliberately proves the exact returned bytes are signed.
            c["message"] = json.dumps(message, ensure_ascii=False, indent=2)
        nonces[nonce] = c
        return c

    @server.tool()
    def register_agent(
        name: str,
        version: str,
        harness_report: str,
        challenge_nonce: str,
        challenge_proof: str,
        public_key: str,
        team: dict | None = None,
    ) -> dict:
        if overload_once and not events:
            events.append("overload")
            raise ValueError("server overloaded; retry after 0 seconds")
        assert challenge_nonce in nonces
        public = serialization.load_ssh_public_key(public_key.encode())
        report_hash = hashlib.sha256(harness_report.encode()).hexdigest()
        message = f"{challenge_nonce}:{name}:{version}:{report_hash}"
        if team is not None:
            raw_team = json.dumps(
                team, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
            )
            message += ":team-v1:" + hashlib.sha256(raw_team.encode()).hexdigest()
        public.verify(base64.b64decode(challenge_proof), message.encode())
        nonces.pop(challenge_nonce)
        blob = base64.b64decode(public_key.split()[1])
        fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
        rid = uuid.uuid4().hex
        owners[rid] = public
        state.update(rid=rid, name=name, version=version)
        if team is not None:
            state["team"] = team
        events.append("register")
        return dict(
            attested=True,
            key_fingerprint="wrong" if corrupt_fingerprint else fingerprint,
            agent=dict(name=name, version=version, registration_id=rid, proof=dict(verified=True)),
            team_registered_at="2026-11-04T00:00:00Z",
            team_updated_at="2026-11-04T00:00:00Z",
            consent_recorded_at="invalid" if invalid_timestamp else "2026-11-04T00:00:00Z",
        )

    def operation(action, name, registration_id, challenge_nonce, challenge_proof, version=None):
        c = nonces.pop(challenge_nonce)
        data = json.loads(c["message"])
        assert data["registration_id"] == registration_id == state["rid"]
        assert data["action"] == action
        assert data["version"] == version
        owners[registration_id].verify(base64.b64decode(challenge_proof), c["message"].encode())
        events.append(action)
        if action == "heartbeat":
            if version is not None:
                state["version"] = version
            return dict(
                ok=True,
                name=name,
                registration_id=registration_id,
                version="wrong" if wrong_heartbeat_version else state["version"],
                last_seen_at="2026-11-04T00:01:00Z",
            )
        return dict(removed=True, name=name)

    @server.tool()
    def heartbeat(
        name: str,
        registration_id: str,
        challenge_nonce: str,
        challenge_proof: str,
        version: str | None = None,
    ) -> dict:
        return operation("heartbeat", name, registration_id, challenge_nonce, challenge_proof, version)

    @server.tool()
    def deregister_agent(name: str, registration_id: str, challenge_nonce: str, challenge_proof: str) -> dict:
        return operation("deregister_agent", name, registration_id, challenge_nonce, challenge_proof)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    app = server.streamable_http_app()
    if http_429_once:
        from starlette.middleware.base import BaseHTTPMiddleware
        from starlette.responses import Response

        async def limit_first_registration(request, call_next):
            if request.method == "POST":
                payload = json.loads(await request.body())
                if (
                    payload.get("method") == "tools/call"
                    and payload.get("params", {}).get("name") == "register_agent"
                ):
                    attempted_nonces.append(payload["params"]["arguments"]["challenge_nonce"])
                    if len(attempted_nonces) == 1:
                        return Response(status_code=429, headers={"Retry-After": "0"})
            return await call_next(request)

        app.add_middleware(BaseHTTPMiddleware, dispatch=limit_first_registration)
    http = uvicorn.Server(uvicorn.Config(app, log_level="critical", lifespan="on"))
    task = asyncio.create_task(http.serve(sockets=[sock]))
    try:
        for _ in range(200):
            if http.started:
                break
            if task.done():
                await task
            await asyncio.sleep(0.01)
        assert http.started
        async with connect(f"http://127.0.0.1:{port}/mcp") as registry:
            yield registry, state, events
        if http_429_once:
            assert len(attempted_nonces) == 2
            assert attempted_nonces[0] != attempted_nonces[1]
    finally:
        http.should_exit = True
        await asyncio.wait_for(task, 5)
        sock.close()


@pytest.mark.asyncio
async def test_full_real_mcp_lifecycle(team):
    async with mock_registry() as (registry, state, events):
        await registry.discover()
        protocol = Protocol(registry, Ed25519PrivateKey.generate())
        registration = await protocol.register("  Scout  ", " 1.0 ", '{"env":{}}', team, digest(CONSENT))
        assert state["team"] == team
        assert state["team"]["team_name"].startswith("  ")
        result = await protocol.operation("heartbeat", "1.1")
        assert result["registration_id"] == registration.registration_id
        await protocol.operation("deregister_agent")
        assert protocol.current is None
        assert events == ["register", "heartbeat", "deregister_agent"]
        assert state["team"] == team  # deregistration keeps the questionnaire


@pytest.mark.asyncio
async def test_consent_change_prevents_submission(team):
    async with mock_registry() as (registry, state, events):
        protocol = Protocol(registry, Ed25519PrivateKey.generate())
        with pytest.raises(ValueError, match="consent_sha256"):
            await protocol.register("Scout", "1.0", "{}", team, "old")
        assert not state and not events


@pytest.mark.asyncio
async def test_wrong_fingerprint_not_accepted(team):
    async with mock_registry(corrupt_fingerprint=True) as (registry, _, events):
        protocol = Protocol(registry, Ed25519PrivateKey.generate())
        with pytest.raises(RegistryError, match="аттестации"):
            await protocol.register("Scout", "1.0", "{}", team, digest(CONSENT))
        assert protocol.current is None
        assert events == ["register"]


@pytest.mark.asyncio
async def test_overload_gets_new_challenge(team, monkeypatch):
    async def no_sleep(_):
        pass

    monkeypatch.setattr("openjiuwen_challenge.protocol.asyncio.sleep", no_sleep)

    # Use a separate sleep during HTTP server startup because asyncio is shared.
    # This test exercises retries with a small in-memory stub; HTTP lifecycle is above.
    class Stub:
        calls = 0
        mutations = 0

        async def call(self, name, args):
            from openjiuwen_challenge.transport import Overloaded

            if name == "create_registration_challenge":
                self.calls += 1
                return dict(
                    nonce=str(self.calls),
                    expires_in=300,
                    scheme="ssh-key-v1",
                    namespace="agent-registry",
                    message_format="{nonce}:{name}:{version}:{sha256(harness_report)}",
                )
            self.mutations += 1
            assert args["challenge_nonce"] == str(self.calls)
            raise Overloaded(0)

    stub = Stub()
    with pytest.raises(Exception, match="перегружен"):
        await Protocol(stub, Ed25519PrivateKey.generate()).register("Scout", "1.0", "{}")
    assert stub.calls == stub.mutations == 5


@pytest.mark.asyncio
async def test_no_recovery_of_foreign_id():
    protocol = Protocol(None, Ed25519PrivateKey.generate())
    with pytest.raises(ValueError, match="текущем процессе"):
        await protocol.operation("deregister_agent")


def test_canonical_input_not_trimmed(team):
    expected = hashlib.sha256(
        json.dumps(team, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    validate_team(team)
    assert digest(team) == expected
    assert b"  " in canonical(team)


@pytest.mark.parametrize(
    "change",
    [
        lambda t: t.update(extra="bad"),
        lambda t: t["participants"].pop(),
        lambda t: t["participants"][0].update(phone=1234),
        lambda t: t["participants"][0].update(email="bad"),
        lambda t: t["participants"][0].update(telegram="@1bad"),
        lambda t: t["participants"][0].update(city="City\n"),
        lambda t: t["consent"].update(accepted="true"),
        lambda t: t["consent"].update(accepted=False),
    ],
)
def test_bad_team_rejected(team, change):
    change(team)
    with pytest.raises(ValueError):
        validate_team(team)


def test_duplicate_json_key_rejected(tmp_path):
    path = tmp_path / "team.json"
    path.write_text('{"team_name":"A","team_name":"B"}')
    with pytest.raises(ValueError, match="повторяющийся"):
        load_team(path)


def test_report_without_harness_evidence_rejected(monkeypatch):
    from openjiuwen_challenge.harness import collect_report

    for name in ("JIUWENSWARM_DATA_DIR", "JIUWENSWARM_HOME", "JIUWENSWARM_LOG_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("psutil.Process.parents", lambda _: [])
    with pytest.raises(ValueError, match="Нет признаков harness"):
        collect_report()


def test_inherited_environment_alone_is_allowed_by_instruction(monkeypatch):
    from openjiuwen_challenge.harness import collect_report

    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", "/test/inherited-harness")
    monkeypatch.setenv("OPENROUTER_API_KEY", "must-not-be-reported")
    monkeypatch.setattr("psutil.Process.parents", lambda _: [])
    report, sha = collect_report()
    assert json.loads(report)["env"]["JIUWENSWARM_DATA_DIR"] == "/test/inherited-harness"
    assert "must-not-be-reported" not in report
    assert sha == hashlib.sha256(report.encode()).hexdigest()


@pytest.mark.asyncio
async def test_http_429_real_transport_retry_uses_new_nonce(team):
    async with mock_registry(http_429_once=True) as (registry, _, events):
        protocol = Protocol(registry, Ed25519PrivateKey.generate())
        await protocol.register("Scout", "1.0", "{}", team, digest(CONSENT))
        await protocol.operation("heartbeat")
        await protocol.operation("deregister_agent")
        assert events == ["register", "heartbeat", "deregister_agent"]


@pytest.mark.asyncio
async def test_invalid_team_timestamp_is_not_success(team):
    async with mock_registry(invalid_timestamp=True) as (registry, _, _):
        protocol = Protocol(registry, Ed25519PrivateKey.generate())
        with pytest.raises(RegistryError, match="ISO 8601"):
            await protocol.register("Scout", "1.0", "{}", team, digest(CONSENT))
        # Attestation and own ID were verified before the invalid timestamp.
        assert protocol.current is not None
        await protocol.operation("deregister_agent")


@pytest.mark.asyncio
async def test_heartbeat_response_version_must_match(team):
    async with mock_registry(wrong_heartbeat_version=True) as (registry, _, _):
        protocol = Protocol(registry, Ed25519PrivateKey.generate())
        await protocol.register("Scout", "1.0", "{}", team, digest(CONSENT))
        with pytest.raises(RegistryError, match="версию"):
            await protocol.operation("heartbeat", "1.1")
        await protocol.operation("deregister_agent")


def test_ed25519_key_format_and_no_overwrite(tmp_path):
    from openjiuwen_challenge.crypto import create_key, fingerprint, load_key, public_key, sign

    path = tmp_path / "private"
    fp = create_key(path)
    key = load_key(path)
    assert fp == fingerprint(public_key(key))
    proof = base64.b64decode(sign(key, "точные байты\n"))
    assert len(proof) == 64
    key.public_key().verify(proof, "точные байты\n".encode())
    with pytest.raises(FileExistsError):
        create_key(path)


@pytest.mark.asyncio
async def test_stale_process_cannot_touch_new_registration(team):
    async with mock_registry() as (registry, state, events):
        key = Ed25519PrivateKey.generate()
        old = Protocol(registry, key)
        await old.register("Scout", "1.0", "{}", team, digest(CONSENT))
        old_id = old.current.registration_id
        new = Protocol(registry, key)
        await new.register("Scout", "1.0", "{}")
        assert new.current.registration_id != old_id
        assert state["team"] == team
        with pytest.raises(RegistryError):
            await old.operation("heartbeat")
        with pytest.raises(RegistryError):
            await old.operation("deregister_agent")
        assert state["rid"] == new.current.registration_id
        await new.operation("deregister_agent")
        assert events == ["register", "register", "deregister_agent"]


@pytest.mark.asyncio
async def test_ambiguous_timeout_does_not_resubmit():
    class TimeoutRegistry:
        mutations = 0

        async def call(self, name, args):
            if name == "create_registration_challenge":
                return dict(
                    nonce="nonce",
                    expires_in=300,
                    scheme="ssh-key-v1",
                    namespace="agent-registry",
                    message_format="{nonce}:{name}:{version}:{sha256(harness_report)}",
                )
            self.mutations += 1
            raise TimeoutError("result unknown")

    registry = TimeoutRegistry()
    with pytest.raises(TimeoutError):
        await Protocol(registry, Ed25519PrivateKey.generate()).register("Scout", "1.0", "{}")
    assert registry.mutations == 1
