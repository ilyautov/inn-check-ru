# Prospective outcome study, protocol v1

This is software instrumentation and a preregistration template, not an executed study.
As of 2026-10-16, new decision-grade real labels, independent real reviewers, matured
held-out outcomes and observed events are all **0**. E-ORACLE and E-OUTCOME remain
`awaiting_external_evidence` in `docs/research/2026-10-16/experiment-status.json`.
The synthetic tests neither recruit reviewers nor create consent, elapsed time or real
outcomes. The existing financial calibration is a different retrospective experiment.

## Registration before outcome access

Save an immutable JSON document conforming to [outcome-cohort.schema.json](outcome-cohort.schema.json)
with `protocol` and `rows`. Register its protocol hash and timestamp outside this tool
before collecting/reading outcome labels. The evaluator reports a canonical SHA-256
of protocol JSON (sorted keys, compact separators, UTF-8, non-ASCII retained); it checks
consistency, not the authenticity of registration, identities or supplied source bytes.
The sampling-frame manifest must bind roster, assignment, selection method, eligibility,
form/size/sector/no-reporting strata and actual inclusion probabilities. Its digest alone
cannot establish a representative sample. Retain all registered test subjects, including
nonresponders; do not repair the roster after viewing outcomes.

Required protocol fields (all fixed in the preregistration except the scheduled analysis
and the subsequently recorded outcome-access time, which must follow the freeze):

| Field | Meaning |
|---|---|
| `schema_version` | 1 |
| `study_id` | Unique study/registration identifier |
| `mode` | `real` or `synthetic`; mixing is invalid, and every row has an explicit synthetic boolean |
| `frozen_at` | Timestamp with timezone of externally registered engine/rubric/sampling/method freeze |
| `engine_hash` | SHA-256 of the pinned engine manifest, matched by every row |
| `rules_hash` | SHA-256 of the pinned rules manifest, matched by every row |
| `rubric_hash` | SHA-256 of the reference rubric, matched by every completed reference |
| `analysis_at` | Scheduled analysis timestamp; observation/review timestamps cannot exceed it |
| `outcomes_read_at` | First study outcome access; cannot precede freeze or any held-out prediction |
| `horizon_days` | Positive fixed integer number of 24-hour days after each prediction |
| `sampling_frame` | Description, frame_hash, method, independent_groups declaration; supported inference method is simple_random |
| `target_ci_width` | Preregistered maximum full width of each two-sided interval, in (0,1] |
| `min_events` | Positive event requirement, applied separately to each metric as below |
| `confidence_method` | `wilson_95`; fixed two-sided nominal 95%, no tunable method search |
| `attrition_policy` | `count_and_block_inference` |
| `weights_policy` | `unit_only`; nonunit rows remain in descriptive counts but block inference |
| `adjudication_policy` | `two_independent_blinded_then_third`, fixed before outcomes are read |
| `strata` | Exactly form, size, sector, no_reporting; report each test-roster distribution |
| `split` | Frozen train_subjects/train_groups/test_subjects/test_groups, each unique; no subject or economic group may span train and test |

`target_ci_width` and `min_events` are design inputs chosen by the study owner with a
statistician before outcome access. This template supplies no real study values, sample
size, claimed power or sufficient-event guarantee. Values in tests are intentionally
loose synthetic fixtures. A failed precision/event gate is not permission to tune a
threshold, change the metric, or repeatedly sample until an interval passes. If the
registered analysis is inconclusive, report that and register a new independent study.
Intervals here are per-metric, not simultaneous across four outcomes and two conditionals.

A row records `inn`, `split`, `group_id`, `synthetic`, `decision_at`,
`data_available_at` (latest availability of **all** prediction inputs), `green`, frozen
engine/rules hashes, `weight`, `strata`, `followup_status` and `outcomes`. A test prediction
must follow freeze and precede first outcome access; input availability must be no later
than that prediction. A report-year is not evidence of availability. Rows from training
are retained only for split/provenance inventory, never in estimates; this evaluator does
not fit, tune or invoke a scorer. Actual prediction provenance is audited independently.
Group membership must cover known economically related entities, not merely exact INNs.

## Four distinct outcomes and reference review

Each row has all four keys; there is no combined adverse-event union:

| Key | Event definition and required reference |
|---|---|
| `case_filed` | A filed bankruptcy case concerning this subject in the debtor role; primary filing/registration evidence with its actual date, not a year inferred from an unrelated case number |
| `procedure_opened` | Primary court act introducing the preregistered bankruptcy procedure for this debtor; a filing alone is insufficient |
| `liquidation` | Recorded legal liquidation of this entity; primary register entry/date, reason and status under the fixed rubric; distinguish liquidation from reorganisation or merely a pending notice |
| `client_loss` | The consenting client's documented realised loss on the preregistered transaction/exposure definition; evidence of amount/date and attribution under the rubric. This target population is exposure-bearing clients, not every registry entity |

