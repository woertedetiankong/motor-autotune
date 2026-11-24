# Call chains

Which function calls which, for the main things the code does. Read by `bearings`; the diagrams of the parts are in `parts.md`.

## A check command, from the typed line to the verdict

`servobench check` is typed in a shell. The chain ends when the result has been written and the process exits with the result's code. It runs once per invocation.

```calls
main | Parses the command line, runs the chosen command inside a safe-state watch, and writes the result.
  in argv: the words typed after `servobench` | the shell, or a test
  out return: the process exit code | the shell
  build_parser | Defines the commands and their options.
  watch_safe_state | Collects every run in this block whose safe state the device never confirmed.
    out return: the `SafeStateWatch` | `keep_safe_state_report`, after the command
  _check | The handler for `check`: loads the bench file, then runs the check.
    Called through `args.handler`, which `build_parser` set for the `check` command.
    in args: the parsed options: `bench`, `no_motion`, `json` | `main`
    out return: the command's `CommandResult` | `main`
    load_bench | Reads, validates and fingerprints `bench.toml`.
      in path: where the bench file is | `--bench`, default `bench.toml` in the current folder
      out return: the loaded `Bench`: target, limits, load, settings, fingerprint | `run_check`
      expand 1
    run_check | Decides every check item and returns the command's result.
      in bench: target, limits, load and settings | `load_bench`
      in motion: False for `--no-motion` | `_check`
      in adapter: an adapter already open, or None to open one | `characterize` passes its own
      out return: the `CommandResult`, with every item's status in its metrics | `main`
      open_adapter | Creates the adapter that the bench file's `[target]` names.
        in bench: the loaded bench file | `run_check`
        out return: the adapter; nothing on the actuator has changed | every later step
      _Check.read_only | Reads only: describe, every signal, the faults, the bus test; sets the read-only items.
        in adapter: the open adapter | `open_adapter`
        out self.items: each read-only item's status | `_Check.may_move`, `_Check.finish`
        expand 1
      branch whether the actuator is moved | `--no-motion`, and then whether every gate item passed
        case read-only | `motion` is False
          _Check.skip_motion | Marks the motion items skipped, with the reason.
        case motion asked | `motion` is True; `move` runs only if `may_move` returns True
          _Check.may_move | Whether every gate item passed; if not, marks the motion items skipped.
          _Check.move | One small step up and back through the runner, then direction and temperature rise.
            in adapter: the open adapter | `open_adapter`
            out self.result: the runner's `RunResult` | `_Check.finish`
            _Check.previous_run | Finds from the run records when this actuator last ran, and tells the runner, for the cool-down.
              in adapter: the open adapter, for its identity and clock | `open_adapter`
              out return: what the records say about the last run and the rest still owed | the result's `cooldown` metrics
            run_experiment | The runner; see "One experiment through the runner".
              in spec: one `Step`, up by `[check] move` and back | built in `_Check.move`
              out return: the `RunResult` | `judge_run`, `analyse_step`
            judge_run | The verdict a run's outcome forces, or None if it completed.
              in result: the runner's `RunResult` | `run_experiment`
              out return: a forced verdict and exit code, or None | when not None, the motion items become unknown
            analyse_step | Measures the step up and the step back from the recorded run.
              in trace: the recorded run | `result.trace`
              in spec: the step that was run | `_Check.move`
              out return: `StepAnalysis`, one entry per step | `_Check.direction_item`
            _Check.direction_item | Whether each step moved the commanded way, far enough.
              in analysis: the measured steps | `analyse_step`
              out return: the `direction` item | `self.items`
            _Check.temperature_rise_item | Whether the moves warmed the actuator more than allowed.
              in result: the run, for its temperature readings | `run_experiment`
              out return: the `temperature_rise` item | `self.items`
      _Check.finish | Works out the overall verdict and writes the run record.
        out return: the `CommandResult` | `run_check`
        expand 1
  keep_safe_state_report | Makes the result say so if any run left the safe state unconfirmed.
    in result: what the command returned | `_check`
    in watch: the runs whose safe state was not confirmed | `watch_safe_state`
    out return: the same result, changed only if a run was unconfirmed | `_emit`
  _emit | Writes the result, the unconfirmed-safe-state warning first, and returns its exit code.
    in result: the final `CommandResult` | `keep_safe_state_report`
    in as_json: whether `--json` was given | `main`
    out return: the exit code | `main`
    expand 1
```

