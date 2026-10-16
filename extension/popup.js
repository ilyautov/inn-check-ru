// popup.js — окно расширения: последний результат «Проверить ИНН» и форма
// ручного блока на странице ФССП/kad.arbitr.ru. Страницу не читает: берёт URL
// активной вкладки (право activeTab — по клику на значок) и, по кнопке, снимок
// видимой части. Всё остальное вводит пользователь.
import { ПОЛЯ, РАЗБОР_ТЕКСТА, источникПоUrl, сегодня, новаяОперация, сообщениеБлока,
  снимокРазрешён, таЖеСтраница, хранилище, runIdВерен, выбраннаяПроверка, сообщениеПроверки } from "./lib.js";

const $ = (id) => document.getElementById(id);
const ПРОФИЛИ = ["нейтрально", "отсрочка", "предоплата", "подрядчик", "доля", "клиент_115фз",
  "ндс_вычет", "самопроверка", "получаю_предоплату", "тендер", "цепочка_поставки"];

function текст(el, строка, класс) {
  el.textContent = строка;
  el.className = класс || "";
}

async function показатьПоследнюю() {
  let запись = null;
  try {
    запись = (await хранилище("session").get("последняя")).последняя;
  } catch (e) {
    console.warn("[inn-check-ru] storage.session недоступен:", e);
  }
  const el = $("результат");
  if (!запись) return null;
  el.replaceChildren();
  if (!запись.ok) {
    текст(el, запись.ошибка || "проверка не удалась", "ошибка");
    return запись.инн || null;
  }
  const р = запись.результат || {};
  const голова = document.createElement("div");
  const свет = document.createElement("span");
  свет.className = "свет";
  свет.textContent = р.светофор || "—";
  голова.append(свет, `${р.название ? р.название + ", " : ""}ИНН ${запись.инн}, профиль «${запись.профиль}»`
    + (запись.когда ? `, ${new Date(запись.когда).toLocaleString("ru-RU")}` : ""));
  el.append(голова);
  const пояснение = document.createElement("div");
  пояснение.className = "тише";
  пояснение.textContent = р.светофор ? (р.рекомендация || "")
    : "Светофор не выдан: " + (р.рекомендация || р.причина || "проверка неполная");
  el.append(пояснение);
  const нп = р.не_проверено || р.источники_обязательные_не_проверены || [];
  if (нп.length) {
    const d = document.createElement("div");
    d.className = "тише";
    d.textContent = "Не проверено: " + нп.join(", ");
    el.append(d);
  }
  return запись.инн;
}

function полеФормы(п) {
  const обёртка = document.createElement("div");
  const id = "поле-" + п.имя;
  const метка = document.createElement("label");
  метка.htmlFor = id;
  метка.textContent = п.подпись + (п.обязательно ? " *" : "");
  let вход;
  if (п.тип === "да-нет") {
    вход = document.createElement("select");
    for (const [v, t] of [["", "—"], ["да", "да"], ["нет", "нет"]]) {
      const o = document.createElement("option");
      o.value = v;
      o.textContent = t;
      вход.append(o);
    }
  } else {
    вход = document.createElement("input");
    вход.type = "text";
    вход.inputMode = п.тип === "целое" ? "numeric" : "decimal";
  }
  вход.id = id;
  вход.dataset.имя = п.имя;
  обёртка.append(метка, вход);
  return обёртка;
}

function собратьФорму() {
  const поля = {};
  for (const el of $("поля").querySelectorAll("[data-имя]")) поля[el.dataset.имя] = el.value;
  return {
    инн: $("инн").value.replace(/\s/g, ""),
    итог: document.querySelector("input[name=итог]:checked").value,
    поля,
    найдено: $("найдено").value,
    параметры: $("параметры").value,
    причина: $("причина").value,
    дата: $("дата").value,
    текст: $("текст").value,
  };
}

async function активнаяВкладка() {
  if (!globalThis.chrome?.tabs) return null;
  const [вкладка] = await chrome.tabs.query({ active: true, currentWindow: true });
  return вкладка;
}

