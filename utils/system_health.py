"""
Machine-wide system health (issue #744).

Why this exists: on 2026-09-24 09:40:09 IST OpenAlgo died with a libzmq
``Assertion failed: No buffer space available [10055]`` (WSAENOBUFS) abort.
The existing Health Monitor recorded free *system* RAM falling 533 -> 237 MB in
the ten minutes before it — but it only alerted on OpenAlgo's own process
numbers, sent no Telegram, and nothing reported the unclean exit afterwards.
Socket buffers come from the Windows **non-paged pool**, a machine-wide region
that must stay in physical RAM (a bigger page file does not enlarge it), so the
signals that matter are machine-wide, not per-process.

This module is the pure logic the existing 10 s Health Monitor collector calls:

* :func:`read_system` — machine-wide readings. On Windows one
  ``GetPerformanceInfo`` call (psapi, no admin rights) gives free RAM, the
  non-paged pool, commit vs limit and the system handle count.
* :class:`SystemHealthMonitor` — per-rule levels with a sustain window, one
  alert per (rule, level) per IST day, WARN -> CRIT escalation and a recovery
  message. Telegram is gated by ``SYSTEM_HEALTH_ALERTS_ENABLED`` (default OFF
  while thresholds are calibrated); the ``health_alerts`` rows are always
  written.
* :func:`classify_apps` — "free up memory" advice: processes grouped by app,
  ranked by working set (the RAM closing them actually frees — committed
  memory overstates it: the Claude Cowork VM holds ~3 GB committed but ~200 MB
  resident). **Recommend only: nothing here ever terminates a process.**
* :class:`RunStateTracker` — a run-state file marked ``running`` at boot and
  ``clean`` by ``atexit``. ``abort()`` skips ``atexit``, so a ``running`` state
  found at the next boot means the previous run died; the report (last sample,
  lowest free RAM in its final minutes, top memory users, the Windows crash
  record when there is one) feeds a banner and a Telegram message.
"""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess  # noqa: S404  # nosec B404 — only the fixed-argv wevtutil crash-log query, no shell
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone

from utils.logging import get_logger

logger = get_logger(__name__)

IS_WINDOWS = platform.system() == "Windows"
IST = timezone(timedelta(hours=5, minutes=30))
MB = 1024 * 1024

LEVEL_ORDER = {"pass": 0, "warn": 1, "fail": 2}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class SystemHealthConfig:
    """Thresholds. Starting values; calibrate from observed minima (issue #744 step 7)."""

    enabled: bool = True
    alerts_enabled: bool = False
    ram_warn_mb: int = 400
    ram_fail_mb: int = 250
    nonpaged_warn_mb: int = 1024
    nonpaged_fail_mb: int = 1536
    nonpaged_growth_warn_mb_per_h: int = 100
    tcp_warn: int = 2000
    sustain_samples: int = 3
    free_ram_target_mb: int = 1500
    apps_refresh_s: int = 60

    @classmethod
    def from_env(cls) -> SystemHealthConfig:
        return cls(
            enabled=_env_bool("SYSTEM_HEALTH_ENABLED", True),
            alerts_enabled=_env_bool("SYSTEM_HEALTH_ALERTS_ENABLED", False),
            ram_warn_mb=_env_int("SYSTEM_HEALTH_RAM_WARN_MB", 400),
            ram_fail_mb=_env_int("SYSTEM_HEALTH_RAM_CRIT_MB", 250),
            nonpaged_warn_mb=_env_int("SYSTEM_HEALTH_NONPAGED_WARN_MB", 1024),
            nonpaged_fail_mb=_env_int("SYSTEM_HEALTH_NONPAGED_CRIT_MB", 1536),
            nonpaged_growth_warn_mb_per_h=_env_int(
                "SYSTEM_HEALTH_NONPAGED_GROWTH_WARN_MB_PER_H", 100
            ),
            tcp_warn=_env_int("SYSTEM_HEALTH_TCP_WARN", 2000),
            sustain_samples=max(1, _env_int("SYSTEM_HEALTH_SUSTAIN_SAMPLES", 3)),
            free_ram_target_mb=_env_int("SYSTEM_HEALTH_FREE_RAM_TARGET_MB", 1500),
        )


