# Principles of good code

Good code reliably fulfills its purpose while minimizing the complexity of
understanding, changing, and operating it.

Apply these principles when designing, implementing, refactoring, and reviewing
context-guard. The order expresses priorities for tradeoffs, not independent
checkboxes or an inflexible algorithm. `AGENTS.md` states the concrete project rules
that apply them; Ruff and `tests/test_architecture.py`
enforce the mechanical ones.

## Priority order

1. **Correct** — Reliably fulfills its intended contract within explicitly
   supported operating conditions. Make important assumptions, invariants, and
   preconditions explicit; do not expand the contract to cover every hypothetical
   scenario.
2. **Safe** — Manages meaningful risks proportionately. Use types, validation,
   invariants, and appropriate error handling to prevent or contain failures.
   Types are one mechanism, not a substitute for sound behavior.
3. **Minimal** — Introduces only the code, abstractions, dependencies, and
   safeguards justified by actual requirements or meaningful risks. Optimize
   for conceptual simplicity, not the fewest lines.
4. **Clear** — Makes intent, behavior, assumptions, and inputs/outputs easy to
   understand through names and structure. Document why when the rationale
   is not apparent; avoid documentation that merely restates the code.
5. **Focused** — Has cohesive responsibilities, clear ownership, and well-defined
   boundaries. Keep unrelated concerns separate and changes localized.
6. **Predictable** — Behaves consistently and unsurprisingly. Make side effects,
   dependencies, and failure behavior explicit; prefer determinism where useful.
7. **Verifiable** — Makes important guarantees demonstrable through appropriate
   tests, static analysis, or other proportionate evidence. Being easy to test
   does not establish correctness by itself.
8. **Composable** — Provides well-defined interfaces so components can work
   together without relying on each other's internals or hidden dependencies.
   Introduce reuse and extension points when there is a concrete need.
9. **Observable** — Provides sufficient visibility to understand behavior,
   diagnose failures, and investigate unexpected outcomes, without needless
   instrumentation or exposing sensitive information.
10. **Efficient** — Meets actual performance and resource requirements. Use time,
    memory, compute, and other resources appropriately; optimize when a real
    requirement or evidence justifies the added complexity.

## Proportionality and tradeoffs

**Meet the contract, manage meaningful risks, then minimize complexity.
Improve everything else in proportion to its expected value.**

- Correctness and safety establish the minimum acceptable guarantees. Prefer
  higher-ranked principles when objectives conflict, while still satisfying
  essential requirements. Among solutions that meet those guarantees, choose
  the one with the least overall complexity.
- Every abstraction, safeguard, dependency, and optimization should have a
  concrete, defensible purpose that justifies its complexity through expected
  value or risk reduction. Consider both the
  likelihood and consequences of failure: rare data corruption or silent wrong
  results can deserve strong protection; speculative low-impact cases usually
  do not justify elaborate machinery.
- Defining unsupported behavior can be enough. Rejecting an input, propagating
  an error, or documenting a legitimate precondition may satisfy the contract;
  elaborate recovery is not inherently better.
- Lower-ranked principles still matter. Additional code or structure may be
  worthwhile when it materially improves clarity, verification, integration,
  diagnosis, or resource use. Explain significant tradeoffs with their concrete
  benefit rather than applying the ranking mechanically.
- A lower-ranked objective becomes part of correctness when the contract
  requires it: for example, a latency limit, memory budget, or pipeline SLA.
- Scale verification and operational safeguards to the change's impact,
  reversibility, and failure consequences. Retain tests needed to establish
  confidence in correctness; avoid redundant or low-value tests, speculative
  generalization, and premature optimization.