async function сохранить(соСнимком, исходная) {
  if ($("режим-записи").value === "run") return сохранитьПроверку(исходная);
  const итог = $("итог-формы");
  const форма = собратьФорму();
  const { сообщение, ошибки } = сообщениеБлока(форма, исходная.url,
    новаяОперация(crypto.randomUUID()));
  const запрет = соСнимком ? снимокРазрешён(форма.инн, $("без-фио").checked) : null;
  if (ошибки.length || запрет) {
    текст(итог, [...ошибки, запрет].filter(Boolean).join("; "), "ошибка");
    return;
  }
  for (const b of document.querySelectorAll("button")) b.disabled = true;
  try {
    // блок — о той странице, что была открыта при открытии окна; снимок — той же
    // вкладки и страницы до и ПОСЛЕ захвата (страница могла смениться за время
    // захвата, а captureVisibleTab снимает то, что активно сейчас)
    if (!таЖеСтраница(await активнаяВкладка(), исходная)) {
      текст(итог, "вкладка или страница сменилась — откройте окно заново", "ошибка");
      return;
    }
    let снимок = null;
    if (соСнимком) {
      const png = await chrome.tabs.captureVisibleTab(исходная.windowId, { format: "png" });
      if (!таЖеСтраница(await активнаяВкладка(), исходная)) {
        текст(итог, "страница сменилась во время снимка — снимок не сохранён", "ошибка");
        return;
      }
      снимок = { png, url: исходная.url };
    }
    текст(итог, "Записываю… окно можно закрыть: запись доведёт фоновый процесс", "тише");
    const ответ = await chrome.runtime.sendMessage({ тип: "записать", сообщение, снимок });
    if (!ответ?.ok) {
      текст(итог, ответ?.ошибка || "блок не записан", "ошибка");
      return;
    }
    let строка = `Блок записан: ${ответ.состояние}` + (ответ.причина ? ` (${ответ.причина})` : "")
      + (ответ.найдено ? `; ${ответ.найдено}` : "");
    if (ответ.снимок) {
      строка += ответ.снимок.ok ? `; снимок: ${ответ.снимок.статус}, версия ${ответ.снимок.версия}`
        : `; снимок не сохранён: ${ответ.снимок.ошибка}`;
    }
    текст(итог, строка, "тише");
  } catch (e) {
    текст(итог, "сбой: " + e.message, "ошибка");
  } finally {
    for (const b of document.querySelectorAll("button")) b.disabled = false;
  }
}

