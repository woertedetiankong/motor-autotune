Explain: `servobench` is a command-line tool for running small, bounded experiments on one actuator and getting measured facts back. A person or their coding agent types a command; the tool reads the owner's limits from `bench.toml`, rehearses the experiment on a simulated servo, runs it while a safety monitor watches every reading, switches the torque off however the run ended, and then prints a verdict and saves the run as plain files.
Explain: Today every experiment runs on the simulated servo only. The code that talks to a real servo is not written yet, so nothing on this map moves hardware.

```flow
Engineer or agent | a person, or their coding agent | user
bench.toml | the owner's limits file | card | external
Run folder | plain files under runs/ | database | external
Engineer or agent <-> Commands: command, verdict
bench.toml -> Safe experiments: target, limits
Commands <-> Safe experiments: experiment, result
Safe experiments <-> Actuators: setpoints, readings
Commands <-> Actuators: direct readings
Commands <-> Results: runs, metrics
Results <-> Actuators: model replay
Results <-> Run folder: record files
Project support | tests, docs and packaging, for the people building the tool
```

```flow A health check on one servo
Engineer or agent | types the command | user
Engineer or agent -> Command line: check command
Command line -> Health check: loaded bench
Health check -> Adapter contract: read-only items
Health check -> Experiment plans: small step
Experiment plans -> Experiment runner: timed plan
Experiment runner -> Simulated servo: rehearsal
Experiment runner -> Safety monitor: every sample
Experiment runner -> Health check: recorded run
Health check -> Response analysis: recorded run
Health check -> Run records: run record
Health check -> Command line: verdict
```

```flow Characterizing a servo into a simulator model
Engineer or agent | types the command | user
Engineer or agent -> Command line: characterize command
Command line -> Characterize: loaded bench
Characterize -> Health check: gate first
Characterize -> Experiment plans: steps, sines
Experiment plans -> Experiment runner: timed plans
Experiment runner -> Characterize: recorded runs
Characterize -> Model fit: recorded runs
Model fit -> Servo model: candidate replay
Characterize -> Simulator export: estimates
Characterize -> Run records: model files
```

# Commands

What a person or their coding agent types, and the verdict they get back.
Icon: lang
Explain: These are the things you can ask the tool to do: check that an actuator is alive and set up correctly, measure how it behaves, and look at past runs. Each command is a thin layer that calls on the other groups and always answers the same way: a verdict first, one sentence, an exit code, and whether a human must take over.

```flow
Engineer or agent | a person, or their coding agent | user
Engineer or agent <-> Command line: command, verdict
Command line <-> Safe experiments: bench file
Command line <-> Health check: bench, result
Command line <-> Characterize: bench, result
Command line <-> Past runs: run name, result
Characterize <-> Health check: gate first
Health check <-> Actuators: direct readings
Health check <-> Safe experiments: test move, result
Characterize <-> Safe experiments: experiments, results
Health check <-> Results: run, record
Characterize <-> Results: runs, fit
Past runs <-> Results: saved records
```

## Command line

Reads the typed command, runs it, and prints the verdict as text or JSON.
Icon: lang
Explain: This is the front door. It works out which command was typed, loads the bench file for commands that need it, runs the command, and writes the result with the verdict first. It also decides what each kind of ending means: which verdict and which exit code a refused, aborted or failed run gets.
Explain: Nothing is allowed to escape as a bare crash: a typing mistake, a bad bench file, Ctrl-C and a bug each come back as a result with a verdict. If any run ended without the actuator confirming it was made safe, that warning is written before anything else. Any new command has to return its result through here and never print on its own.
- `src/servobench/cli.py`
- `src/servobench/commands/__init__.py`

```flow
Engineer or agent | a person, or their coding agent | user
Parse arguments | which command, which options
Run command | inside a watch for unconfirmed safe states
Write result | verdict first, as text or JSON
Engineer or agent -> Parse arguments: typed command
Parse arguments -> Run command: chosen command
Parse arguments -> Write result: usage error
Run command <-> Bench file: bench.toml, limits
Run command <-> Health check: bench, result
Run command <-> Characterize: bench, result
Run command <-> Past runs: run name, result
Run command -> Write result: command result `CommandResult`
Write result -> Engineer or agent: verdict, exit code
```

## Health check

