// All requests go to /api (proxied to the FastAPI backend). No secrets live in the frontend.
const BASE = import.meta.env.VITE_API_BASE || "/api";

async function request(path, options = {}) {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail || detail;
    } catch {
      /* non-JSON error body */
    }
    const err = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    err.status = res.status;
    throw err;
  }
  return res.json();
}

const q = (week) => (week ? `?week=${week}` : "");

export const api = {
  health: () => request("/health"),
  weeks: () => request("/weeks"),
  stocks: (week) => request(`/stocks${q(week)}${week ? "&" : "?"}limit=25`),
  emerging: (week) => request(`/emerging${q(week)}`),
  sentiment: (week) => request(`/sentiment${q(week)}`),
  report: (week) => request(`/weekly-report${q(week)}`),
  run: () => request("/analysis/run", { method: "POST", body: "{}" }),
};