Define jurisdiction, event variants/exclusions, client exposure and loss recognition
thresholds in the hashed rubric before recruitment. Unsupported/nonapplicable outcome
ascertainment is `missing_oracle`, never a negative. One sampling frame may be unsuitable
for all four targets; report that and use a separately registered study. A legal filing,
procedure, liquidation and economic loss are not interchangeable labels.

Outcome fields: `status=event|no_event|censored|missing_oracle`, `event_at` (only events),
`horizon_end`, `observed_until`, `reference`. Horizon equals decision_at + horizon_days.
An event must be strictly after prediction and at/before horizon and observation cutoff.
A no_event needs observation through the entire horizon; `survived` is not an accepted
label. The observation cutoff may precede horizon for an already documented event, but
its horizon still must mature by analysis before it enters these fixed-horizon counts.
This avoids admitting early positives while future negatives are still pending.
No survival model or correction for informative censoring is implemented.

[reference-review.schema.json](reference-review.schema.json) binds each completed label
to `subject_inn`, `outcome_type`, `rubric_hash` and a critical fact. Store permitted-use
statements, specific evidence URI/hash/scope and observation time. Two different reviewers
must independently label the primary documents while blinded to prediction. Record their
identities, labels and timestamps. Disagreement requires a third independent adjudicator,
a rationale, the final label and a timestamp after both reviews. A model's own conclusion
is not an independent oracle. Completed no_event references require `negative_complete`
and scope_start/scope_end covering the entire prediction-to-horizon interval. Empty
search results without evidenced coverage do not satisfy this requirement. This tool
checks declarations and temporal consistency; real humans must audit primary documents,
negative completeness, source licensing, reviewer independence and loss definitions.

## Counts, denominators and uncertainty

For **each** outcome, use only retained, mature test rows with a valid independent oracle.
Count g = green, e = event, and ge = green AND event among those eligible rows.

- `p_outcome_given_green`: label `P(outcome_type|green)`, numerator ge, denominator g.
  This estimates event frequency among green predictions under the registered assumptions.
  The event requirement applies to ge (events within this denominator).
- `p_green_given_outcome`: label `P(green|outcome_type)`, numerator ge, denominator e.
  This describes the green share among event cases. Its event requirement applies to e.
  It is not the event probability given green and not sensitivity for detecting events.

For independent binomial observations, the Wilson interval uses the sample proportion
and a normal-score inversion; here z=1.959963984540054 and n is the respective denominator.
See the primary formula in [NIST, 7.2.4.1](https://www.itl.nist.gov/div898/handbook/prc/section2/prc241.htm).
This is a nominal approximate interval, not an exact small-event guarantee. This
implementation assumes simple random selection, unit weights and independent groups.
It does not supply survey-weighted, clustered, case-control or censoring-adjusted CIs.
Nonunit weights, repeated/dependent groups, unsupported sampling, missing roster members,
attrition or any outcome exclusions therefore block population estimates and intervals.
Counts and `descriptive_rate` remain explicitly unweighted complete-case summaries;
they may be biased and cannot be transferred to the sampling frame.

An empty denominator, inadequate relevant events or excessive interval width yields
`insufficient_evidence`, `estimate:null`, `ci95:null`, with reasons. `descriptive_rate`
may be 0 on an observed finite denominator, but never supplies a zero-risk inference.
No observations yield a null descriptive rate as well. Metric status is per outcome;
the top-level status is sufficient only when every reported conditional passes. A
synthetic report always carries `evidence_kind:synthetic` even if its artificial values
satisfy numerical gates. Sufficient-under-assumptions is not empirical acceptance.

## Attrition and exclusions

Report all input, train and test row counts, the registered test-subject denominator,
missing registered subjects, lost and withdrawn counts, nonunit/unit weight counts,
and each observed test stratum distribution. Missing members remain visible even if
absent from rows. Record their reason in the external enrollment log; a missing reason
cannot be assumed to be random loss.

Each outcome's exclusion counts partition provided test rows in this order: attrition,
immature_horizon, censored, missing_oracle. A row contributes at most one exclusion per
outcome; other underlying deficiencies remain in the retained source record. Eligible
plus excluded equals provided test rows, not the larger registered denominator.
Missing registered members are counted separately. Validation errors (identity, schema,
chronology, split, reference contradictions) stop evaluation as `invalid_cohort` rather
than silently discarding problematic rows. No network access or personal data export is
performed by either API.

## Reproducible mechanism check

Run `/opt/homebrew/bin/python3.12 eval/run_outcome_protocol_eval.py` from the repository.
The stdlib-only APIs are `validate_cohort(rows, protocol)` and
`evaluate_outcomes(rows, protocol)`. The former also enforces the semantic rules that
JSON Schema alone does not capture. Schema-required protocol fields are checked against
this document by the synthetic eval. The eval deliberately tests denominator asymmetry,
Wilson numerical values, censoring, late data, split overlap, oracle absence, disagreements,
synthetic/real separation and null estimates under unsupported assumptions.
