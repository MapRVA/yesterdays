from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("subjects", "0009_person_suffix"),
    ]

    operations = [
        migrations.AddField(
            model_name="wikidataitem",
            name="sparql_last_loaded_at",
            field=models.DateTimeField(
                blank=True,
                help_text="When this entity's RDF was last loaded into Oxigraph",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="wikidataitem",
            name="sparql_fetch_failures",
            field=models.PositiveIntegerField(
                default=0,
                help_text="Consecutive SPARQL mirror load failures (resets on success)",
            ),
        ),
        migrations.AddField(
            model_name="wikidataitem",
            name="discovered_via",
            field=models.ForeignKey(
                blank=True,
                help_text=(
                    "For ancestor entities pulled in by the closure walk, the "
                    "Subject whose graph first surfaced this entity. Null for "
                    "entities that are themselves Subjects."
                ),
                null=True,
                on_delete=models.deletion.SET_NULL,
                related_name="+",
                to="subjects.subject",
            ),
        ),
        migrations.AddIndex(
            model_name="wikidataitem",
            index=models.Index(
                fields=["sparql_last_loaded_at"],
                name="subjects_wi_sparql__7c93ab_idx",
            ),
        ),
    ]
