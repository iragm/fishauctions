# Celery

`fishauctions/celery.py` holds the beat schedule (read it directly for the current task list — this
doc doesn't duplicate it, it drifts); `auctions/tasks.py` implements them; `fishauctions/settings.py`
has the Celery/POST_OFFICE config. Design rules, time limits, retry/locking gotchas and why nothing
periodic runs over `PageView`: `.claude/skills/celery-tasks/SKILL.md`.

Broker: Redis DB 1. Results: Redis DB 2. Beat schedule lives in the database
(`django-celery-beat`) and is reconciled against `beat_schedule` on worker startup.

## Docker

```bash
docker compose up -d
docker logs celery_worker -f
docker logs celery_beat -f
```

## Managing tasks

Django Admin → Periodic Tasks → Periodic tasks: enable/disable, change schedules, view history.

## Dev vs prod

| | Dev (`DEBUG=True`) | Prod (`DEBUG=False`) |
|---|---|---|
| Email backend | Console | post_office → SES/SMTP |
| Failed email retry | — | every 10 minutes (`send_queued_mail`) |

## Troubleshooting

| Symptom | Check |
|---|---|
| Tasks not running | `docker ps \| grep celery`, `docker logs celery_worker`, `docker logs redis`, `docker logs celery_beat` |
| Emails not sent immediately | `POST_OFFICE['CELERY_ENABLED'] = True` in settings; `docker logs celery_worker` |

### weekly_promo

```bash
docker logs --timestamps celery_worker 2>&1 | egrep 'auctions.tasks.weekly_promo|Weekly promo'
docker logs --timestamps --tail 100 celery_worker 2>&1 | egrep 'auctions.tasks.weekly_promo|Weekly promo'
```
