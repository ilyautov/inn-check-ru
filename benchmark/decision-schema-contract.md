# Typed final-output contract, revision 2

Rubric revision 6 binds `decision_schema.py` separately by SHA-256 in the corpus
manifest. The corpus, source records, six segment labels, expected atomic claims
and review/adjudication labels are unchanged. Declarations come from the DTOs and
constructors in `dossier`, `evidence_contract`, `profiles`, `fin_scoring`,
`run_workflow`, `run_store`, `snapshot` and `diff_counterparty`, together with the
already frozen input/rule vocabulary. No saved output supplies a type or key.

All objects are closed. All required fields below must exist; optional fields are
explicitly marked `?`. Arrays and maps have declared minimum/maximum cardinality;
the default is 0..10000. Arrays contain exactly their declared item type and pairs
have exactly two string items. Tables have at least one header cell and every
row has exactly the header width; a one-column table and an empty set of rows
remain structurally valid. The generic object relation fixes row cardinality,
never a row's item type, from the explicitly declared sibling header. No `Any`, arbitrary JSON, coercive boolean, numeric
boolean, open object, unknown union branch or unknown schema kind is accepted.
The only opaque JSON is an exact, recursively type-sensitive quote of the matching
frozen source input or mapping. Such quotes reject changed keys, types and values.

`bool` means `type(value) is bool`. Integers exclude booleans. Counters are
0..2^53-1, revisions 1..2^53-1, years 1900..9998, heading levels 1..6. Numbers are
finite within ±1e308; money capital/staff/count metadata additionally requires
nonnegative values. UTC instants and calendar dates must parse and match their
declared spelling. IDs, hashes and pointers have explicit formats. Enumerations
are type-sensitive and use the frozen catalogue, not inspected output values.

The recursive inventory uses the following named closed objects. Every field in
an accepted boundary is either listed here or an exact frozen input quote.

