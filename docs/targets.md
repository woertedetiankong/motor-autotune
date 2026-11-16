# Hardware and software targets

Facts about the hardware the tool is built against, with how sure each one is. Update this file whenever a fact is confirmed or corrected. Recorded 2026-11-15.

Confidence labels: **confirmed** means read from the primary source; **reported** means from a retailer or third-party page; **unknown** means not found yet.

## 1. Feetech HD1910 smart servo (servo stage)

The first real target. Model string in the shopping list: `HD-1910-C001`.

| Fact | Value | Confidence |
|---|---|---|
| Type | Coreless motor, metal gears, dual shaft, serial-bus servo | reported |
| Bus | Half-duplex asynchronous serial at TTL level, one signal wire | reported |
| IDs and baud rate | IDs 0 to 253; 38,400 bps to 1 Mbps | reported |
| Position range | 0 to 4095 steps over 360 degrees, 2048 at centre | reported |
| Supply voltage | 5 to 8.4 V | reported; listings disagree |
| Torque | About 9 to 10 kg·cm stall; one source cites 2.2 kg·cm rated at 4.8 V | reported; listings disagree |
| Feedback | Position, speed, voltage, current, temperature | reported |
| Modes | Angle, constant speed, constant current | reported |
| Register table (addresses, sizes, units, which are persistent) | | **unknown** |
| Which Feetech protocol family and memory-table revision it follows | | **unknown** |
| Whether position gains can be written to volatile memory | | **unknown** |
| Built-in protections and their registers (angle limits, torque limit, temperature, voltage) | | **unknown** |
| Achievable polling rate and timing jitter through the USB adapter | | **unknown**; measured in plan step 8 |

Do not write register addresses into code from memory or from another Feetech model. They come from Feetech's datasheet and register table for this exact model, which the owner will place in `docs/reference/`.

Sources for the reported rows:

- <https://openelab.io/products/feetech-hd1910-microduck-coreless-servo>
- <https://openelab.io/fr/blogs/learn/microduck-servo-choice-feetech-hd-1910-c001-diy-biped>
- <https://aifitlab.com/products/feetech-hd1910-servo-motor>

### Bench rig

The first experiments run on one spare servo on the bench, not on the robot.

| Item | Value |
|---|---|
| Servo | One spare HD1910 |
| Bus adapter | Feetech URT2 debug board (USB to servo bus), from the Microduck shopping list |
| Power | Bench supply with a current limit preferred; value to be filled in by the owner |
| Load | A small pendulum arm of known mass and length; values to be filled in by the owner |
| Serial port name on the development machine | to be filled in by the owner |
| Servo ID and baud rate | to be filled in by the owner |

A known load is required. Without one, the measurements cannot separate the servo's inertia from its torque.

## 2. Microduck rebuild (servo stage, later steps)

The owner is rebuilding Microduck from <https://github.com/kissqy/Microduck-kissqy>.

| Fact | Value | Confidence |
|---|---|---|
| Servos | 15 × Feetech HD1910 | confirmed from the repository's shopping list |
| Computer | Radxa ZERO 3W | confirmed from the shopping list |
| Boards | Custom HAT, IMU board, and HUB V3 with a 1000 µF buffer capacitor | confirmed from the shopping list |
| Battery | NP-F550 pack, 2200 mAh | confirmed from the shopping list |
| Control | A walking policy run at 50 Hz on the robot | reported for the official Microduck |
| State | The author reports walking, sitting, standing and kicking, using preinstalled motion models | reported by the repository's readme |

### Licence boundary

The repository's index licence says its author's original software, tools and board designs are for personal, non-commercial use, and that commercial or organisational use needs written permission. Some parts keep Apache-2.0 from Pollen Robotics, and some printed parts are CC BY-NC-SA 4.0.

Consequences for this project:

- The owner may build the robot for personal use.
- No code, schema, register table or file from that repository is copied into this project.
- The tool speaks to the servo through Feetech's own documented protocol.
- On the assembled robot the robot's own computer owns the servo bus. The tool only reads there, and only when the robot's software is not driving the bus.

## 3. ServoHub firmware (FOC stage)

The owner's motor-control firmware: <https://github.com/magic-alt/servohub>, MIT licence. The findings below come from reading the repository's structure, readmes and headers at the commit dated 2026-09-12. Nothing was built or run.

| The tool needs | ServoHub has | Gap or caution |
|---|---|---|
| A simulator | `SIL_project/`: the real control code runs on a PC against a simulated motor and talks MAVLink over ZMQ | Not built or run yet |
| High-rate capture | `common/software_scope/` (8 channels, 32,000-sample buffer, triggers) and `service/factory/trace/` (recorder called from the current-loop interrupt, 1024 samples by default) | The factory readme says the trace hook is not yet wired into the control loop and its CANopen objects are not yet in the object dictionary |
| A contract | `contract/schema/`: parameters, signals, units, diagnostics and capabilities, checked in CI | Velocity, acceleration and torque units are marked "not yet verified"; fault entries are marked not firmware-verified |
| Identification | `app/Identify/`: modes for direction, electrical angle, pole pairs, electrical and mechanical parameters, and one more (`tq_fc`) | Purposes inferred from file names |
| Board-side protection | Over-current, over-voltage, over-speed, over-temperature, software position limits; a CAN-timeout fault is listed | Behaviour on lost communication not checked |
| A host protocol | MAVLink, CANopen with CiA 402, EtherCAT | The contract notes say the MAVLink source dialect file is missing and regeneration is blocked. A Python host needs that file. |

Other facts:

- Target microcontroller is an STM32H743 (`embedded_project/Y01/`).
- Control models are in Simulink (`simulink_project/`), including position, speed and current loops, a notch filter, and an impedance-style mode.
- The repository refers to a separate host program, `servo_host`, which is not public. What it already does is an open question for the owner.
- The repository is labelled a fork of `servo_hub`. The owner should confirm they are free to build a public project on the upstream.

## 4. Simulated servo (all stages)

The simulated actuator is a target in its own right. It is described in `docs/development-plan.md`, step 3.

## 5. Related project that is not a target

`octos` (<https://github.com/octos-org/octos>) is an agent harness written in Rust. It runs agents; it does not touch hardware. It could host this tool the way Claude Code and Codex do. This project does not build a harness.
