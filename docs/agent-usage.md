# Using servobench from a coding agent

This file is for a coding agent (Claude Code, Codex or similar) that has been asked to work with an actuator through `servobench`. It says what the tool is for, when to run each command, how to read what it prints, and when to stop and hand over to the human.

## What the tool is for

`servobench` runs small, bounded experiments on one actuator, such as a smart servo on a bench, and reports measured facts: a verdict, a few numbers, and the files it wrote. You cannot see the hardware; this tool is how you find out what it actually did.

Everything it does to the actuator goes through one path. The tool validates the experiment against the bench limits, rehearses it on a simulated servo, runs it while a safety monitor checks every reading, and then puts the actuator in its safe state (for a bench servo, torque off) whatever happened. You cannot skip any of these steps, and you should not look for a way to.

The bench file, `bench.toml` in the directory where the tool runs, belongs to the human who owns the bench. It names the actuator and holds the limits: travel, speed, current, temperature, voltage, how long an experiment may last, and the cool-down between experiments. Read it to understand what the tool will allow. Do not edit it. A limit that stops you is the owner's decision about their hardware, and they need to hear about it from you. If you change the limit yourself, you remove the one check that would have caught a mistake. Every run records the file's fingerprint, so a change shows up in the records.

## Real hardware and the simulated servo

`bench.toml`'s `[target] adapter` says what is being driven. `"sim"` is the built-in simulated servo, which needs no hardware and runs on virtual time, so a check takes well under a second. Run anything you like against it. To see how the tool reports a fault, work in a scratch directory of your own: run `servobench init` there, and in that copy set `[sim] faults`, for example `faults = ["reversed_direction"]` (the template lists them all). `faults = ["ignores_safe_state"]` shows you the most serious verdict, `safe_state_not_confirmed`, for real: the simulated servo never acknowledges the request to turn torque off. A bench file you created yourself for the simulated servo is yours to change. The owner's is not, and the tool refuses `[sim]` settings for any adapter other than `"sim"`.

Any other adapter is real hardware. A motor has no undo, and the human's hand on the power switch is the last safety layer. So:

- Before running a command that moves a real actuator, get the human's go-ahead in the current conversation. A go-ahead from an earlier session does not carry over. `servobench check --no-motion` only reads, and is the safe first step.
- The output tells you which you are on: `metrics.items.identity.values.simulated` is `true` only for the simulated servo.

## Commands

| Command | When to run it | What it does to the actuator |
|---|---|---|
| `servobench check --json` | First, before any other experiment; after any change to wiring, mounting, configuration or firmware; whenever something behaves unexpectedly | Reads it, then moves it a few degrees up and back from where it rests (`[check] move` in bench.toml), through the runner |
| `servobench check --no-motion --json` | On real hardware before the human has agreed to motion; on an assembled robot; whenever you only need to know it is alive | Only reads. Changes nothing, not even torque |
| `servobench characterize --json` | When the human wants to know how the actuator actually behaves, or wants its parameters for a simulator. Only after `check` passes, and on real hardware only with the human's go-ahead | Runs `check` with motion, then four experiments through the runner: steps of up to about 12 deg, a small stepped sine, one slow 40 deg swing, and more steps (sizes from bench.toml's limits). On real hardware this takes several minutes, mostly the cool-down between experiments |
| `servobench runs --json` | To see recent runs, newest first | Nothing; it reads the run records |
| `servobench runs show [RUN] --json` | To read one run's full result again (default: the newest). Its own verdict only says whether the record could be read; the run's verdict is `metrics.run.verdict` | Nothing |
| `servobench init` | Only to create a bench file where none exists, in a scratch directory or when the human asks | Nothing. The template's limits are placeholders that suit only the simulated servo |

`tune` does not exist yet.

Every `check` writes a run record: a directory under `runs/` named by time, command and target, holding `run.json` (everything about the run, including the per-item results under `metrics`) and, when the actuator moved, the samples as CSV and a plot. The output's `run_dir` and `files` point to it.

