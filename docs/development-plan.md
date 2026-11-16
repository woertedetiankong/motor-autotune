# Development plan

This is the working document for the Claude Code session that builds the tool. It is written so that a session with no knowledge of the earlier discussion can pick it up and continue.

## How to use this document

1. Read `docs/PRD.md` (what is being built and why) and `docs/targets.md` (facts about the hardware, with how sure each one is). `docs/background.md` is optional context.
2. Find the first unticked step in the progress list below and work on that step only.
3. Each step has four parts: **Goal**, **Build**, **Done when**, and **Stop and ask**. A step is finished when every "Done when" check passes and you have shown the evidence, usually the command output.
4. When a step is finished: tick it in the progress list, add a dated line under "Notes for the next session" if there is something the next session must know, commit, and report to the owner.
5. Do not start the next step in the same breath if the step you finished needs the owner's input or review. Otherwise continue.

Steps 1 to 8 need no hardware. Step 9 needs a document from the owner. Steps 10 to 14 need hardware on the bench and the owner present.

### What to report at the end of each step

- What was built, in a few lines.
- The evidence that the "Done when" checks pass.
- Anything that is unverified, skipped or weaker than planned, stated plainly.
- What is needed from the owner before the next step, if anything.

## Progress

- [x] 1. Project skeleton
- [x] 2. Units and bench limits
- [x] 3. Adapter contract, clocks and the simulated servo
- [x] 4. Experiment runner and safety monitor
- [ ] 5. Run records and plots
- [ ] 6. Analysis: step response and frequency response
- [ ] 7. Command line and `check`, on the simulated servo
- [ ] 8. Model fit, `characterize` and simulator export, on the simulated servo
- [ ] 9. Feetech protocol layer (needs the register table)
- [ ] 10. HD1910 adapter, read-only, on the bench (needs hardware and the owner)
- [ ] 11. First motion and conformance on the bench
- [ ] 12. `characterize` on the real servo: the first public result
- [ ] 13. `tune`
- [ ] 14. `check` across the Microduck's 15 servos, read-only
- [ ] 15. Release preparation

## Rules for every step

Each rule has a reason. If a situation comes up that the rule does not cover, act on the reason.

1. **Real hardware moves only with the owner present and their go-ahead in the current session.** A motor has no undo, and the owner's hand on the power is the last safety layer. A go-ahead given in an earlier session does not carry over.
2. **No persistent write to a device without the owner confirming that specific write.** This covers servo IDs, baud rate, offsets, limits, gains stored in non-volatile memory, and any firmware. A wrong persistent write can leave a servo unreachable or unsafe after power-up.
3. **Do not edit the bench limits file, and do not loosen a limit check to make something pass.** The limits file belongs to the owner. If a limit blocks an experiment, report what was blocked and why.
4. **The tool has no way to clear a device fault.** Recovery from a fault is a human decision. Do not add such a command, even as a convenience.
5. **Every quantity carries an explicit unit.** In the workshop that motivated this project, an agent sent degrees to a tool expecting radians and the robot turned about 60 times too far. A bare number at any boundary is an error.
6. **Nothing is copied from the Microduck rebuild repository** (`kissqy/Microduck-kissqy`). Its original parts are licensed for personal non-commercial use only, and this project will be released under a permissive licence. Take the Feetech protocol from Feetech's own documents.
7. **Stay inside the scope.** No MCP server, no graphical interface, no agent harness, no FOC work in this stage. If a step seems to need one of these, stop and ask.
8. **Facts about hardware come from `docs/targets.md` or a primary source, not from memory.** Several HD1910 facts are still unknown and retailer listings disagree. If you need an unknown fact, stop and ask; when a fact is confirmed, record it in `docs/targets.md` with its source.
9. **Fix causes, not symptoms.** Do not special-case a test, widen a tolerance, or mark a test as skipped to get a pass. If a "Done when" check cannot be met after a real attempt, say so and show what you found.
10. **Keep dependencies few.** The runtime needs `numpy`, `scipy`, `matplotlib` and `pyserial`. Add another only with a stated reason.
11. **Nothing leaves this machine without the owner's agreement.** That covers creating a public repository, pushing, and publishing a package.

