# 中文对照：parts.md

<!-- 由 bearings 读取：每条先是 parts.md 里的原文（一字不差），下一行以冒号开头是中文。
     原文改了字，这里对应那一行也要跟着改；`bearings.py words` 会列出缺的和对不上的。 -->

`servobench` is a command-line tool for running small, bounded experiments on one actuator and getting measured facts back. A person or their coding agent types a command; the tool reads the owner's limits from `bench.toml`, rehearses the experiment on a simulated servo, runs it while a safety monitor watches every reading, switches the torque off however the run ended, and then prints a verdict and saves the run as plain files.
: `servobench` 是一个命令行工具，用来在一个执行器上做小而有界的实验，并拿回测量到的事实。人或他的编程助手输入一条命令；工具从 `bench.toml` 读取主人设下的限制，先在仿真舵机上预演这个实验，运行时由安全监视器盯着每一个读数，无论运行怎样结束都会断开力矩，最后打印结论，并把这次运行存成普通文件。

Today every experiment runs on the simulated servo only. The code that talks to a real servo is not written yet, so nothing on this map moves hardware.
: 目前所有实验都只在仿真舵机上运行。和真实舵机通信的代码还没有写，所以这张地图上没有任何东西会让硬件动起来。

Engineer or agent
: 工程师或助手

a person, or their coding agent
: 一个人，或他的编程助手

bench.toml
: bench.toml

the owner's limits file
: 主人写的限制文件

Run folder
: 运行文件夹

plain files under runs/
: runs/ 下的普通文件

Commands
: 命令

Safe experiments
: 安全实验

Actuators
: 执行器

Results
: 结果

Project support
: 项目支撑

tests, docs and packaging, for the people building the tool
: 测试、文档和打包，给做这个工具的人用

command, verdict
: 命令、结论

target, limits
: 目标、限制

experiment, result
: 实验、结果

setpoints, readings
: 设定值、读数

direct readings
: 直接读数

runs, metrics
: 运行、指标

model replay
: 模型回放

record files
: 记录文件

## A health check on one servo

A health check on one servo
: 给一个舵机做体检

types the command
: 输入命令

Command line
: 命令行

Health check
: 体检

Adapter contract
: 适配器约定

Experiment plans
: 实验计划

Experiment runner
: 实验运行器

Simulated servo
: 仿真舵机

Safety monitor
: 安全监视器

Response analysis
: 响应分析

Run records
: 运行记录

check command
: check 命令

loaded bench
: 读好的台架文件

read-only items
: 只读检查项

small step
: 小阶跃

timed plan
: 定时计划

rehearsal
: 预演

every sample
: 每个采样

recorded run
: 记下的运行

run record
: 一条运行记录

verdict
: 结论

## Characterizing a servo into a simulator model

Characterizing a servo into a simulator model
: 把一个舵机测成仿真模型

Characterize
: 特性测定

Model fit
: 模型拟合

Servo model
: 舵机模型

Simulator export
: 仿真导出

characterize command
: characterize 命令

gate first
: 先过关

steps, sines
: 阶跃、正弦

timed plans
: 多个定时计划

recorded runs
: 记下的多次运行

candidate replay
: 候选回放

estimates
: 估计值

model files
: 模型文件

## Commands

What a person or their coding agent types, and the verdict they get back.
: 人或编程助手输入的命令，以及拿回来的结论。

These are the things you can ask the tool to do: check that an actuator is alive and set up correctly, measure how it behaves, and look at past runs. Each command is a thin layer that calls on the other groups and always answers the same way: a verdict first, one sentence, an exit code, and whether a human must take over.
: 这些是你能让工具做的事：检查一个执行器是否活着、是否装对了，测量它的行为，查看以前的运行。每条命令都只是薄薄一层，去调用其他几个区，并且总是用同一种方式回答：先给结论，再给一句话、一个退出码，以及是否必须由人接手。

Past runs
: 历史运行

bench file
: 台架文件

bench, result
: 台架、结果

run name, result
: 运行名、结果

test move, result
: 试动、结果

experiments, results
: 各个实验、结果

run, record
: 运行、记录

runs, fit
: 运行、拟合

saved records
: 存下的记录

## Command line

Reads the typed command, runs it, and prints the verdict as text or JSON.
: 读取输入的命令，执行它，把结论以文字或 JSON 打印出来。

This is the front door. It works out which command was typed, loads the bench file for commands that need it, runs the command, and writes the result with the verdict first. It also decides what each kind of ending means: which verdict and which exit code a refused, aborted or failed run gets.
: 这是正门。它弄清输入的是哪条命令，为需要的命令读入台架文件，执行命令，然后把结果写出来，结论放在最前面。它还规定每种结局意味着什么：被拒绝、被中止或失败的运行各得到哪个结论、哪个退出码。

