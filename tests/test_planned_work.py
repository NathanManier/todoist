import copy
from datetime import datetime, timedelta

import pytest

import sync
from test_sync import FakeTodoist, NOW, UTC, ZONE, event, run


def setup_assignment(tmp_path):
    state, api, e = sync.SyncState(tmp_path / 'state.json'), FakeTodoist(), event()
    sync.synchronize(api, state, [e], {}, NOW, ZONE, study_days=0)
    record = state.records['assignment:' + e['uid']]
    return state, api, e, record, api.tasks[record['task_id']]


def task_updates(api, task_id):
    return [payload for method, path, payload, _ in api.calls
            if method == 'POST' and path == 'tasks/' + task_id]


def resync(api, state, e):
    sync.synchronize(api, state, [e], {}, NOW, ZONE, study_days=0)


def test_new_assignment_has_separate_deadline_without_an_invented_work_plan(tmp_path):
    state, api, e, record, task = setup_assignment(tmp_path)
    assert task['due'] is None
    assert not any(key.startswith('due_') for key in task)
    assert task['deadline_date'] == '2026-10-09'  # 06:00 UTC next day.
    assert '2026-10-09T23:00:00-07:00' in task['description']
    assert e['url'] in task['description']
    assert record['canvas_deadline_at'] == '2026-10-10T06:00:00+00:00'
    assert record['canvas_deadline_source'] == 'Canvas calendar'
    assert record['canvas_deadline_date'] == task['deadline_date']
    assert record['planned_work_due'] is None
    assert sync.SyncState(state.path).records == state.records


@pytest.mark.parametrize('planned', [None,
    {'date': '2026-10-05', 'is_recurring': False},
    {'date': '2026-10-05T15:30:00Z', 'timezone': 'America/Los_Angeles', 'is_recurring': False},
    {'date': '2026-10-05T08:30:00', 'timezone': None, 'is_recurring': False},
    {'date': '2026-10-05', 'is_recurring': True, 'string': 'every Monday'}])
@pytest.mark.parametrize('change', ['priority', 'title', 'deadline', 'description'])
def test_every_update_preserves_planned_date_time_and_recurrence(tmp_path, planned, change):
    state, api, e, record, task = setup_assignment(tmp_path)
    task['due'] = copy.deepcopy(planned)
    task['duration'] = {'amount': 90, 'unit': 'minute'}
    task['labels'].append('My_plan')
    if change == 'priority':
        e['priority'] = 4
    elif change == 'title':
        e['title'] = 'Revised Quiz'
    elif change == 'deadline':
        e['due'] += timedelta(days=3)
    else:
        e['description'] = 'New Canvas instructions'
    api.calls.clear()
    resync(api, sync.SyncState(state.path), e)
    assert task['due'] == planned
    assert task['duration'] == {'amount': 90, 'unit': 'minute'}
    assert 'My_plan' in task['labels']
    updates = task_updates(api, task['id'])
    assert len(updates) == 1
    assert not any(k == 'due' or k.startswith('due_') for k in updates[0])
    assert updates[0]['deadline_date'] == e['due'].astimezone(ZONE).date().isoformat()
    saved = sync.SyncState(state.path).records['assignment:' + e['uid']]
    assert saved['planned_work_due'] == sync.planned_due(task)
    api.calls.clear()
    resync(api, sync.SyncState(state.path), e)
    assert not task_updates(api, task['id'])


def test_migration_preserves_legacy_notes_and_work_time(tmp_path):
    state, api, e, record, task = setup_assignment(tmp_path)
    legacy = 'Course: COMS\nOriginal Canvas detail.\n\nWork Tuesday at 2.  \n'
    task['description'] = legacy
    task['due'] = {'date': '2026-10-05T14:00:00', 'is_recurring': False}
    record.pop('managed_description_hash')
    record['payload_hash'] = 'old-full-payload-hash'
    record.pop('canvas_deadline_at')
    state.save()
    resync(api, sync.SyncState(state.path), e)
    assert task['description'].startswith(legacy + '\n\n')
    assert task['description'].count(sync.MANAGED_START) == 1
    assert task['due']['date'] == '2026-10-05T14:00:00'
    e['due'] += timedelta(days=1)
    resync(api, sync.SyncState(state.path), e)
    assert task['description'].startswith(legacy + '\n\n')
    assert task['description'].count(sync.MANAGED_START) == 1


def test_manual_notes_outside_block_survive_canvas_changes(tmp_path):
    state, api, e, record, task = setup_assignment(tmp_path)
    before, after = 'My work plan\n\n', '\n\nKeep these exact notes.  \n'
    task['description'] = before + task['description'] + after
    e['description'] = 'Revised source details'
    resync(api, state, e)
    assert task['description'].startswith(before)
    assert task['description'].endswith(after)
    assert 'Revised source details' in task['description']
    assert 'Review chapter one.' not in task['description']


