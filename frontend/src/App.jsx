import React, { useCallback, useEffect, useState } from "react";
import { api } from "./api.js";
import { Card, EmergingCards, Overview, RedditCommunities, SentimentChart, SubredditChart, SubredditTable, SummaryBox, TopTable } from "./components.jsx";
import StockPage from "./StockPage.jsx";

// Minimal hash router: "#/" = dashboard, "#/stock/RKLB" = stock detail.
function parseRoute() {
  const m = window.location.hash.match(/^#\/stock\/([A-Za-z.\-]{1,10})/);
  return m ? { page: "stock", ticker: m[1].toUpperCase() } : { page: "home" };
}

function useRoute() {
  const [route, setRoute] = useState(parseRoute);
  useEffect(() => {
    const onHash = () => { setRoute(parseRoute()); window.scrollTo(0, 0); };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  return route;
}
import { fmtWeek } from "./format.js";

export default function App() {
  const route = useRoute();
  const [health, setHealth] = useState(null);
  const [weeks, setWeeks] = useState([]);
  const [week, setWeek] = useState(null);
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [runMsg, setRunMsg] = useState(null);

  const loadWeeks = useCallback(async () => {
    const [h, w] = await Promise.all([api.health(), api.weeks()]);
    setHealth(h);
    setWeeks(w);
    return w;
  }, []);

  const loadWeek = useCallback(async (wk, allWeeks) => {
    setLoading(true);
    setError(null);
    try {
      const idx = allWeeks.findIndex((w) => w.week_start === wk);
      const prev = idx >= 0 && idx + 1 < allWeeks.length ? allWeeks[idx + 1].week_start : null;
      const [stocks, emerging, sentiment, report, prevReport, early, subs] = await Promise.all([
        api.stocks(wk), api.emerging(wk), api.sentiment(wk), api.report(wk),
        prev ? api.report(prev) : Promise.resolve(null), api.earlySignals(wk), api.subreddits(wk),
      ]);
      setData({ stocks: stocks.stocks, emerging: emerging.stocks, sentiment, report, prevReport,
                early: early.stocks, subreddits: subs.subreddits, redditCommunities: subs.reddit_communities });
    } catch (e) {
      setError(e.message);
      setData(null);
    } finally {
      setLoading(false);
    }
  }, []);

  const [waiting, setWaiting] = useState(false);

  useEffect(() => {
    let timer;
    const poll = () =>
      loadWeeks()
        .then(async (w) => {
          if (w.length) {
            setWaiting(false);
            setError(null);
            setWeek(w[0].week_start);
            return;
          }
          setLoading(false);
          const h = await api.health();
          if (h.analysis_running) {
            // First live run happens in the background and can take a few minutes (rate limits).
            setWaiting(true);
            timer = setTimeout(poll, 10000);
          } else {
            setWaiting(false);
            setError("No analysis yet. Click “Run Analysis”.");
          }
        })
        .catch((e) => {
          setLoading(false);
          setError(`Cannot reach the API (${e.message}). Is the backend running on :8000?`);
        });
    poll();
    return () => clearTimeout(timer);
  }, [loadWeeks]);

  useEffect(() => {
    if (week) loadWeek(week, weeks);
  }, [week, weeks, loadWeek]);

  async function runAnalysis() {
    setRunning(true);
    setRunMsg(null);
    try {
      const res = await api.run();
      const last = res.weeks[res.weeks.length - 1];
      setRunMsg(`Analysis complete: ${last.posts} posts, ${last.comments} comments, ${last.stocks} stocks (${res.mode} mode).`);
      const w = await loadWeeks();
      const target = last.week_start;
      if (target === week) loadWeek(target, w);
      else setWeek(target);
    } catch (e) {
      setRunMsg(e.status === 409 ? "An analysis is already running. Please wait and refresh in a minute." : `Run failed: ${e.message}`);
    } finally {
      setRunning(false);
    }
  }

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="logo" aria-hidden>◆</span>
          <div>
            <h1>Reddit Market Pulse</h1>
            <p className="muted small">What Reddit is discussing about public companies. Research only, not trading advice.</p>
          </div>
        </div>
        <div className="controls">
          {health?.demo_mode && <span className="pill pill-demo" title="No live text source configured: using generated demo data">DEMO MODE</span>}
          {health && !health.demo_mode && (
            <span className="pill pill-ok" title="Where post/message text comes from">
              TEXT: {health.text_source === "stocktwits" ? "StockTwits" : "Reddit"}
            </span>
          )}
          {health && health.attention_source !== "none" && !health.demo_mode && (
            <span className="pill pill-ok" title="Count-only Reddit attention">REDDIT COUNTS: {health.attention_source === "apewisdom" ? "ApeWisdom" : health.attention_source}</span>
          )}
          {health && (
            <span className={`pill ${health.llm_enabled ? "pill-ok" : ""}`} title="Anthropic API key status">
              {health.llm_enabled ? "LLM: Claude" : "LLM: off (fallbacks)"}
            </span>
          )}
          <label className="week-select">
            <span className="muted small">Week</span>
            <select value={week || ""} onChange={(e) => setWeek(e.target.value)} disabled={!weeks.length}>
              {weeks.map((w) => (
                <option key={w.week_start} value={w.week_start}>
                  {fmtWeek(w.week_start)}{new Date(`${w.week_start}T00:00:00Z`).getTime() + 7 * 86400000 > Date.now() ? " (in progress)" : ""}
                </option>
              ))}
            </select>
          </label>
          <button className="btn" onClick={runAnalysis} disabled={running}>
            {running ? "Running…" : "Run Analysis"}
          </button>
        </div>
      </header>

      {runMsg && <div className="notice">{runMsg}</div>}
      {waiting && (
        <div className="notice">
          ⏳ The first analysis is running in the background. With live sources this can take a few minutes
          (StockTwits rate limits). This page refreshes automatically.
        </div>
      )}
      {route.page === "home" && data?.report.overview.in_progress && (
        <div className="notice">
          This week is still in progress: <b>{data.report.overview.days_elapsed} of 7 days</b> so far. Text comparisons
          with last week are pace-adjusted (last week scaled to the same elapsed time); Reddit counts are per-day
          estimates. Numbers will change until the week ends.
        </div>
      )}
      {route.page === "home" && data && data.stocks.length === 0 && !health?.demo_mode && (
        <div className="notice notice-error">
          No data has been collected for this week yet. Collection runs in the background (every few hours); click
          “Run Analysis” to collect now. If it stays empty, run <code>python scripts/check_sources.py</code> and look at the
          backend window for errors.
        </div>
      )}
      {route.page === "home" && data?.report.overview.reddit_attention && !data.report.overview.in_progress &&
        data.report.overview.reddit_attention.days_covered < 7 && (
        <div className="notice">
          Reddit counts for this week cover {data.report.overview.reddit_attention.days_covered} of 7 days (collection
          started mid-week or the backend was off), so they are estimates from the days available.
        </div>
      )}
      {error && <div className="notice notice-error">{error}</div>}
      {loading && !data && <div className="loading">Loading…</div>}

      {route.page === "stock" && week && (
        <StockPage key={`${route.ticker}:${week}`} ticker={route.ticker} week={week} />
      )}

      {route.page === "home" && data && (
        <main className={`grid ${loading ? "is-loading" : ""}`}>
          <div className="span-12">
            <Overview overview={data.report.overview} prevOverview={data.prevReport?.overview} />
          </div>

          <Card className="span-8" title="Top mentioned stocks"
                subtitle={`Week of ${fmtWeek(data.report.week_start)} · ranked by ${data.report.overview.reddit_attention ? "Reddit attention" : "mentions"} (text: one mention per post/comment per ticker)`}>
            <TopTable stocks={data.stocks} />
          </Card>

          <Card className="span-4" title="Overall sentiment" subtitle="Share of mentions by stance toward the ticker">
            <SentimentChart sentiment={data.sentiment} />
          </Card>

          <Card className="span-12" title="Emerging & rising" subtitle="Attention growing fastest relative to each stock's own baseline">
            <EmergingCards stocks={data.emerging} />
          </Card>

          <Card className="span-12" title="Early signals"
                subtitle="Smaller names whose discussion is broadening fast across people, subreddits and engagement. A screen for research, not a prediction">
            <EmergingCards stocks={data.early} mode="early" />
          </Card>

          <Card className="span-5" title="Communities" subtitle="Text mentions per community (hover for posts/comments)">
            <SubredditChart activity={data.report.overview.subreddit_activity} />
            <SubredditTable subs={data.subreddits} />
            <RedditCommunities comms={data.redditCommunities} />
          </Card>

          <Card className="span-7" title="Weekly AI summary" subtitle="Data, interpretation and speculation kept separate">
            <SummaryBox report={data.report} />
          </Card>
        </main>
      )}
      <footer className="footer muted small">
        Attention and sentiment describe Reddit discussion only. They do not imply that Reddit causes price moves.
      </footer>
    </div>
  );
}