Nothing is allowed to escape as a bare crash: a typing mistake, a bad bench file, Ctrl-C and a bug each come back as a result with a verdict. If any run ended without the actuator confirming it was made safe, that warning is written before anything else. Any new command has to return its result through here and never print on its own.
: 不允许任何情况以光秃秃的崩溃收场：打错命令、台架文件有问题、Ctrl-C、程序缺陷，都会变成一个带结论的结果返回。只要有一次运行结束时执行器没有确认已进入安全状态，这条警告就会最先写出来。任何新命令都必须从这里交回结果，不能自己打印。

Parse arguments
: 解析参数

which command, which options
: 哪条命令，哪些选项

Run command
: 执行命令

inside a watch for unconfirmed safe states
: 全程留意有没有未确认的安全状态

Write result
: 写出结果

verdict first, as text or JSON
: 结论在前，文字或 JSON

Bench file
: 台架文件

typed command
: 输入的命令

chosen command
: 选中的命令

usage error
: 用法错误

bench.toml, limits
: bench.toml、限制

command result
: 命令结果

verdict, exit code
: 结论、退出码

## Health check

The `check` command: is this actuator alive and set up correctly?
: `check` 命令：这个执行器活着吗，装对了吗？

It first only reads from the actuator: does it answer, is it the device the bench file names, does it report a fault, are voltage and temperature in range, is the connection fast and reliable enough, which protections does it claim, and is it resting where zero should be. Only if those pass does it make one small move up and back, to see that the actuator turns the commanded way and does not heat up too much.
: 它先只从执行器读数据：有没有应答，是不是台架文件里写的那个设备，有没有报故障，电压和温度在不在范围内，连接够不够快、够不够可靠，它声称有哪些保护，以及它是不是停在零位应该在的地方。只有这些都通过，它才做一次小小的来回移动，看执行器是否朝命令的方向转、会不会发热太多。

With `--no-motion` it never moves anything, which is the form meant for an assembled robot. Every item ends as pass, fail, warning, skipped or unknown; a skipped or unknown item is never counted as a pass. Its thresholds were chosen on the simulated servo and are due to be re-measured on real hardware.
: 加上 `--no-motion` 时它什么都不会动，这是给已经装好的机器人用的形式。每个检查项的结果是通过、失败、警告、跳过或未知；跳过或未知的项绝不算作通过。它的阈值是在仿真舵机上选的，还要到真实硬件上重新测定。

Read-only items
: 只读检查项

answers, identity, faults, voltage, temperature, bus, zero
: 应答、身份、故障、电压、温度、总线、零位

Test move
: 试动

one small step up and back
: 一次小阶跃，去了再回

Verdict
: 结论

every item's status, and the overall result
: 每一项的状态，和总的结果

gate request
: 把关请求

describe, readings
: 描述、读数

gates passed
: 把关通过

motion skipped
: 不做移动

small step, result
: 小阶跃、结果

recorded run, metrics
: 运行数据、指标

direction, temperature rise
: 方向、温升

record, earlier runs
: 记录、早先的运行

result
: 结果

## Characterize

The `characterize` command: measures how the actuator behaves and exports a model of it.
: `characterize` 命令：测出执行器的行为，并导出它的模型。

It refuses unless the bench file describes the pendulum arm, because the arm's known weight is what lets it tell the servo's inertia from its gains. It then runs the whole health check, motion included, and goes on only if that passes. After that it runs a set of experiments sized from the bench limits: steps of several sizes, a stepped sine, one slow large swing, and a second set of steps kept aside. Each one goes through the experiment runner, with a rest between them.
: 台架文件里没有描述摆臂，它就拒绝执行，因为正是靠摆臂已知的重量，才能把舵机的惯量和增益分开。然后它完整地做一遍体检（包括移动），通过了才继续。接着它按台架限制的大小安排一组实验：几种幅度的阶跃、一次步进正弦、一次缓慢的大幅摆动，另外还有一组留作检验的阶跃。每个实验都经过实验运行器，中间留出休息时间。

The model is fitted to the first group and then tested on the steps that were kept aside; the result says how well each number is known and whether the fitted model is usable at all. If any run does not complete, the sequence stops there and nothing is fitted. On real hardware it is expected to take about five minutes, mostly rests.
: 模型用前一组数据拟合，再用留出的那组阶跃来检验；结果会说明每个数值知道得有多准，以及拟合出的模型到底能不能用。任何一次运行没有完成，整个流程就停在那里，不做拟合。在真实硬件上预计要五分钟左右，大部分是休息时间。

Gate
: 把关

needs the arm described, then a full check
: 要先有摆臂的描述，再做完整体检

Run experiments
: 逐个做实验

sized from the limits, one at a time, with rests
: 按限制定大小，一次一个，中间休息

Fit and validate
: 拟合并检验

fit on most runs, test on the held-out one
: 用多数运行拟合，用留出的那次检验

full check, verdict
: 完整体检、结论

check passed
: 体检通过

each run
: 每次运行

runs, estimates
: 运行、估计值

estimates, model files
: 估计值、模型文件

summary, files
: 汇总、文件

## Past runs

