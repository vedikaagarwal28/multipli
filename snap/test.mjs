import assert from 'node:assert';

import { analyze, display } from './src/api.mjs';

function stubFetch(routes) {
  globalThis.fetch = async (url) => {
    const kind = url.includes('/analyze/contract/') ? 'contract' : 'wallet';
    const [status, body] = routes[kind] ?? [500, {}];
    return { ok: status === 200, status, json: async () => body };
  };
}

stubFetch({ contract: [200, { features: { address: '0xa', is_proxy: true } }] });
assert.equal((await analyze('0xa')).kind, 'contract');

stubFetch({
  contract: [400, { detail: 'address has no code — this is an EOA' }],
  wallet: [200, { features: { address: '0xa', total_tx_count: 7 } }],
});
const eoa = await analyze('0xa');
assert.equal(eoa.kind, 'wallet');
assert.equal(eoa.features.total_tx_count, 7);

stubFetch({
  contract: [400, { detail: 'no code' }],
  wallet: [404, { detail: 'no on-chain history found for this address' }],
});
const miss = await analyze('0xa');
assert.equal(miss.features, undefined);
assert.equal(miss.detail, 'no on-chain history found for this address');

assert.equal(display(null), '—');
assert.equal(display([]), '—');
assert.equal(display(['a', 'b']), 'a, b');
assert.equal(display(0), '0');
assert.equal(display(false), 'false');

console.log('ok');
