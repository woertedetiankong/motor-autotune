# Actuator bench tool

A command-line tool that lets an engineer, or their coding agent, run safe, bounded experiments on one actuator and get back measured facts. Working name: `servobench`.

## Where things are

- `docs/PRD.md`: what is being built and why. Start here.
- `docs/targets.md`: facts about the hardware, each marked with how sure it is.
- `docs/development-plan.md`: the step-by-step plan. Find the first unticked step and work on that one.
- `docs/background.md`: workshop notes behind the design rules. Optional.
- `docs/reference/`: source material, including datasheets when the owner adds them.
- `docs/archive/`: superseded documents. Do not follow them.

## Rules that always apply

The full list, with reasons, is in `docs/development-plan.md`. The ones that matter most:

- Real hardware moves only with the owner present and their go-ahead in the current session.
- No persistent write to a device without the owner confirming that specific write.
- Do not edit `bench.toml` or loosen a limit check to make something pass.
- Every quantity carries an explicit unit. A bare number is an error.
- Hardware facts come from `docs/targets.md` or a primary source, not from memory. If a needed fact is unknown, stop and ask.
- Copy nothing from the `kissqy/Microduck-kissqy` repository; its licence is non-commercial.
- No MCP server, graphical interface, agent harness or FOC work in the current stage.
- Do not push, publish or create a public repository without the owner's agreement.

## Working agreement

- Finish one step, show the evidence that its "Done when" checks pass, then tick it and commit.
- If a check cannot be met after a real attempt, report what you found. Do not widen a tolerance or skip a test to get a pass.
- Say plainly what is unverified.
