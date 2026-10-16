# Scoring audit reproducible evidence

Read-only source review. Commit: `3e06046`. Runtime: `/opt/homebrew/bin/python3.12`.
All mutations below happen to in-memory copies of existing synthetic fixtures; no production data or repository files are changed.

From the repository root run:

```sh
/opt/homebrew/bin/python3.12 - <<'PY'
import sys, json, copy
sys.path[:0] = ['scripts', 'eval']
import fin_scoring as fs, profiles as pf
from run_profiles_eval import fetch_clean
healthy = json.load(open('eval/fixtures/healthy.json'))
def clean():
    f = fetch_clean()
    f['егрюл']['уставный_капитал'] = '10000 руб.'
    f['финансы'] = copy.deepcopy(healthy['финансы'])
    return f

def run(name, f, profile='отсрочка', expected=None):
    fin = fs.score(f)
    r = pf.resolve(f, fin, profile)
    ids = ['ча_отрицательные_2года', 'ча_ниже_уставного_капитала',
           'транзитный_профиль', 'массовый_руководитель']
    out = {'case': name, 'staff': fin['финансовый_профиль']['численность'],
           'flags': [{k: x[k] for k in ('id', 'год', 'порог')} for x in fin['флаги']],
           'not_checked': fin['финансовый_профиль']['не_проверено'],
           'signal_states': {x['сигнал']: x['статус'] for x in r['факты']
                             if x['сигнал'] in ids},
           'light': r['светофор'], 'recommendation': r['рекомендация']}
    if expected:
        expected(fin, r, out)
    print(json.dumps(out, ensure_ascii=False))
    return out

# 1. Missing 2024 is treated as two consecutive years; false red stop.
f = clean()
y = f['финансы']['отчётность_по_годам']
y[0]['год'], y[1]['год'] = 2025, 2023
for x in y:
    x['строки']['1300'] = -100
    x['прибыль_убыток'] = -100
out = run('nonconsecutive_2025_2023', f)
assert out['light'] == '🔴'
assert any(x['id'] == 'ча_отрицательные_2года' and x['год'] == '2023–2025'
           for x in out['flags'])
assert out['not_checked'] == []

# 2a. Growth 400.001%, unknown staff silently becomes absent transit signal.
f = clean()
f['риски'].pop('численность_сотрудников')
f['финансы']['отчётность_по_годам'][0]['выручка'] = 500001
out = run('transit_staff_missing', f, 'клиент_115фз')
assert out['not_checked'] == [] and out['light'] == '🟢'
assert out['signal_states']['транзитный_профиль'] == 'отсутствует'

# 2b. Available official dump staff=0 is ignored; moving it to risks flips red.
f['дампы_фнс'] = {'численность_сотрудников': 0, 'численность_год': 2024}
out = run('transit_staff_in_fns_dump', f, 'клиент_115фз')
assert out['staff'] is None and out['light'] == '🟢'
f['риски']['численность_сотрудников'] = 0
out = run('same_staff_in_risks', f, 'клиент_115фз')
assert out['staff'] == 0 and out['light'] == '🔴'
assert out['signal_states']['транзитный_профиль'] == 'найден'

# 2c. Missing charter capital silently yields a negative comparison result.
f = clean()
f['егрюл'].pop('уставный_капитал')
out = run('share_capital_missing', f)
assert out['not_checked'] == [] and out['light'] == '🟢'
assert out['signal_states']['ча_ниже_уставного_капитала'] == 'отсутствует'

# 2d. Missing prior equity hides unknown red signal behind yellow recommendation.
f = clean()
f['финансы']['отчётность_по_годам'][0]['строки']['1300'] = -100
f['финансы']['отчётность_по_годам'][1]['строки'].pop('1300')
out = run('previous_equity_missing', f)
assert out['not_checked'] == [] and out['light'] == '🟡'
assert out['signal_states']['ча_отрицательные_2года'] == 'отсутствует'

# 3. Script source unknown signal does not prevent false green recommendation.
f = clean()
f['риски'].pop('массовый_руководитель')
out = run('mass_head_missing', f, 'клиент_115фз')
assert out['signal_states']['массовый_руководитель'] == 'не проверен'
assert out['light'] == '🟢' and 'не массовый' in out['recommendation']
PY
```

Observed output (all inline assertions passed):

