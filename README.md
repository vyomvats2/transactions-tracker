# Unified Transactions Tracker

This application is a self-hosted data ingestion and enrichment engine designed to create a "golden source" of personal financial data. It parses transaction statements from various sources (PDFs, CSVs) and stores them in a unified, queryable PostgreSQL database.

The core philosophy is to prioritize data integrity and richness over a polished UI, enabling detailed analysis and tracking through other tools or future application features.

## Core Architecture

*   **Parser-based System:** The application uses a flexible, parser-based architecture. Instead of a single parsing function, it features a registry of specialized parser classes, each designed to handle the unique format of a specific financial statement.
*   **Rich Data Model:** The database schema is designed to capture deep details, including distinctions between transaction and settlement currencies for foreign purchases.
*   **Local-First & Dockerized:** The entire application is containerized using Docker, ensuring a consistent and isolated environment that is easy to set up and run locally.

## Technology Stack

*   **Backend Framework:** [Django](https://www.djangoproject.com/) provides the application structure, ORM (Object-Relational Mapper), and a powerful admin interface for data management.
*   **Database:** [PostgreSQL](https://www.postgresql.org/) is used as the robust and reliable database for storing all transaction data.
*   **PDF Parsing:** [pdfplumber](https://github.com/jsvine/pdfplumber) is the core library used to extract text and table data from PDF documents.
*   **Containerization:** [Docker](https://www.docker.com/) and Docker Compose are used to define and run the application services in an isolated and reproducible environment.

## Quickstart

### Prerequisites

*   Docker must be installed and running on your system.

### 1. Initial Setup

First, clone the repository and set up your local environment configuration.

```bash
# Clone the repository
git clone <your-repo-url>
cd unified_txns_repo

# Create your local environment file from the example
cp .env.example .env
```

### 2. Build and Run Services

Use Docker Compose to build the images and start the `web` and `db` services. The `-d` flag runs them in the background.

```bash
docker compose -f compose/docker-compose.yml up --build -d
```

### 3. Prepare the Database

With the containers running, execute the database migrations to create the necessary tables and create a superuser to access the admin panel.

```bash
# Apply database migrations
docker compose -f compose/docker-compose.yml exec web python manage.py migrate

# Create an admin user (you will be prompted for a username and password)
docker compose -f compose/docker-compose.yml exec web python manage.py createsuperuser
```

### 4. Access the Application

The application is now running! You can access the Django admin interface to view the database tables.

*   **URL:** `http://localhost:8000/admin`
*   **Login:** Use the superuser credentials you created in the previous step.
