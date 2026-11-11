import argparse
import asyncio
import json
import math
import os
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path

from .crypto import create_key, load_key
from .harness import collect_report
from .protocol import Protocol, consent_from
from .team import digest, load_team
from .transport import connect


def write_private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


async def online(args):
    # Validate and collect before creating network sessions/challenges.
    team = None
    if args.command == "run":
        team = load_team(args.team) if args.team is not None else None
        if team is not None:
            for participant in team["participants"]:
                if any("ЗАМЕНИТЕ:" in v for v in participant.values()) or participant[
                    "email"
                ].strip().lower().split("@")[-1] in {"example.org", "example.com", "example.net"}:
                    raise ValueError("Замените вымышленные данные примера реальными данными участников")
        if team is not None and not args.consent_sha256:
            raise ValueError("Нужен --consent-sha256 из команды policy после подтверждения человеком")
        report, _ = collect_report()
        key = load_key(args.key)
    async with connect() as registry:
        if args.command == "discover":
            result = await registry.discover()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        if args.command == "policy":
            challenge = await registry.call("create_registration_challenge", {})
            consent = consent_from(challenge)
            print(json.dumps(consent, ensure_ascii=False, indent=2))
            print(f"consent_sha256: {digest(consent)}")
            print("Прочитайте политику. accepted=true допустимо только после согласия всех участников.")
            return
        protocol = Protocol(registry, key)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        args.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        # A local lock prevents accidental concurrent check-ins rotating our own ID.
        import portalocker

        with portalocker.Lock(str(args.state_dir / "session.lock"), timeout=0):
            receipt = args.state_dir / "receipt.json"
            events = args.state_dir / "events.jsonl"

            def record(event, result):
                data = dict(event=event, at=datetime.now(UTC).isoformat(), result=result)

                # Successful server responses omit the submitted questionnaire.
                # Drop any unexpected participant/report fields defensively.
                def safe(value):
                    if isinstance(value, dict):
                        return {
                            k: safe(v)
                            for k, v in value.items()
                            if k
                            not in {"team", "participants", "harness_report", "challenge_proof", "public_key"}
                        }
                    return [safe(v) for v in value] if isinstance(value, list) else value

                data = safe(data)
                write_private(receipt, data)
                fd = os.open(events, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as f:
                    f.write(json.dumps(data, ensure_ascii=False) + "\n")
                print(json.dumps(data, ensure_ascii=False), flush=True)

            try:
                registration = await protocol.register(
                    args.name, args.version, report, team, args.consent_sha256
                )
                record("registered", registration.response)
                # Verify signed ownership immediately, then every ~30 minutes.
                record("heartbeat", await protocol.operation("heartbeat"))
                if not args.once:
                    deadline = loop.time() + args.duration if args.duration else None
                    while not stop.is_set():
                        seconds = (
                            min(args.interval, max(0, deadline - loop.time())) if deadline else args.interval
                        )
                        if seconds == 0:
                            break
                        try:
                            await asyncio.wait_for(stop.wait(), timeout=seconds)
                        except TimeoutError:
                            if deadline and loop.time() >= deadline:
                                break
                            record("heartbeat", await protocol.operation("heartbeat"))
            except Exception:
                if hasattr(protocol, "last_response"):
                    record("session_failed", protocol.last_response)
                raise
            finally:
                if protocol.current is not None:
                    # Uses the current process's own ID; never discovers an organizer record.
                    record("deregistered", await protocol.operation("deregister_agent"))


def parser():
    p = argparse.ArgumentParser(description="Регистрация TechArena через настоящий JiuwenSwarm")
    sub = p.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate", help="Локальная проверка JSON без отправки")
    validate.add_argument("team", type=Path)
    validate.add_argument(
        "--allow-unaccepted", action="store_true", help="Проверить черновик с accepted=false"
    )
    keygen = sub.add_parser("keygen", help="Создать отдельный Ed25519 ключ, не перезаписывая существующий")
    keygen.add_argument("--key", type=Path, default=Path(".private/agent-registry-key"))
    sub.add_parser("discover", help="Проверить 4 инструмента MCP без анкеты")
    sub.add_parser("policy", help="Получить актуальный текст согласия и его хеш")
    sub.add_parser("report", help="Отчёт и sha256 из shell настоящего JiuwenSwarm")
    sub.add_parser("runtime-init", help="Настроить официальный JiuwenSwarm внутри проекта")
    sub.add_parser("runtime-start", help="Запустить официальный JiuwenSwarm")
    run = sub.add_parser("run", help="Чек-ин → heartbeat → завершение; только из shell агента")
    run.add_argument("--name", required=True, help="Уникальное постоянное имя агента, привязанное к ключу")
    run.add_argument("--version", default="1.0")
    source = run.add_mutually_exclusive_group(required=True)
    source.add_argument("--team", type=Path, help="Полный JSON команды")
    source.add_argument(
        "--preserve-team", action="store_true", help="Сохранить ранее зарегистрированную анкету"
    )
    run.add_argument(
        "--consent-sha256", help="Хеш актуального согласия из policy после подтверждения человеком"
    )
    run.add_argument("--key", type=Path, default=Path(".private/agent-registry-key"))
    run.add_argument("--state-dir", type=Path, default=Path(".state"))
    run.add_argument("--once", action="store_true", help="Регистрация, один heartbeat и завершение")
    run.add_argument("--interval", type=float, default=1800, help="Секунды между heartbeat")
    run.add_argument("--duration", type=float, default=0, help="0 = до SIGINT/SIGTERM")
    return p


def main():
    args = parser().parse_args()
    try:
        if args.command == "validate":
            team = load_team(args.team, require_consent=not args.allow_unaccepted)
            print(f"JSON корректен. Участников: {len(team['participants'])}; team_sha256: {digest(team)}")
        elif args.command == "keygen":
            print(f"Ключ создан. fingerprint: {create_key(args.key)}")
        elif args.command == "report":
            report, report_digest = collect_report()
            print(report)
            print(report_digest)
        elif args.command in {"runtime-init", "runtime-start"}:
            from .runtime import setup, start

            if args.command == "runtime-init":
                setup()
            else:
                return start()
        else:
            if args.command == "run" and (
                not math.isfinite(args.interval)
                or not math.isfinite(args.duration)
                or args.interval <= 0
                or args.duration < 0
            ):
                raise ValueError("interval должен быть > 0, duration >= 0")
            asyncio.run(online(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        # Transport exceptions can embed request bodies. Avoid displaying PII/keys.
        if isinstance(exc, (ValueError, FileNotFoundError, FileExistsError)):
            print(f"Ошибка: {exc}", file=sys.stderr)
        else:
            from .transport import RegistryError

            message = str(exc) if isinstance(exc, RegistryError) else type(exc).__name__
            print(
                f"Ошибка: {message}. Автоматическая повторная отправка анкеты не выполнена.", file=sys.stderr
            )
        return 1
    return 0
