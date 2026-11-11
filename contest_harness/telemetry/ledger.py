"""Shared token reservations for requests running on one event loop."""

import asyncio

from contest_harness.telemetry.usage import CallEstimate, Usage


class BudgetLedger:
    def __init__(self, limit_tenths: int | None):
        self.usage = Usage()
        self.limit_tenths = limit_tenths
        self.in_flight_tenths = 0
        self.reservation_breached = False
        self._settled = asyncio.Event()

    @property
    def remaining_tenths(self) -> int | None:
        if self.limit_tenths is None:
            return None
        return self.limit_tenths - self.occupied_tenths

    @property
    def occupied_tenths(self) -> int:
        return self.usage.measured_tenths + self.usage.held_tenths + self.in_flight_tenths

    def may_fit_after_settlement(self, estimate: CallEstimate) -> bool:
        remaining = self.remaining_tenths
        return remaining is not None and remaining < estimate.tenths <= remaining + self.in_flight_tenths

    async def wait_for_settlement(self) -> None:
        await self._settled.wait()

    def reserve(self, estimate: CallEstimate) -> bool:
        remaining = self.remaining_tenths
        if self.reservation_breached or (remaining is not None and estimate.tenths > remaining):
            return False
        self.in_flight_tenths += estimate.tenths
        return True

    def settle(self, estimate: CallEstimate | None, usage: Usage) -> None:
        self.usage.add(usage)
        if estimate is not None:
            if self.limit_tenths is not None and usage.measured_tenths + usage.held_tenths > estimate.tenths:
                self.reservation_breached = True
            self.in_flight_tenths -= estimate.tenths
            self._settled.set()
            self._settled = asyncio.Event()

    def report(self) -> dict[str, int | None]:
        occupied = self.occupied_tenths
        return {
            "local_limit_tenths": self.limit_tenths,
            "in_flight_tenths": self.in_flight_tenths,
            "occupied_tenths": occupied,
            "remaining_tenths": self.remaining_tenths,
            "local_overrun_tenths": (
                max(0, occupied - self.limit_tenths) if self.limit_tenths is not None else None
            ),
        }
