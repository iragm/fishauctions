from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Move application data from one user account to another without deleting the source account"

    def add_arguments(self, parser):
        parser.add_argument("user_to_empty", help="Username or numeric id of the account to empty")
        parser.add_argument("user_where_it_should_go", help="Username or numeric id of the account to keep")

    def get_user(self, identifier):
        user_model = get_user_model()
        if str(identifier).isdigit():
            user = user_model.objects.filter(pk=int(identifier)).first()
            if user:
                return user
        user = user_model.objects.filter(username=identifier).first()
        if user:
            return user
        msg = f"User '{identifier}' was not found."
        raise CommandError(msg)

    def handle(self, *args, **options):
        user_to_empty = self.get_user(options["user_to_empty"])
        user_where_it_should_go = self.get_user(options["user_where_it_should_go"])
        if user_to_empty == user_where_it_should_go:
            msg = "Source and target users must be different."
            raise CommandError(msg)

        user_to_empty.userdata.merge_into(user_where_it_should_go)
        self.stdout.write(
            self.style.SUCCESS(f"Moved data from {user_to_empty.username} to {user_where_it_should_go.username}.")
        )
