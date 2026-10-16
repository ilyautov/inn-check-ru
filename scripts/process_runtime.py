"""Canonical stdlib process containment for skill ZIP, installed engine and MCP.

The engine owns process groups / suspended Windows Jobs. Transport adapters
share the same module and exception identity through sibling_imports.
"""
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from sibling_imports import load_sibling

_control = load_sibling("process_windows")
CREATE_SUSPENDED, WindowsJob = _control.CREATE_SUSPENDED, _control.WindowsJob
CLEANUP_TIMEOUT = 2.0

def _не_проверено(причина, инструмент=None):
    out = {"статус": "не проверено", "причина": причина}
    if инструмент:
        out["инструмент"] = инструмент
    return out


class _ProcessExchange:
    """One owner of communicate; its deadline is enforced outside blocking I/O.

    Windows communicate writes stdin synchronously before timed reader joins.
    Keeping the entire exchange in one worker lets the supervisor terminate the
    owned job/group even when that write is blocked. Never retry communicate.
    """

    def __init__(self, proc, input_text):
        self.done = threading.Event()
        self.output = None
        self.error = None

        def communicate():
            try:
                self.output = proc.communicate(input=input_text)
            except BaseException as exc:
                self.error = exc
            finally:
                self.done.set()

        self.thread = threading.Thread(target=communicate, daemon=True,
                                       name='inn-check-exchange')

    def run(self, proc, timeout):
        deadline = time.monotonic() + timeout
        self.thread.start()
        if not self.done.wait(max(0, deadline - time.monotonic())):
            raise subprocess.TimeoutExpired(proc.args, timeout)
        if self.error is not None:
            raise self.error
        return self.output


def _stop_process_tree(proc, windows=None, exchange=None):
    """Отменить дерево с ограниченным ожиданием; вернуть пробелы подтверждения.

    После ошибки job/killpg прямой kill спасает только ребёнка. Потомки
    могли сохранить каналы, поэтому ни чтение, ни reap, ни close не безграничны.
    """
    windows = os.name == "nt" if windows is None else windows
    problems = []
    deadline = time.monotonic() + CLEANUP_TIMEOUT
    killed_directly = False

    def remaining():
        return max(0, deadline - time.monotonic())

    def kill_child():
        nonlocal killed_directly
        killed_directly = True
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        except Exception as exc:
            problems.append("kill: %s" % type(exc).__name__)

    if windows:
        # An assignment/resume failure leaves the child suspended; terminate it
        # before closing the job, since it might not belong to the job yet.
        if not getattr(proc, '_inn_job_ready', False):
            kill_child()
        try:
            job = getattr(proc, '_inn_job', None)
            if job is None:
                raise OSError('owned job missing')
            job.close()
        except Exception as exc:
            problems.append("job: %s" % type(exc).__name__)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # группа успела закончиться между таймаутом и отменой
        except Exception as exc:
            problems.append("killpg: %s" % type(exc).__name__)
    if problems and not killed_directly:
        kill_child()
    # Killing containment first releases blocked pipe writers/readers. Join the
    # original exchange and any CPython Windows readers (also created before a
    # UnicodeEncodeError). No second communicate or competing pipe closer.
    active = False

    def join_worker(worker):
        try:
            worker.join(timeout=remaining())
        except RuntimeError:
            # Thread.start itself can fail; there is then no I/O owner to join.
            pass
        return worker.is_alive()

    if exchange is not None:
        active = join_worker(exchange.thread)
    # Readers may be installed by communicate immediately after cancellation.
    # Inspect them only after joining the exchange, so none can appear later
    # on the confirmed-cleanup path.
    for name in ('stdout_thread', 'stderr_thread'):
        if name in proc.__dict__:
            active = join_worker(proc.__dict__[name]) or active
    if active:
        problems.append("каналы: таймаут завершения обмена; reap/close не подтверждены")
    else:
        try:
            proc.wait(timeout=remaining())
        except Exception as exc:
            problems.append("reap: %s" % type(exc).__name__)

        # Even after exchange completion, an exceptional pipe close must not
        # hold the supervisor indefinitely. All cleanup shares one deadline.
        close_errors = []

        def close_pipes():
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                if pipe is not None:
                    try:
                        pipe.close()
                    except Exception as exc:
                        close_errors.append("close: %s" % type(exc).__name__)

        closer = threading.Thread(target=close_pipes, daemon=True,
                                  name='inn-check-pipe-close')
        closer.start()
        closer.join(timeout=remaining())
        if closer.is_alive():
            problems.append("закрытие каналов: таймаут")
        else:
            problems.extend(close_errors)
    if problems:
        return "завершение дерева не подтверждено; " + "; ".join(problems)
    return None


