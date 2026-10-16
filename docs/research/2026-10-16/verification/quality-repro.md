# Quality / packaging review evidence

Repository: `work/inn-check-ru`, commit `3e06046`.
All experiments used temporary directories or process-local monkeypatches. No source edits or external writes.

## HIGH: documented manual installation omits required data

`README.md:136` tells the user to copy only `SKILL.md`, `references/`, and `scripts/`.

Reproduction: copied exactly these three paths into a fresh `TemporaryDirectory`; invoked `python scripts/profiles.py --list` there. Exit 1, JSON status `не проверено`, reason `файл профилей не читается: …/data/profiles_ru.json`. The same invocation from the complete checkout exits 0 and includes profile `отсрочка`.

The data directory also supplies `canon_ru.json`, `benchmarks_ru.json`, and a Rospatent intermediate certificate. ZIP and wheel configuration do include data; the manual instructions do not. Fix the installation instruction and run an isolated installation smoke test against the documented copied paths.

## MEDIUM: concurrency test confuses recycled identities with shared openers

`eval/run_fetch_eval.py:548-550` stores only `id(opener)` and thread IDs. Lines 599-604 compare these across the entire collect. Each `_run_wave` creates a separate executor (`scripts/fetch_counterparty.py:3266-3269`), and openers live in `threading.local` (`3237-3242`). A released opener's numeric ID can be recycled for a new opener in a later wave.

Focused evidence: loaded `fetch_counterparty.py` and `eval/run_fetch_eval.py` using `importlib.util`, replaced only `fc._make_opener` with a function returning a fresh `object()` on every call, and executed `case_parallel(fc)` 20 times. 9 cases falsely reported `opener(ы) … шарятся между потоками`. Repeated with the exact same construction but retaining every fresh object in a list until the end: 0/20 failures. No production opener or threading logic was changed. Real urllib openers passed 50 local repetitions both with and without retention, so the exact CI allocator event was not reproduced with that class.

The parent's retrieved latest GitHub CI has the same failure diagnostic. This is evidence of an invalid identity-based assertion; it is not evidence that the production CookieJar is shared.

Remedy: retain real opener references throughout the assertion; track ownership by the live objects, and use a barrier / active-call count if simultaneous execution needs verification.

## MEDIUM: canary gate labels an expected empty cache as parser failure

`scripts/check_access.py:473-483` calls raw fetchers and treats `data is None` as failure without inspecting `_av[состояние]`. Empty cache correctly returns `(None, {состояние: не проверено, причина: кэш: …})`.

Reproduction: in a fresh temporary HOME, load the real `fetch_counterparty` and `check_access` modules, then run:

```python
report, rc = ca.run_canaries(fc, {
    'реестры_цб': fc.SOURCES['реестры_цб']
})
```

No external requests are needed. Result: `rc == 1`, `результат == провал`, reason `fetcher вернул не dict (NoneType)` with embedded `кэш: индекса нет … сначала cbr_registries.py --refresh`.

The fresh Ubuntu job in `.github/workflows/canary.yml:19-32` neither restores nor builds indices, so this outcome is deterministic for cache-only registries. Retrieved `canary-github.log` confirms the same failures for ERKNM, FNS dumps, disqualification, CBR, trademarks, FTS, RKN, RAR, FSA, Minpromtorg, UC and RZN. Network/TLS/deadline failures also occur but are separate from this reproducible design issue.

Remedy: classify unavailable cache/network as `не запускалась`, fail only established parser/contract violations, and make cache-backed parser canaries a separate job with prepared fixture/index inputs or an explicit refresh phase.

## Strengths inspected

- Pure-stdlib engine and optional MCP dependency keep the CLI installation lean.
- Wheel and ZIP builders ship the canonical data instead of duplicating thresholds in transport wrappers.
- Wheel smoke test runs outside checkout and validates bundled resources and imports.
- Broad fixture/schema-drift, offline-network guard, snapshot and evidence-pack tests target concrete failure modes.
- Source state separates `ok`, empty result, and unavailable data.
- Release version and wrapper dependency parity are gated; native-host Windows protocol has a separate CI job.

No separate performance defect was established with sufficient confidence during this review. Large source files are a maintainability observation, not an additional defect finding.
