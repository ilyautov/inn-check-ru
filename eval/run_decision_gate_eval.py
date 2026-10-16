#!/usr/bin/env python3
"""Focused adversarial checks of serialized final-output acceptance (stdlib only)."""
import copy
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from benchmark import decision_bench as bench
from benchmark import decision_schema as schema

SAVED = None
if '--saved-output' in sys.argv:
    i = sys.argv.index('--saved-output')
    SAVED = Path(sys.argv[i + 1])
    del sys.argv[i:i + 2]


class DecisionGateEval(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.case = bench.load_cases()[0]
        if SAVED:
            cls.output = json.loads(SAVED.read_text())
        else:
            with tempfile.TemporaryDirectory() as directory:
                cls.output = bench.run_case(cls.case, now=bench.CLOCK, root=Path(directory) / 'runs')

    def check_rejected(self, output):
        row = bench._summarize(self.case, output)
        self.assertTrue(row['failures'], 'mutation accepted despite unchanged parity and IDs')
        self.assertGreater(row['unsupported'] + row['contradicted'], 0)
        return row

    def test_intact_baseline(self):
        self.assertEqual(bench._summarize(self.case, self.output)['failures'], [])

    def test_non_text_scalar_fields_reject_prose_and_coercions(self):
        prose = 'Все долги погашены. Отсрочку можно не ограничивать.'
        for surface in ('bold', 'assessment_boolean', 'heading_level', 'coverage_integer'):
            for value in (prose, 'false', 'true', 0, 1, None, [], {}, float('nan'), float('inf')):
                if surface in ('heading_level', 'coverage_integer') and type(value) is int:
                    continue
                with self.subTest(surface=surface, value=value):
                    out = copy.deepcopy(self.output)
                    if surface == 'bold':
                        next(b for b in out['model'] if b['тип'] == 'абзац')['жирный'] = value
                    elif surface == 'assessment_boolean':
                        out['payload']['assessment']['проверка_состоялась'] = value
                    elif surface == 'heading_level':
                        next(b for b in out['model'] if b['тип'] == 'заголовок')['уровень'] = value
                    else:
                        out['payload']['metadata']['decision']['coverage']['numerator'] = value
                    row = bench._summarize(self.case, out)
                    self.assertTrue(any(f['code'] == 'output_schema_invalid' for f in row['failures']))
                    if value == prose:
                        self.assertGreaterEqual(row['unsupported'], 2)

    def test_closed_recursive_containers_fail_without_exception(self):
        prose = 'Все долги погашены. Отсрочку можно не ограничивать.'
        for path in (('payload', 'assessment', 'факты', 0, 'evidence'),
                     ('payload', 'metadata', 'raw_refs', 'responses'),
                     ('payload', 'fetch', '_evidence', 'claims'),
                     ('actions',), ('model',)):
            with self.subTest(path=path):
                out = copy.deepcopy(self.output)
                node = out
                for key in path[:-1]: node = node[key]
                node[path[-1]] = [[{'unexpected': prose}]]
                row = bench._summarize(self.case, out)
                self.assertTrue(any(f['code'] == 'output_schema_invalid' for f in row['failures']))
                self.assertGreaterEqual(row['unsupported'], 2)

    def test_declarative_validator_field_class_mutation_campaign(self):
        # Traverse every declared branch, not merely the fields occupied by one
        # baseline. The specimen comes from declarations; outputs never define
        # an accepted type, enum, object key or numeric range.
        declarations = bench._typed_contract(self.case)
        pending, seen, classes, checked = list(declarations.values()), set(), set(), 0
        candidates = ['false', 'true', 'Новый факт. Другая рекомендация.', True, False,
                      0, 1, -1, None, [], {}, [[{'Новый факт.': 'Другая рекомендация.'}]],
                      float('nan'), float('inf'), -float('inf'), 10 ** 1000]
        while pending:
            spec = pending.pop()
            if type(spec) is not dict or 'kind' not in spec:
                if type(spec) is dict: pending.extend(spec.values())
                continue
            if id(spec) in seen: continue
            seen.add(id(spec))
            kind = spec['kind']
            classes.add(kind)
            if kind == 'object':
                pending.extend(spec['required'].values())
                pending.extend(spec['optional'].values())
                # Every closed object rejects a novel key, even if its other
                # required fields are absent. Missing fields add zero units.
                atoms, failures = schema.audit({'Новый факт.': 'Другая рекомендация.'}, spec)
                self.assertTrue(failures)
                self.assertGreaterEqual(len(atoms), 2)
                checked += 1
            elif kind == 'map': pending.extend((spec['key'], spec['value']))
            elif kind == 'array': pending.append(spec['item'])
            elif kind == 'tuple': pending.extend(spec['items'])
            elif kind == 'union': pending.extend(spec['options'])
            elif kind == 'tagged': pending.extend(spec['variants'].values())
            # For every scalar declaration, determine validity independently by
            # its explicit type/range/enum (without the generic validator).
            if kind not in ('boolean', 'integer', 'number', 'string', 'enum', 'exact'):
                continue
            for value in candidates:
                if kind == 'boolean': allowed = type(value) is bool
                elif kind in ('integer', 'number'):
                    allowed = (type(value) is int if kind == 'integer' else type(value) in (int, float))
                    allowed = allowed and spec['minimum'] <= value <= spec['maximum']
                elif kind == 'enum': allowed = any(type(value) is type(v) and value == v for v in spec['values'])
                elif kind == 'exact':
                    allowed = type(value) is type(spec['value']) and value == spec['value']
                else:
                    allowed = type(value) is str and len(value) <= spec.get('maximum', 2000000)
                    allowed = allowed and ('pattern' not in spec or re.fullmatch(spec['pattern'], value) is not None)
                    allowed = allowed and 'format' not in spec
                issues = schema.validate(value, spec)
                self.assertEqual(not issues, allowed, (kind, spec, type(value)))
                checked += 1
        self.assertEqual(classes, {'boolean', 'integer', 'number', 'string', 'enum', 'exact',
                                  'object', 'map', 'array', 'tuple', 'union', 'tagged'})
        self.assertGreater(checked, 1000)

    def test_recursive_invalid_strings_include_keys_without_schema_padding(self):
        prose = 'Новый факт. Другая рекомендация.'
        for invalid in (prose, [[prose]], {'Новый факт.': {'x': [prose]}}):
            atoms, violations = schema.audit(invalid, schema.BOOL)
            self.assertTrue(violations)
            self.assertGreaterEqual(len(atoms), 2)
            self.assertTrue(all(not a['supported'] for a in atoms))
        self.assertEqual(schema.audit(True, schema.BOOL), ([], []))
        self.assertEqual(schema.audit(3, schema.COUNT), ([], []))

    def test_all_declared_boundaries_reject_nested_prose(self):
        declarations = bench._typed_contract(self.case)
        boundaries = {k: v for k, v in declarations.items() if k != 'envelopes'}
        boundaries.update({'envelope.' + k: v for k, v in declarations['envelopes'].items()})
        for name, contract in boundaries.items():
            with self.subTest(boundary=name):
                atoms, failures = schema.audit([[{'Новый факт.': 'Другая рекомендация.'}]], contract)
                self.assertTrue(failures)
                self.assertGreaterEqual(len(atoms), 2)
        atoms, failures = schema.audit({}, schema.obj({'required_boolean': schema.BOOL}))
        self.assertTrue(failures)
        self.assertEqual(atoms, [])

    def test_numeric_bounds_nullable_and_real_scalar_types(self):
        for contract, values in ((schema.YEAR, (1900, 2026, 9998)), (schema.COUNT, (0, 1, 9007199254740991)),
                                 (schema.NUMBER, (-1.5, 0, 2.4)), (schema.BOOL, (False, True))):
            for value in values:
                self.assertEqual(schema.audit(value, contract), ([], []))
        self.assertEqual(schema.audit(None, schema.nullable(schema.BOOL)), ([], []))
        for contract, value in ((schema.YEAR, 1899), (schema.YEAR, 9999), (schema.COUNT, -1),
                                (schema.COUNT, 9007199254740992), (schema.COUNT, True), (schema.NUMBER, True),
                                (schema.BOOL, 0), (schema.BOOL, 1), (schema.BOOL, None)):
            self.assertTrue(schema.validate(value, contract))

    def test_status_and_boolean_semantics_remain_independently_bound(self):
        for surface in ('assessment', 'decision'):
            out = copy.deepcopy(self.output)
            if surface == 'assessment':
                value = out['payload']['assessment']
                value['проверка_состоялась'] = not value['проверка_состоялась']
            else:
                decision = out['payload']['metadata']['decision']
                decision['decision_status'] = 'facts_only'
                out['payload']['assessment']['_decision'] = copy.deepcopy(decision)
                out['payload']['fetch']['_decision'] = copy.deepcopy(decision)
            self.assertTrue(bench._summarize(self.case, out)['failures'])

    def test_every_decisive_trace_carries_completeness_transform_and_times(self):
        rows = next(b['строки'] for b in self.output['model'] if b.get('шапка', [])[:1] == ['Claim ID'])
        claims = self.output['payload']['fetch']['_evidence']['claims']
        for cid, _, _, trace in rows:
            for label, field in [('полнота', 'coverage_complete'), ('преобразование', 'transformation'),
                                 ('источник', 'source_id'), ('семейство', 'origin_family'),
                                 ('получено', 'retrieved_at'), ('наблюдалось', 'first_observed_at'),
                                 ('действует', 'effective_at'), ('опубликовано', 'published_at'),
                                 ('актуально до', 'valid_until'), ('политика', 'freshness_policy')]:
                self.assertIn('; %s: %s' % (label, claims[cid][field]), trace)

    def test_blank_trace_cells_with_ids_preserved(self):
        out = copy.deepcopy(self.output)
        for block in out['model']:
            if block.get('шапка', [])[:1] == ['Claim ID']:
                for row in block['строки']:
                    row[3] = ''
        out['markdown'] = re.sub(r'Период: [^\n]*?; зависимости: [^|\n]*', '', out['markdown'])
        self.check_rejected(out)

    def test_extra_factual_and_recommendation_paragraph(self):
        out = copy.deepcopy(self.output)
        text = 'У компании нет налоговой задолженности ни за один год. Рекомендуем неограниченную отсрочку.'
        out['model'].append({'тип': 'абзац', 'текст': text})
        out['markdown'] += '\n' + text + '\n'
        row = self.check_rejected(out)
        self.assertGreater(row.get('rendered_assertions', 0),
                           bench._summarize(self.case, self.output).get('rendered_assertions', 0))

    def test_arbitrary_new_wording_not_a_blacklist(self):
        out = copy.deepcopy(self.output)
        text = 'Все обязательства исполнены полностью, поставщик доказанно платёжеспособен.'
        out['model'].insert(8, {'тип': 'абзац', 'текст': text})
        out['markdown'] += '\n' + text + '\n'
        self.check_rejected(out)

    def test_contradicted_fact_text_retains_ids(self):
        out = copy.deepcopy(self.output)
        fact = next(f for f in out['payload']['assessment']['факты'] if f['сигнал'] == 'массовый_руководитель')
        old = fact['текст']
        new = old.replace(': не выявлено', ': выявлено')
        for block in out['model']:
            if block['тип'] == 'список':
                block['пункты'] = [s.replace(old, new) for s in block['пункты']]
        out['markdown'] = out['markdown'].replace(old, new)
        self.check_rejected(out)

    def test_wrong_rendered_numeric_value(self):
        out = copy.deepcopy(self.output)
        for block in out['model']:
            if block.get('шапка', [])[:1] == ['Год']:
                block['строки'][0][4] = '999 000 000 руб.'
        out['markdown'] = out['markdown'].replace('40\xa0000\xa0000 руб.', '999 000 000 руб.')
        self.check_rejected(out)

    def test_same_wrong_fact_in_assessment_model_and_text(self):
        out = copy.deepcopy(self.output)
        old = next(f for f in out['payload']['assessment']['факты'] if f['сигнал'] == 'массовый_руководитель')['текст']
        new = old.replace(': не выявлено', ': выявлено')
        out = json.loads(json.dumps(out, ensure_ascii=False).replace(old, new))
        self.check_rejected(out)

    def test_trace_transplant_cannot_hide_blank_row(self):
        out = copy.deepcopy(self.output)
        block = next(b for b in out['model'] if b.get('шапка', [])[:1] == ['Claim ID'])
        trace = block['строки'][0][3]
        block['строки'][0][3] = ''
        out['model'].append({'тип': 'абзац', 'текст': trace})
        self.check_rejected(out)

    def test_wrong_locator_scope_period_and_dependency_individually(self):
        for token in ('raw:', 'область поиска:', 'Период:', 'зависимости:'):
            with self.subTest(token=token):
                out = copy.deepcopy(self.output)
                block = next(b for b in out['model'] if b.get('шапка', [])[:1] == ['Claim ID'])
                original = block['строки'][0][3]
                block['строки'][0][3] = original.replace(token, token + ' ALTERED ')
                out['markdown'] = out['markdown'].replace(original, block['строки'][0][3])
                self.check_rejected(out)

    def test_missing_or_duplicate_fact_cannot_hide_behind_appendix_ids(self):
        for duplicate in (False, True):
            with self.subTest(duplicate=duplicate):
                out = copy.deepcopy(self.output)
                block = next(b for b in out['model'] if b['тип'] == 'список' and any('руководитель числится' in s for s in b['пункты']))
                text = next(s for s in block['пункты'] if 'руководитель числится' in s)
                if duplicate:
                    block['пункты'].append(text)
                    out['markdown'] += '- ' + text + '\n'
                else:
                    block['пункты'].remove(text)
                    out['markdown'] = out['markdown'].replace('- ' + text + '\n', '')
                self.assertTrue(bench._summarize(self.case, out)['failures'])

    def test_unrecognized_table_heading_is_not_a_free_prose_channel(self):
        out = copy.deepcopy(self.output)
        block = next(b for b in out['model'] if b.get('шапка', [])[:1] == ['Год'])
        old = block['шапка'][1]
        block['шапка'][1] = 'Все финансовые риски отсутствуют'
        out['markdown'] = out['markdown'].replace(old, block['шапка'][1])
        self.check_rejected(out)

    def test_unknown_extra_model_key_is_rejected(self):
        out = copy.deepcopy(self.output)
        out['model'][0]['new_claim'] = 'Все риски отсутствуют.'
        self.check_rejected(out)

    def test_source_period_time_and_origin_are_independent_of_output_claim(self):
        for field, value in [('period', {'start': '2020-01-01', 'end': '2020-12-31'}),
                             ('effective_at', '2020-01-01T00:00:00Z'),
                             ('origin_family', 'invented_independent_registry')]:
            with self.subTest(field=field):
                out = copy.deepcopy(self.output)
                claim = next(c for c in out['payload']['fetch']['_evidence']['claims'].values()
                             if c['field'] == 'финансы.отчётность_по_годам.2025.строки.1300')
                claim[field] = value
                _, failures = bench._presentation_audit(self.case, out)
                self.assertTrue(any(f['code'] == 'frozen_leaf_binding_mismatch' and f['detail'] == claim['claim_id'] for f in failures))

    def test_frozen_source_binds_completeness_even_when_graph_and_rows_agree(self):
        out = copy.deepcopy(self.output)
        claim = next(c for c in out['payload']['fetch']['_evidence']['claims'].values() if c['coverage_complete'] is True)
        claim['coverage_complete'] = False
        for block in out['model']:
            if block.get('шапка', [])[:1] == ['Claim ID']:
                for row in block['строки']:
                    if row[0] == claim['claim_id']:
                        row[3] = row[3].replace('; полнота: True', '; полнота: False')
        assertions, failures = bench._presentation_audit(self.case, out)
        self.assertTrue(any(f['code'] == 'frozen_leaf_binding_mismatch' for f in failures))

    def test_manual_instructions_and_fact_caveats_cannot_supply_the_oracle(self):
        for text in ('У компании нет налоговой задолженности ни за один год. Рекомендуем неограниченную отсрочку.',
                     'Все обязательства исполнены полностью. Поставщик доказанно платёжеспособен.'):
            for surface in ('manual', 'caveat'):
                with self.subTest(surface=surface, text=text):
                    out = copy.deepcopy(self.output)
                    if surface == 'manual':
                        out['payload']['assessment']['вручную'].append(text)
                        out['model'].append({'тип': 'список', 'пункты': [text]})
                        out['markdown'] += '\n- ' + text + '\n'
                    else:
                        fact = next(f for f in out['payload']['assessment']['факты'] if f['сигнал'] == 'массовый_руководитель')
                        fact['оговорка'] = text
                        old, new = fact['текст'], fact['текст'] + ' (оговорка источника: ' + text + ')'
                        for block in out['model']:
                            if block['тип'] == 'список':
                                block['пункты'] = [new if value == old else value for value in block['пункты']]
                        out['markdown'] = out['markdown'].replace(old, new)
                    row = self.check_rejected(out)
                    self.assertGreaterEqual(row['unsupported'], 2)

    def test_heading_assertions_enter_both_metric_counts(self):
        baseline = bench._summarize(self.case, self.output)
        out = copy.deepcopy(self.output)
        text = 'Все долги погашены. Отсрочку можно не ограничивать.'
        out['model'].append({'тип': 'заголовок', 'уровень': 2, 'текст': text})
        out['markdown'] += '\n## ' + text + '\n'
        row = self.check_rejected(out)
        self.assertEqual(row['unsupported'] - baseline['unsupported'], 2)
        self.assertEqual(row['rendered_assertions'] - baseline['rendered_assertions'], 2)

    def test_assertions_on_every_model_text_surface_enter_inventory(self):
        text = 'Все долги погашены. Платёжеспособность гарантирована.'
        baseline = bench._summarize(self.case, self.output)
        for surface in ('paragraph', 'list', 'heading', 'field_label', 'field_value',
                        'table_header', 'table_cell', 'extra_key'):
            with self.subTest(surface=surface):
                out = copy.deepcopy(self.output)
                if surface in ('paragraph', 'heading'):
                    typ = 'абзац' if surface == 'paragraph' else 'заголовок'
                    block = {'тип': typ, 'текст': text}
                    if surface == 'heading': block['уровень'] = 2
                    out['model'].append(block)
                    out['markdown'] += '\n' + ('## ' if surface == 'heading' else '') + text + '\n'
                elif surface == 'list':
                    out['model'].append({'тип': 'список', 'пункты': [text]})
                    out['markdown'] += '\n- ' + text + '\n'
                elif surface == 'extra_key':
                    out['model'][0]['свободный_текст'] = text
                else:
                    if surface.startswith('field'):
                        block = next(b for b in out['model'] if b['тип'] == 'поля')
                        index = 0 if surface == 'field_label' else 1
                        old = block['пары'][0][index]
                        block['пары'][0][index] = text
                    else:
                        block = next(b for b in out['model'] if b.get('шапка', [])[:1] == ['Год'])
                        holder = block['шапка'] if surface == 'table_header' else block['строки'][0]
                        old = holder[1]; holder[1] = text
                    out['markdown'] = out['markdown'].replace(old, text)
                row = self.check_rejected(out)
                self.assertGreaterEqual(row['unsupported'] - baseline['unsupported'], 2)

    def test_finite_reason_code_still_needs_actual_source_support(self):
        out = copy.deepcopy(self.output)
        claim = next(c for c in out['payload']['fetch']['_evidence']['claims'].values()
                     if c['field'] == 'финансы.отчётность_по_годам.2025.строки.1300')
        claim['knowledge'], claim['reason_code'] = 'unknown', 'stale'
        # A well-formed enum is insufficient: annual sources have no TTL.
        _, failures = bench._presentation_audit(self.case, out)
        self.assertTrue(any(f['code'] == 'frozen_leaf_knowledge_mismatch' and f['detail'] == claim['claim_id'] for f in failures))

    def test_frozen_conditional_wording_still_needs_its_input_condition(self):
        out = copy.deepcopy(self.output)
        text = 'Во входном файле нет блока «_доступность»: состояния источников неизвестны. Ни одна строка таблицы выше не означает, что источник был опрошен и что-то ответил.'
        out['model'].append({'тип': 'абзац', 'текст': text})
        out['markdown'] += '\n' + text + '\n'
        self.check_rejected(out)

    def test_financial_and_profile_diagnostics_are_not_output_derived_permissions(self):
        text = 'Все обязательства исполнены. Платёжеспособность гарантирована.'
        for surface in ('fin_reason', 'fin_unverified', 'fin_evaluation_reason', 'profile_question',
                        'required_sources', 'gap_reason', 'claim_reason', 'unknown_assessment_key'):
            with self.subTest(surface=surface):
                out = copy.deepcopy(self.output)
                assessment, fin = out['payload']['assessment'], out['payload']['fin']
                if surface == 'fin_reason':
                    fin.update(статус='не проверено', причина=text)
                    value = 'финансовый разбор: не проверено — ' + text
                elif surface == 'fin_unverified':
                    fin['финансовый_профиль']['не_проверено'].append(text)
                    value = 'финансовый разбор: ' + text
                elif surface == 'fin_evaluation_reason':
                    fin['оценки_сигналов']['коэффициент_автономии']['причина'] = text
                    fact = next(f for f in assessment['факты'] if f['сигнал'] == 'коэффициент_автономии')
                    fact['статус'] = 'не проверен'
                    fact['текст'] = 'собственного капитала меньше 10% от активов (1300/1600 < 0.1): не проверено (' + text + ')'
                    value = fact['текст']
                elif surface == 'profile_question':
                    old = assessment['вопрос']; assessment['вопрос'] = text
                    out = json.loads(json.dumps(out, ensure_ascii=False).replace(old, text))
                    self.check_rejected(out)
                    continue
                elif surface == 'required_sources':
                    assessment['источники_обязательные_не_проверены'].append(text)
                    value = 'обязательные источники профиля без данных: ' + text
                elif surface == 'gap_reason':
                    gap = {'rule_id': 'массовый_руководитель', 'claim_ids': [], 'reason_codes': [text]}
                    for decision in (out['payload']['metadata']['decision'], assessment['_decision'], out['payload']['fetch']['_decision']):
                        decision['missing_evidence'] = [gap]
                    value = 'Пробел массовый_руководитель: ' + text + '; '
                    out['model'].append({'тип': 'абзац', 'текст': value})
                    out['markdown'] += '\n' + value + '\n'
                    self.check_rejected(out)
                    continue
                elif surface == 'claim_reason':
                    claim = next(c for c in out['payload']['fetch']['_evidence']['claims'].values() if c['knowledge'] == 'unknown')
                    old = claim['reason_code']; claim['reason_code'] = text
                    for block in out['model']:
                        if block.get('шапка', [])[:1] == ['Claim ID']:
                            for row in block['строки']:
                                if row[0] == claim['claim_id']:
                                    row[2] = 'unknown:' + text
                    out['markdown'] = out['markdown'].replace('unknown:' + str(old), 'unknown:' + text)
                    self.check_rejected(out)
                    continue
                else:
                    assessment['новое_заключение'] = text
                    value = text
                out['model'].append({'тип': 'список', 'пункты': [value]})
                out['markdown'] += '\n- ' + value + '\n'
                self.check_rejected(out)

    def test_expiry_is_bound_to_frozen_source_policy_even_with_matching_trace(self):
        out = copy.deepcopy(self.output)
        claim = next(c for c in out['payload']['fetch']['_evidence']['claims'].values()
                     if c['field'] == 'финансы.отчётность_по_годам.2025.строки.1300')
        self.assertIsNone(claim['valid_until'])
        claim['valid_until'] = '2099-01-01T00:00:00Z'
        block = next(b for b in out['model'] if b.get('шапка', [])[:1] == ['Claim ID'])
        row = next(r for r in block['строки'] if r[0] == claim['claim_id'])
        old = row[3]; row[3] = old.replace('; актуально до: None', '; актуально до: ' + claim['valid_until'])
        out['markdown'] = out['markdown'].replace(old, row[3])
        self.check_rejected(out)

    def test_markdown_only_extra_claim(self):
        out = copy.deepcopy(self.output)
        out['markdown'] += '\nНалоговые проверки гарантированно не выявят нарушений.\n'
        self.check_rejected(out)


    def test_table_and_pair_structural_boundaries(self):
        for table_kind in ('trace', 'ordinary'):
            for mutation in ('empty_row', 'short_row', 'long_row', 'empty_header', 'short_header'):
                with self.subTest(table_kind=table_kind, mutation=mutation):
                    out = copy.deepcopy(self.output)
                    block = next(b for b in out['model'] if b['тип'] == 'таблица' and
                                 (b['шапка'][:1] == ['Claim ID']) == (table_kind == 'trace') and b['строки'])
                    if mutation == 'empty_row': block['строки'][0] = []
                    elif mutation == 'short_row': block['строки'][0] = block['строки'][0][:-1]
                    elif mutation == 'long_row': block['строки'][0].append('Extra sentence.')
                    elif mutation == 'empty_header': block['шапка'] = []
                    else: block['шапка'] = block['шапка'][:-1]
                    self.assertTrue(schema.validate(out, bench._typed_contract(self.case)['output']))
                    row = bench._summarize(self.case, out)
                    self.assertTrue(any(f['code'] == 'output_schema_invalid' for f in row['failures']))
                    self.assertEqual(row['asserted'], 0)
        for pair in ([], ['one'], ['one', 'two', 'three'], [None, 'two']):
            with self.subTest(pair=pair):
                out = copy.deepcopy(self.output)
                next(b for b in out['model'] if b['тип'] == 'поля')['пары'][0] = pair
                self.assertTrue(any(f['code'] == 'output_schema_invalid' for f in bench._summarize(self.case, out)['failures']))
        for block in ({'тип': 'таблица', 'шапка': ['Column'], 'строки': [['Cell']]},
                      {'тип': 'таблица', 'шапка': ['Column'], 'строки': []},
                      {'тип': 'таблица', 'шапка': ['A', 'B'], 'строки': [['1', '2']]}):
            self.assertEqual(schema.audit([block], schema.MODEL), ([], []))

    def test_empty_semantic_collections_fail_without_traversal_errors(self):
        for surface in ('empty_model', 'short_model', 'empty_claims', 'empty_trace_rows'):
            with self.subTest(surface=surface):
                out = copy.deepcopy(self.output)
                if surface == 'empty_model': out['model'] = []
                elif surface == 'short_model': out['model'] = out['model'][:1]
                elif surface == 'empty_claims': out['payload']['fetch']['_evidence']['claims'] = {}
                else:
                    next(b for b in out['model'] if b.get('шапка', [])[:1] == ['Claim ID'])['строки'] = []
                self.assertTrue(bench._summarize(self.case, out)['failures'])
        self.assertEqual(schema.audit([], schema.array(schema.TEXT)), ([], []))

    def test_generic_container_bounds_survive_nullable_unions(self):
        bounded_array = schema.scalar('array', item=schema.TEXT, minimum=1, maximum=2)
        bounded_map = schema.scalar('map', key=schema.TEXT, value=schema.BOOL, minimum=1, maximum=2)
        for contract, accepted, rejected in ((bounded_array, (['one'], ['one', 'two']), ([], ['one', 'two', 'three'])),
                                              (bounded_map, ({'one': True}, {'one': True, 'two': False}), ({}, {'one': True, 'two': False, 'three': True}))):
            for value in accepted:
                self.assertEqual(schema.audit(value, schema.nullable(contract)), ([], []))
            self.assertEqual(schema.audit(None, schema.nullable(contract)), ([], []))
            for value in rejected:
                self.assertTrue(schema.validate(value, schema.nullable(contract)))
        pair = schema.scalar('tuple', items=[schema.TEXT, schema.TEXT])
        for value in ([], ['one'], ['one', 'two', 'three']): self.assertTrue(schema.validate(value, schema.nullable(pair)))
        self.assertEqual(schema.audit(['one', 'two'], pair), ([], []))

    def test_response_map_cardinality_comes_from_frozen_capture_input(self):
        for case in bench.load_cases():
            with self.subTest(case=case['id']):
                contract = bench._typed_contract(case)['envelopes']['start']
                payload = contract['required']['data']['options'][1]
                responses = payload['required']['metadata']['required']['raw_refs']['required']['responses']
                self.assertEqual(responses.get('minimum', 0), len(case['recordings']))
                self.assertTrue(schema.validate({}, responses))
        no_capture = copy.deepcopy(self.case)
        no_capture['recordings'] = []
        contract = bench._typed_contract(no_capture)['envelopes']['start']
        responses = contract['required']['data']['options'][1]['required']['metadata']['required']['raw_refs']['required']['responses']
        self.assertEqual(schema.audit({}, responses), ([], []))

    def test_action_availability_and_url_form_one_declared_variant(self):
        action_contract = bench._typed_contract(self.case)['output']['required']['actions']['item']
        for available, url, reason in ((True, None, None), (True, 'https://fixture.invalid/action', 'official_url_unavailable'),
                                       (False, 'https://fixture.invalid/action', 'official_url_unavailable'), (False, None, None)):
            with self.subTest(available=available, url=url, reason=reason):
                out = copy.deepcopy(self.output)
                out['actions'][0].update(available=available, url=url, unavailable_reason=reason)
                self.assertTrue(schema.validate(out, bench._typed_contract(self.case)['output']))
                self.assertTrue(any(f['code'] == 'output_schema_invalid' for f in bench._summarize(self.case, out)['failures']))
        for available, url, reason in ((True, 'https://fixture.invalid/action', None), (False, None, 'official_url_unavailable')):
            action = copy.deepcopy(self.output['actions'][0])
            action.update(available=available, url=url, unavailable_reason=reason)
            self.assertEqual(schema.audit(action, action_contract), ([], []))

    def projection_carrier(self):
        """A real-shaped carrier for pure probes, including legacy saved input."""
        out = copy.deepcopy(self.output)
        if 'serialized' not in out:
            revision = out['actions'][0]['revision'] if out['actions'] else 1
            def envelope(data):
                return {'schema_version': 1, 'status': 'ok', 'run_id': out['run_id'],
                    'revision': revision, 'error': None, 'warnings': [],
                    'pending_actions': copy.deepcopy(out['actions']), 'data': data}
            stages = {
                'read': envelope(copy.deepcopy(out['payload'])),
                'model': envelope({'format': 'json', 'content': copy.deepcopy(out['model']), 'source_revision': revision}),
                'markdown': envelope({'format': 'markdown', 'content': out['markdown'], 'source_revision': revision}),
                'snapshot': envelope({'artifact_id': out['run_id'], 'format': 'snapshot',
                    'sha256': bench._hash(out['snapshot']), 'source_revision': revision})}
            stages.update({right: copy.deepcopy(stages[left]) for left, right in
                (('read', 'mcp_read'), ('model', 'mcp_model'), ('markdown', 'mcp_text'))})
            out['serialized'] = stages
        # Exactly the four public CLI views projected by run_case. This emulates
        # the fresh alias relationship without relying on object identity in the gate.
        out['payload'] = out['serialized']['read']['data']
        out['model'] = out['serialized']['model']['data']['content']
        out['markdown'] = out['serialized']['markdown']['data']['content']
        out['actions'] = out['serialized']['read']['pending_actions']
        return out

    def test_cli_projection_inventory_is_stable_across_carrier_representations(self):
        for representation in ('fresh_alias', 'saved_detached', 'json_roundtrip'):
            out = self.projection_carrier()
            block = next(b for b in out['model'] if b.get('шапка', [])[:1] == ['Claim ID'])
            block['строки'][0] = ['First unsupported sentence. Second unsupported sentence.']
            if representation == 'saved_detached':
                for key in ('payload', 'model', 'markdown', 'actions'):
                    out[key] = copy.deepcopy(out[key])
            elif representation == 'json_roundtrip':
                out = json.loads(json.dumps(out))
            with self.subTest(representation=representation):
                row = bench._summarize(self.case, out)
                self.assertEqual((row['unsupported'], row['rendered_assertions']), (2, 2))
                self.assertTrue(any(f['code'] == 'output_schema_invalid' for f in row['failures']))
                self.assertTrue(all(a['key'].startswith('$.serialized.model.data.content')
                                    for a in row['output_assertions']))

    def test_every_bound_projection_has_one_invalid_string_inventory(self):
        prose = 'First unsupported sentence. Second unsupported sentence.'
        for projection in ('payload', 'model', 'markdown', 'actions'):
            out = self.projection_carrier()
            if projection == 'payload': out['payload']['assessment']['проверка_состоялась'] = prose
            elif projection == 'model':
                next(b for b in out['model'] if b['тип'] == 'абзац')['жирный'] = prose
            elif projection == 'markdown':
                out['markdown'] = out['serialized']['markdown']['data']['content'] = [prose]
            else: out['actions'][0]['available'] = prose
            legacy = copy.deepcopy(out)
            legacy.pop('serialized')
            expected = bench._summarize(self.case, legacy)['unsupported']
            self.assertGreaterEqual(expected, 2)
            for representation in ('fresh_alias', 'json_roundtrip'):
                carrier = out if representation == 'fresh_alias' else json.loads(json.dumps(out))
                with self.subTest(projection=projection, representation=representation):
                    row = bench._summarize(self.case, carrier)
                    self.assertEqual((row['unsupported'], row['rendered_assertions']), (expected, expected))
                    self.assertTrue(any(f['code'] == 'output_schema_invalid' for f in row['failures']))
                    self.assertFalse(any(f['code'] == 'canonical_projection_binding_mismatch' for f in row['failures']))

    def test_all_four_cli_projections_require_exact_binding(self):
        for projection in ('payload', 'model', 'markdown', 'actions'):
            out = self.projection_carrier()
            out[projection] = copy.deepcopy(out[projection])
            if projection == 'payload': out[projection]['metadata']['workflow_status'] = 'cancelled'
            elif projection == 'model': out[projection][0]['текст'] += ' changed'
            elif projection == 'markdown': out[projection] += '\nchanged'
            else: out[projection][0]['query']['инн'] = '7700000000'
            with self.subTest(projection=projection):
                self.assertEqual(schema.audit(out, bench._typed_contract(self.case)['output']), ([], []))
                row = bench._summarize(self.case, out)
                self.assertIn({'code': 'canonical_projection_binding_mismatch', 'detail': projection}, row['failures'])
        for channel in ('read', 'model', 'mcp_read'):
            out = self.projection_carrier()
            out['serialized'][channel].update(status='error', data=None, error={'code': 'corrupt_store',
                'message': 'Записанный исходный ответ отсутствует.', 'retryable': False})
            with self.subTest(nullable_error_channel=channel):
                self.assertEqual(schema.audit(out, bench._typed_contract(self.case)['output']), ([], []))
                row = bench._summarize(self.case, out)
                self.assertTrue(any(f['code'] in ('canonical_projection_binding_mismatch', 'serialized_channel_disagreement')
                                    for f in row['failures']))
                self.assertEqual(row['unsupported'], 0)
                self.assertIsNone(row['error_code'])

    def test_valid_text_projection_differences_inventory_actual_occurrences(self):
        prose = 'First unsupported sentence. Second unsupported sentence.'
        for target, expected in (('canonical', 2), ('cli', 2), ('both_actual', 4),
                                 ('common_location', 3), ('distinct_locations', 4)):
            out = self.projection_carrier()
            paragraph = {'тип': 'абзац', 'текст': prose}
            if target in ('common_location', 'distinct_locations'):
                out['model'].append(copy.deepcopy(paragraph))
            out['model'] = copy.deepcopy(out['model'])
            if target == 'canonical': out['model'].append(copy.deepcopy(paragraph))
            elif target in ('cli', 'both_actual', 'distinct_locations'):
                out['serialized']['model']['data']['content'].append(copy.deepcopy(paragraph))
                if target == 'both_actual':
                    out['serialized']['mcp_model']['data']['content'].append(copy.deepcopy(paragraph))
            else:
                out['serialized']['model']['data']['content'][0]['текст'] += ' changed'
            for representation in ('fresh_alias', 'json_roundtrip'):
                carrier = out if representation == 'fresh_alias' else json.loads(json.dumps(out))
                with self.subTest(target=target, representation=representation):
                    self.assertEqual(schema.audit(carrier, bench._typed_contract(self.case)['output']), ([], []))
                    row = bench._summarize(self.case, carrier)
                    self.assertEqual(row['unsupported'], expected)
                    self.assertTrue(any(f['code'] == 'canonical_projection_binding_mismatch' for f in row['failures']))
                    self.assertFalse(any(a['unit'] == '<order changed>' for a in row['output_assertions']))

    def test_schema_inventories_both_shapes_without_merging_actual_channels(self):
        for target in ('canonical', 'cli', 'bound_cli', 'cli_and_mcp', 'root_key'):
            out = self.projection_carrier()
            if target == 'root_key':
                out['model.First unsupported sentence.'] = 'Second unsupported sentence.'
                models = []
            elif target == 'canonical':
                out['model'] = copy.deepcopy(out['model'])
                models = [out['model']]
            elif target == 'cli':
                out['model'] = copy.deepcopy(out['model'])
                models = [out['serialized']['model']['data']['content']]
            else:
                models = [out['model']]
                if target == 'cli_and_mcp': models.append(out['serialized']['mcp_model']['data']['content'])
            for model in models:
                model[0]['First unsupported sentence.'] = 'Second unsupported sentence.'
            with self.subTest(target=target):
                row = bench._summarize(self.case, out)
                expected = 4 if target == 'cli_and_mcp' else 2
                self.assertEqual((row['unsupported'], row['rendered_assertions']), (expected, expected))
                self.assertTrue(any(f['code'] == 'output_schema_invalid' for f in row['failures']))
                mismatch = any(f['code'] == 'canonical_projection_binding_mismatch' for f in row['failures'])
                self.assertEqual(mismatch, target in ('canonical', 'cli'))


    def test_structural_rejections_count_only_actual_prose(self):
        out = copy.deepcopy(self.output)
        # This probe declares the legacy canonical-only boundary explicitly.
        out.pop('serialized', None)
        block = next(b for b in out['model'] if b.get('шапка', [])[:1] == ['Claim ID'])
        block['строки'][0] = ['First unsupported sentence. Second unsupported sentence.']
        self.assertTrue(schema.validate(out, bench._typed_contract(self.case)['output']))
        with patch.object(bench, 'run_case', return_value=out):
            report = bench.evaluate([self.case], now=bench.CLOCK)
        self.assertEqual(report['metrics']['unsupported_assertions']['numerator'], 2)
        self.assertEqual(report['metrics']['unsupported_assertions']['denominator'], 2)
        self.assertEqual(report['counts']['quality_eligible_cases'], 1)
        self.assertEqual(report['counts']['pipeline_errors'], 0)
        with self.assertRaises(AssertionError): bench.assert_gate(report)

    def test_real_pipeline_structural_mutations_fail_closed_and_restore(self):
        original_cli, original_mcp = bench._cli, bench._mcp
        cases = {c['id']: c for c in bench.load_cases()}
        for shape, case_id in (('empty_trace_row', 'full_complete_negative'), ('prose_trace_row', 'full_complete_negative'),
                               ('empty_capture_map', 'raw_loss'),
                               ('available_null_url', 'required_missing_risk')):
            changed = []
            def mutate(result, channel, operation, shape=shape, changed=changed):
                result = copy.deepcopy(result)
                if shape in ('empty_trace_row', 'prose_trace_row') and operation == 'model':
                    block = next(b for b in result['data']['content'] if b.get('шапка', [])[:1] == ['Claim ID'])
                    block['строки'][0] = ([] if shape == 'empty_trace_row' else
                        ['First unsupported sentence. Second unsupported sentence.'])
                    changed.append(channel)
                elif shape == 'empty_capture_map' and operation == 'start':
                    result['data']['metadata']['raw_refs']['responses'] = {}
                    changed.append(channel)
                elif shape == 'available_null_url' and result['pending_actions']:
                    for action in result['pending_actions']:
                        action.update(available=True, url=None, unavailable_reason=None)
                    changed.append(channel)
                return result
            def cli(root, *arguments):
                operation = 'model' if arguments[0] == 'render' and arguments[-1] == 'json' else arguments[0]
                return mutate(original_cli(root, *arguments), 'cli', operation)
            def mcp(root, name, arguments):
                operation = 'model' if name == 'counterparty_run_render' and arguments.get('format') == 'json' else name
                return mutate(original_mcp(root, name, arguments), 'mcp', operation)
            with self.subTest(shape=shape):
                with patch.object(bench, '_cli', side_effect=cli), patch.object(bench, '_mcp', side_effect=mcp):
                    try:
                        report = bench.evaluate([cases[case_id]], now=bench.CLOCK)
                    except Exception as error:
                        self.fail(shape + ' raised uncontrolled ' + type(error).__name__)
                self.assertEqual(report['counts']['failed_cases'], 1)
                self.assertEqual(report['counts']['pipeline_errors'], 0)
                self.assertEqual(report['counts']['quality_eligible_cases'], 1)
                self.assertTrue(any(f['code'] == 'output_schema_invalid' for f in report['cases'][0]['failures']))
                self.assertTrue(changed)
                if shape in ('empty_trace_row', 'prose_trace_row'): self.assertEqual(changed, ['cli', 'mcp'])
                if shape == 'prose_trace_row':
                    metric = report['metrics']['unsupported_assertions']
                    self.assertEqual((metric['numerator'], metric['denominator']), (4, 4))
                with self.assertRaises(AssertionError): bench.assert_gate(report)
                self.assertIs(bench._cli, original_cli)
                self.assertIs(bench._mcp, original_mcp)

    def test_direct_script_entry_imports_canonical_schema(self):
        # Simulate Python's direct-file sys.path from an unrelated cwd. runpy's
        # non-main name loads the real entry without running the full corpus.
        code = '''import runpy, sys
from pathlib import Path
entry = Path(sys.argv[1])
sys.path.insert(0, str(entry.parent))
loaded = runpy.run_path(str(entry), run_name='decision_entry_import_probe')
assert loaded['decision_schema'] is sys.modules['benchmark.decision_schema']
assert callable(loaded['evaluate'])
'''
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, '-I', '-B', '-c', code, str(ROOT / 'benchmark/decision_bench.py')],
                                    cwd=directory, capture_output=True, text=True, timeout=20, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)


