# 🗳️ GP11 Election Management and Voting System (`GP11_EMV_System`)

A secure, privacy-preserving web-based Election Management and Voting System prototype built with Python, Flask, and SQLite. Developed by **Group 11 (GP11)** to demonstrate requirement engineering, role-based access control, atomic ballot processing, and automated validation.

---

## ✨ Key Features & Requirements Coverage

### 1. Functional Capabilities
* **User Registration & Authentication:** Voter self-registration and secure credential validation for both Administrators and Voters.
* **Role-Based Access Control (RBAC):** Strict separation between Administrator management functions and Voter participation interfaces.
* **Election Lifecycle Management:** Administrators can create elections with configured start and end timestamps. The system automatically transitions elections between `UPCOMING`, `ACTIVE`, and `CLOSED` states.
* **Early Election Closure:** Administrators can end an active election early, publish results immediately, and record the action in the audit log.
* **Candidate Management Constraints:** Administrators can add or remove candidates **only** while an election is in the `UPCOMING` state. Candidate lists lock automatically once voting begins.
* **Voter Eligibility Workflow:** Voters can request participation in specific elections (`PENDING`), and Administrators can approve (`APPROVED`) or reject (`REJECTED`) eligibility requests.
* **Electronic Voting & Duplicate Prevention:** Approved voters can cast exactly one ballot during an `ACTIVE` election and receive an immediate confirmation receipt ID.
* **Automated Vote Counting & Publication:** Tallies and percentages remain sealed during active voting and are automatically calculated and published once the election reaches the `CLOSED` state.
* **Administrative Audit Trail:** Records key administrative actions (election creation, candidate updates, and voter eligibility approvals) with timestamps.

### 2. Non-Functional & Privacy Architecture
* **Ballot Secrecy (Privacy by Design):** Voter participation status (`voter_participation` table) and actual candidate choices (`ballots` table) are stored in separate tables. The `ballots` table uses random UUIDs (`UUID v4`) and omits voter foreign keys and timestamps to prevent identity correlation.
* **ACID Data Integrity:** Ballot insertion and the voter's `has_voted` flag update are executed inside a single atomic SQLite transaction (`with db:`). Any unexpected failure triggers a full rollback.
* **Password Security:** All user passwords are salted and hashed using `werkzeug.security`.

---

## 🏗️ System Architecture

The application follows a modular **three-layer architecture**:
1. **Presentation Layer (Frontend):** Responsive Bootstrap 5 interface rendering dashboards, candidate lists, ballot forms, and live audit logs.
2. **Application Layer (Backend):** Python Flask controllers handling authentication, RBAC decorators (`@login_required`, `@admin_required`), eligibility verification, and atomic transaction control.
3. **Data Layer (Database):** Relational SQLite database (`election_system.db`) enforcing foreign key constraints, composite primary keys, and domain checks.

```mermaid
flowchart TD
    A([Voter Logs In]) --> B{Is Election ACTIVE?}
    B -- No --> X[Reject & Display Status Error]
    B -- Yes --> C{Is Voter APPROVED?}
    C -- No --> X
    C -- Yes --> D{Has Voter Already Voted?}
    D -- Yes --> X
    D -- No --> E[Select Candidate & Submit]
    E --> F[Begin Atomic DB Transaction]
    F --> G[Set has_voted = 1 & Insert UUID Ballot]
    G --> H{Commit Successful?}
    H -- No --> I[Rollback Transaction & Show Error]
    H -- Yes --> J[Display Confirmation Receipt]
```

---

## 📂 Repository Structure

```text
GP11_EMV_System/
├── app.py                        # Development entry point
├── election_system/
│   ├── __init__.py               # Application factory and configuration
│   ├── auth.py                   # Authentication, registration and access checks
│   ├── elections.py              # Dashboard and election details
│   ├── voting.py                 # Participation and ballot submission
│   ├── admin.py                  # Election, candidate and eligibility management
│   ├── services.py               # Election status and atomic voting rules
│   ├── db.py                     # SQLite schema, connections, demo data and queries
│   └── templates/                # Shared layout and page templates
├── test_app.py                   # Automated Pytest suite
├── project_dependencies.txt      # Python package dependencies
└── README.md                     # Project documentation
```

The database defaults to `election_system.db` in the current working directory. Running `python app.py` initializes missing tables and seeds demo data only when no users exist. Existing databases remain compatible without migration. Tests use `create_app({"TESTING": True, "DATABASE": ...})` to create isolated application instances.

### Naming and organization conventions

- Use `snake_case` for Python modules, functions, variables and template filenames, `PascalCase` for classes, and `UPPER_SNAKE_CASE` for constants.
- Keep related database operations together in `db.py`; route modules handle HTTP requests and services handle election and voting rules. Data helpers leave commits and rollbacks to their callers.
- Reuse the original section comments and add matching headings for new functional sections.
- Keep public URLs and database identifiers stable unless a change and migration are explicitly planned.

---

## 🚀 Installation & Setup Guide

### Prerequisites
* **Python 3.9+** installed on Windows, macOS, or Linux.

### Step 1: Clone the Repository
```powershell
git clone https://github.com/<your-username>/GP11_EMV_System.git
cd GP11_EMV_System
```

### Step 2: Create & Activate Virtual Environment (Windows PowerShell / PyCharm)
```powershell
py -m venv .venv
.\.venv\Scripts\activate
```
*(If `pip` is missing in your virtual environment, run `python -m ensurepip --upgrade` first).*

### Step 3: Install Dependencies
```powershell
python -m pip install -r project_dependencies.txt
```

### Step 4: Run the Application
```powershell
python app.py
```
Open your browser and navigate to: **`http://127.0.0.1:5000`**

---

## 🔑 Default Demo Accounts

When started for the first time, the system automatically seeds a sample active election (**Student Union Presidential Election**) and two test accounts:

| Role | Username | Password | Capabilities |
| :--- | :--- | :--- | :--- |
| **Administrator** | `admin` | `admin123` | Create elections, manage candidates, approve/reject voters, view audit logs |
| **Voter** | `voter1` | `voter123` | Request election participation, view candidates, cast anonymous ballot |

### Quick Demo Walkthrough
1. Sign in as **`voter1`** (`voter123`), open **Student Union Presidential Election**, and click **Request Participation**.
2. Log out and sign in as **`admin`** (`admin123`). Open the election and click **Approve** next to `voter1` under *Voter Eligibility Management*.
3. Log out and sign back in as **`voter1`**. Select a candidate and click **Confirm & Submit Anonymous Ballot**.
4. Try submitting another vote or re-visiting the page to see duplicate-voting prevention in action.

---

## 🧪 Running Automated Tests

The automated test suite (`test_app.py`) uses temporary SQLite databases to verify authentication, role-based access, election and candidate management, template escaping, result visibility, atomic voting, duplicate prevention, database reinitialization, and early election closure.

Run the test suite from your terminal:
```powershell
python -m pytest test_app.py -v
```

---

## 🔮 Future Evolution Roadmap

This prototype is designed for internal organisational and student society elections. Planned future enhancements include:
* **Multiple Voting Methods:** Ranked-Choice (Instant-Runoff) and Approval voting modules.
* **Cryptographic E2E Verifiability:** Zero-knowledge ballot receipts and public bulletin board verification.
* **Configurable Result Visibility:** Optional live tallying and minimum quorum enforcement.
* **Accessibility & SSO:** WCAG 2.1 AA compliance and university/enterprise OAuth 2.0 integration.
