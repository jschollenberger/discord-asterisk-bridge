# Security Policy

## Reporting a vulnerability

**Please do not report security vulnerabilities through public GitHub issues,
pull requests, or discussions.**

Report privately through GitHub's built-in advisory flow:

1. Go to the [**Security** tab](https://github.com/jschollenberger/discord-asterisk-bridge/security)
   of this repository.
2. Click **Report a vulnerability** to open a private security advisory.

This keeps the report confidential between you and the maintainer until a fix
is available. Please include enough detail to reproduce — affected version or
commit, configuration relevant to the issue (with credentials redacted), and
the impact you observed.

There is no formal SLA on this project (it is maintained by volunteers for an
amateur-radio club), but reports are taken seriously and acknowledged as
promptly as is practical.

## Supported versions

Only the latest release and the current `main` receive security fixes. This is
a source-run application, not a published package — "upgrading" means pulling
the latest `main` (or the latest tag) and restarting.

| Version | Supported |
|---|---|
| `main` (latest) | ✅ |
| `1.0.0` | ✅ |
| < 1.0.0 | ❌ (pre-release internal builds; never tagged) |

## Operator responsibilities

This bot is self-hosted, and most of its security surface is in how it is
*deployed*, not in the code. If you run it, you are responsible for:

- **Protecting `config.yaml`.** It holds live secrets — the Discord bot
  token(s), Asterisk Manager Interface (AMI) credentials, and SIP peer
  secrets. It is gitignored on purpose (`/config.yaml`); **never commit it**.
  Restrict its permissions (`chmod 600 config.yaml`) so only the running user
  can read it.
- **Rotating a leaked token immediately.** Anyone with the Discord bot token
  controls the bot. If it is exposed (committed, pasted, logged), reset it in
  the [Discord Developer Portal](https://discord.com/developers/applications)
  and update `config.yaml`. The same applies to AMI and SIP credentials on the
  Asterisk side.
- **Scoping the bot's Discord permissions.** Grant only the permissions listed
  in [Creating the Discord bot](README.md#creating-the-discord-bot) — do **not**
  grant Administrator. The bot needs no moderation, role-management, or message-
  management permissions.
- **Understanding the TX trust model.** Transmit (Discord → repeater) is gated
  by an allowlist, a per-repeater lock, a maximum transmission length, and an
  admin kill switch — but read the TX comments in `config.example.yaml` for
  exactly what the bot can and cannot enforce about *who* is allowed to key the
  repeater. Enabling TX has real-world (FCC / licensing) implications that are
  the operator's responsibility, not the software's.
- **Securing the Asterisk side.** AMI and SIP endpoints should not be exposed
  to untrusted networks. Setting `rtptimeout=60` in `sip.conf` is recommended
  (see the README "Known issues") so stale dialogs self-clear.

## Dependency posture

CI runs a weekly `pip-audit` scan of the dependency tree (see
[`.github/workflows/ci.yml`](.github/workflows/ci.yml)). It is advisory
(non-blocking) so an upstream CVE with no released fix does not freeze
development, but it stays loudly visible on every run.

One advisory is **knowingly suppressed by ID**, not ignored wholesale:

- **PYSEC-2026-3002** (PyNaCl 1.5.0). The fix (1.6.2) is excluded by
  `discord.py[voice]`'s own `PyNaCl<1.6` constraint, so it is unfixable here
  until upstream relaxes that cap. Suppressing this single ID keeps the audit
  meaningful — any *new* advisory still surfaces instead of being lost in a
  permanently-failing run. See the note in
  [`requirements.txt`](requirements.txt); revisit when discord.py relaxes the
  cap.

Two dependencies (`rfcvoip`, `discord-ext-voice-recv`) are pinned to exact
versions because the project patches and reaches into their internals — bumps
go through Dependabot PRs with CI, and the pins carry comments describing what
to re-test. This is a maintainability measure, but it is also a supply-chain
one: an unreviewed upgrade of either could silently change behavior the bot
depends on.