## Architecture

### Parts

```
src/servobench/
  units.py            parse and format quantities; SI units inside
  limits.py           the bench file: load, validate, fingerprint
  contract.py         the adapter interface and its data types
  clock.py            real clock and virtual clock
  model.py            the servo model shared by the simulator and the fit
  adapters/
    sim.py            simulated servo, with fault injection
    feetech.py        Feetech serial protocol            (step 9)
    hd1910.py         HD1910 register map and adapter    (steps 9 to 11)
  experiments/
    spec.py           experiment definitions: hold, step, stepped sine
    runner.py         rehearsal, thermal budget, abort, safe state on every exit
    monitor.py        the safety monitor's checks (no device calls)
  analysis/
    step.py           step-response metrics
    frequency.py      gain and phase from stepped sine
    fit.py            model fit and identifiability
  records.py          run directories
  plots.py
  export.py           parameters for a simulator
  commands/           check, characterize, tune
  cli.py              thin layer over the library
tests/
  conformance/        tests that every adapter must pass
```

`servobench` is a working name. Keep it in as few places as possible (the package directory and `pyproject.toml`) so a rename is cheap.

### How a command runs

1. Load the bench file: which actuator, how to reach it, the limits, the load.
2. Ask the adapter what the actuator can do.
3. Build the experiment and check it against the limits.
4. Rehearse the experiment on the simulated servo. Stop if the rehearsal leaves the limits.
5. Run it on the target while the safety monitor checks every sample.
6. Put the actuator in its safe state, whatever happened.
7. Analyse, write the run record, print the verdict.

### The adapter contract

An adapter is the only code that knows a particular actuator. Keep the interface narrow, because users and their coding agents will write adapters, and because the layer may later be rewritten in a faster language behind the same interface.

It must provide, in whatever exact shape you design in step 3:

- **Describe.** Identity, the signals it can read with their units, the fastest reliable sampling rate, what it can be commanded to do, which parameters can be read and written and whether a write is volatile or persistent, and which protections the hardware really has (limits on the device, a safe state on lost communication, fault latching, on-board capture, on-board test signals).
- **Read** a set of signals, returning values in SI units with a timestamp from the adapter's clock.
- **Command** a setpoint.
- **Enable, disable, and enter the safe state.** The safe state is defined per target. For a bench servo it is torque off.
- **Read faults.** There is no clear-faults call.
- **A clock.** The simulated servo runs on virtual time so tests are fast and repeatable; real adapters use a monotonic clock. The runner waits through the adapter's clock and never calls `sleep` directly.

### The bench file

A small file the owner writes and owns, named `bench.toml`, in the directory where the tool is run. It holds the target (adapter, port, ID), the limits (travel range, speed, current or load, temperature, voltage range, longest experiment, cool-down), and the load on the actuator (for the bench rig, arm mass and length). Values are written with units, for example `"45 deg"`.

The tool reads this file and never writes it, except that `servobench init` creates a commented template and refuses to overwrite an existing file. Every run record stores a fingerprint of the file, and `check` prints the limits in force.

Be honest in the documentation about what this achieves: an agent with a shell can edit the file. The fingerprint makes a change visible; it does not prevent one. Real enforcement is the servo's own protections and the bench power supply's current limit.

### Output

Every command prints a verdict first. With `--json` it prints one JSON object:

```json
{
  "verdict": "pass | fail | aborted | error",
  "summary": "one sentence",
  "metrics": {},
  "files": {},
  "run_dir": "runs/...",
  "next": "what to do about it, when there is something to do"
}
```

Exit codes: 0 pass; 1 the measurement ran and the verdict is fail; 2 usage or bench-file error; 3 safety abort; 4 device or communication error.

Every run writes a directory under `runs/` holding the run description, the samples, the metrics and a plot, all as plain files.

## Steps

### Step 1. Project skeleton

**Goal.** A repository that builds, lints and tests, with nothing in it yet.

