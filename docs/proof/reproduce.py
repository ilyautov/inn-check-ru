"""Usage: python3 reproduce.py /path/to/inn-check-ru /path/to/new-run-dir"""
from pathlib import Path
import os, sys, subprocess, json, datetime, time
if len(sys.argv)!=3:
    raise SystemExit(__doc__)
repo=Path(sys.argv[1]).resolve(); output=Path(sys.argv[2]).resolve()
if not (repo/'scripts/fetch_counterparty.py').is_file():
    raise SystemExit('Missing scripts/fetch_counterparty.py')
output.mkdir(parents=True,exist_ok=False)
child="""import os,sys
original=os.path.expanduser
cache=sys.argv[2]
def expand(path):
    prefix='~/.cache/inn-check-ru'
    return cache+path[len(prefix):] if isinstance(path,str) and path.startswith(prefix) else original(path)
os.path.expanduser=expand
sys.path.insert(0,sys.argv[1])
import fetch_counterparty
raise SystemExit(fetch_counterparty.main(['fetch_counterparty.py','7707083893','--режим','всё']))
"""
env={k:v for k,v in os.environ.items() if k in ('PATH','TMPDIR','LANG','LC_ALL')}
env.update(COUNTERPARTY_DEADLINE='90',COUNTERPARTY_IGNORE_ACCESS_CACHE='1')
started=datetime.datetime.now(datetime.timezone.utc).isoformat(); timer=time.monotonic()
with (output/'result.private.json').open('w') as stdout,(output/'stderr.txt').open('w') as stderr:
    try:
        process=subprocess.run([sys.executable,'-c',child,str(repo/'scripts'),str(output/'cache')],env=env,cwd=repo,stdout=stdout,stderr=stderr,timeout=150)
        code=process.returncode
    except subprocess.TimeoutExpired:
        code=None
(output/'execution.json').write_text(json.dumps({'started_at':started,'exit_code':code,'elapsed_seconds':round(time.monotonic()-timer,2),'note':'Private raw output may contain service tokens; redact before publishing.'},indent=2))
print('Saved run to',output)
