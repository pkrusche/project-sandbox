# Roadmap

This file contains functionality researched but not yet planned for execution
via <TODO.md>.

## Prebuilt container image distribution

The generated container image is currently intended for local use. Before publishing
or otherwise distributing prebuilt images, complete a third-party licensing and terms
review and make the image carry the notices required by every bundled component.

- Preserve Node.js's license in the image. The generated Dockerfile currently excludes
  `LICENSE` while extracting the official Node.js archive; a distributable image must
  retain or separately reproduce the required copyright and license notice.
- Include the Apache-2.0 license and any applicable notices for the bundled `jj` binary.
- Verify that globally installed npm packages retain their license files and required
  notices, including their transitive production dependencies. Generate and review an
  SBOM and a consolidated third-party-notices file for each released image.
- Treat Claude Code separately from the permissively licensed agents. Its package is
  proprietary and subject to Anthropic's legal agreements; obtain written clarification
  or permission before redistributing an image that embeds it.
- Re-run the review whenever a pinned component or base image changes. Include the base
  image, Debian packages, Node.js, npm packages, downloaded binaries, and generated
  application files in the review scope.
- Never publish an image or layer containing staged user credentials, authentication
  state, project data, build secrets, or local configuration. Build release images from
  a clean, credential-free context and scan the final image before publication.
