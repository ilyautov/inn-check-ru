# Final-output decision benchmark (Phase 6A)

Corpus stimulus revision 2 explicitly amends exactly one synthetic case while
retaining all 29 cases and every expected atomic, recommendation, source-license,
independent-review and adjudication label. In `mixed_years`, current 2025 revenue
changes from 120000 to 500000 both in normalized input and its corresponding
frozen finance response; prior 2024 revenue remains 100000 and staff remains zero
for 2022. Growth is therefore 400% instead of 20%. Staff evidence is necessary
above the 300% growth threshold, so the retained `unknown/period_mismatch` label
now describes its intended branch. At 20% growth, revenue alone proves the transit
condition false and the dated staff is irrelevant; a separate focused control
retains and verifies that result. This is a disclosed stimulus correction, not
relabeling unchanged input to match returned output. Manifest
`corpus_revision_history` preserves old/new case, input, raw-response and corpus
hashes; the other 28 case hashes remain unchanged. Schema revision 2, manifest
revision 6 and all their prior history are preserved. Rubric revision metadata is
aligned from 5 to 6 for the structural contract revision; its semantic content,
thresholds, catalogue and labels remain unchanged.

The independent oracle binds each leaf through its actual response ID, checked
response source/subject/hash and unique frozen field mapping. Equal bytes from
primary and mirror providers remain separate observations in the same origin
family, regardless of provider order. A private local harness context binds real
capture IDs to frozen mappings before inspected CLI/MCP payload extraction; it
adds no production metadata or public output field. Missing provider availability
declarations do not override verified mapped observations; explicit unavailable
states and complete subject-bound scope checks remain enforced. Diagnostic atoms
come from independently expected claim knowledge/reasons, so a verified conflict
permits `conflict` but never arbitrary prose or an unrelated reason enum.

A successful manual import derives its expected capture transition from the
frozen manual source keys: only field mappings under those source prefixes are
removed. All response identities and unrelated field mappings remain exact.
Start uses the original private context; manual and later CLI/MCP reads use the
independently derived transition, never returned metadata.

Revision 5 adds a separately hashed, independent recursive typed schema before
semantic checks. It closes coercive boolean/numeric metadata, nested containers,
all model variants, and real CLI/MCP envelope fields. The complete accepted
recursive field inventory and bounds are in
[`decision-schema-contract.md`](decision-schema-contract.md). Corpus bytes,
sources, labels and atomic expected claims remain unchanged. Invalid shape
returns structured failures and inventories rejected strings without counting
schema checks as successful assertions. Unsafe semantic traversal is skipped for
that malformed output; all existing input/raw/text/date/dependency checks remain
for valid shapes. Retained real envelopes also expose channel-specific strings.

This is a **synthetic mechanism test**, not a measurement of Russian company risk,
real-world decision accuracy, or representative frequency. The 29 deliberately
selected cases are frozen in `eval/fixtures/workflow-scenarios.json`; the source
bodies, normalized inputs, expected atomic claims, scope and licensing labels are
explicit. All independent-review arrays are empty. Adjudication awaits external
review. A missing-independent-oracle case is excluded from quality metrics, never
counted as a success. Representative frequency has denominator zero and value null.

`decision-corpus/manifest.json` binds the fixture and rubric bytes before execution.
Revision 2 corrects missing date/URL in synthetic court/FSSP observations, keeping
expected answers unchanged; revision 1 hashes and the reason remain in history.
The second revision also freezes an explicit recommendation text oracle.
Revision 3 closes the partial-trace and extra-prose acceptance defects. It freezes
a finite presentation grammar, signal descriptions/source names, field provenance
contracts and table headers. Corpus bytes and every prior expected atomic label
are unchanged. Revision-2 hashes and the reason remain in manifest history. Neither
revision claims a successful empirical run. The manifest separately binds an
existing recorded EGRUL parser smoke fixture; its real-world provenance is not an
independent decision oracle and it is outside the 29 synthetic-case denominator.

Revision 4 removes output-derived prose permissions found by independent closure
review. Profile/manual metadata, source caveats, freshness policies, diagnostic
codes and financial explanation templates are frozen in the rubric. Revision-3
hashes remain in manifest history. Source observations, all 29 case labels and
expected atomic claims remain byte-for-byte unchanged. This is a contract
correction, not a waiver or an empirical result.

## Production path

```python
from benchmark.decision_bench import evaluate, load_cases, assert_gate
report = evaluate(load_cases(), now="2026-10-16T09:00:00Z")
assert_gate(report)
```