**Build.**
- Ask the owner to confirm the name and the licence (Apache-2.0 is the default) before creating files that carry them.
- `git init`. Add a `.gitignore` that covers Python artefacts, `.DS_Store`, and `runs/`.
- A `uv` project with a `src/` layout, Python 3.11 or newer, the runtime dependencies from rule 10, and `pytest` and `ruff` as development dependencies.
- A console entry point `servobench` that prints its version.
- A licence file and a three-line `README.md` saying what the project is and that it is not usable yet.

**Done when.**
- `uv run servobench --version` prints a version.
- `uv run pytest` runs and passes with at least one trivial test.
- `uv run ruff check` is clean.
- The first commit exists.

**Stop and ask.** If the owner has not confirmed the name and licence.

### Step 2. Units and bench limits

**Goal.** No number enters the tool without a unit, and the limits are loaded and checked in one place.

**Build.**
- `units.py`: parse strings such as `"45 deg"`, `"0.5 rad"`, `"2 Hz"`, `"300 ms"`, `"6 V"`, `"0.8 A"`, `"60 degC"` into SI values tagged with their dimension. Reject bare numbers, unknown units, and a unit of the wrong dimension for the field. Inside the library, values are SI floats and names carry the unit, for example `position_rad`.
- `limits.py`: load and validate `bench.toml`, produce clear errors that name the field and what was expected, and compute the fingerprint.
- `servobench init`: write the commented template; refuse to overwrite.

**Done when.**
- Tests cover: degrees and radians both accepted for an angle and converted correctly; a bare number rejected; a voltage given where an angle is expected rejected; a missing required limit rejected with a message naming it.
- Changing any limit changes the fingerprint.
- `servobench init` run twice leaves the first file untouched and exits with code 2 the second time.

### Step 3. Adapter contract, clocks and the simulated servo

**Goal.** A simulated servo that behaves enough like a real one to develop and test everything else against, and a contract that the real adapter will also satisfy.

**Build.**
- `contract.py` and `clock.py` as described under Architecture.
- `model.py`: a position servo driving a pendulum load. The internal controller is proportional-derivative with a torque limit and a command delay. The mechanics have the servo's own inertia, viscous and Coulomb friction, and the load's inertia and gravity torque. The load comes from the bench file; the servo's parameters are the model's unknowns.
- `adapters/sim.py`: the model behind the adapter contract, with position quantised to 4096 steps per turn, sampling at a configurable bus rate with timing jitter, measurement noise, a first-order temperature model that heats with effort, and supply voltage.
- Fault injection, selectable from the bench file: reversed direction, wrong zero, overheating, dropped replies, a stuck position reading, and a runaway that ignores commands.
- `tests/conformance/`: tests any adapter must pass. Examples: describe is complete and self-consistent; every signal it claims can be read and has the claimed unit; timestamps increase; the safe state can be entered from any state and reading still works afterwards; a small commanded move changes position in the commanded direction.

**Done when.**
- The conformance suite passes on the simulated servo.
- A simulated step response shows the expected qualitative behaviour, checked by tests: higher proportional gain gives more overshoot, torque limit caps acceleration, the temperature rises under sustained effort and falls at rest.
- A simulated ten-second experiment completes in well under a second of real time.
- Each injected fault is observable through the contract.

### Step 4. Experiment runner and safety monitor

**Goal.** Experiments run the same way every time, and nothing the tool does can leave the limits without being stopped.

**Build.**
- `experiments/spec.py`: `hold`, `step` (one or more amplitudes around a centre), and `stepped_sine` (a list of frequencies at a given amplitude). Each spec validates itself against the limits before anything runs, for example that the commanded positions stay inside the travel range with margin.
- `experiments/runner.py`:
  - **Rehearsal.** Run the spec on the simulated servo first. If the rehearsal leaves the limits, refuse the real run and say which limit and when. There is no flag to skip this.
  - **Safety monitor.** On every sample check position, speed, current or load, temperature and voltage against the limits; count consecutive missing replies; enforce the longest allowed duration. On any violation, enter the safe state, record the reason, and end with a safety abort.
  - **Always leave safely.** The safe state is entered on normal completion, on a violation, on an exception anywhere in the run, and on Ctrl-C.
  - **Thermal budget.** Refuse to start above a starting temperature; after a run, wait for the cool-down before the next one.