async function main() {
  const выбор = $("профиль");
  for (const п of ПРОФИЛИ) {
    const o = document.createElement("option");
    o.value = п;
    o.textContent = п;
    выбор.append(o);
  }
  try {
    выбор.value = (await хранилище("local").get("профиль")).профиль || "нейтрально";
  } catch (e) {
    console.warn("[inn-check-ru] storage.local недоступен:", e);
  }
  выбор.addEventListener("change", () => {
    хранилище("local").set({ профиль: выбор.value }).catch(
      (e) => console.warn("[inn-check-ru] профиль не сохранён:", e));
  });

  const последнийИнн = await показатьПоследнюю();
  const вкладка = await активнаяВкладка();
  const блок = вкладка && источникПоUrl(вкладка.url || "");
  if (!блок) {
    $("не-реестр").hidden = false;
    return;
  }
  $("форма").hidden = false;
  $("заголовок-формы").textContent = { "фссп": "Ручной блок: ФССП", "суды": "Ручной блок: арбитражные суды",
    "залоги": "Ручной блок: реестр залогов" }[блок];
  $("про-текст").hidden = !РАЗБОР_ТЕКСТА.includes(блок);
  $("страница").textContent = вкладка.url;
  привязка(вкладка);
  $("дата").value = сегодня();
  // ИНН не подставляется сам: последняя проверка могла быть о другой компании,
  // и блок этой страницы лёг бы под чужой ИНН (ревью Kimi). Только по кнопке.
  if (последнийИнн) {
    const кнопка = document.createElement("button");
    кнопка.type = "button";
    кнопка.className = "вторая";
    кнопка.textContent = `Подставить ИНН последней проверки: ${последнийИнн}`;
    кнопка.addEventListener("click", () => { $("инн").value = последнийИнн; });
    $("инн").after(кнопка);
  }
  for (const п of ПОЛЯ[блок]) $("поля").append(полеФормы(п));
  for (const r of document.querySelectorAll("input[name=итог]")) {
    r.addEventListener("change", () => {
      $("про-причину").hidden = собратьФорму().итог !== "не проверено";
    });
  }
  try {
    const было = (await хранилище("session").get("сохранение")).сохранение;
    if (было) {
      текст($("итог-формы"), `Последняя запись (${было.блок}, ИНН ${было.инн}): `
        + (было.ok ? было.состояние : "ошибка — " + было.ошибка)
        + (было.снимок ? (было.снимок.ok ? `; снимок: ${было.снимок.статус}` : `; снимок не сохранён: ${было.снимок.ошибка}`) : ""),
        было.ok ? "тише" : "ошибка");
    }
  } catch (e) {
    console.warn("[inn-check-ru] storage.session недоступен:", e);
  }
  $("сохранить").addEventListener("click", () => сохранить(false, вкладка));
  $("со-снимком").addEventListener("click", () => сохранить(true, вкладка));
}

main().catch((e) => текст($("результат"), "Не удалось открыть окно: " + e.message, "ошибка"));


let выбранная = null;
let runЗанят = false;
function сбросВыбора() {
  выбранная = null;
  $("run-confirm").checked = false;
  $("run-confirm").disabled = true;
  $("выбранная-проверка").hidden = true;
}
const ВИДЫ_ШАГОВ = { manual: "ручная проверка", key: "подключение доступа к источнику",
  cache: "подготовка локальных данных", network: "повторный запрос к источнику",
  refresh: "обновление данных", resolve_conflict: "проверка противоречивых сведений" };
const ПРИЧИНЫ_ШАГОВ = { missing_required_field: "не хватает обязательных сведений",
  source_unavailable: "источник ещё не проверен или недоступен", stale: "данные устарели",
  conflict: "источники противоречат друг другу", missing_search_scope: "не указаны параметры поиска",
  incomplete_coverage: "поиск охватывает не все результаты", manual_required: "требуется проверка человеком" };
const СОСТОЯНИЯ_ПРОВЕРКИ = { ok: "операция завершена", partial: "данные неполные",
  needs_input: "нужны дополнительные действия", cancelled: "проверка остановлена", error: "ошибка" };