The `check` command: is this actuator alive and set up correctly?
Icon: search
Explain: It first only reads from the actuator: does it answer, is it the device the bench file names, does it report a fault, are voltage and temperature in range, is the connection fast and reliable enough, which protections does it claim, and is it resting where zero should be. Only if those pass does it make one small move up and back, to see that the actuator turns the commanded way and does not heat up too much.
Explain: With `--no-motion` it never moves anything, which is the form meant for an assembled robot. Every item ends as pass, fail, warning, skipped or unknown; a skipped or unknown item is never counted as a pass. Its thresholds were chosen on the simulated servo and are due to be re-measured on real hardware.
- `src/servobench/commands/check.py`

```flow
Read-only items | answers, identity, faults, voltage, temperature, bus, zero
Test move | one small step up and back
Verdict | every item's status, and the overall result
Command line -> Read-only items: loaded bench
Characterize -> Read-only items: gate request
Read-only items <-> Adapter contract: describe, readings
Read-only items -> Test move: gates passed
Read-only items -> Verdict: motion skipped
Test move <-> Experiment runner: small step, result
Test move <-> Response analysis: recorded run, metrics
Test move -> Verdict: direction, temperature rise
Verdict <-> Run records: record, earlier runs
Verdict -> Command line: result `CommandResult`
```

## Characterize

The `characterize` command: measures how the actuator behaves and exports a model of it.
Icon: flask
Explain: It refuses unless the bench file describes the pendulum arm, because the arm's known weight is what lets it tell the servo's inertia from its gains. It then runs the whole health check, motion included, and goes on only if that passes. After that it runs a set of experiments sized from the bench limits: steps of several sizes, a stepped sine, one slow large swing, and a second set of steps kept aside. Each one goes through the experiment runner, with a rest between them.
Explain: The model is fitted to the first group and then tested on the steps that were kept aside; the result says how well each number is known and whether the fitted model is usable at all. If any run does not complete, the sequence stops there and nothing is fitted. On real hardware it is expected to take about five minutes, mostly rests.
- `src/servobench/commands/characterize.py`

```flow
Gate | needs the arm described, then a full check
Run experiments | sized from the limits, one at a time, with rests
Fit and validate | fit on most runs, test on the held-out one
Command line -> Gate: loaded bench
Gate <-> Health check: full check, verdict
Gate -> Run experiments: check passed
Run experiments <-> Experiment runner: experiment, result
Run experiments -> Run records: each run
Run experiments -> Fit and validate: recorded runs
Fit and validate <-> Model fit: runs, estimates
Fit and validate <-> Simulator export: estimates, model files
Fit and validate -> Run records: summary, files
Fit and validate -> Command line: result `CommandResult`
```

## Past runs

The `runs` command: lists recent run records, or shows one.
Icon: book
Explain: It only reads files that earlier commands saved. Its own verdict says whether reading worked; the verdict of the run being shown is that run's own and is reported inside the output.
- `src/servobench/commands/runs.py`

```flow
List recent | the newest runs, one line each
Show one | by path, by name, or the latest
Command line <-> List recent: how many, list
Command line <-> Show one: run name, record
List recent <-> Run records: run summaries
Show one <-> Run records: one record
```

# Safe experiments

Everything that keeps an experiment inside the owner's limits.
Icon: lock
Explain: This group is the safety layer. It reads the limits the owner wrote, describes experiments as timed plans, and runs each plan through the same fixed stages, with a monitor watching every reading. There is one way to run an experiment and no option that skips a stage.

```flow
bench.toml | the owner's limits file | card | external
bench.toml -> Bench file: target, limits
Commands <-> Bench file: file path, limits
Commands -> Experiment plans: chosen shape
Bench file -> Experiment runner: limits, settings
Experiment plans -> Experiment runner: timed plan
Experiment runner <-> Safety monitor: every sample
Experiment runner <-> Actuators: setpoints, readings
Experiment runner -> Commands: recorded run
```

## Bench file

Reads the owner's `bench.toml` (target, limits, load) and the unit rules every number passes.
Icon: card
Sensitive: these are the limits that every experiment is held to; the file belongs to the owner.
Rule: Do not edit `bench.toml` or loosen a limit check to make something pass. (CLAUDE.md, 2026-11-16)
Rule: Every quantity carries an explicit unit. A bare number is an error. (CLAUDE.md, 2026-11-16)
Explain: `bench.toml` is a small file the owner writes: which actuator and how to reach it, the limits (travel, speed, current, temperature, voltage, longest experiment, cool-down), and the load attached. Every value in it is written with a unit, such as "45 deg", and is converted once, here, to standard units; a number without a unit is refused. The whole file is checked and every problem is reported together; an unknown key is an error, so a typo in a limit cannot be silently ignored.
Explain: The tool reads this file and never writes it. The one exception is `servobench init`, which creates a commented template and refuses to overwrite an existing file. Each load also takes a fingerprint of the file's exact bytes, stored in every run record, so a change to the limits is visible afterwards. It does not prevent a change: real enforcement is the servo's own protections and the power supply's current limit.
- `src/servobench/limits.py`
- `src/servobench/units.py`