The `runs` command: lists recent run records, or shows one.
: `runs` 命令：列出最近的运行记录，或显示其中一条。

It only reads files that earlier commands saved. Its own verdict says whether reading worked; the verdict of the run being shown is that run's own and is reported inside the output.
: 它只读取以前的命令存下的文件。它自己的结论说的是读取是否成功；被显示的那次运行的结论是那次运行自己的，写在输出里面。

List recent
: 列出最近的

the newest runs, one line each
: 最新的几次运行，每次一行

Show one
: 显示一条

by path, by name, or the latest
: 按路径、按名字，或最新的一条

how many, list
: 条数、列表

run name, record
: 运行名、记录

run summaries
: 运行摘要

one record
: 一条记录

## Safe experiments

Everything that keeps an experiment inside the owner's limits.
: 让每个实验都不越过主人所设限制的一切。

This group is the safety layer. It reads the limits the owner wrote, describes experiments as timed plans, and runs each plan through the same fixed stages, with a monitor watching every reading. There is one way to run an experiment and no option that skips a stage.
: 这个区是安全层。它读取主人写下的限制，把实验描述成定时计划，并让每个计划走完同样固定的几个阶段，同时有监视器盯着每一个读数。运行实验只有这一条路，没有任何选项可以跳过某个阶段。

file path, limits
: 文件路径、限制

chosen shape
: 选定的形状

limits, settings
: 限制、设置

## Bench file

Reads the owner's `bench.toml` (target, limits, load) and the unit rules every number passes.
: 读取主人的 `bench.toml`（目标、限制、负载），以及每个数值都要经过的单位规则。

`bench.toml` is a small file the owner writes: which actuator and how to reach it, the limits (travel, speed, current, temperature, voltage, longest experiment, cool-down), and the load attached. Every value in it is written with a unit, such as "45 deg", and is converted once, here, to standard units; a number without a unit is refused. The whole file is checked and every problem is reported together; an unknown key is an error, so a typo in a limit cannot be silently ignored.
: `bench.toml` 是主人写的一个小文件：用哪个执行器、怎样连上它，各项限制（行程、速度、电流、温度、电压、最长实验时间、冷却时间），以及装在上面的负载。里面每个值都带单位，比如 "45 deg"，并且只在这里换算一次，变成标准单位；不带单位的数字会被拒绝。整个文件会被检查，所有问题一起报出来；不认识的键也算错误，所以限制里的拼写错误不会被悄悄忽略。

The tool reads this file and never writes it. The one exception is `servobench init`, which creates a commented template and refuses to overwrite an existing file. Each load also takes a fingerprint of the file's exact bytes, stored in every run record, so a change to the limits is visible afterwards. It does not prevent a change: real enforcement is the servo's own protections and the power supply's current limit.
: 工具只读这个文件，从不写它。唯一的例外是 `servobench init`，它会生成一份带注释的模板，并且拒绝覆盖已有的文件。每次读入时还会对文件的原始字节取一个指纹，存进每条运行记录，所以限制被改过事后能看出来。它并不能阻止改动：真正起强制作用的是舵机自身的保护和电源的限流。

these are the limits that every experiment is held to; the file belongs to the owner.
: 这些是每个实验都必须遵守的限制；这个文件属于主人。

Do not edit `bench.toml` or loosen a limit check to make something pass. (CLAUDE.md, 2026-11-16)
: 不要为了让某件事通过而修改 `bench.toml` 或放松限制检查。（CLAUDE.md，2026-11-16）

Every quantity carries an explicit unit. A bare number is an error. (CLAUDE.md, 2026-11-16)
: 每个量都带明确的单位。光秃秃的数字就是错误。（CLAUDE.md，2026-11-16）

Read and fingerprint
: 读入并取指纹

the exact bytes, and their SHA-256
: 原始字节，及其 SHA-256

Convert units
: 换算单位

"45 deg" becomes a standard-unit number
: "45 deg" 变成标准单位的数

Validate
: 校验

every problem reported, unknown keys refused
: 所有问题都报出，不认识的键被拒绝

file path
: 文件路径

file bytes
: 文件字节

values with units
: 带单位的值

standard-unit numbers
: 标准单位的数

## Experiment plans

Describes an experiment as timed stretches of commands: hold, step, or stepped sine.
: 把一个实验描述成按时间排好的几段指令：保持、阶跃，或步进正弦。

An experiment here is only a description of what to command and when; it sends nothing and knows nothing about the actuator. It is checked twice before anything runs: when it is made, that its numbers make sense, and against the bench limits, that every commanded position stays inside the allowed travel, it fits in the longest allowed time, and a sine is not commanded faster than the speed limit.
: 这里的实验只是一份描述：什么时候发什么指令；它不发送任何东西，也不了解执行器。在真正运行之前它会被检查两次：创建时，检查数值是否合理；对照台架限制时，检查每个指令位置都在允许的行程内、总时长不超过允许的最长时间、正弦指令的速度不超过速度上限。

