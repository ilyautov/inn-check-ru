type ScenarioKey = 'profile' | 'links' | 'changes';
type InstallKey = 'skill' | 'desktop' | 'mcp' | 'cli' | 'browser';
type Fact = { source: string; text: string; status: string; risk?: boolean };
type Scenario = { company: string; title: string; amountLabel: string; termsLabel: string; amount: string; terms: string; tone: string; label: string; verdict: string; reason: string; prompt: string; facts: Fact[]; next: string; sources: string };
const scenarios: Record<ScenarioKey, Scenario> = {
  profile: {
    company: 'КОМПАНИЯ А · УЧЕБНЫЙ ПРИМЕР', title: 'Досье компании', amountLabel: 'Профиль', termsLabel: 'Режим', amount: 'Нейтральный', terms: 'Сбор сведений', tone: 'gray', label: 'СОБРАННЫЕ СВЕДЕНИЯ', verdict: 'Факты о компании в одном досье',
    reason: 'Статус, отчётность и связи собраны по доступным источникам. Без условий сделки итоговая оценка риска не выставляется.',
    prompt: 'Собери досье компании по ИНН [ИНН]: статус, финансы, долги, судебные дела, владельцы и связи. Покажи источники, даты и что не удалось проверить.',
    facts: [{source:'ЕГРЮЛ',text:'Компания действует с 2020 года. Указаны директор и учредитель.',status:'Есть данные'},{source:'ГИР БО',text:'Выручка за 2025 год: 84 млн ₽. Чистая прибыль: 6 млн ₽.',status:'Есть данные'},{source:'Связи',text:'У директора найдена ещё одна компания. Сведения одного источника.',status:'Нужно сверить'},{source:'Арбитраж',text:'Сведения о делах не получены.',status:'Не проверено'}],
    next: 'Изучить исходные сведения, проверить связи и недоступные источники. Для оценки конкретной сделки указать её цель и условия.',
    sources: 'Все сведения учебные. В примере заданы запись ЕГРЮЛ, строки годовой отчётности и связь по директору из одного источника. Данные арбитражной картотеки не получены.'
  },
  links: {
    company: 'КОМПАНИЯ А · УЧЕБНЫЙ ПРИМЕР', title: 'Связи компании', amountLabel: 'Узлы', termsLabel: 'Основание', amount: '3', terms: 'Общий директор', tone: 'gray', label: 'СВЯЗИ ДЛЯ ПРОВЕРКИ', verdict: 'Общий директор у двух компаний',
    reason: 'В учебных данных руководитель А связан с компаниями А и Б. Одна связь получена из агрегатора и требует сверки с первичным источником.',
    prompt: 'Найди связи компании по ИНН [ИНН]: директора, учредителей и связанные компании. Покажи основание каждой связи, источник, дату и что ещё нужно подтвердить.',
    facts: [{source:'ЕГРЮЛ',text:'Руководитель А указан директором компании А.',status:'Есть данные'},{source:'Checko',text:'Руководитель А также связан с компанией Б.',status:'Нужно сверить'},{source:'ЕГРЮЛ',text:'Сведения об учредителях компании Б не получены.',status:'Не проверено'}],
    next:'Выбрать узел на схеме и изучить основание связи. Совпадение ФИО само по себе не доказывает, что это один человек.',
    sources:'Схема учебная. Связь с компанией А задана как запись ЕГРЮЛ, с компанией Б — как сведения агрегатора. Реальные запросы не выполняются.'
  },
  changes: {
    company: 'КОМПАНИЯ А · УЧЕБНЫЙ ПРИМЕР', title: 'Изменения в досье', amountLabel: 'Было', termsLabel: 'Стало', amount: '01.09.2026', terms: '28.09.2026', tone:'gray', label:'СРАВНЕНИЕ СНИМКОВ', verdict:'В новом снимке другой директор',
    reason:'Статус и адрес совпадают. Поле директора изменилось. Данные о судах отсутствуют в обоих снимках: это пробел, а не отсутствие новых дел.',
    prompt:'Сравни два сохранённых снимка компании по ИНН [ИНН]. Покажи, какие поля изменились, даты снимков и пробелы, которые нельзя трактовать как изменения.',
    facts:[{source:'ЕГРЮЛ',text:'Директор: руководитель А → руководитель Б.',status:'Изменилось'},{source:'ЕГРЮЛ',text:'Статус и адрес совпадают в двух снимках.',status:'Без изменений'},{source:'Арбитраж',text:'Данных нет в обоих снимках. Динамика неизвестна.',status:'Не проверено'}],
    next:'Проверить новую запись о руководителе в ЕГРЮЛ. Повторить сбор недостающих сведений. Снимки показывают, когда изменение обнаружили, а не точную дату события.',
    sources:'Обе даты и все значения в сравнении условные. Здесь показан принцип сравнения сохранённых снимков; расписание проверок настраивается отдельно.'
  }
};
type Install = {label:string;title:string;description:string;code:string;codeLabel:string;steps:string[];docs:string};
const installs: Record<InstallKey, Install> = {
  skill:{label:'CLAUDE CODE · CODEX · CURSOR · GEMINI CLI',title:'Добавь скилл своему агенту',description:'Выполни команду в терминале и выбери нужного агента в установщике.',code:'npx skills add ilyautov/inn-check-ru',codeLabel:'TERMINAL',steps:['Выбери агента при установке.','Открой новую сессию и попроси проверить контрагента.'],docs:'https://github.com/ilyautov/inn-check-ru#установка'},
  desktop:{label:'CLAUDE DESKTOP',title:'Открой готовое расширение',description:'Скачай файл .mcpb из последнего релиза и открой его в Claude Desktop. Это подключение MCP-инструментов.',code:'',codeLabel:'',steps:['Скачай расширение и открой двойным кликом.','Подтверди установку. Ключ Checko и свой прокси необязательны.','Открой новый чат и попроси проверить контрагента.'],docs:'https://github.com/ilyautov/inn-check-ru#claude-desktop-расширение-mcpb'},
  mcp:{label:'CURSOR · CODEX · ДРУГИЕ MCP-КЛИЕНТЫ',title:'Подключи инструменты проверки',description:'Нужен установленный uv. Добавь сервер в настройки MCP-клиента; точный формат конфигурации зависит от среды.',code:'{\n  "mcpServers": {\n    "inn-check-ru": {\n      "command": "uvx",\n      "args": ["inn-check-ru-mcp@latest"]\n    }\n  }\n}',codeLabel:'ПРИМЕР MCP-КОНФИГУРАЦИИ',steps:['Открой настройки MCP своего клиента.','Добавь команду uvx с аргументом inn-check-ru-mcp@latest.','Перезапусти подключение и проверь доступность инструментов.'],docs:'https://github.com/ilyautov/inn-check-ru/blob/main/mcp/README.md'},
  browser:{label:'CHROME · BRAVE · EDGE / MACOS · LINUX · WINDOWS',title:'Проверяй ИНН со страницы',description:'В репозитории есть браузерное расширение с локальным движком. Нужны распакованная папка extension/ и настройка Native Messaging.',code:'python3 scripts/install_native_host.py --id <ID_РАСШИРЕНИЯ> --браузер chrome',codeLabel:'ИЗ КОРНЯ РЕПОЗИТОРИЯ',steps:['Склонируй репозиторий и загрузи extension/ как распакованное расширение.','Подставь ID расширения. Для Brave замени chrome на brave, для Edge — на edge. На Windows команда та же через py вместо python3.','Перезапусти браузер и проверь подключение по инструкции. Это отдельная установка, не расширение .mcpb.'],docs:'https://github.com/ilyautov/inn-check-ru/blob/main/extension/README.md'},
  cli:{label:'ТЕРМИНАЛ · PYTHON-ДВИЖОК',title:'Проверь через командную строку',description:'Нужен установленный uv. Замени <ИНН> на номер контрагента. По умолчанию собирается нейтральная карточка фактов.',code:'uvx inn-check-ru <ИНН>',codeLabel:'TERMINAL',steps:['Подставь ИНН своего контрагента вместо <ИНН>.','Выполни команду и изучи результат вместе с непроверенными источниками.'],docs:'https://github.com/ilyautov/inn-check-ru#использование'}
};
const sourceLinks:Record<string,string>={'ЕГРЮЛ':'https://egrul.nalog.ru/','ГИР БО':'https://bo.nalog.ru/','Связи':'https://checko.ru/','Checko':'https://checko.ru/','Арбитраж':'https://kad.arbitr.ru/'};
const relations:Record<string,string>={
 company:'Компания А → руководитель А. В учебном снимке ЕГРЮЛ на 28.09.2026 указан этот директор. Идентификаторы в примере условные.',
 director:'Руководитель А соединяет два узла. В настоящем досье нужно сопоставить идентификаторы человека: совпадения ФИО недостаточно.',
 related:'Компания Б → руководитель А. Учебная связь из Checko на 28.09.2026. Первичный источник не получен; связь требует сверки.'
};
function element<T extends HTMLElement = HTMLElement>(id:string):T {const node=document.getElementById(id);if(!node)throw new Error(`Missing element: ${id}`);return node as T;}
function setText(id:string,text:string):void{element(id).textContent=text;}
function save(key:string,value:string):void{try{localStorage.setItem(`inn-check-landing:${key}`,value);}catch{/* Site remains usable without storage. */}}
function read(key:string):string|null{try{return localStorage.getItem(`inn-check-landing:${key}`);}catch{return null;}}
let activeScenario:ScenarioKey='profile';let activeInstall:InstallKey='skill';
function renderScenario(key:ScenarioKey):void{
  activeScenario=key;const item=scenarios[key];element('dossier').dataset.tone=item.tone;
  const fields:Record<string,string>={'decision-icon':'i','company-label':item.company,'dossier-title':item.title,'amount-label':item.amountLabel,'terms-label':item.termsLabel,'deal-amount':item.amount,'deal-terms':item.terms,'decision-label':item.label,'decision-title':item.verdict,'decision-reason':item.reason,'demo-prompt':item.prompt,'next-step':item.next};
  Object.entries(fields).forEach(([id,text])=>setText(id,text));
  const facts=element('demo-facts');facts.replaceChildren();
  item.facts.forEach(fact=>{
    const row=document.createElement('details');row.className='fact-detail';
    const summary=document.createElement('summary');
    const source=document.createElement('span');const description=document.createElement('p');const status=document.createElement('b');
    source.textContent=fact.source;description.textContent=fact.text;status.textContent=fact.status+' ↗';summary.append(source,description,status);
    const body=document.createElement('div');body.className='fact-evidence';
    const checked=document.createElement('p');checked.textContent='Условная дата сбора: 28.09.2026. '+(key==='changes'?'Сравнение со снимком 01.09.2026.':fact.source==='ГИР БО'?'Период отчётности: 2025 год.':'Состояние на дату учебного снимка.');
    const basis=document.createElement('p');basis.textContent=fact.status==='Не проверено'?'Ответ источника не получен. Подтверждающего документа нет.':'Основание: синтетическая запись для демонстрации интерфейса. Это не реальная выписка и не результат запроса.';
    const link=document.createElement('a');link.href=sourceLinks[fact.source]??sourceLinks['ЕГРЮЛ'];link.textContent='Открыть сервис: '+fact.source+' ↗';link.target='_blank';link.rel='noopener noreferrer';
    const note=document.createElement('small');note.textContent='Ссылка ведёт на сервис, а не на подтверждение сведений об учебной компании.';
    body.append(checked,basis,link,note);row.append(summary,body);facts.append(row);
  });
  element('relation-demo').hidden=key!=='links';
  element('report-changes-link').hidden=key!=='changes';
  const sources=element('demo-sources');sources.replaceChildren();[item.sources,'В настоящем досье нужно проверить ссылки, даты и принадлежность записей тому же контрагенту.'].forEach(text=>{const p=document.createElement('p');p.textContent=text;sources.append(p);});
  document.querySelectorAll<HTMLButtonElement>('[data-scenario]').forEach(button=>{const selected=button.dataset.scenario===key;button.classList.toggle('selected',selected);button.setAttribute('aria-pressed',String(selected));});
  const no=document.querySelector('.document-no');if(no)no.textContent=`№ 0${Object.keys(scenarios).indexOf(key)+1}`;
  save('scenario-v4',key);
  syncTerminal();
}
function renderInstall(key:InstallKey):void{
  activeInstall=key;const item=installs[key];
  const fields:Record<string,string>={'install-label':item.label,'install-title':item.title,'install-description':item.description,'install-code':item.code,'code-label':item.codeLabel};Object.entries(fields).forEach(([id,text])=>setText(id,text));
  element('code-box').hidden=key==='desktop';element('desktop-download').hidden=key!=='desktop';
  const steps=element('install-steps');steps.replaceChildren();item.steps.forEach(text=>{const li=document.createElement('li');li.textContent=text;steps.append(li);});
  element<HTMLAnchorElement>('install-docs').href=item.docs;
  document.querySelectorAll<HTMLButtonElement>('[data-install]').forEach(button=>{const selected=button.dataset.install===key;button.classList.toggle('selected',selected);button.setAttribute('aria-pressed',String(selected));});save('install',key);
}
let toastTimer:ReturnType<typeof setTimeout>|undefined;
function notify(message:string):void{const toast=element('toast');toast.textContent=message;toast.hidden=false;if(toastTimer)clearTimeout(toastTimer);toastTimer=setTimeout(()=>{toast.hidden=true;},4500);}
async function copy(text:string,sourceId:string):Promise<void>{
  try{if(!navigator.clipboard)throw new Error('Clipboard unavailable');await navigator.clipboard.writeText(text);notify('Скопировано');}
  catch{const selection=window.getSelection();const range=document.createRange();range.selectNodeContents(element(sourceId));selection?.removeAllRanges();selection?.addRange(range);notify('Текст выделен. Скопируй его вручную.');}
}
document.querySelectorAll<HTMLButtonElement>('[data-scenario]').forEach(button=>button.addEventListener('click',()=>renderScenario(button.dataset.scenario as ScenarioKey)));
document.querySelectorAll<HTMLButtonElement>('[data-install]').forEach(button=>button.addEventListener('click',()=>renderInstall(button.dataset.install as InstallKey)));
element('copy-prompt').addEventListener('click',()=>{void copy(scenarios[activeScenario].prompt,'demo-prompt');});
element('copy-command').addEventListener('click',()=>{void copy(installs[activeInstall].code,'install-code');});
document.querySelectorAll<HTMLElement>('[role="group"]').forEach(group=>group.addEventListener('keydown',(event:KeyboardEvent)=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;const buttons=[...group.querySelectorAll<HTMLButtonElement>('button')];const index=buttons.indexOf(document.activeElement as HTMLButtonElement);if(index<0)return;event.preventDefault();const next=event.key==='Home'?0:event.key==='End'?buttons.length-1:(index+(event.key==='ArrowRight'?1:-1)+buttons.length)%buttons.length;buttons[next].focus();buttons[next].click();}));

