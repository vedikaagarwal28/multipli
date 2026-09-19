export const API_BASE = 'http://127.0.0.1:8000';

// Try wallet analysis first; if it cannot produce a result, fall back to
// contract analysis.
export async function analyze(address) {
  let detail = 'No result from the risk API.';

  for (const kind of ['wallet', 'contract']) {
    const response = await fetch(`${API_BASE}/analyze/${kind}/${address}`);
    const body = await response.json().catch(() => ({}));

    if (response.ok) {
      return { kind, features: body.features };
    }
    detail = body.detail ?? `HTTP ${response.status}`;
  }

  return { detail };
}

export function display(value) {
  if (value === null || value === undefined) {
    return '—';
  }
  if (Array.isArray(value)) {
    return value.length > 0 ? value.join(', ') : '—';
  }
  return String(value);
}
