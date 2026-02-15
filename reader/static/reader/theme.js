(function () {
  var STORAGE_KEY = "appThemeMode";
  var media = window.matchMedia("(prefers-color-scheme: dark)");

  function resolveMode(mode) {
    if (mode === "dark") return "dark";
    if (mode === "light") return "light";
    return media.matches ? "dark" : "light";
  }

  function setThemeClasses(resolved) {
    var root = document.documentElement;
    var body = document.body;
    if (root) {
      root.classList.toggle("theme-dark", resolved === "dark");
      root.classList.toggle("theme-light", resolved === "light");
      root.setAttribute("data-theme", resolved);
    }
    if (body) {
      body.classList.toggle("theme-dark", resolved === "dark");
      body.classList.toggle("theme-light", resolved === "light");
      body.setAttribute("data-theme", resolved);
    }
  }

  function syncThemeControls(mode, resolved) {
    var selects = document.querySelectorAll("#themeMode");
    selects.forEach(function (sel) {
      sel.value = mode;
    });
    var themeModeVal = document.getElementById("themeModeVal");
    if (themeModeVal) themeModeVal.textContent = resolved === "dark" ? "Oscuro" : "Claro";
    var toggle = document.getElementById("themeToggle");
    if (toggle) {
      toggle.setAttribute("aria-label", resolved === "dark" ? "Cambiar a claro" : "Cambiar a oscuro");
    }
  }

  function applyTheme(mode) {
    var resolved = resolveMode(mode);
    setThemeClasses(resolved);
    syncThemeControls(mode, resolved);
  }

  function currentMode() {
    return localStorage.getItem(STORAGE_KEY) || "system";
  }

  window.applyAppTheme = applyTheme;

  applyTheme(currentMode());

  document.addEventListener("DOMContentLoaded", function () {
    applyTheme(currentMode());

    var selects = document.querySelectorAll("#themeMode");
    selects.forEach(function (sel) {
      sel.addEventListener("change", function () {
        localStorage.setItem(STORAGE_KEY, sel.value);
        applyTheme(sel.value);
      });
    });

    var toggle = document.getElementById("themeToggle");
    if (toggle) {
      toggle.addEventListener("click", function () {
        var resolved = resolveMode(currentMode());
        var next = resolved === "dark" ? "light" : "dark";
        localStorage.setItem(STORAGE_KEY, next);
        applyTheme(next);
      });
    }

    var onSystemThemeChange = function () {
      if (currentMode() === "system") applyTheme("system");
    };
    if (typeof media.addEventListener === "function") {
      media.addEventListener("change", onSystemThemeChange);
    } else if (typeof media.addListener === "function") {
      media.addListener(onSystemThemeChange);
    }
  });
})();
