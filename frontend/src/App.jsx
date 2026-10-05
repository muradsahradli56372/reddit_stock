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

  useEffect(() => {
    loadWeeks()
      .then((w) => {
        if (w.length) setWeek(w[0].week_start);
        else {
          setLoading(false);
          setError("No analysis yet. Click “Run Analysis”.");
        }
      })
      .catch((e) => {
        setLoading(false);
        setError(`Cannot reach the API (${e.message}). Is the backend running on :8000?`);
      });
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
      setRunMsg(`Run failed: ${e.message}`);
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
                <option key={w.week_start} value={w.week_start}>{fmtWeek(w.week_start)}</option>
              ))}
            </select>
          </label>
          <button className="btn" onClick={runAnalysis} disabled={running}>
            {running ? "Running…" : "Run Analysis"}
          </button>
        </div>
      </header>

      {runMsg && <div className="notice">{runMsg}</div>}
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
