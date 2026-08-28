"""Give the AI command palette to everyone who already has an account.

``UserData.use_llm_search`` defaults from ``ASSISTANT_ENABLED_FOR_USERS``, which is now on, but a
default only reaches rows created after it -- every existing user was created while the flag was
off and would keep it.  This is the same write ``manage.py change_assistant on`` does.

It stays a per-user column so a single abuser can be switched off in the Django admin, so the
reverse is a no-op: unchecking everybody again is ``manage.py change_assistant off``, not a
rollback.
"""

from django.db import migrations, models

import auctions.models


def turn_the_assistant_on(apps, schema_editor):
    UserData = apps.get_model("auctions", "UserData")
    UserData.objects.update(use_llm_search=True)


class Migration(migrations.Migration):
    dependencies = [
        ("auctions", "0414_remove_club_enable_club_page"),
    ]

    operations = [
        migrations.AlterField(
            model_name="userdata",
            name="use_llm_search",
            field=models.BooleanField(
                blank=True,
                default=auctions.models.get_default_use_llm_search,
                help_text="Let this user talk to the site in plain English by typing or speaking into the command palette.  On for everyone by default; uncheck it to take the palette away from this one user (they abused it, say), or turn it off site-wide with `manage.py change_assistant off`.  Also needs an LLM configured site-wide (auctions.llm.assist_enabled), because every command spends this site's own budget.  This does NOT gate connecting Claude or another assistant over MCP (/ai/), which is open to everyone: an agent brings its own model and costs this site nothing.",
                verbose_name="AI command palette",
            ),
        ),
        migrations.RunPython(turn_the_assistant_on, migrations.RunPython.noop),
    ]
