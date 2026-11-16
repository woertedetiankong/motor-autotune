# Motor Auto-Tuning Tool: Product Requirements

> This document describes only the problems to solve and the required outcomes. Claude Code should first propose the technical approach, programming language, architecture, protocols, parameters, and specific algorithms, and wait for my confirmation before starting development.
> Background reference: `docs/background.md` (key points from the CoRL 2026 Agentic Robotics Workshop, for reference only).

## 1. Background

- Tuning motors—the current, velocity, and position loops in field-oriented control (FOC)—is one of the most time-consuming tasks for embedded systems and robotics engineers: repeatedly trying parameters, inspecting waveforms, and making changes based on experience.
- AI agents such as Claude Code can already take part in engineering tasks, but letting a large language model control a motor directly presents clear problems: slow responses, basic mistakes such as unit errors, difficulty interpreting raw data, and no independent safeguard when something goes wrong.
- I want to build a tool that **connects to a real motor, automatically collects data, runs experiments, and tunes the control parameters**, while maintaining safety throughout the process.
- This is the first step toward a longer-term goal: enabling AI agents to control real robots, such as robotic arms, safely. We will therefore start with tuning a single motor, where outcomes are measurable and risks can be controlled.

## 2. Target Users

- Embedded systems engineers working on robotic joints, servos, and motor control.
- Graduate students and engineers building robotics prototypes.
- Students in my courses (buildautonomy.dev, ServoHub, and other motor control courses): this tool will also serve as an experimental platform for the courses.

## 3. Core Requirements

### 3.1 Automated Data Collection
- Connect to real motor driver boards and read physical quantities such as current, velocity, position, and temperature in real time.
- Support saving, reviewing, and comparing the data.

### 3.2 Automated Experiments
- Independently apply test signals to the motor, such as steps and frequency sweeps, to characterize the motor and its load.
- Eliminate the need for a person to perform each measurement manually.

### 3.3 Automated Tuning
- Automatically tune the control parameters for the current, velocity, and position loops based on experimental results.
- Support iterative tuning: adjust, measure, and refine until the targets I specify are met, such as maximum overshoot and settling time.
- Clearly explain what changed in each iteration and why.

### 3.4 AI Participation
- Allow AI, such as Claude, to help analyze results, explain problems, and suggest tuning changes.
- AI acts as an "advisor" and is optional: basic tuning must work without an AI connection.
- Ideally, AI agents such as Claude Code should be able to invoke the tool through a standard interface such as MCP.

### 3.5 Results Report
- At the end of each tuning session, provide a before-and-after comparison (plots and key metrics), the final parameters, and a record of the process.

## 4. Safety Requirements (Mandatory)

- Errors in the tool or AI must never cause motor damage, uncontrolled motion, or injury.
- Enforce limits on current, temperature, position, velocity, and other relevant quantities. Stop immediately when a limit is exceeded, without waiting for AI judgment.
- Reject invalid commands, such as those with incorrect units, abnormal values, or out-of-range values, before they reach the motor.
- The motor must automatically enter a safe state if communication is lost or the program hangs.
- Provide an emergency stop.
- After a fault, recovery must require human confirmation. AI must not clear the fault on its own.
- Consider which protections are needed in the tool and which are needed in the driver board firmware.

## 5. Generality

- The tool must not be limited to a single board. When switching driver boards or motors, the tuning workflow itself should be reusable, requiring only adaptation and configuration.
- The first target hardware is my own FOC driver board (details will be provided separately). Later, I would like to support common open-source motor drivers or servos to demonstrate the tool's generality.
- When the motor or load connected to the same board changes, the tool should be able to tune it again automatically.

## 6. Verifiability

- Development and testing must be possible without hardware, for example using a simulated motor.
- Demonstrate that the tool actually works: for example, compare manual tuning against automated tuning in terms of results and time required across several motors or loads.

## 7. Out of Scope for Phase One

- Multi-joint robotic arms and whole-robot control.
- AI agents directly executing tasks such as grasping.
- A polished graphical interface (a functional interface is sufficient).

## 8. How I Want Claude Code to Proceed

1. First read this document and `docs/background.md` in full, then propose the overall approach, technology choices, a phased plan, and any information or decisions needed from me, such as hardware details.
2. After I confirm, develop the tool in phases. Each phase must produce results that can be demonstrated and verified.
3. Ask me before making changes involving real hardware or firmware.
