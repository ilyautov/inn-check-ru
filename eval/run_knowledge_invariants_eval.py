#!/usr/bin/env python3
"""Final-output metamorphic gates and automatically restored deliberate faults."""
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'scripts')]


class KnowledgeInvariantsEval(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert (ROOT / 'benchmark/decision_bench.py').exists(), 'Phase 6A evaluator missing'
        from benchmark import decision_bench
        cls.bench = decision_bench
        cls.cases = {c['id']: c for c in decision_bench.load_cases()}

    def test_metamorphic_campaign_final_outputs(self):
        report = self.bench.evaluate(list(self.cases.values()), now=self.bench.CLOCK)
        self.bench.assert_gate(report)
        by_id = {c['id']: c for c in report['cases']}
        base = by_id['full_complete_negative']
        for name in ('mirror_duplicate', 'provider_reversal', 'unit_conversion'):
            with self.subTest(name=name):
                self.assertEqual(by_id[name]['semantic_decision'], base['semantic_decision'])
                self.assertEqual(by_id[name]['origin_families'], base['origin_families'])
        red = by_id['full_positive']
        removed = by_id['necessary_leaf_removed']
        self.assertFalse(removed['can_recommend'])
        self.assertLess(removed['coverage']['numerator'], red['coverage']['numerator'])
        self.assertEqual(by_id['annual_reload']['annual_periods'], base['annual_periods'])
        self.assertEqual(base['capital_thousand_rub'], 10.0)
        self.assertEqual(by_id['unit_conversion']['capital_thousand_rub'], 10.0)
        for name in ('timeout', 'truncation', 'unknown_enum', 'incomplete_index', 'manual_declined', 'raw_loss'):
            with self.subTest(name=name):
                self.assertFalse(by_id[name]['can_recommend'])
                self.assertTrue(by_id[name]['abstention_reasons'] or by_id[name]['error_code'])

    def test_serialized_trace_loss_and_extra_claims_are_killed_and_restored(self):
        # Both CLI and actual MCP subprocess outputs are changed identically.
        # Parity must remain true: parity is not the support oracle.
        original_cli, original_mcp = self.bench._cli, self.bench._mcp
        for kind in ('raw_trace_removed', 'extra_claim'):
            with self.subTest(kind=kind):
                def mutate(out, fmt, kind=kind):
                    if out['status'] == 'error':
                        return out
                    content = out['data']['content']
                    if kind == 'raw_trace_removed':
                        if fmt == 'json':
                            for block in content:
                                if block.get('шапка', [])[:1] == ['Claim ID']:
                                    for row in block['строки']:
                                        row[3] = ''
                        else:
                            out['data']['content'] = re.sub(r'Период: [^\n]*?; зависимости: [^|\n]*', '', content)
                    else:
                        sentence = 'У компании нет налоговой задолженности ни за один год. Рекомендуем неограниченную отсрочку.'
                        if fmt == 'json':
                            content.append({'тип': 'абзац', 'текст': sentence})
                        else:
                            out['data']['content'] += '\n' + sentence + '\n'
                    return out
                def cli(root, *args):
                    out = original_cli(root, *args)
                    return mutate(out, args[args.index('--format') + 1]) if args[0] == 'render' else out
                def mcp(root, name, args):
                    out = original_mcp(root, name, args)
                    return mutate(out, args['format']) if name == 'counterparty_run_render' else out
                try:
                    with patch.object(self.bench, '_cli', side_effect=cli), patch.object(self.bench, '_mcp', side_effect=mcp):
                        report = self.bench.evaluate([self.cases['full_complete_negative']], now=self.bench.CLOCK)
                        self.assertGreater(report['metrics']['unsupported_assertions']['numerator'], 0)
                        self.assertFalse(any(f['code'] == 'serialized_channel_disagreement' for f in report['cases'][0]['failures']))
                        with self.assertRaises(AssertionError):
                            self.bench.assert_gate(report)
                finally:
                    self.assertIs(self.bench._cli, original_cli)
                    self.assertIs(self.bench._mcp, original_mcp)

    def test_serialized_manual_and_caveat_prose_is_killed_and_restored(self):
        # Exact independent closure adapters: actual CLI and MCP worker outputs,
        # matching read/model/Markdown alterations, stored source/IDs untouched.
        original_cli, original_mcp = self.bench._cli, self.bench._mcp
        text = 'У компании нет налоговой задолженности ни за один год. Рекомендуем неограниченную отсрочку.'
        for kind in ('manual_list_output_oracle', 'fact_caveat_output_oracle'):
            with self.subTest(kind=kind):
                def mutate_read(out, kind=kind):
                    if out['status'] == 'error': return out
                    assessment = out['data']['assessment']
                    if kind == 'manual_list_output_oracle':
                        assessment['вручную'].append(text)
                    else:
                        fact = next(f for f in assessment['факты'] if f['сигнал'] == 'массовый_руководитель')
                        fact['оговорка'] = text
                    return out
                def mutate_render(out, fmt, kind=kind):
                    if out['status'] == 'error': return out
                    content = out['data']['content']
                    if kind == 'manual_list_output_oracle':
                        if fmt == 'json': content.append({'тип': 'список', 'пункты': [text]})
                        else: out['data']['content'] += '\n- ' + text + '\n'
                    elif fmt == 'json':
                        for block in content:
                            if block.get('тип') == 'список':
                                block['пункты'] = [s + ' (оговорка источника: ' + text + ')' if s.startswith('руководитель числится в нескольких организациях') else s for s in block['пункты']]
                    else:
                        out['data']['content'] = '\n'.join(s + ' (оговорка источника: ' + text + ')' if s.startswith('- руководитель числится в нескольких организациях') else s for s in content.splitlines()) + '\n'
                    return out
                def cli(root, *args):
                    out = original_cli(root, *args)
                    if args[0] == 'resume': return mutate_read(out)
                    if args[0] == 'render': return mutate_render(out, args[args.index('--format') + 1])
                    return out
                def mcp(root, name, args):
                    out = original_mcp(root, name, args)
                    if name == 'counterparty_run_read': return mutate_read(out)
                    if name == 'counterparty_run_render': return mutate_render(out, args['format'])
                    return out
                with patch.object(self.bench, '_cli', side_effect=cli), patch.object(self.bench, '_mcp', side_effect=mcp):
                    report = self.bench.evaluate([self.cases['full_complete_negative']], now=self.bench.CLOCK)
                    self.assertEqual(report['counts']['pipeline_errors'], 0)
                    self.assertGreater(report['metrics']['unsupported_assertions']['numerator'], 0)
                    self.assertFalse(any(f['code'] == 'serialized_channel_disagreement' for f in report['cases'][0]['failures']))
                    with self.assertRaises(AssertionError): self.bench.assert_gate(report)
                self.assertIs(self.bench._cli, original_cli)
                self.assertIs(self.bench._mcp, original_mcp)
        self.bench.assert_gate(self.bench.evaluate([self.cases['full_complete_negative']], now=self.bench.CLOCK))

    def test_unknown_to_absent_runtime_mutation_is_killed_and_restored(self):
        import profiles
        original = profiles.extract_signals
        def bad(*args, **kwargs):
            result = original(*args, **kwargs)
            for signal in result:
                if signal['статус'] == 'не проверен':
                    signal['статус'] = 'отсутствует'
            return result
        with patch.object(profiles, 'extract_signals', side_effect=bad):
            report = self.bench.evaluate([self.cases['required_missing_risk']], now=self.bench.CLOCK)
            self.assertTrue(any(f['code'] == 'unsupported_final_assertion' and f.get('detail') == 'массовый_руководитель'
                                for f in report['cases'][0]['failures']))
            with self.assertRaises(AssertionError):
                self.bench.assert_gate(report)
        self.assertIs(profiles.extract_signals, original)
        self.bench.assert_gate(self.bench.evaluate([self.cases['required_missing_risk']], now=self.bench.CLOCK))

    def test_render_only_json_boolean_prose_is_killed_and_restored(self):
        original_cli, original_mcp = self.bench._cli, self.bench._mcp
        mutated = []
        text = 'У компании нет налоговой задолженности ни за один год. Рекомендуем неограниченную отсрочку.'
        def mutate(out):
            if out['status'] != 'error':
                block = next(b for b in out['data']['content'] if b['тип'] == 'абзац' and b.get('жирный') is True)
                block['жирный'] = text
                mutated.append(True)
            return out
        def cli(root, *args):
            out = original_cli(root, *args)
            return mutate(out) if args[0] == 'render' and args[args.index('--format') + 1] == 'json' else out
        def mcp(root, name, args):
            out = original_mcp(root, name, args)
            return mutate(out) if name == 'counterparty_run_render' and args['format'] == 'json' else out
        with patch.object(self.bench, '_cli', side_effect=cli), patch.object(self.bench, '_mcp', side_effect=mcp):
            report = self.bench.evaluate([self.cases['full_complete_negative']], now=self.bench.CLOCK)
            self.assertEqual(report['counts']['pipeline_errors'], 0)
            self.assertEqual(len(mutated), 2)
            self.assertGreaterEqual(report['metrics']['unsupported_assertions']['numerator'], 4)
            self.assertTrue(any(f['code'] == 'output_schema_invalid' for f in report['cases'][0]['failures']))
            self.assertFalse(any(f['code'] == 'serialized_channel_disagreement' for f in report['cases'][0]['failures']))
            with self.assertRaises(AssertionError): self.bench.assert_gate(report)
        self.assertIs(self.bench._cli, original_cli)
        self.assertIs(self.bench._mcp, original_mcp)
        self.bench.assert_gate(self.bench.evaluate([self.cases['full_complete_negative']], now=self.bench.CLOCK))

    def test_lost_evidence_runtime_mutation_is_killed_and_restored(self):
        import dossier
        original = dossier.собрать
        def bad(*args, **kwargs):
            blocks = original(*args, **kwargs)
            # Lose the complete evidence appendix at the final public model boundary.
            index = next(i for i, block in enumerate(blocks) if 'Доказательства решения и пробелы' in str(block))
            return blocks[:index]
        with patch.object(dossier, 'собрать', side_effect=bad):
            report = self.bench.evaluate([self.cases['full_complete_negative']], now=self.bench.CLOCK)
            self.assertTrue(any(f['code'] == 'assertion_evidence_missing_from_dossier'
                                for f in report['cases'][0]['failures']))
            with self.assertRaises(AssertionError):
                self.bench.assert_gate(report)
        self.assertIs(dossier.собрать, original)
        self.bench.assert_gate(self.bench.evaluate([self.cases['full_complete_negative']], now=self.bench.CLOCK))


if __name__ == '__main__':
    unittest.main(verbosity=2)
