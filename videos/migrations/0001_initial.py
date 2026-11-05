"""Create the initial video catalog model."""

from django.db import migrations, models


class Migration(migrations.Migration):
    """Create the Video table for catalog and processing metadata."""

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="Video",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("title", models.CharField(max_length=255)),
                ("description", models.TextField(blank=True)),
                ("category", models.CharField(max_length=100)),
                (
                    "video_file",
                    models.FileField(upload_to="videos/source/"),
                ),
                (
                    "thumbnail",
                    models.FileField(
                        blank=True, upload_to="videos/thumbnails/"
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True, db_index=True),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("processing", "Processing"),
                            ("ready", "Ready"),
                            ("failed", "Failed"),
                        ],
                        db_index=True,
                        default="pending",
                        max_length=16,
                    ),
                ),
            ],
            options={"ordering": ("-created_at", "id")},
        ),
    ]
