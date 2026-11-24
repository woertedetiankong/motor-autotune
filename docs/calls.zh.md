# 中文对照：calls.md

<!-- 由 bearings 读取：每条先是 calls.md 里的原文（一字不差），下一行以冒号开头是中文。
     只是代码名字的条目（如 `main`）两种语言相同，照抄一遍，表示不用译。 -->

## A check command, from the typed line to the verdict

A check command, from the typed line to the verdict
: 一条 check 命令，从输入到给出结论

`servobench check` is typed in a shell. The chain ends when the result has been written and the process exits with the result's code. It runs once per invocation.
: 在终端里输入 `servobench check`。这条链到结果写出、进程以结果里的退出码结束为止。每次调用运行一遍。

Parses the command line, runs the chosen command inside a safe-state watch, and writes the result.
: 解析命令行，在“安全状态留意”之内执行选中的命令，然后写出结果。

Defines the commands and their options.
: 定义各条命令和它们的选项。

Collects every run in this block whose safe state the device never confirmed.
: 收集这个代码块里所有“设备始终没有确认安全状态”的运行。

The handler for `check`: loads the bench file, then runs the check.
: `check` 的处理函数：读入台架文件，然后执行体检。

Called through `args.handler`, which `build_parser` set for the `check` command.
: 通过 `args.handler` 调用，它是 `build_parser` 为 `check` 命令设好的。

Reads, validates and fingerprints `bench.toml`.
: 读取、校验 `bench.toml`，并取它的指纹。

Decides every check item and returns the command's result.
: 判定每一个检查项，返回这条命令的结果。

Creates the adapter that the bench file's `[target]` names.
: 创建台架文件 `[target]` 里写的那个适配器。

Reads only: describe, every signal, the faults, the bus test; sets the read-only items.
: 只读：自述、每个信号、故障、总线测试；定下各个只读检查项。

whether the actuator is moved
: 要不要让执行器动

`--no-motion`, and then whether every gate item passed
: 看 `--no-motion`，再看是否每个把关项都通过

read-only
: 只读

`motion` is False
: `motion` 为 False

Marks the motion items skipped, with the reason.
: 把移动类的检查项标为跳过，并写明原因。

motion asked
: 要求移动

`motion` is True; `move` runs only if `may_move` returns True
: `motion` 为 True；只有 `may_move` 返回 True，`move` 才会运行

Whether every gate item passed; if not, marks the motion items skipped.
: 是否每个把关项都通过；没有的话，把移动类检查项标为跳过。

One small step up and back through the runner, then direction and temperature rise.
: 通过运行器做一次小阶跃（去了再回），然后判断方向和温升。

Finds from the run records when this actuator last ran, and tells the runner, for the cool-down.
: 从运行记录里查出这个执行器上次运行的时间，告诉运行器，用于冷却。

The runner; see "One experiment through the runner".
: 运行器；见“一个实验走过运行器”。

The verdict a run's outcome forces, or None if it completed.
: 一次运行的结局所强制的结论；如果它正常完成，则为 None。

Measures the step up and the step back from the recorded run.
: 从记下的运行里测出去程和回程两个阶跃。

Whether each step moved the commanded way, far enough.
: 每个阶跃是否朝指令方向移动了足够远。

Whether the moves warmed the actuator more than allowed.
: 这些移动让执行器升温是否超过允许值。

Works out the overall verdict and writes the run record.
: 得出总的结论，并写下运行记录。

Makes the result say so if any run left the safe state unconfirmed.
: 只要有一次运行的安全状态没有被确认，就让结果把这一点说出来。

Writes the result, the unconfirmed-safe-state warning first, and returns its exit code.
: 写出结果（未确认安全状态的警告最先写），并返回退出码。

## One experiment through the runner

One experiment through the runner
: 一个实验走过运行器

