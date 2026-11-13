"""Validate and publish a task's canonical answer."""

import re
from dataclasses import asdict
from pathlib import Path

from contest_harness.agent.recovery import AttemptProgress
from contest_harness.telemetry.trace import RunTrace, TaskTrace, describe
from contest_harness.workspace.answers import AnswerFormat, Check
from contest_harness.workspace.evidence import CodingEvidence
from contest_harness.workspace.files import atomic_write
from contest_harness.workspace.paths import InvalidToolCall


class AnswerPublisher:
    def __init__(
        self, task_dir: Path, instruction: str, answer_format: AnswerFormat,
        coding: CodingEvidence | None, reference: tuple[str, str] | None,
        current: TaskTrace, trace: RunTrace,
    ):
        self.task_dir = task_dir
        self.answer_format = answer_format
        self.coding = coding
        self.reference = reference
        self.current = current
        self.trace = trace
        self.requires_confirmation = bool(
            (coding and coding.requires_tests)
            or re.search(
                r"\b(?:only if|only after|after (?:verifying|confirming)|before submitting|do not submit (?:unless|until)|"
                r"must (?:verify|confirm)|once (?:verified|confirmed))\b",
                instruction, re.I,
            )
        )

    async def publish(self, progress: AttemptProgress, raw: str, source: str) -> str | None:
        answer, format_check = self.answer_format.validate(raw)
        if format_check.status == "failed":
            semantic = Check("unverified", "Candidate was rejected by the format check.")
        elif self.coding is not None:
            semantic = await self.coding.verify(run_missing=source != "direct")
        elif self.reference is not None:
            value, method = self.reference
            semantic = (
                Check("confirmed", f"Matches independent verification ({method}).") if answer == value
                else Check("failed", f"Candidate disagrees with independent verification ({method}).")
            )
        else:
            semantic = Check("unverified", "No reliable independent check for this instruction.")
        accepted = (
            format_check.status != "failed"
            and semantic.status != "failed"
            and (semantic.status == "confirmed" or not self.requires_confirmation)
            and (source not in {"direct", "local"} or format_check.status == semantic.status == "confirmed")
        )
        self.trace.emit(
            "answer.validated", source=source, accepted=accepted,
            format=asdict(format_check), semantic=asdict(semantic), format_kind=self.answer_format.kind,
            format_valid=format_check.status == "confirmed",
            verification=("SEMANTIC_VERIFIED" if semantic.status == "confirmed" else
                          "UNVERIFIED" if semantic.status == "unverified" else "FAILED"),
        )
        if not accepted:
            reason = format_check.reason if format_check.status == "failed" else semantic.reason
            if source in {"direct", "local"}:
                return None
            progress.candidate = answer
            progress.rejection = reason
            raise InvalidToolCall(f"Answer rejected: {reason}")
        answer_text = answer + "\n"
        atomic_write(self.task_dir / "answer.txt", answer_text)
        self.current.published = True
        progress.submitted = True
        if self.trace.enabled:
            self.trace.tool_event("answer.saved", source=source, status="SUBMITTED", answer=describe(answer_text))
        return "Answer saved."
