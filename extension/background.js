// background.js — service worker: пункт контекстного меню «Проверить ИНН».
// Страницу не читает: берёт только выделенный пользователем текст из события
// клика. Результат кладёт в chrome.storage.session (окно могло быть закрыто к
// концу проверки) и помечает значок.
import { ХОСТ, иннИзТекста, новаяОперация, своёОкно } from "./lib.js";

const ТАЙМАУТ_МС = 120000;

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: "inn-check",
    title: "Проверить ИНН в inn-check-ru",
    contexts: ["selection"],
  });
});

function спроситьХост(сообщение) {
  return new Promise((resolve) => {
    const таймер = setTimeout(
      () => resolve({ ok: false, ошибка: "хост не ответил за 120 с" }), ТАЙМАУТ_МС);
    chrome.runtime.sendNativeMessage(ХОСТ, сообщение, (ответ) => {
      clearTimeout(таймер);
      if (chrome.runtime.lastError) {
        resolve({ ok: false, ошибка: "хост не установлен или не отвечает: "
          + chrome.runtime.lastError.message
          + ". Установите: python3 scripts/install_native_host.py --id " + chrome.runtime.id });
      } else {
        resolve(ответ || { ok: false, ошибка: "пустой ответ хоста" });
      }
    });
  });
}

async function сохранить(запись) {
  try {
    await chrome.storage.session.set({ последняя: запись });
  } catch (e) {
    console.warn("[inn-check-ru] storage.session недоступен:", e);
  }
}

chrome.contextMenus.onClicked.addListener(async (info) => {
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
    профиль = (await chrome.storage.local.get("профиль")).профиль || профиль;
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
    await chrome.storage.session.set({ сохранение: итог });
  } catch (e) {
    console.warn("[inn-check-ru] storage.session недоступен:", e);
  }
  return итог;
}

chrome.runtime.onMessage.addListener((запрос, отправитель, ответить) => {
  const своё = своёОкно(отправитель, chrome.runtime.id, chrome.runtime.getURL("popup.html"));
  if (!своё || запрос?.тип !== "записать" || запрос.сообщение?.команда !== "ручной_блок") {
    return false;
  }
  записать(запрос.сообщение, запрос.снимок || null).then(ответить);
  return true;   // ответ асинхронный
});
