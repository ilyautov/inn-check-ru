// background.js — service worker: пункт контекстного меню «Проверить ИНН».
// Страницу не читает: берёт только выделенный пользователем текст из события
// клика. Результат кладёт в хранилище("session") (окно могло быть закрыто к
// концу проверки) и помечает значок.
import { ХОСТ, иннИзТекста, новаяОперация, своёОкно, хранилище } from "./lib.js";

const ТАЙМАУТ_МС = 120000;

globalThis.chrome?.runtime?.onInstalled?.addListener(() => {
  chrome.contextMenus.create({
    id: "inn-check",
    title: "Проверить ИНН в inn-check-ru",
    contexts: ["selection"],
  });
});

// connectNative, а не sendNativeMessage: открытый порт держит service worker
// живым (Chrome 105+), а проверка идёт до 150 с — дольше 30 с простоя, после
// которых Chrome гасит worker вместе с хостом (ревью Kimi).
function спроситьХост(сообщение) {
  return new Promise((resolve) => {
    let порт;
    let готово = false;
    const закончить = (ответ) => {
      if (готово) return;
      готово = true;
      clearTimeout(таймер);
      try { порт?.disconnect(); } catch { /* уже закрыт */ }
      resolve(ответ);
    };
    const таймер = setTimeout(
      () => закончить({ ok: false, ошибка: "хост не ответил за 120 с" }), ТАЙМАУТ_МС);
    try {
      порт = chrome.runtime.connectNative(ХОСТ);
    } catch (e) {
      закончить({ ok: false, ошибка: "хост не подключается: " + e.message });
      return;
    }
    порт.onMessage.addListener((ответ) => закончить(ответ || { ok: false, ошибка: "пустой ответ хоста" }));
    порт.onDisconnect.addListener(() => закончить({
      ok: false,
      ошибка: "хост не установлен или завершился: "
        + (chrome.runtime.lastError?.message || "соединение закрыто")
        + ". Установите: python3 scripts/install_native_host.py --id " + chrome.runtime.id,
    }));
    порт.postMessage(сообщение);
  });
}

async function сохранить(запись) {
  try {
    await хранилище("session").set({ последняя: запись });
  } catch (e) {
    console.warn("[inn-check-ru] storage.session недоступен:", e);
  }
}

globalThis.chrome?.contextMenus?.onClicked?.addListener(async (info) => {
  if (info.menuItemId !== "inn-check") return;
  const инн = иннИзТекста(info.selectionText);
  if (!инн) {
    await сохранить({ ok: false, ошибка: "в выделенном тексте нет ИНН с верной контрольной суммой" });
    chrome.action.setBadgeText({ text: "?" });
    return;
  }
  chrome.action.setBadgeText({ text: "…" });
  let профиль = "нейтрально";
  try {
    профиль = (await хранилище("local").get("профиль")).профиль || профиль;
  } catch (e) {
    console.warn("[inn-check-ru] storage.local недоступен:", e);
  }
  const ответ = await спроситьХост({ команда: "проверить", инн, профиль });
  await сохранить({ ...ответ, инн, профиль, когда: new Date().toISOString() });
  const свет = ответ.ok ? ответ.результат?.светофор : null;
  chrome.action.setBadgeText({ text: свет === "🔴" ? "!" : свет === "🟡" ? "~" : свет === "🟢" ? "ok" : "?" });
  chrome.action.setBadgeBackgroundColor({
    color: свет === "🔴" ? "#b3261e" : свет === "🟡" ? "#a86b00" : свет === "🟢" ? "#1b6e3a" : "#555555",
  });
});

// Запись ручного блока и снимка — здесь, а не в окне: окно закрывается от любого
// клика мимо, и начатая запись оборвалась бы на полпути (блок без снимка).
// Принимается только от своего окна расширения.
async function записать(сообщение, снимок) {
  const ответ = await спроситьХост(сообщение);
  let итог = { ...ответ, инн: сообщение.инн, блок: сообщение.блок, когда: new Date().toISOString() };
  if (ответ.ok && снимок) {
    const ев = await спроситьХост({
      команда: "доказательство", операция: новаяОперация(crypto.randomUUID()),
      инн: сообщение.инн, блок: сообщение.блок, url: снимок.url, дата: сообщение.дата,
      png: снимок.png, ...(сообщение.найдено ? { найдено: сообщение.найдено } : {}),
    });
    итог = { ...итог, снимок: ев };
  }
  try {
    await хранилище("session").set({ сохранение: итог });
  } catch (e) {
    console.warn("[inn-check-ru] storage.session недоступен:", e);
  }
  return итог;
}

globalThis.chrome?.runtime?.onMessage?.addListener((запрос, отправитель, ответить) => {
  const своё = своёОкно(отправитель, chrome.runtime.id, chrome.runtime.getURL("popup.html"));
  if (своё && запрос?.тип === "run" && ["run_read", "run_manual"].includes(запрос.сообщение?.команда)) {
    выполнитьRun(запрос.сообщение).then(ответить).catch(() => ответить({ ok: false, ошибка: "Не удалось сохранить запрос" }));
    return true;
  }
  if (!своё || запрос?.тип !== "записать" || запрос.сообщение?.команда !== "ручной_блок") {
    return false;
  }
  записать(запрос.сообщение, запрос.снимок || null).then(ответить);
  return true;   // ответ асинхронный
});


async function выполнитьRun(сообщение) {
  if (сообщение.команда === "run_manual") {
    const key = "run-operation-" + сообщение.run_id;
    const signature = JSON.stringify({ ...сообщение, operation_id: null });
    const previous = (await хранилище("session").get(key))[key];
    // Lost response / popup closure: exact retries retain the operation id.
    if (previous?.signature === signature) сообщение = { ...сообщение, operation_id: previous.operation_id };
    await хранилище("session").set({ [key]: { signature, operation_id: сообщение.operation_id } });
  }
  return спроситьХост(сообщение);
}
