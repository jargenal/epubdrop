function extractBookIdFromPath() {
  const m = (window.location.pathname || "").match(/\/read\/([^/]+)\//);
  return m ? m[1] : "";
}

const pageData = document.body.dataset || {};
const BOOK_ID = pageData.bookId || extractBookIdFromPath();
const SAVED_SECTION = Number(pageData.savedSection || 0);
const SAVED_BLOCK = Number(pageData.savedBlock || 0);
const SAVED_OFFSET = Number(pageData.savedOffset || 0);
const SAVED_BLOCK_ID = Number(pageData.savedBlockId || 0);
const SAVED_ANCHOR_TEXT = pageData.savedAnchorText || "";
const SAVED_ANCHOR_CHAR = Number(pageData.savedAnchorChar || 0);
const readerScroll = document.getElementById("readerScroll");
const progressText = document.getElementById("progressText");
const chapterText = document.getElementById("chapterText");
const tocOverlay = document.getElementById("tocOverlay");
const openTocBtn = document.getElementById("openToc");
const closeTocBtn = document.getElementById("closeToc");
const tocButtons = q("[data-toc-jump]");
const TOC_DRAWER_KEY = "readerTocDrawerOpen";
const bookmarksOverlay = document.getElementById("bookmarksOverlay");
const openBookmarksBtn = document.getElementById("openBookmarks");
const closeBookmarksBtn = document.getElementById("closeBookmarks");
const bookmarksList = document.getElementById("bookmarksList");
const toggleBookmarkBtn = document.getElementById("toggleBookmark");
const searchPanel = document.getElementById("searchPanel");
const toggleSearchBtn = document.getElementById("toggleSearch");
const searchInput = document.getElementById("searchInput");
const searchPrevBtn = document.getElementById("searchPrev");
const searchNextBtn = document.getElementById("searchNext");
const searchClearBtn = document.getElementById("searchClear");
const searchCount = document.getElementById("searchCount");
const stickyHeader = document.querySelector("header.sticky");
const saveStatus = document.getElementById("saveStatus");
const PROGRESS_LOCAL_KEY = BOOK_ID ? `readerProgress:${BOOK_ID}` : "";

function q(sel, root=document){ return Array.from(root.querySelectorAll(sel)); }

// Config
const overlay = document.getElementById("configOverlay");
const openConfig = document.getElementById("openConfig");
const closeConfig = document.getElementById("closeConfig");
const fontSize = document.getElementById("fontSize");
const fontSizeVal = document.getElementById("fontSizeVal");
const fontFamily = document.getElementById("fontFamily");
const themeMode = document.getElementById("themeMode");
const themeModeVal = document.getElementById("themeModeVal");
const themeToggle = document.getElementById("themeToggle");
const themeMedia = window.matchMedia("(prefers-color-scheme: dark)");
const STORAGE_KEYS = {
  size: "readerFontSize",
  family: "readerFontFamily",
  theme: "appThemeMode",
};

function openDrawer(){ overlay.classList.remove("hidden"); document.body.style.overflow="hidden"; }
function closeDrawer(){ overlay.classList.add("hidden"); document.body.style.overflow=""; }
openConfig.addEventListener("click", openDrawer);
closeConfig.addEventListener("click", closeDrawer);
overlay.addEventListener("click", (e)=>{ if(e.target===overlay) closeDrawer(); });

function applyTypography() {
  const size = parseInt(fontSize.value, 10);
  const family = fontFamily.value;
  fontSizeVal.textContent = String(size);
  readerScroll.style.fontSize = `${size}px`;
  readerScroll.style.fontFamily = family;
  saveSettings();
}
fontSize.addEventListener("input", applyTypography);
fontFamily.addEventListener("change", applyTypography);

function saveSettings() {
  localStorage.setItem(STORAGE_KEYS.size, fontSize.value);
  localStorage.setItem(STORAGE_KEYS.family, fontFamily.value);
  localStorage.setItem(STORAGE_KEYS.theme, themeMode.value);
}

function loadSettings() {
  const savedSize = localStorage.getItem(STORAGE_KEYS.size);
  const savedFamily = localStorage.getItem(STORAGE_KEYS.family);
  const savedTheme = localStorage.getItem(STORAGE_KEYS.theme);
  const legacyTheme = localStorage.getItem("readerThemeMode");
  if (savedSize) fontSize.value = savedSize;
  if (savedFamily) fontFamily.value = savedFamily;
  if (savedTheme) themeMode.value = savedTheme;
  else if (legacyTheme) themeMode.value = legacyTheme;
}

themeMode.addEventListener("change", () => {
  if (typeof window.applyAppTheme === "function") window.applyAppTheme(themeMode.value);
  saveSettings();
});

if (themeToggle) {
  themeToggle.addEventListener("click", () => {
    // handled by theme.js; keep state sync after it runs
    setTimeout(() => {
      const stored = localStorage.getItem(STORAGE_KEYS.theme) || "system";
      themeMode.value = stored;
      if (typeof window.applyAppTheme === "function") window.applyAppTheme(stored);
      saveSettings();
    }, 0);
  });
}

// Progreso
function updateProgress() {
  const top = useContainerScroll ? readerScroll.scrollTop : window.scrollY;
  const max = useContainerScroll
    ? readerScroll.scrollHeight - readerScroll.clientHeight
    : document.documentElement.scrollHeight - window.innerHeight;
  const pct = max > 0 ? Math.min(100, Math.max(0, Math.round((top / max) * 100))) : 0;
  progressText.textContent = `${pct}%`;
}
function getCookie(name) {
  const cookieValue = document.cookie
    .split(";")
    .map(c => c.trim())
    .find(c => c.startsWith(`${name}=`));
  return cookieValue ? decodeURIComponent(cookieValue.split("=")[1]) : "";
}

function getCsrfToken() {
  const tokenFromCookie = getCookie("csrftoken");
  if (tokenFromCookie) return tokenFromCookie;
  const tokenInput = document.querySelector("input[name=csrfmiddlewaretoken]");
  return tokenInput ? tokenInput.value : "";
}

const rows = q(".row");
const POS_EPSILON = 1;
const sections = q("section[data-section-index]");
const VIRTUAL_WINDOW = 2;
const sectionFirstRow = new Map();
const sectionBodies = new Map();
const sectionPlaceholders = new Map();
let observedRow = null;
let lastSavedKey = "";
let saveTimer = null;
let activeSectionIndex = 0;
let bookmarks = JSON.parse(document.getElementById("initial-bookmarks")?.textContent || "[]");
let searchHits = [];
let searchCursor = -1;
let useContainerScroll = true;
let rowObserver = null;
let translationObserver = null;
const translationRequests = new Set();
const translatedRows = new Set();
let lazyTranslationScanTimer = null;

function getHeaderOffset() {
  const h = stickyHeader ? Math.round(stickyHeader.getBoundingClientRect().height) : 0;
  return Math.max(8, h + 8);
}

function recalcScrollMode() {
  if (!readerScroll) {
    useContainerScroll = false;
    return;
  }
  useContainerScroll = readerScroll.scrollHeight > readerScroll.clientHeight + 8;
}

function setSaveStatus(state, text) {
  if (!saveStatus) return;
  saveStatus.dataset.state = state;
  if (text) {
    saveStatus.setAttribute("aria-label", text);
    saveStatus.setAttribute("title", text);
  }
}

function readLocalProgress() {
  if (!PROGRESS_LOCAL_KEY) return null;
  try {
    const raw = localStorage.getItem(PROGRESS_LOCAL_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return null;
    return parsed;
  } catch (_) {
    return null;
  }
}

function saveLocalProgress(payload) {
  if (!PROGRESS_LOCAL_KEY || !payload) return;
  try {
    localStorage.setItem(PROGRESS_LOCAL_KEY, JSON.stringify({
      section_index: Number(payload.s || 0),
      block_index: Number(payload.b || 0),
      block_offset_percent: Number(payload.offsetPct || 0),
      block_id: Number(payload.blockId || 0),
      anchor_text: String(payload.anchorText || ""),
      anchor_char_index: Number(payload.anchorChar || 0),
      progress_percent: Number(payload.pct || 0),
      saved_at: Date.now(),
    }));
  } catch (_) {
    // ignore storage quota errors
  }
}

sections.forEach((section) => {
  const idx = Number(section.dataset.sectionIndex || 0);
  const firstRow = section.querySelector(".row");
  if (firstRow) sectionFirstRow.set(idx, firstRow);

  const wrapper = document.createElement("div");
  wrapper.className = "section-body";
  while (section.firstChild) {
    wrapper.appendChild(section.firstChild);
  }
  section.appendChild(wrapper);
  sectionBodies.set(idx, wrapper);

  const placeholder = document.createElement("div");
  placeholder.className = "section-virtual-placeholder hidden";
  placeholder.dataset.virtualPlaceholder = String(idx);
  section.insertBefore(placeholder, wrapper);
  sectionPlaceholders.set(idx, placeholder);
});

function collapseSection(idx) {
  const section = sections.find((s) => Number(s.dataset.sectionIndex || -1) === idx);
  const body = sectionBodies.get(idx);
  const placeholder = sectionPlaceholders.get(idx);
  if (!section || !body || !placeholder) return;
  if (section.classList.contains("section-collapsed")) return;
  const height = Math.max(120, Math.round(body.getBoundingClientRect().height));
  placeholder.style.height = `${height}px`;
  placeholder.classList.remove("hidden");
  section.classList.add("section-collapsed");
  observedRow = null;
}

function expandSection(idx) {
  const section = sections.find((s) => Number(s.dataset.sectionIndex || -1) === idx);
  const placeholder = sectionPlaceholders.get(idx);
  if (!section || !placeholder) return;
  section.classList.remove("section-collapsed");
  placeholder.classList.add("hidden");
}

function updateVirtualSections() {
  const minIdx = Math.max(0, activeSectionIndex - VIRTUAL_WINDOW);
  const maxIdx = Math.min(sections.length - 1, activeSectionIndex + VIRTUAL_WINDOW);
  sections.forEach((section) => {
    const idx = Number(section.dataset.sectionIndex || 0);
    if (idx >= minIdx && idx <= maxIdx) expandSection(idx);
    else collapseSection(idx);
  });
}

function ensureSectionExpanded(sectionIdx) {
  expandSection(sectionIdx);
}

function setTocActive(sectionIndex) {
  tocButtons.forEach((btn) => {
    btn.dataset.active = Number(btn.dataset.tocJump || -1) === sectionIndex ? "1" : "0";
  });
}

function openTocDrawer() {
  tocOverlay.classList.remove("hidden");
  document.body.classList.add("toc-panel-open");
  localStorage.setItem(TOC_DRAWER_KEY, "1");
}

function closeTocDrawer() {
  tocOverlay.classList.add("hidden");
  document.body.classList.remove("toc-panel-open");
  localStorage.setItem(TOC_DRAWER_KEY, "0");
}

function getTopRow() {
  if (!rows.length) return null;
  if (observedRow && observedRow.offsetParent !== null) return observedRow;
  const top = useContainerScroll
    ? readerScroll.scrollTop + POS_EPSILON
    : window.scrollY + getHeaderOffset() + POS_EPSILON;
  for (const row of rows) {
    if (row.offsetParent === null) continue;
    const rowTop = useContainerScroll ? row.offsetTop : row.getBoundingClientRect().top + window.scrollY;
    const rowBottom = rowTop + row.offsetHeight;
    if (rowTop <= top && rowBottom > top) return row;
  }
  for (const row of rows) {
    if (row.offsetParent === null) continue;
    const rowTop = useContainerScroll ? row.offsetTop : row.getBoundingClientRect().top + window.scrollY;
    if (rowTop > top) return row;
  }
  const visible = rows.filter((r) => r.offsetParent !== null);
  return visible.length ? visible[visible.length - 1] : rows[rows.length - 1];
}

function currentTopRowOrNull() {
  return getTopRow();
}

function updateBookmarkButtonState() {
  const row = currentTopRowOrNull();
  if (!row) return;
  const blockId = Number(row.dataset.blockId || 0);
  const exists = bookmarks.some((b) => Number(b.block_id) === blockId);
  toggleBookmarkBtn.classList.toggle("bookmark-active", exists);
  const label = exists ? "Posición marcada. Clic para desmarcar" : "Marcar o desmarcar posición actual";
  toggleBookmarkBtn.setAttribute("title", label);
  toggleBookmarkBtn.setAttribute("aria-label", label);
}

function jumpToRow(row, smooth = true) {
  if (!row) return;
  const sectionIdx = Number(row.dataset.s || 0);
  ensureSectionExpanded(sectionIdx);
  if (useContainerScroll) {
    readerScroll.scrollTo({ top: Math.max(0, row.offsetTop - 8), behavior: smooth ? "smooth" : "auto" });
    return;
  }
  const top = Math.max(0, row.getBoundingClientRect().top + window.scrollY - getHeaderOffset());
  window.scrollTo({ top, behavior: smooth ? "smooth" : "auto" });
}

function bookmarkLabelFromRow(row) {
  const sec = Number(row.dataset.s || 0);
  const blk = Number(row.dataset.b || 0);
  const sectionTitle = sections[sec]?.dataset?.sectionTitle || `Sección ${sec + 1}`;
  return `${sectionTitle} · bloque ${blk + 1}`;
}

function renderBookmarks() {
  if (!bookmarksList) return;
  if (!bookmarks.length) {
    bookmarksList.innerHTML = '<div class="text-xs text-slate-500">No hay bookmarks guardados.</div>';
    return;
  }
  bookmarksList.innerHTML = "";
  for (const bm of bookmarks) {
    const row = rows.find(r => Number(r.dataset.blockId || 0) === Number(bm.block_id));
    const item = document.createElement("div");
    item.className = "rounded-lg border border-slate-200 bg-white p-3";

    const title = document.createElement("div");
    title.className = "text-sm font-semibold text-slate-700";
    title.textContent = bm.label || `Sección ${Number(bm.section_index) + 1}, bloque ${Number(bm.block_index) + 1}`;

    const actions = document.createElement("div");
    actions.className = "mt-2 flex items-center justify-between";
    const go = document.createElement("button");
    go.type = "button";
    go.className = "rounded-md border border-slate-200 bg-white px-3 py-1 text-xs font-semibold text-slate-700 hover:bg-slate-50";
    go.textContent = "Ir";
    go.addEventListener("click", () => {
      jumpToRow(row);
      if (window.innerWidth < 1024) closeBookmarksDrawer();
    });

    const del = document.createElement("button");
    del.type = "button";
    del.className = "rounded-md border border-slate-200 bg-white px-3 py-1 text-xs font-semibold text-slate-700 hover:bg-slate-50";
    del.textContent = "Eliminar";
    del.addEventListener("click", async () => {
      const res = await fetch(`/api/books/${BOOK_ID}/bookmarks/${bm.id}/delete/`, {
        method: "POST",
        headers: { "X-CSRFToken": getCsrfToken() },
      });
      if (res.ok) {
        bookmarks = bookmarks.filter(x => Number(x.id) !== Number(bm.id));
        renderBookmarks();
        updateBookmarkButtonState();
      }
    });
    actions.appendChild(go);
    actions.appendChild(del);
    item.appendChild(title);
    item.appendChild(actions);
    bookmarksList.appendChild(item);
  }
}

async function toggleBookmarkForCurrentRow() {
  const row = currentTopRowOrNull();
  if (!row) return;
  const blockId = Number(row.dataset.blockId || 0);
  const existing = bookmarks.find((b) => Number(b.block_id) === blockId);
  if (existing) {
    const res = await fetch(`/api/books/${BOOK_ID}/bookmarks/${existing.id}/delete/`, {
      method: "POST",
      headers: { "X-CSRFToken": getCsrfToken() },
    });
    if (res.ok) {
      bookmarks = bookmarks.filter((b) => Number(b.id) !== Number(existing.id));
      renderBookmarks();
      updateBookmarkButtonState();
    }
    return;
  }

  const body = new URLSearchParams({
    block_id: String(blockId),
    label: bookmarkLabelFromRow(row),
  });
  const res = await fetch(`/api/books/${BOOK_ID}/bookmarks/create/`, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-www-form-urlencoded",
      "X-CSRFToken": getCsrfToken(),
    },
    body: body.toString(),
  });
  if (!res.ok) return;
  const data = await res.json();
  if (data?.bookmark) {
    bookmarks = [data.bookmark, ...bookmarks.filter((b) => Number(b.id) !== Number(data.bookmark.id))];
    renderBookmarks();
    updateBookmarkButtonState();
  }
}