Every experiment that moves an actuator enters here, from `check` or `characterize`. The chain ends with the actuator asked to enter its safe state and a `RunResult` returned. It runs once per experiment.
: 每个会让执行器动的实验都从这里进入，来自 `check` 或 `characterize`。这条链以要求执行器进入安全状态、并返回一个 `RunResult` 结束。每个实验运行一遍。

Takes one experiment through every stage and always ends by entering the safe state.
: 带着一个实验走完每个阶段，并且总是以进入安全状态结束。

While inside, Ctrl-C sets a flag instead of raising.
: 在其中时，Ctrl-C 只设一个标志，而不是抛出异常。

Stage 1, nothing contacted: the spec against the limits and against what the adapter describes.
: 第 1 阶段，不接触任何东西：把实验同限制、同适配器的自述做对照。

Stages 2 to 5 in order. A stage that stops the run raises `_Stopped`.
: 依次执行第 2 到第 5 阶段。某个阶段要停止运行时，抛出 `_Stopped`。

Stage 2, reading only: the faults and one sample; refuses on a fault or a reading outside the limits.
: 第 2 阶段，只读：故障和一个采样；有故障或读数越限就拒绝。

Puts the approach ramp from the present position in front of the spec's segments.
: 在实验各段之前，加上从当前位置出发的接近斜坡。

Stage 3: inside the cool-down or above the start temperature, waits or refuses as the bench file says.
: 第 3 阶段：处于冷却期内或高于起始温度时，按台架文件所说等待或拒绝。

Stage 4: the same plan on a simulated servo, under a monitor of its own.
: 第 4 阶段：同一个计划在仿真舵机上跑，由它自己的监视器看着。

A healthy simulated servo, starting where and as warm as the real one is now.
: 一个健康的仿真舵机，起始位置和温度与真实舵机此刻相同。

The same loop as the real run, on the simulated servo.
: 和真实运行同一个循环，只是跑在仿真舵机上。

Stage 5: one reading, enable, then command, read and check on every pass until the plan ends or something stops it.
: 第 5 阶段：先读一次，使能，然后每一轮发指令、读数、检查，直到计划结束或被什么停下。

One reading of every signal.
: 把每个信号读一次。

Called as `adapter.read()` through the `Adapter` interface; the simulated servo is the only adapter today.
: 通过 `Adapter` 接口以 `adapter.read()` 调用；目前仿真舵机是唯一的适配器。

Checks one reading against the limits, the following checks and the gap since the last reading.
: 把一个读数对照限制、跟随检查，以及距上一个读数的间隔做检查。

Torque on, holding the present position. Called as `adapter.enable()`.
: 力矩接通，保持当前位置。以 `adapter.enable()` 调用。

Whether too long has passed without a good reading, or the run has been enabled too long.
: 是否太久没有有效读数，或者运行使能的时间太长。

Whether the next setpoint is allowed, before it is sent.
: 下一个设定值在发出之前是否被允许。

Sends one position setpoint. Called as `adapter.command(...)`.
: 发送一个位置设定值。以 `adapter.command(...)` 调用。

Remembers what was commanded and when, for the following checks.
: 记住发过什么指令、何时发的，供跟随检查使用。

Records a read or fault poll that got no reply; whether that stops the run.
: 记录一次没有应答的读取或故障查询；并判断这是否要让运行停止。

The device's own fault report, polled every half second. Called as `adapter.read_faults()`.
: 设备自己的故障报告，每半秒查询一次。以 `adapter.read_faults()` 调用。

A fault the device reports stops the run.
: 设备报告了故障，运行就停止。

The last check when the plan has run to its end.
: 计划跑到头时的最后一次检查。

Asks the adapter to enter its safe state, retrying; never raises.
: 要求适配器进入安全状态，会重试；绝不抛出异常。

Runs in a `finally`, whatever ended the stages.
: 在 `finally` 里运行，不管各阶段是怎么结束的。

Reports a safe state that was not confirmed to every open `watch_safe_state` block.
: 把没有被确认的安全状态报告给每一个打开着的 `watch_safe_state` 代码块。

