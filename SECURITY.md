# Security policy

This software holds service credentials and writes to building automation
systems, so security reports are taken seriously.

## Reporting a vulnerability

**Please don't open a public issue.** Report it privately through GitHub:
[**Report a vulnerability**](https://github.com/KSU-PlantOps/25live-bas-sync/security/advisories/new)
(the *Security* tab → *Advisories*). Include what you found, how to reproduce
it, and what an attacker could do with it.

The report stays private while it's fixed, and the fix is published as a
release with an advisory. If you'd like credit in it, say so.

Private reporting has to be enabled on the repository (Settings → Code
security → Private vulnerability reporting). If the link above doesn't offer
it, open an issue asking a maintainer to contact you, without any details.

## Supported versions

Fixes go into the newest release line. Run the latest release, or the latest
patch of its minor version (the `:X.Y` image tag follows it).

## In scope

- The web UI: sign-in (the local password and Microsoft Entra ID), roles,
  sessions, CSRF, and anything that lets someone do more than their role
  allows.
- Handling of secrets: the environment, `state/secrets.json`, logs, reports and
  the web UI never showing a password.
- The Docker image and entrypoint: privileges, file ownership, what runs as
  root.
- Anything that makes the sync write to a schedule it wasn't mapped to, or
  bypass the safety check without `--force`.

## Out of scope

- **BACnet/IP itself has no authentication.** That's a property of the
  protocol, not a vulnerability here: run the sync on a segmented controls
  network, never expose UDP 47808 to the internet, and see
  [Networking](docs/networking.md).
- A web UI served over plain HTTP, or with `BAS_WEB_AUTH=none`, on a network
  where others can reach it. See [Security](docs/web-ui.md#security) for
  serving it safely.
- Vulnerabilities in dependencies that don't affect this project. Dependabot
  and `pip-audit` in CI keep them current.
