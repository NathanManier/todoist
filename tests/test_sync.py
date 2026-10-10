import copy
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
import requests

import sync
from canvas_data import (CanvasClient, CanvasError, assessment_type, confirmed_submitted,
                         current_grade, grade_impact, match_assignment)

UTC = timezone.utc
ZONE = ZoneInfo('America/Los_Angeles')
NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)


@pytest.fixture
def snapshot():
    assignment = {'id': 11, 'name': 'Quiz 1', 'assignment_group_id': 1, 'points_possible': 10,
                  'due_at': '2026-10-10T06:00:00Z', 'published': True,
                  'html_url': 'https://school.example/courses/10/assignments/11',
                  'submission_types': ['online_quiz'], 'submission': {'workflow_state': 'unsubmitted'}}
    return {'10': {'course': {'id': 10, 'course_code': 'COMS-1102-V13-2268',
                              'apply_assignment_group_weights': True,
                              'enrollments': [{'type': 'student', 'computed_current_score': 94.5}]},
                   'groups': [{'id': 1, 'name': 'Quizzes', 'group_weight': 25, 'rules': {}},
                              {'id': 2, 'name': 'Exams', 'group_weight': 75, 'rules': {}}],
                   'assignments': [assignment,
                                   {'id': 12, 'assignment_group_id': 1, 'points_possible': 90},
                                   {'id': 13, 'assignment_group_id': 2, 'points_possible': 100}]}}


def impact(snapshot):
    c = snapshot['10']
    return grade_impact(c['assignments'][0], c['course'], c['groups'], c['assignments'])


def event(snapshot=None):
    raw = {'uid': 'event-assignment-11', 'title': 'Quiz 1', 'course': 'COMS-1102-V13-2268',
           'description': 'Review chapter one.', 'url': 'https://school.example/courses/10/assignments/11',
           'due': datetime(2026, 10, 10, 6, tzinfo=UTC), 'date_only': False}
    return sync.enrich([raw], snapshot or {}, NOW)[0]


class FakeTodoist:
    def __init__(self):
        self.tasks, self.reminders, self.calls = {}, {}, []
        self.idempotency = {}
        self.fail_reminders = False
        self.fail_get = False

    def project(self, name):
        return name

    def ensure_labels(self, names):
        pass

    def list(self, path, params=None):
        assert path == 'reminders'
        return [copy.deepcopy(r) for r in self.reminders.values()
                if r.get('task_id') == params['task_id']]

    def call(self, method, path, payload=None, key=None, **kwargs):
        self.calls.append((method, path, copy.deepcopy(payload), key))
        if self.fail_get and method == 'GET':
            raise RuntimeError('Temporary network error')
        if self.fail_reminders and path.startswith('reminders'):
            raise RuntimeError('Reminder unavailable')
        if method == 'POST' and key in self.idempotency:
            return copy.deepcopy(self.idempotency[key])
        if path in ('tasks', 'reminders') and method == 'POST':
            target = self.tasks if path == 'tasks' else self.reminders
            identifier = str(len(self.idempotency) + 1)
            result = {'id': identifier, **copy.deepcopy(payload)}
            if path == 'tasks':
                value = payload.get('due_datetime') or payload.get('due_date')
                result['due'] = {'date': value, 'timezone': None, 'is_recurring': False} if value else None
            target[identifier] = result
            self.idempotency[key] = copy.deepcopy(result)
            return copy.deepcopy(result)
        parts = path.split('/')
        target = self.tasks if parts[0] == 'tasks' else self.reminders
        identifier = parts[1]
        if method == 'GET':
            return copy.deepcopy(target.get(identifier))
        if method == 'DELETE':
            target.pop(identifier, None)
            return None
        if len(parts) == 3 and parts[2] == 'close':
            target[identifier]['checked'] = True
            return None
        if method == 'POST':
            if identifier not in target:
                return None
            target[identifier].update(copy.deepcopy(payload))
            result = copy.deepcopy(target[identifier])
            self.idempotency[key] = result
            return result
        raise AssertionError((method, path))


def run(api, state, events, snapshot=None, now=NOW):
    sync.synchronize(api, state, events, snapshot or {}, now, ZONE)


def test_weights_use_full_course_not_only_upcoming_calendar(snapshot):
    assert impact(snapshot).percent == 2.5  # 25% * 10 / (10 + 90).
    snapshot['10']['course']['apply_assignment_group_weights'] = False
    assert impact(snapshot).percent == 5  # 10 / 200 * 100.