After the safe state: notes when the run ended, for the cool-down, and reads the faults.
: 进入安全状态之后：记下运行结束的时间（供冷却用），并读取故障。

Builds the `RunResult` from everything the stages left behind.
: 用各阶段留下的一切，组装出 `RunResult`。

## Characterize: measure, fit and export

Characterize: measure, fit and export
: 特性测定：测量、拟合并导出

`servobench characterize` reaches `run_characterize` from `main` through `_characterize`, as `check` reaches `run_check`. The chain ends with the fitted model exported and the command's result returned. On hardware it is expected to take about five minutes.
: `servobench characterize` 从 `main` 经 `_characterize` 到达 `run_characterize`，和 `check` 到达 `run_check` 的方式一样。这条链以导出拟合好的模型、返回命令结果结束。在真实硬件上预计要五分钟左右。

Refuses without a `[load]`, opens the adapter, runs the sequence, and reports a crash in full.
: 没有 `[load]` 就拒绝；打开适配器，执行整个流程；如果崩溃，完整地报告出来。

The sequence: gate, plan, the experiments one by one, then the fit.
: 整个流程：把关、安排实验、逐个做实验，然后拟合。

The gate: the full check, motion included, on the same adapter.
: 把关：在同一个适配器上做完整体检，包括移动。

the gate
: 把关

by the verdict `run_check` returned
: 取决于 `run_check` 返回的结论

verdict `pass`: the sequence goes on
: 结论为 `pass`：流程继续

The experiment set, sized from the bench limits with the model's predictions.
: 一组实验，按台架限制并借助模型的预测来确定大小。

check did not pass
: 体检没有通过

any other verdict: nothing more is moved
: 其他任何结论：不再移动任何东西

Returns check's own verdict and exit code, saying nothing was characterized.
: 返回体检自己的结论和退出码，并说明没有做任何特性测定。

Before each experiment: waits, on the adapter's clock, for the rest of the cool-down.
: 每个实验之前：按适配器的时钟，等完剩余的冷却时间。

Once per planned experiment; see "One experiment through the runner".
: 每个安排好的实验一次；见“一个实验走过运行器”。

The analysis that suits the experiment.
: 选用适合这个实验的分析方法。

kind of experiment
: 实验的种类

by the spec's class
: 取决于实验的类

step
: 阶跃

a `Step` spec
: 一个 `Step` 实验

Delay, rise time, overshoot and settling of every step.
: 每个阶跃的延迟、上升时间、超调和调节时间。

stepped sine
: 步进正弦

a `SteppedSine` spec
: 一个 `SteppedSine` 实验

Gain and phase at every frequency.
: 每个频率下的增益和相位。

Writes this experiment's own run directory.
: 写下这个实验自己的运行文件夹。

how the experiment ended
: 这个实验是怎么结束的

by `judge_run`, and whether the record could be written
: 取决于 `judge_run`，以及记录能否写下

completed
: 完成

`judge_run` returned None: the loop goes on to the next experiment
: `judge_run` 返回 None：循环继续做下一个实验

did not complete
: 没有完成

refused, aborted, an error, or the safe state not confirmed
: 被拒绝、被中止、出错，或安全状态未确认

Stops the sequence there; nothing is fitted.
: 流程停在这里；不做拟合。

record not written
: 记录没有写成

`write_run` raised
: `write_run` 抛出了异常

Stops the sequence with verdict `error`.
: 以结论 `error` 停止流程。

Fits the model, tests it on the held-out experiment, exports, and builds the result.
: 拟合模型，在留出的实验上检验，导出，并组装结果。

One run made ready for the fit: what was commanded, when, and what was read.
: 把一次运行整理成可供拟合的形式：发了什么指令、何时发的、读到了什么。

Least squares from several starting points, then how well each parameter is known.
: 从多个起点做最小二乘，然后算出每个参数知道得有多准。

