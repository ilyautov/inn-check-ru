#!/usr/bin/env python3
"""Synthetic-only offline contracts for prospective outcome instrumentation."""
import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from benchmark.evaluate_outcomes import evaluate_outcomes, validate_cohort

OUTCOMES = ('case_filed', 'procedure_opened', 'liquidation', 'client_loss')
H = 'a' * 64


def protocol(n=4):
    return {'schema_version': 1, 'study_id': 'synthetic-mechanism', 'mode': 'synthetic',
                'frozen_at': '2026-01-01T00:00:00Z', 'engine_hash': H, 'rules_hash': H,
                'rubric_hash': H, 'analysis_at': '2026-03-01T00:00:00Z',
                'outcomes_read_at': '2026-02-15T00:00:00Z', 'horizon_days': 30,
                'sampling_frame': {'description': 'Synthetic frame, not real companies',
                                    'frame_hash': H, 'method': 'simple_random', 'independent_groups': True},
                'target_ci_width': 0.99, 'min_events': 1, 'confidence_method': 'wilson_95',
                'attrition_policy': 'count_and_block_inference', 'weights_policy': 'unit_only',
                'adjudication_policy': 'two_independent_blinded_then_third',
                'strata': ['form', 'size', 'sector', 'no_reporting'],
                'split': {'train_subjects': ['train'], 'train_groups': ['train-g'],
                           'test_subjects': [str(i) for i in range(n)],
                           'test_groups': [f'g{i}' for i in range(n)]}}


def reference(label, inn, outcome_type):
    return {'schema_version': 1, 'rubric_hash': H, 'critical': True, 'label': label, 'subject_inn': inn, 'outcome_type': outcome_type,
                'evidence': [{'sha256': H, 'uri': 'synthetic://document', 'permitted_use': 'synthetic fixture',
                               'observed_at': '2026-02-02T00:00:00Z', 'scope': 'specific subject and outcome'}],
                'negative_complete': label == 'no_event',
                'scope_start': '2026-01-02T00:00:00Z', 'scope_end': '2026-02-01T00:00:00Z',
                'reviews': [{'reviewer_id': x, 'independent': True, 'blinded_to_prediction': True,
                              'label': label, 'reviewed_at': '2026-02-16T00:00:00Z'} for x in ('r1', 'r2')],
                'adjudication': None}


def row(i=0, green=True, event=False):
    outcomes = {}
    for kind in OUTCOMES:
        status = 'event' if event and kind == 'case_filed' else 'no_event'
        outcomes[kind] = {'status': status, 'event_at': '2026-01-20T00:00:00Z' if status == 'event' else None,
                              'horizon_end': '2026-02-01T00:00:00Z', 'observed_until': '2026-02-02T00:00:00Z',
                              'reference': reference(status, str(i), kind)}
    return {'inn': str(i), 'split': 'test', 'group_id': f'g{i}', 'synthetic': True,
                'decision_at': '2026-01-02T00:00:00Z', 'data_available_at': '2026-01-01T12:00:00Z',
                'green': green, 'engine_hash': H, 'rules_hash': H, 'weight': 1,
                'strata': {'form': 'full', 'size': 'small', 'sector': 'example', 'no_reporting': False},
                'followup_status': 'retained', 'outcomes': outcomes}