def test_edits_inside_generated_block_are_preserved_instead_of_guessed(tmp_path):
    state, api, e, record, task = setup_assignment(tmp_path)
    task['description'] = task['description'].replace('Review chapter one.', 'My custom edited plan')
    edited = task['description']
    e['priority'] = 4
    resync(api, state, e)
    assert task['description'].startswith(edited + '\n\n')
    e['description'] = 'New instructions'
    resync(api, state, e)
    assert task['description'].startswith(edited + '\n\n')
    assert task['description'].count(sync.MANAGED_START) == 2


def test_no_silent_truncation_of_long_manual_description(tmp_path):
    state, api, e, record, task = setup_assignment(tmp_path)
    task['description'] = 'N' * sync.DESCRIPTION_LIMIT
    e['priority'] = 4
    old_record = copy.deepcopy(record)
    api.calls.clear()
    with pytest.raises(RuntimeError, match='Existing notes were preserved'):
        resync(api, state, e)
    assert task['description'] == 'N' * sync.DESCRIPTION_LIMIT
    assert record == old_record
    assert not task_updates(api, task['id'])


@pytest.mark.parametrize(('deadline', 'day', 'offset'), [
    ('2026-11-01T06:30:00+00:00', '2026-10-31', '-07:00'),
    ('2026-11-02T07:30:00+00:00', '2026-11-01', '-08:00')])
def test_deadline_local_day_and_exact_cutoff_across_dst(deadline, day, offset):
    e = event()
    e['due'] = datetime.fromisoformat(deadline)
    payload = sync.assignment_payload(e, 'project', ZONE)
    assert payload['deadline_date'] == day
    assert offset in payload['description']
    assert sync.canvas_deadline(e, ZONE)['canvas_deadline_at'] == deadline


def test_all_day_deadline_does_not_claim_an_exact_cutoff():
    e = event()
    e.update(due=datetime(2026, 10, 10, 23, 59, 59, tzinfo=ZONE), date_only=True)
    payload = sync.assignment_payload(e, 'project', ZONE)
    assert payload['deadline_date'] == '2026-10-10'
    assert 'date only; exact cutoff not provided' in payload['description']
    assert '23:59:59' not in payload['description']
    assert sync.canvas_deadline(e, ZONE)['canvas_deadline_at'] is None


def test_canvas_api_source_is_explicit():
    e = event()
    e['assignment'] = {'due_at': e['due'].isoformat()}
    assert sync.canvas_deadline(e, ZONE)['canvas_deadline_source'] == 'Canvas API'


def test_manual_reminders_survive_deadline_change(tmp_path):
    state, api, e, record, task = setup_assignment(tmp_path)
    api.reminders['manual'] = {'id': 'manual', 'task_id': task['id'], 'due': {'date': '2026-10-05T10:00:00Z'}}
    manual = copy.deepcopy(api.reminders['manual'])
    reminder_id = record['reminder_id']
    e['due'] += timedelta(days=1)
    resync(api, state, e)
    assert api.reminders['manual'] == manual
    assert record['reminder_id'] == reminder_id
    assert record['reminder_at'] == (e['due'] - timedelta(days=1)).isoformat()


@pytest.mark.parametrize('planned', [None,
    {'date': '2026-10-05', 'is_recurring': False},
    {'date': '2026-10-05T10:00:00-07:00', 'is_recurring': False},
    {'date': '2026-10-05T10:00:00-07:00', 'is_recurring': True, 'string': 'every day'}])
def test_moved_study_sessions_survive_deadline_change_and_are_not_recreated(tmp_path, planned):
    state, api, e = sync.SyncState(tmp_path / 'state.json'), FakeTodoist(), event()
    run(api, state, [e])
    key, record = next((k, r) for k, r in state.records.items() if r['kind'] == 'study')
    task = api.tasks[record['task_id']]
    task['due'] = copy.deepcopy(planned)
    e['priority'] = 4
    run(api, state, [e])
    assert task['due'] == planned
    e['due'] += timedelta(days=10)
    run(api, state, [e])
    assert record['task_id'] in api.tasks
    assert record['status'] == 'manual'
    e['due'] -= timedelta(days=10)
    run(api, sync.SyncState(state.path), [e])
    assert state.records[key]['task_id'] == record['task_id']
    creates = [payload for method, path, payload, _ in api.calls if method == 'POST' and path == 'tasks']
    assert len(creates) == 21  # 1 assignment + 7 initial + 7 moved + 6 restored.
    assert task['due'] == planned