function openBookmarksDrawer() {
  bookmarksOverlay.classList.remove("hidden");
  document.body.classList.add("toc-panel-open");
  renderBookmarks();
}

function closeBookmarksDrawer() {
  bookmarksOverlay.classList.add("hidden");
  document.body.classList.remove("toc-panel-open");
}

function clearSearchHighlights() {
  rows.forEach((row) => row.classList.remove("search-hit"));
  searchHits = [];
  searchCursor = -1;
  if (searchCount) searchCount.textContent = "0 resultados";
}

function runSearch() {
  const term = (searchInput?.value || "").trim().toLowerCase();
  clearSearchHighlights();
  if (!term) return;
  searchHits = rows.filter((row) => (row.textContent || "").toLowerCase().includes(term));
  searchHits.forEach((row) => row.classList.add("search-hit"));
  if (searchCount) searchCount.textContent = `${searchHits.length} resultados`;
  if (!searchHits.length) return;
  searchCursor = 0;
  jumpToRow(searchHits[0]);
}

function stepSearch(delta) {
  if (!searchHits.length) return;
  searchCursor = (searchCursor + delta + searchHits.length) % searchHits.length;
  jumpToRow(searchHits[searchCursor]);
}

function updateChapter() {
  const anchorTop = useContainerScroll ? 40 : getHeaderOffset() + 32;
  let active = sections[0];
  for (const section of sections) {
    const top = useContainerScroll
      ? section.getBoundingClientRect().top - readerScroll.getBoundingClientRect().top
      : section.getBoundingClientRect().top;
    if (top <= anchorTop) active = section;
    else break;
  }
  if (active) {
    const title = active.getAttribute("data-section-title") || "Sección";
    chapterText.textContent = title;
    activeSectionIndex = Number(active.dataset.sectionIndex || 0);
    setTocActive(activeSectionIndex);
  }
}

