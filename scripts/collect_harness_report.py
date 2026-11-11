"""Protocol-compatible local collector (not an upstream organizer-supplied script)."""

from openjiuwen_challenge.harness import collect_report

report, sha256 = collect_report()
print(report)
print(sha256)