The evaluator uses actual `workflow_cli.main` JSON serialization to start, resume,
import a declined manual check, render JSON and Markdown, and save a snapshot.
The existing scorer and resolver materialize the saved decision. Actual MCP
worker subprocesses then read and render that same persisted run. A separate
optional SDK test initializes the real MCP 1.30 stdio server and compares final
JSON and rendered text. The EGRUL smoke test additionally uses the existing raw
fixture and production parser/capture mappings.

The only synthetic injection is private test code replacing the collector with
fixed, declared observations and locator mappings. Captures still traverse the
real sanitization, raw hashing, immutable store and evidence verification code.
There is no added runtime environment flag, arbitrary executor, path hook, or
production injection API. Synthetic normalizations do not test the correctness
of every live source parser. Timeout, truncation and incomplete-index cases model
their downstream normalized outcomes, not live endpoint behavior.

The corpus fixes seed 601016 and clock 2026-10-16T09:00:00Z; the annual reload has
an explicitly recorded next-day clock. Mutable source code or rules during a run
cause `engine_changed_during_evaluation`. Report engine hashes cover all Python
runtime files in scripts and both MCP entrypoints, not tests or documentation.

## Assertions and denominators

Every final asserted fact must refer to existing known claims with recursively
supported dependencies and the exact field paths of its frozen signal. Frozen
recording bytes/mappings independently bind each available leaf's value, locator,
source hash, scope/completeness, transformation, source/origin, annual period and
recorded times. Explicit `raw_unavailable` / `unmapped_locator` remain explicit;
they never require invented raw evidence. Existing synthetic atomic labels also
check the selected derived conclusions; this is not an independent real oracle.
Expiry is calculated independently from the frozen policy's actual dated source
basis; annual sources with no TTL must display `None`. Retrieval/adaptation time
cannot supply publication, effective date or invented historical expiry. Declined
manual observations retain source URL, declared day, actual import time and their
explicit unavailable status. Unmapped/derived observations cannot acquire invented
source dates or arbitrary scope text.

The actual dossier model is checked at block/field/list/table-row level. Each
required transitive claim has exactly one matching row containing its own entire
trace; an ID or trace pasted into another appendix cannot rescue a blank cell.
The appendix now also exposes actual completeness, transformation, source/origin,
retrieval/observation/effective/publication/expiry times and freshness policy.
Known fact wording/value must match frozen signal templates and bound values;
unknown statements keep explicit status and finite source/rule diagnostics. A
well-formed reason enum still needs the matching input condition and raw support.
Omission/duplication of a fact is a gate failure even if IDs remain elsewhere.
Recommendation and abstention wording have frozen templates. Additional prose,
unknown block keys and invented table headings fail closed. An independent
Markdown line parser checks the complete ordered surface against the semantically
validated model; no production renderer is called to generate an expected answer.

The finite language covers the declared synthetic corpus. Fixed procedural and
legal explanations are allowlisted wording, not newly verified legal propositions.
A newly introduced free-text feature needs an explicit contract revision before
this gate accepts it. Source tables, identity cells and financial amount cells
have typed, source-bound projections; administrative metadata receives independent
version/mode/clock and run-format checks. Text-bearing financial diagnostics and
numeric flag tuples use independent arithmetic over the frozen source operands.
This finite diagnostic specification does not validate real source parsers or
constitute a separate population risk model. Missing required risk must not become green. Next checks must
map to an available official action with subject/run binding, or explicitly explain
unavailability.

Every metric contains numerator, denominator, value (null for no observations)
and unit. `unsupported_assertions` counts assertion occurrences at declared
boundaries: asserted assessment facts, assessment recommendation sentences, and
model list items, field/value assertions, table rows, and dynamic decision/gap
paragraphs. An unrecognized added paragraph, list entry, heading, header, field,
row cell or unexpected key contributes its emitted sentences. Invalid serialized
assessment/financial prose also enters the inventory; copying it to model and
Markdown cannot make it a new permission. Unmatched Markdown atoms are counted. `output_assertions` gives the inventory and
`rendered_assertions` its exact size. Repetition across assessment/model boundaries
is intentional; this is not a count of distinct real-world facts. Static explanatory
boilerplate, recognized structural headings, input/schema checks and dependency-edge
checks do not add successful assertions to dilute the denominator. They still fail the gate on a
contract violation. Omitted expected content fails the gate without being counted
as an emitted successful assertion. Quality metrics include only emitted outputs with the explicit
synthetic-specification oracle. Expected raw loss is counted as a pipeline error
and excluded from those quality denominators, while its fail-closed behavior is
checked on all seven final channel operations. Unexpected errors fail the gate.
Missing oracle, raw loss, no representative sample and unsupported unit conversion
have separate exclusion records. `evaluated_cases` counts pipeline attempts,
not successful factual decisions.

