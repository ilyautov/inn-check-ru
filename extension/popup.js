// popup.js — окно расширения: последний результат «Проверить ИНН» и форма
// ручного блока на странице ФССП/kad.arbitr.ru. Страницу не читает: берёт URL
// активной вкладки (право activeTab — по клику на значок) и, по кнопке, снимок
// видимой части. Всё остальное вводит пользователь.
import { ПОЛЯ, источникПоUrl, сегодня, новаяОперация, сообщениеБлока,
  снимокРазрешён, таЖеСтраница } from "./lib.js";

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
    запись = (await chrome.storage.session.get("последняя")).последняя;
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
  голова.append(свет, `${р.название ? р.название + ", " : ""}ИНН ${запись.инн}, профиль «${запись.профиль}»`);
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
    вход.inputMode = "decimal";
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
  };
}

async function активнаяВкладка() {
  const [вкладка] = await chrome.tabs.query({ active: true, currentWindow: true });
  return вкладка;
}

async function сохранить(соСнимком, исходная) {
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
    let строка = `Блок записан: ${ответ.состояние}` + (ответ.причина ? ` (${ответ.причина})` : "");
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
    выбор.value = (await chrome.storage.local.get("профиль")).профиль || "нейтрально";
  } catch (e) {
    console.warn("[inn-check-ru] storage.local недоступен:", e);
  }
  выбор.addEventListener("change", () => {
    chrome.storage.local.set({ профиль: выбор.value }).catch(
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
  $("заголовок-формы").textContent = блок === "фссп" ? "Ручной блок: ФССП" : "Ручной блок: арбитражные суды";
  $("страница").textContent = new URL(вкладка.url).hostname;
  $("дата").value = сегодня();
  if (последнийИнн) $("инн").value = последнийИнн;
  for (const п of ПОЛЯ[блок]) $("поля").append(полеФормы(п));
  for (const r of document.querySelectorAll("input[name=итог]")) {
    r.addEventListener("change", () => {
      $("про-причину").hidden = собратьФорму().итог !== "не проверено";
    });
  }
  try {
    const было = (await chrome.storage.session.get("сохранение")).сохранение;
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

main();