# ---------------------------------------------------------------------------
# Readings
# ---------------------------------------------------------------------------


def _read_performance_info() -> dict | None:
    """Windows ``GetPerformanceInfo`` (psapi). No admin rights needed."""
    if not IS_WINDOWS:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class PERFORMANCE_INFORMATION(ctypes.Structure):  # noqa: N801 — Win32 name
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("CommitTotal", ctypes.c_size_t),
                ("CommitLimit", ctypes.c_size_t),
                ("CommitPeak", ctypes.c_size_t),
                ("PhysicalTotal", ctypes.c_size_t),
                ("PhysicalAvailable", ctypes.c_size_t),
                ("SystemCache", ctypes.c_size_t),
                ("KernelTotal", ctypes.c_size_t),
                ("KernelPaged", ctypes.c_size_t),
                ("KernelNonpaged", ctypes.c_size_t),
                ("PageSize", ctypes.c_size_t),
                ("HandleCount", wintypes.DWORD),
                ("ProcessCount", wintypes.DWORD),
                ("ThreadCount", wintypes.DWORD),
            ]

        info = PERFORMANCE_INFORMATION()
        info.cb = ctypes.sizeof(info)
        if not ctypes.windll.psapi.GetPerformanceInfo(ctypes.byref(info), info.cb):
            return None
        page = info.PageSize
        return {
            "available_mb": info.PhysicalAvailable * page / MB,
            "total_mb": info.PhysicalTotal * page / MB,
            "nonpaged_mb": info.KernelNonpaged * page / MB,
            "commit_mb": info.CommitTotal * page / MB,
            "commit_limit_mb": info.CommitLimit * page / MB,
            "handles": int(info.HandleCount),
        }
    except Exception:
        logger.debug("system_health: GetPerformanceInfo failed", exc_info=True)
        return None


def _count_tcp() -> tuple[int | None, int | None]:
    """(system-wide TCP entries, OpenAlgo's own TCP entries)."""
    import psutil

    system_total = own = None
    try:
        system_total = len(psutil.net_connections(kind="tcp"))
    except Exception:
        logger.debug("system_health: system tcp count failed", exc_info=True)
    try:
        proc = psutil.Process()
        getter = getattr(proc, "net_connections", None) or proc.connections
        own = len(getter(kind="tcp"))
    except Exception:
        logger.debug("system_health: own tcp count failed", exc_info=True)
    return system_total, own


def read_system() -> dict:
    """One machine-wide sample. Fields a platform cannot supply are ``None``."""
    import psutil

    reading: dict = {
        "available_mb": None,
        "total_mb": None,
        "nonpaged_mb": None,
        "commit_mb": None,
        "commit_limit_mb": None,
        "commit_pct": None,
        "handles": None,
        "tcp_system": None,
        "tcp_openalgo": None,
        "platform_supported": IS_WINDOWS,
    }
    perf = _read_performance_info()
    if perf:
        reading.update(perf)
    else:
        try:
            vm = psutil.virtual_memory()
            reading["available_mb"] = vm.available / MB
            reading["total_mb"] = vm.total / MB
        except Exception:
            logger.debug("system_health: virtual_memory failed", exc_info=True)
    if reading["commit_mb"] is not None and reading["commit_limit_mb"]:
        reading["commit_pct"] = 100.0 * reading["commit_mb"] / reading["commit_limit_mb"]
    reading["tcp_system"], reading["tcp_openalgo"] = _count_tcp()
    return reading


# ---------------------------------------------------------------------------
# "Free up memory" advice — recommend only, never terminate anything
# ---------------------------------------------------------------------------

# Never listed: Windows itself, security, and anything OpenAlgo depends on.
PROTECTED_NAMES = frozenset(
    {
        "system",
        "registry",
        "idle",
        "system idle process",
        "memory compression",
        "secure system",
        "smss",
        "csrss",
        "wininit",
        "winlogon",
        "services",
        "lsass",
        "lsaiso",
        "svchost",
        "dwm",
        "explorer",
        "fontdrvhost",
        "sihost",
        "ctfmon",
        "spoolsv",
        "audiodg",
        "msmpeng",
        "nissrv",
        "mpdefendercoreservice",
        "securityhealthservice",
        "securityhealthsystray",
        "smartscreen",
        "wmiprvse",
        "runtimebroker",
        "taskhostw",
        "textinputhost",
        "startmenuexperiencehost",
        "shellexperiencehost",
        "searchhost",
        "searchindexer",
        "dllhost",
        "wudfhost",
        "dashost",
        "vmcompute",
        "wslservice",
        "applicationframehost",
    }
)