function _normalizeText(value) {
  return String(value || "").replace(/\s+/g, " ").trim();
}

function _anchorSearchToken(anchorText) {
  const normalized = _normalizeText(anchorText).toLowerCase();
  if (!normalized) return "";
  return normalized.slice(0, 72);
}

function extractAnchorFromRow(row) {
  if (!row) return { text: "", charIndex: 0 };
  const textNodes = [];
  const walker = document.createTreeWalker(row, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      if (!_normalizeText(node.nodeValue || "")) return NodeFilter.FILTER_REJECT;
      return NodeFilter.FILTER_ACCEPT;
    },
  });
  while (walker.nextNode()) textNodes.push(walker.currentNode);
  if (!textNodes.length) return { text: "", charIndex: 0 };

  const viewportTop = useContainerScroll
    ? readerScroll.getBoundingClientRect().top + 24
    : getHeaderOffset() + 20;

  let selected = textNodes[0];
  let selectedRect = null;
  let bestDistance = Number.POSITIVE_INFINITY;

  for (const node of textNodes) {
    const range = document.createRange();
    range.selectNodeContents(node);
    const rect = range.getBoundingClientRect();
    const distance = Math.abs(rect.top - viewportTop);
    if (rect.bottom >= viewportTop && rect.top <= viewportTop) {
      selected = node;
      selectedRect = rect;
      break;
    }
    if (distance < bestDistance) {
      bestDistance = distance;
      selected = node;
      selectedRect = rect;
    }
  }

  const normalizedNodeText = _normalizeText(selected.nodeValue || "");
  const anchorText = normalizedNodeText.slice(0, 180);
  if (!anchorText) return { text: "", charIndex: 0 };

  let charIndex = 0;
  try {
    const rowText = _normalizeText(row.textContent || "");
    const idx = rowText.toLowerCase().indexOf(anchorText.toLowerCase());
    charIndex = idx >= 0 ? idx : 0;
  } catch (_) {
    charIndex = 0;
  }

  if (!selectedRect) {
    const range = document.createRange();
    range.selectNodeContents(selected);
    selectedRect = range.getBoundingClientRect();
  }
  return { text: anchorText, charIndex };
}