```flow
bench.toml | the owner's limits file | card | external
Read and fingerprint | the exact bytes, and their SHA-256
Convert units | "45 deg" becomes a standard-unit number
Validate | every problem reported, unknown keys refused
Commands -> Read and fingerprint: file path
bench.toml -> Read and fingerprint: file bytes
Read and fingerprint -> Convert units: values with units
Convert units -> Validate: standard-unit numbers
Validate -> Commands: loaded bench `Bench`
Validate -> Experiment runner: limits, settings
```

## Experiment plans

Describes an experiment as timed stretches of commands: hold, step, or stepped sine.
Icon: note
Explain: An experiment here is only a description of what to command and when; it sends nothing and knows nothing about the actuator. It is checked twice before anything runs: when it is made, that its numbers make sense, and against the bench limits, that every commanded position stays inside the allowed travel, it fits in the longest allowed time, and a sine is not commanded faster than the speed limit.
Explain: The runner then puts a slow approach ramp in front, from wherever the actuator is to the experiment's first position. What a plan cannot know is how the actuator will respond, for example a step that overshoots; that is what the rehearsal and the safety monitor are for.
- `src/servobench/experiments/spec.py`

```flow
Choose shape | hold, step, or stepped sine
Well-formed check | finite numbers, positive durations
Limits check | travel, total time, commanded speed
Add approach | a slow ramp to the first position
Commands -> Choose shape: sizes, timings
Choose shape -> Well-formed check: the numbers
Well-formed check -> Limits check: experiment `ExperimentSpec`
Limits check -> Add approach: allowed experiment
Add approach -> Experiment runner: timed plan `build_plan`
```

## Experiment runner

The one way to run an experiment: validate, preflight, cool-down, rehearsal, run, safe state.
Icon: play
Sensitive: this is the code that enables an actuator and must always switch it off again.
Explain: Every experiment goes through the same stages in the same order. First it checks the plan against the limits and against what the actuator says it can do, without contacting it. Then it reads the faults and one sample, and waits or refuses if the actuator is too warm or ran too recently. Then it rehearses the same plan on a simulated servo; if the rehearsal leaves the limits, the real actuator is never enabled. Only then does it enable the actuator and run the plan, sending a command, taking a reading and asking the safety monitor on every pass.
Explain: Whatever ends the run, a normal finish, a limit, Ctrl-C, an error or a bug, the next thing it does is ask the actuator to enter its safe state (torque off), retrying if needed, and the result says whether the actuator confirmed it. The rehearsal checks the experiment as designed against a model; it is not a prediction of the real actuator. Because a run always ends with torque off, this must never be used on an assembled robot that needs to keep holding itself up.
- `src/servobench/experiments/runner.py`
- `src/servobench/experiments/__init__.py`

```flow
Validate | plan against limits and the actuator's description
Preflight | faults, one reading, cool-down
Rehearsal | the same plan on a simulated servo
Real run | command, read, check, every pass
Safe state | torque off, whatever happened
Experiment plans -> Validate: timed plan
Bench file -> Validate: limits, settings
Validate -> Preflight: plan allowed
Preflight <-> Actuators: faults, one reading
Preflight -> Rehearsal: cool, no faults
Rehearsal <-> Simulated servo: same plan
Rehearsal -> Real run: stayed inside
Real run <-> Actuators: setpoints, readings
Real run <-> Safety monitor: every sample
Real run -> Safe state: any ending
Safe state -> Actuators: torque off
Safe state -> Commands: recorded run `RunResult`
```

## Safety monitor

Checks every command, reading and missed reply of a run against the bench limits.
Icon: lock
Sensitive: this decides when a moving actuator must be stopped.
Explain: The monitor never talks to the device. The runner tells it about each command, each reading, each read that got no answer, each fault poll and the time, and the monitor answers with the first thing that breaks a limit, or nothing. It checks position, speed, current, temperature and voltage against the limits. It also checks that the actuator is following its commands, which catches a servo turning the wrong way, running away, stuck, or a sensor frozen at a harmless value.
Explain: It also limits how long the tool may go blind: too many missed replies in a row, or too long without a good reading, stops the run, and so does a run that stays enabled past its planned time. The rehearsal runs under a monitor of its own with the same limits. Loosening a check here to make something pass is against the project's rules.
- `src/servobench/experiments/monitor.py`

