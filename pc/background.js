// При запуске браузера: привести вкладки к списку из tabs.json (агент перезаписывает его перед каждым запуском).
//  — Brave сам восстанавливает закреплённые вкладки, а у ещё не вошедших сайтов адрес уходит на страницу входа —
//    по адресу их не узнать. Поэтому закреплённые всегда пересоздаём: старые закреплённые закрываем,
//    закреплённые из списка открываем заново — ровно по списку, без дублей;
//  — обычные вкладки из списка узнаём по самой странице (адрес без параметров): уже открытую не дублируем,
//    лишние копии закрываем; вкладки не из списка не трогаем;
//  — пустую стартовую вкладку закрываем.

const sleep = ms => new Promise(r => setTimeout(r, ms));
const isBlank = u => !u || /^(chrome|brave|edge):\/\/(newtab|new-tab-page)/.test(u) || u === 'about:blank';

function pageKey(u) {
  try {
    let url = new URL(u);
    // Выбор аккаунта / вход Google: настоящая страница — в continue
    if (url.hostname === 'accounts.google.com' && url.searchParams.get('continue')) url = new URL(url.searchParams.get('continue'));
    const path = url.pathname.replace(/^\/drive\/u\/\d+\//, '/drive/').replace(/\/$/, '');
    return (url.hostname.toLowerCase() + path);
  } catch (e) {
    return '';
  }
}
// exact — та же страница (её копии закрываются); inside — страница внутри неё: годится как «уже открыта», не закрывается
const exactTab = (tab, item) => pageKey(tab.pendingUrl || tab.url) === item.match;
const insideTab = (tab, item) => pageKey(tab.pendingUrl || tab.url).startsWith(item.match + '/');

chrome.runtime.onStartup.addListener(() => { setupWorkTabs(); syncBookmarks(); });
// После установки или обновления расширения (кнопка ⟳ в brave://extensions) — закладки сразу
chrome.runtime.onInstalled.addListener(() => { syncBookmarks(); });

// Закладки из bookmarks.json (пишет агент) — на панель закладок, с папками и по порядку.
// Уже есть такая (та же страница или то же название) — не дублируем, а обновляем; свои закладки пользователя не трогаем.
async function syncBookmarks() {
  let list;
  try {
    list = await (await fetch(chrome.runtime.getURL('bookmarks.json'), { cache: 'no-store' })).json();
  } catch (e) {
    return;
  }
  if (!Array.isArray(list) || !list.length) return;
  const tree = await chrome.bookmarks.getTree();
  const bar = tree[0].children[0]; // «Панель закладок»
  const norm = s => String(s || '').trim().toLowerCase();
  async function sync(parentId, nodes) {
    for (let i = 0; i < nodes.length; i++) {
      const node = nodes[i];
      const children = await chrome.bookmarks.getChildren(parentId);
      let bm;
      if (node.children) {
        bm = children.find(c => !c.url && norm(c.title) === norm(node.title)) ||
             await chrome.bookmarks.create({ parentId, title: node.title });
        await sync(bm.id, node.children);
      } else {
        bm = children.find(c => c.url && pageKey(c.url) === pageKey(node.url)) ||
             children.find(c => c.url && norm(c.title) === norm(node.title));
        if (bm) {
          if (bm.url !== node.url || bm.title !== node.title) bm = await chrome.bookmarks.update(bm.id, { title: node.title, url: node.url });
        } else {
          bm = await chrome.bookmarks.create({ parentId, title: node.title, url: node.url });
        }
      }
      try { await chrome.bookmarks.move(bm.id, { parentId, index: i }); } catch (e) { /* не страшно */ }
    }
  }
  await sync(bar.id, list);
}

async function setupWorkTabs() {
  let list;
  try {
    list = await (await fetch(chrome.runtime.getURL('tabs.json'), { cache: 'no-store' })).json();
  } catch (e) {
    return; // нет tabs.json — ничего не делаем
  }
  // Ждём окно и пока восстановление вкладок не успокоится (число вкладок не меняется 2 с, максимум ~12 с)
  let wins = [];
  for (let i = 0; i < 40 && !wins.length; i++) {
    wins = await chrome.windows.getAll({ windowTypes: ['normal'] });
    if (!wins.length) await sleep(250);
  }
  const winId = wins.length ? wins[0].id : (await chrome.windows.create({})).id;
  let count = -1, stable = 0;
  for (let i = 0; i < 48 && stable < 8; i++) {
    const n = (await chrome.tabs.query({})).length;
    stable = n === count ? stable + 1 : 0;
    count = n;
    await sleep(250);
  }

  const tabs = await chrome.tabs.query({});
  const oldPinned = tabs.filter(t => t.pinned);
  const normal = tabs.filter(t => !t.pinned);
  const used = new Set();
  const keep = [];
  for (const item of list) {
    if (item.pinned) {
      keep.push(await chrome.tabs.create({ windowId: winId, url: item.url, pinned: true, active: false }));
      continue;
    }
    const exact = normal.filter(t => !used.has(t.id) && exactTab(t, item));
    let tab = exact[0] || normal.find(t => !used.has(t.id) && insideTab(t, item));
    exact.forEach(t => used.add(t.id));
    if (tab) used.add(tab.id);
    for (const extra of exact.filter(t => t !== tab)) {
      try { await chrome.tabs.remove(extra.id); } catch (e) { /* уже закрыта */ }
    }
    if (!tab) tab = await chrome.tabs.create({ windowId: winId, url: item.url, active: false });
    keep.push(tab);
  }
  // Старые закреплённые — закрываем (новые уже открыты, окно не опустеет)
  for (const t of oldPinned) {
    try { await chrome.tabs.remove(t.id); } catch (e) { /* уже закрыта */ }
  }
  // Порядок как в списке (закреплённые идут первыми — так их и задаём в config.json)
  for (let i = 0; i < keep.length; i++) {
    try { await chrome.tabs.move(keep[i].id, { windowId: winId, index: i }); } catch (e) { /* вкладка в другом окне */ }
  }
  if (keep.length) await chrome.tabs.update(keep[0].id, { active: true });
  await sleep(800);
  for (const t of normal) {
    if (!used.has(t.id) && isBlank(t.pendingUrl || t.url)) {
      try { await chrome.tabs.remove(t.id); } catch (e) { /* уже закрыта */ }
    }
  }
}
