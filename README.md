# Transactions Tracker

This application parses financial transaction statements from PDF/CSV files and stores them in a unified PostgreSQL database. It is designed to be run locally using Docker.

## Stack
- **Backend:** Django + Django REST Framework
- **Database:** PostgreSQL
- **PDF Parsing:** pdfplumber
- **Containerization:** Docker Compose

## Quickstart

1. `cp .env.example .env`
2. `docker compose -f compose/docker-compose.yml up --build`
3. `docker compose -f compose/docker-compose.yml exec web python manage.py migrate`
4. `docker compose -f compose/docker-compose.yml exec web python manage.py createsuperuser`
5. Access admin at `http://localhost:8000/admin`
