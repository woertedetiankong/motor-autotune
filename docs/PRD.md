# Product requirements: actuator bench tool for coding agents

Status: agreed direction as of 2026-11-15. This replaces the original autotune-first PRD, which is kept in `docs/archive/` for history.

Working name: `servobench`. The name is a placeholder until the owner confirms it (see section 12).

## 1. Summary

A command-line tool that lets an engineer, or the coding agent working for them, run safe, bounded experiments on one actuator and get back measured facts.

A coding agent such as Claude Code or Codex can write motor and robot code, but it cannot see the hardware. Today the engineer flashes, watches a scope or a plotter, and describes the result in words. This tool closes that loop: run an experiment, get a verdict and a few numbers, check whether a change worked.

The tool works with no AI attached. An agent is one more way to drive it.

## 2. Who it is for

- Robotics engineers who bring up, characterize and tune actuators: smart servos, CAN actuators, and their own motor-control boards.
- Engineers who use a coding agent day to day and want it to verify its own work on real hardware.
- Builders of small legged robots and servo arms who need simulation to match the real actuator.
- Students of the owner's motor-control courses, who are also the first test users.

## 3. What it does

Three commands, each usable on every supported actuator:

| Command | Question it answers | Result |
|---|---|---|
| `check` | Is this actuator alive and set up correctly? | Pass or fail per item: responds, identity, direction, zero, limits, voltage, temperature, bus rate |
| `characterize` | How does this actuator actually behave? | Response metrics, measured model parameters, a simulated-versus-real plot, parameters exported for a simulator |
| `tune` | Can the outer gains meet this target? | Before and after metrics against a stated target; the change is kept only if it is better |

Every command returns a verdict first, then a few numbers, then paths to the files it wrote. Every run is saved as plain files in the user's repository.

Loop tuning inside a field-oriented-control (FOC) drive is a later stage, not part of the first release (see section 8).

## 4. The three parts of the tool

| Part | Runs on | Does | Language |
|---|---|---|---|
| Host tool | The engineer's computer | Runs experiments, analyses data, writes reports, exposes the commands | Python |
| Board-side component | The motor controller | Everything that must be fast or must be trusted: capture, test-signal generation, limits, safe state, fault latching | C |
| Contract | Shared | Signals, parameters, units, declared capabilities | Schema |

In the servo stage only the host tool is built, because the servo runs the manufacturer's closed firmware. The board-side component arrives with the FOC stage.

## 5. Safety requirements

These are mandatory. They are written as layers because no single layer can carry the guarantee.

1. **Physical layer.** The engineer has a way to cut power. The tool's documentation says so plainly and the tool never claims to replace it.
2. **Board layer.** Limits on current, temperature, position and speed act on the board without waiting for the host. The board enters a safe state by itself when communication is lost. A fault stays latched until a human clears it.
3. **Host layer.** The tool rejects a command before it reaches the actuator if it has no unit, a wrong unit, or a value outside the bench limits. It watches every sample during an experiment and aborts on a limit. It respects a thermal budget and a cool-down between experiments.
4. **Agent layer.** An agent can only do what the three layers below allow.

Specific rules:

- **Units are always explicit.** A bare number is rejected. (In the workshop that motivated this project, a degrees-for-radians mix-up produced a rotation about 60 times too large.)
- **Rehearsal is enforced.** Every experiment runs in simulation first, inside the same command, and the real run happens only if the rehearsal stays inside the limits. The agent cannot skip it.
- **Changes are measured.** Every change is compared against a baseline and reverted if the result is worse.
- **A human recovers from faults.** The tool offers no command that clears a device fault.
- **Capabilities are declared.** Each adapter states which protections its hardware really has. Where a protection is missing, the tool says so and restricts what it will run.
- **Honest limits.** An agent with a shell can edit files and open the serial port directly. Host-side checks are therefore a second line of defence, not the guarantee. The guarantee has to come from the board and the physical layer.

