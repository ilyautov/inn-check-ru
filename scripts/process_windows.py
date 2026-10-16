"""Windows lifetime containment using public Win32 APIs (stdlib only).

The caller creates Popen with CREATE_SUSPENDED, then calls assign_and_resume.
No engine code runs before assignment. Nested jobs require Windows 8+; an
incompatible enclosing job fails closed rather than launching uncontained.
"""
import ctypes
from ctypes import wintypes

CREATE_SUSPENDED = 0x00000004
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
TH32CS_SNAPTHREAD = 0x00000004
THREAD_SUSPEND_RESUME = 0x0002
PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100
ERROR_NO_MORE_FILES = 18


class _BasicLimits(ctypes.Structure):
    _fields_ = [('PerProcessUserTimeLimit', ctypes.c_longlong),
                ('PerJobUserTimeLimit', ctypes.c_longlong),
                ('LimitFlags', wintypes.DWORD),
                ('MinimumWorkingSetSize', ctypes.c_size_t),
                ('MaximumWorkingSetSize', ctypes.c_size_t),
                ('ActiveProcessLimit', wintypes.DWORD),
                ('Affinity', ctypes.c_size_t),
                ('PriorityClass', wintypes.DWORD),
                ('SchedulingClass', wintypes.DWORD)]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in
                ('ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
                 'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [('BasicLimitInformation', _BasicLimits),
                ('IoInfo', _IoCounters),
                ('ProcessMemoryLimit', ctypes.c_size_t),
                ('JobMemoryLimit', ctypes.c_size_t),
                ('PeakProcessMemoryUsed', ctypes.c_size_t),
                ('PeakJobMemoryUsed', ctypes.c_size_t)]


class _ThreadEntry(ctypes.Structure):
    _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD),
                ('th32ThreadID', wintypes.DWORD), ('th32OwnerProcessID', wintypes.DWORD),
                ('tpBasePri', wintypes.LONG), ('tpDeltaPri', wintypes.LONG),
                ('dwFlags', wintypes.DWORD)]


def _kernel32():
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    signatures = {
        'CreateJobObjectW': ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
        'SetInformationJobObject': ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                    wintypes.DWORD], wintypes.BOOL),
        'AssignProcessToJobObject': ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
        'OpenProcess': ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        'CloseHandle': ([wintypes.HANDLE], wintypes.BOOL),
        'CreateToolhelp32Snapshot': ([wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE),
        'Thread32First': ([wintypes.HANDLE, ctypes.POINTER(_ThreadEntry)], wintypes.BOOL),
        'Thread32Next': ([wintypes.HANDLE, ctypes.POINTER(_ThreadEntry)], wintypes.BOOL),
        'OpenThread': ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        'ResumeThread': ([wintypes.HANDLE], wintypes.DWORD),
    }
    for name, (args, result) in signatures.items():
        fn = getattr(api, name)
        fn.argtypes, fn.restype = args, result
    return api


def _winerror(operation):
    error = ctypes.WinError(ctypes.get_last_error())
    return OSError(error.errno, '%s: %s' % (operation, error))


class WindowsJob:
    """Own one non-inheritable, unnamed kill-on-close job handle."""

    def __init__(self):
        self.api = _kernel32()
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise _winerror('CreateJobObjectW')
        try:
            limits = _ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not self.api.SetInformationJobObject(
                    self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise _winerror('SetInformationJobObject')
        except BaseException:
            self.close()
            raise

    def _close_handle(self, handle):
        if not self.api.CloseHandle(handle):
            raise _winerror('CloseHandle')

    def close(self):
        """Closing the last handle terminates members, even after parent exit."""
        if self.handle is not None:
            self._close_handle(self.handle)
            self.handle = None

    def assign_and_resume(self, pid):
        """Assign a suspended child, find its sole initial thread, then resume.

        Popen closes CreateProcess's initial thread handle. Toolhelp/OpenThread
        reacquire it without relying on CPython internals or undocumented NT APIs.
        Any failure propagates to the owner, which kills/reaps the suspended child.
        """
        process = self.api.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not process:
            raise _winerror('OpenProcess')
        try:
            if not self.api.AssignProcessToJobObject(self.handle, process):
                raise _winerror('AssignProcessToJobObject')
        finally:
            self._close_handle(process)
        snapshot = self.api.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
        if snapshot == ctypes.c_void_p(-1).value:
            raise _winerror('CreateToolhelp32Snapshot')
        try:
            entry = _ThreadEntry()
            entry.dwSize = ctypes.sizeof(entry)
            found = []
            ok = self.api.Thread32First(snapshot, ctypes.byref(entry))
            while ok:
                if entry.th32OwnerProcessID == pid:
                    found.append(entry.th32ThreadID)
                entry.dwSize = ctypes.sizeof(entry)
                ok = self.api.Thread32Next(snapshot, ctypes.byref(entry))
            if ctypes.get_last_error() != ERROR_NO_MORE_FILES:
                raise _winerror('Thread32First/Next')
            if len(found) != 1:
                raise OSError('suspended engine must have exactly one initial thread')
        finally:
            self._close_handle(snapshot)
        thread = self.api.OpenThread(THREAD_SUSPEND_RESUME, False, found[0])
        if not thread:
            raise _winerror('OpenThread')
        try:
            count = self.api.ResumeThread(thread)
            if count == 0xFFFFFFFF:
                raise _winerror('ResumeThread')
            if count != 1:
                raise OSError('unexpected initial thread suspend count: %s' % count)
        finally:
            self._close_handle(thread)