**Done when.** Tests show, using fault injection:
- an overheating servo is stopped at the temperature limit and the result is a safety abort;
- a runaway is stopped at the travel or speed limit;
- dropped replies beyond the allowed count stop the run;
- an exception raised in the middle of a run still ends in the safe state;
- a spec whose commanded range exceeds the travel limit is refused before any command is sent;
- a spec that passes its own validation but fails in rehearsal is refused before the real target is touched;
- a second run started while the servo is too warm waits or refuses, as configured.

**Stop and ask.** If you find a path by which a command could reach the target without passing rehearsal and the monitor. That is a design problem to raise, not to patch around.

### Step 5. Run records and plots

**Goal.** Every run leaves a complete, readable record in the user's repository.

**Build.**
- `records.py`: a directory per run under `runs/`, named by time, command and target. It holds the experiment spec, the bench-file fingerprint, the tool version, the adapter's description, the samples as CSV with units in the column names, the verdict and metrics, and the reason for any abort.
- `plots.py`: commanded and measured position against time, with temperature. Saved as PNG.

**Done when.**
- A completed run and an aborted run each produce a record that can be loaded back and that contains everything listed above.
- The record of an aborted run includes the samples up to the abort and the reason.
- A plot file exists for each and opens as a valid image.

### Step 6. Analysis: step response and frequency response

**Goal.** Turn samples into numbers an engineer or an agent can act on, and be right about them.

**Build.**
- `analysis/step.py`: delay, rise time, overshoot, settling time and steady-state error for each step. Work from the timestamps actually recorded; do not assume even spacing.
- `analysis/frequency.py`: for each stepped-sine frequency, discard the start-up transient and fit a sine to command and response by least squares, giving gain and phase.
- Report the sampling rate and jitter actually achieved. Refuse to report a frequency point with too few samples per cycle, and say so, instead of returning a weak number.

**Done when.**
- On simulated data without noise, where the true answer can be computed from the model, overshoot is within 1 percentage point, delay within one sample period, and rise and settling time within 5%.
- With the simulator's noise and timing jitter switched on, the same metrics stay within twice those tolerances.
- Gain and phase from a stepped sine match the model's own frequency response within 1 dB and 5 degrees at frequencies with enough samples per cycle.
- A frequency too high for the sampling rate is reported as not measurable.

If a tolerance here turns out to be unrealistic, report what you measured and propose a new one with the reason. Do not change it silently.

### Step 7. Command line and `check`, on the simulated servo

**Goal.** The first command a user or an agent can run end to end.

**Build.**
- `cli.py` with `init`, `check`, and `runs` (list recent runs, show one). Use the output format and exit codes under Architecture. Error messages say what went wrong and what to do next.
- `commands/check.py`, reporting pass or fail per item: the actuator responds; its identity matches the bench file; voltage and temperature are in range; no fault is reported; a small move in the positive direction increases position; the position at rest is near the expected zero; the device's own protections are present, listed as the adapter declares them; the bus sustains the sampling rate the other commands need.
- `--no-motion`, which skips the items that move the actuator and says they were skipped.
- `docs/agent-usage.md`: a short instruction file for a coding agent using the tool. It says when to run each command, how to read the JSON, what each exit code means, and that after a safety abort or a fault the agent must stop and tell the human.

**Done when.**
- `uv run servobench check --json` on a healthy simulated servo exits 0 with verdict `pass`.
- With each injected fault in turn (reversed direction, wrong zero, overheating, dropped replies), `check` exits 1, names the failing item, and the `next` field says something useful.
- With `--no-motion` the direction item is reported as skipped, not passed.
- A bench file with a missing limit gives exit code 2 and names the field.

### Step 8. Model fit, `characterize` and simulator export, on the simulated servo

**Goal.** Measured parameters for the servo model, with an honest statement of how well they are known, in a form a simulator can load.

