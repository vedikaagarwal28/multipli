// Runs frontend/base.html's own renderParameterTable() over a real API response
// and prints the HTML it produces.
//
// The page's script is evaluated as-is rather than copied here, so a change to
// the real render code shows up in the test. Only the DOM it touches on load is
// stubbed — renderParameterTable itself is pure, it just returns a string.
//
//   node frontend_harness.mjs <path-to-base.html>   # response JSON on stdin
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

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

const html = readFileSync(process.argv[2], 'utf8');
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
if (scripts.length === 0) {
  throw new Error('no inline <script> found in the page');
}

const context = vm.createContext({ document, window: {}, console, fetch });
vm.runInContext(scripts.join('\n'), context);

if (typeof context.renderParameterTable !== 'function') {
  throw new Error('renderParameterTable is not defined by the page');
}

console.log(context.renderParameterTable(JSON.parse(readFileSync(0, 'utf8'))));
