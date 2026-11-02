import { useCallback, useEffect, useState } from "react";
import { api } from "./api.js";
import { LangContext, useT } from "./i18n.js";
import Live from "./pages/Live.jsx";
import History from "./pages/History.jsx";
import Status from "./pages/Status.jsx";
import Settings from "./pages/Settings.jsx";
import Insights from "./pages/Insights.jsx";
import Alerts from "./pages/Alerts.jsx";
import Quality from "./pages/Quality.jsx";
import Login from "./pages/Login.jsx";

const PAGES = { live: Live, history: History, insights: Insights, alerts: Alerts, quality: Quality, status: Status, settings: Settings };
const pageFromHash = () => { const p = location.hash.replace(/^#\/?/, "").split("?")[0]; return PAGES[p] ? p : "live"; };
const browserLang = () => (navigator.language || "nl").toLowerCase().startsWith("nl") ? "nl" : "en";

export default function App() {
  const [user, setUser] = useState(undefined); // undefined = loading, null = logged out
  const [page, setPage] = useState(pageFromHash);

  const loadMe = useCallback(() => api("/api/auth/me").then(setUser).catch(() => setUser(null)), []);
  useEffect(() => { loadMe(); }, [loadMe]);
  useEffect(() => {
    const on = () => setPage(pageFromHash());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);

  const lang = user?.language || browserLang();
  useEffect(() => { document.documentElement.lang = lang; }, [lang]);

  if (user === undefined) return null;
  return (
    <LangContext.Provider value={lang}>
      {user === null ? <Login onLogin={setUser} /> : <Shell user={user} page={page} setUser={setUser} />}
    </LangContext.Provider>
  );
}

function Shell({ user, page, setUser }) {
  const t = useT();
  const Page = PAGES[page];
  const logout = async () => { await api("/api/auth/logout", { method: "POST" }).catch(() => {}); setUser(null); };
  return (
    <div className="app">
      <header className="top">
        <div className="brand"><img src="/icon.svg" width="24" height="24" alt="" /> P1 Monitor</div>
        <nav className="tabs main">
          {Object.keys(PAGES).map((p) => (
            <button key={p} className={p === page ? "on" : ""} onClick={() => { location.hash = "/" + p; }}>{t(p)}</button>
          ))}
        </nav>
        <button className="btn" onClick={logout} title={user.username}>{t("logout")}</button>
      </header>
      <Page user={user} setUser={setUser} />
    </div>
  );
}
