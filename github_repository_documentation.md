# Repository Documentation

[README.md](README.md) is the main guide for the GP11 Election Management and Voting System. It contains the implemented scope and limits, election workflows, architecture, installation, administrator setup, and deployment instructions.

Use these references:

- [Installation and project guide](README.md): Python 3.14 or later, the repository clone URL, local setup, Docker hosting, and automatic deployment.
- [Testing and evidence](docs/TESTING.md): requirement-to-test mapping, reproducible commands, manual checks, and how to record actual results.
- [Application modules](election_system/): routes, election rules, database schema, and templates.
- [Automated tests](tests/): pytest cases and temporary-database fixtures.
- [CI workflow](.github/workflows/ci.yml): automated validation configuration.

The prototype prevents duplicate voting per approved account in each election. It does not implement SMS OTP verification, unique verified phone numbers, encrypted ballot choices, or immutable audit storage. See the scope section in the main guide before describing these capabilities in a report or demonstration.

This page points to the main guide so setup and scope information have one maintained source.