@pytest.mark.parametrize('change', ['drop', 'missing_points', 'period', 'nonstandard_weights', 'unknown_weighting'])
def test_ambiguous_weights_are_not_fabricated(snapshot, change):
    c = snapshot['10']
    if change == 'drop':
        c['groups'][0]['rules'] = {'drop_lowest': 1}
    elif change == 'missing_points':
        c['assignments'][1]['points_possible'] = None
    elif change == 'period':
        c['course']['enrollments'][0]['multiple_grading_periods_enabled'] = True
    elif change == 'nonstandard_weights':
        c['groups'][1]['group_weight'] = 80
    else:
        c['course'].pop('apply_assignment_group_weights')
    assert impact(snapshot).percent is None


def test_omitted_and_excused_work(snapshot):
    snapshot['10']['assignments'][1]['omit_from_final_grade'] = True
    assert impact(snapshot).percent == 25
    snapshot['10']['assignments'][0]['submission']['excused'] = True
    assert impact(snapshot).percent == 0


def test_grade_zero_and_hidden_are_distinct(snapshot):
    c = snapshot['10']['course']
    c['enrollments'][0]['computed_current_score'] = 0
    assert current_grade(c) == 0
    c['hide_final_grades'] = True
    assert current_grade(c) is None
    assert current_grade({'enrollments': None}) is None


@pytest.mark.parametrize(('title', 'kind'), [('Quiz 2', 'Quiz'), ('Weekly quizzes', 'Quiz'),
                                          ('Midterm', 'Exam'), ('Test 1', 'Exam'), ('Final exam', 'Exam'),
                                          ('Final project', None), ('Contest', None), ('Homework', None)])
def test_assessment_detection(title, kind):
    assert assessment_type(title) == kind


def test_submission_requires_current_user_evidence():
    assert not confirmed_submitted({'has_submitted_submissions': True})
    assert not confirmed_submitted({'submission': {'workflow_state': 'graded', 'score': 0, 'missing': True}})
    assert confirmed_submitted({'submission': {'workflow_state': 'submitted', 'submitted_at': '2026-10-01'}})
    assert confirmed_submitted({'submission': {'excused': True}})


def test_id_matching_does_not_use_names(snapshot):
    assert match_assignment({'uid': 'something-else', 'title': 'Quiz 1'}, snapshot) == (None, None)
    assert match_assignment({'uid': 'event-assignment-11'}, snapshot)[0]['id'] == 11


def test_local_six_pm_across_daylight_saving_change():
    due = datetime(2026, 11, 5, 18, tzinfo=ZONE)
    dates = sync.study_dates(due, datetime(2026, 10, 20, tzinfo=UTC), ZONE)
    assert len(dates) == 7
    assert all(d.hour == 18 for d in dates)
    assert {d.utcoffset() for d in dates} == {timedelta(hours=-7), timedelta(hours=-8)}
    assert dates[-1].date().isoformat() == '2026-11-04'


def test_late_discovery_only_schedules_remaining_days():
    due = datetime(2026, 10, 10, 12, tzinfo=ZONE)
    dates = sync.study_dates(due, datetime(2026, 10, 8, 19, tzinfo=ZONE), ZONE)
    assert [d.day for d in dates] == [9]


def test_exclusions_match_full_course_identity_only():
    exclusions = {sync.course_key('COMS-1102-V13-2268'), sync.course_key('Public Speaking')}
    assert sync.excluded_course(['coms_1102_v13_2268'], exclusions)
    assert sync.excluded_course(['PUBLIC SPEAKING'], exclusions)
    assert not sync.excluded_course(['COMS-1102-V14-2268', 'Public Speaking Practice'], exclusions)


def test_excluded_course_keeps_assignments_grades_and_other_course_study(tmp_path, snapshot):
    state, api = sync.SyncState(tmp_path / 'state.json'), FakeTodoist()
    speaking = event(snapshot)
    other = {**event(), 'uid': 'other-quiz', 'course': 'BIO-1111'}
    exclusions = {sync.course_key('COMS-1102-V13-2268')}
    for _ in range(2):
        sync.synchronize(api, state, [speaking, other], snapshot, NOW, ZONE, study_exclusions=exclusions)
    assert state.records['assignment:' + speaking['uid']]['status'] == 'active'
    assert state.records['grade:10']['status'] == 'active'
    studies = [r for r in state.records.values() if r['kind'] == 'study']
    assert len(studies) == 7
    assert {r['uid'] for r in studies} == {'other-quiz'}
    assert all(datetime.fromisoformat(r['due']).hour == 18 for r in studies)