type Stage = 0 | 1 | 2;
const stageTitles = ['Начни с ИНН компании', 'Сведения из источников', 'Досье для твоего агента'];
let activeStage: Stage = 0;
let manualStage = false;
const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
const narrowScreen = window.matchMedia('(max-width: 760px)');
let motionEnabled = read('motion-v3') !== 'off';

function syncTerminal(): void {
  const item = scenarios[activeScenario];
  setText('terminal-prompt', item.prompt);
  setText('terminal-result-label', item.label);
  setText('terminal-verdict', item.verdict);
  setText('terminal-reason', item.reason);
  const list = element('terminal-sources');
  list.replaceChildren();
  item.facts.forEach(fact => {
    const row = document.createElement('div');
    const name = document.createElement('span');
    const status = document.createElement('b');
    name.textContent = fact.source;
    status.textContent = fact.status;
    status.className = fact.risk ? 'risk' : fact.status !== 'Есть данные' ? 'missing' : '';
    row.append(name, status);
    list.append(row);
  });
}
function setStage(stage: Stage, manual = false): void {
  activeStage = stage;
  if (manual) manualStage = true;
  element('terminal').dataset.stage = String(stage);
  ['stage-input','stage-sources','stage-result'].forEach((id,index) => { element(id).hidden = index !== stage; });
  setText('terminal-counter', `0${stage + 1} / 03`);
  setText('terminal-heading', stageTitles[stage]);
  document.querySelectorAll<HTMLButtonElement>('[data-stage-button]').forEach(button => {
    button.setAttribute('aria-pressed', String(Number(button.dataset.stageButton) === stage));
  });
  document.querySelectorAll<HTMLElement>('[data-chapter]').forEach(chapter => {
    chapter.classList.toggle('active', Number(chapter.dataset.chapter) === stage);
  });
  element('scroll-mode').hidden = !manualStage;
}
function syncMotion(): void {
  const enabled = motionEnabled && !reducedMotion.matches;
  const button = element<HTMLButtonElement>('motion-toggle');
  button.textContent = reducedMotion.matches ? 'Движение отключено системой' : `Движение пульта: ${enabled ? 'вкл.' : 'выкл.'}`;
  button.setAttribute('aria-pressed', String(enabled));
  button.disabled = reducedMotion.matches;
  updateScrollScene();
}
function updateScrollScene(): void {
  const terminal = element('terminal');
  if (narrowScreen.matches || reducedMotion.matches || !motionEnabled) {
    terminal.style.setProperty('--turn','0deg');
    terminal.style.setProperty('--tilt','0deg');
  }
  if (narrowScreen.matches) return;
  const layout = element('story-layout').getBoundingClientRect();
  const progress = Math.max(0, Math.min(1, (140 - layout.top) / Math.max(1, layout.height - window.innerHeight * .6)));
  if (motionEnabled && !reducedMotion.matches) {
    terminal.style.setProperty('--turn', `${-4 + progress * 7}deg`);
    terminal.style.setProperty('--tilt', `${1.5 - progress * 2}deg`);
  }
  if (!manualStage) {
    let stage: Stage = 0;
    document.querySelectorAll<HTMLElement>('[data-chapter]').forEach(chapter => {
      if (chapter.getBoundingClientRect().top < window.innerHeight * .47) stage = Number(chapter.dataset.chapter) as Stage;
    });
    if (stage !== activeStage) setStage(stage);
  }
}
let scrollFramePending = false;
function queueSceneUpdate(): void {
  if (scrollFramePending) return;
  scrollFramePending = true;
  requestAnimationFrame(() => { scrollFramePending = false; updateScrollScene(); });
}
document.querySelectorAll<HTMLButtonElement>('[data-stage-button]').forEach(button => {
  button.addEventListener('click', () => setStage(Number(button.dataset.stageButton) as Stage, true));
});
element('scroll-mode').addEventListener('click', () => { manualStage = false; element('scroll-mode').hidden = true; updateScrollScene(); });
element('motion-toggle').addEventListener('click', () => { motionEnabled = !motionEnabled; save('motion-v3', motionEnabled ? 'on' : 'off'); syncMotion(); });
window.addEventListener('scroll', queueSceneUpdate, {passive:true});
window.addEventListener('resize', queueSceneUpdate, {passive:true});
reducedMotion.addEventListener('change', syncMotion);
narrowScreen.addEventListener('change', queueSceneUpdate);
setStage(0);
syncTerminal();
syncMotion();