Real hardware and firmware are changed only with the owner's explicit agreement.

## 6. Agent use

- The interface is a command-line program with a JSON output option, clear exit codes, and error messages that say what to do next.
- A short instruction file tells an agent when to run each command and how to read the result.
- The commands are a thin layer over a Python library.
- MCP is not in the first version. It can be added later as a thin wrapper over the same library.
- The tool is not an agent harness and does not depend on one. Claude Code, Codex and others are hosts.

## 7. Several boards

Engineers have several boards on hand, so the workflow must carry across hardware. The project does not promise to support every board itself. It provides:

- an adapter contract, the small piece of code that translates the tool's commands for one actuator;
- a conformance test suite that tells an adapter's author whether it is correct;
- instructions that let a user's own coding agent write an adapter;
- a simulated actuator, which is treated as a real target from day one so the contract is exercised early.

## 8. Targets and stage order

The owner chose this order on 2026-11-15: easy to hard, and the Microduck rebuild is happening anyway.

1. **Servo stage.** Feetech HD1910 smart servo on the bench, then across the 15 servos of a Microduck rebuild. Host tool only.
2. **FOC stage.** The owner's ServoHub firmware, first against its software-in-the-loop build and then on the board. Adds the board-side component, frequency-response measurement, and high-rate capture.
3. **Loop-tuning stage.** Current, velocity and position loop tuning on boards with open firmware.
4. **Later.** A portable board-side C module for other people's boards, then a whole leg, then a whole robot.

Details of each target are in `docs/targets.md`.

## 9. Verification

- **Without hardware.** All development and automated testing runs against the simulated actuator, including fault injection for the safety behaviour.
- **Conformance.** Every adapter passes the same test suite.
- **Proof on hardware.** The servo stage is complete when the tool has produced, on a real HD1910, a plot of simulated against real response before and after using the measured parameters, with the data published.
- **User test.** `characterize` is given to five course students. The signal looked for is whether they run it on a second actuator without being asked.

## 10. Out of scope for the first release

- MCP server, graphical interface, agent harness.
- FOC inner-loop tuning.
- Multi-joint motion experiments on an assembled robot. On the assembled Microduck the tool only reads.
- Torque accuracy and efficiency measurement, which need a torque sensor or test stand.
- The runtime-agent problems reported at the workshop: model latency, cost per task, precise contact, task-completion detection.

## 11. Decisions made and why

| Decision | Reason |
|---|---|
| Debugging and characterization first, auto-tuning later | Computing gains is deterministic and already solved in commercial drives. Diagnosis and measurement are where a tool that an agent can drive adds something. |
| Define the user by the job, not the hardware | Most robotics engineers work at the actuator boundary: command in, measurements out. That boundary is the same for a servo, a CAN actuator and an FOC board. |
| Servo first, FOC second | Owner's choice: easy to hard, lower-risk hardware, and a visible result sooner. |
| Python on the host, C on the board | The host work is orchestration and analysis, where Python's ecosystem and low barrier to contribution matter. Fast and trusted work lives on the board. |
| Command line first, no MCP in version one | Coding agents already run command-line tools. MCP would add a second interface without adding capability. |
| Own code, permissive licence | The Microduck rebuild repository is personal non-commercial only, so nothing is copied from it. |

## 12. Open decisions for the owner

- **Name.** Decided 2026-11-16: `servobench`. It was free on the Python package index on that date; check again before the first public release.
- **Licence.** Decided 2026-11-16: Apache-2.0.
- **Data licence** for published servo measurements.
- **Bench rig.** Arm mass and length for the pendulum load, and the bench power supply.
- **ServoHub host software.** What `servo_host` already does, so the FOC stage reuses it instead of duplicating it.

## 13. How to proceed

Development follows `docs/development-plan.md`, one step at a time. Each step ends with something that can be demonstrated and checked.