def test_exclusion_removes_legacy_overdue_and_future_tasks_even_without_feed(tmp_path):
    state, api = sync.SyncState(tmp_path / 'state.json'), FakeTodoist()
    run(api, state, [event()])
    studies = [r for r in state.records.values() if r['kind'] == 'study']
    # Model the version already deployed, which stored course labels only.
    for record in studies:
        record.pop('course')
        record.pop('course_name')
    completed_id = studies[0]['task_id']
    api.tasks[completed_id]['checked'] = True
    api.tasks['manual'] = {'id': 'manual', 'content': 'My own study plan', 'labels': ['Study', 'COMS-1102-V13-2268']}
    exclusions = {sync.course_key('COMS-1102-V13-2268')}
    later = datetime(2026, 10, 8, 22, tzinfo=UTC)
    sync.synchronize(api, state, [], {}, later, ZONE, study_exclusions=exclusions)
    assert len(api.tasks) == 3  # Parent assignment, completed session, and manual task.
    assert api.tasks[completed_id]['checked']
    assert 'manual' in api.tasks
    assert sum(r['status'] == 'cancelled' for r in studies) == 6
    # A later feed refresh must not resurrect the cancelled sessions.
    sync.synchronize(api, state, [event()], {}, later, ZONE, study_exclusions=exclusions)
    assert len(api.tasks) == 3
    assert not any(r['status'] == 'active' for r in studies)


def test_api_course_name_exclusion_survives_course_code_alias(tmp_path, snapshot):
    snapshot['10']['course']['name'] = 'Public Speaking'
    state, api = sync.SyncState(tmp_path / 'state.json'), FakeTodoist()
    exclusions = {sync.course_key('Public Speaking')}
    e = event(snapshot)
    assert not sync.study_enabled(e, exclusions)
    sync.synchronize(api, state, [e], snapshot, NOW, ZONE, study_exclusions=exclusions)
    assert not any(r['kind'] == 'study' for r in state.records.values())


def test_all_day_event_uses_start_not_exclusive_end():
    content = ('BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:a\nSUMMARY:Quiz [BIO 1111]\n'
               'DTSTART;VALUE=DATE:20261010\nDTEND;VALUE=DATE:20261011\nEND:VEVENT\nEND:VCALENDAR')
    result = sync.parse_ics_events(content, ZONE, NOW)[0]
    assert result['due'].date().isoformat() == '2026-10-10'
    assert result['date_only']


def test_repeat_runs_and_manual_completion_create_no_duplicates(tmp_path, snapshot):
    state, api = sync.SyncState(tmp_path / 'state.json'), FakeTodoist()
    events = [event(snapshot)]
    run(api, state, events, snapshot)
    assert len(api.tasks) == 9  # Assignment, seven sessions, one grade summary.
    assert len(api.reminders) == 8
    session = next(r for r in state.records.values() if r['kind'] == 'study')
    api.tasks[session['task_id']]['checked'] = True
    run(api, sync.SyncState(state.path), events, snapshot)
    assert len(api.tasks) == 9
    assert len(api.reminders) == 8
    assert api.tasks[session['task_id']]['checked']


def test_reschedule_removes_obsolete_future_sessions_and_can_move_back(tmp_path, snapshot):
    state, api = sync.SyncState(tmp_path / 'state.json'), FakeTodoist()
    e = event(snapshot)
    run(api, state, [e])
    original_dates = {r['due'] for r in state.records.values() if r['kind'] == 'study'}
    e['due'] += timedelta(days=10)
    run(api, state, [e])
    assert len([r for r in state.records.values() if r['kind'] == 'study' and r['status'] == 'active']) == 7
    e['due'] -= timedelta(days=10)
    run(api, state, [e])
    current = {r['due'] for r in state.records.values() if r['kind'] == 'study' and r['status'] == 'active'}
    assert current == original_dates


def test_feed_disappearance_never_completes_assignments(tmp_path):
    state, api = sync.SyncState(tmp_path / 'state.json'), FakeTodoist()
    run(api, state, [event()])
    before = copy.deepcopy(state.records)
    run(api, state, [])
    assert state.records == before
    assert not any(t.get('checked') for t in api.tasks.values())


def test_api_submission_completes_even_when_missing_from_feed(tmp_path, snapshot):
    state, api = sync.SyncState(tmp_path / 'state.json'), FakeTodoist()
    run(api, state, [event(snapshot)], snapshot)
    snapshot['10']['assignments'][0]['submission'] = {'workflow_state': 'submitted', 'submitted_at': '2026-10-01'}
    run(api, state, [], snapshot)
    assert state.records['assignment:event-assignment-11']['status'] == 'done'
    assert not any(r['kind'] == 'study' and r['status'] == 'active' for r in state.records.values())


