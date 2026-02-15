const dz = document.getElementById("dropzone");
const fileInput = document.getElementById("fileInput");
const fileMeta = document.getElementById("fileMeta");
const fileName = document.getElementById("fileName");
const fileSize = document.getElementById("fileSize");

function formatBytes(bytes) {
  if (!bytes && bytes !== 0) return "";
  const sizes = ["B", "KB", "MB", "GB"];
  const i = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), sizes.length - 1);
  const val = bytes / Math.pow(1024, i);
  return `${val.toFixed(val >= 10 || i === 0 ? 0 : 1)} ${sizes[i]}`;
}

function setFile(f) {
  if (!f) return;
  fileName.textContent = f.name || "archivo.epub";
  fileSize.textContent = formatBytes(f.size);
  fileMeta.classList.remove("hidden");
}

if (dz) {
  dz.addEventListener("click", () => fileInput.click());

  dz.addEventListener("dragover", (e) => {
    e.preventDefault();
    dz.classList.add("border-[#60a5fa]");
  });

  dz.addEventListener("dragleave", () => {
    dz.classList.remove("border-[#60a5fa]");
  });

  dz.addEventListener("drop", (e) => {
    e.preventDefault();
    dz.classList.remove("border-[#60a5fa]");
    const files = e.dataTransfer.files;
    if (files && files.length) {
      fileInput.files = files;
      setFile(files[0]);
    }
  });

  fileInput.addEventListener("change", (e) => {
    const f = e.target.files && e.target.files[0];
    setFile(f);
  });
}

const progressCount = document.getElementById("progressCount");
const progressList = document.getElementById("progressList");
const progressEmpty = document.getElementById("progressEmpty");
const hasProgressItems = Boolean(document.querySelector("[data-progress-item]"));
const wantsAutoRefresh = sessionStorage.getItem("epubdropAutoRefresh") === "1";
let refreshTimer = null;

function setProgressCount(count) {
  if (progressCount) {
    progressCount.textContent = `${count} en proceso`;
  }
}

function buildProgressCard(book) {
  const wrapper = document.createElement("div");
  wrapper.className = "glass rounded-2xl border border-[#cbd5e1] px-4 py-3 shadow-sm";
  wrapper.dataset.progressItem = "1";
  wrapper.dataset.bookId = book.id;

  const header = document.createElement("div");
  header.className = "flex flex-wrap items-center justify-between gap-3";

  const left = document.createElement("div");
  const title = document.createElement("div");
  title.className = "text-sm font-semibold text-[#0f172a]";
  title.textContent = book.title || "Libro";
  const authors = document.createElement("div");
  authors.className = "text-xs text-[#475569]";
  authors.textContent = book.authors || "Autor desconocido";
  const statusText = document.createElement("div");
  statusText.className = "mt-1 text-xs font-semibold text-red-600";
  if (book.status === "failed") {
    statusText.textContent = "Fallo al traducir";
  } else {
    statusText.classList.add("hidden");
  }
  left.appendChild(title);
  left.appendChild(authors);
  left.appendChild(statusText);

  const right = document.createElement("div");
  right.className = "text-xs font-semibold text-[#2563eb]";
  right.textContent = book.status === "failed" ? "Error" : `${book.progress_percent}%`;

  header.appendChild(left);
  header.appendChild(right);

  const counts = document.createElement("div");
  counts.className = "mt-1 text-[11px] text-[#475569]";
  counts.textContent = `${book.translated_blocks} / ${book.total_blocks} bloques`;

  const barWrap = document.createElement("div");
  barWrap.className = "mt-3 h-2 w-full overflow-hidden rounded-full bg-[#e0e7ff]";
  const bar = document.createElement("div");
  bar.className = `h-full rounded-full ${book.status === "failed" ? "bg-red-400" : "bg-[#1e3a8a]"}`;
  bar.style.width = `${book.progress_percent}%`;
  barWrap.appendChild(bar);

  wrapper.appendChild(header);
  wrapper.appendChild(counts);
  wrapper.appendChild(barWrap);
  return wrapper;
}