| Object | Complete field inventory |
|---|---|
| Envelope | schema_version=1; status enum; run_id nullable UUID; revision nullable positive int; data nullable operation DTO; error nullable Error; warnings Warning[]; pending_actions Action[] |
| Error | code=corrupt_store; message exact missing-raw diagnostic; retryable=false. Other error shapes fail this finite benchmark explicitly. |
| Warning | code=interrupted or collection_failed; matching exact constructor message. Unmaterialized-capture recovery is outside this frozen corpus and is explicitly rejected. |
| Read/start/manual data | fetch Fetch; fin Fin; assessment Assessment; metadata Metadata |
| Render data | format=json or markdown (bound to operation); content Model or string; source_revision positive int |
| Snapshot artifact data | artifact_id UUID; format=snapshot; sha256 hash; source_revision positive int |
| Metadata | schema_version=1; engine_version version format; rules_version hash; evaluated_at UTC instant; decision Decision; profile catalogue enum; mode=полный; workflow_status enum; raw_refs RawRefs; manual_outcomes? source→completed/declined/incomplete; warnings? Warning[]; operation? Operation |
| Operation | kind manual/refresh; request_sha256 hash |
| Action | run_id UUID; revision positive int; kind enum; source_id source enum; reason_code frozen reason enum; query {инн INN}; required_fields frozen field-name enum[]; expires_at nullable instant; action_id hash. Exactly one availability variant: available=true, url HTTPS URL, unavailable_reason=null; or available=false, url=null, unavailable_reason=official_url_unavailable. |
| Fetch | Exactly the frozen input keys and values, plus _evidence Evidence and _decision Decision. Declined manual import replaces only its source block, availability and summary with independent constructor projections and adds _evidence_scope. |
| Declined manual source | инн; ввод=браузер; дата_проверки; url; проверил=пользователь; параметры_поиска; статус exact declined diagnostic; outcome=declined; expires_at=null; scope_declared=true. All text comes from the frozen manual input. |
| Manual scope entry | relation=manual_observation; search_scope nullable Scope; coverage_complete bool; imported_at instant; observation_ref BlockRef |
| RawRefs | responses response-ID→Response, minimum cardinality from the number of frozen input recordings (at least one for a declared raw-loss operation); fields frozen-field→Mapping[]. An input declaring zero captures and no raw-loss operation still permits an empty response map. |
| Response | source_id source enum; subject_inn INN; request {subject_inn INN}; sha256 hash; retrieved_at instant; first_observed_at instant; capture_format sanitized_json_v1/withheld_non_json_v1; original_response_preserved bool |
| Mapping | response_id response ID; every other key/value exactly the matching frozen mapping. Possible frozen keys: locator, subject_locator, transformation, source_id, search_scope, coverage_complete, candidate, relation, upstream_dataset, period, effective_at, published_at, first_observed_at, date_locators. Unknown keys fail. |
| Evidence | schema_version=1; claims claim-ID→Claim; conflicts Conflict[]; applicability Applicability; rules_version hash; source_states exact independently normalized frozen source states |
| Claim | claim_id; subject_inn; field catalogue/annual-field enum; value nullable derived bool or exact matching frozen source value; unit enum; knowledge enum; reason_code nullable enum; source_id enum; origin_family nullable enum; upstream_dataset nullable frozen mapping enum; relation enum; raw_ref RawRef; retrieved_at; published_at nullable; first_observed_at nullable; effective_at nullable; period nullable Period; valid_until nullable; freshness_policy enum; transformation enum; depends_on claim-ID[] (max 32); search_scope nullable Scope; coverage_complete nullable bool |
| RawRef | response_id nullable response ID; sha256 nullable hash; locator nullable JSON pointer; availability available/raw_unavailable/unmapped_locator |
| Period | start calendar date; end calendar date |
| Scope | subject_inn INN; query nullable exact frozen query; manual_observation? ManualObservation |
| ManualObservation | block_ref BlockRef; source_url HTTPS URL; observed_on date; date_precision=day; imported_at instant; expires_at nullable instant; freshness unknown/explicit_expiry |
| BlockRef | run_id UUID; revision positive int; source_id enum; locator pointer; block_sha256 hash |
| Conflict | field enum; period nullable Period; candidate_claim_ids IDs; selected_claim_id nullable ID; reason_code=conflicting_values |
| Applicability | subject_type legal_entity/ip/unknown; reporting_form nullable full/simplified/ip/bank/npo/restricted; form_version exact frozen source value; form_evidence_ids IDs; source_coverage source→{state known/unknown, reason_code null/coverage_incomplete}; rules field→{status applicable/not_applicable/unknown, reason_code nullable enum} |
| Decision | schema_version=1; rules_version hash; decision_status supported/facts_only/insufficient_evidence; can_recommend bool; missing_evidence Gap[]; next_checks NextCheck[]; claim_ids IDs; coverage Coverage |
| Gap | rule_id signal enum; reason_codes enum[]; claim_ids IDs |
| NextCheck | rule_id signal enum; reason_codes enum[] |
| Coverage | numerator count; denominator count; supported_rules signal[]; required_rules signal[] |
| Assessment | _decision Decision; профиль enum; вопрос nullable string; формулировка факты/вердикт; светофор null/green/yellow/red; поднят_сигналами Raised[]; рекомендация nullable string; факты Fact[]; не_проверено_deal_killer signal[]; не_проверено_обязательные_сигналы signal[]; проверка_состоялась bool; источники_обязательные_не_проверены source[]; вручную string[]; итог_fetch? string; подсказка? exact frozen profile hint only when declared |
| Fact | сигнал enum; статус found/absent/unknown/not-applicable enum; текст string; источник nullable frozen source-name enum; дата nullable frozen date enum; claim_ids IDs; evidence Trace[]; неполно? string; оговорка? string |
| Trace | claim_id; field; knowledge; reason_code nullable; raw_ref RawRef; period nullable Period; search_scope nullable Scope; coverage_complete nullable bool |
| Raised | сигнал enum; severity enum; описание frozen description enum; источник nullable source-name enum; дата nullable date enum; значение exact quoted source value or FinancialValue |
| FinancialValue | показатель string; значение finite number; порог string; год year or year–year format |
| Fin unverified | _input_identity Identity; статус=не проверено; причина string; источник=ГИР БО |
| Fin evaluated | _input_identity Identity; статус=ок; флаги Flag[]; версия_оценок=1; оценки_сигналов exactly seven finite financial rule IDs→Evaluation; финансовый_профиль FinancialProfile |
| Identity | schema_version=1; sha256 hash; scorer_sha256 hash; rules_sha256 hash |
| Evaluation | статус found/absent/unknown; зависимости dependency[]; причина nullable string; достаточные_зависимости dependency[]. A dependency is the declared capital/staff operand or a known balance-code:year. |
| Flag | id financial enum; severity yellow/red; год year or year–year; значение finite number; источник=ГИР БО; показатель string; порог string; пояснение string; строки_баланса Balances |
| FinancialProfile | сводка string; оценено_лет count; года year[]; уставный_капитал_тыс_руб nullable nonnegative number; численность nullable nonnegative number; численность_год nullable year; численность_источник null/риски/дампы_фнс/вход; не_проверено string[]; строки_баланса Balances |
| Balances | year-key→known balance-code→finite number. Codes: 1300,1600,1200,1500,1250,1400,2110,2400. |
| Model | array of five variants, selected by exact тип enum |
| Heading | тип=заголовок; уровень integer 1..6; текст string |
| Paragraph | тип=абзац; текст string; жирный? bool; курсив? bool |
| List | тип=список; пункты string[] |
| Fields | тип=поля; пары [string,string][] |
| Table | тип=таблица; шапка nonempty string[]; строки string[][], each row exactly as wide as шапка; an empty row set is allowed. |
| Snapshot | _evidence Evidence; _decision Decision; версия_формата=2; инн INN; дата instant; поля SnapshotFields; вердикт SnapshotVerdict; состояния source→ok/empty/unknown; сбор {режим:null,профиль:null}; хеш_остального sha256-prefixed hash; _итог {проверка_состоялась:bool,не_проверено:count}; не_покрыты source[] |
| SnapshotFields | статус; дата_прекращения; руководитель; адрес; недостоверность_сведений; налоговая_задолженность; мсп_статус; мсп_категория; банкротство; список_цб exact frozen source projections; спецреестры exact source-status projection; финансы_годы {год year,чистые_активы nullable number,выручка nullable number}[]; ликвидация bool; год_отчётности nullable year; чистые_активы nullable number; выручка nullable number |
| SnapshotVerdict | _evidence Evidence; _decision Decision; optional frozen source blocks containing only exact input leaves; финансы? {форма_отчётности? exact source value,версия_формы? exact source value,отчётность_по_годам? {год year,строки balance-code→number}[]}; manual source? exact declared manual projection |