The records are also how the cool-down between experiments holds across invocations: before it moves the actuator, `check` (and so `characterize`) finds when the actuator last ran from the newest record of a run that enabled it, and the runner waits or refuses (bench.toml's `[runner] when_too_warm`) until `[limits] cooldown` has passed. When it cannot establish that, it assumes the worse case: a directory under `runs/` named like a record (it starts with the time it was written, `20261116T100524Z-...`) whose `run.json` cannot be read counts as a run of this actuator that ended at the time in its name, or just now if that time is not valid or is in the future. The refusal, or with `"wait"` the summary, names the record and says how to get out of it (repair its `run.json`, or move the directory out of `runs/`); `metrics.cooldown` has `previous_run`, `previous_run_readable`, `previous_run_problem`, `unreadable_records` and `not_run_directories`. A directory under `runs/` that is not named like a record does not count, and is only noted. Do not delete or edit records yourself to get past a cool-down; tell the human.

## Reading the output

Always pass `--json`. The tool then prints exactly one JSON object on standard output, even for a usage error or an internal error. Branch on `verdict` and `exit_code`, then read `next`. The exit code does not depend on the output being read: if standard output or standard error cannot be written (for example a pipe whose reader has exited), the tool gives that stream up and still exits with the code the result calls for, so check the exit code even when the JSON did not arrive.

```json
{
  "verdict": "fail",
  "summary": "Fail: direction: Commanded +2.5 deg, the position changed by -2.42 deg: ...",
  "exit_code": 1,
  "needs_human": true,
  "next": "direction: Tell the human: the actuator's direction setting or its wiring is reversed. ...",
  "command": "check",
  "metrics": {"items": {"direction": {"status": "fail", "detail": "...", "values": {}, "next": "..."}}},
  "files": {"runs/20261116T100524Z-check-sim-1/run.json": "the run record (run.json)"},
  "run_dir": "runs/20261116T100524Z-check-sim-1"
}
```

- `verdict`: what was established, one of the values below.
- `summary`: one sentence. Quote it to the human rather than paraphrasing it.
- `needs_human`: when `true`, stop and tell the human. Do not try a workaround, a different experiment or a retry first. The few cases where it is `false` with a non-zero exit code are ones where waiting is all it takes (see below).
- `next`: what to do about it, in words. `null` when there is nothing to do.
- `metrics`: the numbers. Every quantity is in SI units under a name that ends in its unit (`position_at_rest_rad`, `temperature_rise_degc`, `answered_rate_hz`). A value that could not be measured is `null`, never a guess. When the actuator was moved, `metrics.run` describes the experiment, and `metrics.run.stop_kind` says what stopped it, if anything (for example `speed`, `temperature`, `device_fault`, `missed_replies`, `sample_gap`, `behind_schedule`, `call_time_exceeded`, `cooldown`). `sample_gap` means the safety monitor went longer than bench.toml's `[runner] sample_gap_max` without a usable reading (the device stopped answering, or answered too slowly); `behind_schedule` that the run could not keep to its plan (a part of the experiment passed before its command could be sent, or the actuator stayed enabled past the end of the plan); and `call_time_exceeded` that one call to the adapter took longer than the adapter declares any call takes (`call_time_max_s`), so the adapter broke its contract and nothing about the run's timing can be trusted. All three stop the run, exit 4. `metrics.run.stop_time_max_s` is the worst case, worked out before the run, from the last good reading to the tool sending the stop request, and `metrics.run.call_time_longest_s` the longest adapter call it measured (see "What the tool cannot promise").
- `error`: present only when the tool itself failed. It holds the exception and its traceback; give them to the human.

### Verdicts

| verdict | meaning |
|---|---|
| `pass` | Everything was measured and is as it should be. Warnings may still be listed (see `protections` below). |
| `partial` | Nothing failed, but some items were not measured. They are listed as `skipped` or `unknown`, never as passed. `check --no-motion` always gives `partial` when nothing is wrong, because it cannot check direction or heating. |
| `fail` | The measurement ran and found something wrong. `metrics.by_status.fail` lists the items. |
| `refused` | The tool would not enable the actuator. Nothing moved. |
| `aborted` | The actuator was enabled, and the safety monitor (or Ctrl-C) stopped it before the end. It is in its safe state. |
| `error` | Usage error, bench-file error, the actuator could not be reached, the tool or adapter failed, or the command's run record or export files could not be written (exit 4; see below). |
| `safe_state_not_confirmed` | The tool tried to put the actuator in its safe state and the device never confirmed it. The actuator may still be powered and driving. See below. It overrides every other verdict: if anything fails after such a run, a record that cannot be written or an error in the tool, the verdict stays `safe_state_not_confirmed`. |

A command whose record or export files could not be written (a full disk, for example) did not do its job, so its verdict is `error` with exit 4 and `needs_human: true`, even if the measurement itself went well; the `summary` says what could not be written. The numbers that were measured are still in `metrics`, and `files` lists only the files that do exist. Tell the human; do not rerun the command to get the files until the cause is fixed.

### `check` items

`metrics.items` holds one entry per item, in this order. Each has a `status` (`pass`, `fail`, `warning`, `skipped` or `unknown`), a `detail` sentence, the `values` measured and expected, and a `next`.

| item | what it establishes |
|---|---|
| `responds` | The actuator answers and its readings are numbers. |
| `identity` | It is the device `bench.toml` describes (adapter, and id when given). |
| `faults` | The device reports no fault, before or after the moves. |
| `voltage` | The supply is inside the bench limits. |
| `temperature` | It is cool enough to start an experiment. |
| `bus` | Reads are answered reliably and fast enough for the safety monitor and the analyses, and no read took longer than the adapter declares one call can take (`values.read_time_longest_s` against `values.call_time_max_s`). |
| `protections` | Which protections the device has on board, as its adapter declares: each is yes, no or unknown. A missing or unknown protection gives `warning`, not `fail`, because no setting can add one. Tell the human what is missing when it matters for what they plan to run: it means only the host-side monitor, which works only while a command runs, and the power supply guard against that hazard. Its `detail` also states the host's worst-case stop time (`values.host_stop_time_max_s`: `sample_gap_max` plus the adapter's declared `call_time_max_s`) and whether the device enters its safe state by itself when communication is lost (`values.protections.safe_state_on_comm_loss`: yes, no or unknown), which is what stops the actuator if the computer hangs. |
| `zero` | At rest, torque off, the actuator reads close to zero, where the arm should hang. `unknown` without a `[load]` in bench.toml. |
| `direction` | A small positive command increases the position (moves the actuator). |
| `temperature_rise` | It does not heat abnormally during those small moves (moves the actuator). |