function renderProgress(books) {
  const list = progressList || document.createElement("div");
  if (!progressList) {
    list.id = "progressList";
    list.className = "grid gap-4";
    progressEmpty?.replaceWith(list);
  }
  list.innerHTML = "";
  books.forEach((book) => {
    list.appendChild(buildProgressCard(book));
  });
  setProgressCount(books.length);
  if (books.length === 0) {
    if (progressEmpty) {
      progressEmpty.classList.remove("hidden");
      if (progressList) progressList.classList.add("hidden");
    } else {
      const empty = document.createElement("div");
      empty.id = "progressEmpty";
      empty.className = "rounded-2xl border border-dashed border-[#cbd5e1] bg-white/70 px-6 py-4 text-xs text-[#475569]";
      empty.textContent = "No hay traducciones en progreso.";
      list.replaceWith(empty);
    }
  } else if (progressList) {
    progressList.classList.remove("hidden");
    if (progressEmpty) progressEmpty.classList.add("hidden");
  }
}

async function refreshProgress() {
  if (document.body.classList.contains("uploading")) return;
  try {
    const res = await fetch("/api/progress/", { headers: { "Accept": "application/json" } });
    if (!res.ok) return;
    const data = await res.json();
    renderProgress(data.in_progress || []);
    if ((data.in_progress || []).length === 0) {
      sessionStorage.removeItem("epubdropAutoRefresh");
    }
  } catch (_) {
    // ignore transient network errors
  }
}

if (hasProgressItems || wantsAutoRefresh) {
  refreshProgress();
  refreshTimer = setInterval(refreshProgress, 8000);
}

const uploadForm = document.getElementById("uploadForm");
if (uploadForm) {
  uploadForm.addEventListener("submit", () => {
    document.body.classList.add("uploading");
    sessionStorage.setItem("epubdropAutoRefresh", "1");
  });
}

if (!hasProgressItems) {
  sessionStorage.removeItem("epubdropAutoRefresh");
}

const libraryList = document.getElementById("libraryList");
const viewButtons = Array.from(document.querySelectorAll(".view-btn"));
const LIBRARY_VIEW_KEY = "epubdropLibraryView";
const allowedViews = new Set(["mosaico", "galeria"]);
const sectionButtons = Array.from(document.querySelectorAll("[data-home-section]"));
const sectionPanels = Array.from(document.querySelectorAll("[data-home-panel]"));
const HOME_SECTION_KEY = "epubdropHomeSection";
const allowedSections = new Set(["biblioteca", "carga-metricas"]);

function setLibraryView(view) {
  if (!libraryList || !allowedViews.has(view)) return;
  libraryList.dataset.view = view;
  viewButtons.forEach((btn) => {
    btn.dataset.active = btn.dataset.view === view ? "1" : "0";
  });
  localStorage.setItem(LIBRARY_VIEW_KEY, view);
}

if (libraryList && viewButtons.length) {
  const initial = localStorage.getItem(LIBRARY_VIEW_KEY) || "mosaico";
  setLibraryView(allowedViews.has(initial) ? initial : "mosaico");
  viewButtons.forEach((btn) => {
    btn.addEventListener("click", () => setLibraryView(btn.dataset.view || "mosaico"));
  });
}

function setHomeSection(section) {
  if (!allowedSections.has(section)) return;
  sectionButtons.forEach((btn) => {
    btn.dataset.active = btn.dataset.homeSection === section ? "1" : "0";
  });
  sectionPanels.forEach((panel) => {
    panel.classList.toggle("hidden", panel.dataset.homePanel !== section);
  });
  localStorage.setItem(HOME_SECTION_KEY, section);
}