**Build.**
- `analysis/fit.py`: fit the model's unknowns (controller gains, own inertia, viscous and Coulomb friction, torque limit, delay) by least squares, replaying the recorded commands through the model. Use several starting points. Estimate how well each parameter is determined. A parameter the data cannot determine, for example the torque limit when the servo never saturated, is reported as not identifiable. It is never filled in with a guess.
- `commands/characterize.py`: run a set of steps at several amplitudes and a stepped sine; fit on part of the data; validate on an experiment that was held out of the fit.
- A plot overlaying the measured response, the model with default parameters, and the model with fitted parameters, on the held-out experiment, with the error of each.
- `export.py`: write the fitted parameters as JSON with the conditions of the measurement (voltage, temperature, load, firmware version when known), and as a fragment for MuJoCo. Check the current MuJoCo documentation for the right attribute names instead of relying on memory.

**Done when.**
- Against a simulated servo whose parameters are hidden from the fit, with noise and jitter on, each identifiable parameter is recovered within 20%, and within 10% without noise.
- On the held-out experiment, the fitted model's error is clearly lower than the default model's, and both numbers appear in the output.
- An experiment set that never saturates torque reports the torque limit as not identifiable.
- Against a simulated servo with an effect the model does not have (add backlash for this test), the tool still returns a result, and the reported fit error is visibly worse. It does not claim a good fit.
- `uv run servobench characterize --json` on the simulated servo exits 0 and lists the files it wrote.

Passing this step proves the machinery, not the model. Whether the model describes a real HD1910 is only learned in step 12.

### Step 9. Feetech protocol layer

**Needs from the owner first.** Feetech's datasheet and register table for the HD-1910-C001, placed in `docs/reference/`.

**Goal.** Correct packets and a correct register map, proven without hardware.

**Build.**
- Record the register table's facts in `docs/targets.md`: addresses, sizes, units and scaling, which registers are persistent, the built-in protections, and which protocol family the servo follows. Cite the document.
- `adapters/feetech.py`: packet framing, checksum, the read and write instructions, and reply parsing with every error bit surfaced.
- `adapters/hd1910.py`: the register map and conversion between register values and SI units, built only from the recorded table. Mark each writable register volatile or persistent.
- A guard in the adapter: a write to a persistent register is refused unless a confirmation is passed in by a human-run command. No command in this plan passes it.

**Done when.**
- Packet encoding and decoding are tested against examples from the Feetech documentation, including a bad checksum and a truncated reply.
- Unit conversion is tested in both directions for every signal.
- A test proves that a persistent write is refused by default.
- `docs/targets.md` has no remaining "unknown" row that this step depends on.

**Stop and ask.** If the documents do not settle a register's meaning, size or scaling. Do not fill the gap from another Feetech model.

### Step 10. HD1910 adapter, read-only, on the bench

**Needs from the owner first.** One spare servo, the URT2 adapter, power, and the bench rows of `docs/targets.md` filled in. The owner is present and has given the go-ahead for this session.

**Goal.** Talk to a real servo without moving it.

**Build and run, with the owner.**
- Open the port, ping the servo, read the model and firmware version, position, voltage, temperature, and the servo's own limit and protection registers.
- Measure the fastest sampling rate that stays reliable and the timing jitter, and record both in `docs/targets.md`.
- Complete the adapter's describe from what was actually read.
- Add hardware tests under a marker that is skipped unless explicitly enabled.

**Done when.**
- The read-only conformance tests pass on the real servo.
- `servobench check --no-motion --json` runs against the real servo and its readings are plausible to the owner (position changes when the horn is turned by hand, voltage matches the supply).
- The measured sampling rate and jitter are recorded.

**Stop and ask.** If the model or firmware version does not match the documents, or any reply shows an error bit.

### Step 11. First motion and conformance on the bench

**Needs.** The pendulum arm fitted, the bench file written by the owner with conservative limits, the owner present with a hand on the power.

**Goal.** Move the real servo, starting as small as possible.