function restorePreciseAnchor(row, anchorText) {
  const token = _anchorSearchToken(anchorText);
  if (!row || !token) return false;

  const walker = document.createTreeWalker(row, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      if (!_normalizeText(node.nodeValue || "")) return NodeFilter.FILTER_REJECT;
      return NodeFilter.FILTER_ACCEPT;
    },
  });

  let targetNode = null;
  while (walker.nextNode()) {
    const text = _normalizeText(walker.currentNode.nodeValue || "").toLowerCase();
    if (text.includes(token)) {
      targetNode = walker.currentNode;
      break;
    }
  }
  if (!targetNode) return false;

  const range = document.createRange();
  range.selectNodeContents(targetNode);
  const rect = range.getBoundingClientRect();
  if (!rect || rect.height === 0) return false;

  if (useContainerScroll) {
    const containerTop = readerScroll.getBoundingClientRect().top;
    const scrollTo = readerScroll.scrollTop + (rect.top - containerTop) - 24;
    readerScroll.scrollTop = Math.max(0, scrollTo);
  } else {
    const scrollTo = window.scrollY + rect.top - getHeaderOffset() - 12;
    window.scrollTo({ top: Math.max(0, scrollTo), behavior: "auto" });
  }
  return true;
}

function schedulePreciseRestore(row, source) {
  if (!row || !source) return;
  const anchorText = String(source.anchor_text || "");
  if (!_normalizeText(anchorText)) return;

  const attempts = [0, 350, 1000];
  attempts.forEach((delay) => {
    window.setTimeout(() => {
      ensureSectionExpanded(Number(row.dataset.s || 0));
      restorePreciseAnchor(row, anchorText);
    }, delay);
  });

  const imgs = q("img", row).filter((img) => !img.complete);
  imgs.slice(0, 6).forEach((img) => {
    img.addEventListener("load", () => {
      restorePreciseAnchor(row, anchorText);
    }, { once: true });
  });
}

