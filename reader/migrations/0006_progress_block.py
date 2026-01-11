from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("reader", "0005_progress_offset"),
    ]

    operations = [
        migrations.AddField(
            model_name="readingprogress",
            name="block",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="progress_entries",
                to="reader.block",
            ),
        ),
    ]