```jsonl
{"case": "nonconsecutive_2025_2023", "staff": 12.0, "flags": [{"id": "ча_отрицательные_2года", "год": "2023–2025", "порог": "< 0 два года подряд"}, {"id": "ча_ниже_уставного_капитала", "год": 2025, "порог": "< уставного капитала 10 тыс. руб."}, {"id": "коэффициент_автономии", "год": 2025, "порог": "< 0.1"}, {"id": "убыток_2года", "год": "2023–2025", "порог": "< 0 два года подряд"}], "not_checked": [], "signal_states": {"ча_отрицательные_2года": "найден", "ча_ниже_уставного_капитала": "найден", "массовый_руководитель": "отсутствует", "транзитный_профиль": "отсутствует"}, "light": "🔴", "recommendation": "Отсрочку не давать: найден стоп-сигнал. Только 100% предоплата или отказ; если сделка нужна — обеспечение (залог, поручительство, аккредитив)."}
{"case": "transit_staff_missing", "staff": null, "flags": [], "not_checked": [], "signal_states": {"массовый_руководитель": "отсутствует", "транзитный_профиль": "отсутствует", "ча_отрицательные_2года": "отсутствует", "ча_ниже_уставного_капитала": "отсутствует"}, "light": "🟢", "recommendation": "Совпадений с перечнями РФМ / OFAC / EU не найдено, сведения в ЕГРЮЛ без отметок о недостоверности, руководитель не дисквалифицирован и не массовый. Стандартная идентификация; бенефициаров — по анкете клиента."}
{"case": "transit_staff_in_fns_dump", "staff": null, "flags": [], "not_checked": [], "signal_states": {"массовый_руководитель": "отсутствует", "транзитный_профиль": "отсутствует", "ча_отрицательные_2года": "отсутствует", "ча_ниже_уставного_капитала": "отсутствует"}, "light": "🟢", "recommendation": "Совпадений с перечнями РФМ / OFAC / EU не найдено, сведения в ЕГРЮЛ без отметок о недостоверности, руководитель не дисквалифицирован и не массовый. Стандартная идентификация; бенефициаров — по анкете клиента."}
{"case": "same_staff_in_risks", "staff": 0.0, "flags": [{"id": "транзитный_профиль", "год": 2024, "порог": "> 300% при численности ~0"}], "not_checked": [], "signal_states": {"транзитный_профиль": "найден", "массовый_руководитель": "отсутствует", "ча_отрицательные_2года": "отсутствует", "ча_ниже_уставного_капитала": "отсутствует"}, "light": "🔴", "recommendation": "Найден стоп-сигнал: совпадение с перечнями, недостоверность сведений, дисквалификация, массовый руководитель или транзитный профиль. Принимать без решения ответственного по 115-ФЗ нельзя; совпадение по имени — сначала ручная сверка по дате рождения."}
{"case": "share_capital_missing", "staff": 12.0, "flags": [], "not_checked": [], "signal_states": {"ча_отрицательные_2года": "отсутствует", "массовый_руководитель": "отсутствует", "транзитный_профиль": "отсутствует", "ча_ниже_уставного_капитала": "отсутствует"}, "light": "🟢", "recommendation": "Отсрочка допустима: стоп-сигналов не видно, чистые активы положительные, открытых долгов и крупных исков не найдено. Лимит и срок — по сумме сделки; снимок --save перед каждой крупной отгрузкой."}
{"case": "previous_equity_missing", "staff": 12.0, "flags": [{"id": "ча_ниже_уставного_капитала", "год": 2024, "порог": "< уставного капитала 10 тыс. руб."}, {"id": "коэффициент_автономии", "год": 2024, "порог": "< 0.1"}], "not_checked": [], "signal_states": {"ча_ниже_уставного_капитала": "найден", "ча_отрицательные_2года": "отсутствует", "массовый_руководитель": "отсутствует", "транзитный_профиль": "отсутствует"}, "light": "🟡", "recommendation": "Без отсрочки или с коротким лимитом: видны сигналы, которые сами по себе не стоп, но снижают шанс взыскать долг. Предоплата или частичная предоплата; отсрочка — только под обеспечение."}
{"case": "mass_head_missing", "staff": 12.0, "flags": [], "not_checked": [], "signal_states": {"массовый_руководитель": "не проверен", "транзитный_профиль": "отсутствует", "ча_отрицательные_2года": "отсутствует", "ча_ниже_уставного_капитала": "отсутствует"}, "light": "🟢", "recommendation": "Совпадений с перечнями РФМ / OFAC / EU не найдено, сведения в ЕГРЮЛ без отметок о недостоверности, руководитель не дисквалифицирован и не массовый. Стандартная идентификация; бенефициаров — по анкете клиента."}
```

Verified source locations:

- Nonconsecutive periods: `scripts/fin_scoring.py:179-192`, `:212-221`, `:258-285`.
- Unreported unknown financial inputs: `scripts/fin_scoring.py:222-230`, `:268-276`; classifier fallback: `scripts/profiles.py:577-583`.
- Ignored actual staff source: `scripts/fin_scoring.py:160-166`; dump collection: `scripts/fetch_counterparty.py:1500-1502`.
- Unknown scripted signal permits green: `scripts/profiles.py:713-727`, `:737-751`; live parser exposes missing details: `scripts/fetch_counterparty.py:689-726`.
- Dossier carries these facts/recommendations through to section 4: `scripts/dossier.py:924-955`.

Recommended corrections:

1. Validate years and deduplicate revised statements; only use year Y-1 for consecutive/year-over-year rules. Missing intervening years require an explicit unknown result.
2. Return per-signal evaluation status and dependencies from the scorer. Missing prior equity, charter capital, or staff must not become absence. Read staff from the verified FNS-dump source with its year/provenance; preserve zero values.
3. Prevent green or absolute recommendations for unknown required risk signals, including red severity overrides; source automation/manual origin cannot establish a missing fact.
