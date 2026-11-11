"""Infer the requested answer format and validate candidates."""

import re
from dataclasses import dataclass

from contest_harness.json_codec import strict_json_loads

MAX_ANSWER_BYTES = 4 * 1024 * 1024
INTEGER = r"-?(?:0|[1-9][0-9]*)"


@dataclass(frozen=True)
class Check:
    status: str
    reason: str


@dataclass(frozen=True)
class AnswerFormat:
    kind: str
    literal: str | None = None
    keys: tuple[str, ...] = ()

    @classmethod
    def infer(cls, instruction):
        if re.search(r"\b(?:otherwise|else)\s*[,;:]?\s*(?:write|submit|return|output|respond)\b", instruction, re.I):
            return cls("text")
        literal = re.search(
            r"\b(?:write|return|submit|output|respond with)\s+(?:only\s+)?(?:the\s+)?(?:word|literal|token)\s+"
            r"(?:`([^`]+)`|\"([^\"]+)\"|'([^']+)'|([\w-]+))",
            instruction, re.I,
        )
        if literal:
            return cls("literal", next(value for value in literal.groups() if value is not None))
        if re.search(
            r"\b(?:write|return|submit|output|respond with)\s+(?:only\s+|just\s+)?(?:(?:the|an?)\s+)?"
            r"(?:final\s+)?integer\b|\b(?:result|answer)\s+as\s+(?:an?\s+)?integer\b",
            instruction, re.I,
        ):
            return cls("integer")
        structured = re.search(
            r"\b(?:write|return|submit|output|respond with)\s+(?:only\s+)?(?:(?:the|an?)\s+)?JSON\b"
            r"(?:\s+(object|array)\b)?", instruction, re.I,
        )
        if structured:
            kind = "json_" + structured[1].lower() if structured[1] else "json"
            key_list = re.match(r"\s+with (?:the )?(?:required )?(?:keys|fields)\s+(\[[^\n]*?\])",
                                instruction[structured.end():], re.I)
            if key_list:
                try:
                    keys = strict_json_loads(key_list[1])
                except ValueError:
                    keys = None
                if isinstance(keys, list) and all(isinstance(key, str) for key in keys):
                    return cls("json_object", keys=tuple(keys))
            return cls(kind)
        if re.search(
            r"\b(?:write|return|submit|output)\s+(?:only|just)\s+(?:the\s+)?"
            r"(?:final\s+)?(?:answer|value|name|string)\b|\bsingle[- ]line\b",
            instruction, re.I,
        ) and not re.search(r"\b(?:multiline|multiple lines|one per line)\b", instruction, re.I):
            return cls("line")
        bare_literal = re.search(
            r"\b(?:write|return|submit|output)\s+(?:only\s+)?(?:the\s+)?[\x60\"]?"
            r"(?-i:([A-Z][A-Z_0-9]+))[\x60\"]?(?:\s+to\s+answer\.txt(?: in the current directory)?)?\s*(?:[.!]|$)",
            instruction, re.I,
        )
        if bare_literal:
            return cls("literal", bare_literal[1])
        return cls("text")

    def validate(self, raw):
        answer = raw.removesuffix("\n")
        if not answer.strip() or any(ord(c) < 32 and c not in "\n\t" for c in answer):
            return answer, Check("failed", "Answer must be nonblank text without control characters.")
        try:
            size = len(answer.encode("utf-8"))
        except UnicodeError:
            size = MAX_ANSWER_BYTES + 1
        if size > MAX_ANSWER_BYTES:
            return answer, Check("failed", "Answer is too large or is invalid UTF-8.")
        if self.kind == "integer" and not re.fullmatch(INTEGER, answer):
            return answer, Check("failed", "Expected only a decimal integer.")
        if self.kind == "literal" and answer != self.literal:
            return answer, Check("failed", f"Expected exactly {self.literal}.")
        if self.kind == "line" and (
            len(answer.splitlines()) != 1 or answer != answer.strip()
            or answer.startswith(("`", "**", "- ", "> ", "Answer:"))
        ):
            return answer, Check("failed", "Expected only the requested value on one line.")
        if self.kind.startswith("json"):
            try:
                value = strict_json_loads(answer)
            except (ValueError, RecursionError):
                return answer, Check("failed", "Expected valid JSON with unique object keys.")
            if (self.kind == "json_object" and not isinstance(value, dict)
                    or self.kind == "json_array" and not isinstance(value, list)):
                return answer, Check("failed", "JSON value has the wrong top-level type.")
            if self.keys and any(key not in value for key in self.keys):
                return answer, Check("failed", "JSON object is missing a required field.")
        if self.kind == "text":
            return answer, Check("unverified", "No supported exact answer format was specified.")
        return answer, Check("confirmed", "Candidate matches the format specified in the instruction.")
