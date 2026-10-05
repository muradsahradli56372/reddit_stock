import React, { useCallback, useEffect, useState } from "react";
import { api } from "./api.js";
import { Card, EmergingCards, Overview, SentimentChart, SubredditChart, SummaryBox, TopTable } from "./components.jsx";
import { fmtWeek } from "./format.js";

export default function App() {
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
      const [stocks, emerging, sentiment, report, prevReport] = await Promise.all([
        api.stocks(wk), api.emerging(wk), api.sentiment(wk), api.report(wk),
        prev ? api.report(prev) : Promise.resolve(null),
      ]);
      setData({ stocks: stocks.stocks, emerging: emerging.stocks, sentiment, report, prevReport });
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
          {health?.demo_mode && <span className="pill pill-demo" title="Reddit credentials not configured: using generated demo data">DEMO MODE</span>}
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

      {data && (
        <main className={`grid ${loading ? "is-loading" : ""}`}>
          <div className="span-12">
            <Overview overview={data.report.overview} prevOverview={data.prevReport?.overview} />
          </div>

          <Card className="span-8" title="Top mentioned stocks"
                subtitle={`Week of ${fmtWeek(data.report.week_start)} · ranked by mentions (one per post/comment per ticker)`}>
            <TopTable stocks={data.stocks} />
          </Card>

          <Card className="span-4" title="Overall sentiment" subtitle="Share of mentions by stance toward the ticker">
            <SentimentChart sentiment={data.sentiment} />
          </Card>

          <Card className="span-12" title="Emerging & rising" subtitle="Attention growing fastest relative to each stock's own baseline">
            <EmergingCards stocks={data.emerging} />
          </Card>

          <Card className="span-5" title="Subreddit activity" subtitle="Stock mentions per subreddit (hover for posts/comments)">
            <SubredditChart activity={data.report.overview.subreddit_activity} />
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