def test_network_failure_is_not_mistaken_for_deleted_task(tmp_path):
    state, api = sync.SyncState(tmp_path / 'state.json'), FakeTodoist()
    run(api, state, [event()])
    api.fail_get = True
    with pytest.raises(RuntimeError):
        run(api, state, [event()])
    assert len(api.tasks) == 8
    assert state.records['assignment:event-assignment-11']['status'] == 'active'


def test_reminder_failure_saves_tasks_and_retries_without_duplicates(tmp_path, snapshot):
    state, api = sync.SyncState(tmp_path / 'state.json'), FakeTodoist()
    api.fail_reminders = True
    with pytest.raises(RuntimeError, match='Tasks were saved'):
        run(api, state, [event(snapshot)], snapshot)
    assert len(api.tasks) == 9
    api.fail_reminders = False
    run(api, sync.SyncState(state.path), [event(snapshot)], snapshot)
    assert len(api.tasks) == 9
    assert len(api.reminders) == 8


def test_manual_labels_survive_grade_updates(tmp_path, snapshot):
    state, api = sync.SyncState(tmp_path / 'state.json'), FakeTodoist()
    run(api, state, [event(snapshot)], snapshot)
    task = api.tasks[state.records['assignment:event-assignment-11']['task_id']]
    task['labels'].append('My_custom_label')
    snapshot['10']['assignments'][0]['points_possible'] = 100
    run(api, state, [event(snapshot)], snapshot)
    assert 'My_custom_label' in task['labels']
    assert 'High_grade_impact' in task['labels']


def test_legacy_state_migration_and_corruption(tmp_path):
    path = tmp_path / 'state.json'
    path.write_text(json.dumps({'synced_events': {'uid': {'todoist_task_id': 'old-id', 'event_hash': 'old'}}}))
    state = sync.SyncState(path)
    assert state.records['assignment:uid']['task_id'] == 'old-id'
    state.save()
    assert sync.SyncState(path).records == state.records
    path.write_text('{broken')
    with pytest.raises(ValueError):
        sync.SyncState(path)


def test_canvas_pagination_and_cross_origin_guard():
    session = Mock()
    session.get.side_effect = [SimpleNamespace(status_code=200, json=lambda: [{'id': 1}],
                                              links={'next': {'url': 'https://school.example/api/v1/courses?page=2'}}),
                               SimpleNamespace(status_code=200, json=lambda: [{'id': 2}], links={})]
    assert len(CanvasClient('https://school.example', 'secret', session).list('courses')) == 2
    session.get.side_effect = None
    session.get.return_value = SimpleNamespace(status_code=200, json=lambda: [],
                                               links={'next': {'url': 'https://outside.example/api/v1/courses'}})
    with pytest.raises(CanvasError):
        CanvasClient('https://school.example', 'secret', session).list('courses')
    assert session.get.call_count == 3


def test_todoist_uses_current_endpoint_and_timezone_payload():
    session = Mock()
    session.request.return_value = SimpleNamespace(status_code=200, content=b'{}', json=lambda: {'id': 'x'})
    api = sync.Todoist('secret', session)
    api.call('POST', 'reminders', {'due': {'date': '2026-10-02T01:00:00', 'timezone': 'UTC'}}, key='one')
    args, kwargs = session.request.call_args
    assert args == ('POST', 'https://api.todoist.com/api/v1/reminders')
    assert kwargs['headers']['X-Request-ID'] == sync.request_id('one')
    assert kwargs['allow_redirects'] is False


def test_dry_run_never_contacts_todoist_or_saves_state(tmp_path, monkeypatch):
    monkeypatch.setenv('CANVAS_ICS_URL', 'https://school.example/private-token.ics')
    monkeypatch.delenv('CANVAS_API_TOKEN', raising=False)
    path = tmp_path / 'state.json'
    monkeypatch.setenv('STATE_FILE', str(path))
    monkeypatch.setattr('sys.argv', ['sync.py', '--dry-run'])
    monkeypatch.setattr(sync.requests, 'get', Mock(return_value=SimpleNamespace(
        text='BEGIN:VCALENDAR\nVERSION:2.0\nEND:VCALENDAR', raise_for_status=lambda: None)))
    todoist = Mock(side_effect=AssertionError('No Todoist access allowed'))
    monkeypatch.setattr(sync, 'Todoist', todoist)
    assert sync.main() == 0
    assert not path.exists()
    todoist.assert_not_called()


