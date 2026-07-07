"""R48 analysis — reads outputs/tod_volume_gate/results.parquet, prints the full result tables.

Sections:
  1. Baseline anatomy: fire counts, fire-time distribution, binding-gate attribution
  2. Per-K comparison: both/variant-only/baseline-only, latency gains, entry extension
  3. Outcome quality: ret_to_close / ret_to_t1 by arm and fire class
  4. Split-half consistency (2025-07..2025-12 vs 2026-01..2026-07)
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parents[2] / "outputs" / "tod_volume_gate"
df = pd.read_parquet(HERE / "results.parquet")
K_VALUES = [0.75, 1.0, 1.25, 1.5, 2.0, 3.0]
PREFIXES = [f"k{str(k).replace('.', '_')}" for k in K_VALUES]

df["day"] = pd.to_datetime(df["day"])
half_b = df["day"] >= "2026-01-01"

pd.set_option("display.width", 200)
pd.set_option("display.float_format", lambda v: f"{v:.4f}")


def pct(x):
    return f"{100 * x:.1f}%"


def fmt_min(m):
    if pd.isna(m):
        return "-"
    h = int(m // 60)
    mm = int(m % 60)
    return f"{9 + h + (15 + mm) // 60:02d}:{(15 + mm) % 60:02d}"


print("=" * 88)
print("1. BASELINE ANATOMY")
print("=" * 88)
base = df[df["base_ts"].notna()].copy()
print(f"symbol-days recorded (any arm fired): {len(df)}")
print(
    f"baseline fires (true PASS days): {len(base)} "
    f"({len(base) / max(df['day'].nunique(), 1):.2f}/trading day over {df['day'].nunique()} days)"
)
q = base["base_min"].quantile([0.1, 0.25, 0.5, 0.75, 0.9])
print("baseline first-fire minute after open (quantiles):")
for qq, v in q.items():
    print(f"  p{int(qq * 100):02d}: {v:6.0f} min  ({fmt_min(v)} IST)")
print(f"  fires at/after 14:00 IST: {pct((base['base_min'] >= 285).mean())}")
print(f"  fires at/after 15:00 IST: {pct((base['base_min'] >= 345).mean())}")

# attribution: on baseline-fire days, was the volume gate or the other stack the binder?
both_known = base[base["other_ts"].notna()].copy()
vol_bound = (both_known["base_min"] - both_known["other_min"]) > 0
print(f"\nbinding-gate attribution on baseline-fire days (n={len(both_known)}):")
print(f"  volume gate was the late binder (other stack passed earlier): {pct(vol_bound.mean())}")
print(
    f"  median minutes the volume gate delayed beyond the other stack: "
    f"{(both_known['base_min'] - both_known['other_min']).median():.0f}"
)

print()
print("=" * 88)
print("2. PER-K COMPARISON (variant: cumvol >= K*f(t)*SMA50 AND >= K*f(t)*SMA200)")
print("=" * 88)
rows = []
for k, p in zip(K_VALUES, PREFIXES, strict=True):
    has_v = df[f"{p}_ts"].notna()
    has_b = df["base_ts"].notna()
    both = df[has_v & has_b]
    vonly = df[has_v & ~has_b]
    bonly = df[~has_v & has_b]
    gain = both["base_min"] - both[f"{p}_min"]
    rows.append(
        {
            "K": k,
            "fires": int(has_v.sum()),
            "both": len(both),
            "v_only": len(vonly),
            "b_only": len(bonly),
            "gain_med_min": gain.median(),
            "gain_mean_min": gain.mean(),
            "gain>0_pct": 100 * (gain > 0).mean() if len(both) else np.nan,
            "ext_at_fire_v": both[f"{p}_ret_at_fire"].mean() * 100,
            "ext_at_fire_b": both["base_ret_at_fire"].mean() * 100,
        }
    )
print(pd.DataFrame(rows).to_string(index=False))

print()
print("=" * 88)
print("3. OUTCOME QUALITY (signal-time forward returns, %)")
print("=" * 88)
rows = []
b_ = df[df["base_ts"].notna()]
rows.append(
    {
        "arm": "baseline (all fires)",
        "n": len(b_),
        "ret_to_close": b_["base_ret_to_close"].mean() * 100,
        "ret_to_close_med": b_["base_ret_to_close"].median() * 100,
        "win_close": 100 * (b_["base_ret_to_close"] > 0).mean(),
        "ret_to_t1": b_["base_ret_to_t1"].mean() * 100,
        "win_t1": 100 * (b_["base_ret_to_t1"] > 0).mean(),
        "fade_vol_pct": np.nan,
    }
)
for k, p in zip(K_VALUES, PREFIXES, strict=True):
    has_v = df[f"{p}_ts"].notna()
    has_b = df["base_ts"].notna()
    for label, sub in [
        (f"K={k} all fires", df[has_v]),
        (f"K={k} both (earlier entry)", df[has_v & has_b]),
        (f"K={k} VARIANT-ONLY (added)", df[has_v & ~has_b]),
    ]:
        if not len(sub):
            continue
        rows.append(
            {
                "arm": label,
                "n": len(sub),
                "ret_to_close": sub[f"{p}_ret_to_close"].mean() * 100,
                "ret_to_close_med": sub[f"{p}_ret_to_close"].median() * 100,
                "win_close": 100 * (sub[f"{p}_ret_to_close"] > 0).mean(),
                "ret_to_t1": sub[f"{p}_ret_to_t1"].mean() * 100,
                "win_t1": 100 * (sub[f"{p}_ret_to_t1"] > 0).mean(),
                "fade_vol_pct": 100 * (sub["fullday_ratio50"] < 1).mean(),
            }
        )
out = pd.DataFrame(rows)
print(out.to_string(index=False))

print()
print("=" * 88)
print("4. SPLIT-HALF CONSISTENCY (A: 2025-07..12, B: 2026-01..07)")
print("=" * 88)
for k, p in zip(K_VALUES, PREFIXES, strict=True):
    line = [f"K={k}"]
    for name, mask in [("A", ~half_b), ("B", half_b)]:
        sub = df[mask]
        has_v = sub[f"{p}_ts"].notna()
        has_b = sub["base_ts"].notna()
        both = sub[has_v & has_b]
        vonly = sub[has_v & ~has_b]
        gain = (both["base_min"] - both[f"{p}_min"]).median() if len(both) else np.nan
        vq = vonly[f"{p}_ret_to_close"].mean() * 100 if len(vonly) else np.nan
        vq_t1 = vonly[f"{p}_ret_to_t1"].mean() * 100 if len(vonly) else np.nan
        line.append(
            f"{name}: both={len(both)} gain_med={gain:.0f}m vonly={len(vonly)} "
            f"vonly_close={vq:+.2f}% vonly_t1={vq_t1:+.2f}%"
        )
    print("  " + " | ".join(line))

# per-day added load (false-positive pressure on downstream engine)
print()
print("5. ADDED SIGNAL LOAD (fires per trading day)")
n_days = df["day"].nunique()
print(f"  baseline: {df['base_ts'].notna().sum() / n_days:.2f}")
for k, p in zip(K_VALUES, PREFIXES, strict=True):
    print(
        f"  K={k}: {df[f'{p}_ts'].notna().sum() / n_days:.2f} "
        f"(added vs baseline: {(df[f'{p}_ts'].notna().sum() - df['base_ts'].notna().sum()) / n_days:+.2f})"
    )
