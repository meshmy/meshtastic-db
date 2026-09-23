const STORAGE_KEY = "map-app-theme";
const MODES = ["system", "dark", "light"];

function getStoredMode() {
  try {
    return localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

function setStoredMode(mode) {
  try {
    if (mode === "system") localStorage.removeItem(STORAGE_KEY);
    else localStorage.setItem(STORAGE_KEY, mode);
  } catch {
    // Private browsing / blocked storage — the toggle still works for the
    // current page load, it just won't be remembered next time.
  }
}

// Three-state cycle (system -> dark -> light -> system) rather than a
// binary toggle: "system" live-follows the OS preference until the user
// explicitly pins one, matching how the button's next press always returns
// to following the OS rather than just bouncing between two fixed states.
export class ThemeController {
  constructor(onChange) {
    this.onChange = onChange;
    this.mode = getStoredMode() || "system";
    this.media = matchMedia("(prefers-color-scheme: dark)");
    this.media.addEventListener("change", () => {
      if (this.mode === "system") this._applyAndNotify();
    });
    this._setDatasetOnly();
  }

  isLight() {
    return this.resolved() === "light";
  }

  resolved() {
    if (this.mode === "system") return this.media.matches ? "dark" : "light";
    return this.mode;
  }

  cycle() {
    const idx = MODES.indexOf(this.mode);
    this.mode = MODES[(idx + 1) % MODES.length];
    setStoredMode(this.mode);
    this._applyAndNotify();
  }

  _setDatasetOnly() {
    document.documentElement.dataset.theme = this.resolved();
  }

  _applyAndNotify() {
    this._setDatasetOnly();
    if (this.onChange) this.onChange(this.resolved(), this.mode);
  }
}