Replays the held-out experiment through the model and measures the error.
: 把留出的实验在模型里回放一遍，并测出误差。

Called twice: with the default placeholder parameters and with the fitted ones.
: 调用两次：一次用默认的占位参数，一次用拟合出的参数。

Replays the run's commands through the model at its reading times.
: 把这次运行的指令在模型里回放，取各个读数时刻的结果。

The servo model stepped through the recorded commands.
: 让舵机模型按记录的指令一步步走。

The error a perfect model would still have, from noise and reading resolution.
: 即使模型完全正确也仍会存在的误差，来自噪声和读数分辨率。

The contents of `servo_model.json`.
: `servo_model.json` 的内容。

The MuJoCo fragment; called only when the fit quality is good.
: MuJoCo 片段；只有拟合质量良好时才调用。

The verdict, exit code and summary from the fit quality and what could not be identified.
: 根据拟合质量和哪些量无法辨识，得出结论、退出码和摘要。

Adds the safe-state report and writes the command's own record.
: 加上安全状态报告，并写下这条命令自己的记录。

Writes the summary record, the exported files and the held-out overlay plot.
: 写下汇总记录、导出的文件，以及留出实验的对比图。

Measured position against the default and the fitted model, as a PNG.
: 实测位置与默认模型、拟合模型的对比，存成 PNG。

# 进和出：每个函数进来什么、出去什么

## A check command, from the typed line to the verdict

the words typed after `servobench`
: 在 `servobench` 后面输入的词

the shell, or a test
: 终端，或某个测试

the process exit code
: 进程的退出码

the shell
: 终端

the `SafeStateWatch`
: 那个 `SafeStateWatch`

`keep_safe_state_report`, after the command
: `keep_safe_state_report`，在命令执行之后

the parsed options: `bench`, `no_motion`, `json`
: 解析好的选项：`bench`、`no_motion`、`json`

`main`
: `main`

the command's `CommandResult`
: 这条命令的 `CommandResult`

where the bench file is
: 台架文件的位置

`--bench`, default `bench.toml` in the current folder
: `--bench`，默认是当前文件夹里的 `bench.toml`

the loaded `Bench`: target, limits, load, settings, fingerprint
: 读好的 `Bench`：目标、限制、负载、设置、指纹

`run_check`
: `run_check`

target, limits, load and settings
: 目标、限制、负载和设置

`load_bench`
: `load_bench`

False for `--no-motion`
: 给了 `--no-motion` 时为 False

`_check`
: `_check`

an adapter already open, or None to open one
: 一个已经打开的适配器；为 None 时自己打开一个

`characterize` passes its own
: `characterize` 会传入它自己的

the `CommandResult`, with every item's status in its metrics
: 那个 `CommandResult`，每个检查项的状态都在它的指标里

the loaded bench file
: 读好的台架文件

the adapter; nothing on the actuator has changed
: 适配器；执行器上什么都没有改变

every later step
: 后面的每一步

the open adapter
: 打开着的适配器

`open_adapter`
: `open_adapter`

each read-only item's status
: 每个只读检查项的状态

`_Check.may_move`, `_Check.finish`
: `_Check.may_move`, `_Check.finish`

the runner's `RunResult`
: 运行器给出的 `RunResult`

`_Check.finish`
: `_Check.finish`

the open adapter, for its identity and clock
: 打开着的适配器，取它的身份和时钟

what the records say about the last run and the rest still owed
: 记录里关于上次运行的信息，以及还欠多少休息时间

the result's `cooldown` metrics
: 结果里的 `cooldown` 指标

one `Step`, up by `[check] move` and back
: 一个 `Step`：按 `[check] move` 的幅度去了再回

built in `_Check.move`
: 在 `_Check.move` 里构造

the `RunResult`
: 那个 `RunResult`

`judge_run`, `analyse_step`
: `judge_run`, `analyse_step`

`run_experiment`
: `run_experiment`

a forced verdict and exit code, or None
: 被强制的结论和退出码，或 None

