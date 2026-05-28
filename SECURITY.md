# Security Policy

## Reporting a vulnerability

If you discover a security vulnerability, **do not open a public issue**.

Email the project maintainer with details. You should receive a response within 48 hours.

## Bot token safety

- Never commit your `.env` file or bot token. The `.gitignore` excludes `.env` by default.
- Regenerate your token via [@BotFather](https://t.me/BotFather) if it's ever exposed.
- The bot runs with the permissions you give it. Use a dedicated token, not one shared with other bots.

## Dependencies

Dependencies are pinned to specific versions in `requirements.txt` and `package.json`. Dependabot / Renovate PRs are welcome.