function buildProgressPayload() {
  const row = getTopRow();
  if (!row) return null;
  const s = row.dataset.s || "0";
  const b = row.dataset.b || "0";
  const blockId = row.dataset.blockId || "0";
  const top = useContainerScroll ? readerScroll.scrollTop : window.scrollY;
  const max = useContainerScroll
    ? readerScroll.scrollHeight - readerScroll.clientHeight
    : document.documentElement.scrollHeight - window.innerHeight;
  const pct = max > 0 ? Math.min(100, Math.max(0, Math.round((top / max) * 100))) : 0;
  const rowTop = useContainerScroll ? row.offsetTop : row.getBoundingClientRect().top + window.scrollY;
  const rowHeight = Math.max(1, row.offsetHeight);
  const viewTop = useContainerScroll ? readerScroll.scrollTop : window.scrollY + getHeaderOffset();
  const offsetPct = Math.min(1, Math.max(0, (viewTop - rowTop) / rowHeight));
  const anchor = extractAnchorFromRow(row);
  return {
    s,
    b,
    pct,
    offsetPct,
    blockId,
    anchorText: anchor.text || "",
    anchorChar: Number(anchor.charIndex || 0),
  };
}

function saveProgress(useBeacon = false) {
  if (!BOOK_ID) {
    setSaveStatus("error", "Sin ID de libro");
    return;
  }
  const payload = buildProgressPayload();
  if (!payload) return;
  const { s, b, pct, offsetPct, blockId, anchorText, anchorChar } = payload;
  saveLocalProgress(payload);
  setSaveStatus("saving", "Guardando...");
  const key = `${blockId}:${pct}:${offsetPct.toFixed(3)}:${String(anchorText || "").slice(0, 32)}`;
  if (key === lastSavedKey) {
    setSaveStatus("saved", "Guardado");
    return;
  }
  lastSavedKey = key;

  const url = `/api/books/${BOOK_ID}/progress/`;
  if (useBeacon && navigator.sendBeacon) {
    const beaconBody = new FormData();
    beaconBody.append("csrfmiddlewaretoken", getCsrfToken());
    beaconBody.append("section_idx", String(s));
    beaconBody.append("block_idx", String(b));
    beaconBody.append("progress_percent", String(pct));
    beaconBody.append("block_offset_percent", String(offsetPct));
    beaconBody.append("block_id", String(blockId));
    beaconBody.append("anchor_text", String(anchorText || ""));
    beaconBody.append("anchor_char_index", String(anchorChar || 0));
    navigator.sendBeacon(url, beaconBody);
    setSaveStatus("saved", "Guardado");
    return;
  }

  const body = new URLSearchParams({
    csrfmiddlewaretoken: getCsrfToken(),
    section_idx: s,
    block_idx: b,
    progress_percent: String(pct),
    block_offset_percent: String(offsetPct),
    block_id: String(blockId),
    anchor_text: String(anchorText || ""),
    anchor_char_index: String(anchorChar || 0),
  });

  fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-www-form-urlencoded",
      "X-CSRFToken": getCsrfToken(),
    },
    credentials: "same-origin",
    keepalive: true,
    body: body.toString(),
  })
    .then((res) => {
      if (!res.ok) {
        throw new Error(`save_progress_http_${res.status}`);
      }
      setSaveStatus("saved", "Guardado");
    })
    .catch(() => {
      setSaveStatus("error", "Guardado local");
    });
}

