from django.db import migrations, models


def combine_names(apps, schema_editor):
    ClubMember = apps.get_model("auctions", "ClubMember")
    for member in ClubMember.objects.all().iterator():
        combined = f"{member.first_name} {member.last_name}".strip()
        if combined != (member.first_name or ""):
            ClubMember.objects.filter(pk=member.pk).update(first_name=combined[:200])


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0263_clubdiscordrole_bot_can_manage"),
    ]

    operations = [
        migrations.AlterField(
            model_name="clubmember",
            name="first_name",
            field=models.CharField(max_length=200, blank=True),
        ),
        migrations.RunPython(combine_names, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="clubmember",
            name="last_name",
        ),
        migrations.RenameField(
            model_name="clubmember",
            old_name="first_name",
            new_name="name",
        ),
        migrations.AlterModelOptions(
            name="clubmember",
            options={"ordering": ["name"]},
        ),
        migrations.AlterField(
            model_name="clubmember",
            name="possible_duplicate",
            field=models.ForeignKey(
                blank=True,
                help_text="Another club member with the same name; may be a duplicate",
                null=True,
                on_delete=models.SET_NULL,
                related_name="duplicate_of",
                to="auctions.clubmember",
            ),
        ),
    ]