class DecisionOracleBindingEval(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = {case['id']: case for case in bench.load_cases()}
        control = copy.deepcopy(cls.cases['mixed_years'])
        control['id'] = 'small_growth_staff_mismatch_control'
        control['fetch']['финансы']['отчётность_по_годам'][0]['строки']['2110'] = 120000
        recording = next(r for r in control['recordings'] if r['id'] == 'финансы')
        recording['body']['payload']['отчётность_по_годам'][0]['строки']['2110'] = 120000
        next(s for s in control['sources'] if s['recording_id'] == 'финансы')['sha256'] = bench._hash(recording['body'])
        control['input_sha256'] = bench._hash(control['fetch'])
        control['expected_atomic_claims'] = [{'field': 'fin.транзитный_профиль', 'knowledge': 'known', 'value': False}]
        control['expected_can_recommend'] = True
        cls.cases[control['id']] = control
        cls.outputs = {}
        with tempfile.TemporaryDirectory() as directory:
            for case_id in ('mixed_years', 'mirror_duplicate', 'provider_reversal', 'provider_conflict', control['id']):
                cls.outputs[case_id] = bench.run_case(cls.cases[case_id], now=bench.CLOCK,
                    root=Path(directory) / case_id)

    def test_four_frozen_targets_pass_without_label_changes(self):
        for case_id in ('mixed_years', 'mirror_duplicate', 'provider_reversal', 'provider_conflict'):
            with self.subTest(case_id=case_id):
                self.assertEqual(bench._summarize(self.cases[case_id], self.outputs[case_id])['failures'], [])

    def test_revenue_only_false_proof_does_not_require_mismatched_staff(self):
        case_id = 'small_growth_staff_mismatch_control'
        output = self.outputs[case_id]
        self.assertEqual(bench._summarize(self.cases[case_id], output)['failures'], [])
        claims = output['payload']['fetch']['_evidence']['claims']
        transit = next(c for c in claims.values() if c['field'] == 'fin.транзитный_профиль')
        self.assertEqual((transit['knowledge'], transit['reason_code'], transit['value']), ('known', None, False))
        self.assertTrue(transit['depends_on'])
        self.assertTrue(all(claims[cid]['field'].startswith('финансы.') for cid in transit['depends_on']))

    def test_growth_above_threshold_requires_matching_staff_period(self):
        output = self.outputs['mixed_years']
        claims = output['payload']['fetch']['_evidence']['claims']
        transit = next(c for c in claims.values() if c['field'] == 'fin.транзитный_профиль')
        self.assertEqual((transit['knowledge'], transit['reason_code']), ('unknown', 'period_mismatch'))
        self.assertTrue(any(claims[cid]['field'] == 'риски.численность_сотрудников' for cid in transit['depends_on']))
        self.assertFalse(output['payload']['metadata']['decision']['can_recommend'])

    def test_mirror_order_preserves_semantics_and_origin_families(self):
        baseline = bench._summarize(self.cases['small_growth_staff_mismatch_control'],
                                    self.outputs['small_growth_staff_mismatch_control'])
        for case_id in ('mirror_duplicate', 'provider_reversal'):
            with self.subTest(case_id=case_id):
                row = bench._summarize(self.cases[case_id], self.outputs[case_id])
                self.assertEqual(row['semantic_decision'], baseline['semantic_decision'])
                self.assertEqual(row['origin_families'], baseline['origin_families'])

    def test_mapped_mirror_still_respects_explicit_unavailability(self):
        case = copy.deepcopy(self.cases['mirror_duplicate'])
        case['fetch']['_доступность']['агрегатор'] = {'состояние': 'не проверено', 'причина': 'timeout'}
        output = self.outputs['mirror_duplicate']
        claim = next(c for c in output['payload']['fetch']['_evidence']['claims'].values()
                     if c['field'] == 'риски.массовый_руководитель' and c['source_id'] == 'агрегатор')
        _, failures = bench._presentation_audit(case, output)
        self.assertTrue(any(f['code'] == 'frozen_leaf_knowledge_mismatch' and f['detail'] == claim['claim_id'] for f in failures))

    def test_conflict_atom_does_not_permit_unbound_enum_or_prose(self):
        for replacement in ('stale', 'Все долги погашены. Отсрочка безопасна.'):
            with self.subTest(replacement=replacement):
                out = copy.deepcopy(self.outputs['provider_conflict'])
                fact = next(f for f in out['payload']['assessment']['факты'] if f['сигнал'] == 'массовый_руководитель')
                old = fact['текст']
                self.assertTrue(old.endswith('(conflict)'))
                new = old.removesuffix('(conflict)') + '(' + replacement + ')'
                fact['текст'] = new
                for block in out['model']:
                    if block['тип'] == 'список':
                        block['пункты'] = [new if item == old else item for item in block['пункты']]
                out['markdown'] = out['markdown'].replace(old, new)
                row = bench._summarize(self.cases['provider_conflict'], out)
                self.assertTrue(any(f['code'] == 'fact_wording_outside_frozen_oracle' for f in row['failures']))
                self.assertGreater(row['unsupported'], 0)

    def test_actual_same_hash_provider_identity_swap_is_rejected_before_summary(self):
        original_cli, original_mcp = bench._cli, bench._mcp
        def mutate(out):
            if out['status'] == 'error': return out
            payload = out['data']
            refs = payload['metadata']['raw_refs']
            swapped = {}
            for rid, response in refs['responses'].items():
                if response['source_id'] in ('риски', 'агрегатор'):
                    source = 'агрегатор' if response['source_id'] == 'риски' else 'риски'
                    response['source_id'] = source
                    swapped[rid] = source
            for claim in payload['fetch']['_evidence']['claims'].values():
                if claim['raw_ref']['response_id'] in swapped:
                    source = swapped[claim['raw_ref']['response_id']]
                    claim['source_id'] = source
                    claim['relation'] = 'mirror' if source == 'агрегатор' else 'primary'
            return out
        def cli(root, *args):
            out = original_cli(root, *args)
            return mutate(out) if args[0] == 'resume' else out
        def mcp(root, name, args):
            out = original_mcp(root, name, args)
            return mutate(out) if name == 'counterparty_run_read' else out
        with tempfile.TemporaryDirectory() as directory, patch.object(bench, '_cli', side_effect=cli), patch.object(bench, '_mcp', side_effect=mcp):
            output = bench.run_case(self.cases['mirror_duplicate'], now=bench.CLOCK, root=Path(directory) / 'runs')
        self.assertIs(bench._cli, original_cli)
        self.assertIs(bench._mcp, original_mcp)
        self.assertTrue(all(output.get('contract_parity', {}).values()))
        self.assertEqual(set(output.get('contract_parity', {})), {'read', 'model', 'markdown'})
        failures = output.get('contract_failures', [])
        self.assertEqual({f['detail'] for f in failures if f['code'] == 'capture_identity_binding_mismatch'}, {'read', 'mcp_read'})


class ManualCaptureContextEval(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.case = next(c for c in bench.load_cases() if c['id'] == 'manual_declined')

    def test_trusted_manual_transition_removes_only_frozen_source_prefixes(self):
        before = {'responses': {'capture': {'source_id': 'суды'}}, 'fields': {
            'суды.ответчик_крупные': [{'response_id': 'capture'}],
            'риски.массовый_руководитель': [{'response_id': 'other'}]}}
        frozen = copy.deepcopy(before)
        transition = getattr(bench, '_post_manual_capture_context', None)
        self.assertTrue(callable(transition), 'explicit trusted post-manual transition missing')
        after = transition(self.case, before)
        self.assertEqual(after['responses'], before['responses'])
        self.assertEqual(after['fields'], {'риски.массовый_руководитель': [{'response_id': 'other'}]})
        self.assertEqual(before, frozen)
        after['responses']['capture']['source_id'] = 'риски'
        self.assertEqual(before, frozen)

    def test_actual_manual_transition_and_metadata_tampering(self):
        original_cli, original_mcp, original_audit = bench._cli, bench._mcp, bench._envelope_audit
        for mutation in ('intact', 'manual_identity', 'read_foreign_other_field'):
            audits, changed = [], []
            def mutate(out, stage, mutation=mutation, changed=changed):
                if out['status'] == 'error': return out
                if mutation == 'manual_identity' and stage == 'manual':
                    response = next(iter(out['data']['metadata']['raw_refs']['responses'].values()))
                    response['source_id'] = 'финансы' if response['source_id'] != 'финансы' else 'риски'
                    changed.append(stage)
                elif mutation == 'read_foreign_other_field' and stage in ('read', 'mcp_read'):
                    refs = out['data']['metadata']['raw_refs']
                    field = next(f for f in refs['fields'] if f.split('.', 1)[0] not in self.case['manual'])
                    refs['fields'][field][0]['response_id'] = 'response-ffffffff-0000-4000-8000-000000000000'
                    changed.append(stage)
                return out
            def cli(root, *args):
                return mutate(original_cli(root, *args), 'read' if args[0] == 'resume' else args[0])
            def mcp(root, name, args):
                return mutate(original_mcp(root, name, args), 'mcp_read' if name == 'counterparty_run_read' else name)
            def audit(case, stages, audits=audits, **kwargs):
                context = kwargs.get('trusted_refs')
                audits.append((list(stages), context, copy.deepcopy(context)))
                return original_audit(case, stages, **kwargs)
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                with patch.object(bench, '_cli', side_effect=cli), patch.object(bench, '_mcp', side_effect=mcp), \
                     patch.object(bench, '_envelope_audit', side_effect=audit):
                    output = bench.run_case(self.case, now=bench.CLOCK, root=Path(directory) / 'runs')
                self.assertIs(bench._cli, original_cli)
                self.assertIs(bench._mcp, original_mcp)
                self.assertIs(bench._envelope_audit, original_audit)
                failures = output.get('contract_failures', [])
                if mutation == 'intact':
                    self.assertEqual(bench._summarize(self.case, output)['failures'], [])
                    self.assertTrue(all(context == frozen for _, context, frozen in audits))
                    before, after, final = (entry[2] for entry in audits)
                    self.assertEqual(before['responses'], after['responses'])
                    self.assertEqual(set(before['fields']) - set(after['fields']), {'суды.ответчик_крупные'})
                    self.assertEqual(after['fields'], {k: v for k, v in before['fields'].items()
                        if k.split('.', 1)[0] not in self.case['manual']})
                    self.assertEqual(after, final)
                else:
                    expected = {'manual'} if mutation == 'manual_identity' else {'read', 'mcp_read'}
                    self.assertEqual(set(changed), expected)
                    self.assertEqual({f['detail'] for f in failures if f['code'] == 'capture_identity_binding_mismatch'}, expected)
                    self.assertFalse(any(f['code'] == 'output_schema_invalid' for f in failures))
                    if mutation == 'read_foreign_other_field':
                        self.assertTrue(all(output['contract_parity'].values()))


class FrozenResponseBindingUnitEval(unittest.TestCase):
    def setUp(self):
        self.case = next(c for c in bench.load_cases() if c['id'] == 'mirror_duplicate')
        self.field = 'риски.массовый_руководитель'
        self.refs = {'responses': {}, 'fields': {}}
        self.ids = {}
        for i, recording in enumerate(self.case['recordings']):
            rid = 'response-%08x-0000-4000-8000-000000000000' % i
            self.ids[recording['id']] = rid
            self.refs['responses'][rid] = {'source_id': recording['source_id'], 'subject_inn': self.case['fetch']['инн'],
                'request': {'subject_inn': self.case['fetch']['инн']}, 'sha256': bench._hash(recording['body'])}
            for field, mapping in recording['mappings'].items():
                self.refs['fields'].setdefault(field, []).append(dict(copy.deepcopy(mapping), response_id=rid))
        self.resolve = getattr(bench, '_frozen_leaf_binding', None)
        self.assertTrue(callable(self.resolve), 'independent response-bound mapping resolver missing')

    def raw(self, recording_id):
        rid = self.ids[recording_id]
        return {'availability': 'available', 'response_id': rid, 'sha256': self.refs['responses'][rid]['sha256'],
                'locator': '/payload/массовый_руководитель'}

    def test_identical_body_providers_resolve_by_response_and_frozen_mapping(self):
        for order in (self.case['recordings'], list(reversed(self.case['recordings']))):
            case = dict(self.case, recordings=order)
            for recording_id in ('риски', 'mirror'):
                recording, mapping = self.resolve(case, self.refs, self.field, self.raw(recording_id))
                self.assertEqual(recording['id'], recording_id)
                self.assertEqual(mapping, recording['mappings'][self.field])

    def test_foreign_response_source_subject_hash_or_mapping_is_rejected(self):
        for mutation in ('foreign_id', 'source', 'subject', 'request_subject', 'hash', 'mapping', 'locator'):
            with self.subTest(mutation=mutation):
                refs, raw = copy.deepcopy(self.refs), self.raw('mirror')
                response = refs['responses'][raw['response_id']]
                if mutation == 'foreign_id': raw['response_id'] = 'response-ffffffff-0000-4000-8000-000000000000'
                elif mutation == 'source': response['source_id'] = 'финансы'
                elif mutation == 'subject': response['subject_inn'] = '7700000000'
                elif mutation == 'request_subject': response['request']['subject_inn'] = '7700000000'
                elif mutation == 'hash': response['sha256'] = '0' * 64
                elif mutation == 'locator': raw['locator'] = '/payload/массовый_адрес'
                else: refs['fields'][self.field] = [m for m in refs['fields'][self.field] if m['response_id'] != raw['response_id']]
                self.assertIsNone(self.resolve(self.case, refs, self.field, raw)[0])

    def test_duplicate_same_source_recording_is_ambiguous(self):
        case = copy.deepcopy(self.case)
        duplicate = copy.deepcopy(next(r for r in case['recordings'] if r['id'] == 'mirror'))
        duplicate['id'] = 'ambiguous_second_mirror'
        case['recordings'].append(duplicate)
        self.assertIsNone(self.resolve(case, self.refs, self.field, self.raw('mirror'))[0])


if __name__ == '__main__':
    unittest.main(verbosity=2)
