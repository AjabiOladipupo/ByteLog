"""Dataclasses shared between storage, collector, reports and UI.

This module is the contract between layers: query functions return these types and
the UI and reports consume them. Keep it free of logic and of Qt imports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum


class Period(StrEnum):
    TODAY = "today"
    WEEK = "week"
    MONTH = "month"
    ALL = "all"


@dataclass(frozen=True)
class Network:
    id: int
    kind: str  # wifi | ethernet | cellular | wifi_unknown
    identity: str
    ssid: str | None
    adapter_name: str | None
    user_label: str | None
    is_metered: bool
    first_seen_utc: int
    last_seen_utc: int

    @property
    def label(self) -> str:
        if self.user_label:
            return self.user_label
        if self.kind == "wifi" and self.ssid:
            return self.ssid
        if self.kind == "wifi_unknown":
            return f"Wi-Fi (name unavailable) · {self.adapter_name or '?'}"
        return self.adapter_name or self.identity


@dataclass(frozen=True)
class Adapter:
    id: int
    guid: str
    name: str
    description: str
    kind: str
    include_override: bool | None  # None = follow the default rule
    first_seen_utc: int
    last_seen_utc: int


@dataclass(frozen=True)
class Bucket:
    """One bar in a chart."""

    label: str  # x-axis label, e.g. "08", "Mon 14", "Mar 26"
    tooltip_title: str  # e.g. "08:00–10:00", "Monday 14 Oct 2026", "March 2026"
    start: datetime | date
    rx: int
    tx: int

    @property
    def total(self) -> int:
        return self.rx + self.tx


@dataclass(frozen=True)
class NetworkUsage:
    network_id: int
    label: str
    kind: str
    is_metered: bool
    rx: int
    tx: int

    @property
    def total(self) -> int:
        return self.rx + self.tx


@dataclass(frozen=True)
class PeriodSummary:
    period: Period
    start: date  # first local day included
    end: date  # last local day included (today for current periods)
    rx: int
    tx: int
    buckets: list[Bucket]
    average_title: str  # "Busiest 2-hour slot" | "Daily average" | "Monthly average"
    average_bytes: int
    average_detail: str  # e.g. "18:00–20:00" or "over 17 days"
    per_network: list[NetworkUsage] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.rx + self.tx


@dataclass(frozen=True)
class DailyRow:
    day: date
    network_id: int
    network_label: str
    rx: int
    tx: int

    @property
    def total(self) -> int:
        return self.rx + self.tx


@dataclass(frozen=True)
class Cycle:
    start: date  # inclusive
    end: date  # exclusive

    @property
    def last_day(self) -> date:
        from datetime import timedelta

        return self.end - timedelta(days=1)


@dataclass(frozen=True)
class Plan:
    id: int
    name: str
    cap_bytes: int
    cycle_type: str  # monthly_on_day | rolling_days
    cycle_value: int
    anchor_date: date | None
    thresholds: list[int]
    active: bool
    network_ids: list[int]  # empty = all networks


@dataclass(frozen=True)
class PlanStatus:
    plan: Plan
    cycle: Cycle | None  # None if a rolling plan has not started yet
    used: int
    remaining: int  # max(cap - used, 0)
    percent: float  # uncapped, e.g. 112.0
    days_left: int  # includes today; >= 1 inside a cycle
    daily_allowance: int  # remaining // days_left
    crossed: list[int]  # thresholds crossed in this cycle, ascending
    over_by: int  # max(used - cap, 0)


@dataclass(frozen=True)
class CollectorStatus:
    pid: int | None
    state: str  # running | paused | stopped
    started_utc: int | None
    heartbeat_utc: int | None
    beat: int
    app_version: str | None
    last_error: str | None
