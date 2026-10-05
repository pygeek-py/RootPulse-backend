from django.apps import AppConfig
from django.db.models.signals import post_migrate


class ProvidersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "providers"

    def ready(self):
        from .catalog import sync_catalog

        # A deployment always has the curated list after `migrate` (idempotent; never touches
        # status or polling state).
        post_migrate.connect(sync_catalog, sender=self, dispatch_uid="providers.sync_catalog")
