import { FormEvent, lazy, Suspense, useEffect, useRef, useState } from "react";
import { fetchHealth, searchObservability } from "./api";
import type { SearchHit, SearchResponse } from "./types";

const QueryExplorerTab = lazy(() => import("./components/QueryExplorerTab"));
const AdkAssistantTab = lazy(() => import("./components/AdkAssistantTab"));

type SearchMode = "classic" | "ai";
type ThemeMode = "dark" | "light";

type ExplorerTarget = {
  sourceId: string;
  metricId: string;
  queryId: string;
  range?: string;
};

const THEME_KEY = "observability-lab-theme";

const PANEL_TO_EXPLORER: Record<string, ExplorerTarget> = {
  demo_errors: {
    sourceId: "prometheus",
    metricId: "demo_dataset",
    queryId: "demo_error_rate",
  },
  demo_latency: {
    sourceId: "prometheus",
    metricId: "demo_dataset",
    queryId: "demo_latency",
  },
  cpu_busy: {
    sourceId: "prometheus",
    metricId: "cpu",
    queryId: "cpu_busy",
  },
  error_logs: {
    sourceId: "opensearch",
    metricId: "app_logs",
    queryId: "error_logs_rate",
  },
};

function readStoredTheme(): ThemeMode {
  try {
    const stored = localStorage.getItem(THEME_KEY);
    if (stored === "light" || stored === "dark") return stored;
  } catch {
    /* ignore */
  }
  if (typeof window !== "undefined" && window.matchMedia) {
    return window.matchMedia("(prefers-color-scheme: light)").matches
      ? "light"
      : "dark";
  }
  return "dark";
}

function applyTheme(theme: ThemeMode) {
  document.documentElement.setAttribute("data-theme", theme);
  try {
    localStorage.setItem(THEME_KEY, theme);
  } catch {
    /* ignore */
  }
}

function hitToExplorerTarget(hit: SearchHit): ExplorerTarget | null {
  if (hit.type === "metric" && hit.sourceId && hit.metricId && hit.queryId) {
    return {
      sourceId: hit.sourceId,
      metricId: hit.metricId,
      queryId: hit.queryId,
    };
  }
  if (hit.type === "panel") {
    return PANEL_TO_EXPLORER[hit.id] || null;
  }
  return null;
}

function collectHits(data: SearchResponse): SearchHit[] {
  return [
    ...data.dashboards,
    ...data.panels.map((p) => ({
      type: "panel" as const,
      id: p.id,
      title: p.title,
      description: p.description,
      score: p.score,
    })),
    ...data.metrics,
  ].sort((a, b) => b.score - a.score);
}

