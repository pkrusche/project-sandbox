# Headless browser testing with Codex

Use `--node-npm` to give Codex an npm environment with Chromium, then keep
repeatable browser tests and their execution instructions in the application
repository. Browser tests can run from an interactive Codex session or an
unsupervised session started with a prompt.

## Install the test dependency on the host

From the application repository, install Playwright Test:

```bash
npm install --save-dev @playwright/test
```

Keep `package.json` and `package-lock.json` in version control. If the project
already uses Playwright, extend its existing configuration and tests instead of
adding a second setup.

At image build time, project-sandbox runs `npm ci` and installs system Chromium
at `/usr/bin/chromium`. It also installs Playwright's matching Chromium when
Playwright is detected in the lockfile. This guide explicitly selects the
system executable through `launchOptions.executablePath`.
`CHROME_BIN` does not configure Playwright, and
`PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright` controls its bundled browser lookup,
not the system executable. Do not run `playwright install` inside a running
sandbox.

If you add or upgrade dependencies, exit the current sandbox and start a new
session to rebuild from the updated manifests. Runtime `node_modules` is
ephemeral; source files, tests, and artifacts under `/workspace` persist on the
host. See [Node.js + npm projects](usage.md#nodejs--npm-projects) for details.

## Configure the application tests

The following example assumes a Vite application whose `dev` npm script starts
Vite. Adapt the server command and port for your framework. Create
`playwright.config.ts` in the application repository:

```ts
import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests/e2e',
  outputDir: './test-results',
  reporter: 'list',
  workers: 1,
  use: {
    browserName: 'chromium',
    headless: true,
    launchOptions: {
      executablePath: '/usr/bin/chromium',
      args: ['--no-sandbox'],
    },
    baseURL: 'http://127.0.0.1:5173',
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
  },
  webServer: {
    command: 'npm run dev -- --host 127.0.0.1 --port 5173 --strictPort',
    url: 'http://127.0.0.1:5173',
    reuseExistingServer: false,
    timeout: 60_000,
  },
});
```

Playwright starts the server, waits for readiness, and stops it after testing.
Both processes run inside the container; no host port forwarding is required.
With `reuseExistingServer: false`, stop any manually started server on that
port before testing. See Playwright's [web server documentation](https://playwright.dev/docs/test-webserver)
and [configuration guide](https://playwright.dev/docs/test-configuration).

Use Chromium only: the sandbox does not preinstall Playwright Firefox or
WebKit. Avoid `channel: 'chrome'`, which selects a separate branded browser.
The example explicitly launches `/usr/bin/chromium` with `--no-sandbox`, as
required inside this container. See Playwright's
[launch options](https://playwright.dev/docs/api/class-browsertype#browser-type-launch).
This path is specific to the Linux sandbox; adapt it for host-side runs.

Add an npm script to the existing `scripts` object in `package.json`:

```json
"test:e2e": "playwright test"
```

Create `tests/e2e/home.spec.ts`:

```ts
import { test, expect } from '@playwright/test';

test('home page renders', async ({ page }) => {
  await page.goto('/');
  // Replace this with the actual accessible heading in your application.
  await expect(page.getByRole('heading', { name: 'Welcome', exact: true }))
    .toBeVisible();
});
```

Add `/test-results/` and `/playwright-report/` to the application's `.gitignore`.
Replace the sample assertion with a real application expectation, and add tests
for its main interactions and error states.

## Give Codex persistent instructions

Add the following section to the application repository's root `AGENTS.md`.
This is for the application being tested, not project-sandbox's own contributor
instructions. Update the command and URL if you changed the example above.
Codex reads repository instructions automatically; see the
[official OpenAI documentation](https://learn.chatgpt.com/docs/agent-configuration/agents-md).

```markdown
## Browser testing

- For changes to user-visible behavior, add or update relevant Playwright tests
  in tests/e2e and run npm run test:e2e. For a focused rerun, use
  npm run test:e2e -- tests/e2e/<file>.spec.ts.
- These tests use disposable local fixtures and have no production access.
  You may run them, fix failures caused by the requested change, and rerun
  affected tests without asking for approval at each step.
- Run Chromium headlessly using playwright.config.ts. Its webServer starts
  the app at http://127.0.0.1:5173, waits for readiness, and stops it afterward.
  Do not start a second server or use a server on the host.
- Set use.launchOptions.executablePath to /usr/bin/chromium and
  use.launchOptions.args to ['--no-sandbox'] in playwright.config.ts.
  For standalone Playwright scripts, pass executablePath: '/usr/bin/chromium',
  headless: true, and args: ['--no-sandbox'] to chromium.launch().
- Dependencies and Chromium are preinstalled by project-sandbox --node-npm.
  Use the existing npm script; do not download packages or browsers at runtime.
  CHROME_BIN and PLAYWRIGHT_BROWSERS_PATH do not select the system executable
  for Playwright; keep the explicit executablePath above. Do not use headed
  mode, UI mode, Chrome channels, Firefox, or WebKit.
- Test the changed user flow and relevant error states with observable
  assertions. Check unexpected browser console errors and uncaught page errors
  when relevant. Use mocked external APIs and local assets so tests work with
  the runtime firewall enabled.
- Failure screenshots and traces go under test-results/. Keep generated
  artifacts out of version control. Capture extra screenshots there when
  visual inspection helps validate the change.
- Report the commands run, pass/fail results, and any remaining blockers.
  If a dependency or browser is missing, report the required host-side change
  and sandbox rebuild rather than silently skipping browser validation.
```

## Start the sandbox and run a task

Start an interactive Codex session:

```bash
project-sandbox --node-npm --agent codex /absolute/path/to/app
```

Then ask Codex to validate a concrete flow, for example:

```text
Test the sign-up form in headless Chromium using the AGENTS.md browser-testing
instructions. Cover successful submission and invalid email input with mocked
API responses. Add repeatable tests, run them, and report the results.
```

To run the same task without an interactive session:

```bash
project-sandbox --node-npm --agent codex /absolute/path/to/app \
  --prompt-text 'Validate the sign-up form using the AGENTS.md browser-testing instructions. Cover successful submission and invalid email input with mocked API responses. Add repeatable tests, run them, and report the results.'
```

From a project-sandbox source checkout, prefix the command with `uv run`.
To verify the setup manually, start with `--agent bash` and run
`npm run test:e2e` inside the container.

## Troubleshooting

- **Browser executable missing:** check that `/usr/bin/chromium` exists and
  `use.launchOptions.executablePath` selects it. A missing executable under
  `/opt/ms-playwright` indicates the explicit launch option is missing or
  overridden. Ensure you started with `--node-npm`; use `--force-build` if you
  need to rebuild an existing image. Restart after dependency changes.
- **Server timeout or port conflict:** check the configured npm command, URL,
  and port. The example's Vite-specific flags do not apply to every framework.
- **External resources fail:** mock third-party calls and serve assets locally.
  For tests that require external hosts, follow the
  [Internet proxy guide](internet-proxy.md#web-tests-that-need-external-hosts).
- **Chromium crashes under load:** reduce test workers or raise `--memory` and
  `--shm-size`. The Node/npm defaults are 8 GB memory and 2 GB shared memory;
  dependencies in tmpfs also consume container memory.