## One experiment through the runner

Every experiment that moves an actuator enters here, from `check` or `characterize`. The chain ends with the actuator asked to enter its safe state and a `RunResult` returned. It runs once per experiment.

```calls
run_experiment | Takes one experiment through every stage and always ends by entering the safe state.
  in adapter: the actuator, behind the adapter contract | the command, from `open_adapter`
  in bench: the limits and runner settings in force | `load_bench`
  in spec: the experiment: a hold, a step or a stepped sine | the command
  in rehearsal_params: servo model parameters for the rehearsal | the default placeholders unless the command gives a fitted set
  out return: `RunResult`: outcome, stop reason, safe-state report, both traces | the command
  _ctrl_c_requests_stop | While inside, Ctrl-C sets a flag instead of raising.
  _Run.validate | Stage 1, nothing contacted: the spec against the limits and against what the adapter describes.
    out return: the problems found; any problem refuses the run | `run_experiment`
    expand 1
  _Run.stages | Stages 2 to 5 in order. A stage that stops the run raises `_Stopped`.
    in stop_request: the Ctrl-C flag | `_ctrl_c_requests_stop`
    _Run.run_preflight | Stage 2, reading only: the faults and one sample; refuses on a fault or a reading outside the limits.
      out return: the plan: the spec's segments with the approach ramp in front | `_Run.rehearse`, `_execute`
      out self.preflight: the sample read | `_rehearsal_servo`
      build_plan | Puts the approach ramp from the present position in front of the spec's segments.
        in spec: the experiment | `run_experiment`
        in start_position_rad: where the actuator is now | the preflight sample
        in approach_speed_rad_s: how fast the ramp may go | the bench file's `[runner]`
        out return: the timed segments, approach first | `_Run.run_preflight`
    _Run.cool_down | Stage 3: inside the cool-down or above the start temperature, waits or refuses as the bench file says.
      out return: True if it waited, so the preflight is repeated | `_Run.stages`
    _Run.rehearse | Stage 4: the same plan on a simulated servo, under a monitor of its own.
      in plan: the timed segments | `_Run.run_preflight`
      out self.rehearsal_trace: what the rehearsal did | `_Run.result`
      _rehearsal_servo | A healthy simulated servo, starting where and as warm as the real one is now.
        in params: the servo model's parameters | `rehearsal_params`
        in start: position and temperature to start from | `self.preflight`
        out return: a `SimServo` on its own virtual clock | the rehearsal's `_execute`
      runner.py:_execute | The same loop as the real run, on the simulated servo.
        in adapter: the rehearsal's simulated servo | `_rehearsal_servo`
        in plan: the same timed segments as the real run | `_Run.run_preflight`
        out trace: what the simulated servo did, and whether the monitor stopped it | `self.rehearsal_trace`
    runner.py:_execute | Stage 5: one reading, enable, then command, read and check on every pass until the plan ends or something stops it.
      in adapter: the real target | `run_experiment`
      in plan: the timed segments, approach first | `_Run.run_preflight`
      in monitor: a fresh `Monitor` holding the bench limits | `_Run.stages`
      in period_s: the time between passes | the adapter's sample rate, or the spec's if slower
      in stop_request: the Ctrl-C flag | `_ctrl_c_requests_stop`
      in timer: times every adapter call against the adapter's declared bound | `_Run.stages`
      out trace: samples, commands, missed reads and the stop reason | `_Run.stages` freezes it into `self.target_trace`
      SimServo.read | One reading of every signal.
        out return: a `Sample`: position, velocity, current, voltage, temperature, one timestamp | `Monitor.on_sample`, the trace
        Called as `adapter.read()` through the `Adapter` interface; the simulated servo is the only adapter today.
      Monitor.on_sample | Checks one reading against the limits, the following checks and the gap since the last reading.
        out return: the first `Violation`, or None | `_execute` stops the run on one
      SimServo.enable | Torque on, holding the present position. Called as `adapter.enable()`.
      Monitor.check_time | Whether too long has passed without a good reading, or the run has been enabled too long.
        in t_s: the time now, on the adapter's clock | `_execute`
        out return: a `Violation`, or None | `_execute`
      Monitor.check_command | Whether the next setpoint is allowed, before it is sent.
        in position_rad: the setpoint about to be sent | the segment's `setpoint_rad`
        out return: a `Violation` if it is outside the command band, or None | `_execute`
      SimServo.command | Sends one position setpoint. Called as `adapter.command(...)`.
        out return: when the adapter sent it, on its clock | recorded in the trace, told to `Monitor.on_command`
      Monitor.on_command | Remembers what was commanded and when, for the following checks.
        in t_s: when the adapter sent it | `SimServo.command`
        in position_rad: what was commanded | `_execute`
      _missed | Records a read or fault poll that got no reply; whether that stops the run.
        in error: the `CommunicationError` the call raised | the adapter
        in poll: True for a fault poll, False for a read | `_execute`
        out return: True if the monitor stops the run for it | `_execute`
      SimServo.read_faults | The device's own fault report, polled every half second. Called as `adapter.read_faults()`.
      Monitor.on_faults | A fault the device reports stops the run.
        in faults: what the device reports | `SimServo.read_faults`
        out return: a `Violation` if there is any fault, or None | `_execute`
      Monitor.check_end | The last check when the plan has run to its end.
        in t_s: the time now | `_execute`
        out return: a `Violation` if the run ended without a good reading, or None | `_execute`
  _enter_safe_state | Asks the adapter to enter its safe state, retrying; never raises.
    Runs in a `finally`, whatever ended the stages.
    in adapter: the real target | `run_experiment`
    out return: `SafeStateReport`: confirmed or not, attempts, errors | `_Run.result`, `_tell_watches`
  _tell_watches | Reports a safe state that was not confirmed to every open `watch_safe_state` block.
    in safe: the safe-state report | `_enter_safe_state`
    in description: which actuator it was | `adapter.describe()`
  _Run.finish | After the safe state: notes when the run ended, for the cool-down, and reads the faults.
  _Run.result | Builds the `RunResult` from everything the stages left behind.
    in stop: why the run stopped early, or None | the stage that stopped it
    in safe: the safe-state report | `_enter_safe_state`
    out return: the `RunResult` | the command
```