when not None, the motion items become unknown
: 不是 None 时，移动类检查项变为未知

the recorded run
: 记下的运行

`result.trace`
: `result.trace`

the step that was run
: 实际运行的那个阶跃

`_Check.move`
: `_Check.move`

`StepAnalysis`, one entry per step
: `StepAnalysis`，每个阶跃一项

`_Check.direction_item`
: `_Check.direction_item`

the measured steps
: 测出的各个阶跃

`analyse_step`
: `analyse_step`

the `direction` item
: `direction` 检查项

`self.items`
: `self.items`

the run, for its temperature readings
: 这次运行，取它的温度读数

the `temperature_rise` item
: `temperature_rise` 检查项

the `CommandResult`
: 那个 `CommandResult`

what the command returned
: 命令返回的结果

the runs whose safe state was not confirmed
: 安全状态没有被确认的那些运行

`watch_safe_state`
: `watch_safe_state`

the same result, changed only if a run was unconfirmed
: 同一个结果，只有存在未确认的运行时才被改动

`_emit`
: `_emit`

the final `CommandResult`
: 最终的 `CommandResult`

`keep_safe_state_report`
: `keep_safe_state_report`

whether `--json` was given
: 是否给了 `--json`

the exit code
: 退出码

## One experiment through the runner

the actuator, behind the adapter contract
: 执行器，隔着适配器约定

the command, from `open_adapter`
: 命令，来自 `open_adapter`

the limits and runner settings in force
: 生效的限制和运行器设置

the experiment: a hold, a step or a stepped sine
: 这个实验：保持、阶跃或步进正弦

the command
: 命令

servo model parameters for the rehearsal
: 预演用的舵机模型参数

the default placeholders unless the command gives a fitted set
: 默认的占位值，除非命令给了一组拟合出的参数

`RunResult`: outcome, stop reason, safe-state report, both traces
: `RunResult`：结局、停止原因、安全状态报告、两份数据

the problems found; any problem refuses the run
: 发现的问题；有任何问题就拒绝运行

the Ctrl-C flag
: Ctrl-C 标志

`_ctrl_c_requests_stop`
: `_ctrl_c_requests_stop`

the plan: the spec's segments with the approach ramp in front
: 计划：实验的各段，前面加上接近斜坡

`_Run.rehearse`, `_execute`
: `_Run.rehearse`, `_execute`

the sample read
: 读到的那个采样

`_rehearsal_servo`
: `_rehearsal_servo`

the experiment
: 这个实验

where the actuator is now
: 执行器现在的位置

the preflight sample
: 预检时的采样

how fast the ramp may go
: 斜坡最快可以多快

the bench file's `[runner]`
: 台架文件的 `[runner]`

the timed segments, approach first
: 定时的各段，接近段在最前

`_Run.run_preflight`
: `_Run.run_preflight`

True if it waited, so the preflight is repeated
: 等待过则为 True，这时预检要重做一遍

`_Run.stages`
: `_Run.stages`

the timed segments
: 定时的各段

what the rehearsal did
: 预演做了什么

`_Run.result`
: `_Run.result`

the servo model's parameters
: 舵机模型的参数

`rehearsal_params`
: `rehearsal_params`

position and temperature to start from
: 起始的位置和温度

`self.preflight`
: `self.preflight`

a `SimServo` on its own virtual clock
: 一个 `SimServo`，跑在它自己的虚拟时钟上

the rehearsal's `_execute`
: 预演的那次 `_execute`

the rehearsal's simulated servo
: 预演用的仿真舵机

the same timed segments as the real run
: 和真实运行相同的定时各段

what the simulated servo did, and whether the monitor stopped it
: 仿真舵机做了什么，以及监视器是否让它停下

`self.rehearsal_trace`
: `self.rehearsal_trace`

the real target
: 真实目标

a fresh `Monitor` holding the bench limits
: 一个新的 `Monitor`，带着台架限制

the time between passes
: 两轮之间的时间