The runner then puts a slow approach ramp in front, from wherever the actuator is to the experiment's first position. What a plan cannot know is how the actuator will respond, for example a step that overshoots; that is what the rehearsal and the safety monitor are for.
: 运行器随后会在前面加一段缓慢的接近斜坡，从执行器当前所在的位置走到实验的第一个位置。计划无法知道的是执行器会怎样响应，比如某个阶跃会超调；预演和安全监视器就是为此而设的。

Choose shape
: 选形状

hold, step, or stepped sine
: 保持、阶跃，或步进正弦

Well-formed check
: 格式检查

finite numbers, positive durations
: 数值有限，时长为正

Limits check
: 对照限制检查

travel, total time, commanded speed
: 行程、总时长、指令速度

Add approach
: 加接近段

a slow ramp to the first position
: 缓慢走到第一个位置的斜坡

sizes, timings
: 幅度、时间

the numbers
: 这些数值

experiment
: 实验

allowed experiment
: 允许的实验

## Experiment runner

The one way to run an experiment: validate, preflight, cool-down, rehearsal, run, safe state.
: 运行实验的唯一途径：校验、预检、冷却、预演、运行、安全状态。

Every experiment goes through the same stages in the same order. First it checks the plan against the limits and against what the actuator says it can do, without contacting it. Then it reads the faults and one sample, and waits or refuses if the actuator is too warm or ran too recently. Then it rehearses the same plan on a simulated servo; if the rehearsal leaves the limits, the real actuator is never enabled. Only then does it enable the actuator and run the plan, sending a command, taking a reading and asking the safety monitor on every pass.
: 每个实验都按同样的顺序走过同样的阶段。首先，在不接触执行器的情况下，把计划同限制、同执行器自述的能力做对照。然后读取故障和一个采样；如果执行器太热或刚运行过不久，就等待或拒绝。接着在仿真舵机上预演同一个计划；预演一旦越出限制，真实执行器就根本不会被使能。只有到这时，它才使能执行器并运行计划：每一轮发一条指令、取一次读数、问一次安全监视器。

Whatever ends the run, a normal finish, a limit, Ctrl-C, an error or a bug, the next thing it does is ask the actuator to enter its safe state (torque off), retrying if needed, and the result says whether the actuator confirmed it. The rehearsal checks the experiment as designed against a model; it is not a prediction of the real actuator. Because a run always ends with torque off, this must never be used on an assembled robot that needs to keep holding itself up.
: 无论运行因为什么结束——正常完成、触到限制、Ctrl-C、出错或程序缺陷——它接下来做的第一件事都是让执行器进入安全状态（断开力矩），必要时重试，结果里会写明执行器是否确认。预演是拿设计好的实验去对照一个模型做检查，并不是对真实执行器的预测。因为运行总是以断开力矩结束，所以绝不能用在需要保持自身姿态的已装配机器人上。

this is the code that enables an actuator and must always switch it off again.
: 这段代码会使能执行器，并且必须保证每次都把它关掉。

plan against limits and the actuator's description
: 计划对照限制和执行器的描述

Preflight
: 预检

faults, one reading, cool-down
: 故障、一次读数、冷却

Rehearsal
: 预演

the same plan on a simulated servo
: 同一个计划，在仿真舵机上

Real run
: 真实运行

command, read, check, every pass
: 每一轮：发指令、读数、检查

Safe state
: 安全状态

torque off, whatever happened
: 不管发生什么，断开力矩

plan allowed
: 计划可行

faults, one reading
: 故障、一次读数

cool, no faults
: 够凉、无故障

same plan
: 同一计划

stayed inside
: 没有越限

any ending
: 任何结局

torque off
: 断开力矩

## Safety monitor

Checks every command, reading and missed reply of a run against the bench limits.
: 把一次运行中的每条指令、每个读数、每次丢失的应答都对照台架限制检查。

The monitor never talks to the device. The runner tells it about each command, each reading, each read that got no answer, each fault poll and the time, and the monitor answers with the first thing that breaks a limit, or nothing. It checks position, speed, current, temperature and voltage against the limits. It also checks that the actuator is following its commands, which catches a servo turning the wrong way, running away, stuck, or a sensor frozen at a harmless value.
: 监视器从不和设备通信。运行器把每条指令、每个读数、每次没有应答的读取、每次故障查询以及时间都告诉它，监视器则回答第一件越限的事，或者什么都不回答。它对照限制检查位置、速度、电流、温度和电压。它还检查执行器是否在跟随指令，这能发现舵机转反了、失控、卡住，或者传感器冻结在一个看似无害的数值上。

It also limits how long the tool may go blind: too many missed replies in a row, or too long without a good reading, stops the run, and so does a run that stays enabled past its planned time. The rehearsal runs under a monitor of its own with the same limits. Loosening a check here to make something pass is against the project's rules.
: 它还限制工具可以“失明”多久：连续丢失太多应答，或太久没有一个有效读数，都会让运行停止；使能时间超过计划的运行也一样。预演在它自己的一个监视器下进行，用的是同样的限制。为了让某件事通过而放松这里的检查，违反项目的规矩。