Retained carriers bind all four internal CLI projections to the explicit envelope
origins listed in the typed contract. Both shapes are validated. An exactly bound
internal view is counted at its CLI origin once, so two rejected sentences yield
2/2 for one CLI emission and 4/4 for separate CLI and MCP emissions. This is stable
across fresh aliases, detached saved values and JSON roundtrips.
Well-typed differing projections use the same independent prose oracle in their
own channel context. Location/kind/sentence occurrence counts prevent an internal
canonical/CLI duplicate, while separate MCP emissions remain separate. JSON tags
and non-emitted Markdown ordering diagnostics never become assertion units.

Coverage and abstention are compared with the current public legacy resolver on
identical materialized evidence, financial input and rules. This is a matched-input
compatibility comparison, not a comparison with historical measured-svetofor or a
claim of improvement over an earlier engine. Existing calibration outputs remain
separate. Latency measures local fixture execution including persistence and MCP
process startup. Requests count recorded source responses; manual steps count
manual import operations. None estimates real registry latency or human effort.

The dynamic text suppliers are inventoried explicitly:

| Output surface | Independent permission |
|---|---|
| Profile question, formulation, manual list | Frozen profile metadata |
| Fact description, source/date, value | Frozen signal catalogue, source dates and matching verified leaves |
| Fact partial reason/caveat, unknown/not-applicable wording | Matching source diagnostic/rule condition; exact frozen caveat only for that empty source |
| Financial reason, evaluations, unverified items, summary, flags and explanations | Finite diagnostic specification over frozen annual rows, capital and staff |
| Raised signal prose and recommendation | Frozen severity policy, supported fact IDs and frozen descriptions/action wording |
| Decision coverage, gap reasons/IDs, conflicts | Closed rule/claim schemas, applicable required facts and actual source-bound support |
| Source tables and manual decline | Frozen source observations and the declared manual operation |
| Evidence appendix | Exact local row plus frozen raw mapping, values, scope, origin, transformation, all dates and freshness |
| Title, headings, headers, unknown keys and other prose | Closed structural grammar or source-bound text projections; unexpected sentences count as unsupported |

No returned manual instruction, caveat, financial reason, source diagnostic,
conflict description or raised description supplies its own acceptance oracle.

## Fault and invariance checks

Six forms are represented: full, simplified, IP, bank, NPO and restricted.
Positive, scoped negative, unknown, current/repaid debt, bankruptcy roles,
conflict, mixed years, missing required risk, manual decline, incomplete pages
and index, and raw loss have explicit cases. Removing a necessary leaf cannot
increase supported coverage; red may legitimately become unknown. Equal mirrored
observations do not add independent origin families; reversing their order must
preserve the semantic decision. Annual retrieval does not change reporting year.
The unit test exercises the actually supported RUB capital to thousand-RUB scorer
conversion and equivalent numeric presentation. Arbitrary accounting-unit
conversion is explicitly unsupported with denominator zero, not silently passed.

Runtime mutations change unknown signal statuses to absent and remove the final
dossier evidence appendix. Both must cause the final-output gate to fail for the
specific mutated field or missing evidence. `unittest.mock.patch` restores the
original functions even when assertions raise. Each failure carries a case ID,
case hash, fixed seed/clock, expected atomic claims and reproduction expression;
full immutable inputs remain in the hash-bound manifest.

The independent closure adapters also inject unsupported two-sentence prose into
the serialized assessment manual list or a fact caveat and copy it consistently
to both real transport renderings. Both must fail with parity preserved; adapters
restore their callables and an unmutated baseline must still pass.

## Running verification

```sh
python3 eval/run_decision_gate_eval.py
python3 eval/run_workflow_e2e_eval.py
python3 eval/run_knowledge_invariants_eval.py
uv run --no-project --python /opt/homebrew/bin/python3.12 --with mcp==1.30.0 python eval/run_workflow_e2e_eval.py
```

The base interpreter may skip only the optional SDK stdio test; actual MCP worker
JSON is always exercised. A synthetic target corpus cannot close E-ORACLE,
E-FORMS, E-HUMAN or E-OUTCOME. Independent labels, representative sampling,
real-source form acceptance and prospective outcomes remain separate experiments.

Current acceptance status is recorded in the hash-bound implementation report;
a generated report or a zero-error metric with an empty denominator is never
itself acceptance.

Revision-3 adversarial regressions also mutate actual serialized CLI and MCP
render outputs identically: clearing all trace cells and appending two unsupported
sentences must fail with channel parity preserved. Pure gate regressions cover
contradicted fact wording, numeric values, extra prose outside the decision
section, missing/duplicate facts, wrong raw/scope/period/dependency, pasted trace,
unknown model keys/headers, independently corrupted completeness/time/origin and
Markdown-only additions. Every runtime adapter restores its original callable;
full29 acceptance remains a separate frozen run.
