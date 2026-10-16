#!/usr/bin/env python3
"""Offline sibling loading regression, each import mode runs in a fresh process."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = r'''
import datetime
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch
from types import SimpleNamespace
root = Path(sys.argv[1])
mode = sys.argv[2]
if mode in ("normal", "installed"):
    if mode == "normal":
        sys.path.insert(0, str(root / "scripts"))
    fc = importlib.import_module("fetch_counterparty")
else:
    if mode == "absolute_contract":
        spec = importlib.util.spec_from_file_location("evidence_contract", root / "scripts/evidence_contract.py")
        contract = importlib.util.module_from_spec(spec)
        sys.modules["evidence_contract"] = contract
        spec.loader.exec_module(contract)
    spec = importlib.util.spec_from_file_location("legacy_fetch", root / "scripts/fetch_counterparty.py")
    fc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fc)
paths, environment = list(sys.path), dict(os.environ)
handlers = fc.proxy.обработчики("socks5://proxy.invalid:1080", fc._SSL)
assert any(type(handler).__name__ == "SocksHTTPSHandler" for handler in handlers)
inn = "7707083893"
received, completed = [], []
secret = "private-import-regression-token"
raw = json.dumps({"rows": [{"i": inn, "r": "2000-01-01"}], "api_key": secret}).encode()

def source(sid, subject, **kwargs):
    if sid == "егрюл":
        fc._ИСТОЧНИК.id = sid
        fc._ИСТОЧНИК.subject_inn = subject
        fc._CAPTURE.get().record("GET", "https://egrul.nalog.ru/", 200, raw)
        data = {"дата_регистрации": "2000-01-01", "наименование": "TEST", "api_key": secret}
        return data, {"состояние": "ok"}
    return None, {"состояние": "не проверено", "причина": "offline fixture"}

class FrozenDatetime(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 16, 9, 0, 0, tzinfo=datetime.timezone.utc).astimezone(tz)

clock = SimpleNamespace(datetime=FrozenDatetime, timezone=datetime.timezone, date=datetime.date)
with patch.object(fc, "run_source", source), patch.object(fc, "_load_access_cache", return_value=None), patch.object(fc, "_dt", clock):
    with fc.capture_session(inn, on_response=received.append) as capture:
        result = fc.collect(inn, режим="quick", on_source=lambda *args: completed.append(args))
        refs = capture.raw_refs(result)
assert received and completed and refs["responses"] and refs["fields"]
assert secret not in repr(received) + repr(completed) + repr(result)
assert all(hashlib.sha256(item["данные"]).hexdigest() == item["sha256"] for item in received)
ec = fc._load_sibling("evidence_contract")
assert ec is fc._load_sibling("evidence_contract")
now = "2026-10-16T09:00:00Z"
enriched = ec.enrich(result, now=now, raw_refs=refs)
assert ec.validate_evidence(enriched) == []
assert any(c["raw_ref"]["availability"] == "available" for c in enriched["_evidence"]["claims"].values())
observed_contexts = []
original_prepare = ec.prepare
def observe_prepare(*args, **kwargs):
    observed_contexts.append(ec._EVALUATION_NOW.get())
    return original_prepare(*args, **kwargs)
with patch.object(ec, "prepare", observe_prepare):
    evaluated = ec.evaluate(enriched, profile="нейтрально", now=now)
assert len(observed_contexts) >= 2 and set(observed_contexts) == {now}, observed_contexts
assert evaluated["metadata"]["evaluated_at"] == now
assert "source_states" in evaluated["fetch"]["_evidence"]
assert ec is importlib.import_module("evidence_contract")
assert fc._load_sibling("redaction").redact is importlib.import_module("redaction").redact
assert paths == sys.path, (paths, sys.path)
assert environment == dict(os.environ)
if mode in ("normal", "installed"):
    assert fc._load_sibling("fetch_counterparty") is fc
    sentinel = object()
    with patch("redaction.redact", return_value=sentinel):
        assert fc._load_sibling("redaction").redact({}) is sentinel
'''


class CompatibilityTests(unittest.TestCase):
    def scenario(self, mode):
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        run = subprocess.run([sys.executable, "-c", SCENARIO, str(ROOT), mode],
                             check=False, env=environment, text=True, capture_output=True)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def test_process_runtime_identity_across_absolute_entrypoints(self):
        program = r'''import concurrent.futures, importlib, importlib.util, os, sys
from pathlib import Path
from unittest.mock import patch
root = Path(sys.argv[1])
paths, environment = list(sys.path), dict(os.environ)
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
document = load('absolute_document', root / 'scripts/document_input.py')
with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
    futures = [pool.submit(document._process_helpers) for _ in range(7)]
    tools = pool.submit(load, 'absolute_tools', root / 'mcp/tools_impl.py').result()
    modules = [future.result() for future in futures]
runtime = modules[0]
assert all(module is runtime for module in modules)
assert tools._process_runtime is runtime is importlib.import_module('process_runtime')
assert tools._ProcessStartError is runtime._ProcessStartError
assert tools._ProcessExchange is runtime._ProcessExchange
compat = load('compat_control', root / 'mcp/process_control.py')
windows = importlib.import_module('process_windows')
assert compat.WindowsJob is runtime.WindowsJob is windows.WindowsJob
assert tools._probe_binary_version is runtime._probe_binary_version
watchlist = load('absolute_watchlist', root / 'scripts/watchlist.py')
with patch.object(runtime, 'run_script', return_value={'status': 'partial'}) as runner:
    assert watchlist._workflow_runner('7707083893', 'нейтрально', root / 'runs') == {'status': 'partial'}
    runner.assert_called_once()
assert sys.path == paths, (paths, sys.path)
assert dict(os.environ) == environment
'''
        environment = dict(os.environ)
        environment.pop('PYTHONPATH', None)
        with tempfile.TemporaryDirectory() as directory:
            process = subprocess.run([sys.executable, '-S', '-c', program, str(ROOT)],
                                     cwd=directory, env=environment, text=True,
                                     capture_output=True, check=False)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)

    def test_fallback_locks_rollback_and_dependency_errors(self):
        program = r'''
import builtins
import concurrent.futures
import importlib
import importlib.util
import threading
from pathlib import Path
import shutil
import sys
from unittest.mock import patch
root, folder = map(Path, sys.argv[1:])
shutil.copyfile(root / "scripts/sibling_imports.py", folder / "sibling_imports.py")
spec = importlib.util.spec_from_file_location("loader_test", folder / "sibling_imports.py")
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
(folder / "parallel_fixture.py").write_text("import time\ntime.sleep(0.02)\nVALUE = object()\n")
with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
    modules = list(pool.map(lambda _: helper.load_sibling("parallel_fixture"), range(48)))
assert all(module is modules[0] for module in modules)
assert sys.modules["parallel_fixture"] is modules[0]
builtins.import_compat_event = threading.Event()
(folder / "mixed_fixture.py").write_text("import builtins, time\nbuiltins.import_compat_event.set()\ntime.sleep(0.02)\nVALUE = 7\n")
with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
    pending = pool.submit(helper.load_sibling, "mixed_fixture")
    assert builtins.import_compat_event.wait(5)
    normal = importlib.import_module("mixed_fixture")
    assert normal.VALUE == 7 and normal is pending.result(timeout=5)
del builtins.import_compat_event
(folder / "broken_fixture.py").write_text("import absent_transitive_fixture\n")
try:
    helper.load_sibling("broken_fixture")
except ModuleNotFoundError as error:
    assert error.name == "absent_transitive_fixture", error
else:
    raise AssertionError("transitive error swallowed")
assert "broken_fixture" not in sys.modules
(folder / "broken_fixture.py").write_text("VALUE = 3\n")
assert helper.load_sibling("broken_fixture").VALUE == 3
with patch.object(helper.importlib, "import_module", side_effect=ModuleNotFoundError("dependency", name="transitive")):
    try:
        helper.load_sibling("parallel_fixture")
    except ModuleNotFoundError as error:
        assert error.name == "transitive"
    else:
        raise AssertionError("transitive normal import error swallowed")
with patch.object(helper, "_bootstrap", object()):
    assert helper.load_sibling("json").__name__ == "json"
    try:
        helper.load_sibling("absent_fixture")
    except ImportError as error:
        assert "module locks" in str(error)
    else:
        raise AssertionError("unsupported fallback accepted")
for name in ("../redaction", "os.path", "", None):
    try:
        helper.load_sibling(name)
    except ValueError:
        pass
    else:
        raise AssertionError("invalid sibling name accepted")
'''
        with tempfile.TemporaryDirectory() as folder:
            environment = dict(os.environ)
            environment.pop("PYTHONPATH", None)
            run = subprocess.run([sys.executable, "-c", program, str(ROOT), folder],
                                 check=False, env=environment, text=True, capture_output=True, timeout=20)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def test_legacy_absolute_capture_callbacks_enrich_evaluate(self):
        self.scenario("absolute")

    def test_registered_absolute_contract_entry_keeps_evaluation_context(self):
        self.scenario("absolute_contract")

    def test_normal_capture_callbacks_enrich_evaluate_singletons(self):
        self.scenario("normal")


if __name__ == "__main__":
    unittest.main()