this decides when a moving actuator must be stopped.
: 由它决定什么时候必须让运动中的执行器停下。

Limit checks
: 限值检查

position, speed, current, temperature, voltage
: 位置、速度、电流、温度、电压

Following checks
: 跟随检查

not following, or a frozen reading
: 没有跟随，或读数冻结

Time checks
: 时间检查

missed replies, too long blind, enabled too long
: 丢失应答、失明太久、使能太久

Fault check
: 故障检查

the device's own fault report
: 设备自己报告的故障

First violation
: 第一次越限

the first limit broken, or nothing
: 第一个被打破的限制，或者没有

each reading
: 每个读数

commands, readings
: 指令、读数

missed replies, time
: 丢失应答、时间

fault polls
: 故障查询

limit broken
: 越过限制

not following
: 没有跟随

blind too long
: 失明太久

device fault
: 设备故障

stop reason
: 停止原因

## Actuators

The one interface to an actuator, and the simulated servo behind it.
: 通向执行器的唯一接口，以及它背后的仿真舵机。

Everything else in the tool talks to an actuator through one narrow interface, so that a new actuator needs only one new piece of code. Today a single actuator sits behind it: a simulated servo, built on a mathematical model of a position servo swinging a pendulum arm. The same model is reused when a real servo's behaviour is fitted.
: 工具里其余的一切都通过一个很窄的接口和执行器打交道，所以接入一种新执行器只需要新写一段代码。目前接口后面只有一种执行器：仿真舵机，它建立在“位置舵机带动摆臂”的数学模型之上。拟合真实舵机的行为时，用的也是同一个模型。

Real servo adapter
: 真实舵机适配器

not built yet: plan steps 9 to 11
: 还没做：计划第 9 到 11 步

same calls
: 同样的调用

commands, motion
: 指令、运动

## Adapter contract

The single interface between the tool and any actuator, with its clock.
: 工具和任何执行器之间的唯一接口，连同它的时钟。

An adapter is the only code that knows a particular actuator. It must describe itself (what it can read and with which units, how fast, what it can be commanded to do, and which protections the hardware really has), read signals, accept a position command, enable, enter the safe state, and report faults. There is deliberately no call to clear a fault: that is a human's decision.
: 适配器是唯一了解某一种具体执行器的代码。它必须描述自己（能读什么、用什么单位、多快、能接受什么指令、硬件真正具备哪些保护），读取信号，接受位置指令，使能，进入安全状态，并报告故障。这里故意没有“清除故障”的调用：那是人来决定的事。

Every call must finish within a time the adapter states, whether or not the device answers, because the runner can only act between calls. Each adapter also owns a clock, and all waiting goes through it, so the simulated servo can run on virtual time and tests finish quickly. The bench file names the adapter to use, and a small registry turns that name into the code; adding an adapter is one line there.
: 每次调用都必须在适配器声明的时间内结束，不管设备有没有应答，因为运行器只能在两次调用之间采取行动。每个适配器还有自己的时钟，所有等待都通过它进行，所以仿真舵机可以跑在虚拟时间上，测试很快就能跑完。台架文件写明用哪个适配器，一张小小的登记表把这个名字对应到代码；新增一个适配器，只需在那里加一行。

The tool has no way to clear a device fault. (docs/development-plan.md, 2026-11-16)
: 工具没有任何办法清除设备故障。（docs/development-plan.md，2026-11-16）

No persistent write to a device without the owner confirming that specific write. (CLAUDE.md, 2026-11-16)
: 没有主人对那一次写入的确认，就不向设备做任何持久写入。（CLAUDE.md，2026-11-16）

Adapter registry
: 适配器登记表

the name in the bench file picks the code
: 台架文件里的名字决定用哪段代码

Description
: 自述

what it can read and do, checked for gaps
: 能读什么、能做什么，并查过有无缺漏

Device calls
: 设备调用

read, command, enable, safe state, faults
: 读取、指令、使能、安全状态、故障

Clock
: 时钟

real or virtual time; all waiting goes through it
: 真实或虚拟时间；所有等待都经过它

adapter name
: 适配器名

opens it
: 打开它

what it can do
: 它能做什么

signals, rates
: 信号、速率

now, wait
: 当前时间、等待

virtual time
: 虚拟时间

## Simulated servo

A pretend servo that behaves like a real target, faults included.
: 一个假的舵机，表现得像真实目标，连故障也一样。

It stands in for hardware. Each call takes a realistic amount of time on a pretend serial bus, with jitter and a reply timeout; the position reading comes in steps and with noise; the supply voltage sags with current; the temperature rises with effort. Faults can be switched on from the bench file, such as dropped replies, a stuck position reading or overheating, to prove that the safety layer catches them.
: 它代替硬件。每次调用都在一条假想的串行总线上花掉符合实际的时间，带抖动，也有应答超时；位置读数是一格一格的，还带噪声；电源电压随电流下跌；温度随出力升高。可以从台架文件里打开各种故障，比如丢应答、位置读数卡死、过热，用来证明安全层能抓住它们。

