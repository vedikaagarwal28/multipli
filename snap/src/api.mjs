export const API_BASE = 'http://127.0.0.1:8000';

// The backend already runs the eth_getCode check and answers 400 on an EOA,
// so ask /contract first and read that 400 as "route to /wallet" instead of
// duplicating the code lookup here.
export async function analyze(address) {
  let detail = 'No result from the risk API.';

  for (const kind of ['contract', 'wallet']) {
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
