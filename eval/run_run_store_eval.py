#!/usr/bin/env python3
"""Offline local run storage regression suite; all writes use temporary roots."""
import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
try:
    import run_store as store
except ImportError:
    store = None

NOW = '2026-10-16T09:00:00Z'
PAYLOAD = {'fetch': {'инн': '7707083893'}, 'fin': {}, 'assessment': {},
           'metadata': {'schema_version': 1}}


class StoreEval(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / 'runs'
        self.assertIsNotNone(store, 'run_store module must implement the fixed local storage API')

    def create(self, payload=None):
        return store.create(copy.deepcopy(PAYLOAD if payload is None else payload), root=self.root, now=NOW)

    def error(self, code, fn, *args, **kwargs):
        with self.assertRaises(store.RunStoreError) as caught:
            fn(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        return str(caught.exception)

    def test_create_append_read_cas_and_idempotency(self):
        one = self.create()
        self.assertEqual(str(uuid.UUID(one['run_id'], version=4)), one['run_id'])
        self.assertEqual(one['revision'], 1)
        self.assertIsNone(one['parent_revision'])
        self.assertEqual(one['payload'], PAYLOAD)
        operation = str(uuid.uuid4())
        two = store.append(one['run_id'], PAYLOAD, expected_revision=1, operation_id=operation,
                           root=self.root, now=NOW)
        self.assertEqual(two, store.append(one['run_id'], PAYLOAD, expected_revision=1,
                         operation_id=operation, root=self.root, now='2026-10-16T10:00:00Z'))
        self.assertEqual(store.read(one['run_id'], revision=1, root=self.root), one)
        self.assertEqual(store.read(one['run_id'], root=self.root), two)
        self.error('revision_conflict', store.append, one['run_id'], PAYLOAD, expected_revision=1,
                   operation_id=str(uuid.uuid4()), root=self.root)
        changed = copy.deepcopy(PAYLOAD)
        changed['fetch']['ordinary'] = 1
        self.error('operation_conflict', store.append, one['run_id'], changed, expected_revision=2,
                   operation_id=operation, root=self.root)
        self.assertEqual(store.read(one['run_id'], root=self.root), two)

    def test_no_write_on_read_missing_or_existing(self):
        self.error('not_found', store.read, str(uuid.uuid4()), root=self.root)
        self.assertFalse(self.root.exists())
        one = self.create()
        before = {str(p.relative_to(self.root)): p.stat().st_mtime_ns for p in self.root.rglob('*')}
        with patch.object(store.os, 'mkdir', side_effect=AssertionError('read must never mkdir')), \
             patch.object(store.os, 'replace', side_effect=AssertionError('read must never write')):
            store.read(one['run_id'], root=self.root)
        after = {str(p.relative_to(self.root)): p.stat().st_mtime_ns for p in self.root.rglob('*')}
        self.assertEqual(before, after)

    def test_invalid_ids_revisions_and_materialized_schema(self):
        one = self.create()
        for bad in ['../x', '/tmp/x', one['run_id'].upper(), str(uuid.uuid1()), '', None]:
            self.error('invalid_run_id', store.read, bad, root=self.root)
        for bad in [True, 0, -1, '1', 1.5]:
            self.error('invalid_revision', store.read, one['run_id'], revision=bad, root=self.root)
        self.error('not_found', store.read, one['run_id'], revision=2, root=self.root)
        for bad in [{}, {**PAYLOAD, 'extra': {}}, {**PAYLOAD, 'fin': []},
                    {**PAYLOAD, 'metadata': {'schema_version': 2}},
                    {**PAYLOAD, 'fetch': {'инн': '../SECRET'}}]:
            self.error('invalid_payload' if bad.get('metadata', {}).get('schema_version') != 2
                       else 'unsupported_schema', self.create, bad)
        for bad in ['', str(uuid.uuid1()), '../x']:
            self.error('invalid_operation_id', store.append, one['run_id'], PAYLOAD,
                       expected_revision=1, operation_id=bad, root=self.root)

    def test_payload_non_json_and_oversize(self):
        for value in [float('nan'), object(), {1: 'bad'}]:
            bad = copy.deepcopy(PAYLOAD)
            bad['fin']['value'] = value
            self.error('invalid_payload', self.create, bad)
        bad = copy.deepcopy(PAYLOAD)
        bad['fin']['huge'] = 'x' * (32 * 1024 * 1024)
        self.error('too_large', self.create, bad)
        self.assertFalse(self.root.exists(), 'invalid input must not create store')

    def test_redaction_preserves_data_and_removes_secret_copies(self):
        bad = copy.deepcopy(PAYLOAD)
        secret = 'SENTINEL_RUN_STORE_PRIVATE_271828'
        bad['fetch'].update({'_token_pb': secret, 'Authorization': 'Bearer ' + secret,
                             'api-key': secret,
                             'ordinary': {'rows': [{'value': 0, 'flag': False, 'none': None}],
                                          'name': 'Обычное поле'}})
        bad['metadata'].update({'environment': {'PRIVATE': secret},
                               'nested': [{'api_key': secret}],
                               'url': 'https://alice:' + secret + '@proxy.test/path?api_key=' + secret,
                               'diagnostic': 'Authorization: Bearer ' + secret,
                               'copy': 'failure using ' + secret})
        one = self.create(bad)
        self.assertEqual(one['payload']['fetch']['ordinary'], bad['fetch']['ordinary'])
        self.assertNotIn('_token_pb', one['payload']['fetch'])
        self.assertEqual(store.read(one['run_id'], root=self.root), one)
        self.assertNotIn(secret, json.dumps(one, ensure_ascii=False))
        for file in self.root.rglob('*'):
            if file.is_file():
                self.assertNotIn(secret.encode(), file.read_bytes(), str(file))
        self.assertEqual(bad['fetch']['_token_pb'], secret, 'caller data must be unchanged')

    def test_credentials_only_in_diagnostics_never_reach_disk(self):
        for diagnostic in ['https://example.test/?api_token=PRIVATEQUERY',
                           'Authorization: Token PRIVATEQUERY']:
            with self.subTest(diagnostic=diagnostic):
                payload = copy.deepcopy(PAYLOAD)
                payload['metadata'].update(diagnostic=diagnostic, copy='PRIVATEQUERY')
                one = self.create(payload)
                self.assertEqual(store.read(one['run_id'], root=self.root), one)
                for file in self.root.rglob('*'):
                    if file.is_file():
                        self.assertNotIn(b'PRIVATEQUERY', file.read_bytes())

    def test_short_environment_username_preserves_identity_and_canonical_raw_refs(self):
        payload = copy.deepcopy(PAYLOAD)
        raw_refs = {'responses': {'r1': {'source_id': 'source', 'subject_inn': '7707083893',
                    'sha256': 'b' * 64}},
                    'fields': {'staff': [{'response_id': 'r1', 'locator': '/records/0/staff'}]}}
        payload['metadata'].update(environment={'DEBUG': '7'}, raw_refs=raw_refs,
                                   proxy='http://a:password@proxy.example:8080',  # trufflehog:ignore -- reserved-domain synthetic redaction fixture
                                   nested=[{'proxy': 'http://b:password@proxy.example:8080'}])  # trufflehog:ignore -- reserved-domain synthetic redaction fixture
        one = self.create(payload)
        self.assertEqual(one['subject_inn'], '7707083893')
        self.assertEqual(one['manifest']['raw_refs'], raw_refs)
        self.assertEqual(store.read(one['run_id'], root=self.root), one)

    def test_secret_collision_with_identity_or_raw_reference_rejected_before_write(self):
        for secret, refs in [('770', {}), ('b', {'responses': {'r1': {'sha256': 'b' * 64}}}),
                             ('r1', {'responses': {'r1': {'sha256': 'b' * 64}}}),
                             ('records', {'fields': {'staff': [{'locator': '/records/0/staff'}]}})]:
            with self.subTest(secret=secret):
                payload = copy.deepcopy(PAYLOAD)
                payload['metadata'].update(password=secret, raw_refs=refs)
                self.error('invalid_payload', self.create, payload)
                self.assertFalse(self.root.exists())

    def test_noncanonical_redaction_rejected_before_write(self):
        with patch.object(store, 'redact', side_effect=[copy.deepcopy(PAYLOAD),
                                                     {**PAYLOAD, 'changed': {}}]):
            self.error('invalid_payload', self.create)
        self.assertFalse(self.root.exists())

    def test_post_redaction_schema_and_ordinary_digest_collisions_rejected(self):
        for safe in [{**PAYLOAD, 'fetch': {}}, {**PAYLOAD, 'metadata': {'schema_version': 2}},
                     {**PAYLOAD, 'metadata': {'raw_refs': []}}, {**PAYLOAD, 'metadata': {}}]:
            with self.subTest(safe=safe), patch.object(store, 'redact', return_value=safe):
                self.error('invalid_payload', self.create)
                self.assertFalse(self.root.exists())
        for secret, field, value in [('b', 'checksum', 'b' * 64),
                                     ('ordinary', 'ordinary_field', 'safe')]:
            payload = copy.deepcopy(PAYLOAD)
            payload['fin'][field] = value
            payload['metadata']['password'] = secret
            self.error('invalid_payload', self.create, payload)
            self.assertFalse(self.root.exists())

    def test_nested_evidence_identity_collision_rejected_before_write(self):
        for key in ['response_id', 'source_id', 'locator', 'transformation']:
            with self.subTest(key=key):
                payload = copy.deepcopy(PAYLOAD)
                payload['fetch']['_evidence'] = {'claims': {'c1': {'raw_ref': {key: 'PRIVATECOPY'}}}}
                payload['metadata']['api_token'] = 'PRIVATECOPY'
                self.error('invalid_payload', self.create, payload)
                self.assertFalse(self.root.exists())

    def test_marker_auth_payload_is_immediately_readable(self):
        payload = copy.deepcopy(PAYLOAD)
        payload['metadata'].update(password='D', diagnostic='[REDACTED] Bearer D')
        one = self.create(payload)
        self.assertEqual(store.read(one['run_id'], root=self.root), one)

    def test_root_and_store_entries_refuse_symlinks(self):
        outside = self.root.parent / 'outside'
        outside.mkdir()
        linked = self.root.parent / 'linked'
        linked.symlink_to(outside, target_is_directory=True)
        self.error('unsafe_path', store.create, PAYLOAD, root=linked / 'runs')
        self.assertEqual(list(outside.iterdir()), [])
        one = self.create()
        run = self.root / one['run_id']
        for relative in ['HEAD.json', 'revisions/000001.json', '.lock']:
            target = run / relative
            original = target.read_bytes()
            target.unlink()
            target.symlink_to(outside / 'target')
            fn = store.append if relative == '.lock' else store.read
            kwargs = {'expected_revision': 1, 'operation_id': str(uuid.uuid4())} if fn is store.append else {}
            args = (one['run_id'], PAYLOAD) if fn is store.append else (one['run_id'],)
            self.error('unsafe_path', fn, *args, root=self.root, **kwargs)
            target.unlink()
            target.write_bytes(original)
        revisions = run / 'revisions'
        revisions.rename(run / 'saved')
        revisions.symlink_to(outside, target_is_directory=True)
        self.error('unsafe_path', store.read, one['run_id'], root=self.root)
        self.assertEqual(list(outside.iterdir()), [])

    def test_tampered_head_revision_parent_and_foreign_record(self):
        one = self.create()
        run = self.root / one['run_id']
        head_path = run / 'HEAD.json'
        revision_path = run / 'revisions/000001.json'
        head, revision = head_path.read_bytes(), revision_path.read_bytes()
        for damaged in [b'{}', b'{bad', json.dumps({'revision': 1, 'digest': '0' * 64}).encode()]:
            head_path.write_bytes(damaged)
            self.error('corrupt_store', store.read, one['run_id'], root=self.root)
        head_path.write_bytes(head)
        revision_path.write_bytes(revision + b' ')
        self.error('corrupt_store', store.read, one['run_id'], root=self.root)
        foreign = json.loads(revision)
        foreign['run_id'] = str(uuid.uuid4())
        encoded = json.dumps(foreign, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
        revision_path.write_bytes(encoded)
        new_head = json.loads(head)
        new_head['digest'] = hashlib.sha256(encoded).hexdigest()
        head_path.write_text(json.dumps(new_head), encoding='utf-8')
        self.error('corrupt_store', store.read, one['run_id'], root=self.root)
        revision_path.write_bytes(revision)
        head_path.write_bytes(head)
        store.append(one['run_id'], PAYLOAD, expected_revision=1,
                     operation_id=str(uuid.uuid4()), root=self.root, now=NOW)
        revision_path.write_bytes(revision + b' ')
        self.error('corrupt_store', store.read, one['run_id'], revision=2, root=self.root)

    def test_schema_types_and_safe_path_errors(self):
        one = self.create()
        path = self.root / one['run_id'] / 'HEAD.json'
        head = json.loads(path.read_bytes())
        head['schema_version'] = True
        path.write_text(json.dumps(head), encoding='utf-8')
        self.error('unsupported_schema', store.read, one['run_id'], root=self.root)
        self.error('unsafe_path', store.read, one['run_id'], root='bad\x00root')
        for field, value in [('profile', []), ('workflow_status', {}), ('raw_refs', [])]:
            bad = copy.deepcopy(PAYLOAD)
            bad['metadata'][field] = value
            self.error('invalid_payload', self.create, bad)

    def child(self, source, *args, env=None):
        return subprocess.Popen([sys.executable, '-c', 'import sys; sys.path.insert(0, ' +
                                 repr(str(SCRIPTS)) + '); ' + source, *map(str, args)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)

    def wait_file(self, path, process):
        limit = time.monotonic() + 10
        while time.monotonic() < limit:
            if path.exists():
                return
            if process.poll() is not None:
                self.fail('child exited before barrier: ' + repr(process.communicate()))
            time.sleep(.01)
        process.kill()
        self.fail('child did not reach barrier')

    def test_configured_root_in_child_and_explicit_precedence(self):
        configured = self.root.parent / 'configured'
        env = dict(os.environ, INN_CHECK_RUNS_DIR=str(configured))
        source = 'import run_store,json; print(json.dumps(run_store.create(json.loads(sys.argv[1]))))'
        child = self.child(source, json.dumps(PAYLOAD), env=env)
        out, err = child.communicate(timeout=10)
        self.assertEqual(child.returncode, 0, err)
        one = json.loads(out)
        self.assertEqual(store.read(one['run_id'], root=configured), one)
        source = 'import run_store,json; print(json.dumps(run_store.create(json.loads(sys.argv[2]),root=sys.argv[1])))'
        child = self.child(source, self.root, json.dumps(PAYLOAD), env=env)
        out, err = child.communicate(timeout=10)
        self.assertEqual(child.returncode, 0, err)
        explicit = json.loads(out)
        self.assertTrue((self.root / explicit['run_id']).exists())
        self.assertFalse((configured / explicit['run_id']).exists())

    def test_subprocess_restart_and_simultaneous_writer_race(self):
        source = 'import run_store,json; print(json.dumps(run_store.create(json.loads(sys.argv[2]),root=sys.argv[1])))'
        child = self.child(source, self.root, json.dumps(PAYLOAD))
        out, err = child.communicate(timeout=10)
        self.assertEqual(child.returncode, 0, err)
        one = json.loads(out)
        self.assertEqual(store.read(one['run_id'], root=self.root), one)
        source = '''
import run_store,json,time
from pathlib import Path
root,run,op,ready,go=sys.argv[1:]
Path(ready).touch()
while not Path(go).exists(): time.sleep(.01)
try:
 r=run_store.append(run,json.loads(%r),expected_revision=1,operation_id=op,root=root)
 print(json.dumps({'ok':r['revision']}))
except run_store.RunStoreError as error: print(json.dumps({'error':error.code}))
''' % json.dumps(PAYLOAD)
        go = self.root.parent / 'go'
        children = [self.child(source, self.root, one['run_id'], str(uuid.uuid4()),
                               self.root.parent / ('ready' + str(i)), go) for i in range(2)]
        for i, child in enumerate(children):
            self.wait_file(self.root.parent / ('ready' + str(i)), child)
        go.touch()
        results = []
        for child in children:
            out, err = child.communicate(timeout=10)
            self.assertEqual(child.returncode, 0, err)
            results.append(json.loads(out))
        self.assertCountEqual(results, [{'ok': 2}, {'error': 'revision_conflict'}])
        self.assertEqual(store.read(one['run_id'], root=self.root)['revision'], 2)

    def test_killed_writers_leave_head_and_retry_recoverable(self):
        source = '''
import run_store,json,time,os
from pathlib import Path
root,run,op,stage,marker=sys.argv[1:]
original=run_store.os.replace
def replace(src,dst,*args,**kwargs):
 name=Path(os.fspath(dst)).name
 target=(stage=='before_revision' and name=='000002.json') or (stage=='before_head' and name=='HEAD.json')
 if target:
  Path(marker).touch()
  while True: time.sleep(.1)
 result=original(src,dst,*args,**kwargs)
 if (stage=='after_revision' and name=='000002.json') or (stage=='after_head' and name=='HEAD.json'):
  Path(marker).touch()
  while True: time.sleep(.1)
 return result
run_store.os.replace=replace
run_store.append(run,json.loads(%r),expected_revision=1,operation_id=op,root=root,now=%r)
''' % (json.dumps(PAYLOAD), NOW)
        for stage in ['before_revision', 'after_revision', 'before_head', 'after_head']:
            with self.subTest(stage=stage):
                one = self.create()
                marker = self.root.parent / stage
                operation = str(uuid.uuid4())
                child = self.child(source, self.root, one['run_id'], operation, stage, marker)
                self.wait_file(marker, child)
                child.kill()
                child.communicate(timeout=10)
                if stage == 'after_head':
                    committed = store.read(one['run_id'], root=self.root)
                    self.assertEqual(committed['revision'], 2)
                    self.assertEqual(store.append(one['run_id'], PAYLOAD, expected_revision=1,
                                     operation_id=operation, root=self.root), committed)
                    continue
                self.assertEqual(store.read(one['run_id'], root=self.root), one)
                self.error('not_found', store.read, one['run_id'], revision=2, root=self.root)
                # A different operation can proceed; old uncommitted bytes cannot block recovery.
                two = store.append(one['run_id'], PAYLOAD, expected_revision=1,
                                   operation_id=str(uuid.uuid4()), root=self.root, now=NOW)
                self.assertEqual(two['revision'], 2)
                self.assertEqual(store.read(one['run_id'], root=self.root), two)


    def test_raw_and_artifact_confinement_integrity_and_atomic_registry(self):
        one = self.create()
        rid = one['run_id']
        digest = store.put_raw(rid, b'raw fixture', root=self.root)
        self.assertEqual(digest, hashlib.sha256(b'raw fixture').hexdigest())
        self.assertEqual(store.read_raw(rid, digest, root=self.root), b'raw fixture')
        self.error('invalid_payload', store.read_raw, rid, '../outside', root=self.root)
        self.error('too_large', store.put_raw, rid, b'x' * (store.MAX_JSON_BYTES + 1), root=self.root)
        artifact = store.put_artifact(rid, b'document', format='markdown', source_revision=1, root=self.root)
        self.assertEqual(store.read_artifact(rid, artifact['artifact_id'], root=self.root), b'document')
        atomic = store._atomic_write
        def disk_full(directory, name, data):
            if name == 'artifacts.json':
                raise OSError(28, 'disk full')
            return atomic(directory, name, data)
        with patch.object(store, '_atomic_write', side_effect=disk_full):
            self.error('storage_error', store.put_artifact, rid, b'orphan', format='json', source_revision=1, root=self.root)
        self.assertEqual(store.list_artifacts(rid, root=self.root), [artifact])
        raw = self.root / rid / 'raw' / digest
        raw.write_bytes(b'tampered')
        self.error('corrupt_store', store.read_raw, rid, digest, root=self.root)
        if os.name == 'posix':
            raw.unlink()
            raw.symlink_to('/etc/passwd')
            self.error('unsafe_path', store.read_raw, rid, digest, root=self.root)

    def test_find_operation_reads_committed_history_only(self):
        one = self.create()
        op = str(uuid.uuid4())
        self.assertIsNone(store.find_operation(one['run_id'], op, root=self.root))
        two = store.append(one['run_id'], PAYLOAD, expected_revision=1, operation_id=op, root=self.root)
        self.assertEqual(store.find_operation(one['run_id'], op, root=self.root), two)

if __name__ == '__main__':
    unittest.main(verbosity=2)
