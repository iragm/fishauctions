# Celery Implementation for Fish Auctions

This document describes the Celery implementation for scheduled tasks and email delivery.

## Overview

Celery handles:
1. **Periodic tasks** - Scheduled tasks like ending auctions, sending notifications, etc.
2. **Immediate email delivery** - Emails are sent immediately via django-post-office's Celery integration

## Architecture

### Components

1. **Celery Worker** (`celery_worker` container)
   - Executes tasks asynchronously
   - Handles email sending via post_office
   - Runs management commands as Celery tasks

2. **Celery Beat** (`celery_beat` container)
   - Scheduler for periodic tasks
   - Uses Django database as backend (django-celery-beat)

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

## Docker Services

### Starting Services

```bash
# Start all services including Celery
docker compose up -d

# Check Celery worker logs
docker logs celery_worker -f

# Check Celery beat logs
docker logs celery_beat -f
```

Periodic tasks are automatically configured in the database on container startup.

## Managing Periodic Tasks

Tasks are managed via Django Admin:
1. Navigate to Django Admin → Periodic Tasks → Periodic tasks
2. You can enable/disable tasks, modify schedules, and view execution history

Tasks are defined in `fishauctions/celery.py` and automatically synced to the database on startup.

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

## Troubleshooting

### Tasks Not Running

1. Check worker is running: `docker ps | grep celery`
2. Check worker logs: `docker logs celery_worker`
3. Verify Redis connection: `docker logs redis`
4. Check beat scheduler: `docker logs celery_beat`

### Emails Not Being Sent Immediately

1. Verify `POST_OFFICE['CELERY_ENABLED'] = True` in settings
2. Check worker is processing tasks: `docker logs celery_worker`

## Additional Resources

- [Celery Documentation](https://docs.celeryproject.org/)
- [Django-Celery-Beat Documentation](https://django-celery-beat.readthedocs.io/)
- [Django-Post-Office Celery Integration](https://github.com/ui/django-post-office#celery)