**Run, with the owner.**
- Agree the first move with the owner before sending it: a step of a few degrees at the centre of travel.
- Run `check` with motion.
- Run one small `step` experiment and one `hold` through the normal runner, rehearsal included.
- Run the full conformance suite with the hardware marker enabled.
- Deliberately trigger one safety abort in a harmless way the owner agrees to, for example a temperature limit set just above the present temperature, and confirm the servo ends in its safe state.

**Done when.**
- `check` passes on the real servo.
- The conformance suite passes on the real servo.
- The deliberate abort ends in the safe state with exit code 3 and a complete run record.
- The owner has seen each of these happen.

### Step 12. `characterize` on the real servo: the first public result

**Goal.** The result that proves the tool: simulated against real response, before and after using measured parameters.

**Run, with the owner.**
- Run `characterize` on the bench servo with the pendulum load.
- Compare default and fitted models on the held-out experiment.
- Look at where the fitted model still disagrees with the servo, and write down what the model is missing. Backlash, voltage dependence and temperature dependence are likely candidates, but report what the data shows.

**Done when.**
- The run completes without a safety abort and produces the overlay plot, the fitted parameters with their identifiability, and the export files.
- The held-out error of the default and fitted models is reported as measured. If the fitted model is not clearly better, that is the finding: report it and investigate the cause. Do not adjust the comparison until it looks good.
- `docs/targets.md` records the measured parameters and their conditions.

**Stop and ask.** Before improving the model. Which effects are worth modelling is a decision to make with the owner, with the data in front of you.

### Step 13. `tune`

**Goal.** Adjust the servo's position gains to meet a stated target, and never leave it worse than it was.

**Build.**
- `commands/tune.py`: take a target such as maximum overshoot and settling time; measure a baseline; propose gains using the fitted model; rehearse; apply as a volatile write; measure; keep the change only if the target is met or the result is better than the baseline, otherwise restore the original gains.
- The command prints the final gains and never stores them persistently. Making them permanent is left to the owner.

**Done when.**
- On the simulated servo, a mistuned controller is brought inside the target, and a case where no gains can meet the target ends with the original gains restored and verdict `fail`.
- An interrupted run restores the original gains.
- With the owner present, the same works on the bench servo.

**Stop and ask.** If step 9 showed the HD1910's gains can only be written persistently. Then `tune` needs a different design and the owner's decision.

### Step 14. `check` across the Microduck's 15 servos, read-only

**Needs.** The assembled or part-assembled robot, with its own software not driving the servo bus, and the owner present.

**Goal.** Find wiring and setup mistakes across the whole robot without moving it.

**Build.**
- Extend the simulator to a bus carrying several servos, so this step can be developed and tested without the robot.
- A bus scan and a `check --no-motion` over a list of IDs, reporting per servo: present or missing, duplicate IDs, voltage, temperature, faults, position at the current pose, and the protection settings.
- A summary table and one verdict.

**Done when.**
- On the simulated bus with 15 servos, a missing servo, a duplicate ID and an overheated servo are each found and named.
- With the owner, the scan runs on the real robot and the owner confirms the report matches what they see.

Motion experiments on the assembled robot are out of scope. A sweep on one joint could drive a leg into its limit or tip the robot.

### Step 15. Release preparation

**Goal.** A stranger can install the tool, run it on the simulated servo in ten minutes, and understand what it measured on the real one.

**Build.**
- `README.md`: what the tool is; the step-12 plot and numbers on the first screen; install; a first run on the simulated servo; the safety model, including the honest limits; what is not supported.
- `docs/writing-an-adapter.md`: the contract, how to run the conformance suite, and how to hand the task to a coding agent.
- The measured HD1910 parameters and their conditions as data files, under the data licence the owner chooses.
- A changelog and a version.

**Done when.**
- Following the README from a clean checkout reaches a passing `check` on the simulated servo.
- A fresh coding-agent session, given only `docs/agent-usage.md` and the installed tool, can run `check` on a faulty simulated servo and correctly report what is wrong.
- The owner has reviewed the README and the published data.

**Stop and ask.** Before creating a public repository, pushing, or publishing a package.

