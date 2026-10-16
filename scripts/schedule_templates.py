"""Generate scheduler templates only; never register or run a job."""
from __future__ import annotations

import datetime as dt
import plistlib
import shlex
import subprocess
from xml.sax.saxutils import escape


def render(argv, *, interval_days, hour=4, minute=0, label='tech.aifrontier.inn-check-ru.refresh',
           start_at=None, timeout_seconds=120):
    if not argv or any(not isinstance(x, str) or not x or '\n' in x or '\r' in x for x in argv):
        raise ValueError('argv must contain nonempty single-line strings')
    if not isinstance(interval_days, int) or interval_days < 1 or not 0 <= hour < 24 or not 0 <= minute < 60:
        raise ValueError('invalid scheduler interval or clock')
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 6060:
        raise ValueError('invalid execution timeout')
    if start_at is None:
        start = dt.datetime.combine(dt.datetime.now().date() + dt.timedelta(days=1),
                                    dt.time(hour, minute)).astimezone()
    else:
        start = dt.datetime.fromisoformat(start_at.replace('Z', '+00:00'))
        if start.tzinfo is None:
            raise ValueError('start_at requires timezone')
        local = start.astimezone()
        hour, minute = local.hour, local.minute
    boundary = start.astimezone(dt.timezone.utc).isoformat().replace('+00:00', 'Z')
    # Daily is portable and conservatively precedes every supported TTL margin.
    # Day-of-month */N is unsafe across short months and DST; do not emit it.
    cron = ('%d %d * * * %s' % (minute, hour, shlex.join(argv))).replace('%', r'\%')
    plist = plistlib.dumps({'Label': label, 'ProgramArguments': argv,
                           'StartCalendarInterval': {'Hour': hour, 'Minute': minute}}, sort_keys=False).decode()
    arguments = subprocess.list2cmdline(argv[1:])
    windows = ('<?xml version="1.0" encoding="UTF-8"?>'
               '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
               '<Triggers><CalendarTrigger><StartBoundary>%s</StartBoundary>'
               '<Enabled>true</Enabled><ScheduleByDay><DaysInterval>1</DaysInterval>'
               '</ScheduleByDay></CalendarTrigger></Triggers>'
               '<Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>'
               '<StartWhenAvailable>true</StartWhenAvailable>'
               '<ExecutionTimeLimit>PT%dS</ExecutionTimeLimit>'
               '<RestartOnFailure><Interval>PT1M</Interval><Count>2</Count></RestartOnFailure>'
               '</Settings><Actions><Exec>'
               '<Command>%s</Command><Arguments>%s</Arguments></Exec></Actions></Task>'
               % (escape(boundary), timeout_seconds, escape(argv[0]), escape(arguments)))
    return {'schema_version': 1, 'installed': False, 'argv': list(argv), 'cron': cron,
            'plist': plist, 'windows_xml': windows, 'maximum_interval_days': 1,
            'requested_interval_days': interval_days}