if (sectionButtons.length && sectionPanels.length) {
  const initialSection = localStorage.getItem(HOME_SECTION_KEY) || "biblioteca";
  setHomeSection(allowedSections.has(initialSection) ? initialSection : "biblioteca");
  sectionButtons.forEach((btn) => {
    btn.addEventListener("click", () => setHomeSection(btn.dataset.homeSection || "biblioteca"));
  });
}

const appThemeMode = document.getElementById("themeMode");
const APP_THEME_KEY = "appThemeMode";
const themeMedia = window.matchMedia("(prefers-color-scheme: dark)");
const metricsEls = {
  totalBooks: document.getElementById("mTotalBooks"),
  readyBooks: document.getElementById("mReadyBooks"),
  translatingBooks: document.getElementById("mTranslatingBooks"),
  translatedBlocks: document.getElementById("mTranslatedBlocks"),
  avgReading: document.getElementById("mAvgReading"),
  bookmarks: document.getElementById("mBookmarks"),
  uploads7d: document.getElementById("mUploads7d"),
  completed7d: document.getElementById("mCompleted7d"),
};

if (appThemeMode && typeof window.applyAppTheme === "function") {
  const savedMode = localStorage.getItem(APP_THEME_KEY) || "system";
  appThemeMode.value = savedMode;
  window.applyAppTheme(savedMode);
}
if (appThemeMode && typeof window.applyAppTheme !== "function") {
  appThemeMode.addEventListener("change", () => {
    localStorage.setItem(APP_THEME_KEY, appThemeMode.value);
    const resolved = appThemeMode.value === "dark"
      || (appThemeMode.value === "system" && themeMedia.matches)
      ? "dark"
      : "light";
    document.documentElement.classList.toggle("theme-dark", resolved === "dark");
    document.documentElement.classList.toggle("theme-light", resolved === "light");
    document.documentElement.setAttribute("data-theme", resolved);
    document.body.classList.toggle("theme-dark", resolved === "dark");
    document.body.classList.toggle("theme-light", resolved === "light");
    document.body.setAttribute("data-theme", resolved);
  });
}

function setMetricsFallback() {
  if (!metricsEls.totalBooks) return;
  metricsEls.totalBooks.textContent = "0";
  metricsEls.readyBooks.textContent = "0";
  metricsEls.translatingBooks.textContent = "0";
  metricsEls.translatedBlocks.textContent = "0/0 (0%)";
  metricsEls.avgReading.textContent = "0%";
  metricsEls.bookmarks.textContent = "0";
  metricsEls.uploads7d.textContent = "0";
  metricsEls.completed7d.textContent = "0";
}

function renderMetrics(m) {
  if (!metricsEls.totalBooks) return;
  metricsEls.totalBooks.textContent = String(m.total_books ?? 0);
  metricsEls.readyBooks.textContent = String(m.ready_books ?? 0);
  metricsEls.translatingBooks.textContent = String(m.translating_books ?? 0);
  metricsEls.translatedBlocks.textContent = `${m.translated_blocks ?? 0}/${m.total_blocks ?? 0} (${m.translated_percent ?? 0}%)`;
  metricsEls.avgReading.textContent = `${Math.round(Number(m.avg_reading_progress_percent ?? 0))}%`;
  metricsEls.bookmarks.textContent = String(m.bookmarks_count ?? 0);
  metricsEls.uploads7d.textContent = String(m.uploads_last_7_days ?? 0);
  metricsEls.completed7d.textContent = String(m.completed_last_7_days ?? 0);
}

async function refreshMetrics() {
  try {
    const res = await fetch("/api/metrics/", { headers: { "Accept": "application/json" } });
    if (!res.ok) {
      setMetricsFallback();
      return;
    }
    const data = await res.json();
    renderMetrics(data.metrics || {});
  } catch (_) {
    setMetricsFallback();
  }
}

refreshMetrics();
setInterval(refreshMetrics, 15000);