function scheduleSave() {
  if (saveTimer) clearTimeout(saveTimer);
  saveTimer = setTimeout(saveProgress, 1200);
}

function recreateRowObserver() {
  if (!rows.length) return;
  if (rowObserver) {
    rowObserver.disconnect();
    rowObserver = null;
  }
  rowObserver = new IntersectionObserver((entries) => {
    let best = null;
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      if (!best || entry.intersectionRatio > best.intersectionRatio) {
        best = entry;
      }
    }
    if (best) observedRow = best.target;
  }, {
    root: useContainerScroll ? readerScroll : null,
    threshold: [0.25, 0.5, 0.75, 1.0],
  });
  rows.forEach((r) => rowObserver.observe(r));
}

function getTranslatedCell(row) {
  return row ? row.querySelector(".translated-content") : null;
}

function markTranslationState(row, state) {
  const cell = getTranslatedCell(row);
  if (cell) cell.dataset.translationState = state;
}

async function requestLazyTranslation(row) {
  if (!BOOK_ID || !row) return;
  const blockId = row.dataset.blockId || `${row.dataset.s}:${row.dataset.b}`;
  const cell = getTranslatedCell(row);
  if (!cell || cell.dataset.translationState !== "pending") return;
  if (translationRequests.has(blockId) || translatedRows.has(blockId)) return;

  translationRequests.add(blockId);
  markTranslationState(row, "loading");

  const sectionIdx = Number(row.dataset.s || 0);
  const blockIdx = Number(row.dataset.b || 0);
  try {
    const res = await fetch(`/api/books/${BOOK_ID}/translate-block/${sectionIdx}/${blockIdx}/`, {
      method: "GET",
      credentials: "same-origin",
      headers: { "Accept": "application/json" },
    });
    if (!res.ok) throw new Error(`translate_block_http_${res.status}`);
    const data = await res.json();
    if (data && data.ok && typeof data.translated_html === "string" && data.translated_html.trim()) {
      cell.innerHTML = data.translated_html;
      optimizeImgs(cell);
      translatedRows.add(blockId);
      markTranslationState(row, data.fallback ? "fallback" : "ready");
      recalcScrollMode();
      return;
    }
    markTranslationState(row, "fallback");
  } catch (_) {
    markTranslationState(row, "fallback");
  } finally {
    translationRequests.delete(blockId);
  }
}

