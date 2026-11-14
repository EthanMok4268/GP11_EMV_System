# Testing and Evidence

The tests check the implemented single-choice election workflow. This guide maps the main requirements to automated checks and lists the manual evidence needed for a project report. A passing test suite does not establish identity verification, encrypted ballots, load capacity, or live deployment health.

## Run the automated tests

Use Python 3.14 or later and follow the [installation guide](../README.md#-installation--setup-guide). Run these commands from the repository root, with the project environment active:

```bash
python --version
python -m pip install -r requirements.txt
python -m pytest -q
```

Application tests create temporary SQLite databases and signing keys. They do not need a configured administrator, a production database, SMS credentials, or a running web server. Flask test clients submit forms with CSRF protection enabled.

The deployment tool uses POSIX file locking. Its tests in `test_deploy.py` run on Linux and are skipped on Windows when `fcntl` is unavailable. The GitHub Actions workflow runs the full suite on Ubuntu with Python 3.14, validates Compose, and builds the application image. See [CI](../.github/workflows/ci.yml).

For a recorded run, save the tested commit, runtime, dependency versions, console output, and JUnit results:

```bash
git rev-parse HEAD
git status --short
python --version
python -m pip freeze
python -m pytest -q --junitxml=pytest-results.xml
```

Keep the generated XML and console log with the evidence for that run, outside the source checkout if they are not intended for Git. Record any uncommitted changes. Do not publish credentials, session cookies, personal voter data, or a real election database.

## Requirement coverage

These areas describe the current implementation. They do not assert that every requirement in a separate project proposal has been implemented.

| Requirement or rule | Automated evidence | Expected behaviour |
| --- | --- | --- |
| Registration, login, logout, and password rules | [test_app.py](../tests/test_app.py), [test_login_security.py](../tests/test_login_security.py) | Valid accounts can sign in; invalid credentials and weak new passwords are rejected; stale sessions are invalidated |
| Administrator-only management | [test_app.py](../tests/test_app.py), [test_election_creation.py](../tests/test_election_creation.py), [test_election_deletion.py](../tests/test_election_deletion.py) | Voters cannot perform administrative actions, including by direct POST |
| Draft creation, complete rosters, and candidate editing | [test_election_creation.py](../tests/test_election_creation.py), [test_candidate_management.py](../tests/test_candidate_management.py) | Drafts remain private; confirmation requires a complete roster; roster changes revoke confirmation; active rosters are locked |
| Election-specific participation and approval | [test_voter_eligibility.py](../tests/test_voter_eligibility.py), [test_app.py](../tests/test_app.py) | Only an approved account can vote in that election; closed elections reject new requests and decisions |
| One accepted ballot per account per election | [test_app.py](../tests/test_app.py), [test_concurrent_voting.py](../tests/test_concurrent_voting.py) | A repeat submission is rejected, including two overlapping requests from the same account |
| Atomic ballots and participation status | [test_app.py](../tests/test_app.py), [test_candidate_management.py](../tests/test_candidate_management.py), [test_concurrent_voting.py](../tests/test_concurrent_voting.py) | A failed ballot write rolls back participation; queued requests recheck current state; an accepted ballot and `has_voted` stay consistent |
| Candidate belongs to the selected election | [test_ballot_integrity.py](../tests/test_ballot_integrity.py) | Cross-election ballot references are rejected by database constraints |
| Voting window and results publication | [test_app.py](../tests/test_app.py) | Voting stops at the cutoff or early closure; results are displayed only after closure |
| CSRF protection and template escaping | [test_csrf.py](../tests/test_csrf.py), [test_app.py](../tests/test_app.py), [test_audit_logs.py](../tests/test_audit_logs.py) | Invalid form tokens are rejected; displayed inputs are escaped |
| Administrative audit history | [test_audit_logs.py](../tests/test_audit_logs.py), management tests above | Only administrators can view the history; pagination is stable; changes and their audit writes commit or roll back together |
| Initialization and legacy data integrity | [test_admin_cli.py](../tests/test_admin_cli.py), [test_ballot_integrity.py](../tests/test_ballot_integrity.py), [test_candidate_management.py](../tests/test_candidate_management.py) | Initialization preserves valid data and rejects inconsistent legacy records without a partial migration |
| CI-gated deployment decisions and recovery logic | [test_deploy.py](../tests/test_deploy.py), Linux only | Mocked GitHub/Docker operations exercise CI gating, failures, backup retention, and recovery decisions |

## Concurrent voting regression

Run the new cases separately with:

```bash
python -m pytest -q tests/test_concurrent_voting.py
```

The two cases submit the same choice or different choices from two independently authenticated clients for the same approved voter. Each client has its own session, CSRF token, and SQLite connection. A real SQLite write lock holds both requests until SQL tracing confirms that each has reached its voting transaction; the lock is then released. The test uses bounded waits rather than timing-based sleeps.

Each case requires exactly one success response and one duplicate-vote rejection, one ballot for the successful request's candidate, `has_voted = 1`, unchanged approval, and a tally matching that ballot. It does not require a particular request to win. This checks correctness under contention, not requests per second or a capacity target.

## Manual acceptance checks

Use a separate test database with synthetic accounts. Run the application using the local setup guide, or use an isolated deployment. Record actual results and screenshots; the checklist itself is not a completed test report.

| Check | Steps | Expected result |
| --- | --- | --- |
| Full voting flow | Create an election with two candidates and confirm it; register a voter; request participation; approve as admin; vote while active | A pending voter is rejected; an approved voter receives confirmation; a second vote is rejected |
| Roles and timing | Try management as a voter; check a draft, upcoming election, active election, and closed election | Management is administrator-only; drafts are hidden from voters; voting is available only while active |
| Results | Vote with known synthetic choices, check before closure, then close the election | No candidate tallies are shown before closure; final candidate totals match the accepted ballots, including zero-vote candidates |
| Desktop and mobile | Repeat registration, participation, voting, candidate entry, and deletion-dialog cancellation at recorded viewport sizes | Forms remain readable and usable; cancellation performs no deletion; validation is clear |
| Keyboard interaction | Navigate forms and confirmation dialogs using Tab, Shift+Tab, Enter, and Escape where applicable | Controls are reachable and focus is visible; dialog cancellation behaves correctly |
| Persistence and recovery | Restart an isolated instance; rehearse a backup and restore with disposable data | Expected accounts, election settings, and ballots persist or restore correctly |
| Public demo, if in scope | Use the actual HTTPS hostname, sign in, submit a form, and restart the isolated demo | HTTPS and session/form behaviour work; deployment and data persistence are checked separately from the image build |

The repository does not include a reproducible browser automation suite. Pytest uses Flask clients rather than a real browser, so it cannot establish layout, JavaScript interaction, keyboard accessibility, or mobile usability. A keyboard check alone is not WCAG certification.

## Performance evidence

If the group retains the requirement for 20 concurrent users and at least 95% of normal page requests within three seconds, run a separate load test. Record:

- The tested commit, hardware, operating system, deployment settings, and database size.
- The selected normal-page journey, authenticated users, concurrency, test duration, and request count.
- Latency distribution including the 95th percentile, failed requests, and the pass/fail decision against the requirement.

Use synthetic accounts and an isolated database. A two-request voting regression is not a 20-user load test. No load result is supplied by this guide. A 10,000-user target, failover, or an uptime percentage needs its own agreed scope and evidence.

## Record results honestly

Use one row per check or a linked machine-readable result:

| Field | What to record |
| --- | --- |
| Requirement and test | Requirement reference, test name or manual check, and purpose |
| Version and environment | Commit, local changes, Python/dependency versions, OS, and browser/device where relevant |
| Execution | Date, command or exact steps, inputs, and who or what ran the check |
| Outcome | Expected result, observed result, pass/fail/skip, and reason for a skip |
| Evidence | CI job URL, console log, JUnit report, or relevant screenshot |

Distinguish automated CI or agent runs from testing personally performed by group members. Do not record planned work as a passed test. Test counts may change with parameterization and platform-specific skips, so quote them together with the tested version and environment.

SMS OTP verification, unique verified phone numbers, encrypted ballot storage, immutable audit storage, and cryptographic tally verification are not implemented in the current prototype. Tests and reports must describe those limits until the group agrees on and implements any scope change. Administrative logs are not a substitute for recounting the anonymous ballot records.
