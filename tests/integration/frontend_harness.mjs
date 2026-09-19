// Runs frontend/base.html's own renderParameterTable() over a real API response
// and prints the HTML it produces.
//
// The page's script is evaluated as-is rather than copied here, so a change to
// the real render code shows up in the test. Only the DOM it touches on load is
// stubbed — renderParameterTable itself is pure, it just returns a string.
//
//   node frontend_harness.mjs <path-to-base.html>                   # response JSON on stdin
//   node frontend_harness.mjs <path-to-base.html> <api-url> <addr>  # -> tab the page picked
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const [, , pagePath, apiUrl, address] = process.argv;

const element = {
  addEventListener() {},
  querySelectorAll: () => [],
  classList: { add() {}, remove() {}, toggle() {} },
  textContent: '',
  innerHTML: '',
  disabled: false,
  value: '',
  style: {},
  dataset: {},
};

const document = {
  getElementById: () => element,
  querySelectorAll: () => [],
  addEventListener() {},
};

const html = readFileSync(pagePath, 'utf8');
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
if (scripts.length === 0) {
  throw new Error('no inline <script> found in the page');
}

// API_BASE is a const in the page, so the test server's ephemeral port is
// swapped in at the fetch boundary instead of reassigning it. Each call is
// recorded, which is how the contract-then-wallet fall-through is observed —
// the page's own `state` is a const too, so it never reaches this scope.
const calls = [];
const pointAtTestServer = async (url, options) => {
  const response = await fetch(String(url).replace('http://127.0.0.1:8000', apiUrl), options);
  calls.push({ endpoint: String(url).split('/analyze/')[1].split('/')[0], status: response.status });
  return response;
};

const context = vm.createContext({
  document,
  window: {},
  console,
  fetch: apiUrl ? pointAtTestServer : fetch,
});
vm.runInContext(scripts.join('\n'), context);

if (typeof context.renderParameterTable !== 'function') {
  throw new Error('renderParameterTable is not defined by the page');
}

if (!address) {
  console.log(context.renderParameterTable(JSON.parse(readFileSync(0, 'utf8'))));
} else {
  // showTab is a top-level declaration, so analyzeAddress's call resolves
  // through the context global and this records which tab it settled on.
  let chosen = null;
  context.showTab = (target) => { chosen = target; };
  await context.analyzeAddress(address);
  console.log(JSON.stringify({ tab: chosen, calls }));
}
