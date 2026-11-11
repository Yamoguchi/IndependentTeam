import asyncio
import hashlib
import random
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from .crypto import fingerprint, public_key, sign
from .team import digest, validate_team
from .transport import Overloaded, RegistryError


def identity(value):
    value = value.strip()
    if not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("Имя/версия: непустая строка без управляющих символов")
    return value


def check_challenge(challenge):
    if challenge.get("scheme") != "ssh-key-v1" or challenge.get("namespace") != "agent-registry":
        raise RegistryError("Схема сервера изменилась; поддерживается только ssh-key-v1/agent-registry")
    if not isinstance(challenge.get("nonce"), str) or not challenge["nonce"]:
        raise RegistryError("Нет nonce в challenge")
    if not isinstance(challenge.get("expires_in"), (int, float)) or challenge["expires_in"] <= 0:
        raise RegistryError("Некорректный TTL challenge")


def consent_from(challenge):
    consent = challenge.get("consent")
    if (
        not isinstance(consent, dict)
        or not all(
            isinstance(consent.get(k), str) and consent[k]
            for k in ("text_version", "policy_url", "statement")
        )
        or consent.get("accepted_required") is not True
    ):
        raise RegistryError("Неожиданный формат согласия; отправка остановлена")
    return consent


def utc_timestamp(value):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise RegistryError("Сервер не вернул время в UTC ISO 8601 с Z")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise RegistryError("Некорректное время в ответе сервера") from exc
    if parsed.utcoffset() != UTC.utcoffset(parsed):
        raise RegistryError("Время ответа сервера не в UTC")
    return parsed


@dataclass
class Registration:
    """ID belongs to this successful call in this process, never recovered from organizer APIs."""

    name: str
    version: str
    registration_id: str
    key_fingerprint: str
    response: dict


class Protocol:
    def __init__(self, registry, key):
        self.registry = registry
        self.key = key
        self.current = None

    async def _retry_overload(self, operation):
        # Only explicit overloads are retried. A timeout may already have committed data.
        for attempt in range(5):
            try:
                return await operation()
            except Overloaded as exc:
                if attempt == 4:
                    raise
                await asyncio.sleep(max(exc.seconds, 2**attempt) + random.uniform(0, 1))
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 429 or attempt == 4:
                    raise
                from datetime import datetime
                from email.utils import parsedate_to_datetime

                retry = exc.response.headers.get("Retry-After", "1")
                try:
                    seconds = float(retry)
                except ValueError:
                    seconds = (parsedate_to_datetime(retry) - datetime.now(UTC)).total_seconds()
                await asyncio.sleep(max(seconds, 2**attempt) + random.uniform(0, 1))

    async def register(self, name, version, report, team=None, consent_sha256=None):
        name, version = identity(name), identity(version)
        report_digest = hashlib.sha256(report.encode("utf-8")).hexdigest()
        if team is not None:
            validate_team(team)

        async def once():
            started = time.monotonic()
            challenge = await self.registry.call("create_registration_challenge", {})
            check_challenge(challenge)
            if challenge.get("message_format") != "{nonce}:{name}:{version}:{sha256(harness_report)}":
                raise RegistryError("Формат подписываемого сообщения изменился")
            message = f"{challenge['nonce']}:{name}:{version}:{report_digest}"
            if team is not None:
                consent = consent_from(challenge)
                if consent_sha256 != digest(consent):
                    raise ValueError("Сначала прочитайте policy и подтвердите актуальный consent_sha256")
                validate_team(team, consent_version=consent["text_version"])
                expected = (
                    "{nonce}:{name}:{version}:{sha256(harness_report)}:team-v1:{sha256(canonical_team_json)}"
                )
                if challenge.get("team_message_format") != expected:
                    raise RegistryError("Формат подписи анкеты изменился")
                message += ":team-v1:" + digest(team)
            args = dict(
                name=name,
                version=version,
                harness_report=report,
                challenge_nonce=challenge["nonce"],
                challenge_proof=sign(self.key, message),
                public_key=public_key(self.key),
            )
            if team is not None:
                args["team"] = team
            if time.monotonic() - started >= challenge["expires_in"] - 1:
                raise RegistryError("Challenge истёк; запустите новый чек-ин")
            response = await self.registry.call("register_agent", args)
            # Retain the own response even if subsequent validation detects a protocol change.
            self.last_response = response
            agent = response.get("agent", {})
            proof = agent.get("proof", {})
            expected_fp = fingerprint(public_key(self.key))
            actual_fp = response.get(
                "key_fingerprint", proof.get("key_fingerprint", agent.get("key_fingerprint"))
            )
            rid = agent.get("registration_id")
            if (
                response.get("attested") is not True
                or proof.get("verified") is not True
                or actual_fp != expected_fp
                or agent.get("name") != name
                or agent.get("version") != version
                or not isinstance(rid, str)
                or not re.fullmatch(r"[0-9a-f]{32}", rid)
            ):
                raise RegistryError(
                    "Ответ регистрации не прошёл проверки аттестации/ключа/ID; "
                    "результат на сервере неизвестен, автоматически не повторяем"
                )
            self.current = Registration(name, version, rid, expected_fp, response)
            if team is not None:
                for field in ("team_registered_at", "team_updated_at", "consent_recorded_at"):
                    utc_timestamp(response.get(field, agent.get(field)))
            return self.current

        return await self._retry_overload(once)

    async def operation(self, action, version=None):
        if self.current is None:
            raise ValueError("Нет собственной регистрации в текущем процессе: сначала register")
        if action not in {"heartbeat", "deregister_agent"}:
            raise ValueError("Неизвестная операция")
        if action == "deregister_agent" and version is not None:
            raise ValueError("Для deregister_agent версия запрещена")
        current = self.current
        params = dict(action=action, name=current.name, registration_id=current.registration_id)
        if version is not None:
            params["version"] = identity(version)

        async def once():
            started = time.monotonic()
            challenge = await self.registry.call("create_registration_challenge", params)
            # Operation challenges may omit the registration-only scheme/namespace fields.
            message = challenge.get("message")
            if not isinstance(message, str):
                raise RegistryError("Нет точного message в challenge операции")
            expected = dict(params, nonce=challenge.get("nonce"), protocol="agent-registry-operation-v1")
            expected.setdefault("version", None)
            import json

            try:
                decoded = json.loads(message)
            except ValueError as exc:
                raise RegistryError("Неожиданное message операции") from exc
            # Absent version and JSON null both mean no version update.
            if version is None and "version" not in decoded:
                expected.pop("version")
            if decoded != expected:
                raise RegistryError("Challenge не соответствует собственной операции/ID/версии")
            if time.monotonic() - started >= challenge.get("expires_in", 0) - 1:
                raise RegistryError("Истёк challenge операции")
            args = {k: v for k, v in params.items() if k != "action"}
            args.update(challenge_nonce=challenge["nonce"], challenge_proof=sign(self.key, message))
            result = await self.registry.call(action, args)
            if action == "heartbeat":
                if (
                    result.get("ok") is not True
                    or result.get("registration_id") != current.registration_id
                    or result.get("name") != current.name
                ):
                    raise RegistryError("Heartbeat не подтвердил собственный ID")
                if result.get("version") != (params.get("version") or current.version):
                    raise RegistryError("Heartbeat не подтвердил ожидаемую версию")
                utc_timestamp(result.get("last_seen_at"))
                if version is not None:
                    current.version = params["version"]
            elif result.get("removed") is not True or result.get("name") != current.name:
                raise RegistryError("Удаление не подтверждено")
            else:
                self.current = None
            return result

        return await self._retry_overload(once)
