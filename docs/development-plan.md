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
- [x] 5. Run records and plots
- [x] 6. Analysis: step response and frequency response
- [x] 7. Command line and `check`, on the simulated servo
- [x] 8. Model fit, `characterize` and simulator export, on the simulated servo
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
  commands/           check, runs, characterize, tune
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
  "verdict": "pass | partial | fail | refused | aborted | error | safe_state_not_confirmed",
  "summary": "one sentence",
  "exit_code": 0,
  "needs_human": false,
  "next": "what to do about it, when there is something to do",
  "command": "check",
  "metrics": {},
  "files": {},
  "run_dir": "runs/..."
}
```

Exit codes: 0 pass; 1 the measurement ran and the verdict is fail; 2 usage or bench-file error; 3 safety abort; 4 device or communication error. Step 7 settled the verdicts (`partial` for a run in which nothing failed but something was not measured; `refused` when the actuator was never enabled; `safe_state_not_confirmed` on its own) and which outcome takes which code: see `servobench.commands` and `docs/agent-usage.md`.

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

Amended 2026-11-16 after step 6 was built and reviewed:
- **Settling time** is within the larger of 5% (10% with noise and jitter) and the uncertainty the analysis itself reports. Measured worst case was 5.6% and 11.4% on an overdamped servo; with an unquantised reading the error was 0.00%, so the shortfall is the 4096-step reading (one step is 0.9% of a 10 deg step, against a 2% settling band), not the analysis.
- **Steady-state error** is within half a reading step, or one reading step with noise.
- **"The model's own frequency response"** means the nonlinear model's response at the experiment's amplitude. With Coulomb friction the small-signal linearisation differs by up to 0.7 dB and 4 deg at 3 deg amplitude.
- An independent review check, with friction and load removed, matched the closed-form transfer function within 0.07 dB and 0.3 deg from 0.5 to 6 Hz.

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

- 2026-11-16: Second review, for steps 9 to 11: `Description.call_time_max_s` is now required: the longest any one adapter call takes to return or raise, whether or not the device answers. The Feetech adapter must set the serial port's read and write timeouts (counting any retries it makes) so that every call keeps to it, declare that worst case, never wait on the bus without a timeout, and pass the conformance tests on it, including the one for a device that does not answer (`ConformanceTarget.make_silent`, for example an id no servo on the bus has).
- 2026-11-16: Second review: the runner refuses an adapter whose `call_time_max_s` is more than half of `sample_gap_max` (`GAP_CALLS_MIN` = 2), states the worst case from the last good reading to the stop request as `sample_gap_max + call_time_max_s` (`runner.stop_time_max_s`, 0.22 s on the simulated servo with the template), and stops a run on any call longer than declared (`call_time_exceeded`, exit 4). The factor of two and the template's 200 ms were reasoned on the simulator only; in step 10, measure the real servo's longest read (`check`'s bus item reports `read_time_longest_s`) and settle both with the owner.
- 2026-11-16: Second review, for step 9 and the owner: the host cannot stop the actuator while a call is blocked, or at all if the computer hangs or the cable is pulled; only a safe state the device enters by itself on lost communication covers that. Find out from Feetech's documents whether the HD1910 has one, and record it in `docs/targets.md` and in the adapter's `safe_state_on_comm_loss`. No host-side watchdog thread was added: a second writer on the half-duplex bus during a pending transaction is a hazard of its own.
- 2026-11-16: Second review: the CLI writes a `safe_state_not_confirmed` summary to stderr, flushed, before stdout, and a stream that cannot be written never raises or changes the exit code (its descriptor is pointed at the null device so the interpreter's flush at exit cannot turn the code into 120). New `[sim]` fault `ignores_safe_state` produces that verdict from a bench file. Any later command (`tune`, step 13) must return through `cli._emit` and never print on its own.
- 2026-11-16: Second review: the earlier note's second half (an unreadable latest record dropping the cross-invocation cool-down) is fixed: `records.records_for_cooldown` counts a directory named like a record whose `run.json` cannot be read as a run of this actuator that ended at the end of the second the time in its name gives (corrected in the third review, below; just now if that time is invalid or in the future), and `check` and `characterize` say which record and how the owner gets out (repair or move it). It counts for every actuator, since the target in a name cannot be trusted; for step 14 with 15 servos, one corrupt record delays them all by one cool-down, which the owner may want narrowed.
- 2026-11-16: Third review: a run directory's name holds whole seconds, cut down, so taking its time as the end of an unreadable record's run placed the end up to a second too early and a corrupt record could let a short cool-down run out early. `UnreadableRecord.assumed_end` now takes the end of that second, and just now while that second is the current one (not a record with no usable time: `needs_human` stays false). Left for the owner: after `safe_state_not_confirmed`, the record's `ended_utc` is when the tool gave up, not when the actuator was made safe (a human cuts its power later), so the next invocation's cool-down counts from too early; the tool cannot know when power was cut, and what to do (refuse until the owner says, or count from the first later invocation) is the owner's decision.

- 2026-11-16: Review fixes: new required bench-file setting `[runner] sample_gap_max` (template "200 ms"): the longest an enabled actuator may go without a good reading, and how far a run may overrun its plan. 200 ms was sized on the simulated servo (three missed replies at its 20 ms timeout cost about 90 ms); for steps 10 and 11, set it with the owner from the real bus's reply timeout and rate, and any bench file written before this needs the key added by its owner.
- 2026-11-16: Review fixes: the runner refuses a spec whose segments are shorter than `SEGMENT_PERIODS_MIN` (10) sampling periods or whose period does not fit `GAP_PERIODS_MIN` (4) times into `sample_gap_max`, and stops a run that falls behind its plan (stop kind `behind_schedule`, exit 4); both constants assume a pass of the loop takes at most three transactions and should be checked against the real servo's timing in step 10.
- 2026-11-16: Review fixes, for step 13: `tune` must run inside `runner.watch_safe_state()`, pass every result it records or returns through `commands.keep_safe_state_report`, and report a failed write with `commands.record_not_written`, as `check` and `characterize` do; then an unconfirmed safe state can never be replaced by a later failure.
- 2026-11-16: Found in the review and left for the owner: the frozen-reading check is reset by any change in the reading, so a stuck sensor that flickers by one step is never "frozen" (the follow check still catches it once the commands move away); and `records.latest_enabled_run` skips an unreadable record, so a corrupt latest record lets the cross-invocation cool-down fall back to `start_temperature_max` only.

- 2026-11-16: For step 12: `characterize` swings the arm slowly to +/-40 deg (the gravity sweep; half the command range on the narrower side of zero, at most 40 deg) as well as stepping about 12 deg; agree that travel with the owner before the first run. On hardware it takes about 5 min, mostly four 60 s cool-downs.
- 2026-11-16: For step 12: the experiment set is sized by the default placeholder model with a 20 % speed margin; a real servo more than about 25 % faster will be aborted on `speed_max` in the first experiment. That is a finding for the owner, not a reason to resize silently.
- 2026-11-16: For step 12: `GOOD_FIT_NOISE_RATIO` (1.5) and the noise floor were calibrated on the simulated servo only (a correct model scores 0.89 to 1.12; 0.5 deg of backlash 1.64 to 1.70; 0.3 deg is not caught). Revisit both with the owner once real data shows its own noise and timing.
- 2026-11-16: For step 13: under the template's limits only kd + viscous is measured, never the split, and the torque limit only as a lower bound; proposing a new kd needs a decision on that split (or an experiment that saturates, which the template's speed limit forbids).
- 2026-11-16: The MuJoCo fragment was checked by loading it in MuJoCo 4.0.1 (installed in a throwaway environment, not a dependency): every attribute lands where intended, and a 10 deg step differs from `servobench.model` by at most 0.074 deg at a 2 ms timestep. Nothing in the test suite runs MuJoCo.
- 2026-11-16: Backlash in the simulator (`SimConfig.backlash_rad`) and hidden servo parameters are set only from Python, not from bench.toml, so `characterize` on the command-line simulator always measures the default model (fitted and default errors equal).

- 2026-11-16: For steps 10 and 11: `check`'s zero item assumes the actuator is at rest with torque off when check starts (a servo holding torque reads where it holds, not where the arm hangs); find out the HD1910's torque state at power-up, and whether its gearbox lets the 50 g arm fall to hanging at all, before trusting that item.
- 2026-11-16: For step 10: `check`'s bus item (200 back-to-back reads, 99 % answered, `[check] sample_rate_min` 100 Hz) and `temperature_rise_max` 3 degC were chosen against the simulated servo; revisit both with the owner once the real servo's rate, jitter and temperature step are measured. The cool-down across invocations finds the previous run by adapter name and device id, so the real adapter must report a stable `device_id`.
- 2026-11-16: The PRD says the tool "restricts what it will run" where a protection is missing; step 7 only reports it (`protections` is a warning, never a fail). What to restrict, for example longer experiments without `safe_state_on_comm_loss`, is a decision for the owner, not yet made.
- 2026-11-16: For step 14: `check --no-motion` calls only `describe`, `read` and `read_faults` (a test checks this), never `run_experiment` or `enter_safe_state`, so it can be reused on the assembled robot; `check` with motion must not be.

- 2026-11-16: Step 6's settling-time tolerance is open with the owner: at 4096 steps per turn the plan's 5 % (10 % with noise) is missed on an overdamped servo (5.6 %, 11.4 %), and on the default servo's 10 deg steps the analysis refuses many settling times as unresolvable (3 % overshoot against the 2 % band). The tests use the proposed tolerance, the larger of the plan's figure and the uncertainty the analysis reports.
- 2026-11-16: For steps 8 and 13: with Coulomb friction the model's frequency response depends on amplitude (at 3 deg it differs from the linearisation by up to 0.7 dB and 3.8 deg, at 1 deg by 2 dB and 11 deg), so fit by replaying through `model.simulate`, not a linear transfer function; and `tune` on settling time needs steps of 14 deg (the template's largest) or a decision, because 10 deg steps sometimes get none (with noise, 17 of 180 steps of 10 and 14 deg; without, both 10 deg return steps).
- 2026-11-16: For step 7: the analysis warns on a reversed direction but does not judge the zero; a wrong zero shows only as `position_before_enable_rad` and a steady offset, so `check` must compare those with the bench's zero itself.
- 2026-11-16: Two paths reach an actuator outside `run_experiment`, reported to the owner at step 4 and not yet decided: the conformance suite's `motion` tests command the adapter directly (step 11 runs them on the real servo), and `write_parameter` is not routed through the runner (step 13 writes gains; rehearse them by passing `rehearsal_params`, and settle with the owner how the write itself is guarded).
- 2026-11-16: `run_experiment` enters the safe state (torque off) on every exit, refusals included, so it must never be used on the assembled Microduck; step 14 stays read-only and must not call it.
- 2026-11-16: The time-based cool-down is remembered per adapter object in one process only (adapter clocks are not comparable across processes). A CLI that runs one experiment per invocation (step 7) gets only the `start_temperature_max` check unless it also reads the previous run record's end time.
- 2026-11-16: The monitor refuses an adapter that cannot read `current_a`, `voltage_v` and `temperature_degc`. If the HD1910 reports only a raw "load" (step 9), how `current_max` is enforced is a decision for the owner, not a check to drop.
- 2026-11-16: Measured through the runner, with the default model, the template's arm and its 180 deg/s `speed_max`: a 14 deg step peaks at 174 deg/s and passes rehearsal, a 15 deg step peaks at 181 deg/s and is refused. Speed, not torque saturation (about 32 deg), is what limits step size under the template.
- 2026-11-16: Suggested step-7 mapping of `Outcome` to exit codes: completed 0 or 1 (by analysis); refused with stop kind `spec_invalid` or `adapter_unsuitable` 2; other refusals, aborted and interrupted 3; error 4; `safe_state_not_confirmed` 4 with the summary shown first and in full.

- 2026-11-16: On the default simulated servo, any step big enough to saturate the torque limit (above about 32 deg) peaks above 380 deg/s, past the template's 180 deg/s `speed_max`; a saturating experiment for step 8's torque-limit identification would be aborted by the safety monitor under the template's limits, so it needs a decision (a bench file with a higher speed limit for that test, or another experiment), not a loosened check.
- 2026-11-16: `model.py` assumes the pendulum arm hangs straight down at position zero and is a point mass at its centre of mass (inertia m L^2). The bench rig must be mounted that way, or the bench file needs a hanging-angle setting, before step 11; raise it with the owner then.
- 2026-11-16: The contract has no dimension for a raw, unscaled device value (a Feetech gain register or "load" percentage, if the register table gives no SI scaling). Add one to `units.Dimension` and `contract.UNIT_SUFFIXES` in step 9 rather than passing a bare number.
- 2026-11-16: `limits.py` rejects unknown sections and keys so a typo in a limit cannot be silently ignored; a new bench-file setting (step 3's fault injection, step 4's starting temperature and missed-reply count) must be added to the field tables or `_SECTIONS` and to `TEMPLATE`. Temperatures are stored in degC (`_degc`), not kelvin.

- 2026-11-16: Before any public push (step 15): the workshop transcript in `docs/reference/` is other people's talks and is in the git history from the first commit, and commits so far carry a neutral author identity because git has none configured. Raise both with the owner then.
- 2026-11-16: The owner confirmed the name `servobench` and the Apache-2.0 licence. Step 1 does not need to ask again.
- 2026-11-15: Plan written. No code exists yet. The development machine is macOS with Python 3.14, `uv` 0.11 and `git` installed. The project folder is not yet a git repository.