class OutcomeProtocolTests(unittest.TestCase):
    def codes(self, rows, p=None):
        return {e['code'] for e in validate_cohort(rows, p or protocol())}

    def test_timestamp_syntax_and_calendar_bounds_return_validation_errors(self):
        invalid = ['2026-01-01T00:00:00+03:99', '20260101T000000Z',
                   '2026-01-01T00:00:00', '2026-01-01T00:00:00+24:00',
                   '2026-01-01T00:00:00-00:60', '2026-01-01T24:00:00Z',
                   '2026-01-01T00:60:00Z', '2026-01-01T00:00:60Z',
                   '2026-02-29T00:00:00Z', '2026-13-01T00:00:00Z',
                   '2026-01-01T00:00:00.1234567Z', '2026-01-01T00:00:00Z\n']
        for timestamp in invalid:
            with self.subTest(timestamp=timestamp):
                p = protocol(1); p['frozen_at'] = timestamp
                errors = validate_cohort([row(event=True)], p)
                self.assertTrue(any(e['code'] == 'schema' and e['path'] == '/protocol/frozen_at'
                                    for e in errors), errors)
                self.assertEqual(evaluate_outcomes([row(event=True)], p)['status'], 'invalid_cohort')

    def test_valid_timestamp_offsets_fraction_and_leap_day(self):
        for timestamp in ['2026-01-01T00:00:00Z', '2026-01-01T03:00:00+03:00',
                          '2025-12-31T19:00:00-05:00', '2026-01-01T00:00:00.000001Z',
                          '2024-02-29T23:59:59.999999+23:59']:
            with self.subTest(timestamp=timestamp):
                p = protocol(1); p['frozen_at'] = timestamp
                self.assertEqual(validate_cohort([row(event=True)], p), [])

    def test_nested_reference_timestamp_uses_same_strict_contract(self):
        r = row(event=True)
        r['outcomes']['case_filed']['reference']['reviews'][0]['reviewed_at'] = '20260216T000000Z'
        # The nullable reference is an anyOf: its error belongs to that boundary.
        self.assertTrue(any(e['code'] == 'schema' and e['path'] == '/rows/0/outcomes/case_filed/reference'
                            for e in validate_cohort([r], protocol(1))))

    def test_empty_is_insufficient(self):
        r = evaluate_outcomes([], protocol())
        self.assertEqual(r['status'], 'insufficient_evidence')
        self.assertIsNone(r['outcomes']['case_filed']['metrics']['p_outcome_given_green']['estimate'])
        self.assertEqual(r['counts']['missing_registered_test_subjects'], 4)

    def test_denominators_and_outcome_types(self):
        rows = [row(0, True, True), row(1, True), row(2, True), row(3, False, True)]
        r = evaluate_outcomes(rows, protocol())
        self.assertEqual(r['evidence_kind'], 'synthetic')
        a, b = (r['outcomes']['case_filed']['metrics'][k] for k in ('p_outcome_given_green', 'p_green_given_outcome'))
        self.assertEqual((a['numerator'], a['denominator']), (1, 3))
        self.assertEqual((b['numerator'], b['denominator']), (1, 2))
        self.assertAlmostEqual(a['estimate'], 1/3)
        self.assertEqual(b['estimate'], .5)
        self.assertIsNone(r['outcomes']['liquidation']['metrics']['p_green_given_outcome']['estimate'])
        self.assertEqual(set(r['outcomes']), set(OUTCOMES))

    def test_wilson_known_interval(self):
        rows = [row(i, True, i < 4) for i in range(20)]
        m = evaluate_outcomes(rows, protocol(20))['outcomes']['case_filed']['metrics']['p_outcome_given_green']
        self.assertAlmostEqual(m['ci95'][0], .08065766258)
        self.assertAlmostEqual(m['ci95'][1], .41601743225)

    def test_precision_and_event_gates(self):
        p = protocol(1); p['target_ci_width'] = .01; p['min_events'] = 2
        m = evaluate_outcomes([row(event=True)], p)['outcomes']['case_filed']['metrics']['p_outcome_given_green']
        self.assertIsNone(m['estimate'])
        self.assertIn('insufficient_events', m['reasons'])
        self.assertIn('target_ci_width_not_met', m['reasons'])

    def test_missing_oracle_is_exclusion(self):
        r = row(event=True); r['outcomes']['case_filed']['reference'] = None
        result = evaluate_outcomes([r], protocol(1))['outcomes']['case_filed']
        self.assertEqual(result['exclusions']['missing_oracle'], 1)
        self.assertEqual(result['eligible'], 0)

    def test_explicit_missing_oracle_status(self):
        r = row(); r['outcomes']['case_filed'].update(status='missing_oracle', reference=None)
        self.assertFalse(validate_cohort([r], protocol(1)))
        self.assertIsNone(evaluate_outcomes([r], protocol(1))['outcomes']['case_filed']['metrics']['p_outcome_given_green']['estimate'])

    def test_immature_positive_also_excluded(self):
        p = protocol(1); p['analysis_at'] = '2026-01-25T00:00:00Z'; p['outcomes_read_at'] = p['analysis_at']
        r = row(event=True)
        for o in r['outcomes'].values():
            o['observed_until'] = p['analysis_at']; o['reference'] = None
            if o['status'] == 'no_event': o['status'] = 'censored'
        result = evaluate_outcomes([r], p)['outcomes']['case_filed']
        self.assertEqual(result['exclusions']['immature_horizon'], 1)
        self.assertEqual(result['eligible'], 0)

    def test_censoring_cannot_be_survival(self):
        r = row(); r['outcomes']['case_filed']['observed_until'] = '2026-01-20T00:00:00Z'
        self.assertIn('censored_as_no_event', self.codes([r]))
        r['outcomes']['case_filed']['status'] = 'survived'
        self.assertIn('schema', self.codes([r]))

    def test_subject_and_group_overlap(self):
        self.assertIn('subject_split_overlap', self.codes([{'inn':'x','split':'train','group_id':'g'}, {'inn':'x','split':'test','group_id':'g'}]))
        self.assertIn('group_split_overlap', self.codes([{'inn':'x','split':'train','group_id':'g'}, {'inn':'y','split':'test','group_id':'g'}]))
        p = protocol(); p['split']['train_groups'] = ['g0']
        self.assertIn('group_split_overlap', self.codes([], p))

    def test_leakage_and_freeze(self):
        for field, value, code in [('data_available_at','2026-01-03T00:00:00Z','leakage_cutoff'), ('decision_at','2025-12-01T00:00:00Z','decision_before_freeze'), ('engine_hash','b'*64,'freeze_hash_mismatch')]:
            with self.subTest(field=field):
                r = row(); r[field] = value
                self.assertIn(code, self.codes([r]))
        p = protocol(); p['outcomes_read_at'] = '2025-01-01T00:00:00Z'
        self.assertIn('outcomes_before_freeze', self.codes([], p))

    def test_synthetic_cannot_enter_real_report(self):
        p = protocol(1); p['mode'] = 'real'
        self.assertIn('synthetic_in_real_report', self.codes([row()], p))
        self.assertEqual(evaluate_outcomes([row()], p)['status'], 'invalid_cohort')

    def test_independent_reviewers_and_adjudication(self):
        r = row(); ref = r['outcomes']['case_filed']['reference']
        ref['reviews'][1]['reviewer_id'] = 'r1'
        self.assertIn('independent_review_required', self.codes([r]))
        ref['reviews'][1]['reviewer_id'] = 'r2'; ref['reviews'][1]['label'] = 'event'
        self.assertIn('adjudication_required', self.codes([r]))
        ref['adjudication'] = {'reviewer_id': 'r3', 'independent': True, 'label': 'no_event', 'rationale': 'Resolved against primary evidence', 'reviewed_at': '2026-02-17T00:00:00Z'}
        self.assertFalse(validate_cohort([r], protocol(1)))
        ref['adjudication']['reviewer_id'] = 'r1'
        self.assertIn('adjudication_required', self.codes([r]))

    def test_negative_scope_is_required(self):
        r = row(); r['outcomes']['case_filed']['reference']['negative_complete'] = False
        self.assertIn('negative_scope_incomplete', self.codes([r]))

    def test_reference_bound_to_subject_and_outcome(self):
        for key, value in [('subject_inn', 'someone-else'), ('outcome_type', 'client_loss')]:
            r = row(); r['outcomes']['case_filed']['reference'][key] = value
            self.assertIn('reference_identity_mismatch', self.codes([r]))

    def test_event_requires_time_within_horizon(self):
        for when in (None, '2026-01-01T00:00:00Z', '2026-02-10T00:00:00Z'):
            r = row(event=True); r['outcomes']['case_filed']['event_at'] = when
            self.assertIn('invalid_event_time', self.codes([r]))

    def test_horizon_is_preregistered(self):
        r = row(); r['outcomes']['case_filed']['horizon_end'] = '2026-01-30T00:00:00Z'
        self.assertIn('horizon_mismatch', self.codes([r]))

    def test_weights_clusters_and_attrition_block_inference(self):
        for reason in ('nonunit_weights', 'dependent_groups', 'attrition'):
            with self.subTest(reason=reason):
                p = protocol(2); rows = [row(0, True, True), row(1, True, True)]
                if reason == 'nonunit_weights': rows[0]['weight'] = 2
                elif reason == 'dependent_groups': rows[1]['group_id'] = 'g0'
                else: rows[1]['followup_status'] = 'lost'
                m = evaluate_outcomes(rows, p)['outcomes']['case_filed']['metrics']['p_outcome_given_green']
                self.assertIsNone(m['estimate']); self.assertIsNone(m['ci95'])
                self.assertIn(reason, m['reasons'])
                self.assertIsNotNone(m['descriptive_rate'])

    def test_missing_subjects_block_inference_and_report_attrition(self):
        r = evaluate_outcomes([row(event=True)], protocol(2))
        self.assertEqual(r['counts']['missing_registered_test_subjects'], 1)
        self.assertIn('missing_registered_subjects', r['outcomes']['case_filed']['metrics']['p_outcome_given_green']['reasons'])

    def test_duplicate_and_unregistered_subjects_rejected(self):
        self.assertIn('duplicate_subject', self.codes([row(), row()]))
        self.assertIn('unregistered_subject', self.codes([row(5)]))

    def test_train_not_evaluated_and_inputs_unchanged(self):
        r = row(); r.update(inn='train', group_id='train-g', split='train')
        rows = [r, row(event=True)]; before = copy.deepcopy(rows)
        result = evaluate_outcomes(rows, protocol(1))
        self.assertEqual(rows, before)
        self.assertEqual(result['counts']['train_rows'], 1)
        self.assertEqual(result['outcomes']['case_filed']['eligible'], 1)

    def test_schema_required_fields_are_enforced(self):
        schema = json.loads((ROOT/'benchmark/outcome-cohort.schema.json').read_text())
        for field in schema['$defs']['protocol']['required']:
            p = protocol(1); p.pop(field)
            with self.subTest(protocol=field): self.assertTrue(validate_cohort([row()], p))
        for field in schema['$defs']['row']['required']:
            r = row(); r.pop(field)
            with self.subTest(row=field): self.assertTrue(validate_cohort([r], protocol(1)))
        doc = (ROOT/'benchmark/outcome-study-protocol.md').read_text()
        for field in schema['$defs']['protocol']['required']:
            self.assertIn(f'`{field}`', doc)

    def test_invalid_types_fail_closed(self):
        for rows, p in [(None, protocol()), ([None], protocol()), ([], None), ([row()], dict(protocol(), min_events=True)), ([dict(row(),weight=float('nan'))], protocol(1))]:
            with self.subTest(rows=rows, p=p):
                self.assertTrue(validate_cohort(rows, p))
                self.assertEqual(evaluate_outcomes(rows, p)['status'], 'invalid_cohort')

    def test_actual_external_observations_remain_zero(self):
        ledger = json.loads((ROOT/'docs/research/2026-10-16/experiment-status.json').read_text())
        for e in ledger['experiments']:
            if e['id'] in ('E-ORACLE','E-OUTCOME'):
                self.assertEqual(e['observed_count'], 0)
                self.assertEqual(e['status'], 'awaiting_external_evidence')

if __name__ == '__main__':
    unittest.main(verbosity=2)
