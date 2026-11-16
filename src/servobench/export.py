"""Fitted servo parameters in a form a simulator can load.

Two files, written by ``characterize``:

``servo_model.json``
    The fitted parameters of ``servobench.model``, each with its unit, its
    status (identifiable or not), its value and one standard uncertainty when
    identifiable, and the reason. A parameter that the data could not determine
    has ``"value": null``; what is known about it (a lower bound, or the
    combination that is determined) is given under its own name, never as a
    value. The file also holds how well the fitted model predicted an
    experiment it was not fitted on, and the conditions of the measurement:
    supply voltage and temperature ranges, the load, the device and its
    firmware when the adapter knows it, whether the device was simulated, the
    tool version and the bench file's fingerprint.

``servo_model_mujoco.xml``
    A MuJoCo fragment: a ``<default>`` class holding the joint and position
    actuator attributes, to be included in a model (``<include file=.../>``) and
    applied with ``class=``. Written only when the fit describes the actuator
    (``fit_quality`` good); a parameter that is not identifiable is left out of
    it and an XML comment says why.

The mapping to MuJoCo (checked against the MuJoCo 4.0.1 XML reference,
November 2026; ``MUJOCO_MAPPING`` holds it as data):

==================  =============================  ===========================================
this model          MuJoCo                         exact?
==================  =============================  ===========================================
inertia_kg_m2       joint ``armature``             yes: added inertia of a hinge, not due to
                                                   body mass. The load's own inertia and
                                                   gravity come from the bodies of the user's
                                                   model, not from this file
kp_n_m_rad          position ``kp``                yes: the position actuator's force is
                                                   kp * (ctrl - q) - kv * qdot (gainprm kp,
                                                   biasprm 0 -kp -kv), with gear 1
kd_n_m_s_rad        position ``kv``                yes, when identifiable
viscous_n_m_s_rad   joint ``damping``              yes, when identifiable (f = -damping * v)
kd + viscous        joint ``damping`` (kv 0)       when only the sum is known: the same
                                                   dynamics while the torque is inside its
                                                   limit, which is the only regime in which
                                                   the sum was measured
torque_max_n_m      position ``forcerange``,       yes: MuJoCo clamps the actuator force,
                    ``forcelimited="true"``        kv term included, as this model clamps
                                                   kp * e - kd * v. Not identifiable:
                                                   forcelimited="false" and a comment
coulomb_n_m         joint ``frictionloss``         no: MuJoCo models dry friction as a soft
                                                   constraint (solreffriction,
                                                   solimpfriction), so sticking at rest is
                                                   approximate; this model sticks exactly
delay_s             position ``delay`` and         approximately: MuJoCo (3.5.0 and newer)
                    ``nsample``                    reads ctrl from a history buffer at
                                                   time - delay; the delay is effectively at
                                                   least one timestep, and nsample must cover
                                                   delay / timestep, or the delay is silently
                                                   cut short. nsample here covers timesteps
                                                   down to 0.5 ms
==================  =============================  ===========================================

Not exact in any case: MuJoCo's integrators differ from this model's (the
trapezoidal rule at 0.5 ms, see ``servobench.model``). MuJoCo's ``Euler``
integrator is semi-implicit and treats joint damping implicitly, and the
reference recommends ``implicitfast`` or ``implicit`` when ``kv`` is used. At
MuJoCo's default 2 ms step the same parameters give a slightly different
response, more so for a stiff or lightly damped servo; check by replaying a
measured experiment in the user's own model. The reading's quantisation and
the servo's real-world effects that this model lacks (backlash, voltage and
temperature dependence) are not in either.
"""

from __future__ import annotations

import math
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from servobench import __version__
from servobench.analysis.fit import DAMPING_TOTAL, PARAMETERS, Estimate

FORMAT = "servobench-servo-model/1"
JSON_FILE = "servo_model.json"
MUJOCO_FILE = "servo_model_mujoco.xml"
# The delay's history buffer is sized to hold the delay for any timestep down
# to this (the fit's own grid). A buffer too short for the timestep in use
# cuts the delay short without any error: checked with MuJoCo 4.0.1, nsample 2
# for a 4 ms delay was right at a 2 ms timestep and 3 ms early at 0.5 ms.
MUJOCO_MIN_TIMESTEP_S = 0.0005

