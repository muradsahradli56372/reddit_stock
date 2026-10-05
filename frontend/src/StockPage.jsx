import React, { useEffect, useState } from "react";
import {
  Bar, BarChart, CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import { api } from "./api.js";
import { Card, SENTIMENT, TrendBadge } from "./components.jsx";
import { changeClass, community, fmtChange, fmtInt, fmtPct, fmtWeek } from "./format.js";

const shortWeek = (iso) =>
  new Date(`${iso}T00:00:00Z`).toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });

const axisProps = { stroke: "var(--axis)", tick: { fill: "var(--text-muted)", fontSize: 11 } };

function ChartTooltip({ active, payload, label, rows }) {
  if (!active || !payload?.length) return null;
  const d = payload[0].payload;
  return (
    <div className="tooltip">
      <div className="tooltip-title">Week of {shortWeek(label)}</div>
      {rows.map((r) => (
        <div className="tooltip-row" key={r.key}>
          <span className="swatch" style={{ background: r.color }} />
          <span>{r.label}</span>
          <span className="num">{r.fmt ? r.fmt(d[r.key]) : d[r.key] ?? "–"}</span>
        </div>
      ))}
    </div>
  );
}

function Kpi({ label, value, sub, subClass = "" }) {
  return (
    <div className="tile">
      <div className="tile-label">{label}</div>
      <div className="tile-value">{value}</div>
      <div className={`tile-delta ${subClass}`}>{sub || " "}</div>
    </div>
  );
}