It runs on virtual time, so it never sleeps and the same seed gives the same result. The runner also builds a fresh one for every rehearsal. Its constants are invented placeholders chosen to behave plausibly; none is a fact about the HD1910 or any real servo.
: 它跑在虚拟时间上，所以从不真正等待，同一个随机种子给出同样的结果。运行器每次预演也会新建一个。它的常数是为了表现得合理而编出来的占位值，没有一个是 HD1910 或任何真实舵机的事实。

Bus transaction
: 总线事务

one call takes one bus period, plus jitter
: 一次调用占一个总线周期，外加抖动

Injected faults
: 注入的故障

switched on in the bench file, for tests
: 在台架文件里打开，供测试用

Catch up
: 追上时间

the model is stepped to the present moment
: 把模型一步步推进到当前时刻

Take reading
: 取读数

steps, noise, voltage sag, temperature
: 量化、噪声、电压下跌、温度

call, reply
: 调用、应答

rehearsal calls
: 预演的调用

missed replies
: 丢失应答

time passed
: 过去的时间

true state
: 真实状态

stuck, too hot
: 卡死、过热

noisy sample
: 带噪声的采样

## Servo model

The equations of a position servo swinging a pendulum arm.
: 位置舵机带动摆臂的运动方程。

This is the mathematics shared by the simulated servo and the model fit. The servo pushes toward its commanded position with a gain, is damped, has a torque limit, friction, and a short delay before it acts on a command; gravity pulls on the arm. The servo's own numbers are the unknowns that the fit estimates; the arm's mass and length come from the bench file.
: 这是仿真舵机和模型拟合共用的数学。舵机以一个增益朝指令位置推，带阻尼，有力矩上限和摩擦，收到指令后要过一小段延迟才起作用；重力拉着摆臂。舵机自身的这些数值是拟合要估计的未知量；摆臂的质量和长度来自台架文件。

It assumes the arm hangs straight down at position zero and is a point mass, so a bench rig mounted differently needs a decision before real use. It advances in fixed small time steps and can answer for any time in between.
: 它假定摆臂在零位时竖直下垂，并且是一个质点，所以台架如果装法不同，在真正使用之前需要先做决定。它以固定的小时间步推进，并能给出其间任意时刻的状态。

Replay
: 回放

recorded commands in, predicted positions out
: 输入记录的指令，输出预测的位置

Integrator
: 积分器

fixed small steps, answers for any time
: 固定小步长，任意时刻都能回答

Setpoint schedule
: 设定值时间表

each command takes effect after the delay
: 每条指令在延迟之后生效

Equations of motion
: 运动方程

gain, damping, torque limit, friction, gravity
: 增益、阻尼、力矩上限、摩擦、重力

Parameters
: 参数

the servo's unknowns, and the arm
: 舵机的未知量，以及摆臂

commands, prediction
: 指令、预测

delayed setpoint
: 延迟后的设定值

one step
: 一步

gains, friction, arm
: 增益、摩擦、摆臂

## Results

Turns a recorded run into numbers, a fitted model and files on disk.
: 把记录下来的运行变成数值、拟合出的模型和磁盘上的文件。

Once a run is recorded, this group measures it and keeps it. It measures step and frequency response, fits the servo model to several runs, writes the fitted model in a form a simulator can load, and saves every run as a folder of plain files with a plot. None of it needs an actuator: it works from recorded timestamps and values, live or loaded back from disk.
: 一次运行被记录下来之后，由这个区来测量它、保存它。它测量阶跃响应和频率响应，用几次运行拟合舵机模型，把拟合出的模型写成仿真器能加载的形式，并把每次运行存成一个文件夹，里面是普通文件和一张图。这一切都不需要执行器：它只用记录下来的时间戳和数值工作，不管是刚测的还是从磁盘读回的。

results, saved records
: 结果、存下的记录

## Response analysis

Measures step response and frequency response from a recorded run.
: 从记录下来的运行中测出阶跃响应和频率响应。

For a step it measures delay, rise time, overshoot, settling time and where the actuator came to rest; for a stepped sine, the gain and phase at each frequency. Everything is taken from the timestamps the run recorded, and nothing assumes readings arrive evenly spaced, because on a real serial bus they do not.
: 对阶跃，它测量延迟、上升时间、超调、调节时间以及执行器最后停在哪里；对步进正弦，测量每个频率下的增益和相位。一切都取自运行时记录的时间戳，不假定读数是等间隔到达的，因为在真实的串行总线上并不是。

A value that could not be measured is reported as missing, with the reason, never as a guess. Every result also says how good its inputs were: the timing actually achieved, the resolution of the reading, and an uncertainty for each number.
: 测不出来的值会报告为缺失，并附上原因，绝不用猜测代替。每个结果还会说明它的输入质量如何：实际达到的采样时序、读数的分辨率，以及每个数值的不确定度。