## After the servo stage: FOC with ServoHub

This is an outline only. Write a detailed plan for it when step 15 is done, using what the servo stage taught.

1. Build ServoHub's software-in-the-loop project and confirm it runs.
2. Recover the MAVLink source dialect file. The Python host cannot be generated without it.
3. Learn from the owner what `servo_host` already does, and reuse its protocol layer.
4. Write a ServoHub adapter that talks to the software-in-the-loop build. The contract should need few changes; note every change it does need.
5. Use the on-board capture for high-rate data, and add frequency-response measurement of the loops.
6. Firmware work, each item agreed with the owner first: wire the trace recorder into the control loop, expose it over the bus, verify the units still marked unverified, confirm the behaviour on lost communication, and define how a fault is latched and cleared by a human.
7. Move to the real board.

Design choices in the servo stage that this depends on: the narrow adapter contract, explicit units, declared capabilities, and the adapter-owned clock.

## Notes for the next session

Add dated, one-line notes here when a step leaves something the next session must know.

- 2026-11-16: Two paths reach an actuator outside `run_experiment`, reported to the owner at step 4 and not yet decided: the conformance suite's `motion` tests command the adapter directly (step 11 runs them on the real servo), and `write_parameter` is not routed through the runner (step 13 writes gains; rehearse them by passing `rehearsal_params`, and settle with the owner how the write itself is guarded).
- 2026-11-16: `run_experiment` enters the safe state (torque off) on every exit, refusals included, so it must never be used on the assembled Microduck; step 14 stays read-only and must not call it.
- 2026-11-16: The time-based cool-down is remembered per adapter object in one process only (adapter clocks are not comparable across processes). A CLI that runs one experiment per invocation (step 7) gets only the `start_temperature_max` check unless it also reads the previous run record's end time.
- 2026-11-16: The monitor refuses an adapter that cannot read `current_a`, `voltage_v` and `temperature_degc`. If the HD1910 reports only a raw "load" (step 9), how `current_max` is enforced is a decision for the owner, not a check to drop.
- 2026-11-16: Suggested step-7 mapping of `Outcome` to exit codes: completed 0 or 1 (by analysis); refused with stop kind `spec_invalid` or `adapter_unsuitable` 2; other refusals, aborted and interrupted 3; error 4; `safe_state_not_confirmed` 4 with the summary shown first and in full.

- 2026-11-16: On the default simulated servo, any step big enough to saturate the torque limit (above about 32 deg) peaks above 380 deg/s, past the template's 180 deg/s `speed_max`; a saturating experiment for step 8's torque-limit identification would be aborted by the safety monitor under the template's limits, so it needs a decision (a bench file with a higher speed limit for that test, or another experiment), not a loosened check.
- 2026-11-16: `model.py` assumes the pendulum arm hangs straight down at position zero and is a point mass at its centre of mass (inertia m L^2). The bench rig must be mounted that way, or the bench file needs a hanging-angle setting, before step 11; raise it with the owner then.
- 2026-11-16: The contract has no dimension for a raw, unscaled device value (a Feetech gain register or "load" percentage, if the register table gives no SI scaling). Add one to `units.Dimension` and `contract.UNIT_SUFFIXES` in step 9 rather than passing a bare number.
- 2026-11-16: `limits.py` rejects unknown sections and keys so a typo in a limit cannot be silently ignored; a new bench-file setting (step 3's fault injection, step 4's starting temperature and missed-reply count) must be added to the field tables or `_SECTIONS` and to `TEMPLATE`. Temperatures are stored in degC (`_degc`), not kelvin.

- 2026-11-16: Before any public push (step 15): the workshop transcript in `docs/reference/` is other people's talks and is in the git history from the first commit, and commits so far carry a neutral author identity because git has none configured. Raise both with the owner then.
- 2026-11-16: The owner confirmed the name `servobench` and the Apache-2.0 licence. Step 1 does not need to ask again.
- 2026-11-15: Plan written. No code exists yet. The development machine is macOS with Python 3.14, `uv` 0.11 and `git` installed. The project folder is not yet a git repository.