```flow
Limit checks | position, speed, current, temperature, voltage
Following checks | not following, or a frozen reading
Time checks | missed replies, too long blind, enabled too long
Fault check | the device's own fault report
First violation | the first limit broken, or nothing
Experiment runner -> Limit checks: each reading
Experiment runner -> Following checks: commands, readings
Experiment runner -> Time checks: missed replies, time
Experiment runner -> Fault check: fault polls
Limit checks -> First violation: limit broken
Following checks -> First violation: not following
Time checks -> First violation: blind too long
Fault check -> First violation: device fault
First violation -> Experiment runner: stop reason `Violation`
```

# Actuators

The one interface to an actuator, and the simulated servo behind it.
Icon: plug
Explain: Everything else in the tool talks to an actuator through one narrow interface, so that a new actuator needs only one new piece of code. Today a single actuator sits behind it: a simulated servo, built on a mathematical model of a position servo swinging a pendulum arm. The same model is reused when a real servo's behaviour is fitted.

```flow
Safe experiments <-> Adapter contract: setpoints, readings
Commands <-> Adapter contract: direct readings
Adapter contract <-> Simulated servo: same calls
Safe experiments <-> Simulated servo: rehearsal
Simulated servo <-> Servo model: commands, motion
Results <-> Servo model: model replay
Real servo adapter | not built yet: plan steps 9 to 11
```

## Adapter contract

The single interface between the tool and any actuator, with its clock.
Icon: plug
Rule: The tool has no way to clear a device fault. (docs/development-plan.md, 2026-11-16)
Rule: No persistent write to a device without the owner confirming that specific write. (CLAUDE.md, 2026-11-16)
Explain: An adapter is the only code that knows a particular actuator. It must describe itself (what it can read and with which units, how fast, what it can be commanded to do, and which protections the hardware really has), read signals, accept a position command, enable, enter the safe state, and report faults. There is deliberately no call to clear a fault: that is a human's decision.
Explain: Every call must finish within a time the adapter states, whether or not the device answers, because the runner can only act between calls. Each adapter also owns a clock, and all waiting goes through it, so the simulated servo can run on virtual time and tests finish quickly. The bench file names the adapter to use, and a small registry turns that name into the code; adding an adapter is one line there.
- `src/servobench/contract.py`
- `src/servobench/clock.py`
- `src/servobench/adapters/__init__.py`

```flow
Adapter registry | the name in the bench file picks the code
Description | what it can read and do, checked for gaps
Device calls | read, command, enable, safe state, faults
Clock | real or virtual time; all waiting goes through it
Commands -> Adapter registry: adapter name
Adapter registry -> Simulated servo: opens it
Simulated servo -> Description: what it can do
Description -> Safe experiments: signals, rates
Safe experiments <-> Device calls: setpoints, readings
Commands <-> Device calls: direct readings
Device calls <-> Simulated servo: same calls
Safe experiments <-> Clock: now, wait
Clock <-> Simulated servo: virtual time
```

## Simulated servo

A pretend servo that behaves like a real target, faults included.
Icon: box
Explain: It stands in for hardware. Each call takes a realistic amount of time on a pretend serial bus, with jitter and a reply timeout; the position reading comes in steps and with noise; the supply voltage sags with current; the temperature rises with effort. Faults can be switched on from the bench file, such as dropped replies, a stuck position reading or overheating, to prove that the safety layer catches them.
Explain: It runs on virtual time, so it never sleeps and the same seed gives the same result. The runner also builds a fresh one for every rehearsal. Its constants are invented placeholders chosen to behave plausibly; none is a fact about the HD1910 or any real servo.
- `src/servobench/adapters/sim.py`

```flow
Bus transaction | one call takes one bus period, plus jitter
Injected faults | switched on in the bench file, for tests
Catch up | the model is stepped to the present moment
Take reading | steps, noise, voltage sag, temperature
Adapter contract <-> Bus transaction: call, reply
Safe experiments <-> Bus transaction: rehearsal calls
Injected faults -> Bus transaction: missed replies
Bus transaction -> Catch up: time passed
Catch up <-> Servo model: commands, motion
Catch up -> Take reading: true state
Injected faults -> Take reading: stuck, too hot
Take reading -> Bus transaction: noisy sample
```