The internal evaluation carrier additionally has run_id, case_clock, elapsed_ms,
recorded_requests, manual_steps, error_code=null, payload, model, markdown,
snapshot, actions and exactly four boolean parity entries. `serialized?` retains
all seven real final envelopes. Legacy saved probes omit this optional carrier
field; they do not become schema definitions.

When `serialized` is retained, four canonical CLI views have exact origins:
`payload` is `read.data`, `model` is `model.data.content`, `markdown` is
`markdown.data.content`, and `actions` is `read.pending_actions`. Both forms are
schema-validated and require exact value/type equality. An exactly bound internal
projection contributes no additional emitted units; rejected strings belong to
its real CLI origin. A broken binding fails independently, and rejected prose on
either differing shape remains in the inventory. This uses exact top-level keys,
so unknown keys containing dotted path-like text remain fully inventoried. Equal
text in unrelated paths or independent CLI/MCP emissions is never deduplicated.
Fresh aliases, detached saved carriers and JSON roundtrips have the same declared
boundary inventory; legacy canonical-only probes explicitly omit `serialized`.

Well-typed differing views also pass through the existing independent prose
oracle in their coherent CLI/MCP payload/model/Markdown/action context. Canonical
and CLI common occurrences are subtracted only by their declared location, kind
and sentence with multiplicity; separate MCP emissions and different locations
remain separate. Structural JSON tags are not assertion text. Missing/reordered
Markdown still fails, but a diagnostic placeholder is never an emitted sentence.

Shape validation precedes semantic permission construction and any truthiness or
formatting. Invalid shapes return structured `output_schema_invalid` failures.
Their rejected string occurrences, including nested object keys, contribute
unsupported sentence units to both counts; schema successes contribute zero.
Unsafe semantic traversal is skipped after an invalid shape, so no successes are
claimed for the remainder of that malformed output. Existing semantic input/raw,
date/expiry, dependency, independent text and ordered Markdown checks remain.
Boolean outcome and decision-status semantics also remain independently bound.
Cross-channel discrepancies fail parity and their changed serialized strings
enter the inventory. This is a finite mechanism contract, not empirical accuracy.

Revision 2 closes the structural traversal class found during independent review:
empty/short table rows, capture-map selection and nullable action URLs are checked
by explicit declarations before any index, iterator selection or string method.
The finite contract's other indexed sequences were traced: field pairs already
have exact tuple width; first-row/first-candidate accesses have explicit nonempty
guards; financial/source sequences are frozen input. Empty models, claim maps
and trace row sets remain safe to traverse and fail semantic completeness when
the input requires content. These checks do not create a new text permission or
turn a transport exception into a quality-metric exclusion. Revision-5 rubric
and revision-1 schema hashes remain recorded in the manifest history.