const savedScenario=read('scenario-v4');renderScenario(savedScenario&&Object.hasOwn(scenarios,savedScenario)?savedScenario as ScenarioKey:'profile');
const savedInstall=read('install');if(savedInstall&&Object.hasOwn(installs,savedInstall))renderInstall(savedInstall as InstallKey);

// Interactive illustrative relationships and comparison: no network requests.
document.querySelectorAll<HTMLButtonElement>('[data-node]').forEach(button=>button.addEventListener('click',()=>{
  setText('relation-explanation',relations[button.dataset.node!]);
  document.querySelectorAll<HTMLButtonElement>('[data-node]').forEach(node=>node.setAttribute('aria-pressed',String(node===button)));
}));
document.querySelectorAll<HTMLButtonElement>('[data-diff]').forEach(button=>button.addEventListener('click',()=>{
  const onlyChanged=button.dataset.diff==='changed';
  document.querySelectorAll<HTMLElement>('[data-change]').forEach(row=>row.hidden=onlyChanged&&row.dataset.change!=='changed');
  document.querySelectorAll<HTMLButtonElement>('[data-diff]').forEach(control=>control.setAttribute('aria-pressed',String(control===button)));
  setText('diff-status',onlyChanged?'Показано 1 изменение. Суды не проверены: динамика неизвестна.':'Показаны 4 поля: 1 изменение, 2 совпадения, 1 пробел.');
}));
