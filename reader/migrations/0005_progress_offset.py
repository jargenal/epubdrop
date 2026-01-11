from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("reader", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="readingprogress",
            name="block_offset_percent",
            field=models.FloatField(default=0.0),
        ),
    ]
