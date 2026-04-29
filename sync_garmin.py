#!/usr/bin/env python3
"""
sync_garmin.py
Pulls today's Garmin metrics and merges with any manually entered sleep data.
Writes / updates data/health.json in the repo root.
"""

import json
import os
import sys
from datetime import date, timedelta, datetime
from pathlib import Path

DATA_FILE = Path("data/health.json")

# ---------------------------------------------------------------------------
# Garmin fetch
# ---------------------------------------------------------------------------

def fetch_garmin():
    try:
        from garminconnect import Garmin
    except ImportError:
        print("garminconnect not installed — skipping Garmin fetch")
        return {}

    email    = os.environ.get("GARMIN_EMAIL")
    password = os.environ.get("GARMIN_PASSWORD")

    if not email or not password:
        print("GARMIN_EMAIL / GARMIN_PASSWORD not set — skipping Garmin fetch")
        return {}

    print(f"Logging into Garmin as {email}…")
    client = Garmin(email, password)
    client.login()

    today     = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    metrics = {"date": today, "synced_at": datetime.utcnow().isoformat() + "Z"}

    # Body battery / recovery
    try:
        bb = client.get_body_battery(today, today)
        if bb:
            metrics["body_battery"] = bb[0].get("charged")
            metrics["body_battery_drained"] = bb[0].get("drained")
    except Exception as e:
        print(f"body_battery error: {e}")

    # HRV
    try:
        hrv = client.get_hrv_data(today)
        ln  = hrv.get("lastNight", {})
        metrics["hrv_overnight_avg"] = ln.get("avgOvernight")
        metrics["hrv_5min_high"]     = ln.get("highOvernight")
        metrics["hrv_5min_low"]      = ln.get("lowOvernight")
        wk = hrv.get("hrvSummary", {})
        metrics["hrv_weekly_avg"]    = wk.get("weeklyAvg")
        metrics["hrv_status"]        = hrv.get("hrvSummary", {}).get("status")  # BALANCED / UNBALANCED / POOR
    except Exception as e:
        print(f"hrv error: {e}")

    # Resting heart rate
    try:
        rhr = client.get_rhr_day(today)
        metrics["rhr"] = rhr.get("restingHeartRate")
    except Exception as e:
        print(f"rhr error: {e}")

    # Stress
    try:
        stress = client.get_stress_data(today)
        metrics["stress_avg"]  = stress.get("avgStressLevel")
        metrics["stress_rest"] = stress.get("restStressDuration")
        metrics["stress_low"]  = stress.get("lowStressDuration")
        metrics["stress_med"]  = stress.get("mediumStressDuration")
        metrics["stress_high"] = stress.get("highStressDuration")
    except Exception as e:
        print(f"stress error: {e}")

    # Steps
    try:
        steps = client.get_steps_data(today)
        if steps:
            metrics["steps"]      = steps[0].get("totalSteps")
            metrics["steps_goal"] = steps[0].get("stepGoal")
    except Exception as e:
        print(f"steps error: {e}")

    # SpO2
    try:
        spo2 = client.get_spo2_data(today)
        if spo2:
            metrics["spo2_avg"] = spo2[0].get("averageSpO2")
            metrics["spo2_low"] = spo2[0].get("lowestSpO2")
    except Exception as e:
        print(f"spo2 error: {e}")

    # Respiration
    try:
        resp = client.get_respiration_data(today)
        if resp:
            metrics["respiration_avg"] = resp[0].get("avgWakingRespirationValue")
            metrics["respiration_sleep_avg"] = resp[0].get("avgSleepRespirationValue")
    except Exception as e:
        print(f"respiration error: {e}")

    # 7-day HRV trend (for sparkline)
    try:
        trend = []
        for i in range(6, -1, -1):
            d = (date.today() - timedelta(days=i)).isoformat()
            try:
                h = client.get_hrv_data(d)
                val = h.get("lastNight", {}).get("avgOvernight")
                trend.append({"date": d, "hrv": val})
            except Exception:
                trend.append({"date": d, "hrv": None})
        metrics["hrv_7day_trend"] = trend
    except Exception as e:
        print(f"hrv trend error: {e}")

    # ── Nutrition ──────────────────────────────────────────────────────────
    # Garmin stores nutrition under get_nutrition_day() — returns a dict with
    # totalNutritionalIntake and user-set goals.
    try:
        nutrition = client.get_nutrition_day(today)
        intake = nutrition.get("totalNutritionalIntake", {})
        goals  = nutrition.get("nutritionGoals", {})

        metrics["nutrition"] = {
            # Actuals
            "calories":       intake.get("calories"),
            "protein_g":      intake.get("protein"),
            "carbs_g":        intake.get("carbohydrate"),
            "fat_g":          intake.get("fat"),
            "fiber_g":        intake.get("dietaryFiber"),
            "sodium_mg":      intake.get("sodium"),
            "sugar_g":        intake.get("totalSugars"),
            "water_ml":       intake.get("water"),
            "saturated_fat_g":intake.get("saturatedFat"),

            # Garmin user-set targets
            "goal_calories":  goals.get("calories"),
            "goal_protein_g": goals.get("protein"),
            "goal_carbs_g":   goals.get("carbohydrate"),
            "goal_fat_g":     goals.get("fat"),
            "goal_fiber_g":   goals.get("dietaryFiber"),
            "goal_sodium_mg": goals.get("sodium"),
            "goal_water_ml":  goals.get("water"),

            # Logged meals list — used for food quality scoring by Claude
            # Each entry has: name, calories, mealType (BREAKFAST/LUNCH/DINNER/SNACK)
            "meals": [
                {
                    "name":      m.get("name"),
                    "meal_type": m.get("mealType"),
                    "calories":  m.get("calories"),
                    "protein_g": m.get("protein"),
                    "carbs_g":   m.get("carbohydrate"),
                    "fat_g":     m.get("fat"),
                }
                for m in nutrition.get("meals", [])
                if m.get("name")
            ]
        }
        print(f"Nutrition: {metrics['nutrition']['calories']} kcal logged, "
              f"{len(metrics['nutrition']['meals'])} meals")
    except Exception as e:
        print(f"nutrition error: {e}")
        metrics["nutrition"] = {}

    print(f"Garmin fetch complete: {list(metrics.keys())}")
    return metrics


# ---------------------------------------------------------------------------
# Merge with existing file (preserves manual sleep data)
# ---------------------------------------------------------------------------

def load_existing():
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text())
        except Exception:
            pass
    return {}


def merge(existing: dict, fresh: dict) -> dict:
    """
    Garmin data wins for Garmin fields.
    Manual sleep fields (prefixed 'sleep_') are preserved if Garmin
    didn't supply them (Garmin sleep is disabled since user uses Amazfit).
    """
    merged = {**existing, **fresh}

    # Re-stamp date
    merged["date"] = fresh.get("date", date.today().isoformat())

    # Preserve manual sleep fields if present
    for key, val in existing.items():
        if key.startswith("sleep_") and key not in fresh:
            merged[key] = val

    return merged


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)

    existing = load_existing()
    today    = date.today().isoformat()

    # If existing data is from a different day, start fresh (keep nothing)
    if existing.get("date") != today:
        print(f"New day ({today}) — resetting data file")
        existing = {}

    fresh   = fetch_garmin()
    merged  = merge(existing, fresh)

    DATA_FILE.write_text(json.dumps(merged, indent=2))
    print(f"\nWrote {DATA_FILE}:")
    print(json.dumps(merged, indent=2))