## Characterize: measure, fit and export

`servobench characterize` reaches `run_characterize` from `main` through `_characterize`, as `check` reaches `run_check`. The chain ends with the fitted model exported and the command's result returned. On hardware it is expected to take about five minutes.

```calls
run_characterize | Refuses without a `[load]`, opens the adapter, runs the sequence, and reports a crash in full.
  in bench: the loaded bench file | `load_bench`, from `_characterize`
  out return: the `CommandResult` | `main`
  open_adapter | Creates the adapter that the bench file's `[target]` names.
    in bench: the loaded bench file | `run_characterize`
    out return: the adapter | `_Characterize.run`
  _Characterize.run | The sequence: gate, plan, the experiments one by one, then the fit.
    in adapter: the open adapter | `open_adapter`
    out return: the `CommandResult` | `run_characterize`
    run_check | The gate: the full check, motion included, on the same adapter.
      in adapter: the same adapter characterize will use | `open_adapter`
      out return: check's `CommandResult` | `self.check`; its verdict decides the fork below
    branch the gate | by the verdict `run_check` returned
      case check passed | verdict `pass`: the sequence goes on
        plan_experiments | The experiment set, sized from the bench limits with the model's predictions.
          in bench: the limits and the load | `run_characterize`
          in description: what the adapter says it can do | `adapter.describe()`
          out return: the experiments, each marked as fitted or held out | the loop in `_Characterize.run`
          expand 1
      case check did not pass | any other verdict: nothing more is moved
        _Characterize.not_checked | Returns check's own verdict and exit code, saying nothing was characterized.
    _Characterize.wait_cooldown | Before each experiment: waits, on the adapter's clock, for the rest of the cool-down.
      in last_end_s: when the previous run ended, on the adapter's clock | the previous `RunResult`'s safe-state time
    run_experiment | Once per planned experiment; see "One experiment through the runner".
      in spec: the planned experiment | `plan_experiments`
      out return: the `RunResult` | `analyse`, `judge_run`, `write_run`, and later the fit
    analyse | The analysis that suits the experiment.
      in trace: the recorded run | `run_experiment`
      in spec: the experiment that was run | `plan_experiments`
      out return: step or frequency metrics | written into the run record
      branch kind of experiment | by the spec's class
        case step | a `Step` spec
          analyse_step | Delay, rise time, overshoot and settling of every step.
            in trace: the recorded run | `analyse`
            out return: `StepAnalysis` | `analyse`
        case stepped sine | a `SteppedSine` spec
          analyse_stepped_sine | Gain and phase at every frequency.
            in trace: the recorded run | `analyse`
            out return: `FrequencyAnalysis` | `analyse`
    judge_run | The verdict a run's outcome forces, or None if it completed.
      in result: the `RunResult` | `run_experiment`
      out return: a forced verdict and exit code, or None | the fork below
    write_run | Writes this experiment's own run directory.
      in result: the `RunResult` | `run_experiment`
      in metrics: the experiment's name, its role and its analysis | `analyse`
      out return: the run directory's path | the command's list of files
    branch how the experiment ended | by `judge_run`, and whether the record could be written
      case completed | `judge_run` returned None: the loop goes on to the next experiment
      case did not complete | refused, aborted, an error, or the safe state not confirmed
        _Characterize.stopped | Stops the sequence there; nothing is fitted.
          in judgement: the forced verdict | `judge_run`
          out return: the `CommandResult` | `_Characterize.run`
      case record not written | `write_run` raised
        _Characterize.not_recorded | Stops the sequence with verdict `error`.
          in error: why the record could not be written | `write_run`
          out return: the `CommandResult` | `_Characterize.run`
    _Characterize.fit_and_report | Fits the model, tests it on the held-out experiment, exports, and builds the result.
      out return: the `CommandResult` | `_Characterize.run`
      FitExperiment.from_trace | One run made ready for the fit: what was commanded, when, and what was read.
        in trace: one recorded run | `self.results`
        out return: a `FitExperiment` | `fit_model`, `prediction_error`
      fit_model | Least squares from several starting points, then how well each parameter is known.
        in experiments: the runs to fit, the held-out one left out | `FitExperiment.from_trace`
        in load: the pendulum arm | the bench file's `[load]`
        out return: `FitResult`: an estimate for each parameter and combination | `export_json`, `mujoco_fragment`
        expand 1
      prediction_error | Replays the held-out experiment through the model and measures the error.
        Called twice: with the default placeholder parameters and with the fitted ones.
        in params: the parameter set to judge | the defaults, then `fit.prediction_params`
        in experiment: the held-out run | `FitExperiment.from_trace`
        out return: RMS and largest error, in radians | compared with the noise floor
        replay | Replays the run's commands through the model at its reading times.
          in params: the parameter set | `prediction_error`
          in experiment: the run to replay | `prediction_error`
          out return: the model's position at each reading time, and the difference | `prediction_error`
          simulate | The servo model stepped through the recorded commands.
            in command_times_s: when each command was sent | the recorded run
            in command_positions_rad: what was commanded | the recorded run
            in sample_times_s: the times to answer for | the recorded readings
            out return: the model's position, velocity and torque at those times | `replay`
      _Characterize.noise_floor | The error a perfect model would still have, from noise and reading resolution.
        in traces: every recorded run | `self.results`
        out return: a `NoiseFloor` | the fit-quality ratio
      export_json | The contents of `servo_model.json`.
        in estimates: every parameter with its value, uncertainty and status | `fit_model`
        in fit_quality: good or poor, and why | `_Characterize.fit_and_report`
        in conditions: voltage, temperature, load, device, bench fingerprint | `_Characterize.conditions`
        out return: the file's contents | `_Characterize.write_record`
      mujoco_fragment | The MuJoCo fragment; called only when the fit quality is good.
        in estimates: the fitted parameters | `fit_model`
        out return: the XML text | `_Characterize.write_record`
      _Characterize.judge | The verdict, exit code and summary from the fit quality and what could not be identified.
        in quality: good or poor | the held-out error against the noise floor
        in missing: the parameters that are not identifiable | `fit_model`
        out return: verdict, exit code, whether a human is needed, summary, what to do next | the `CommandResult`
      _Characterize.finish | Adds the safe-state report and writes the command's own record.
        in result: the command's result so far | `_Characterize.fit_and_report`, or a step that stopped the sequence
        in exported: the contents for `servo_model.json` | `export_json`
        out return: the same result, with the safe-state report applied | `_Characterize.run`
        _Characterize.write_record | Writes the summary record, the exported files and the held-out overlay plot.
          in result: the final result | `_Characterize.finish`
          in overlay: the data for the held-out plot | `_Characterize.overlay_data`
          plot_overlay | Measured position against the default and the fitted model, as a PNG.
            in path: where to save the PNG | the command's run directory
            in measured_rad: the held-out run's readings | the recorded run
            in fitted_rad: the fitted model's replay | `_Characterize.overlay_data`
```
