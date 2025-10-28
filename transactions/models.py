from django.db import models

class Transaction(models.Model):
    posted_date = models.DateField()
    description_raw = models.TextField()
    amount_minor = models.BigIntegerField()
    currency = models.CharField(max_length=3)
    hash_dedupe = models.CharField(max_length=64, unique=True)
    source_file = models.CharField(max_length=1024, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