# (display name, how to free it)
CAREFUL_APPS: dict[str, tuple[str, str]] = {
    "claude": ("Claude app", "Close idle sessions. Keep the one you're using."),
    "vmmem": (
        "Claude Cowork VM (Hyper-V)",
        "Close Cowork sessions if not needed during market hours.",
    ),
    "vmmemwsl": ("WSL VM", "Run 'wsl --shutdown' if WSL isn't needed."),
    "vmwp": ("Hyper-V VM worker", "Stop the VM's app if it isn't needed."),
    "msedgewebview2": ("Edge WebView", "Belongs to other apps. Close those apps."),
    "code": ("VS Code", "Save your work, then close unused windows."),
    "python": ("Other Python processes", "Check what they are (backtests?) before stopping."),
    "pythonw": ("Other Python processes", "Check what they are before stopping."),
    "uv": ("Other Python processes", "Check what they are before stopping."),
    "node": ("Node.js processes", "Check what they are before stopping."),
    "powershell": ("Terminal windows", "Close finished terminals, not the one running OpenAlgo."),
    "pwsh": ("Terminal windows", "Close finished terminals, not the one running OpenAlgo."),
    "cmd": ("Terminal windows", "Close finished terminals, not the one running OpenAlgo."),
    "conhost": ("Terminal windows", "Close finished terminals, not the one running OpenAlgo."),
    "windowsterminal": (
        "Terminal windows",
        "Close finished terminals, not the one running OpenAlgo.",
    ),
    "bash": ("Terminal windows", "Close finished terminals, not the one running OpenAlgo."),
    "docker desktop": ("Docker Desktop", "Quit Docker Desktop if containers aren't needed."),
    "com.docker.backend": ("Docker Desktop", "Quit Docker Desktop if containers aren't needed."),
    "outlook": ("Outlook", "Close it if you don't need email open."),
    "olk": ("Outlook", "Close it if you don't need email open."),
}

SAFE_APPS: dict[str, tuple[str, str]] = {
    "chrome": ("Chrome", "Close unused tabs or quit Chrome."),
    "msedge": ("Microsoft Edge", "Close unused tabs or quit Edge."),
    "firefox": ("Firefox", "Close unused tabs or quit Firefox."),
    "brave": ("Brave", "Close unused tabs or quit Brave."),
    "opera": ("Opera", "Close unused tabs or quit Opera."),
    "whatsapp": ("WhatsApp", "Quit during market hours."),
    "whatsapp.root": ("WhatsApp", "Quit during market hours."),
    "telegram": ("Telegram Desktop", "Quit during market hours."),
    "slack": ("Slack", "Quit during market hours."),
    "teams": ("Microsoft Teams", "Quit during market hours."),
    "ms-teams": ("Microsoft Teams", "Quit during market hours."),
    "discord": ("Discord", "Quit during market hours."),
    "spotify": ("Spotify", "Quit during market hours."),
    "zoom": ("Zoom", "Quit when not in a meeting."),
    "onedrive": ("OneDrive", "Pause sync or quit OneDrive."),
    "lenovovantageservice": ("Lenovo Vantage", "Quit during market hours."),
    "lenovovantage": ("Lenovo Vantage", "Quit during market hours."),
    "radeonsoftware": ("AMD Radeon Software", "Quit the Radeon panel."),
    "acrobat": ("Adobe Acrobat", "Close open PDFs."),
    "acrord32": ("Adobe Acrobat", "Close open PDFs."),
}

UNKNOWN_MIN_MB = 300
SAFE_MIN_MB = 20
CAREFUL_MIN_MB = 50


def _norm(name: str) -> str:
    name = (name or "").strip().lower()
    return name[:-4] if name.endswith(".exe") else name