function recreateTranslationObserver() {
  if (translationObserver) {
    translationObserver.disconnect();
    translationObserver = null;
  }
  if (!rows.length || !("IntersectionObserver" in window)) return;
  translationObserver = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (!entry.isIntersecting) return;
      requestLazyTranslation(entry.target);
    });
  }, {
    root: useContainerScroll ? readerScroll : null,
    rootMargin: "900px 0px",
    threshold: 0.01,
  });
  rows.forEach((row) => {
    const cell = getTranslatedCell(row);
    if (cell && cell.dataset.translationState === "pending") {
      translationObserver.observe(row);
    }
  });
}

function requestVisibleLazyTranslations() {
  rows.forEach((row) => {
    const cell = getTranslatedCell(row);
    if (!cell || cell.dataset.translationState !== "pending") return;
    const rect = row.getBoundingClientRect();
    const rootRect = useContainerScroll
      ? readerScroll.getBoundingClientRect()
      : { top: 0, bottom: window.innerHeight };
    if (rect.bottom >= rootRect.top - 900 && rect.top <= rootRect.bottom + 900) {
      requestLazyTranslation(row);
    }
  });
}

function scheduleLazyTranslationScan() {
  if (lazyTranslationScanTimer) return;
  lazyTranslationScanTimer = window.setTimeout(() => {
    lazyTranslationScanTimer = null;
    requestVisibleLazyTranslations();
  }, 180);
}

// Imágenes
function optimizeImgs(root=document) {
  q("img", root).forEach(img => { img.loading = "lazy"; img.decoding = "async"; });
}

document.addEventListener("DOMContentLoaded", async () => {
  recalcScrollMode();
  loadSettings();
  applyTypography();
  if (typeof window.applyAppTheme === "function") window.applyAppTheme(themeMode.value);
  saveSettings();
  updateProgress();
  optimizeImgs(document);
  updateChapter();
  updateVirtualSections();
  recalcScrollMode();
  recreateRowObserver();
  recreateTranslationObserver();
  scheduleLazyTranslationScan();
  setTocActive(activeSectionIndex);
  updateBookmarkButtonState();
  renderBookmarks();

  if (localStorage.getItem(TOC_DRAWER_KEY) === "1" && window.innerWidth >= 1024) {
    openTocDrawer();
  }

  if (document.fonts && document.fonts.ready) {
    try { await document.fonts.ready; } catch {}
  }

  if (rows.length) {
    const localProgress = readLocalProgress();
    const hasServerProgress = SAVED_BLOCK_ID > 0 || SAVED_SECTION > 0 || SAVED_BLOCK > 0 || SAVED_OFFSET > 0;
    const hasLocalProgress = localProgress && (
      Number(localProgress.block_id || 0) > 0
      || Number(localProgress.section_index || 0) > 0
      || Number(localProgress.block_index || 0) > 0
      || Number(localProgress.block_offset_percent || 0) > 0
    );
    const source = hasServerProgress ? {
      block_id: SAVED_BLOCK_ID,
      section_index: SAVED_SECTION,
      block_index: SAVED_BLOCK,
      block_offset_percent: SAVED_OFFSET,
      anchor_text: SAVED_ANCHOR_TEXT,
      anchor_char_index: SAVED_ANCHOR_CHAR,
    } : (hasLocalProgress ? localProgress : null);

    const target = source && Number(source.block_id || 0) > 0
      ? rows.find(r => r.dataset.blockId == String(source.block_id))
      : source
        ? rows.find(r => r.dataset.s == String(source.section_index || 0) && r.dataset.b == String(source.block_index || 0))
        : null;
      if (target) {
        ensureSectionExpanded(Number(target.dataset.s || 0));
        const targetOffset = Math.max(0, Math.min(1, Number(source.block_offset_percent || 0)));
        if (useContainerScroll) {
          const scrollTo = target.offsetTop + (target.offsetHeight * targetOffset);
          readerScroll.scrollTop = Math.max(0, scrollTo);
        } else {
          const docTop = target.getBoundingClientRect().top + window.scrollY;
          const scrollTo = docTop + (target.offsetHeight * targetOffset) - getHeaderOffset();
          window.scrollTo({ top: Math.max(0, scrollTo), behavior: "auto" });
        }
        schedulePreciseRestore(target, source);
      }
    }
  updateProgress();
  updateChapter();
  updateVirtualSections();
  scheduleLazyTranslationScan();
  saveProgress();
  setSaveStatus("saved", "Guardado");
});