Pick analysis
: 选分析方法

step response for a step, frequency for a sine
: 阶跃用阶跃响应，正弦用频率响应

Trace quality
: 数据质量

timing achieved, reading resolution
: 实际时序、读数分辨率

Step response
: 阶跃响应

delay, rise, overshoot, settling
: 延迟、上升、超调、调节

Frequency response
: 频率响应

gain and phase at each frequency
: 每个频率的增益和相位

Plain metrics
: 整理成指标

numbers with units, missing ones with reasons
: 带单位的数值，缺失的附原因

step runs
: 阶跃运行

sine runs
: 正弦运行

timing, resolution
: 时序、分辨率

step numbers
: 阶跃指标

gain, phase
: 增益、相位

metrics
: 指标

## Model fit

Estimates the servo model's unknowns from recorded runs, and says how well each is known.
: 从记录的运行中估计舵机模型的未知量，并说明每个量知道得有多准。

It searches for the servo numbers that make the model reproduce what was recorded: for each candidate it replays the commands a run actually sent, at the times they were sent, through the servo model, and compares with every reading. It starts the search from several places so one bad start does not decide the answer.
: 它寻找能让模型重现记录数据的那组舵机数值：对每一组候选值，它把某次运行实际发出的指令，按实际发出的时刻，在舵机模型里回放一遍，再和每个读数比较。搜索从几个不同的起点出发，免得一个不好的起点决定了答案。

It then works out how firmly the data pins each number down. Some cannot be separated by gentle experiments: for example two kinds of damping only show up as their sum unless the motor is pushed to its torque limit. Such a number is reported as not identifiable, with what is known about it, instead of a made-up value. A separate check replays an experiment that was kept out of the fit and compares the error with the noise in the readings.
: 然后它算出数据把每个数值确定到了什么程度。有些量靠温和的实验分不开：比如两种阻尼只以它们的和出现，除非把电机推到力矩上限。这样的量会报告为“不可辨识”，并附上已知的信息，而不是给一个编出来的值。另有一项检查：把没有参与拟合的那个实验回放一遍，把误差和读数里的噪声相比。

Prepare runs
: 整理运行数据

what was commanded, when, and what was read
: 发了什么指令、何时发的、读到了什么

Least squares
: 最小二乘

several starting points, best match kept
: 多个起点，留下最吻合的

Uncertainty
: 不确定度

how firmly the data fixes each number
: 数据把每个数值定得多牢

Identifiability
: 可辨识性

known, or not determined, with the reason
: 已确定，或无法确定并说明原因

Held-out error
: 留出实验的误差

a run the fit never saw, against the noise
: 拟合没见过的一次运行，对比噪声

best fit
: 最佳拟合

spread per number
: 每个数值的离散度

unseen run
: 没见过的运行

replay
: 回放

error against noise
: 误差对比噪声

## Simulator export

Writes the fitted servo numbers in a form a simulator can load.
: 把拟合出的舵机数值写成仿真器能加载的形式。

It produces two things for `characterize`. One is a parameter file listing each fitted number with its unit, whether it is known, its uncertainty, and the conditions of the measurement (voltage, temperature, load, device, tool version, bench fingerprint). The other is a MuJoCo fragment that can be included in a robot model.
: 它为 `characterize` 产出两样东西。一是参数文件，列出每个拟合出的数值及其单位、是否已确定、不确定度，以及测量时的条件（电压、温度、负载、设备、工具版本、台架文件指纹）。二是一段 MuJoCo 片段，可以包含进机器人模型里。

A number the data could not determine is left empty, never filled in, and the MuJoCo fragment is written only when the fit was judged good. Nothing in the test suite runs MuJoCo itself; the fragment was checked by hand once.
: 数据无法确定的数值会留空，绝不填补；MuJoCo 片段只有在拟合被判定为良好时才写出。测试里没有任何一项真正运行 MuJoCo；这段片段只人工核对过一次。

Parameter file
: 参数文件

servo_model.json: values, uncertainties, conditions
: servo_model.json：数值、不确定度、条件

MuJoCo fragment
: MuJoCo 片段

joint and actuator settings, as XML
: 关节和执行器的设置，XML 格式

MuJoCo mapping
: MuJoCo 对应表

which fitted number becomes which setting
: 哪个拟合值变成哪项设置

estimates, file contents
: 估计值、文件内容

estimates, XML text
: 估计值、XML 文本

setting names
: 设置项名称

## Run records

Saves each run as a folder of plain files with a plot, and reads them back.
: 把每次运行存成一个文件夹，里面是普通文件和一张图，并能读回来。