class _ProcessStartError(Exception):
    """Launch failure with a bounded cleanup diagnostic safe for the wire."""


def _start_process(command, **kwargs):
    """Return an owned process only after platform containment is established."""
    if os.name != 'nt':
        return subprocess.Popen(command, start_new_session=True, **kwargs)
    job = WindowsJob()
    proc = None
    try:
        proc = subprocess.Popen(command, creationflags=CREATE_SUSPENDED, **kwargs)
        proc._inn_job = job
        proc._inn_job_ready = False
        job.assign_and_resume(proc.pid)
        proc._inn_job_ready = True
        return proc
    except BaseException as exc:
        cleanup = None
        if proc is not None:
            cleanup = _stop_process_tree(proc)
        else:
            try:
                job.close()
            except Exception as close_error:
                cleanup = 'job: %s' % type(close_error).__name__
        if cleanup and isinstance(exc, Exception):
            raise _ProcessStartError('%s; %s' % (type(exc).__name__, cleanup)) from exc
        raise


def _script_output(script, proc, stdout, stderr, reason, cleanup, raw):
    if reason or cleanup:
        return _не_проверено("; ".join(part for part in (reason, cleanup) if part), script)
    if raw:
        if proc.returncode != 0 or not (stdout or "").strip():
            return _не_проверено(
                "движок не отдал документ (exit %s): %s"
                % (proc.returncode, (stderr or "")[:300]), script)
        return stdout
    try:
        return json.loads(stdout)
    except ValueError:
        return _не_проверено(
            "движок вернул не-JSON (exit %s): %s"
            % (proc.returncode, (stderr or stdout)[:300]), script)