the adapter's sample rate, or the spec's if slower
: 适配器的采样率；实验要求得更慢时用实验的

times every adapter call against the adapter's declared bound
: 给适配器的每次调用计时，对照适配器声明的上限

samples, commands, missed reads and the stop reason
: 采样、指令、丢失的读取和停止原因

`_Run.stages` freezes it into `self.target_trace`
: `_Run.stages` 把它定格成 `self.target_trace`

a `Sample`: position, velocity, current, voltage, temperature, one timestamp
: 一个 `Sample`：位置、速度、电流、电压、温度，共用一个时间戳

`Monitor.on_sample`, the trace
: `Monitor.on_sample`，以及运行数据

the first `Violation`, or None
: 第一个 `Violation`，或 None

`_execute` stops the run on one
: 有的话，`_execute` 就停止运行

the time now, on the adapter's clock
: 当前时间，按适配器的时钟

`_execute`
: `_execute`

a `Violation`, or None
: 一个 `Violation`，或 None

the setpoint about to be sent
: 即将发出的设定值

the segment's `setpoint_rad`
: 这一段的 `setpoint_rad`

a `Violation` if it is outside the command band, or None
: 超出指令范围时是一个 `Violation`，否则为 None

when the adapter sent it, on its clock
: 适配器发出它的时刻，按它的时钟

recorded in the trace, told to `Monitor.on_command`
: 记进运行数据，并告诉 `Monitor.on_command`

when the adapter sent it
: 适配器发出它的时刻

`SimServo.command`
: `SimServo.command`

what was commanded
: 发出的指令内容

the `CommunicationError` the call raised
: 这次调用抛出的 `CommunicationError`

the adapter
: 适配器

True for a fault poll, False for a read
: 故障查询为 True，读取为 False

True if the monitor stops the run for it
: 监视器因此停止运行时为 True

what the device reports
: 设备报告的内容

`SimServo.read_faults`
: `SimServo.read_faults`

a `Violation` if there is any fault, or None
: 有任何故障时是一个 `Violation`，否则为 None

the time now
: 当前时间

a `Violation` if the run ended without a good reading, or None
: 运行结束时没有过有效读数则是一个 `Violation`，否则为 None

`SafeStateReport`: confirmed or not, attempts, errors
: `SafeStateReport`：是否确认、尝试次数、错误

`_Run.result`, `_tell_watches`
: `_Run.result`, `_tell_watches`

the safe-state report
: 安全状态报告

`_enter_safe_state`
: `_enter_safe_state`

which actuator it was
: 是哪个执行器

`adapter.describe()`
: `adapter.describe()`

why the run stopped early, or None
: 运行提前停止的原因，或 None

the stage that stopped it
: 让它停下的那个阶段

## Characterize: measure, fit and export

`load_bench`, from `_characterize`
: `load_bench`，来自 `_characterize`

`run_characterize`
: `run_characterize`

`_Characterize.run`
: `_Characterize.run`

the same adapter characterize will use
: 特性测定要用的同一个适配器

check's `CommandResult`
: 体检的 `CommandResult`

`self.check`; its verdict decides the fork below
: `self.check`；它的结论决定下面的分叉

the limits and the load
: 限制和负载

what the adapter says it can do
: 适配器自述能做什么

the experiments, each marked as fitted or held out
: 各个实验，每个都标明是用来拟合还是留出

the loop in `_Characterize.run`
: `_Characterize.run` 里的循环

when the previous run ended, on the adapter's clock
: 上一次运行结束的时刻，按适配器的时钟

the previous `RunResult`'s safe-state time
: 上一个 `RunResult` 里进入安全状态的时刻

the planned experiment
: 安排好的那个实验

`plan_experiments`
: `plan_experiments`

`analyse`, `judge_run`, `write_run`, and later the fit
: `analyse`、`judge_run`、`write_run`，以及之后的拟合

the experiment that was run
: 实际运行的那个实验