## Servo model

The equations of a position servo swinging a pendulum arm.
Icon: grid
Explain: This is the mathematics shared by the simulated servo and the model fit. The servo pushes toward its commanded position with a gain, is damped, has a torque limit, friction, and a short delay before it acts on a command; gravity pulls on the arm. The servo's own numbers are the unknowns that the fit estimates; the arm's mass and length come from the bench file.
Explain: It assumes the arm hangs straight down at position zero and is a point mass, so a bench rig mounted differently needs a decision before real use. It advances in fixed small time steps and can answer for any time in between.
- `src/servobench/model.py`

```flow
Replay | recorded commands in, predicted positions out
Integrator | fixed small steps, answers for any time
Setpoint schedule | each command takes effect after the delay
Equations of motion | gain, damping, torque limit, friction, gravity
Parameters | the servo's unknowns, and the arm
Simulated servo <-> Integrator: commands, motion
Results <-> Replay: commands, prediction `simulate`
Replay <-> Integrator: commands, motion
Integrator <-> Setpoint schedule: delayed setpoint
Integrator <-> Equations of motion: one step
Parameters -> Equations of motion: gains, friction, arm
```

# Results

Turns a recorded run into numbers, a fitted model and files on disk.
Icon: database
Explain: Once a run is recorded, this group measures it and keeps it. It measures step and frequency response, fits the servo model to several runs, writes the fitted model in a form a simulator can load, and saves every run as a folder of plain files with a plot. None of it needs an actuator: it works from recorded timestamps and values, live or loaded back from disk.

```flow
Run folder | plain files under runs/ | database | external
Commands <-> Response analysis: recorded run, metrics
Commands <-> Model fit: runs, estimates
Model fit <-> Actuators: model replay
Commands <-> Simulator export: estimates, model files
Commands <-> Run records: results, saved records
Run records <-> Run folder: record files
```

## Response analysis

Measures step response and frequency response from a recorded run.
Icon: layout
Explain: For a step it measures delay, rise time, overshoot, settling time and where the actuator came to rest; for a stepped sine, the gain and phase at each frequency. Everything is taken from the timestamps the run recorded, and nothing assumes readings arrive evenly spaced, because on a real serial bus they do not.
Explain: A value that could not be measured is reported as missing, with the reason, never as a guess. Every result also says how good its inputs were: the timing actually achieved, the resolution of the reading, and an uncertainty for each number.
- `src/servobench/analysis/__init__.py`
- `src/servobench/analysis/step.py`
- `src/servobench/analysis/frequency.py`
- `src/servobench/analysis/quality.py`

```flow
Pick analysis | step response for a step, frequency for a sine
Trace quality | timing achieved, reading resolution
Step response | delay, rise, overshoot, settling
Frequency response | gain and phase at each frequency
Plain metrics | numbers with units, missing ones with reasons
Commands -> Pick analysis: recorded run `analyse`
Pick analysis -> Step response: step runs
Pick analysis -> Frequency response: sine runs
Trace quality -> Step response: timing, resolution
Trace quality -> Frequency response: timing, resolution
Step response -> Plain metrics: step numbers
Frequency response -> Plain metrics: gain, phase
Plain metrics -> Commands: metrics `as_metrics`
```

## Model fit

Estimates the servo model's unknowns from recorded runs, and says how well each is known.
Icon: spark
Explain: It searches for the servo numbers that make the model reproduce what was recorded: for each candidate it replays the commands a run actually sent, at the times they were sent, through the servo model, and compares with every reading. It starts the search from several places so one bad start does not decide the answer.
Explain: It then works out how firmly the data pins each number down. Some cannot be separated by gentle experiments: for example two kinds of damping only show up as their sum unless the motor is pushed to its torque limit. Such a number is reported as not identifiable, with what is known about it, instead of a made-up value. A separate check replays an experiment that was kept out of the fit and compares the error with the noise in the readings.
- `src/servobench/analysis/fit.py`

```flow
Prepare runs | what was commanded, when, and what was read
Least squares | several starting points, best match kept
Uncertainty | how firmly the data fixes each number
Identifiability | known, or not determined, with the reason
Held-out error | a run the fit never saw, against the noise
Commands -> Prepare runs: recorded runs
Prepare runs -> Least squares: commands, readings
Least squares <-> Servo model: candidate replay
Least squares -> Uncertainty: best fit
Uncertainty -> Identifiability: spread per number
Identifiability -> Commands: estimates `FitResult`
Commands -> Held-out error: unseen run
Held-out error <-> Servo model: replay
Held-out error -> Commands: error against noise
```

