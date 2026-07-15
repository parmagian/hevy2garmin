"""Build a Garmin Connect planned-workout payload from a Hevy routine.

A Hevy *routine* is a template (a plan of exercises/sets/reps), not a logged
session, so it maps to a Garmin **planned workout** in the Training/Workouts
library — not to an uploaded activity. This module converts a routine dict from
``GET /v1/routines`` into the JSON body accepted by Garmin's
``POST /workout-service/workout`` endpoint.

The payload shape is reverse-engineered (Garmin does not publish this API). The
numeric IDs below (``sportType``, ``stepType``, ``endCondition``, ``weightUnit``)
should be confirmed against a real strength workout exported from Garmin Connect
before relying on them in production — see the plan's "spike" step. The exercise
``category``/``exerciseName`` strings come from :func:`mapper.fit_exercise_strings`,
which derives them from the FIT SDK enums fit-tool ships.
"""

from __future__ import annotations

import logging

from hevy2garmin.mapper import fit_exercise_strings, lookup_exercise

logger = logging.getLogger("hevy2garmin")

# --- Reverse-engineered Garmin workout-service enums (confirm via spike) ----- #
SPORT_TYPE_STRENGTH = {"sportTypeId": 5, "sportTypeKey": "strength_training"}

# stepType: warmup sets vs. working sets.
_STEP_TYPE_WARMUP = {"stepTypeId": 1, "stepTypeKey": "warmup"}
_STEP_TYPE_INTERVAL = {"stepTypeId": 3, "stepTypeKey": "interval"}

# endCondition: how a step ends.
_END_REPS = {"conditionTypeId": 10, "conditionTypeKey": "reps"}
_END_TIME = {"conditionTypeId": 2, "conditionTypeKey": "time"}
_END_LAP_BUTTON = {"conditionTypeId": 1, "conditionTypeKey": "lap.button"}

_WEIGHT_UNIT_KG = {"unitId": 8, "unitKey": "kilogram"}
_WEIGHT_UNIT_LB = {"unitId": 7, "unitKey": "pound"}
_KG_TO_LB = 2.2046226218


def _weight_fields(set_data: dict, weight_unit: str) -> dict:
    """Weight fields for a step, or empty when the set has no weight."""
    weight_kg = set_data.get("weight_kg")
    if weight_kg is None:
        return {}
    if weight_unit == "pound":
        return {"weightValue": round(weight_kg * _KG_TO_LB, 2), "weightUnit": _WEIGHT_UNIT_LB}
    return {"weightValue": float(weight_kg), "weightUnit": _WEIGHT_UNIT_KG}


def _build_step(
    order: int,
    set_data: dict,
    exercise_title: str,
    category_str: str | None,
    exercise_name_str: str | None,
    weight_unit: str,
) -> dict:
    """Build one ``ExecutableStepDTO`` for a single Hevy set."""
    is_warmup = (set_data.get("type") or "").lower() == "warmup"
    step: dict = {
        "type": "ExecutableStepDTO",
        "stepOrder": order,
        "stepType": _STEP_TYPE_WARMUP if is_warmup else _STEP_TYPE_INTERVAL,
    }

    reps = set_data.get("reps")
    duration = set_data.get("duration_seconds")
    if reps is not None:
        step["endCondition"] = _END_REPS
        step["endConditionValue"] = float(reps)
    elif duration is not None:
        step["endCondition"] = _END_TIME
        step["endConditionValue"] = float(duration)
    else:
        step["endCondition"] = _END_LAP_BUTTON

    # Garmin identifies the strength exercise by string enums. When we can't map
    # it, fall back to a named step so the user still sees the exercise.
    if category_str is not None:
        step["category"] = category_str
    if exercise_name_str is not None:
        step["exerciseName"] = exercise_name_str
    if exercise_name_str is None:
        step["stepName"] = exercise_title

    step.update(_weight_fields(set_data, weight_unit))
    return step


def routine_to_garmin_workout(routine: dict, *, weight_unit: str = "kilogram") -> dict:
    """Convert a Hevy routine into a Garmin ``/workout-service/workout`` body.

    ``weight_unit`` is ``"kilogram"`` (default) or ``"pound"``; Hevy always
    stores ``weight_kg`` so pounds are converted. Exercises that don't map to a
    known FIT category become generic named steps (logged via ``UNKNOWN`` count).
    """
    exercises = routine.get("exercises") or []
    steps: list[dict] = []
    unknown = 0
    order = 1
    for exercise in exercises:
        title = exercise.get("title") or exercise.get("name") or "Exercise"
        template_id = exercise.get("exercise_template_id")
        category, subcategory, _ = lookup_exercise(title, template_id)
        category_str, exercise_name_str = fit_exercise_strings(category, subcategory)
        if category_str is None:
            unknown += 1
        for set_data in exercise.get("sets") or []:
            steps.append(
                _build_step(order, set_data, title, category_str, exercise_name_str, weight_unit)
            )
            order += 1

    name = routine.get("title") or routine.get("name") or "Hevy Routine"
    if unknown:
        logger.info("  Routine '%s': %d exercise(s) had no Garmin mapping", name, unknown)

    return {
        "workoutName": name,
        "description": (routine.get("notes") or "Synced from Hevy").strip()[:1024],
        "sportType": SPORT_TYPE_STRENGTH,
        "workoutSegments": [
            {
                "segmentOrder": 1,
                "sportType": SPORT_TYPE_STRENGTH,
                "workoutSteps": steps,
            }
        ],
    }