def classify_apps(
    procs: list[dict], own_pids: set[int], available_mb: float | None, target_mb: int
) -> dict:
    """Group processes by app and rank by working set (``rss_mb``).

    ``procs``: ``[{"pid", "name", "rss_mb"}]``. ``own_pids``: OpenAlgo's own
    process, its children and its ancestors — excluded entirely (closing the
    terminal that runs OpenAlgo would stop it).

    Safe apps are listed largest first and marked ``suggested`` until their
    total would lift free RAM back to ``target_mb``.
    """
    groups: dict[tuple[str, str], dict] = {}
    for p in procs:
        if p.get("pid") in own_pids:
            continue
        key = _norm(p.get("name", ""))
        if not key or key in PROTECTED_NAMES:
            continue
        if key in SAFE_APPS:
            cls, (display, how) = "safe", SAFE_APPS[key]
        elif key in CAREFUL_APPS:
            cls, (display, how) = "careful", CAREFUL_APPS[key]
        else:
            cls, display, how = "unknown", key, "Check what this is before closing."
        g = groups.setdefault(
            (cls, display),
            {"app": display, "class": cls, "how": how, "rss_mb": 0.0, "processes": 0},
        )
        g["rss_mb"] += float(p.get("rss_mb") or 0)
        g["processes"] += 1

    def _sorted(cls: str, floor: int) -> list[dict]:
        rows = [g for g in groups.values() if g["class"] == cls and g["rss_mb"] >= floor]
        rows.sort(key=lambda g: g["rss_mb"], reverse=True)
        return [{**g, "rss_mb": round(g["rss_mb"], 1)} for g in rows]

    safe = _sorted("safe", SAFE_MIN_MB)
    careful = _sorted("careful", CAREFUL_MIN_MB)
    unknown = _sorted("unknown", UNKNOWN_MIN_MB)

    need = None if available_mb is None else max(0.0, target_mb - available_mb)
    freed = 0.0
    for g in safe:
        g["suggested"] = bool(need) and freed < need
        if g["suggested"]:
            freed += g["rss_mb"]
            g["reaches_target"] = freed >= need
    return {
        "available_mb": None if available_mb is None else round(available_mb, 1),
        "target_mb": target_mb,
        "need_mb": None if need is None else round(need, 1),
        "safe": safe,
        "careful": careful,
        "unknown": unknown,
        "top": sorted(
            (
                {"app": g["app"], "rss_mb": round(g["rss_mb"], 1), "class": g["class"]}
                for g in groups.values()
            ),
            key=lambda g: g["rss_mb"],
            reverse=True,
        )[:6],
    }