Every run becomes a folder under `runs/`, named by time, command and target. It holds a description of the run (limits in force, bench fingerprint, what stopped it, the verdict), the readings and commands as tables, the rehearsal, and a picture of commanded and measured position with temperature. If drawing the picture fails, the rest is still saved.
: 每次运行都成为 `runs/` 下的一个文件夹，以时间、命令和目标命名。里面有这次运行的描述（生效的限制、台架文件指纹、是什么让它停下、结论），表格形式的读数和指令，预演的数据，以及一张画着指令位置、实测位置和温度的图。即使画图失败，其余部分照样保存。

The records are also read back: to list and show past runs, to analyse a run again, and to find when this actuator last ran so the cool-down holds across separate invocations. A folder that looks like a record but cannot be read is treated as a recent run, to be safe. The `runs/` folder belongs to the user's experiments and is not kept in the repository.
: 这些记录也会被读回来：用来列出和显示历史运行，重新分析某次运行，以及查出这个执行器上次运行的时间，让冷却时间在多次分开的调用之间也能保持。看起来像记录却读不出来的文件夹，为保险起见会被当作一次刚刚发生的运行。`runs/` 文件夹属于用户自己的实验，不放进代码仓库。

Write record
: 写记录

run.json and the tables
: run.json 和各张表

Draw plot
: 画图

position and temperature against time
: 位置和温度随时间的变化

Load and list
: 读回和列出

one record, or the newest few
: 一条记录，或最新的几条

Cool-down lookup
: 冷却查询

when this actuator last ran
: 这个执行器上次运行的时间

result, verdict
: 结果、结论

run.json, tables
: run.json、表格

plot.png
: plot.png

saved files
: 存下的文件

records, summaries
: 记录、摘要

earlier records
: 早先的记录

last run's end
: 上次运行的结束时间

## Project support

Tests, documents and packaging, for the people building the tool.
: 测试、文档和打包，给做这个工具的人用。

Nothing here runs when a command is used. It is what keeps the tool trustworthy and explains it: the tests that prove each safety rule, the documents that say what is being built and what is known about the hardware, and the files that make it installable.
: 使用命令时，这里的东西都不会运行。它们让工具值得信任，并把它讲清楚：证明每条安全规则的测试，说明在做什么、对硬件了解多少的文档，以及让它可以安装的文件。

Tests
: 测试

prove each rule; run against the simulated servo
: 证明每条规则；在仿真舵机上运行

Documentation
: 文档

the plan, the hardware facts and the rules, for owner and agents
: 计划、硬件事实和规矩，给主人和助手看

Project setup
: 项目配置

name, licence and dependencies, for installing the tool
: 名称、许可证和依赖，用于安装工具

## Tests

Several hundred automated tests, all run against the simulated servo.
: 几百个自动化测试，全部在仿真舵机上运行。

Each area of the tool has its own tests. One set, the conformance tests, is written to be run against any adapter, so that a future real-servo adapter must pass the same checks as the simulated one; the tests that would move an actuator are marked so they can be left out. The project's rule is that a failing test is never loosened, skipped or special-cased to get a pass.
: 工具的每个部分都有自己的测试。其中一组叫一致性测试，写成可以对任何适配器运行，所以将来的真实舵机适配器必须通过和仿真舵机同样的检查；会让执行器移动的测试都做了标记，可以不运行它们。项目的规矩是：绝不为了通过而放松、跳过失败的测试，或给它开特例。

## Documentation

What is being built and why, the step-by-step plan, the hardware facts, and the rules.
: 在做什么、为什么做，一步步的计划，硬件事实，以及规矩。

The requirements document says what the tool is for; the development plan lists fifteen steps, of which the first eight are done, and carries dated notes for whoever continues; the targets file records each hardware fact with how sure it is. Datasheets go in the reference folder when the owner adds them. Older documents are kept in an archive and must not be followed. This parts list lives here too.
: 需求文档说明这个工具是做什么用的；开发计划列出十五个步骤，其中前八步已经完成，还带着写给接手者的、标了日期的备注；目标文件记录每一条硬件事实以及它有多可靠。数据手册等主人加进来后放在参考资料文件夹里。旧文档存在归档里，不要照着做。这份清单也放在这里。

Hardware facts come from `docs/targets.md` or a primary source, not from memory. (CLAUDE.md, 2026-11-16)
: 硬件事实来自 `docs/targets.md` 或第一手资料，而不是凭记忆。（CLAUDE.md，2026-11-16）

## Project setup

The package's name, licence, dependencies and tool settings.
: 软件包的名称、许可证、依赖和工具设置。

These files make the tool installable and name the `servobench` command. The project keeps its dependencies few on purpose: four libraries at run time, and adding another needs a stated reason. The licence is Apache-2.0.
: 这些文件让工具可以安装，并定下 `servobench` 这个命令名。项目有意把依赖保持得很少：运行时只有四个库，再加一个需要说明理由。许可证是 Apache-2.0。

Do not push, publish or create a public repository without the owner's agreement. (CLAUDE.md, 2026-11-16)
: 没有主人的同意，不要推送、发布或创建公开仓库。（CLAUDE.md，2026-11-16）