function показатьВыбор(run, вкладка) {
  const action = run.pending_actions.find((a) => a.source_id === источникПоUrl(вкладка.url));
  текст($("выбранная-проверка"), `${run.название}\nИНН ${run.инн}\nПрофиль: ${run.профиль} · Ревизия: ${run.revision}\nrun_id: ${run.run_id}\nСтраница: ${вкладка.url}`
    + (action ? `\nСледующий шаг: ${ВИДЫ_ШАГОВ[action.kind] || "дополнительное действие"}; ${ПРИЧИНЫ_ШАГОВ[action.reason_code] || "Диагностика: " + action.reason_code}.` : "\nДля этого источника нет ожидаемого шага; результат будет добавлен новой ревизией."));
  $("выбранная-проверка").hidden = false;
}
function привязка(вкладка) {
  $("режим-записи").addEventListener("change", () => {
    const run = $("режим-записи").value === "run";
    сбросВыбора();
    $("run-binding").hidden = !run;
    $("legacy-outcome").hidden = run;
    $("со-снимком").hidden = run;
    $("без-фио").parentElement.hidden = run;
    $("про-текст").hidden = run || !РАЗБОР_ТЕКСТА.includes(источникПоUrl(вкладка.url));
    $("сохранить").textContent = run ? "Сохранить исход в выбранную проверку" : "Сохранить блок";
    $("про-причину").hidden = !run && собратьФорму().итог !== "не проверено";
  });
  $("run-id").addEventListener("input", сбросВыбора);
  $("run-load").addEventListener("click", async () => {
    if (runЗанят) return;
    сбросВыбора();
    const id = $("run-id").value.trim();
    if (!runIdВерен(id)) return текст($("run-status"), "Укажите run_id в формате UUID", "ошибка");
    runЗанят = true;
    $("run-load").disabled = true;
    try {
      const ответ = await chrome.runtime.sendMessage({ тип: "run", сообщение: { команда: "run_read", run_id: id } });
      if ($("run-id").value.trim() !== id || $("режим-записи").value !== "run") return;
      выбранная = выбраннаяПроверка(ответ, id);
      if (!выбранная) throw new Error(ответ?.error?.message || ответ?.ошибка || "Проверка не найдена");
      показатьВыбор(выбранная, вкладка);
      $("run-confirm").disabled = false;
      текст($("run-status"), "Сверьте реквизиты и ИНН формы, затем подтвердите привязку.", "тише");
    } catch (e) { текст($("run-status"), e.message, "ошибка"); }
    finally { runЗанят = false; $("run-load").disabled = false; }
  });
}
async function сохранитьПроверку(вкладка) {
  if (runЗанят) return;
  const форма = { ...собратьФорму(), текст: "", outcome: $("run-outcome").value,
    pagination_complete: $("pagination-complete").checked };
  const { сообщение, ошибки } = сообщениеПроверки(форма, вкладка.url, выбранная,
    $("run-confirm").checked, crypto.randomUUID());
  if (ошибки.length) return текст($("итог-формы"), ошибки.join("; "), "ошибка");
  runЗанят = true;
  for (const b of document.querySelectorAll("button")) b.disabled = true;
  try {
    if (!таЖеСтраница(await активнаяВкладка(), вкладка)) throw new Error("Страница сменилась — откройте окно заново");
    текст($("итог-формы"), "Сохраняю исход ручного шага…", "тише");
    const ответ = await chrome.runtime.sendMessage({ тип: "run", сообщение });
    if (ответ?.status === "error" && ["revision_conflict", "operation_conflict"].includes(ответ.error?.code)) {
      сбросВыбора();
      const current = await chrome.runtime.sendMessage({ тип: "run", сообщение: { команда: "run_read", run_id: сообщение.run_id } });
      const fresh = выбраннаяПроверка(current, сообщение.run_id);
      if (fresh) показатьВыбор(fresh, вкладка);
      throw new Error("Проверка изменилась. " + (fresh ? "Загружена актуальная ревизия для просмотра. " : "Не удалось перечитать актуальную ревизию. ") + "Нажмите «Загрузить выбранную проверку», сверьте данные и подтвердите привязку заново.");
    }
    if (!ответ || ответ.status === "error" || !ответ.revision) throw new Error(ответ?.error?.message || ответ?.ошибка || "Нет подтверждения записи. Повторите тот же запрос.");
    const outcome = ответ.data?.metadata?.manual_outcomes?.[источникПоUrl(вкладка.url)];
    текст($("итог-формы"), `Сохранено в ${ответ.run_id}, ревизия ${ответ.revision}: `
      + ({ completed: "ручной шаг завершён", incomplete: "поиск не завершён; пробел остаётся", declined: "отказ от проверки; пробел остаётся" }[outcome] || "исход сохранён")
      + `. Состояние проверки: ${СОСТОЯНИЯ_ПРОВЕРКИ[ответ.status] || "уточните в сохранённой проверке"}.`, "тише");
    сбросВыбора();
  } catch (e) { текст($("итог-формы"), e.message, "ошибка"); }
  finally { runЗанят = false; for (const b of document.querySelectorAll("button")) b.disabled = false; }
}
