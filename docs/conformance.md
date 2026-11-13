# Adapter infrastructure and conformance

The registry and shared test infrastructure discover five adapters, including Copilot CLI.
Structural completeness is a separate gate from behavioral conformance. Shared scenarios
construct temporary native drivers and invoke source hooks by default; scenarios that call
the driver's install operation instead invoke the registered installed-kit commands.
The separate `tests/installed/` layer installs a kit and exercises its exact registered
commands. Native mechanics and retained lifecycle evidence are decoded independently.
Failures and unfinished capabilities remain visible in command output and optional
JSON results. The dispatcher and rollout refactors are outside this contract.

## Run the two gates

Run from the repository root with Python 3.14 and `uv`:

```sh
uv run --python 3.14 python scripts/conformance.py --structure
uv run --python 3.14 python -m unittest discover -s tests -t .
uv run --python 3.14 python scripts/gen_docs.py --check
uvx ruff check .
uvx ruff format --check .
```

The structural command validates every registered adapter, independent of any behavior filter.
The shared unittest command retains the recorded-payload and installed-kit layers, including
their independent native-schema, deadline, exact-command and byte-preservation checks. Adapter
mechanics suites listed in `AGENTS.md` still run separately. None of these commands reads live
runtime homes or starts authenticated runtime sessions.

```sh
uv run --python 3.14 python scripts/conformance.py
uv run --python 3.14 python scripts/conformance.py --runtime codex --contract prompt_warning
uv run --python 3.14 python scripts/conformance.py --runtime cursor --scenario prompt_warning_once
uv run --python 3.14 python scripts/conformance.py --json /tmp/conformance.json
```

Conformance exits nonzero for a failed scenario or a `NOT IMPLEMENTED` declaration. Do not mask
its exit status when collecting reports. Filters support focused iterations; they do not replace
the complete suite. Repeat the relevant structural and conformance checks with Python 3.15
when changing shared infrastructure.

## Interpret results

| Result | Meaning |
| --- | --- |
| PASS | The scenario executed and its assertions passed. |
| FAIL | The executable scenario observed a mismatch, or failed to establish required evidence. Read its assertion and classify the cause before changing anything. |
| GAP | A validated `runtime_limit` or `not_applicable` declaration links to the runtime documentation. |
| NOT IMPLEMENTED | Declared adapter behavior is unfinished. This keeps conformance nonzero. Missing driver, fixture or scenario infrastructure instead fails structural validation. A supported silence or wake capability cannot inherit the base driver's unimplemented action. |

Guarantees never accept gap declarations. An emulated capability runs the same shared contract.
Declared support in the generated README matrix is separate from executable results;
run `scripts/conformance.py` for the current dispositions.

Offline hooks cannot prove a native host starts an idle parent turn or actually delivers hook
context to a model. Claude, Copilot and Cursor wake scenarios independently replay recorded
native lifecycle captures. Cursor requires actual post-idle child output, matching Task notification and a new
parent generation without intervening input. Documentation alone never produces PASS,
and CLI evidence does not establish IDE behavior.

## Extension points

`adapters/registry.py` discovers explicit `adapters/<name>/adapter.py` markers in sorted order.
Production declarations in `core/features.py` contain import paths and capability gaps only;
they do not import hooks, mappings, fixtures or drivers. Source-document validation is separate,
so the installed kit can discover adapters without shipping source docs or tests.

Tests load `adapters/<name>/tests/driver.py`. The driver translates semantic actions, native tools,
identities, telemetry, installed commands and response fields. Shared scenarios in
`tests/conformance/catalogue.py` and `test_cases.py` own the assertions. Native decoders preserve
blocking, delivery channel and text independently of production hooks and mappings. Structural
checks trace local imports transitively to enforce that boundary. Invocation and fault-injection
wrappers may exercise production code in a subprocess; they do not decode by calling `respond`.

Drivers validate process exit status, stderr, one JSON response and the native response schema
before semantic decoding. Existing schema validation reuses recorded Codex/Genie binary schemas
and the maintained Claude/Cursor native contracts. A new native response family supplies its
driver's response validator. Existing adapters can reuse `tests/driver_support.py`; it is test-only.

Cursor's prompt recipe advances from a valid prompt response to a later tool response before
checking delivery. The same scenario still requires returned text, `note_delivered`, and once-only
behavior. Silence tests control native activity time without editing policy state. Native-specific
unreturned/orphan tests remain in the Cursor suite.

The temporary synthetic-package test demonstrates discovery, complete scenario generation and
one actual hook-process scenario without editing shared scenario definitions. The delivery mutation
test copies shipped source into a temporary kit, removes returned note text while retaining native
response shape and event logging, observes the unchanged shared scenario fail, restores the source,
and observes it pass. Production adapter files are untouched by these tests.

Native recording and smoke wiring require explicit runtime-specific implementations;
registry discovery does not infer a recorder or a host-specific smoke path. Ordinary
tests never collect live fixtures. Driver decoders validate native responses independently
of production hook and mapping renderers; mandatory guarantees cannot be opted out.

Copilot's handoff-access action explicitly configures a workspace grant through the packaged
helper; other adapters may configure access during ordinary installation. The shared scenario
independently reads native configuration and requires the exact handoff-directory grant in either case.

## Shared scenario organization and discovery

`catalogue.SCENARIOS` is the single ordered inventory. `test_cases.ConformanceCase`
composes cohesive contract mixins from `test_context.py`, `test_handoffs.py`,
`test_subagents.py`, `test_installation.py` and `test_boundaries.py`; `base.py` owns
only temporary driver setup and common assertion support. Adding a scenario requires
an executable method and one catalogue entry. Structural checks reject missing,
duplicate and unregistered scenarios before any adapter executes.

Ordinary unittest package discovery and direct loading of `test_cases` both return
an empty behavior suite. Run behavior through `scripts/conformance.py`, which validates
all adapters before selecting runtime, contract and scenario intersections. This keeps
unbound cases from executing and ensures filters cannot hide missing infrastructure.
A report includes only executed cases; a selector intersection containing no cases fails.

Shared role-budget thresholds require the exact role and budget event details, and
shared tool-warning checks require actual returned context, one delivery event and no
repeat at the same tier. Adapter hook tests retain native telemetry, response field
contracts and distinct lifecycle/failure variants even when they overlap these behaviors.

Shared prompt-block scenarios replay recognized report envelopes above the block tier,
require the next ordinary prompt to block, and retain blocking for quoted or embedded
markers. They use the same discovered adapters and native drivers as every other scenario.