def collect_processes() -> tuple[list[dict], set[int]]:
    """All processes' working sets plus OpenAlgo's own pid set (self, children, ancestors)."""
    import psutil

    procs: list[dict] = []
    for proc in psutil.process_iter(attrs=["pid", "name", "memory_info"]):
        try:
            mem = proc.info.get("memory_info")
            if mem is None:
                continue
            procs.append(
                {
                    "pid": proc.info["pid"],
                    "name": proc.info.get("name") or "",
                    "rss_mb": mem.rss / MB,
                }
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    own: set[int] = {os.getpid()}
    try:
        me = psutil.Process()
        own.update(c.pid for c in me.children(recursive=True))
        own.update(p.pid for p in me.parents())
    except Exception:
        logger.debug("system_health: own pid set failed", exc_info=True)
    return procs, own


# ---------------------------------------------------------------------------
# Rules, sustain window, alerting
# ---------------------------------------------------------------------------

RULE_LABELS = {
    "free_ram": "Free RAM",
    "nonpaged": "Non-paged pool",
    "tcp": "System TCP connections",
}


@dataclass
class _RuleState:
    active: str = "pass"
    pending: str = "pass"
    count: int = 0
    alerted: bool = False  # an alert went out during the current episode


@dataclass
class SystemHealthMonitor:
    """Evaluates readings, applies the sustain window and fires alerts."""

    config: SystemHealthConfig = field(default_factory=SystemHealthConfig.from_env)
    notifier: object = None  # callable(event_type, message); None -> notification_service
    db_alerts: bool = True

    def __post_init__(self) -> None:
        self._rules: dict[str, _RuleState] = {r: _RuleState() for r in RULE_LABELS}
        self._sent: set[tuple[str, str, str]] = set()  # (rule, level, IST date)
        self._nonpaged_hist: deque[tuple[float, float]] = deque()
        self._lock = threading.Lock()
        self._apps: dict | None = None
        self._apps_at = 0.0
        self.latest: dict = {}

    # -- growth -------------------------------------------------------------
    def _nonpaged_growth(self, now: float, nonpaged_mb: float | None) -> float | None:
        if nonpaged_mb is None:
            return None
        self._nonpaged_hist.append((now, nonpaged_mb))
        while self._nonpaged_hist and now - self._nonpaged_hist[0][0] > 7200:
            self._nonpaged_hist.popleft()
        # Need at least ~55 min of history before calling a trend.
        base = None
        for t, v in self._nonpaged_hist:
            if now - t <= 3600:
                base = (t, v)
                break
        if base is None or now - self._nonpaged_hist[0][0] < 3300:
            return None
        span_h = (now - base[0]) / 3600
        if span_h <= 0:
            return None
        return (nonpaged_mb - base[1]) / span_h

    # -- raw levels ---------------------------------------------------------
    def raw_levels(
        self, reading: dict, growth: float | None
    ) -> dict[str, tuple[str, float | None]]:
        c = self.config
        out: dict[str, tuple[str, float | None]] = {}
        avail = reading.get("available_mb")
        if avail is None:
            out["free_ram"] = ("pass", None)
        elif avail < c.ram_fail_mb:
            out["free_ram"] = ("fail", avail)
        elif avail < c.ram_warn_mb:
            out["free_ram"] = ("warn", avail)
        else:
            out["free_ram"] = ("pass", avail)
        npg = reading.get("nonpaged_mb")
        if npg is None:
            out["nonpaged"] = ("pass", None)
        elif npg > c.nonpaged_fail_mb:
            out["nonpaged"] = ("fail", npg)
        elif npg > c.nonpaged_warn_mb or (
            growth is not None and growth > c.nonpaged_growth_warn_mb_per_h
        ):
            out["nonpaged"] = ("warn", npg)
        else:
            out["nonpaged"] = ("pass", npg)
        tcp = reading.get("tcp_system")
        if tcp is not None and tcp > c.tcp_warn:
            out["tcp"] = ("warn", float(tcp))
        else:
            out["tcp"] = ("pass", None if tcp is None else float(tcp))
        return out

    # -- main entry ---------------------------------------------------------
    def evaluate(self, reading: dict, now: float | None = None, apps: dict | None = None) -> dict:
        """Apply one sample. Returns the snapshot served to the UI."""
        now = time.time() if now is None else now
        with self._lock:
            growth = self._nonpaged_growth(now, reading.get("nonpaged_mb"))
            raws = self.raw_levels(reading, growth)
            transitions: list[tuple[str, str, str, float | None]] = []
            for rule, (level, value) in raws.items():
                st = self._rules[rule]
                if level == st.pending:
                    st.count += 1
                else:
                    st.pending, st.count = level, 1
                if st.count >= self.config.sustain_samples and st.pending != st.active:
                    transitions.append((rule, st.active, st.pending, value))
                    st.active = st.pending
            if apps is not None:
                self._apps, self._apps_at = apps, now
            status = max((s.active for s in self._rules.values()), key=LEVEL_ORDER.get)
            self.latest = {
                "timestamp": datetime.fromtimestamp(now, UTC).isoformat(),
                "status": status,
                "readings": {**reading, "nonpaged_growth_mb_per_h": growth},
                "rules": {
                    r: {
                        "label": RULE_LABELS[r],
                        "level": self._rules[r].active,
                        "raw": raws[r][0],
                        "value": raws[r][1],
                    }
                    for r in RULE_LABELS
                },
                "thresholds": self.thresholds(),
                "alerts_enabled": self.config.alerts_enabled,
                "recommendations": self._apps,
            }
        for rule, old, new, value in transitions:
            self._on_transition(rule, old, new, value, reading, growth, now)
        return self.latest

    def thresholds(self) -> dict:
        c = self.config
        return {
            "ram_warn_mb": c.ram_warn_mb,
            "ram_fail_mb": c.ram_fail_mb,
            "nonpaged_warn_mb": c.nonpaged_warn_mb,
            "nonpaged_fail_mb": c.nonpaged_fail_mb,
            "nonpaged_growth_warn_mb_per_h": c.nonpaged_growth_warn_mb_per_h,
            "tcp_warn": c.tcp_warn,
            "sustain_samples": c.sustain_samples,
            "free_ram_target_mb": c.free_ram_target_mb,
        }

    def apps_due(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return self._apps is None or now - self._apps_at >= self.config.apps_refresh_s

    # -- alerting -----------------------------------------------------------
    def _on_transition(self, rule, old, new, value, reading, growth, now) -> None:
        st = self._rules[rule]
        day = datetime.fromtimestamp(now, IST).date().isoformat()
        if new == "pass":
            self._record_db(rule, "pass", value)
            if st.alerted:
                st.alerted = False
                self._send(
                    f"RECOVERED system_health · {_ist_hm(now)} IST\n"
                    f"{RULE_LABELS[rule]} back to normal ({_fmt(rule, value)})."
                )
            return
        self._record_db(rule, new, value)
        if LEVEL_ORDER[new] < LEVEL_ORDER[old]:
            return  # fail -> warn: de-escalation is not news
        key = (rule, new, day)
        if key in self._sent:
            return
        self._sent.add(key)
        st.alerted = True
        self._send(self.build_alert(rule, new, reading, growth, now))

    def build_alert(self, rule, level, reading, growth, now) -> str:
        c = self.config
        tag = "CRIT" if level == "fail" else "WARN"
        lines = [f"{tag} system_health · {_ist_hm(now)} IST"]
        avail = reading.get("available_mb")
        if avail is not None:
            lines.append(f"Free RAM {avail:,.0f} MB (warn <{c.ram_warn_mb}, crit <{c.ram_fail_mb})")
        npg = reading.get("nonpaged_mb")
        if npg is not None:
            g = "" if growth is None else f", {growth:+,.0f} MB/h"
            lines.append(f"Non-paged pool {npg:,.0f} MB{g}")
        if rule == "tcp":
            lines.append(f"System TCP {reading.get('tcp_system')} (warn >{c.tcp_warn})")
        tip = self._top_tip()
        if tip:
            lines.append(tip)
        if rule in ("free_ram", "nonpaged"):
            lines.append("Risk: libzmq 10055 abort (2026-09-24 incident). Free memory now.")
        return "\n".join(lines)

    def _top_tip(self) -> str | None:
        apps = self._apps
        if not apps:
            return None
        pick = next((g for g in apps.get("safe", []) if g.get("suggested")), None)
        if pick:
            suffix = " (reaches target)" if pick.get("reaches_target") else ""
            return f"Free now: {pick['app']} {_fmt_mb(pick['rss_mb'])}{suffix}"
        careful = apps.get("careful") or []
        if careful:
            return f"Largest: {careful[0]['app']} {_fmt_mb(careful[0]['rss_mb'])} (check first)"
        return None

    def _send(self, message: str) -> None:
        if not self.config.alerts_enabled:
            logger.info(
                "system_health alert (Telegram off, calibrating): %s", message.replace("\n", " | ")
            )
            return
        logger.warning("system_health alert: %s", message.replace("\n", " | "))
        send_notification(message, self.notifier)

    def _record_db(self, rule: str, level: str, value) -> None:
        if not self.db_alerts:
            return
        try:
            from database.health_db import HealthAlert

            metric = f"system_{rule}"
            if level == "pass":
                HealthAlert.resolve_metric_alerts(metric)
                return
            HealthAlert.create_alert(
                alert_type=f"{metric}_{level}",
                severity=level,
                metric_name=metric,
                metric_value=value,
                threshold_value=None,
                message=f"{RULE_LABELS[rule]} {level}: {_fmt(rule, value)}",
            )
        except Exception:
            logger.exception("system_health: recording health alert failed")


def send_notification(message: str, notifier=None) -> None:
    try:
        if notifier is not None:
            notifier("system_health", message)
            return
        from services.notification_service import get_notification_service

        get_notification_service().notify("system_health", message)
    except Exception:
        logger.exception("system_health: notify failed")


def _ist_hm(now: float) -> str:
    return datetime.fromtimestamp(now, IST).strftime("%H:%M:%S")


def _fmt_mb(mb: float) -> str:
    return f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{mb:,.0f} MB"


def _fmt(rule: str, value) -> str:
    if value is None:
        return "n/a"
    return f"{value:,.0f}" if rule == "tcp" else f"{value:,.0f} MB"


# ---------------------------------------------------------------------------
# Unclean-exit detection
# ---------------------------------------------------------------------------

# wevtutil /f:text separates records with a bare "Event[N]" line.
_EVENT_BLOCK = re.compile(r"(?m)^Event\[\d+\]:?\s*$")


def find_windows_crash(pid: int, since_utc: datetime, runner=subprocess.run) -> dict | None:
    """Look up an Application-log Event 1000 (APPCRASH) for ``pid`` since ``since_utc``.

    Best effort, Windows only, no admin needed. Returns ``None`` when there is
    no record (killed, power loss) or the lookup fails.
    """
    if not IS_WINDOWS or pid is None:
        return None
    stamp = since_utc.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    query = f"*[System[(EventID=1000) and TimeCreated[@SystemTime>='{stamp}']]]"
    try:
        out = runner(
            ["wevtutil", "qe", "Application", f"/q:{query}", "/f:text", "/rd:true", "/c:20"],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        text = out.stdout or ""
    except Exception:
        logger.debug("system_health: wevtutil lookup failed", exc_info=True)
        return None
    want = f"0x{pid:x}"
    for block in _EVENT_BLOCK.split(text):
        if "Faulting process id:" not in block:
            continue
        m_pid = re.search(r"Faulting process id:\s*(0x[0-9a-fA-F]+)", block)
        if not m_pid or m_pid.group(1).lower() != want:
            continue

        def _grab(pattern: str, text: str = block) -> str | None:
            m = re.search(pattern, text)
            return m.group(1).strip() if m else None

        # wevtutil prints the LOCAL time with a trailing "Z"; drop the
        # misleading suffix and keep it as the local wall-clock string.
        local_time = (_grab(r"Date:\s*(\S+)") or "").rstrip("Z") or None
        return {
            "time_local": local_time,
            "application": _grab(r"Faulting application name:\s*([^,\r\n]+)"),
            "module": _grab(r"Faulting module name:\s*([^,\r\n]+)"),
            "exception_code": _grab(r"Exception code:\s*(0x[0-9a-fA-F]+)"),
        }
    return None


EXCEPTION_MEANINGS = {
    "0x40000015": "abort() — e.g. a failed libzmq assertion (the 10055 crash)",
    "0xc0000409": "fast-fail / stack buffer overrun (a native module aborted)",
    "0xc0000005": "access violation in a native module",
}


class RunStateTracker:
    """Detects that the previous run ended without a clean shutdown."""

    def __init__(self, state_dir: str):
        self.state_path = os.path.join(state_dir, "health_run_state.json")
        self.report_path = os.path.join(state_dir, "health_unclean_exit.json")
        self.pending_alert: dict | None = None
        self._previous: dict | None = None
        self.boot_time = time.time()

    # -- lifecycle ------------------------------------------------------------
    def begin(self, sample_reader=None) -> dict | None:
        """Call once at boot, BEFORE the collector writes this run's first sample.

        Only cheap DB reads happen here; the Windows crash-log lookup is
        deferred to :meth:`take_pending_alert` on the collector thread so it can
        never delay boot.
        """
        previous = self._read(self.state_path)
        report = None
        if previous and previous.get("status") == "running" and previous.get("pid") != os.getpid():
            self._previous = previous
            report = self._build_report(previous, sample_reader)
            self._write(self.report_path, report)
            self.pending_alert = report
            logger.warning(
                "system_health: previous run (pid %s, started %s) ended without a clean shutdown",
                previous.get("pid"),
                previous.get("started_at"),
            )
        self._write(
            self.state_path,
            {"status": "running", "pid": os.getpid(), "started_at": datetime.now(UTC).isoformat()},
        )
        return report

    def mark_clean(self) -> None:
        state = self._read(self.state_path) or {}
        if state.get("pid") not in (None, os.getpid()):
            return  # another process owns the file now
        state.update(
            {"status": "clean", "pid": os.getpid(), "stopped_at": datetime.now(UTC).isoformat()}
        )
        self._write(self.state_path, state)

    # -- report -------------------------------------------------------------------
    def _build_report(self, previous, sample_reader) -> dict:
        started = _parse_iso(previous.get("started_at"))
        report = {
            "detected_at": datetime.now(UTC).isoformat(),
            "previous_pid": previous.get("pid"),
            "previous_started_at": previous.get("started_at"),
            "last_sample": None,
            "min_available_mb": None,
            "min_available_at": None,
            "top_apps": None,
            "crash": None,
            "dismissed": False,
        }
        if sample_reader is not None:
            try:
                report.update(sample_reader(started))
            except Exception:
                logger.exception("system_health: reading previous run's samples failed")
        return report

    def current_report(self, max_age_h: int = 24) -> dict | None:
        report = self._read(self.report_path)
        if not report or report.get("dismissed"):
            return None
        detected = _parse_iso(report.get("detected_at"))
        if detected and datetime.now(UTC) - detected > timedelta(hours=max_age_h):
            return None
        return report

    def dismiss(self) -> bool:
        report = self._read(self.report_path)
        if not report:
            return False
        report["dismissed"] = True
        self._write(self.report_path, report)
        return True

    def take_pending_alert(
        self, min_uptime_s: int = 90, crash_finder=find_windows_crash
    ) -> dict | None:
        """Hand the alert to the collector once the Telegram bot has had time to start.

        Runs on the collector thread: looks up the Windows crash record for the
        previous pid first and stores it in the report the banner reads.
        """
        if self.pending_alert is None or time.time() - self.boot_time < min_uptime_s:
            return None
        report, self.pending_alert = self.pending_alert, None
        previous = self._previous or {}
        started = _parse_iso(previous.get("started_at"))
        if crash_finder is not None and started is not None:
            try:
                crash = crash_finder(previous.get("pid"), started)
            except Exception:
                logger.exception("system_health: crash lookup failed")
                crash = None
            if crash:
                code = (crash.get("exception_code") or "").lower()
                crash["meaning"] = EXCEPTION_MEANINGS.get(code)
            report["crash"] = crash
            stored = self._read(self.report_path) or {}
            if stored.get("detected_at") == report.get("detected_at"):
                stored["crash"] = crash
                self._write(self.report_path, stored)
        return report

    @staticmethod
    def format_alert(report: dict) -> str:
        lines = ["CRIT system_health · OpenAlgo restarted after an unclean exit"]
        crash = report.get("crash")
        if crash:
            meaning = f" — {crash['meaning']}" if crash.get("meaning") else ""
            lines.append(
                f"Windows crash record: {crash.get('application')} {crash.get('exception_code')}"
                f" in {crash.get('module')}{meaning}"
            )
        else:
            lines.append("No Windows crash record (killed, power loss, or reboot).")
        last = report.get("last_sample") or {}
        if last.get("at"):
            lines.append(
                f"Last sample {_to_ist_hm(last['at'])} IST: free RAM "
                f"{_num(last.get('available_mb'))}, non-paged pool {_num(last.get('nonpaged_mb'))}"
            )
        if report.get("min_available_mb") is not None:
            lines.append(
                f"Lowest free RAM in its final 5 min: {_num(report['min_available_mb'])}"
                f" at {_to_ist_hm(report.get('min_available_at'))} IST"
            )
        top = report.get("top_apps") or []
        if top:
            lines.append(
                "Top memory: " + ", ".join(f"{a['app']} {_fmt_mb(a['rss_mb'])}" for a in top[:3])
            )
        return "\n".join(lines)

    # -- io -------------------------------------------------------------------------
    @staticmethod
    def _read(path: str) -> dict | None:
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return None
        except Exception:
            logger.exception("system_health: reading %s failed", path)
            return None

    @staticmethod
    def _write(path: str, data: dict) -> None:
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp = f"{path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, path)
        except Exception:
            logger.exception("system_health: writing %s failed", path)


def _parse_iso(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        return None


def _to_ist_hm(value) -> str:
    dt = _parse_iso(value)
    return dt.astimezone(IST).strftime("%H:%M:%S") if dt else "?"


def _num(value) -> str:
    """Whole megabytes with the unit, or ``n/a`` (no unit) when unknown."""
    return "n/a" if value is None else f"{value:,.0f} MB"