The actuator is moved only if `responds`, `identity`, `faults`, `voltage`, `temperature` and `bus` all passed. Otherwise the moving items are `skipped`, and their `detail` says why.

### `characterize`

`characterize` measures the parameters of the tool's servo model: the controller's gains, the servo's own inertia, friction, the torque limit and the command delay. It needs a `[load]` in bench.toml, because the load's known gravity torque is what separates the servo's inertia from its gains, and it refuses without one (exit 2, nothing contacted).

It first runs `check` in full on the same actuator. If `check` does not pass, nothing is characterized: the result carries `check`'s verdict, exit code, `needs_human` and `next`, and a `partial` check becomes `refused` (exit 3). A reversed or wrongly zeroed servo produces data that looks ordinary and fits to confident nonsense, so do not look for a way around this. If any experiment is refused, aborted, fails or leaves the safe state unconfirmed, the sequence stops there, nothing is fitted, and the verdict and exit code follow the same rules as for `check`. If an experiment's run record cannot be written, the sequence also stops there, with verdict `error` (exit 4). If the fit ran but `servo_model.json`, the MuJoCo fragment or the command's record could not be written, the verdict is `error` (exit 4): the parameters are still in `metrics.parameters`, but they were not saved, and no file in `files` holds them.

What to read in `metrics`:

