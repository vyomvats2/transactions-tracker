# Unified Transactions Tracker

This application is a self-hosted data ingestion and enrichment engine designed to create a "golden source" of personal financial data. It parses transaction statements from various sources (PDFs, CSVs) and stores them in a unified, queryable PostgreSQL database.

The core philosophy is to prioritize data integrity and richness over a polished UI, enabling detailed analysis and tracking through other tools or future application features.

## Core Architecture

*   **Parser-based System:** The application uses a flexible, parser-based architecture. Instead of a single parsing function, it features a registry of specialized parser classes, each designed to handle the unique format of a specific financial statement.
*   **Rich Data Model:** The database schema is designed to capture deep details, including distinctions between transaction and settlement currencies for foreign purchases, exchange rates, and transaction vs. posted dates.
*   **Local-First & Containerized:** The entire application is containerized, ensuring a consistent and isolated environment that is easy to set up and run locally.

## Technology Stack

*   **Backend Framework:** [Django](https://www.djangoproject.com/) provides the application structure, ORM (Object-Relational Mapper), and a powerful admin interface for data management.
*   **Database:** [PostgreSQL](https://www.postgresql.org/) is used as the robust and reliable database for storing all transaction data.
*   **PDF Parsing:** [pdfplumber](https://github.com/jsvine/pdfplumber) is the core library used to extract text and table data from PDF documents.
*   **Containerization:** [finch](https://runfinch.com/) is used to build images and run the application services. The Compose file follows the standard format finch reads natively.

## Quickstart

### Prerequisites

*   [finch](https://runfinch.com/) must be installed on your system.

### 1. Initial Setup

First, clone the repository and set up your local environment configuration.

```bash
# Clone the repository
git clone <your-repo-url>
cd unified_txns_repo

# Create your local environment file from the example
cp .env.example .env
```

If this is a fresh machine, initialize and start the finch VM once:

```bash
finch vm init
```

On subsequent uses, make sure the VM is running:

```bash
finch vm status       # check
finch vm start        # if stopped
```

### 2. Build and Run Services

Build the images and start the `web` and `db` services. The `-d` flag runs them in the background. The `./scripts/fc` wrapper invokes `finch compose` with the right compose file and env file pre-wired.

```bash
./scripts/fc up --build -d
```

### 3. Prepare the Database

With the containers running, execute the database migrations to create the necessary tables and create a superuser to access the admin panel.

```bash
# Apply database migrations
./scripts/fc exec web python manage.py migrate

# Create an admin user (you will be prompted for a username and password)
./scripts/fc exec web python manage.py createsuperuser
```

### 4. Access the Application

The application is now running! You can access the Django admin interface to view the database tables.

*   **URL:** `http://localhost:8000/admin`
*   **Login:** Use the superuser credentials you created in the previous step.

### 5. Parsing a Statement

To parse a statement, you must place the file in a location accessible to the container. The project root is mounted as `/code/` inside the container. A good practice is to create a `statements/` directory in the project root to hold your files.

Then, run the `parse_statement` management command, specifying which parser to use and the path to the file. Current parsers:

*   `simple_pdf` — generic single-table PDF format.
*   `chase_credit_pdf` — Chase credit card PDF statements. Used for FX enrichment of existing QFX-sourced transactions, not primary ingestion.
*   `chase_credit_qfx` — Chase credit card QFX statements. The primary source for Chase transactions; uses FITID-based deduplication.

For Chase credit card data, the workflow is two-pass: ingest the QFX file first (creates transactions with settlement amounts and stable bank-assigned IDs), then ingest the matching PDF to enrich each transaction with its original GBP amount and exchange rate.

```bash
# Example: Create a directory for your statements
mkdir statements

# Copy your PDF into the new directory
cp /path/to/your/bank-statement.pdf statements/

# Run the parser from your host machine
./scripts/fc exec web python manage.py parse_statement --parser simple_pdf /code/statements/bank-statement.pdf
```

The command will use the `simple_pdf` parser to process the file and save the transactions to the database. You can then view the imported data in the Django admin.

### 6. Running Tests

The test suite uses `pytest` + `pytest-django` and lives under `transactions/tests/`.

Run the full suite:

```bash
./scripts/fc exec web pytest
```

Verbose output, one test per line:

```bash
./scripts/fc exec web pytest -v
```

Run a single module, class, or test:

```bash
./scripts/fc exec web pytest transactions/tests/test_qfx_parser.py
./scripts/fc exec web pytest transactions/tests/test_qfx_parser.py::TestHappyPath
./scripts/fc exec web pytest transactions/tests/test_qfx_parser.py::TestHappyPath::test_auto_creates_account
```

The suite reuses the same test database across runs for speed (`--reuse-db` is set in `pytest.ini`). If you change a migration, force a fresh DB:

```bash
./scripts/fc exec web pytest --create-db
```

Test fixtures live at `transactions/tests/fixtures/`:

*   `chase_credit_sample.qfx` — anonymized Chase QFX file, used by the happy-path tests.
*   `non_chase.qfx`, `bank_only.qfx` — hand-written rejection fixtures.
*   `anonymize.py` — standalone script for generating a fresh anonymized fixture from a real Chase QFX. Run as `python transactions/tests/fixtures/anonymize.py <input> <output>`.
