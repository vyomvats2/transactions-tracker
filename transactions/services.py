import pdfplumber, hashlib
from datetime import datetime
from .models import Transaction

def parse_pdf_statement(pdf_path: str, source_file_path: str = ""):
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            table = page.extract_table()
            # If no table is found on the page, skip to the next page
            if table is None:
                continue

            # Skip header row by starting from the second row (index 1)
            for row in table[1:]:
                try:
                    txn_date = datetime.strptime(row[0], "%d/%m/%Y").date()
                    description = row[1].strip()
                    amount_minor = int(float(row[2]) * 100)
                    currency = "GBP"
                    hash_dedupe = hashlib.sha256(f"{txn_date}{amount_minor}{description}".encode()).hexdigest()
                    Transaction.objects.update_or_create(
                        hash_dedupe=hash_dedupe,
                        defaults=dict(
                            posted_date=txn_date,
                            description_raw=description,
                            amount_minor=amount_minor,
                            currency=currency,
                            source_file=source_file_path
                        )
                    )
                except (ValueError, TypeError, IndexError):
                    # This will catch errors from bad date formats, non-numeric amounts, or incomplete rows.
                    print(f"Skipping malformed row: {row}")
                    continue
