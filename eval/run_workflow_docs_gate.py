#!/usr/bin/env python3
"""Parse copyable workflow examples and resolve local Markdown links without execution."""
import re
import shlex
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import workflow_cli

FILES = ['README.md', 'README.en.md', 'SKILL.md', 'PUBLISHING.md', 'KNOWN_LIMITS.md',
         'mcp/README.md', 'extension/README.md', 'references/agent-workflow.md',
         'references/workflow-recovery.md', 'references/capability-matrix.md']


def main():
    errors = []
    examples = 0
    links = 0
    documents = FILES + [str(p.relative_to(ROOT)) for folder in ('commands', 'references') for p in (ROOT / folder).glob('*.md')]
    for name in dict.fromkeys(documents):
        path = ROOT / name
        if not path.is_file():
            errors.append(f'{name}: missing document')
            continue
        text = path.read_text(encoding='utf-8')
        for target in re.findall(r'(?<!!)\[[^\]\n]+\]\(([^)\s]+)(?:\s+"[^"]*")?\)', text):
            parsed = urlsplit(target.strip('<>'))
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            links += 1
            destination = (path.parent / unquote(parsed.path)).resolve()
            if not destination.exists():
                errors.append(f'{name}: broken relative link {target}')
        for block in re.findall(r'```(?:bash|sh|shell|console)\s*\n(.*?)```', text, re.DOTALL):
            for line in block.replace('\\\n', ' ').splitlines():
                line = line.strip().removeprefix('$ ')
                prefix = re.match(r'^(?:inn-check-workflow|python3?(?:\.\d+)? (?:scripts/workflow_cli\.py|-m workflow_cli))\s+', line)
                if not prefix:
                    continue
                examples += 1
                try:
                    words = shlex.split(line[prefix.end():], comments=True)
                    # Redirection belongs to the shell, never the CLI parser.
                    for symbol in ('>', '>>', '|', '&&'):
                        if symbol in words:
                            words = words[:words.index(symbol)]
                    for i, word in enumerate(words):
                        if word.startswith('$') and i and words[i - 1] in {'--expected-revision', '--revision', '--limit', '--lease-seconds', '--max-bytes', '--timeout-seconds', '--max-subjects'}:
                            words[i] = '1'
                    workflow_cli.parser().parse_args(words)
                except (ValueError, workflow_cli.workflow.WorkflowError, SystemExit) as error:
                    errors.append(f'{name}: invalid example {line}: {error}')
    if examples < 6:
        errors.append(f'only {examples} workflow examples parsed; expected at least six')
    for error in errors:
        print('FAIL', error)
    print(f'{"FAIL" if errors else "PASS"}: {examples} workflow parser examples; {links} relative links')
    return bool(errors)


if __name__ == '__main__':
    raise SystemExit(main())