def test_moved_study_reminder_follows_current_plan(tmp_path):
    state, api, e = sync.SyncState(tmp_path / 'state.json'), FakeTodoist(), event()
    run(api, state, [e])
    record = next(r for r in state.records.values() if r['kind'] == 'study')
    task = api.tasks[record['task_id']]
    when = '2026-10-05T10:00:00-07:00'
    task['due'] = {'date': when, 'is_recurring': False}
    run(api, state, [e])
    assert record['reminder_at'] == '2026-10-05T17:00:00+00:00'
    assert task['due']['date'] == when


def test_recurring_study_removes_only_owned_absolute_reminder_and_is_not_retired(tmp_path):
    state, api, e = sync.SyncState(tmp_path / 'state.json'), FakeTodoist(), event()
    run(api, state, [e])
    key, record = next((k, r) for k, r in state.records.items() if r['kind'] == 'study')
    task = api.tasks[record['task_id']]
    task['due']['is_recurring'] = True
    reminder_id = record['reminder_id']
    api.reminders['manual'] = {'id': 'manual', 'task_id': task['id'], 'due': {'date': '2026-10-05T10:00:00Z'}}
    manual = copy.deepcopy(api.reminders['manual'])
    e['priority'] = 4
    run(api, state, [e])
    sync.retire(api, state, key, delete=True)
    assert record['status'] == 'manual'
    assert task['due']['is_recurring']
    assert reminder_id not in api.reminders
    assert api.reminders['manual'] == manual


@pytest.mark.parametrize('status', ['checked', 'is_completed', 'is_deleted', 'missing'])
def test_completed_deleted_assignments_never_resurrect(tmp_path, status):
    state, api, e, record, task = setup_assignment(tmp_path)
    if status == 'missing':
        api.tasks.pop(task['id'])
    else:
        task[status] = True
    api.calls.clear()
    e['priority'] = 4
    resync(api, state, e)
    resync(api, sync.SyncState(state.path), e)
    assert record['status'] == 'done'
    assert not any(method == 'POST' for method, *_ in api.calls)


def test_submitted_recurring_assignment_is_not_advanced(tmp_path):
    state, api, e, record, task = setup_assignment(tmp_path)
    task['due'] = {'date': '2026-10-05', 'is_recurring': True, 'string': 'every Monday'}
    owned_reminder = record['reminder_id']
    api.reminders['manual'] = {'id': 'manual', 'task_id': task['id'], 'due': {'date': '2026-10-05T10:00:00Z'}}
    manual = copy.deepcopy(api.reminders['manual'])
    e['submitted'] = True
    api.calls.clear()
    resync(api, state, e)
    assert record['status'] == 'manual'
    assert not any(path.endswith('/close') for _, path, *_ in api.calls)
    assert not task.get('checked')
    assert owned_reminder not in api.reminders
    assert api.reminders['manual'] == manual


def test_reminder_retry_does_not_duplicate_block_or_reset_plan(tmp_path):
    state, api, e, record, task = setup_assignment(tmp_path)
    task['due'] = {'date': '2026-10-04', 'is_recurring': False}
    e['due'] += timedelta(days=1)
    api.fail_reminders = True
    with pytest.raises(RuntimeError, match='Tasks were saved'):
        resync(api, state, e)
    api.fail_reminders = False
    resync(api, sync.SyncState(state.path), e)
    assert task['description'].count(sync.MANAGED_START) == 1
    assert task['due']['date'] == '2026-10-04'
    assert len(api.tasks) == 1


def test_lost_update_response_does_not_duplicate_description(tmp_path):
    state, api, e, record, task = setup_assignment(tmp_path)
    task['description'] += '\nKeep my plan'
    task['due'] = {'date': '2026-10-04', 'is_recurring': False}
    e['description'] = 'Updated Canvas text'
    original_call = api.call

    def lose_response(method, path, *args, **kwargs):
        result = original_call(method, path, *args, **kwargs)
        if method == 'POST' and path == 'tasks/' + task['id']:
            raise RuntimeError('Response lost after successful task write')
        return result

    api.call = lose_response
    with pytest.raises(RuntimeError, match='Response lost'):
        resync(api, state, e)
    api.call = original_call
    resync(api, sync.SyncState(state.path), e)
    assert task['description'].count(sync.MANAGED_START) == 1
    assert task['description'].endswith('\nKeep my plan')
    assert task['due']['date'] == '2026-10-04'


