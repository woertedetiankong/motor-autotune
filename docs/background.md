# Background: what the CoRL 2026 Agentic Robotics workshop means for this tool

Reference only. `docs/PRD.md` governs what is built.

## Source

- Workshop on Agentic Robotics at CoRL 2026, Austin, Texas, 12 November 2026: <https://agentic-robotics-workshop.github.io/>
- Recording: <https://www.youtube.com/watch?v=SuZwqlS-weA>
- Transcript: `docs/reference/corl2026-agentic-robotics-workshop-transcript.txt`, taken from YouTube's automatic captions. Timestamps below refer to it.

The captions garble names. Speaker names and affiliations here come from the workshop schedule. The phrase "Kuka with 5.5 and Kortex with 56 Astra" in the last talk is probably "Claude Code with Opus 5.5 and Codex with GPT-6 Astra"; that reading is inferred from the other talks, not confirmed.

The earlier, longer summary of the last talk is kept in `docs/archive/2026-11-15-original-background.md`.

## What each talk contributes

### Guanya Shi, Carnegie Mellon University (from 0:17)

- The hardest part of letting agents improve a real robot was building the environment: reset, step, observe, reward. Humans built it together with the agent; after that the improvement loop ran unattended (0:23:46).
- The biggest problem during unattended runs was motors overheating, which still needed a human (0:26:06).
- A safety harness capped how fast the agent could move the real robot (0:27:35).
- Rehearsing in a coarse, inaccurate simulation and then allowing only five real trials worked far better than starting on the real robot (0:27:10, 0:29:01). The simulation did not need to be correct to be useful (0:30:32).
- With full access to simulator state, agents did much better than with raw sensors. Robotics stays hard because the world is only partly observable (0:33:28).
- No single model won every task (0:35:36).

### Ken Goldberg, UC Berkeley (from 0:40)

- Industrial users want reliability, speed and interpretability, and distrust a black box (0:43:55).
- His group's agent produces a program that then runs with no agent in it (0:48:32).
- At Ambi Robotics an agent proposed three fixes for package jams. The first two made things worse; the third halved the failures (1:00:10).

### Wentao Yuan, Google (from 1:12)

- Three constraints separate robots from software: actions cannot be undone, observations are partial, decisions must be made in real time (1:15:52).
- The stack is layered by rate: a slow reasoning layer, a faster action model, and a fast controller on the robot (1:18:10).
- The same agent drove a different vendor's stack underneath. It does not matter what sits below "as long as the interface is clear" (1:22:28).
- Risky actions are practised in simulation before they are run for real (1:27:44).

### Jonathan Tremblay, NVIDIA (from 1:37)

- Problems that used to be sticky, such as calibrating a camera or building a simulation from the real scene, are becoming easy. What remains is evaluation, environments and deployment (1:51:17).
- Robot tasks should be judged on precision, not only on roughly placing an object (1:54:31).

### Tianyu Li, Dexmate (from 2:07:49)

- Coding agents completed simple real-robot tasks but failed the one needing precise contact (2:10:02).
- Most of the time went to model reasoning between moves, not to robot motion (2:11:24).
- Safety reactions came too late. An agent noticed force on the arm and retracted almost a minute later (2:13:45). The model considered a motion complete even after an unnoticed collision (2:14:03).
- An agent sent an angle in degrees to a tool expecting radians, producing a rotation almost 60 times too large (2:14:27).
- Giving the agent more tools lowered the success rate and raised the cost. A tool helps only when its result feeds the next decision (2:15:44 to 2:16:35).
- Agents declined to rehearse in simulation when they judged a task easy (2:17:05). When they did build a simulation, they guessed contact parameters such as mass and friction (2:17:36).
- In a safety-critical moment the stop must happen within the second, without waiting for the model (2:20:27).
- The proposed stack puts hardware self-protection at the bottom, running fastest (2:22:34, and the answer at 2:27:53).

## What is not this project's problem

These are runtime problems of agents controlling whole robots. This tool does not address them and should not claim to:

- model latency of seconds per move;
- cost per task;
- precise contact tasks;
- detecting that a task is finished.

## Design rules taken from the workshop

| Rule for this tool | Where it comes from |
|---|---|
| The product is the experiment environment: reset, step, observe, and a measurable result | Shi |
| Thermal budget and cool-down between experiments are in the first release | Shi |
| Simulation is for rehearsal and safety tests; real trials are few and budgeted | Shi, Yuan |
| The tool enforces rehearsal; it is not left to the agent's judgment | Li |
| Limits and the stop act below the agent, without waiting for it | Li, Yuan |
| Every quantity carries an explicit unit | Li |
| Few commands, each returning a verdict the agent can act on directly | Li |
| Every change is measured against a baseline and reverted if worse | Goldberg |
| The output runs and reads without an agent: numbers, a report, a replayable record | Goldberg |
| A clear adapter contract lets the same workflow drive different hardware | Yuan |
| Measured actuator parameters replace guessed ones in simulation | Li |
| Stay neutral between agent vendors | Shi |
