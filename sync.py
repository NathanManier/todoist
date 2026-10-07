#!/usr/bin/env python3
"""Canvas calendar sync with optional grade context and bounded study sessions."""
import argparse
import hashlib
import json
import logging
import os
import re
import tempfile
import uuid
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests
from icalendar import Calendar
from canvas_data import (CanvasClient, assessment_type, confirmed_submitted,
                         current_grade, grade_impact, match_assignment)

UTC = timezone.utc
logger = logging.getLogger(__name__)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def request_id(key):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, 'canvas-todoist:' + key))


def sanitize_label_name(name):
    return re.sub(r'\s+', '_', re.sub(r'[^\w\s-]', '', name)).strip('_')[:60] or 'General'


def course_key(name):
    return re.sub(r'[^a-z0-9]', '', name.casefold())


def excluded_course(names, exclusions):
    return any(course_key(name) in exclusions for name in names if name)


def study_enabled(event, exclusions):
    return (event['kind_label'] and event['study_allowed']
            and not excluded_course([event['course'], event.get('course_name')], exclusions))


def parse_course_name(summary, description=''):
    bracket = re.search(r'\[([^\]]+)\]\s*$', summary)
    if bracket:
        return bracket.group(1).strip()
    code = re.search(r'\b[A-Z]{2,6}[-\s]?\d{3,4}(?:-[A-Z0-9]+)*\b', summary + ' ' + description)
    return code.group() if code else 'General'


def calculate_priority(due, now):
    days = (due - now).total_seconds() / 86400
    return next((p for threshold, p in [(1, 4), (3, 3), (7, 2)] if days <= threshold), 1)


def parse_ics_events(content, zone, now):
    events, seen = [], set()
    for component in Calendar.from_ical(content).walk('VEVENT'):
        uid = str(component.get('uid', ''))
        if not uid or uid in seen or str(component.get('status', '')).upper() == 'CANCELLED':
            continue
        start, end = component.get('dtstart'), component.get('dtend')
        dt = start or end
        if dt is None:
            continue
        all_day = not isinstance(dt.dt, datetime)
        # All-day DTEND is exclusive, not the assignment's deadline.
        due = datetime.combine(dt.dt, time(23, 59, 59), zone) if all_day else (end or start).dt
        if due.tzinfo is None:
            due = due.replace(tzinfo=zone)
        summary = str(component.get('summary', ''))
        description = str(component.get('description', ''))
        events.append({'uid': uid, 'title': re.sub(r'\s*\[[^\]]+\]\s*$', '', summary).strip(),
                       'course': parse_course_name(summary, description), 'description': description,
                       'url': str(component.get('url', '')), 'due': due,
                       'date_only': all_day, 'priority': calculate_priority(due, now)})
        seen.add(uid)
    return events


