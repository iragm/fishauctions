# Celery Implementation for Fish Auctions

This document describes the Celery implementation that replaces cron jobs for scheduled tasks.

## Overview

Celery has been implemented to handle:
1. **Periodic tasks** - Previously handled by cron jobs (via Celery Beat)
2. **Immediate email delivery** - Emails are now sent immediately via django-post-office's Celery integration

## Benefits

- **Immediate email delivery**: Emails are sent as soon as they're queued, not on a fixed schedule
- **Better task management**: Tasks can be monitored, retried, and managed through Django admin
- **Scalability**: Celery workers can be scaled independently
- **Reliability**: Built-in retry mechanisms and error handling
- **Visibility**: Task status and history available through django-celery-beat admin interface

## Architecture

### Components

1. **Celery Worker** (`celery_worker` container)
   - Executes tasks asynchronously
   - Handles email sending via post_office
   - Runs management commands as Celery tasks

2. **Celery Beat** (`celery_beat` container)
   - Scheduler for periodic tasks
   - Uses Django database as backend (django-celery-beat)
   - Configuration in `fishauctions/celery.py`

3. **Redis** (Broker and Result Backend)
   - Task queue (broker): Redis DB 1
   - Results storage: Redis DB 2

### Files

- `fishauctions/celery.py` - Main Celery configuration and beat schedule
- `fishauctions/__init__.py` - Loads Celery app on Django startup
- `auctions/tasks.py` - Task implementations (wraps management commands)
- `fishauctions/settings.py` - Celery and POST_OFFICE configuration
- `docker-compose.yaml` - Celery worker and beat service definitions

## Task Schedule

All tasks from the old crontab have been converted:

| Task | Schedule | Description |
|------|----------|-------------|
| `endauctions` | Every minute | End lots and declare winners |
| `sendnotifications` | Every 15 minutes | Send watched item notifications |
| `auctiontos_notifications` | Every 15 minutes | Welcome and reminder emails |
| `email_invoice` | Every 15 minutes | Invoice notification emails |
| `send_queued_mail` | Every 10 minutes | Retry failed emails (post_office) |
| `auction_emails` | Every 4 minutes | Drip marketing emails |
| `email_unseen_chats` | Daily at 10:00 | Unread chat notifications |
| `weekly_promo` | Wednesday at 9:30 | Weekly promotional email |
| `set_user_location` | Every 2 hours | Update user locations from IP |
| `remove_duplicate_views` | Every 15 minutes | Clean duplicate page views |
| `webpush_notifications_deduplicate` | Daily at 10:00 | Remove duplicate push subscriptions |
| `update_auction_stats` | Every minute | Update cached auction statistics |

## Configuration

### Environment Variables

No new environment variables required. Celery uses existing:
- `REDIS_PASSWORD` - Redis authentication
- `REDIS_HOST` - Redis hostname (default: "redis")

### Settings

In `fishauctions/settings.py`:

```python
# Celery Configuration
CELERY_BROKER_URL = "redis://:password@redis:6379/1"
CELERY_RESULT_BACKEND = "redis://:password@redis:6379/2"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE
CELERY_ENABLE_UTC = True
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"

# POST_OFFICE Configuration
POST_OFFICE = {
    ...
    "CELERY_ENABLED": True,  # Enable immediate email delivery
}
```

## Docker Services

### Starting Services

```bash
# Start all services including Celery
docker compose up -d

# Initial setup: Populate periodic tasks in the database
docker exec -it django python manage.py setup_celery_beat

# Check Celery worker logs
docker logs celery_worker -f

# Check Celery beat logs
docker logs celery_beat -f
```

**Important**: After the first deployment or when adding new tasks, you must run `docker exec -it django python manage.py setup_celery_beat` to populate the periodic tasks in the database. This creates the task entries that appear in Django admin.

### Service Configuration

**celery_worker**:
- Command: `celery -A fishauctions worker --loglevel=info --concurrency=2`
- Depends on: db, redis
- Auto-restart: always

