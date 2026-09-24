# Capability Map Rule

Standing rule for the living [Robie Capability Map](https://docs.google.com/document/d/1RKtB1mb4wUoR4V_UJiPKDPmmklq3ktY22iS78px57bY/edit).

## Rule

Any new capability or integration gets a row in this map before it ships. The human confirms the row; LLMs do not invent capabilities.

## Reliability

Every capability or integration row MUST include a **Reliability** field with exactly one of:

- **Reliable** — agency can bet on it in Production with normal babysitting
- **Proving** — real, but Test / not boring yet / still breaking
- **Blocked** — waiting on vendor, IAM, or a hard gate

A row without that field, or with any other value, is not a complete row and does not satisfy the rule above.

## Operating focus

As of 2026-09-23, Carlo's operating focus is: stop opening new fronts; clear the existing list until items move to Reliable. Do not invent new capabilities; humans confirm rows.

## Living map

- Title: Robie Capability Map
- https://docs.google.com/document/d/1RKtB1mb4wUoR4V_UJiPKDPmmklq3ktY22iS78px57bY/edit

## Accuracy audit

Dusty + Carlo do a weekly accuracy audit. First audit week of 2026-09-29.

## Coverage

The map covers:

- Safety spine
- Test-before-Prod
- Integrations (QBO / Ascend / EZLynx / Chat / etc.)
- Infrastructure (`hermes-poc-01` / `hermes-test-01`, project `streetsmart-hermes-poc`)
