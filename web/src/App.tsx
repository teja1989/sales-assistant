import { useEffect, useState } from "react";
import { api } from "./api";
import { Chat } from "./pages/Chat";
import { Dashboard } from "./pages/Dashboard";
import { Home } from "./pages/Home";
import { Search } from "./pages/Search";
import type { AppConfig } from "./types";

function usePath() {
  const [path, setPath] = useState(window.location.pathname);
  useEffect(() => {
    const onPop = () => setPath(window.location.pathname);
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);
  return path;
}

export function App() {
  const path = usePath();
  const [config, setConfig] = useState<AppConfig | null>(null);

  useEffect(() => {
    api.config().then(setConfig).catch(() => setConfig(null));
  }, []);

  useEffect(() => {
    const name = config?.app_name ?? "Tidelink Assist";
    const page = path.startsWith("/chat") ? "Chat" : path.startsWith("/dashboard") ? "Impact" : path.startsWith("/search") ? "Search" : "";
    document.title = page ? `${page} | ${name}` : name;
  }, [path, config]);

  if (path.startsWith("/search")) return <Search />;
  if (path.startsWith("/chat")) return <Chat config={config} />;
  if (path.startsWith("/dashboard")) return <Dashboard config={config} />;
  return <Home config={config} />;
}