## Simulator export

Writes the fitted servo numbers in a form a simulator can load.
Icon: mail
Explain: It produces two things for `characterize`. One is a parameter file listing each fitted number with its unit, whether it is known, its uncertainty, and the conditions of the measurement (voltage, temperature, load, device, tool version, bench fingerprint). The other is a MuJoCo fragment that can be included in a robot model.
Explain: A number the data could not determine is left empty, never filled in, and the MuJoCo fragment is written only when the fit was judged good. Nothing in the test suite runs MuJoCo itself; the fragment was checked by hand once.
- `src/servobench/export.py`

```flow
Parameter file | servo_model.json: values, uncertainties, conditions
MuJoCo fragment | joint and actuator settings, as XML
MuJoCo mapping | which fitted number becomes which setting
Commands <-> Parameter file: estimates, file contents `export_json`
Commands <-> MuJoCo fragment: estimates, XML text `mujoco_fragment`
MuJoCo mapping -> MuJoCo fragment: setting names
```

## Run records

Saves each run as a folder of plain files with a plot, and reads them back.
Icon: database
Explain: Every run becomes a folder under `runs/`, named by time, command and target. It holds a description of the run (limits in force, bench fingerprint, what stopped it, the verdict), the readings and commands as tables, the rehearsal, and a picture of commanded and measured position with temperature. If drawing the picture fails, the rest is still saved.
Explain: The records are also read back: to list and show past runs, to analyse a run again, and to find when this actuator last ran so the cool-down holds across separate invocations. A folder that looks like a record but cannot be read is treated as a recent run, to be safe. The `runs/` folder belongs to the user's experiments and is not kept in the repository.
- `src/servobench/records.py`
- `src/servobench/plots.py`

```flow
Run folder | plain files under runs/ | database | external
Write record | run.json and the tables
Draw plot | position and temperature against time
Load and list | one record, or the newest few
Cool-down lookup | when this actuator last ran
Commands -> Write record: result, verdict
Write record -> Draw plot: recorded run
Write record -> Run folder: run.json, tables
Draw plot -> Run folder: plot.png
Run folder -> Load and list: saved files
Load and list -> Commands: records, summaries
Run folder -> Cool-down lookup: earlier records
Cool-down lookup -> Commands: last run's end
```

# Project support

Tests, documents and packaging, for the people building the tool.
Icon: book
Explain: Nothing here runs when a command is used. It is what keeps the tool trustworthy and explains it: the tests that prove each safety rule, the documents that say what is being built and what is known about the hardware, and the files that make it installable.

```flow
Tests | prove each rule; run against the simulated servo
Documentation | the plan, the hardware facts and the rules, for owner and agents
Project setup | name, licence and dependencies, for installing the tool
```

## Tests

Several hundred automated tests, all run against the simulated servo.
Icon: flask
Explain: Each area of the tool has its own tests. One set, the conformance tests, is written to be run against any adapter, so that a future real-servo adapter must pass the same checks as the simulated one; the tests that would move an actuator are marked so they can be left out. The project's rule is that a failing test is never loosened, skipped or special-cased to get a pass.
- `tests/`

## Documentation

What is being built and why, the step-by-step plan, the hardware facts, and the rules.
Icon: note
Rule: Hardware facts come from `docs/targets.md` or a primary source, not from memory. (CLAUDE.md, 2026-11-16)
Explain: The requirements document says what the tool is for; the development plan lists fifteen steps, of which the first eight are done, and carries dated notes for whoever continues; the targets file records each hardware fact with how sure it is. Datasheets go in the reference folder when the owner adds them. Older documents are kept in an archive and must not be followed. This parts list lives here too.
- `docs/`
- `README.md`
- `CLAUDE.md`

## Project setup

The package's name, licence, dependencies and tool settings.
Icon: box
Rule: Do not push, publish or create a public repository without the owner's agreement. (CLAUDE.md, 2026-11-16)
Explain: These files make the tool installable and name the `servobench` command. The project keeps its dependencies few on purpose: four libraries at run time, and adding another needs a stated reason. The licence is Apache-2.0.
- `pyproject.toml`
- `uv.lock`
- `.gitignore`
- `.python-version`
- `LICENSE`
- `src/servobench/__init__.py`