def test_lost_state_save_does_not_duplicate_description(tmp_path, monkeypatch):
    state, api, e, record, task = setup_assignment(tmp_path)
    e['description'] = 'Updated Canvas text'
    monkeypatch.setattr(state, 'save', lambda: (_ for _ in ()).throw(OSError('Disk unavailable')))
    with pytest.raises(OSError):
        resync(api, state, e)
    resync(api, sync.SyncState(state.path), e)
    assert task['description'].count(sync.MANAGED_START) == 1


@pytest.mark.parametrize(('due', 'expected'), [
    ({'date': '2026-10-05T12:00:00', 'timezone': None}, None),
    ({'date': '2026-10-05', 'timezone': 'America/Los_Angeles'}, None),
    ({'date': '2026-10-05T12:00:00', 'timezone': 'America/New_York'}, '2026-10-05T12:00:00-04:00'),
    ({'date': '2026-10-05T12:00:00Z', 'timezone': None}, '2026-10-05T12:00:00+00:00'),
    ({'date': '2026-10-05T12:00:00Z', 'is_recurring': True}, None)])
def test_reminders_require_an_unambiguous_nonrecurring_instant(due, expected):
    instant = sync.planned_instant({'planned_work_due': due})
    assert (instant.isoformat() if instant else None) == expected


def test_moved_study_reminder_refreshes_after_original_slot_has_passed(tmp_path):
    state, api, e = sync.SyncState(tmp_path / 'state.json'), FakeTodoist(), event()
    run(api, state, [e])
    record = next(r for r in state.records.values() if r['kind'] == 'study')
    task = api.tasks[record['task_id']]
    task['due'] = {'date': '2026-10-05T15:00:00Z', 'is_recurring': False}
    run(api, state, [e], now=datetime(2026, 10, 4, tzinfo=UTC))
    assert record['reminder_at'] == '2026-10-05T15:00:00+00:00'


def test_new_preference_cannot_reclassify_manual_move_as_generated(tmp_path):
    state, api, e = sync.SyncState(tmp_path / 'state.json'), FakeTodoist(), event()
    run(api, state, [e])
    record = next(r for r in state.records.values() if r['kind'] == 'study')
    original = record['managed_study_due']
    task = api.tasks[record['task_id']]
    moved = datetime.fromisoformat(original) + timedelta(hours=1)
    task['due'] = {'date': moved.astimezone(UTC).isoformat(), 'is_recurring': False}
    sync.synchronize(api, state, [e], {}, NOW, ZONE, study_time='19:00')
    assert record['managed_study_due'] == original
    assert sync.study_plan_changed(task, record)
    e['due'] += timedelta(days=10)
    sync.synchronize(api, state, [e], {}, NOW, ZONE, study_time='19:00')
    assert record['status'] == 'manual'
    assert task['id'] in api.tasks
    assert record['reminder_at'] == moved.astimezone(UTC).isoformat()


def test_legacy_study_baseline_is_seeded_before_metadata_update(tmp_path):
    state, api, e = sync.SyncState(tmp_path / 'state.json'), FakeTodoist(), event()
    run(api, state, [e])
    record = next(r for r in state.records.values() if r['kind'] == 'study')
    original = record.pop('managed_study_due')
    sync.synchronize(api, state, [e], {}, NOW, ZONE, study_time='19:00')
    assert record['managed_study_due'] == original


def test_protected_retirement_reminder_failure_does_not_block_other_assignments(tmp_path):
    state, api, e = sync.SyncState(tmp_path / 'state.json'), FakeTodoist(), event()
    run(api, state, [e])
    record = next(r for r in state.records.values() if r['kind'] == 'study')
    task = api.tasks[record['task_id']]
    task['due']['is_recurring'] = True
    e['title'] = 'Updated assignment title'
    other = {**event(), 'uid': 'other-assignment', 'course': 'BIO-1111'}
    exclusions = {sync.course_key(e['course'])}
    api.fail_reminders = True
    with pytest.raises(RuntimeError, match='Tasks were saved'):
        sync.synchronize(api, state, [e, other], {}, NOW, ZONE, study_exclusions=exclusions)
    assert record['status'] == 'active'
    parent = api.tasks[state.records['assignment:' + e['uid']]['task_id']]
    assert parent['content'] == 'Updated assignment title'
    assert 'assignment:other-assignment' in state.records
    assert task['id'] in api.tasks
    api.fail_reminders = False
    restored = sync.SyncState(state.path)
    sync.synchronize(api, restored, [e, other], {}, NOW, ZONE, study_exclusions=exclusions)
    saved = next(r for r in restored.records.values() if r['task_id'] == task['id'])
    assert saved['status'] == 'manual'
    assert task['id'] in api.tasks
    assert 'reminder_id' not in saved