MUJOCO_MAPPING: tuple[dict[str, str], ...] = (
    {"parameter": "inertia_kg_m2", "mujoco": "joint armature", "exact": "yes",
     "note": "added inertia of the hinge, not due to body mass; the load comes from the"
     " bodies of your model"},
    {"parameter": "kp_n_m_rad", "mujoco": "position kp", "exact": "yes",
     "note": "force = kp (ctrl - q) - kv qdot, gear 1"},
    {"parameter": "kd_n_m_s_rad", "mujoco": "position kv", "exact": "yes, when identifiable",
     "note": "use the implicitfast or implicit integrator with kv"},
    {"parameter": "viscous_n_m_s_rad", "mujoco": "joint damping", "exact":
     "yes, when identifiable", "note": "f = -damping v"},
    {"parameter": DAMPING_TOTAL, "mujoco": "joint damping, kv 0", "exact":
     "while the torque is inside its limit", "note": "used when kd and viscous are only"
     " known as their sum"},
    {"parameter": "torque_max_n_m", "mujoco": "position forcerange, forcelimited",
     "exact": "yes", "note": "MuJoCo clamps the whole actuator force, kv term included, as"
     " this model clamps kp e - kd v"},
    {"parameter": "coulomb_n_m", "mujoco": "joint frictionloss", "exact": "no",
     "note": "a soft constraint in MuJoCo: sticking at rest is approximate"},
    {"parameter": "delay_s", "mujoco": "position delay and nsample (MuJoCo 3.5.0 or newer)",
     "exact": "approximately", "note": "at least one timestep; nsample must cover delay /"
     " timestep or the delay is cut short; nsample here covers timesteps down to 0.5 ms"},
)  # fmt: skip


def export_json(
    estimates: dict[str, Estimate],
    *,
    fit_quality: dict[str, Any],
    conditions: dict[str, Any],
    written_utc: str,
) -> dict[str, Any]:
    """The contents of ``servo_model.json``."""

    def entry(estimate: Estimate) -> dict[str, Any]:
        data = {
            "status": "identifiable" if estimate.identifiable else "not_identifiable",
            "value": estimate.value,
            "uncertainty": estimate.uncertainty,
            "unit": estimate.unit,
            "meaning": estimate.meaning,
            "reason": estimate.reason,
        }
        if estimate.lower_bound is not None:
            data["lower_bound"] = estimate.lower_bound
        if estimate.determined_only_as is not None:
            data["determined_only_as"] = estimate.determined_only_as
        return data

    return {
        "format": FORMAT,
        "tool_version": __version__,
        "written_utc": written_utc,
        "model": {
            "name": "servobench.model: position servo with a pendulum load",
            "equations": [
                "J dv/dt = tau_m - m g L sin(theta) - viscous v - coulomb sign(v)",
                "J = inertia_kg_m2 + m L^2 (the load, a point mass, from conditions.load)",
                "tau_m = clamp(kp (r(t - delay) - theta) - kd v, -torque_max, torque_max)",
                "theta = 0 where the arm hangs straight down",
            ],
            "uncertainty": "one standard uncertainty, from the fit's Jacobian, inflated for"
            " the correlation of the residuals in time",
            "how_to_read": "A parameter whose status is not_identifiable has value null: the"
            " experiments did not determine it, and it must not be used as a measurement."
            " lower_bound and determined_only_as say what is known about it.",
        },
        "fit_quality": fit_quality,
        "parameters": {name: entry(estimates[name]) for name in PARAMETERS},
        "combinations": {DAMPING_TOTAL: entry(estimates[DAMPING_TOTAL])},
        "conditions": conditions,
        "mujoco": {
            "file": MUJOCO_FILE if fit_quality.get("quality") == "good" else None,
            "mapping": list(MUJOCO_MAPPING),
            "integrator": "MuJoCo's integrators differ from this model's (trapezoidal rule"
            " at 0.5 ms); the same parameters give a slightly different response there.",
        },
    }