export default function App() {
  const [searchMode, setSearchMode] = useState<SearchMode>("ai");
  const [theme, setTheme] = useState<ThemeMode>(() => readStoredTheme());
  const [themeMenuOpen, setThemeMenuOpen] = useState(false);
  const [health, setHealth] = useState<string>("checking…");
  const themeMenuRef = useRef<HTMLDivElement>(null);
  const searchWrapRef = useRef<HTMLDivElement>(null);

  const [searchText, setSearchText] = useState("");
  const [searchLoading, setSearchLoading] = useState(false);
  const [searchData, setSearchData] = useState<SearchResponse | null>(null);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [dropdownOpen, setDropdownOpen] = useState(false);
  const [aiPrompt, setAiPrompt] = useState<string | null>(null);
  const [explorerTarget, setExplorerTarget] = useState<ExplorerTarget | null>(
    null
  );
  const [searchNotice, setSearchNotice] = useState<string | null>(null);

  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  useEffect(() => {
    fetchHealth()
      .then(() => setHealth("Connected"))
      .catch(() => setHealth("Not connected"));
  }, []);

  useEffect(() => {
    if (!themeMenuOpen) return;
    function onPointerDown(e: MouseEvent) {
      if (!themeMenuRef.current?.contains(e.target as Node)) {
        setThemeMenuOpen(false);
      }
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setThemeMenuOpen(false);
    }
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [themeMenuOpen]);

  useEffect(() => {
    if (!dropdownOpen) return;
    function onPointerDown(e: MouseEvent) {
      if (!searchWrapRef.current?.contains(e.target as Node)) {
        setDropdownOpen(false);
      }
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setDropdownOpen(false);
    }
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [dropdownOpen]);

  function pickTheme(next: ThemeMode) {
    setTheme(next);
    setThemeMenuOpen(false);
  }

  function clearSearchUi() {
    setSearchData(null);
    setSearchError(null);
    setDropdownOpen(false);
    setSearchNotice(null);
  }

  function openHit(hit: SearchHit) {
    setSearchMode("classic");

    if (hit.type === "dashboard" && hit.url) {
      window.open(hit.url, "_blank", "noopener,noreferrer");
      setSearchNotice(`Opened dashboard “${hit.title}” in a new tab.`);
      setDropdownOpen(false);
      return;
    }

    const target = hitToExplorerTarget(hit);
    if (!target) {
      setSearchNotice(`No explorer view mapped for “${hit.title}”.`);
      return;
    }

    setExplorerTarget({ ...target });
    setSearchNotice(
      `Opened “${hit.title}” in Query explorer — charting on the right.`
    );
    setDropdownOpen(false);
  }

  async function runClassicSearch(q: string) {
    setSearchLoading(true);
    setSearchError(null);
    setSearchNotice(null);
    try {
      const data = await searchObservability(q);
      setSearchData(data);
      const hits = collectHits(data);
      const best =
        hits.find((h) => h.type === "metric") ||
        hits.find((h) => h.type === "panel") ||
        hits.find((h) => h.type === "dashboard") ||
        data.best;

      if (best) {
        openHit(best);
        // Keep dropdown available so user can pick another match without leaving explorer
        setDropdownOpen(hits.length > 1);
      } else {
        setSearchNotice("No dashboards, panels, or metrics matched that search.");
        setDropdownOpen(true);
      }
    } catch (err) {
      setSearchData(null);
      setSearchError(err instanceof Error ? err.message : String(err));
      setDropdownOpen(true);
    } finally {
      setSearchLoading(false);
    }
  }

  async function onSearchSubmit(event: FormEvent) {
    event.preventDefault();
    const q = searchText.trim();
    if (!q) return;

    if (searchMode === "ai") {
      clearSearchUi();
      setAiPrompt(q);
      return;
    }

    await runClassicSearch(q);
  }

  const hits = searchData ? collectHits(searchData) : [];

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <h1>Observability Lab</h1>
        </div>

        <div className="search-wrap" ref={searchWrapRef}>
          <form
            className={
              searchMode === "ai"
                ? "search-mode-bar ai-active"
                : "search-mode-bar"
            }
            role="search"
            aria-label="Search dashboards, panels, and metrics"
            onSubmit={onSearchSubmit}
          >
            <span className="search-mode-icon" aria-hidden="true">
              <SearchIcon />
            </span>
            <input
              className="search-mode-input"
              type="search"
              value={searchText}
              onChange={(e) => {
                setSearchText(e.target.value);
                if (searchNotice) setSearchNotice(null);
              }}
              onFocus={() => {
                if (hits.length || searchError) setDropdownOpen(true);
              }}
              placeholder={
                searchMode === "ai"
                  ? "Ask about your data..."
                  : "Search dashboards, panels, metrics..."
              }
              aria-label={
                searchMode === "ai"
                  ? "Ask about your data"
                  : "Search dashboards, panels, metrics"
              }
              aria-expanded={dropdownOpen}
              aria-controls="search-results-dropdown"
            />
            <button
              type="button"
              role="switch"
              aria-checked={searchMode === "ai"}
              aria-label={`Turn AI search ${searchMode === "ai" ? "off" : "on"}`}
              className="mode-toggle"
              onClick={() => {
                setSearchMode(searchMode === "ai" ? "classic" : "ai");
                clearSearchUi();
              }}
            >
              <SparklesIcon />
              <span>AI</span>
              <span className="mode-toggle-track" aria-hidden="true">
                <span className="mode-toggle-thumb" />
              </span>
            </button>
          </form>

          {dropdownOpen && searchMode === "classic" && (
            <div
              id="search-results-dropdown"
              className="search-dropdown"
              role="listbox"
              aria-label="Search matches"
            >
              <div className="search-dropdown-head">
                <span>
                  {searchLoading
                    ? "Searching…"
                    : searchError
                      ? "Search failed"
                      : hits.length
                        ? `${hits.length} matches — opens in Query explorer`
                        : "No matches"}
                </span>
                <button
                  type="button"
                  className="btn ghost small"
                  onClick={clearSearchUi}
                >
                  Close
                </button>
              </div>
              {searchError && (
                <div className="alert error search-dropdown-error">
                  {searchError}
                </div>
              )}
              <div className="search-dropdown-list">
                {hits.map((hit) => (
                  <button
                    key={`${hit.type}-${hit.id}`}
                    type="button"
                    role="option"
                    className="search-dropdown-item"
                    onClick={() => openHit(hit)}
                  >
                    <span className={`search-hit-kind ${hit.type}`}>
                      {hit.type}
                    </span>
                    <span className="search-dropdown-title">{hit.title}</span>
                    {hit.description && (
                      <span className="search-dropdown-desc muted small">
                        {hit.description}
                      </span>
                    )}
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>

        <div className="topbar-actions">
          <div className="theme-menu" ref={themeMenuRef}>
            <button
              type="button"
              className="theme-toggle"
              aria-haspopup="menu"
              aria-expanded={themeMenuOpen}
              aria-label="Appearance mode"
              onClick={() => setThemeMenuOpen((open) => !open)}
            >
              {theme === "dark" ? <MoonIcon /> : <SunIcon />}
              <span className="theme-toggle-label">Mode</span>
              <span className="theme-caret" aria-hidden="true" />
            </button>
            {themeMenuOpen && (
              <div className="theme-menu-panel" role="menu" aria-label="Theme">
                <button
                  type="button"
                  role="menuitemradio"
                  aria-checked={theme === "light"}
                  className={
                    theme === "light"
                      ? "theme-menu-item active"
                      : "theme-menu-item"
                  }
                  onClick={() => pickTheme("light")}
                >
                  <SunIcon />
                  <span>Light</span>
                </button>
                <button
                  type="button"
                  role="menuitemradio"
                  aria-checked={theme === "dark"}
                  className={
                    theme === "dark"
                      ? "theme-menu-item active"
                      : "theme-menu-item"
                  }
                  onClick={() => pickTheme("dark")}
                >
                  <MoonIcon />
                  <span>Dark</span>
                </button>
              </div>
            )}
          </div>

          <div className="status" aria-live="polite">
            <span
              className={
                health === "Connected"
                  ? "dot online"
                  : health === "checking…"
                    ? "dot"
                    : "dot offline"
              }
              aria-hidden="true"
            />
            {health === "Connected" ? "Connected" : health}
          </div>
        </div>
      </header>

      {searchNotice && searchMode === "classic" && (
        <div className="search-notice" role="status">
          <span>{searchNotice}</span>
          <button
            type="button"
            className="btn ghost small"
            onClick={() => setSearchNotice(null)}
          >
            Dismiss
          </button>
        </div>
      )}

      <main className="main">
        <Suspense
          fallback={
            <div className="panel loading-fallback">
              <p className="muted">Loading…</p>
            </div>
          }
        >
          {searchMode === "classic" ? (
            <QueryExplorerTab
              initialTarget={explorerTarget}
              onInitialTargetConsumed={() => setExplorerTarget(null)}
            />
          ) : (
            <AdkAssistantTab
              externalPrompt={aiPrompt}
              onExternalPromptConsumed={() => setAiPrompt(null)}
            />
          )}
        </Suspense>
      </main>
    </div>
  );
}

function SearchIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <circle cx="11" cy="11" r="6.5" />
      <path d="m16 16 4.5 4.5" />
    </svg>
  );
}

function SparklesIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="m12 3 1.5 5.5L19 10l-5.5 1.5L12 17l-1.5-5.5L5 10l5.5-1.5L12 3Z" />
      <path d="m19 15 .7 2.3L22 18l-2.3.7L19 21l-.7-2.3L16 18l2.3-.7L19 15Z" />
    </svg>
  );
}

function SunIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" />
    </svg>
  );
}

function MoonIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M21 14.5A8.5 8.5 0 0 1 9.5 3 7 7 0 1 0 21 14.5Z" />
    </svg>
  );
}
