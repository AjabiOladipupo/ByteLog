# ByteLog architecture and module contract

This file is the contract between modules. Signatures here are binding: if you own a
module, implement exactly these names; if you consume one, code against them.

Already written (shared foundation; extend carefully, don't rename):
`paths.py`, `logging_setup.py`, `units.py` (`format_bytes`), `timeutil.py`,
`storage/db.py` (`connect`, `open_db`, `write_tx`), `storage/schema.py` (DDL + `migrate`),
`storage/settings.py` (typed accessors + `DEFAULTS`), `storage/models.py` (all shared
dataclasses), `platform/base.py` (`Platform` protocol), `platform/__init__.py`
(`get_platform`), `ui/theme.py` (tokens + QSS), `ui/widgets.py`, `ui/context.py`.

## Processes

* **Collector** (`python -m bytelog.collector`, `ByteLogCollector.exe`): headless. Holds a
  named mutex (`Global\ByteLog.Collector.<user SID>`). Loop: wait `interval` on the stop
  event → read control settings → `Sampler.tick()` (one `BEGIN IMMEDIATE` transaction:
  sample rows + `collector_state` + `collector_status` heartbeat) → `run_rollup()` (each
  tick; cheap, idempotent) → `check_alerts()` (each tick) → `run_retention()` (hourly).
  Every step is wrapped in try/except + log; one bad tick never stops the loop.
* **Dashboard** (`python -m bytelog.ui`, `ByteLog.exe`): PySide6 window + tray. Reads the
  DB; writes only settings, plans/plan_networks, `networks.user_label/is_metered` and
  `adapters.include_override`. Single instance via `QLocalServer`.

## Control protocol (settings table, dashboard → collector)

| key | writer | meaning |
|---|---|---|
| `collection_paused` = 1/0 | tray | collector keeps reading counters and advancing baselines but writes no samples; heartbeat state = `paused`. Paused traffic is intentionally not counted. |
| `stop_requested` = token | Settings | collector exits cleanly, sets state `stopped`, clears the key. `Platform.signal_stop()` also sets the named stop event for an instant wake-up. |
| `reset_requested` = token | Settings | collector runs `maintenance.reset_usage()` in one transaction, then writes `reset_done` = same token. If no collector is running the dashboard calls `reset_usage()` itself. `reset_forget_networks`=1 also deletes networks, adapters baselines and plans. |

Collector status for the UI: `repo.read_collector_status(conn)`. Stale rule (UI): stale if
`heartbeat_utc` older than `max(60, 3*interval)` s **or** `beat` has not changed for 60 s of
the dashboard's own monotonic clock. States shown: running (green), paused (amber),
stopped/stale (red + Start button).

## Data rules

* `samples` hold deltas. Rollups select by `id > rollup_state.last_sample_id` and upsert
  additively into `usage_15m` (`bucket_utc = ts_utc - ts_utc % 900`) and `daily`
  (`day_local = date(ts_utc + utc_offset_s, 'unixepoch')`), then advance the watermark —
  all in one transaction. Running twice is a no-op.
* Retention: delete samples older than 7 days **and** `id <= watermark`; `usage_15m` older
  than 400 days; `daily` never.
* Period queries never read `samples` — rollups run every tick, so `usage_15m`/`daily` lag
  by at most one interval.
* Counter decrease = reset: delta = the new raw value (bytes since the counter restarted),
  subject to a plausibility cap (`span_s * link_speed * 1.25`, fallback 10 Gbit/s; above it
  → store 0, rebaseline, flag 16). First sight of an adapter = baseline only.
* Sleep gap: wall-clock elapsed > `max(3*interval, 60)` → flag 1, whole delta at wake time.
* Clock moved back (wall elapsed < 0 or |wall - monotonic| > 5 s within a run): never
  clamp; store the true `now`, `span_s = max(0, wall elapsed)`, flag 4, log.
* Plans count rx+tx over `daily` rows in `[cycle.start, cycle.end)` for the plan's
  networks (no rows in `plan_networks` = all networks).

## Module ownership and public API

### platform/ (collector agent)
* `platform/windows.py` — `class WindowsPlatform` implementing `Platform`: ctypes
  `GetIfTable2` for metadata+counters (join key `InterfaceGuid`; kind from `Type`
  6→ethernet, 71→wifi, 243/244→cellular; `is_hardware` from `InterfaceAndOperStatusFlags`
  bit0 and not FilterInterface/EndPointInterface; Bluetooth via `PhysicalMediumType` 10 →
  kind other), WLAN API (`WlanOpenHandle/WlanEnumInterfaces/WlanQueryInterface` opcode 7)
  for raw SSID bytes, `CreateMutexW` lock, named auto-reset event for stop, Windows-Toasts
  with AUMID `ByteLog.App` registered under HKCU (fallback: return False), detached
  `launch_collector`, autostart via `schtasks /create /xml` (UTF-16 XML: logon trigger for
  current user, InteractiveToken, LeastPrivilege, no battery stop, ExecutionTimeLimit PT0S,
  IgnoreNew, restart on failure) with HKCU Run fallback. Every subprocess uses
  `CREATE_NO_WINDOW` and a timeout. If `GetIfTable2` fails, psutil counters are used,
  re-keyed to the same `InterfaceGuid` (alias map from the last native read, else
  `ConvertInterfaceAliasToLuid`; unmappable names skipped), and the native call is retried
  every 60 s. `ssid_refresh_s = 0`: the WLAN query is cheap, so it is asked on every Wi-Fi
  sample; only the slow profile-name fallback is cached inside the platform.
* `platform/fake.py` — `class FakePlatform` for tests: scripted snapshots, settable
  boot time, SSIDs, records notifications in `.notifications: list[tuple[str, str]]`.

### collector/ (collector agent unless noted)
* `collector/filter.py` — `should_count(adapter: AdapterInfo, patterns: list[str],
  override: bool | None) -> bool` (override wins; else hardware + kind in
  wifi/ethernet/cellular + no pattern matches name/description).
* `collector/delta.py` — pure: `@dataclass Baseline(rx, tx, ts_utc, boot_time_utc)`,
  `@dataclass DeltaResult(rx, tx, span_s, flags, rebaseline_reason: str | None)`,
  `compute_delta(prev: Baseline | None, cur: Counters, now_utc: int, boot_time_utc: int,
  interval_s: int, speed_bps: int | None, mono_elapsed_s: float | None = None) -> DeltaResult`.
  Flag constants `FLAG_GAP=1, FLAG_RESET=2, FLAG_CLOCK=4, FLAG_SSID_UNKNOWN=8, FLAG_CAPPED=16`.
  Reboots across restarts are detected by `rebooted_since` (uptime went down, or the boot
  lies after the baseline), not by comparing boot timestamps, which wall-clock steps move.
* `collector/sampler.py` — `class Sampler(conn, platform, tz_provider)` with
  `tick(now_utc: int | None = None, paused: bool = False) -> int` (rows written). Caches
  SSIDs per adapter GUID; re-queries when connection state changes, after a gap, or once
  the platform's `ssid_refresh_s` has passed (0 on Windows = every sample, 300 in
  `FakePlatform`); on failure reuses the last network for 5 min then uses `wifi_unknown`.
