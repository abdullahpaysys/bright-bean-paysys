from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("workspaces", "0003_alter_workspace_primary_color_and_more"),
        ("slack_bot", "0003_bot_whitelisting"),
    ]

    operations = [
        migrations.AddField(
            model_name="botuseraccess",
            name="bot_created_org_membership",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="botuseraccess",
            name="bot_created_workspace_membership",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="botuseraccess",
            name="brightbean_user",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="slack_bot_access_records",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="botuseraccess",
            name="brightbean_workspace",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="slack_bot_access_records",
                to="workspaces.workspace",
            ),
        ),
    ]
