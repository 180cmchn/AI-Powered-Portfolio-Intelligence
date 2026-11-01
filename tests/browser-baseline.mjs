import {execFileSync} from 'node:child_process';

// Fail before any temporary database, server, or browser is created. Route
// callbacks must only serve these already-read, immutable baseline resources.
export function loadBrowserBaseline(commit, files) {
  const resources = {};
  for (const file of files) {
    const object = `${commit}:app/static/${file}`;
    try {
      resources[file] = execFileSync('git', ['show', object], {
        encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'],
      });
    } catch (cause) {
      throw new Error(
        `Cannot load browser baseline ${object}. The checkout may lack the required Git history. ` +
        `Use actions/checkout with fetch-depth: 0, or explicitly fetch commit ${commit}, ` +
        `and verify that the baseline static file exists. No test server or browser has been started.`,
        {cause},
      );
    }
  }
  return Object.freeze(resources);
}