* `collector/service.py` — `run(conn, platform, *, once: bool = False) -> int` main loop +
  control flags.
* `collector/__main__.py` — `main(argv: list[str] | None = None) -> int`; flags
  `--data-dir`, `--once`, `--stop`, `--debug`.
* `collector/alerts.py` (**rollups agent**) — `check_alerts(conn, platform, now_utc: int,
  tz) -> list[str]` (messages shown), `format_alert(plan_name, threshold, used, cap,
  days_left) -> str` → `"MiFi plan: 80% used (24.0 of 30.0 GB), 7 days left."`.

### storage/
* `storage/repo.py` (collector agent) — `upsert_adapter(conn, info: AdapterInfo, now_utc)
  -> int`, `upsert_network(conn, kind, identity, ssid, adapter_name, now_utc) -> int`,
  `list_networks(conn) -> list[Network]`, `update_network(conn, network_id, *,
  user_label: str | None = ..., is_metered: bool | None = None) -> None`,
  `list_adapters(conn) -> list[Adapter]`, `set_adapter_override(conn, adapter_id,
  include: bool | None) -> None`, `read_collector_status(conn) -> CollectorStatus`,
  `write_heartbeat(conn, *, pid, state, version, last_error=None) -> None` (caller holds tx).
* `storage/maintenance.py` (collector agent) — `reset_usage(conn, forget_networks_and_plans:
  bool = False) -> None`, `backup_database(conn, dest: Path) -> None`.