class SyncState:
    def __init__(self, path):
        self.path = Path(path)
        self.exists = self.path.exists()
        if self.exists:
            data = json.loads(self.path.read_text())
            if not isinstance(data, dict):
                raise ValueError('Invalid sync state.')
            if 'records' not in data:
                if not isinstance(data.get('synced_events'), dict):
                    raise ValueError('Unrecognized sync state format.')
                data['records'] = {
                    'assignment:' + uid: {'task_id': str(info['todoist_task_id']),
                                         'kind': 'assignment', 'uid': uid, 'status': 'active',
                                         'legacy_reminder': True,
                                         'due': info.get('due_date'), 'payload_hash': None}
                    for uid, info in data['synced_events'].items()
                }
            if not isinstance(data['records'], dict):
                raise ValueError('Invalid sync records.')
            self.records = data['records']
        else:
            self.records = {}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=self.path.parent, prefix='.sync-state-', suffix='.tmp')
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump({'version': 2, 'records': self.records}, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


class Todoist:
    def __init__(self, token, session=None):
        self.session = session or requests.Session()
        self.session.headers.update({'Authorization': f'Bearer {token}'})
        self.labels = None

    def call(self, method, path, payload=None, key=None, params=None, missing_ok=False):
        try:
            response = self.session.request(
                method, 'https://api.todoist.com/api/v1/' + path, json=payload, params=params,
                headers={'X-Request-ID': request_id(key)} if key else {}, timeout=30,
                allow_redirects=False)
            if missing_ok and response.status_code in (404, 410):
                return None
            if not 200 <= response.status_code < 300:
                raise RuntimeError(f'Todoist {method} failed (HTTP {response.status_code}). '
                                   'Saved progress will be retried; check permissions, plan, or rate limits.')
            return response.json() if response.content else None
        except requests.RequestException:
            raise RuntimeError('Todoist could not be reached; saved progress will be retried.') from None

    def list(self, path, params=None):
        result, cursor, seen = [], None, set()
        while True:
            page = self.call('GET', path, params={**(params or {}), 'limit': 200,
                                                **({'cursor': cursor} if cursor else {})})
            result.extend(page['results'])
            cursor = page.get('next_cursor')
            if not cursor:
                return result
            if cursor in seen:
                raise RuntimeError('Todoist returned a repeated pagination cursor.')
            seen.add(cursor)

    def project(self, name):
        for project in self.list('projects'):
            if project['name'] == name:
                return str(project['id'])
        return str(self.call('POST', 'projects', {'name': name}, key=str(uuid.uuid4()))['id'])

    def ensure_labels(self, names):
        if self.labels is None:
            self.labels = {label['name'] for label in self.list('labels')}
        for name in names:
            if name not in self.labels:
                self.call('POST', 'labels', {'name': name}, key=str(uuid.uuid4()))
                self.labels.add(name)


def active(task):
    return task is not None and not any(task.get(field) for field in ('checked', 'is_completed', 'is_deleted'))


def upsert(todoist, state, key, payload, metadata):
    record = state.records.get(key)
    generation = (record or {}).get('generation', 0)
    if record and record.get('status') == 'cancelled' and metadata.get('kind') == 'study':
        generation += 1
        record = None  # The assessment moved back; only our own cancellations can return.
    if record and record.get('status') != 'active':
        return None  # Completed/deleted tasks stay completed/deleted.
    digest = fingerprint(payload)
    if record:
        existing = todoist.call('GET', 'tasks/' + record['task_id'], missing_ok=True)
        if not active(existing):
            record['status'] = 'done'
            state.save()
            return None
        if record.get('payload_hash') != digest:
            manual = set(existing.get('labels', [])) - set(record.get('managed_labels', []))
            updated = {**payload, 'labels': sorted(manual | set(payload['labels']))}
            updated.pop('project_id', None)
            todoist.ensure_labels(payload['labels'])
            todoist.call('POST', 'tasks/' + record['task_id'], updated,
                         key=str(uuid.uuid4()))
    else:
        todoist.ensure_labels(payload['labels'])
        task = todoist.call('POST', 'tasks', payload,
                            key=f"create:{payload['project_id']}:{key}:{generation}")
        record = {'task_id': str(task['id']), 'status': 'active', 'generation': generation}
        state.records[key] = record
    record.update(metadata)
    record.update(payload_hash=digest, managed_labels=payload['labels'])
    state.save()  # Commit task ID before a reminder can fail.
    return record


def sync_reminder(todoist, state, record, when, now):
    wanted = when.astimezone(UTC).isoformat() if when and when > now else None
    if wanted and record.get('legacy_reminder'):
        # Adopt an existing reminder at this exact instant during an upgrade.
        # Do not touch unrelated reminders the user may have configured.
        for reminder in todoist.list('reminders', {'task_id': record['task_id']}):
            due = reminder.get('due') or {}
            try:
                old_when = datetime.fromisoformat(due.get('date', '').replace('Z', '+00:00'))
                if old_when.tzinfo is None:
                    old_when = old_when.replace(tzinfo=ZoneInfo(due.get('timezone') or 'UTC'))
                if old_when == when:
                    record['reminder_id'] = str(reminder['id'])
                    record['reminder_at'] = wanted
                    break
            except (ValueError, KeyError):
                continue
        record.pop('legacy_reminder', None)
        state.save()
    if wanted == record.get('reminder_at'):
        return
    old_id = record.get('reminder_id')
    if not wanted:
        if old_id:
            todoist.call('DELETE', 'reminders/' + old_id, missing_ok=True, key='remove-reminder:' + old_id)
        record.pop('reminder_id', None)
        record.pop('reminder_at', None)
    else:
        due = {'date': when.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%S'), 'timezone': 'UTC'}
        result = None
        if old_id:
            result = todoist.call('POST', 'reminders/' + old_id, {'due': due, 'service': 'push'},
                                  key=str(uuid.uuid4()), missing_ok=True)
        if result is None:
            result = todoist.call('POST', 'reminders',
                                  {'task_id': record['task_id'], 'reminder_type': 'absolute',
                                   'due': due, 'service': 'push'},
                                  key='reminder:' + record['task_id'] + ':' + wanted)
        record['reminder_id'] = str(result['id'])
        record['reminder_at'] = wanted
    state.save()


def retire(todoist, state, key, delete=False):
    record = state.records[key]
    if record.get('status') != 'active':
        return
    task = todoist.call('GET', 'tasks/' + record['task_id'], missing_ok=True)
    if active(task):
        method, suffix = ('DELETE', '') if delete else ('POST', '/close')
        todoist.call(method, 'tasks/' + record['task_id'] + suffix,
                     key='retire:' + key + ':' + record['task_id'], missing_ok=True)
    record['status'] = 'cancelled' if delete and active(task) else 'done'
    state.save()


def study_dates(due, now, zone, days=7, hour='18:00'):
    study_time = time.fromisoformat(hour)
    end = due.astimezone(zone).date()
    dates = [datetime.combine(end - timedelta(days=offset), study_time, zone)
             for offset in range(days, 0, -1)]
    return [d for d in dates if now < d < due]


def enrich(events, snapshot, now):
    for event in events:
        assignment, context = match_assignment(event, snapshot)
        event.update(assignment=assignment, submitted=confirmed_submitted(assignment),
                     study_allowed=True, impact=None,
                     impact_note='Unavailable: connect the Canvas API to calculate assignment weight.')
        if context:
            course = context['course']
            event['course'] = course.get('course_code') or course['name']
            event['course_name'] = course.get('name', '')
            event['url'] = assignment.get('html_url') or event['url']
            event['title'] = assignment.get('name') or event['title']
            if assignment.get('due_at'):
                event['due'] = datetime.fromisoformat(assignment['due_at'].replace('Z', '+00:00'))
                event['date_only'] = False
            else:
                event['study_allowed'] = False
            result = grade_impact(assignment, course, context['groups'], context['assignments'])
            event['impact'], event['impact_note'] = result.percent, result.explanation
        elif snapshot:
            event['impact_note'] = 'Unavailable: this calendar entry could not be matched to a Canvas assignment.'
        event['kind_label'] = assessment_type(event['title'], assignment)
        event['priority'] = calculate_priority(event['due'], now)
    return events


def assignment_payload(event, project_id):
    labels = [sanitize_label_name(event['course'])]
    if event['kind_label']:
        labels.append(event['kind_label'])
    impact = event['impact']
    if impact is not None and impact >= 5:
        labels.append('High_grade_impact')
    description = f"Course: {event['course']}\n"
    if impact is not None:
        description += f"Estimated share of final grade: {impact:.2f}%\n"
    description += event['impact_note'] + '\n'
    points = (event['assignment'] or {}).get('points_possible')
    if points is not None:
        description += f'Points possible: {points}\n'
    if event['url']:
        description += f"\nCanvas: {event['url']}\n"
    description += '\n' + event['description']
    due = {'due_date': event['due'].date().isoformat()} if event['date_only'] else {
        'due_datetime': event['due'].astimezone(UTC).isoformat()}
    return {'content': event['title'][:500], 'project_id': project_id,
            'description': description[:16383], 'labels': labels, 'priority': event['priority'], **due}


def synchronize(todoist, state, events, snapshot, now, zone, study_days=7, study_time='18:00',
                reminder_days=1, summaries=True, project_name='Canvas Assignments', study_exclusions=frozenset()):
    project = todoist.project(project_name)
    done = set()
    reminder_failures = 0
    excluded_uids = {e['uid'] for e in events
                     if excluded_course([e['course'], e.get('course_name')], study_exclusions)}
    removed = 0
    for key, record in list(state.records.items()):
        if record.get('kind') != 'study' or record.get('status') != 'active':
            continue
        # Legacy study records store the course in managed_labels. Use saved
        # identity too so exclusion removes overdue tasks and absent feed entries.
        names = [record.get('course'), record.get('course_name'), *record.get('managed_labels', [])]
        if record['uid'] in excluded_uids or excluded_course(names, study_exclusions):
            retire(todoist, state, key, delete=True)
            removed += record['status'] == 'cancelled'
    logger.info('Removed %d active study tasks for excluded courses.', removed)

    def remind(record, when):
        nonlocal reminder_failures
        try:
            sync_reminder(todoist, state, record, when, now)
        except RuntimeError:
            reminder_failures += 1
    for key, record in list(state.records.items()):
        if record.get('kind') != 'assignment':
            continue
        assignment, _ = match_assignment({'uid': record['uid'], 'url': record.get('canvas_url', '')}, snapshot)
        if confirmed_submitted(assignment):
            retire(todoist, state, key)
            done.add(record['uid'])
    for event in events:
        uid, key = event['uid'], 'assignment:' + event['uid']
        record = state.records.get(key)
        if event['submitted']:
            if record:
                retire(todoist, state, key)
            done.add(uid)
            continue
        if event['due'] <= now:
            if record:
                existing_record = upsert(todoist, state, key, assignment_payload(event, project),
                                         {'kind': 'assignment', 'uid': uid, 'due': event['due'].isoformat(),
                                          'canvas_url': event['url']})
                if existing_record:
                    remind(existing_record, None)
            for old_key, old in list(state.records.items()):
                if old.get('kind') == 'study' and old['uid'] == uid and datetime.fromisoformat(old['due']) > now:
                    retire(todoist, state, old_key, delete=True)
            continue
        payload = assignment_payload(event, project)
        record = upsert(todoist, state, key, payload,
                        {'kind': 'assignment', 'uid': uid, 'due': event['due'].isoformat(), 'canvas_url': event['url']})
        if record is None:
            done.add(uid)
            continue
        remind_at = event['due'] - timedelta(days=reminder_days) if reminder_days > 0 else None
        remind(record, remind_at)
        wanted = set()
        if study_enabled(event, study_exclusions):
            for when in study_dates(event['due'], now, zone, study_days, study_time):
                study_key = f'study:{uid}:{when.date().isoformat()}'
                wanted.add(study_key)
                study_payload = {
                    'content': f"Study: {event['title']} — {event['course']}"[:500],
                    'description': (f"Prepare for {event['title']}.\n"
                                    f"Assessment due: {event['due'].astimezone(zone).strftime('%b %d, %Y at %I:%M %p %Z')}\n\n"
                                    + payload['description'])[:16383],
                    'project_id': project, 'labels': [*payload['labels'], 'Study'],
                    'priority': event['priority'], 'due_datetime': when.astimezone(UTC).isoformat(),
                }
                study_record = upsert(todoist, state, study_key, study_payload,
                                      {'kind': 'study', 'uid': uid, 'due': when.isoformat(),
                                       'course': event['course'], 'course_name': event.get('course_name', '')})
                if study_record:
                    remind(study_record, when)
        for old_key, old in list(state.records.items()):
            if old.get('kind') == 'study' and old['uid'] == uid and old_key not in wanted:
                if datetime.fromisoformat(old['due']) > now:
                    retire(todoist, state, old_key, delete=True)
    for key, record in list(state.records.items()):
        if record.get('kind') == 'study' and record['uid'] in done:
            retire(todoist, state, key, delete=True)
    if summaries and snapshot:
        grades_project = todoist.project('Canvas Grades')
        for cid, context in snapshot.items():
            course = context['course']
            name = course.get('course_code') or course['name']
            score = current_grade(course)
            display = f'{score:.2f}%' if score is not None else 'Grade unavailable'
            description = ('Canvas current grade (graded work), not a forecast of the final grade. '
                           'Ungraded work may be excluded.\n' if score is not None else
                           'Canvas did not provide a visible current grade. This does not mean zero.\n')
            weights = [f"{g['name']}: {g['group_weight']:g}%" for g in context['groups']
                       if isinstance(g.get('group_weight'), (int, float))]
            if course.get('apply_assignment_group_weights') and weights:
                description += '\nGrading categories:\n' + '\n'.join(weights) + '\n'
            description += f'\nLast checked: {now.astimezone(zone).date()}\nOpen Canvas for official grades.'
            if context.get('grade_url'):
                description += '\n' + context['grade_url']
            upsert(todoist, state, 'grade:' + cid,
                   {'content': f'{name} — {display}'[:500], 'description': description[:16383],
                    'project_id': grades_project, 'labels': [sanitize_label_name(name), 'Grade'], 'priority': 1},
                   {'kind': 'grade', 'course_id': cid})
    if reminder_failures:
        raise RuntimeError(f'Tasks were saved, but {reminder_failures} reminder operations failed. '
                           'Check Todoist reminder access and retry; saved tasks will not be recreated.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true', help='Read Canvas; never contact Todoist or save state.')
    parser.add_argument('--initialize-state', action='store_true', help='Allow a first run without saved state.')
    parser.add_argument('--preview-file', help='Write a private local JSON preview; do not publish it.')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    try:
        zone = ZoneInfo(os.getenv('STUDY_TIMEZONE', 'America/Los_Angeles'))
        days, hour = int(os.getenv('STUDY_DAYS_BEFORE', '7')), os.getenv('STUDY_TIME', '18:00')
        exclusions = {course_key(name) for name in re.split(r'[,\n]', os.getenv('STUDY_EXCLUDED_COURSES', ''))
                      if course_key(name)}
        if not 0 <= days <= 30 or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', hour):
            raise RuntimeError('Study settings require 0–30 days and a time in HH:MM format.')
        reminder_days = int(os.getenv('REMINDER_DAYS_BEFORE', '1'))
        if reminder_days < 0:
            raise RuntimeError('REMINDER_DAYS_BEFORE cannot be negative.')
        now = datetime.now(UTC)
        feed = os.getenv('CANVAS_ICS_URL', '')
        if not feed:
            raise RuntimeError('CANVAS_ICS_URL is required.')
        state = SyncState(os.getenv('STATE_FILE', 'sync_state.json'))
        if not args.dry_run and not state.exists and not args.initialize_state:
            raise RuntimeError('Saved sync state is missing. Restore it to prevent duplicates; '
                             'use --initialize-state only for an intentional first sync.')
        try:
            response = requests.get(feed, timeout=30)
            response.raise_for_status()
        except requests.RequestException:
            raise RuntimeError('The Canvas calendar feed could not be read.') from None
        events = parse_ics_events(response.text, zone, now)
        token, snapshot = os.getenv('CANVAS_API_TOKEN', ''), {}
        if token:
            site = os.getenv('CANVAS_BASE_URL') or f'https://{urlparse(feed).netloc}'
            snapshot = CanvasClient(site, token).snapshot()
        else:
            logger.warning('Canvas API is not connected: grade data and verified auto-completion are unavailable.')
        enrich(events, snapshot, now)
        upcoming = [e for e in events if e['due'] > now and not e['submitted']]
        assessments = [e for e in upcoming if e['kind_label']]
        eligible = [e for e in upcoming if study_enabled(e, exclusions)]
        excluded_count = sum(excluded_course([e['course'], e.get('course_name')], exclusions) for e in assessments)
        session_count = sum(len(study_dates(e['due'], now, zone, days, hour)) for e in eligible)
        logger.info('Calendar: %d upcoming entries; %d assessments detected; %d excluded by course; '
                    '%d eligible assessments; %d future study sessions planned.',
                    len(upcoming), len(assessments), excluded_count, len(eligible), session_count)
        if args.preview_file:
            preview = {'assignments': [assignment_payload(e, '(preview)') for e in upcoming],
                       'study_sessions': [{'assignment': e['title'], 'at': at.isoformat()}
                                          for e in eligible
                                          for at in study_dates(e['due'], now, zone, days, hour)],
                       'course_grades': [{'course': c['course'].get('course_code'),
                                          'current_score': current_grade(c['course'])} for c in snapshot.values()]}
            fd = os.open(args.preview_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump(preview, stream, indent=2)
        if args.dry_run:
            logger.info('Preview: %d upcoming calendar entries; %d courses read. No Todoist changes.', len(upcoming), len(snapshot))
            return 0
        api_token = os.getenv('TODOIST_API_TOKEN', '')
        if not api_token:
            raise RuntimeError('TODOIST_API_TOKEN is required for a live sync.')
        synchronize(Todoist(api_token), state, events, snapshot, now, zone, days, hour,
                    reminder_days, os.getenv('SYNC_COURSE_GRADES', 'true').lower() == 'true',
                    os.getenv('TODOIST_PROJECT_NAME', 'Canvas Assignments'), exclusions)
        state.save()
        logger.info('Sync completed. Task details and grades are omitted from logs.')
        return 0
    except Exception as error:
        # Network exception strings can expose the private calendar URL.
        if isinstance(error, RuntimeError):
            logger.error('%s', error)
        elif isinstance(error, ValueError) and not isinstance(error, requests.RequestException):
            logger.error('Invalid configuration or data (%s). No fresh state will be assumed.', type(error).__name__)
        else:
            logger.error('Sync stopped (%s). Saved progress is retained.', type(error).__name__)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