def mujoco_fragment(
    estimates: dict[str, Estimate],
    *,
    class_name: str,
    conditions: dict[str, Any],
    timestep_s: float = MUJOCO_MIN_TIMESTEP_S,
) -> str:
    """The MuJoCo fragment: a default class with the joint and position attributes."""
    e = estimates
    joint: dict[str, str] = {}
    actuator: dict[str, str] = {}
    notes: list[str] = []

    if e["inertia_kg_m2"].identifiable:
        joint["armature"] = _num(e["inertia_kg_m2"].value)
    else:
        notes.append(f"armature not set: inertia_kg_m2 {_why(e['inertia_kg_m2'])}")
    if e["kp_n_m_rad"].identifiable:
        actuator["kp"] = _num(e["kp_n_m_rad"].value)
    else:
        notes.append(f"kp not set (MuJoCo's default is 1): kp_n_m_rad {_why(e['kp_n_m_rad'])}")

    kd, viscous, total = e["kd_n_m_s_rad"], e["viscous_n_m_s_rad"], e[DAMPING_TOTAL]
    if kd.identifiable and viscous.identifiable:
        actuator["kv"] = _num(kd.value)
        joint["damping"] = _num(viscous.value)
    elif total.identifiable:
        joint["damping"] = _num(total.value)
        notes.append(
            "damping holds kd + viscous (damping_total_n_m_s_rad): only their sum was"
            " determined, so kv is not set. While the torque is inside its limit this is the"
            " same dynamics; when it saturates it is not."
        )
    else:
        notes.append(f"no damping set: {DAMPING_TOTAL} {_why(total)}")

    torque = e["torque_max_n_m"]
    if torque.identifiable:
        actuator["forcelimited"] = "true"
        actuator["forcerange"] = f"{_num(-torque.value)} {_num(torque.value)}"
    else:
        actuator["forcelimited"] = "false"
        bound = (
            f" It is at least {_num(torque.lower_bound)} N m."
            if torque.lower_bound is not None
            else ""
        )
        notes.append(f"no force limit: torque_max_n_m was not identifiable.{bound}")

    if e["coulomb_n_m"].identifiable:
        joint["frictionloss"] = _num(e["coulomb_n_m"].value)
        notes.append(
            "frictionloss is MuJoCo's soft-constraint dry friction; sticking at rest is"
            " approximate."
        )
    else:
        notes.append(f"frictionloss not set: coulomb_n_m {_why(e['coulomb_n_m'])}")

    delay = e["delay_s"]
    if delay.identifiable:
        nsample = math.ceil(delay.value / timestep_s - 1e-9) + 1
        actuator["delay"] = _num(delay.value)
        actuator["nsample"] = str(nsample)
        notes.append(
            f"delay needs MuJoCo 3.5.0 or newer. nsample {nsample} holds the delay for any"
            f" timestep of {timestep_s * 1e3:g} ms or more; for a smaller timestep raise it to at"
            " least delay / timestep + 1, or the delay is cut short. MuJoCo's delay is at least"
            " one timestep."
        )
    else:
        notes.append(f"no delay set: delay_s {_why(delay)}")

    load = conditions.get("load") or {}
    header = [
        f"servobench {__version__} characterize: fitted servo parameters for MuJoCo.",
        f"Device: {conditions.get('device_model')} (adapter {conditions.get('adapter')}, id"
        f" {conditions.get('device_id')}, firmware {conditions.get('firmware')})"
        + (", SIMULATED" if conditions.get("simulated") else "")
        + ".",
        f"Measured with a load of {load.get('mass_kg')} kg at {load.get('com_distance_m')} m;"
        " model that load in your bodies, not here. Supply"
        f" {_range(conditions.get('supply_voltage_v'), 'V')}, temperature"
        f" {_range(conditions.get('temperature_degc'), 'degC')}.",
        f"Bench file fingerprint {conditions.get('bench_fingerprint')}.",
        "Parameters, uncertainties and identifiability: servo_model.json next to this file.",
        "Use: <include file=\"servo_model_mujoco.xml\"/>, then class=\""
        + class_name
        + "\" on the hinge joint and on its <position> actuator.",
        "MuJoCo's integrator differs from the fit's; use implicitfast or implicit with kv.",
    ]
    lines = ["<mujoco>"]
    lines += [f"  <!-- {escape(_comment(text))} -->" for text in header]
    lines.append("  <default>")
    lines.append(f"    <default class={quoteattr(class_name)}>")
    lines += [f"      <!-- {escape(_comment(text))} -->" for text in notes]
    lines.append(f"      <joint{_attributes(joint)}/>")
    lines.append(f"      <position{_attributes(actuator)}/>")
    lines.append("    </default>")
    lines.append("  </default>")
    lines.append("</mujoco>")
    return "\n".join(lines) + "\n"


def _why(estimate: Estimate) -> str:
    text = estimate.reason
    if text.startswith("Not identifiable: "):
        text = text[len("Not identifiable: ") :]
    return "was not identifiable: " + text.rstrip(".") + "."


def _attributes(values: dict[str, str]) -> str:
    return "".join(f" {key}={quoteattr(value)}" for key, value in values.items())


def _comment(text: str) -> str:
    return text.replace("--", "- -")  # "--" may not appear inside an XML comment


def _num(value: float | None) -> str:
    return "nan" if value is None else f"{value:.6g}"


def _range(values: dict[str, float] | None, unit: str) -> str:
    if not values or values.get("min") is None:
        return "unknown"
    return f"{values['min']:.3g} to {values['max']:.3g} {unit}"