function AttentionChart({ weeks, current }) {
  const rows = [
    { key: "mentions", label: "Mentions", color: "var(--series-1)" },
    { key: "unique_authors", label: "Unique authors", color: "var(--series-2)" },
  ];
  return (
    <>
      <ul className="legend">
        {rows.map((r) => <li key={r.key}><span className="swatch" style={{ background: r.color }} />{r.label}</li>)}
      </ul>
      <ResponsiveContainer width="100%" height={240}>
        <LineChart data={weeks} margin={{ top: 8, right: 16, bottom: 0, left: -12 }}>
          <CartesianGrid vertical={false} stroke="var(--grid)" />
          <XAxis dataKey="week_start" tickFormatter={shortWeek} {...axisProps} />
          <YAxis allowDecimals={false} {...axisProps} />
          <ReferenceLine x={current} stroke="var(--text-muted)" strokeDasharray="3 3" />
          <Tooltip content={<ChartTooltip rows={rows} />} cursor={{ stroke: "var(--axis)" }} />
          {rows.map((r) => (
            <Line key={r.key} dataKey={r.key} stroke={r.color} strokeWidth={2} isAnimationActive={false}
                  dot={{ r: 4, fill: r.color, stroke: "var(--surface)", strokeWidth: 2 }} activeDot={{ r: 5 }} />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </>
  );
}

function SentimentHistory({ weeks }) {
  const data = weeks.map((w) => {
    const total = w.bullish + w.neutral + w.bearish + w.unclear;
    const pct = (n) => (total ? (100 * n) / total : 0);
    return { ...w, b: pct(w.bullish), n: pct(w.neutral), r: pct(w.bearish), u: pct(w.unclear), total };
  });
  const keys = { bullish: "b", neutral: "n", bearish: "r", unclear: "u" };
  const rows = SENTIMENT.map((s) => ({ key: keys[s.key], label: s.label, color: s.color, fmt: (v) => fmtPct(v) }));
  return (
    <>
      <ul className="legend">
        {SENTIMENT.map((s) => <li key={s.key}><span className="swatch" style={{ background: s.color }} />{s.label}</li>)}
      </ul>
      <ResponsiveContainer width="100%" height={240}>
        <BarChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: -12 }} barCategoryGap={6}>
          <CartesianGrid vertical={false} stroke="var(--grid)" />
          <XAxis dataKey="week_start" tickFormatter={shortWeek} {...axisProps} />
          <YAxis domain={[0, 100]} ticks={[0, 25, 50, 75, 100]} tickFormatter={(v) => `${v}%`} {...axisProps} />
          <Tooltip content={<ChartTooltip rows={rows} />} cursor={{ fill: "var(--hover)" }} />
          {SENTIMENT.map((s, i) => (
            <Bar key={s.key} dataKey={keys[s.key]} stackId="s" fill={s.color} stroke="var(--surface)" strokeWidth={2}
                 radius={i === SENTIMENT.length - 1 ? [4, 4, 0, 0] : 0} isAnimationActive={false} />
          ))}
        </BarChart>
      </ResponsiveContainer>
      <p className="muted small">Weeks with no mentions show as empty columns.</p>
    </>
  );
}

function ChangeBars({ weeks, dataKey, label, color, current }) {
  const rows = [{ key: dataKey, label, color, fmt: (v) => (v == null ? "–" : `${v > 0 ? "+" : ""}${v.toFixed(1)}%`) }];
  return (
    <div>
      <div className="mini-title">{label}</div>
      <ResponsiveContainer width="100%" height={170}>
        <BarChart data={weeks} margin={{ top: 8, right: 8, bottom: 0, left: -12 }}>
          <CartesianGrid vertical={false} stroke="var(--grid)" />
          <XAxis dataKey="week_start" tickFormatter={shortWeek} {...axisProps} />
          <YAxis tickFormatter={(v) => `${v}%`} {...axisProps} />
          <ReferenceLine y={0} stroke="var(--axis)" />
          <ReferenceLine x={current} stroke="var(--text-muted)" strokeDasharray="3 3" />
          <Tooltip content={<ChartTooltip rows={rows} />} cursor={{ fill: "var(--hover)" }} />
          <Bar dataKey={dataKey} fill={color} radius={[4, 4, 0, 0]} isAnimationActive={false} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

function ReasonList({ items, stance }) {
  if (!items.length) return <p className="muted small">No {stance} reasons stated this week.</p>;
  const max = Math.max(...items.map((r) => r.count));
  return (
    <ul className="reason-list">
      {items.map((r) => (
        <li key={r.category}>
          <span className="reason-label">{r.label}</span>
          <span className="reason-track">
            <span className={`reason-fill ${stance}`} style={{ width: `${(100 * r.count) / max}%` }} />
          </span>
          <span className="num">{r.count}</span>
        </li>
      ))}
    </ul>
  );
}

function NetBar({ value }) {
  // Diverging bar: left = net bearish (red), right = net bullish (blue), centred at 0.
  const w = Math.min(Math.abs(value), 100) / 2;
  return (
    <span className="net-track" title={`${value > 0 ? "+" : ""}${value} net`}>
      <span className="net-mid" />
      <span className={`net-fill ${value >= 0 ? "pos" : "neg"}`}
            style={value >= 0 ? { left: "50%", width: `${w}%` } : { right: "50%", width: `${w}%` }} />
    </span>
  );
}

export default function StockPage({ ticker, week, onBack }) {
  const [data, setData] = useState(null);
  const [hist, setHist] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let alive = true;
    setError(null);
    Promise.all([api.stock(ticker, week), api.history(ticker, 12)])
      .then(([d, h]) => { if (alive) { setData(d); setHist(h.weeks); } })
      .catch((e) => alive && setError(e.message));
    return () => { alive = false; };
  }, [ticker, week]);

  if (error) return <div className="notice notice-error">{error} <a href="#/" onClick={onBack}>Back</a></div>;
  if (!data || !hist) return <div className="loading">Loading {ticker}…</div>;

  const m = data.metrics;
  const hasText = hist.some((w) => w.mentions > 0);
  const hasRedditHist = hist.some((w) => w.reddit_mentions != null);
  const isDemoPrice = data.price?.source === "demo";
  return (
    <main className="grid">
      <div className="span-12 stock-head">
        <a className="back" href="#/">← Market overview</a>
        <div className="stock-title">
          <h1>{data.ticker}</h1>
          <span className="muted">{data.company} · {data.exchange}</span>
          {m && <TrendBadge cls={m.trend_class} />}
          {m?.is_early_signal && <span className="badge badge-early">EARLY SIGNAL</span>}
        </div>
        <p className="muted small">Week of {fmtWeek(data.week_start)}</p>
      </div>

      {!m && <div className="span-12 notice">No mentions of {data.ticker} in this week. History is shown below.</div>}

      {m && m.mentions === 0 && (
        <div className="span-12 notice">
          {data.ticker} is discussed on Reddit but wasn't in this week's text sample, so there's no sentiment, reasons or
          author data yet. Add it to STOCKTWITS_WATCHLIST to collect its messages.
        </div>
      )}

      {m && (
        <div className="span-12 tiles tiles-7">
          {m.reddit_mentions != null && (
            <Kpi label="Reddit mentions (est.)" value={fmtInt(m.reddit_mentions)}
                 sub={m.reddit_prev_mentions == null ? `${m.reddit_days_covered}/7 days covered` : `${fmtChange(m.reddit_change_pct)} WoW`}
                 subClass={m.reddit_prev_mentions == null ? "" : changeClass(m.reddit_change_pct)} />
          )}
          {m.mentions > 0 && (
            <>
              <Kpi label="Text mentions" value={fmtInt(m.mentions)} sub={`${fmtChange(m.mention_change_pct)} WoW`}
                   subClass={changeClass(m.mention_change_pct)} />
              <Kpi label="Unique authors" value={fmtInt(m.unique_authors)} sub={`${fmtChange(m.author_change_pct)} WoW`}
                   subClass={changeClass(m.author_change_pct)} />
              <Kpi label="Bullish" value={fmtPct(m.bullish_pct)} sub={`${m.bullish} mentions`} />
              <Kpi label="Bearish" value={fmtPct(m.bearish_pct)} sub={`${m.bearish} mentions`} />
            </>
          )}
          <Kpi label="Trend score" value={m.trend_score.toFixed(0)} sub={m.trend_class} />
          <Kpi label="Early signal" value={(m.early_signal_score ?? 0).toFixed(0)}
               sub={m.is_early_signal ? "flagged" : "not flagged"} />
          <Kpi label={`Price (week)${isDemoPrice ? " · demo" : ""}`}
               value={data.price ? `${data.price.change_pct > 0 ? "+" : ""}${data.price.change_pct.toFixed(1)}%` : "–"}
               sub={data.price ? `${data.price.open} → ${data.price.close}` : "no price data"}
               subClass="" />
        </div>
      )}

      {hist.some((w) => w.reddit_mentions != null) && (
        <Card className="span-12" title="Reddit attention over time"
              subtitle="Estimated weekly Reddit mentions from daily count snapshots (count-only source, no text)">
          <div className="two-col">
            <ResponsiveContainer width="100%" height={200}>
              <BarChart data={hist} margin={{ top: 8, right: 8, bottom: 0, left: -6 }}>
                <CartesianGrid vertical={false} stroke="var(--grid)" />
                <XAxis dataKey="week_start" tickFormatter={shortWeek} {...axisProps} />
                <YAxis allowDecimals={false} {...axisProps} />
                <ReferenceLine x={data.week_start} stroke="var(--text-muted)" strokeDasharray="3 3" />
                <Tooltip content={<ChartTooltip rows={[{ key: "reddit_mentions", label: "Reddit mentions (est.)", color: "var(--series-1)", fmt: (v) => (v == null ? "no data" : fmtInt(v)) }]} />} cursor={{ fill: "var(--hover)" }} />
                <Bar dataKey="reddit_mentions" fill="var(--series-1)" radius={[4, 4, 0, 0]} isAnimationActive={false} />
              </BarChart>
            </ResponsiveContainer>
            <div>
              <div className="mini-title">By subreddit, this week</div>
              <ReasonList stance="bullish" items={Object.entries(m?.reddit_distribution || {}).map(([c, v]) => ({ category: c, label: `r/${c}`, count: Math.round(v) }))} />
            </div>
          </div>
        </Card>
      )}

      {hasText && <Card className="span-7" title="Text attention over time" subtitle="Weekly text mentions vs. unique authors (dashed line = selected week)">
        <AttentionChart weeks={hist} current={data.week_start} />
      </Card>}
      {hasText && <Card className="span-5" title="Sentiment over time" subtitle="Share of mentions by stance, per week">
        <SentimentHistory weeks={hist} />
      </Card>}

      <Card className="span-12" title="Reddit attention vs. price"
            subtitle="Two separate scales on purpose: attention changes are often 10-100x larger than price changes">
        <div className="two-col">
          <ChangeBars weeks={hist} dataKey={hasRedditHist ? "reddit_change_pct" : "mention_change_pct"}
                      label={hasRedditHist ? "Reddit mentions, week-over-week %" : "Mentions, week-over-week %"}
                      color="var(--series-1)" current={data.week_start} />
          <ChangeBars weeks={hist} dataKey="price_change_pct" label={`Share price, weekly %${isDemoPrice ? " (DEMO prices, not real)" : ""}`} color="var(--series-2)" current={data.week_start} />
        </div>
        {data.price?.attention_vs_price && <p className="neutral-note">{data.price.attention_vs_price}</p>}
      </Card>

      {hasText && <>
      <Card className="span-6" title="Why people are bullish / bearish"
            subtitle={`Reasons stated in mentions, counted by category${data.reasons.methods.includes("fallback_keywords") ? " · keyword fallback" : ""}`}>
        <div className="two-col">
          <div><h3 className="bull-text">Bullish</h3><ReasonList items={data.reasons.bullish} stance="bullish" /></div>
          <div><h3 className="bear-text">Bearish</h3><ReasonList items={data.reasons.bearish} stance="bearish" /></div>
        </div>
      </Card>

      <Card className="span-6" title="Sentiment by subreddit" subtitle="Where the discussion happens and how each community leans">
        <div className="table-wrap">
          <table className="data-table">
            <thead><tr><th>Community</th><th className="num">Mentions</th><th className="num">Bullish</th><th className="num">Bearish</th><th>Net (bear ← → bull)</th></tr></thead>
            <tbody>
              {data.subreddit_sentiment.map((s) => (
                <tr key={s.subreddit}>
                  <td>{community(s.subreddit)}</td>
                  <td className="num">{s.mentions}</td>
                  <td className="num">{fmtPct(s.bullish_pct)}</td>
                  <td className="num">{fmtPct(s.bearish_pct)}</td>
                  <td><NetBar value={s.net_sentiment} /> <span className="num small muted">{s.net_sentiment > 0 ? "+" : ""}{s.net_sentiment.toFixed(0)}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      <Card className="span-6" title="Top threads" subtitle="Posts mentioning the stock first, then threads with the most mentions">
        <ul className="post-list">
          {data.top_posts.map((p, i) => (
            <li key={i}>
              {p.permalink ? <a href={p.permalink} target="_blank" rel="noreferrer">{p.title}</a> : <span>{p.title}</span>}
              <div className="muted small">
                {community(p.subreddit)} · ▲ {fmtInt(p.score)} · {p.num_comments} {p.subreddit === "stocktwits" ? "replies" : "comments"} · {p.mentions_in_thread} mention{p.mentions_in_thread === 1 ? "" : "s"}
                {p.post_sentiment && <> · <span className={`sent-${p.post_sentiment}`}>{p.post_sentiment}</span></>}
                {p.is_demo && " · demo"}
              </div>
            </li>
          ))}
        </ul>
      </Card>

      <Card className="span-6" title="Recent mentions" subtitle="Exact text the classifier saw">
        <ul className="mention-list">
          {data.recent_mentions.slice(0, 8).map((x, i) => (
            <li key={i}>
              <p>“{x.context}”</p>
              <div className="muted small">
                {community(x.subreddit)} · {x.source_type} · <span className={`sent-${x.sentiment}`}>{x.sentiment}</span>
                {" "}({x.sentiment_method === "llm" ? "LLM" : x.sentiment_method === "author_label" ? "author's own tag" : "fallback"}) · detected via {x.detection_method} ({x.confidence})
              </div>
            </li>
          ))}
        </ul>
      </Card>
      </>}
    </main>
  );
}