def test_missing_state_stops_live_run_before_network(tmp_path, monkeypatch):
    monkeypatch.setenv('CANVAS_ICS_URL', 'https://school.example/private-token.ics')
    monkeypatch.setenv('STATE_FILE', str(tmp_path / 'absent.json'))
    monkeypatch.setattr('sys.argv', ['sync.py'])
    get = Mock(side_effect=AssertionError('No network allowed'))
    monkeypatch.setattr(sync.requests, 'get', get)
    assert sync.main() == 1
    get.assert_not_called()


def test_feed_errors_do_not_log_private_url(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv('CANVAS_ICS_URL', 'https://school.example/private-token.ics')
    monkeypatch.setenv('STATE_FILE', str(tmp_path / 'absent.json'))
    monkeypatch.setattr('sys.argv', ['sync.py', '--dry-run'])
    monkeypatch.setattr(sync.requests, 'get', Mock(side_effect=requests.ConnectionError('private-token.ics')))
    assert sync.main() == 1
    assert 'private-token' not in caplog.text


def test_legacy_matching_reminder_is_adopted(tmp_path):
    state, api = sync.SyncState(tmp_path / 'state.json'), FakeTodoist()
    when = NOW + timedelta(days=2)
    record = {'task_id': 'old-task', 'legacy_reminder': True}
    state.records['assignment:old'] = record
    api.reminders['old-reminder'] = {'id': 'old-reminder', 'task_id': 'old-task',
                                     'due': {'date': when.isoformat(), 'timezone': 'UTC'}}
    sync.sync_reminder(api, state, record, when, NOW)
    assert record['reminder_id'] == 'old-reminder'
    assert not api.calls


def test_deadline_moved_to_past_updates_parent_and_cancels_future_study(tmp_path):
    state, api = sync.SyncState(tmp_path / 'state.json'), FakeTodoist()
    e = event()
    run(api, state, [e])
    e['due'] = NOW - timedelta(hours=1)
    e['priority'] = 4
    run(api, state, [e])
    task = api.tasks[state.records['assignment:event-assignment-11']['task_id']]
    assert task['deadline_date'] == e['due'].astimezone(ZONE).date().isoformat()
    assert task['due'] is None
    assert state.records['assignment:event-assignment-11']['canvas_deadline_at'] == e['due'].isoformat()
    assert not any(r['kind'] == 'study' and r['status'] == 'active' for r in state.records.values())


def test_timezone_reaches_todoist_without_naive_due_string(tmp_path):
    state, api = sync.SyncState(tmp_path / 'state.json'), FakeTodoist()
    run(api, state, [event()])
    studies = [t for t in api.tasks.values() if 'Study' in t['labels']]
    assert all(datetime.fromisoformat(t['due_datetime']).astimezone(ZONE).hour == 18 for t in studies)
    assert all('due_string' not in t for t in api.tasks.values())


def test_hidden_grade_summary_is_unavailable_not_zero(tmp_path, snapshot):
    snapshot['10']['course']['hide_final_grades'] = True
    state, api = sync.SyncState(tmp_path / 'state.json'), FakeTodoist()
    run(api, state, [], snapshot)
    task = next(t for t in api.tasks.values() if 'Grade' in t['labels'])
    assert task['content'].endswith('Grade unavailable')
    assert '0%' not in task['content']


def test_canvas_failure_is_before_any_todoist_contact(tmp_path, monkeypatch):
    monkeypatch.setenv('CANVAS_ICS_URL', 'https://school.example/feed.ics')
    monkeypatch.setenv('CANVAS_API_TOKEN', 'never-log-this')
    monkeypatch.setenv('TODOIST_API_TOKEN', 'also-private')
    monkeypatch.setenv('STATE_FILE', str(tmp_path / 'state.json'))
    monkeypatch.setattr('sys.argv', ['sync.py', '--initialize-state'])
    monkeypatch.setattr(sync.requests, 'get', Mock(return_value=SimpleNamespace(
        text='BEGIN:VCALENDAR\nVERSION:2.0\nEND:VCALENDAR', raise_for_status=lambda: None)))
    client = Mock()
    client.snapshot.side_effect = CanvasError('Canvas read failed (HTTP 401).')
    monkeypatch.setattr(sync, 'CanvasClient', Mock(return_value=client))
    todoist = Mock(side_effect=AssertionError('Should not access Todoist'))
    monkeypatch.setattr(sync, 'Todoist', todoist)
    assert sync.main() == 1
    todoist.assert_not_called()