if (openTocBtn) openTocBtn.addEventListener("click", openTocDrawer);
if (closeTocBtn) closeTocBtn.addEventListener("click", closeTocDrawer);
if (tocOverlay) {
  tocOverlay.addEventListener("click", (e) => {
    if (e.target === tocOverlay) closeTocDrawer();
  });
}

tocButtons.forEach((btn) => {
  btn.addEventListener("click", () => {
    const idx = Number(btn.dataset.tocJump || 0);
    const row = sectionFirstRow.get(idx);
    if (!row) return;
    if (useContainerScroll) {
      readerScroll.scrollTo({ top: Math.max(0, row.offsetTop - 8), behavior: "smooth" });
    } else {
      const top = Math.max(0, row.getBoundingClientRect().top + window.scrollY - getHeaderOffset());
      window.scrollTo({ top, behavior: "smooth" });
    }
    setTocActive(idx);
    if (window.innerWidth < 1024) closeTocDrawer();
  });
});

if (openBookmarksBtn) openBookmarksBtn.addEventListener("click", openBookmarksDrawer);
if (closeBookmarksBtn) closeBookmarksBtn.addEventListener("click", closeBookmarksDrawer);
if (bookmarksOverlay) {
  bookmarksOverlay.addEventListener("click", (e) => {
    if (e.target === bookmarksOverlay) closeBookmarksDrawer();
  });
}
if (toggleBookmarkBtn) toggleBookmarkBtn.addEventListener("click", toggleBookmarkForCurrentRow);

if (toggleSearchBtn) {
  toggleSearchBtn.addEventListener("click", () => {
    searchPanel.classList.toggle("hidden");
    if (!searchPanel.classList.contains("hidden")) searchInput?.focus();
  });
}
if (searchInput) searchInput.addEventListener("input", runSearch);
if (searchPrevBtn) searchPrevBtn.addEventListener("click", () => stepSearch(-1));
if (searchNextBtn) searchNextBtn.addEventListener("click", () => stepSearch(1));
if (searchClearBtn) {
  searchClearBtn.addEventListener("click", () => {
    if (searchInput) searchInput.value = "";
    clearSearchHighlights();
  });
}

readerScroll.addEventListener("scroll", () => {
  if (!useContainerScroll) return;
  updateProgress();
  updateChapter();
  updateVirtualSections();
  scheduleLazyTranslationScan();
  updateBookmarkButtonState();
  scheduleSave();
});

window.addEventListener("scroll", () => {
  if (useContainerScroll) return;
  updateProgress();
  updateChapter();
  updateVirtualSections();
  scheduleLazyTranslationScan();
  updateBookmarkButtonState();
  scheduleSave();
}, { passive: true });

window.addEventListener("resize", () => {
  const prev = useContainerScroll;
  recalcScrollMode();
  if (prev !== useContainerScroll) {
    recreateRowObserver();
    recreateTranslationObserver();
  }
  requestVisibleLazyTranslations();
});

const backLink = document.getElementById("backLink");
if (backLink) {
  backLink.addEventListener("click", () => {
    saveProgress(true);
  });
}

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden") {
    saveProgress(true);
  }
});

window.addEventListener("beforeunload", () => {
  saveProgress(true);
});

setInterval(() => {
  saveProgress();
}, 10000);
