from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0002_workspace_membership_invite"),
    ]

    operations = [
        migrations.AddField(
            model_name="workspace",
            name="brand_color",
            field=models.CharField(default="#2563eb", max_length=9),
        ),
        migrations.AddField(
            model_name="workspace",
            name="welcome_message",
            field=models.CharField(default="Hi! How can we help?", max_length=200),
        ),
        migrations.AddField(
            model_name="workspace",
            name="allowed_origins",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
