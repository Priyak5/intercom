import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="MailboxCursor",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("account", models.CharField(max_length=255, unique=True)),
                ("uidvalidity", models.PositiveBigIntegerField(default=0)),
                ("last_uid", models.PositiveBigIntegerField(default=0)),
            ],
            options={"abstract": False},
        ),
    ]
