import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { Link, navigate } from "../router";
import type { ScenarioView } from "../types";

/**
 * Stand-in for an external AI search assistant (e.g. Muse). It is intentionally plain and
 * labelled as simulated: its only job is to show the moment a customer is handed off to us
 * with their question already known.
 */
export function Search() {
  const [scenarios, setScenarios] = useState<ScenarioView[]>([]);
  const [query, setQuery] = useState("");
  const [submitted, setSubmitted] = useState<string | null>(null);
  const [match, setMatch] = useState<{ token: string; scenario: ScenarioView } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.scenarios().then((list) => {
      setScenarios(list);
      const preset = new URLSearchParams(window.location.search).get("scenario");
      const found = list.find((s) => s.id === preset);
      if (found) {
        // Launched from the demo launcher: run the search right away so the handoff card is ready.
        setQuery(found.search_query);
        void search(found.search_query, found.id);
      }
    });
  }, []);

  async function search(q: string, scenarioId?: string) {
    const text = q.trim();
    if (!text) return;
    setBusy(true);
    setError(null);
    setMatch(null);
    setSubmitted(text);
    try {
      const res = await api.handoff(text, scenarioId);
      setMatch({ token: res.token, scenario: res.scenario });
    } catch (err) {
      setError(err instanceof ApiError && err.status === 404
        ? "Tidelink doesn't have a flow for that search yet. Try one of the examples below."
        : "Search failed. Check that the app is running and try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="page search-page">
      <div className="sim-banner" role="note">
        Simulated external search assistant. This page stands in for the handoff from a third-party AI assistant.{" "}
        <Link to="/">Back to Tidelink</Link>
      </div>

      <main className="search-main">
        <h1 className="search-logo">Ask anything</h1>
        <form
          className="search-box"
          onSubmit={(e) => {
            e.preventDefault();
            void search(query);
          }}
        >
          <label htmlFor="q" className="visually-hidden">
            Search
          </label>
          <input
            id="q"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Why is my internet so slow?"
            autoComplete="off"
            maxLength={200}
          />
          <button type="submit" disabled={busy || !query.trim()}>
            {busy ? "Searching" : "Search"}
          </button>
        </form>

        <div className="search-examples" aria-label="Example searches">
          {scenarios.map((s) => (
            <button
              key={s.id}
              type="button"
              className="chip"
              onClick={() => {
                setQuery(s.search_query);
                void search(s.search_query);
              }}
            >
              {s.search_query}
            </button>
          ))}
        </div>

        {submitted && (
          <section className="search-answer" aria-live="polite">
            <p className="search-answer-q">{submitted}</p>
            {error && <p className="search-error">{error}</p>}
            {match && (
              <>
                <p>
                  This usually depends on your provider's network and your home setup. Your provider can check your
                  line, outages in your area and your plan directly.
                </p>
                <div className="handoff-card">
                  <div>
                    <strong>Tidelink can check your connection now</strong>
                    <p>Continue with Tidelink, your provider's assistant. Your question comes with you, so there's nothing to repeat.</p>
                  </div>
                  <button
                    type="button"
                    className="btn btn-primary"
                    onClick={() => navigate(`/chat#ctx=${encodeURIComponent(match.token)}&s=${encodeURIComponent(match.scenario.id)}`)}
                  >
                    Continue with Tidelink
                  </button>
                </div>
              </>
            )}
          </section>
        )}
      </main>
    </div>
  );
}
