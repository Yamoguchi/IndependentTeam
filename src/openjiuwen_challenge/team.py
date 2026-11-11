"""Validate without mutating the object: the server hashes the original input."""

import hashlib
import json
import re
import unicodedata
from pathlib import Path


def canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def _object(value, fields, path):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError(f"{path}: обязательны ровно поля {', '.join(fields)}")


def _string(value, limit, path):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path}: требуется непустая строка")
    if any(unicodedata.category(c) == "Cc" for c in value):
        raise ValueError(f"{path}: управляющие символы запрещены")
    if len(value.strip()) > limit:
        raise ValueError(f"{path}: максимум {limit} символов")


def validate_team(team, *, require_consent=True, consent_version=None):
    _object(team, ("team_name", "participants", "consent"), "team")
    _string(team["team_name"], 128, "team.team_name")
    members = team["participants"]
    if not isinstance(members, list) or not 2 <= len(members) <= 3:
        raise ValueError("team.participants: требуется 2–3 участника")
    limits = dict(full_name=256, email=254, telegram=64, phone=32, university=256, city=256, citizenship=256)
    for i, member in enumerate(members):
        path = f"team.participants[{i}]"
        _object(member, limits, path)
        for key, limit in limits.items():
            _string(member[key], limit, f"{path}.{key}")
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", member["email"].strip()):
            raise ValueError(f"{path}.email: неверный формат")
        if not re.fullmatch(r"@?[A-Za-z][A-Za-z0-9_]{4,31}", member["telegram"].strip()):
            raise ValueError(f"{path}.telegram: неверный формат")
    consent = team["consent"]
    _object(consent, ("accepted", "text_version"), "team.consent")
    if type(consent["accepted"]) is not bool:
        raise ValueError("team.consent.accepted: требуется boolean")
    if require_consent and consent["accepted"] is not True:
        raise ValueError("team.consent.accepted: необходимо явное согласие отправителя")
    _string(consent["text_version"], 256, "team.consent.text_version")
    if consent_version is not None and consent["text_version"].strip() != consent_version:
        raise ValueError("Версия согласия изменилась: прочитайте policy и подтвердите заново")
    return team


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON: повторяющийся ключ {key}")
        result[key] = value
    return result


def load_team(path: Path, *, require_consent=True):
    def invalid_constant(value):
        raise ValueError(f"JSON: недопустимое значение {value}")

    team = json.loads(
        path.read_text(encoding="utf-8"), object_pairs_hook=_pairs, parse_constant=invalid_constant
    )
    return validate_team(team, require_consent=require_consent)