step or frequency metrics
: 阶跃指标或频率指标

written into the run record
: 写进运行记录

`analyse`
: `analyse`

`StepAnalysis`
: `StepAnalysis`

`FrequencyAnalysis`
: `FrequencyAnalysis`

the fork below
: 下面的分叉

the experiment's name, its role and its analysis
: 实验的名字、它的角色和它的分析结果

the run directory's path
: 运行文件夹的路径

the command's list of files
: 命令的文件清单

the forced verdict
: 被强制的结论

`judge_run`
: `judge_run`

why the record could not be written
: 记录写不下来的原因

`write_run`
: `write_run`

one recorded run
: 一次记下的运行

`self.results`
: `self.results`

a `FitExperiment`
: 一个 `FitExperiment`

`fit_model`, `prediction_error`
: `fit_model`, `prediction_error`

the runs to fit, the held-out one left out
: 要拟合的那些运行，不包括留出的那次

`FitExperiment.from_trace`
: `FitExperiment.from_trace`

the pendulum arm
: 摆臂

the bench file's `[load]`
: 台架文件的 `[load]`

`FitResult`: an estimate for each parameter and combination
: `FitResult`：每个参数和每个组合量的估计值

`export_json`, `mujoco_fragment`
: `export_json`, `mujoco_fragment`

the parameter set to judge
: 要评判的那组参数

the defaults, then `fit.prediction_params`
: 先是默认值，然后是 `fit.prediction_params`

the held-out run
: 留出的那次运行

RMS and largest error, in radians
: 均方根误差和最大误差，单位是弧度

compared with the noise floor
: 和噪声下限比较

the parameter set
: 那组参数

`prediction_error`
: `prediction_error`

the run to replay
: 要回放的那次运行

the model's position at each reading time, and the difference
: 模型在每个读数时刻的位置，以及差值

when each command was sent
: 每条指令发出的时刻

the times to answer for
: 要给出结果的那些时刻

the recorded readings
: 记下的读数

the model's position, velocity and torque at those times
: 模型在这些时刻的位置、速度和力矩

`replay`
: `replay`

every recorded run
: 每一次记下的运行

a `NoiseFloor`
: 一个 `NoiseFloor`

the fit-quality ratio
: 拟合质量的比值

every parameter with its value, uncertainty and status
: 每个参数及其数值、不确定度和状态

`fit_model`
: `fit_model`

good or poor, and why
: 良好还是不佳，以及原因

`_Characterize.fit_and_report`
: `_Characterize.fit_and_report`

voltage, temperature, load, device, bench fingerprint
: 电压、温度、负载、设备、台架文件指纹

`_Characterize.conditions`
: `_Characterize.conditions`

the file's contents
: 文件的内容

`_Characterize.write_record`
: `_Characterize.write_record`

the fitted parameters
: 拟合出的参数

the XML text
: XML 文本

good or poor
: 良好还是不佳

the held-out error against the noise floor
: 留出实验的误差对比噪声下限

the parameters that are not identifiable
: 无法辨识的那些参数

verdict, exit code, whether a human is needed, summary, what to do next
: 结论、退出码、是否需要人、摘要、接下来做什么

the command's result so far
: 到目前为止的命令结果

`_Characterize.fit_and_report`, or a step that stopped the sequence
: `_Characterize.fit_and_report`，或某个让流程停下的步骤

the contents for `servo_model.json`
: `servo_model.json` 的内容

`export_json`
: `export_json`

the same result, with the safe-state report applied
: 同一个结果，已加上安全状态报告

the final result
: 最终的结果

`_Characterize.finish`
: `_Characterize.finish`

the data for the held-out plot
: 留出实验对比图的数据

`_Characterize.overlay_data`
: `_Characterize.overlay_data`

where to save the PNG
: PNG 存到哪里

the command's run directory
: 命令的运行文件夹

the held-out run's readings
: 留出那次运行的读数

the fitted model's replay
: 拟合模型的回放结果
