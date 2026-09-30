# TODO - outstanding items for next release

## `--node-npm` follow-ups

- pnpm, yarn, and bun support (needs corepack or pinned package-manager installs; Node 25+ no longer bundles corepack).
- Devcontainer support for `--node-npm`: the generated devcontainer has no `node_modules` tmpfs or `--shm-size`.
- Volume-cached `node_modules` keyed by lockfile/image hash, to avoid copying the tree into tmpfs on every session start.
- Puppeteer and Cypress browser downloads (Puppeteer currently uses system Chromium; Cypress is untested).
- CJK fonts (`fonts-noto-cjk`) for screenshot tests of CJK content.
- Per-project Node.js version selection.
- Verify Apple container `--tmpfs` / `--shm-size` behaviour (openspec change `add-node-npm-flavor` task 1.1) and run the end-to-end checks (tasks 3.3, 4.3).