**celery_beat**:
- Command: `celery -A fishauctions beat --loglevel=info --scheduler django_celery_beat.schedulers:DatabaseScheduler`
- Depends on: db, redis
- Auto-restart: always

## Managing Periodic Tasks

### Initial Setup

After starting the services for the first time, you need to populate the periodic tasks in the database:

```bash
docker exec -it django python manage.py setup_celery_beat
```

This management command reads the `beat_schedule` from `fishauctions/celery.py` and creates corresponding database entries for django-celery-beat. You need to run this:
- After first deployment
- When adding new tasks to `fishauctions/celery.py`
- When modifying task schedules in code (optional - you can also modify via admin)

### Via Django Admin

1. Navigate to Django Admin → Periodic Tasks → Periodic tasks
2. You'll see all tasks created by the setup command
3. You can:
   - Enable/disable tasks
   - Modify schedules
   - View task execution history
   - Manually trigger tasks
   - Add new tasks

### Via Code

Tasks are defined in `fishauctions/celery.py`:

```python
app.conf.beat_schedule = {
    'task-name': {
        'task': 'auctions.tasks.task_function',
        'schedule': 60.0,  # seconds, or use crontab()
    },
}
```

After modifying the beat schedule in code, run `python manage.py setup_celery_beat` to sync changes to the database.

## Manual Task Execution

### Run a task immediately (for testing):

```bash
# Enter Django container
docker exec -it django python manage.py shell

# Import and run a task
from auctions.tasks import endauctions
endauctions.delay()  # Run asynchronously
# or
endauctions()  # Run synchronously (for testing)
```

### Run management commands (old way still works):

```bash
docker exec -it django python manage.py endauctions
```

## Monitoring

### Check Celery Status

```bash
# Worker status
docker exec -it celery_worker celery -A fishauctions inspect active

# Scheduled tasks
docker exec -it celery_beat celery -A fishauctions inspect scheduled

# Worker stats
docker exec -it celery_worker celery -A fishauctions inspect stats
```

### View Task Results

Through Django admin:
1. Navigate to → Periodic Tasks → Crontabs/Intervals
2. View execution logs and results

## Troubleshooting

### Tasks Not Running

1. Check worker is running: `docker ps | grep celery`
2. Check worker logs: `docker logs celery_worker`
3. Verify Redis connection: `docker logs redis`
4. Check beat scheduler: `docker logs celery_beat`

### Emails Not Being Sent Immediately

1. Verify `POST_OFFICE['CELERY_ENABLED'] = True` in settings
2. Check worker is processing tasks: `docker logs celery_worker`
3. Verify post_office tasks are discovered:
   ```bash
   docker exec -it celery_worker celery -A fishauctions inspect registered
   ```

### Task Failures

1. Check worker logs: `docker logs celery_worker -f`
2. View in Django admin: Periodic Tasks → Task results
3. Check task implementation in `auctions/tasks.py`

## Development vs Production

### Development (DEBUG=True)
- Email backend: Console (prints to stdout)
- Tasks run normally via Celery
- Full task visibility in logs

### Production (DEBUG=False)
- Email backend: post_office → SES/SMTP
- Tasks run via Celery
- Emails sent immediately when queued
- Failed emails retried every 10 minutes

## Migration from Cron

The old crontab file has been preserved with documentation showing the mapping to Celery tasks. The cron jobs are **no longer active** - all scheduling is now handled by Celery Beat.

### Old Way (Deprecated)
```bash
# Via cron
* * * * * /home/app/web/task.sh endauctions
```

### New Way
```python
# Via Celery (automatic, configured in fishauctions/celery.py)
'endauctions': {
    'task': 'auctions.tasks.endauctions',
    'schedule': 60.0,
}
```

## Testing

Run Celery task tests:

```bash
docker exec -it django python manage.py test auctions.test_celery_tasks
```

## Additional Resources

- [Celery Documentation](https://docs.celeryproject.org/)
- [Django-Celery-Beat Documentation](https://django-celery-beat.readthedocs.io/)
- [Django-Post-Office Celery Integration](https://github.com/ui/django-post-office#celery)
