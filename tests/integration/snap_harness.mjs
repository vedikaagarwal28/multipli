// Runs the snap's own api.mjs against a live API and prints the result as JSON,
// so the Python integration test can assert on what the snap would actually
// have received. Nothing is reimplemented here — analyze() is imported from the
// shipped source.
//
//   node snap_harness.mjs <api-base-url> <address>
import { analyze } from '../../snap/src/api.mjs';

const [base, address] = process.argv.slice(2);
console.log(JSON.stringify(await analyze(address, base)));
