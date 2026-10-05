import React from "react";
import { Bar, BarChart, CartesianGrid, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { changeClass, community, fmtChange, fmtInt, fmtPct } from "./format.js";

// Sentiment is polarity -> diverging pair (blue = bullish, red = bearish) with gray neutral.
// Colour is never alone: segments have gaps, a legend, and text labels/tooltips.
export const SENTIMENT = [
  { key: "bullish", label: "Bullish", color: "var(--bull)" },
  { key: "neutral", label: "Neutral", color: "var(--neutral)" },
  { key: "bearish", label: "Bearish", color: "var(--bear)" },
  { key: "unclear", label: "Unclear", color: "var(--unclear)" },
];

export function Card({ title, subtitle, children, className = "", right }) {
  return (
    <section className={`card ${className}`}>
      {(title || right) && (
        <header className="card-head">
          <div>
            {title && <h2>{title}</h2>}
            {subtitle && <p className="muted small">{subtitle}</p>}
          </div>
          {right}
        </header>
      )}
      {children}
    </section>
  );
}

export function Overview({ overview, prevOverview }) {
  // Only compare with last week when both are complete, fully collected weeks.
  const comparable = prevOverview && !overview.in_progress && overview.baseline_available !== false &&
    prevOverview.text_coverage?.complete !== false && overview.text_coverage?.complete !== false;
  const delta = (k) => {
    if (!comparable || !prevOverview[k]) return null;
    return (100 * (overview[k] - prevOverview[k])) / prevOverview[k];
  };
  const tiles = [
    ["Posts / messages", "posts"],
    ["Comments", "comments"],
    ["Text mentions", "total_mentions"],
    ["Unique authors", "unique_authors"],
    ["Stocks detected", "stocks_detected"],
  ];
  const ra = overview.reddit_attention;
  const pra = prevOverview?.reddit_attention;
  // Reddit counts are per-day estimates, so they compare fairly even mid-week (when both weeks have data).
  const rd = ra && pra?.total_mentions ? (100 * (ra.total_mentions - pra.total_mentions)) / pra.total_mentions : null;
  return (
    <div className="tiles">
      {ra && (
        <div className="tile tile-reddit" title="Estimated weekly Reddit mentions from daily count snapshots (no text)">
          <div className="tile-label">Reddit mentions (est.)</div>
          <div className="tile-value">{fmtInt(ra.total_mentions)}</div>
          <div className={`tile-delta ${changeClass(rd)}`}>
            {rd == null ? "" : `${fmtChange(rd)} vs prev week · `}{ra.days_covered}/7 days · {ra.source === "demo" ? "demo" : "ApeWisdom"}
          </div>
        </div>
      )}
      {tiles.map(([label, k]) => {
        const d = delta(k);
        return (
          <div className="tile" key={k}>
            <div className="tile-label">{label}</div>
            <div className="tile-value">{fmtInt(overview[k])}</div>
            <div className={`tile-delta ${changeClass(d)}`}>
              {d == null ? " " : `${fmtChange(d)} vs prev week`}
            </div>
          </div>
        );
      })}
    </div>
  );
}

export function TrendBadge({ cls }) {
  const title = cls === "UNRATED" ? "Not enough collected history to compare with an earlier week yet" : undefined;
  return <span className={`badge badge-${cls.toLowerCase()}`} title={title}>{cls}</span>;
}

// Text week-over-week: "–" when there is no comparable earlier week (not "NEW").
export const textChange = (s) => (s.text_baseline === false ? "–" : fmtChange(s.mention_change_pct));

function ScoreBar({ score, unrated }) {
  if (unrated) return <span className="muted">–</span>;
  return (
    <div className="score">
      <span className="num">{score.toFixed(0)}</span>
      <span className="score-track">
        <span className="score-fill" style={{ width: `${score}%` }} />
      </span>
    </div>
  );
}

export function TopTable({ stocks }) {
  const hasReddit = stocks.some((s) => s.reddit_mentions != null);
  return (
    <div className="table-wrap">
      <table className="data-table">
        <thead>
          {hasReddit && (
            <tr className="group-head">
              <th colSpan={3} />
              <th colSpan={2} className="grp">Reddit · counts</th>
              <th colSpan={5} className="grp">Text sample · who &amp; why</th>
              <th colSpan={2} />
            </tr>
          )}
          <tr>
            <th className="num">#</th>
            <th>Ticker</th>
            <th>Company</th>
            {hasReddit && <th className="num">Mentions</th>}
            {hasReddit && <th className="num">WoW</th>}
            <th className="num">Mentions</th>
            <th className="num">Authors</th>
            <th className="num">WoW</th>
            <th className="num">Bullish</th>
            <th className="num">Bearish</th>
            <th>Trend score</th>
            <th>Trend</th>
          </tr>
        </thead>
        <tbody>
          {stocks.map((s) => (
            <tr key={s.ticker}>
              <td className="num muted">{s.rank}</td>
              <td className="ticker"><a href={`#/stock/${s.ticker}`}>{s.ticker}</a></td>
              <td className="company" title={s.company}>{s.company}</td>
              {hasReddit && <td className="num">{s.reddit_mentions == null ? "–" : fmtInt(s.reddit_mentions)}</td>}
              {hasReddit && (
                <td className={`num ${s.reddit_prev_mentions == null ? "" : changeClass(s.reddit_change_pct)}`}>
                  {s.reddit_prev_mentions == null ? "–" : fmtChange(s.reddit_change_pct)}
                </td>
              )}
              <td className="num">{s.mentions ? fmtInt(s.mentions) : <span className="muted">–</span>}</td>
              <td className="num" title={`Top single author wrote ${(s.top_author_share * 100).toFixed(0)}% of mentions`}>
                {fmtInt(s.unique_authors)}
                {s.top_author_share >= 0.25 && <span className="warn-dot" aria-label="concentrated">●</span>}
              </td>
              <td className={`num ${s.mentions && s.text_baseline !== false ? changeClass(s.mention_change_pct) : ""}`}>{s.mentions ? textChange(s) : "–"}</td>
              <td className="num">{s.mentions ? fmtPct(s.bullish_pct) : "–"}</td>
              <td className="num">{s.mentions ? fmtPct(s.bearish_pct) : "–"}</td>
              <td><ScoreBar score={s.trend_score} unrated={s.trend_class === "UNRATED"} /></td>
              <td><TrendBadge cls={s.trend_class} /></td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="muted small footnote">
        <span className="warn-dot">●</span> one account wrote ≥25% of this stock's mentions, so mention counts overstate breadth.
        WoW = week-over-week change in mentions; NEW = no mentions the previous week.
        {hasReddit && " Reddit counts are estimated weekly mentions from daily snapshots (no text, so no sentiment). \"–\" in the text columns = the ticker wasn't in the text sample."}
      </p>
    </div>
  );
}

export function EmergingCards({ stocks, mode = "trend" }) {
  if (!stocks.length) {
    return <p className="muted">{mode === "early" ? "No early signals this week." : "No stock is gaining unusual attention this week."}</p>;
  }
  return (
    <div className="em-grid">
      {stocks.map((s) => (
        <a className="em-card" key={s.ticker} href={`#/stock/${s.ticker}`}>
          <div className="em-top">
            <div>
              <div className="ticker big">{s.ticker}</div>
              <div className="muted small">{s.company}</div>
            </div>
            {mode === "early" ? <span className="badge badge-early">EARLY SIGNAL</span> : <TrendBadge cls={s.trend_class} />}
          </div>
          <div className="em-score">
            <span className="em-score-num">{(mode === "early" ? s.early_signal_score : s.trend_score).toFixed(0)}</span>
            <span className="muted small">{mode === "early" ? "early signal score" : "trend score"}</span>
          </div>
          <dl className="em-stats">
            {s.reddit_mentions != null && (
              <div><dt>Reddit</dt><dd>{s.reddit_prev_mentions == null ? "" : `${fmtInt(s.reddit_prev_mentions)} → `}{fmtInt(s.reddit_mentions)} <span className={changeClass(s.reddit_change_pct)}>{s.reddit_prev_mentions == null ? "" : `(${fmtChange(s.reddit_change_pct)})`}</span></dd></div>
            )}
            {s.mentions > 0 ? (
              <>
                <div><dt>Text mentions</dt><dd>{s.text_baseline === false ? s.mentions : <>{s.prev_mentions} → {s.mentions} <span className={changeClass(s.mention_change_pct)}>({fmtChange(s.mention_change_pct)})</span></>}</dd></div>
                <div><dt>Authors</dt><dd>{s.text_baseline === false ? s.unique_authors : <>{s.prev_unique_authors} → {s.unique_authors} <span className={changeClass(s.author_change_pct)}>({fmtChange(s.author_change_pct)})</span></>}</dd></div>
                <div><dt>Bull / Bear</dt><dd>{fmtPct(s.bullish_pct)} / {fmtPct(s.bearish_pct)}</dd></div>
              </>
            ) : (
              <div><dt>Text</dt><dd className="muted">not in text sample</dd></div>
            )}
            <div><dt>Communities</dt><dd>{new Set([...Object.keys(s.subreddit_distribution || {}), ...Object.keys(s.reddit_distribution || {})]).size}</dd></div>
            {mode === "early" && <div><dt>Avg engagement</dt><dd>▲ {fmtInt(Math.round(s.avg_engagement))}</dd></div>}
          </dl>
          {s.attention_vs_price && mode !== "early" && <p className="em-note muted small">{s.attention_vs_price.split(". ")[0]}.</p>}
        </a>
      ))}
    </div>
  );
}

function Legend() {
  return (
    <ul className="legend">
      {SENTIMENT.map((s) => (
        <li key={s.key}><span className="swatch" style={{ background: s.color }} />{s.label}</li>
      ))}
    </ul>
  );
}

function SentimentTooltip({ active, payload, label }) {
  if (!active || !payload?.length) return null;
  const row = payload[0].payload;
  return (
    <div className="tooltip">
      <div className="tooltip-title">{label}</div>
      {SENTIMENT.map((s) => (
        <div className="tooltip-row" key={s.key}>
          <span className="swatch" style={{ background: s.color }} />
          <span>{s.label}</span>
          <span className="num">{row[`${s.key}_n`]} · {fmtPct(row[s.key])}</span>
        </div>
      ))}
      <div className="tooltip-row muted"><span /><span>Mentions</span><span className="num">{row.total}</span></div>
    </div>
  );
}

export function SentimentChart({ sentiment }) {
  const o = sentiment.overall;
  const pct = (n, total) => (total ? (100 * n) / total : 0);
  const row = (label, counts, total) => ({
    label,
    total,
    ...Object.fromEntries(SENTIMENT.map((s) => [s.key, pct(counts[s.key], total)])),
    ...Object.fromEntries(SENTIMENT.map((s) => [`${s.key}_n`, counts[s.key]])),
  });
  const data = [
    row("All stocks", Object.fromEntries(SENTIMENT.map((s) => [s.key, o[s.key].count])), o.total),
    ...sentiment.stocks.slice(0, 8).map((s) => row(s.ticker, s, s.mentions)),
  ];
  const fallback = sentiment.methods.includes("fallback_keywords");
  return (
    <>
      <div className="sent-headline">
        <span><b className="bull-text">{fmtPct(o.bullish.pct)}</b> bullish</span>
        <span><b>{fmtPct(o.neutral.pct)}</b> neutral</span>
        <span><b className="bear-text">{fmtPct(o.bearish.pct)}</b> bearish</span>
      </div>
      <Legend />
      <ResponsiveContainer width="100%" height={36 * data.length + 30}>
        <BarChart data={data} layout="vertical" margin={{ top: 4, right: 8, bottom: 0, left: 0 }} barCategoryGap={8}>
          <CartesianGrid horizontal={false} stroke="var(--grid)" />
          <XAxis type="number" domain={[0, 100]} ticks={[0, 25, 50, 75, 100]} tickFormatter={(v) => `${v}%`}
                 stroke="var(--axis)" tick={{ fill: "var(--text-muted)", fontSize: 11 }} />
          <YAxis type="category" dataKey="label" width={78} stroke="var(--axis)"
                 tick={{ fill: "var(--text-secondary)", fontSize: 12 }} />
          <Tooltip content={<SentimentTooltip />} cursor={{ fill: "var(--hover)" }} />
          {SENTIMENT.map((s, i) => (
            <Bar key={s.key} dataKey={s.key} stackId="s" fill={s.color} stroke="var(--surface)" strokeWidth={2}
                 radius={i === 0 ? [4, 0, 0, 4] : i === SENTIMENT.length - 1 ? [0, 4, 4, 0] : 0}
                 isAnimationActive={false} />
          ))}
        </BarChart>
      </ResponsiveContainer>
      {fallback && (
        <p className="muted small footnote">
          ⚠ Sentiment from the keyword <b>fallback</b> classifier (no Anthropic API key). Expect lower accuracy on sarcasm.
        </p>
      )}
    </>
  );
}

function SubTooltip({ active, payload }) {
  if (!active || !payload?.length) return null;
  const r = payload[0].payload;
  return (
    <div className="tooltip">
      <div className="tooltip-title">{community(r.subreddit)}</div>
      <div className="tooltip-row"><span /><span>Stock mentions</span><span className="num">{r.mentions}</span></div>
      <div className="tooltip-row"><span /><span>Posts</span><span className="num">{r.posts}</span></div>
      <div className="tooltip-row"><span /><span>Comments</span><span className="num">{r.comments}</span></div>
    </div>
  );
}

export function SubredditChart({ activity }) {
  const data = activity.map((a) => ({ ...a, name: community(a.subreddit) }));
  return (
    <ResponsiveContainer width="100%" height={40 * data.length + 30}>
      <BarChart data={data} layout="vertical" margin={{ top: 4, right: 40, bottom: 0, left: 0 }} barCategoryGap={10}>
        <CartesianGrid horizontal={false} stroke="var(--grid)" />
        <XAxis type="number" stroke="var(--axis)" tick={{ fill: "var(--text-muted)", fontSize: 11 }} allowDecimals={false} />
        <YAxis type="category" dataKey="name" width={130} stroke="var(--axis)"
               tick={{ fill: "var(--text-secondary)", fontSize: 12 }} />
        <Tooltip content={<SubTooltip />} cursor={{ fill: "var(--hover)" }} />
        <Bar dataKey="mentions" fill="var(--series-1)" radius={[0, 4, 4, 0]} isAnimationActive={false}
             label={{ position: "right", fill: "var(--text-secondary)", fontSize: 11 }}>
          {data.map((d) => <Cell key={d.subreddit} />)}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

export function SummaryBox({ report }) {
  const s = report.summary;
  const sections = [
    ["data", "Data", "Measured from collected posts and comments."],
    ["interpretation", "Interpretation", "What the numbers suggest. Reasoned, not measured."],
    ["speculation", "Speculation", "Hypotheses only. Not facts, not predictions."],
  ];
  return (
    <div className="summary">
      <div className="summary-meta muted small">
        {report.summary_method === "llm" ? "Written by Claude from the computed metrics" : "Template summary generated from the computed metrics (no LLM)"}
        {report.is_demo && " · DEMO DATA"}
      </div>
      <div className="summary-grid">
        {sections.map(([k, title, hint]) => (
          <div className={`summary-col sum-${k}`} key={k}>
            <h3>{title}</h3>
            <p className="muted small">{hint}</p>
            <ul>{(s[k] || []).map((line, i) => <li key={i}>{line}</li>)}</ul>
          </div>
        ))}
      </div>
      <p className="disclaimer small">{s.disclaimer}</p>
    </div>
  );
}

export function SubredditTable({ subs }) {
  return (
    <div className="table-wrap">
      <table className="data-table compact">
        <thead>
          <tr><th>Community</th><th className="num">Authors</th><th className="num">Bull</th><th className="num">Bear</th><th>Most discussed</th></tr>
        </thead>
        <tbody>
          {subs.map((s) => (
            <tr key={s.subreddit}>
              <td>{community(s.subreddit)}</td>
              <td className="num">{fmtInt(s.mentioning_authors)}</td>
              <td className="num">{fmtPct(s.bullish_pct)}</td>
              <td className="num">{fmtPct(s.bearish_pct)}</td>
              <td className="tickers-cell">
                {s.top_tickers.slice(0, 3).map((t) => (
                  <a key={t.ticker} href={`#/stock/${t.ticker}`} className="chip">{t.ticker} <span className="muted">{t.mentions}</span></a>
                ))}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function RedditCommunities({ comms }) {
  if (!comms?.length) return null;
  return (
    <>
      <h3 className="sub-head">Reddit attention by subreddit <span className="muted small">(counts only · est. weekly mentions)</span></h3>
      <div className="table-wrap">
        <table className="data-table compact">
          <thead><tr><th>Subreddit</th><th className="num">Mentions</th><th>Most discussed</th></tr></thead>
          <tbody>
            {comms.map((c) => (
              <tr key={c.community}>
                <td>r/{c.community}</td>
                <td className="num">{fmtInt(c.mentions)}</td>
                <td className="tickers-cell">
                  {c.top_tickers.slice(0, 4).map((t) => (
                    <a key={t.ticker} href={`#/stock/${t.ticker}`} className="chip">{t.ticker} <span className="muted">{fmtInt(t.mentions)}</span></a>
                  ))}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