async def _probe_binary_version(command, timeout=2.0):
    """Bound version output/time and own every descendant, including on cancellation."""
    import asyncio
    import tempfile
    # A version probe needs executable/loader and text-runtime configuration,
    # never API/proxy credentials or arbitrary host settings. In particular do
    # not give wrappers a host PYTHONPATH or user site-package override.
    inherited = (
        'PATH', 'PATHEXT', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'SYSTEMDRIVE',
        'PYTHONHOME', 'PYTHONPLATLIBDIR', 'PYTHONUTF8', 'PYTHONIOENCODING',
        'PYTHONCOERCECLOCALE', 'PYTHONNOUSERSITE',
        'LD_LIBRARY_PATH', 'DYLD_LIBRARY_PATH', 'DYLD_FALLBACK_LIBRARY_PATH',
        'LANG', 'LANGUAGE', 'LC_ALL', 'LC_CTYPE', 'LC_MESSAGES', 'LC_TIME',
        'LC_NUMERIC', 'LC_MONETARY', 'LC_COLLATE', 'LC_ADDRESS', 'LC_IDENTIFICATION',
        'LC_MEASUREMENT', 'LC_NAME', 'LC_PAPER', 'LC_TELEPHONE', 'TZ',
    )
    env = {name: value for name in inherited
           if (value := os.environ.get(name)) is not None}
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    proc = exchange = None
    result = {'version_ready': False, 'version': None, 'reason_code': 'binary_version_unavailable'}
    deadline = time.monotonic() + timeout
    limit = 16384
    with tempfile.TemporaryFile() as output:
        try:
            proc = _start_process(command, stdin=subprocess.DEVNULL, stdout=output,
                                  stderr=subprocess.STDOUT, env=env)
            exchange = _ProcessExchange(proc, None)
            exchange.thread.start()
            while not exchange.done.is_set():
                if time.monotonic() >= deadline:
                    result['reason_code'] = 'binary_version_timeout'
                    break
                if os.fstat(output.fileno()).st_size > limit:
                    result['reason_code'] = 'binary_version_output_too_large'
                    break
                await asyncio.sleep(.02)
            else:
                if exchange.error is not None:
                    raise exchange.error
                output.seek(0)
                raw = output.read(limit + 1)
                if len(raw) > limit:
                    result['reason_code'] = 'binary_version_output_too_large'
                elif proc.returncode == 0 and raw.strip():
                    # Publish a recognized numeric release token only. Exit 0
                    # or arbitrary first-line prose does not establish binary
                    # readiness and must not become a diagnostic leak channel.
                    first = raw.strip().splitlines()[0].decode('ascii')
                    match = re.fullmatch(r'(tesseract|pdftoppm version) v?'
                                         r'([0-9]{1,4}(?:\.[0-9]{1,4}){1,3})', first)
                    if match:
                        result = {'version_ready': True, 'version': ' '.join(match.groups())}
        except Exception:
            pass
        finally:
            if proc is not None and _stop_process_tree(proc, exchange=exchange):
                result = {'version_ready': False, 'version': None, 'reason_code': 'cleanup_unconfirmed'}
    return result


def run_script(script, args, stdin_text=None, timeout=150, сырой=False, env=None, *,
               scripts_dir=None, _start=None, _exchange=None, _stop=None):
    """Запуск scripts/<script> и разбор его JSON-stdout.

    Возвращает dict скрипта как есть; любая деградация (таймаут, не-JSON,
    отсутствующий файл) -> {"статус": "не проверено", "причина": ...}.

    `сырой=True` — вернуть stdout строкой без разбора (досье отдаёт Markdown,
    а не JSON). Деградация и там остаётся словарём, чтобы вызывающий отличал
    документ от отказа по типу значения.
    """
    # Private dependency arguments retain MCP fault-injection seams while all
    # default callers use this one canonical owner. No public CLI exposes them.
    # env is an exact child environment, matching Popen; adapters own overlays.
    scripts_dir = str(scripts_dir) if scripts_dir is not None else str(Path(__file__).resolve().parent)
    start, exchange_type, stop = _start or _start_process, _exchange or _ProcessExchange, _stop or _stop_process_tree
    path = os.path.join(scripts_dir, script)
    if not os.path.exists(path):
        return _не_проверено("скрипт движка не найден: %s" % path, script)
    proc = None
    exchange = None
    reason = None
    cleanup = None
    try:
        proc = start(
            [sys.executable, '-X', 'utf8', path, *args],
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8',
            env=env,
        )
        exchange = exchange_type(proc, stdin_text)
        stdout, stderr = exchange.run(proc, timeout)
    except subprocess.TimeoutExpired:
        reason = ("движок не уложился в таймаут %.0f с (INN_CHECK_MCP_TIMEOUT)"
                  % (timeout))
    except _ProcessStartError as exc:
        reason = "сбой запуска движка (%s)" % exc
    except Exception as exc:
        reason = ("сбой обмена с движком" if proc is not None else "сбой запуска движка")
        reason += " (%s)" % type(exc).__name__
    finally:
        # Includes successful output, arbitrary exceptions, and cancellation.
        # KeyboardInterrupt/SystemExit propagate after the owned tree is reaped.
        if proc is not None:
            cleanup = stop(proc, exchange=exchange)
    return _script_output(script, proc, stdout if not reason else None,
                          stderr if not reason else None, reason, cleanup, сырой)