- `parameters`: one entry per model parameter, plus `damping_total_n_m_s_rad` (kd + viscous). Each has a `status`, `identifiable` or `not_identifiable`, and a `reason`. An identifiable one has a `value` and an `uncertainty` (one standard uncertainty, same unit). A not-identifiable one has `value: null`: the experiments did not determine it, and you must not report a number for it or fill one in from anywhere else. Some say what is known instead: `lower_bound` (the torque limit is at least this) or `determined_only_as` (kd and viscous friction separately are not known, only their sum is). Under limits like the template's the servo never reaches its torque limit, so `torque_max_n_m`, `kd_n_m_s_rad` and `viscous_n_m_s_rad` are always not identifiable there. That is a correct result, and it is why the verdict is `partial`.
- `validation`: the fitted model and the default placeholder model, each replayed on an experiment that was not used in the fit (`holdout`), with their RMS error in radians, and `noise_floor_rad`, the error a model that described the actuator exactly would still have against these readings. `fit_quality` is `good` when the fitted model's error is at most 1.5 times the noise floor, `poor` otherwise. `default_error_to_fitted_error` says how much better the fit predicts than the defaults; on the simulated servo with its default settings it is about 1, because the simulated servo is the default model.
- `fit`: the starting points and whether they agree, the largest torque the fitted model needed, and whether the servo saturated.
- `files`: `servo_model.json` (the parameters with their uncertainties, identifiability and the conditions of the measurement), `servo_model_mujoco.xml` (a MuJoCo fragment, written only for a good fit), `holdout_overlay.png` (measured, default and fitted response on the held-out experiment, each model's error on the plot), and every experiment's own run record.

| verdict | meaning |
|---|---|
| `pass` | Every parameter was identified and the fit is good. |
| `partial` (exit 0) | The fit is good, but some parameters are not identifiable. Report the identifiable ones with their uncertainties and the others as not identifiable with their reasons. |
| `fail` (exit 1, `needs_human`) | The fitted model does not describe this actuator: on the held-out experiment its error is more than 1.5 times the noise floor. The parameters are still reported, with `metrics.parameters_usable: false` and every reason starting `POOR FIT:`, because they are numbers of a model that is wrong for this actuator: they can look precise and still be far off. They must not be loaded into a simulator or quoted as measurements, and no MuJoCo fragment is written. Show the human the summary and the overlay plot. |

**A poor fit or a parameter that is not identifiable is a finding to report, not a problem to fix by retrying.** Do not rerun `characterize` hoping for a better fit, do not design other experiments to make a parameter look determined, and do not change bench.toml to allow bigger moves. The data says what it says: on the simulated servo, repeated runs agree within the stated uncertainty, and a retry that happened to look better would be chance, not a better measurement. Which effect the model lacks (backlash, voltage or temperature dependence, ...) and whether to measure more is the human's decision.

Two limits of the result, which you should mention when you report one: the parameters are of this model, measured with this load, supply voltage and temperature (all in `servo_model.json`'s `conditions`), and MuJoCo's integrator and dry friction differ from the model's, so the same parameters behave slightly differently there.

## Exit codes and what to do next

| exit | meaning | what to do |
|---|---|---|
| 0 | `pass` or `partial`: nothing wrong was found | For `pass`, carry on. For `partial`, look at what was skipped, unknown or not identifiable before relying on it. After `--no-motion` on real hardware, ask the human before running `check` with motion. |
| 1 | `fail`: the setup has a problem, named in `metrics.by_status.fail`; for `characterize`, also a model that does not describe the actuator | Tell the human which item failed, quoting `summary` and `next`. Do not run other experiments on this actuator until it is fixed. These problems are physical (wiring, mounting, direction or zero settings stored in the device) and you cannot fix them from software: a device setting is a persistent write, which only the owner makes. The one exception is a `temperature` failure with `needs_human: false`: the actuator is warm, so wait for it to cool and run `check` again. |
| 2 | Usage or bench-file error, or an experiment the bench limits do not allow (including one whose parts are too short for the rate at which the actuator can be sampled), or an adapter that declares one call can take more than half of `sample_gap_max` (`stop_kind` `adapter_unsuitable`; the message says what to change). Nothing was contacted | If the problem is your command line (`needs_human: false`), correct it. If it is the bench file, or the limits refused the experiment (`needs_human: true`), tell the human each problem in `metrics.problems` or `summary`. Do not edit `bench.toml`. |
| 3 | The safety layer stopped or refused motion: a safety abort, a reading outside the limits before starting, a failed rehearsal, the cool-down, or Ctrl-C | Stop and tell the human what was stopped and why. Do not rerun the same experiment, and do not try a smaller or different one to get past it. The exception is `metrics.run.stop_kind` `cooldown` (`needs_human: false`): the bench file asks for a rest between experiments, and `next` says how long. Wait that long, then run the command again. If the message says a run record cannot be read, the tool could not establish when the actuator last ran and assumed the worse case (see below); tell the human which record it names. When that record will hold the cool-down on every run, `needs_human` is `true`: waiting does not help, and only the owner can repair or move the record. |
| 4 | The device or the link to it: it reports a fault, it does not answer or answers too slowly (`stop_kind` `missed_replies`, `sample_gap` or `behind_schedule`), an adapter call took longer than the adapter declares (`call_time_exceeded`), the adapter or the tool failed, a record or export could not be written, or the safe state was not confirmed | Stop and tell the human. The tool cannot clear a device fault and must not try: recovery from a fault is a human decision, often a power cycle once they know the cause. If `error` is present, the tool itself failed; pass the traceback on. |

## When to stop and tell the human

Stop, and tell the human in plain words before doing anything else with this actuator, when:

- **The verdict is `safe_state_not_confirmed`.** This is the most serious result. The actuator may still be powered and holding or driving its output. Tell the human at once that they should cut its power, then run nothing against it, not even read-only commands, until they have checked it. In both text and JSON mode the tool writes this summary to standard error first, before it writes anything to standard output, so that a person watching the terminal sees it, and so that it is not lost if standard output is a broken pipe; the exit code is 4 whatever happens to either stream.
- **A safety abort** (`verdict: aborted`). The monitor stopped a moving actuator because a reading left the limits. The cause has to be understood first, and running again is how a near miss becomes damage.
- **A device fault** (the `faults` item failed, or `stop_kind` is `device_fault`). Only a human can clear it, and they should know what caused it first.
- **The actuator does not respond or stops answering** (`responds` failed, or `stop_kind` is `missed_replies`, `sample_gap`, `behind_schedule` or `call_time_exceeded`). Cables, power and the adapter's timeouts are for the human.
- **Any `needs_human: true`.** This covers the cases above, every `check` failure except a warm actuator, and every bench-file problem.
- **A limit blocks what you were asked to do.** Report what was refused and why. Do not loosen the limit, edit `bench.toml`, change the experiment to slip under the check, or reach the device another way, such as opening its serial port yourself. Each of those removes a protection the owner put there deliberately.

When you report, give the `summary`, the failing item or `stop_kind`, the `next` text and the `run_dir`, so the human can look at the record and the plot.

## What the tool cannot promise

The host-side checks are a second line of defence. An agent with a shell could edit files or talk to the serial port directly, and the tool cannot prevent that.

The host is also slower to stop the actuator than `sample_gap_max` suggests. The tool can act only between calls to the adapter, so after the last good reading the stop request goes out at most `sample_gap_max` plus the longest one call can take, which the adapter declares (`check`'s `protections` item prints the sum; on the simulated servo with the template, 0.2 s + 0.02 s = 0.22 s), and the safe state is confirmed up to one more call after that. That holds only while every call keeps to its declared time and the computer keeps running. A call that blocks for longer delays the stop until it returns (the run then ends as `call_time_exceeded`, but the actuator was unwatched for that long), and if the computer hangs or the cable is pulled no stop request is sent at all: only a device that enters its safe state by itself on lost communication (`protections`) or the human cutting power stops it then. The real protection is the device's own protections (the `protections` item says which exist), the bench power supply's current limit, and the human's hand on the power. Use the tool as the only way you touch the actuator, and it will tell you plainly what it measured and when it is time to hand back to the human.
