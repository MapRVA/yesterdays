from django.db import migrations, models


class Migration(migrations.Migration):
    """Indexes backing in-view search (see ``images.in_view``).

    The GiST index is on the ``geography`` *expression*, not on the geometry
    column, because that is what makes the KNN operator ``<->`` return true
    metres. Ordering by degrees on a 4326 geometry is latitude-distorted, so a
    point 1 km east would sort ahead of one 1 km north. ``geometry::geography``
    is ``IMMUTABLE``, so the expression index is legal.

    It has no model counterpart — Django cannot express a cast in
    ``Meta.indexes`` — so it is created with ``RunSQL``, the same way the
    ``public_georeferences_mvt`` materialized view is.
    """

    dependencies = [
        ("images", "0065_unique_anonymous_georeference_per_image"),
    ]

    operations = [
        migrations.RunSQL(
            sql="CREATE INDEX images_georeference_point_geog_gist "
            "ON images_georeference USING GIST ((point::geography));",
            reverse_sql="DROP INDEX IF EXISTS images_georeference_point_geog_gist;",
        ),
        migrations.AddIndex(
            model_name="georeference",
            index=models.Index(
                fields=["image", "-georeferenced_at"], name="images_geor_img_recent_idx"
            ),
        ),
    ]
