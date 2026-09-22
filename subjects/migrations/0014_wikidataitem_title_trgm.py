"""Trigram GIN index on ``WikidataItem.title``.

Backs the autocomplete categories query (``ancestor__title__icontains``),
which previously scanned every label in scope. With ``gin_trgm_ops`` the
``ILIKE`` lookup that Django generates from ``__icontains`` becomes an
indexed search.

The ``pg_trgm`` extension itself is already created by
``images.migrations.0035_enhance_search_vector``; no need to enable it
again here.
"""

from django.contrib.postgres.indexes import GinIndex
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("subjects", "0013_subjectancestor"),
        ("images", "0035_enhance_search_vector"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="wikidataitem",
            index=GinIndex(
                fields=["title"],
                name="wikidataitem_title_trgm",
                opclasses=["gin_trgm_ops"],
            ),
        ),
    ]