* `storage/rollup.py` (rollups agent) — `run_rollup(conn) -> int` (samples consumed).
* `storage/retention.py` (rollups agent) — `run_retention(conn, now_utc: int) -> dict[str, int]`.
* `storage/periods.py` (rollups agent) — `period_range(period: Period, today: date,
  week_start: int) -> tuple[date, date]`; `summarize(conn, period: Period, *, today: date |
  None = None, tz=None, week_start: int | None = None, network_ids: list[int] | None = None)
  -> PeriodSummary` (TODAY: 12 two-hour local buckets from `usage_15m`, totals from
  `daily`; WEEK/MONTH: one bucket per day incl. zero days; ALL: one bucket per month from
  the first month with data); `daily_rows(conn, start, end, network_ids=None) ->
  list[DailyRow]`; `daily_totals(conn, start, end, network_ids=None) -> list[Bucket]` (one
  per day, zero-filled); `network_usage(conn, start, end) -> list[NetworkUsage]` (sorted by
  total desc); `today_total(conn, tz=None) -> int`; `date_range_label(start, end) -> str`.
* `storage/billing.py` (rollups agent) — pure: `cycle_for(cycle_type, cycle_value,
  anchor: date | None, today: date) -> Cycle | None`, `days_left(cycle, today) -> int`,
  `daily_allowance(remaining, days_left) -> int`.
* `storage/plans.py` (rollups agent) — `list_plans(conn, active_only=False) -> list[Plan]`,
  `get_plan(conn, plan_id) -> Plan | None`, `create_plan(conn, *, name, cap_bytes,
  cycle_type, cycle_value, anchor_date=None, thresholds=(80, 100), network_ids=(),
  active=True) -> int`, `update_plan(conn, plan_id, **same fields) -> None` (clears
  `alerts_sent` rows of the current cycle whose threshold is no longer crossed),
  `delete_plan(conn, plan_id) -> None`, `plan_status(conn, plan, today: date | None =
  None, tz=None) -> PlanStatus`, `parse_thresholds(text) -> list[int]` (raises ValueError).

### reports/ (reports agent; must not import PySide6)
* `reports/csv_export.py` — `export_csv(conn, path: Path, start: date, end: date,
  network_ids: list[int] | None = None) -> int` (rows written). Columns: date, network,
  download_bytes, upload_bytes, total_bytes, download, upload, total (formatted).
* `reports/pdf_report.py` — `export_pdf(conn, path: Path, start: date, end: date,
  network_ids: list[int] | None = None) -> None`: summary, stacked bar chart (reportlab
  graphics, same colours), daily table, per-network breakdown; IBM Plex via
  `reports/fonts.py: register_fonts() -> tuple[str, str]` (sans, mono names; falls back to
  Helvetica/Courier).

### ui/ (two agents)
* Shell agent: `ui/app.py` (`main()`), `ui/__main__.py`, `ui/fonts.py` (`load_fonts()`),
  `ui/main_window.py`, `ui/sidebar.py`, `ui/tray.py`, `ui/charts/stacked_bar.py`
  (`StackedBarChart(QWidget)` with `set_buckets(list[Bucket])`, hover tooltips,
  `tallest() -> Bucket | None`), `ui/pages/overview.py` (`OverviewPage`).
* Pages agent: `ui/pages/networks.py` (`NetworksPage`), `ui/pages/plans.py`
  (`PlansPage`), `ui/pages/reports.py` (`ReportsPage`), `ui/pages/settings.py`
  (`SettingsPage`).
* Every page: `class XPage(QWidget)`, `__init__(self, ctx: AppContext)`, `refresh(self) ->
  None` (cheap; called on show and every 10 s while visible). Pages have objectName
  `Page`, a header row (`QLabel#PageTitle` + muted subtitle), and use `Card` etc. from
  `ui/widgets.py`. Colours only from `ui/theme.tokens()`.

### Assets
`src/bytelog/assets/fonts/` (IBM Plex Sans Regular/Medium/SemiBold, Mono Regular/Medium
TTF + `OFL.txt`), `src/bytelog/assets/icon.ico`, `icon.png`. Resolve with
`paths.resource_path(...)`.
