# Canvas to Todoist Sync

Sync Canvas assignments into Todoist, see their estimated grade impact, and
schedule a week of study tasks before tests and quizzes.

## What appears in Todoist

- **Canvas Assignments:** assignment tasks with the Canvas deadline, a stable
  course label, a Canvas link, and the estimated percentage of the final grade
  in the description when the available grading data supports it.
- **Study tasks:** one task at **6 PM America/Los_Angeles** on each of the
  **seven calendar days before** an exam or quiz, each with a push reminder.
  If an assessment is discovered late, only the remaining future sessions are
  scheduled. There is no study session on the assessment's due date.
- **Canvas Grades:** one undated summary task per active course. Its title shows
  the current grade supplied by Canvas; the description explains the scope and
  shows the grading-category weights and the last checked date.
- **Labels:** the course code, `Exam`, `Quiz`, `Study`, `Grade`, and
  `High_grade_impact` (estimated weight at least 5%). Grades do not change course
  label names, so saved filters remain stable.

### Deadlines and planned work dates

Canvas owns the **deadline**. You own Todoist's **date/time** (when you plan to
work). New assignment tasks have a deadline and start without a planned work
date. Existing tasks keep their date, time, duration, and recurrence. Sync never
sends a `due`, `due_date`, `due_datetime`, or other `due_*` field when updating a
task, including when its title, priority, grade context, or Canvas deadline changes.
Existing dates that were previously copied from Canvas are left in place too:
there is no reliable way to distinguish those from dates you chose yourself.
This prevents future resets; it cannot recover plans erased by earlier runs.

Todoist's `deadline_date` stores the Canvas deadline's calendar day in
`STUDY_TIMEZONE`. Todoist deadlines have no time component, so the exact Canvas
cutoff, explicit UTC offset/timezone, source, and assignment link are kept near
the top of the description. All-day calendar entries are explicitly marked as
date-only; the sync does not invent an exact cutoff for them. Fixed deadlines
can coexist with recurring work dates.

Generated descriptions use `[Canvas sync]` / `[/Canvas sync]` markers. Add your
notes outside that block. Upgrades preserve the entire old description because
it may contain planning notes; a new managed block is appended once. If you
edit inside the block, that edited text is preserved and a fresh block is added
on the next source update. An overfull description stops the update rather than
silently truncating your notes. Retries recognize a block already written.

New study tasks still receive the configured initial time. Once created, their
date/time is also left alone. Changing `STUDY_TIME` affects new sessions, not
existing plans. Manually moved, undated, all-day, or recurring study sessions
are protected from automatic deletion when a deadline changes, a course is
excluded, or the parent completes. Such obsolete sessions are handed back to
you and are no longer managed or recreated by the sync. Recurring assignment
tasks are not automatically completed, since doing so would advance their
recurrence.

Assignment reminders continue to use the Canvas cutoff. Study reminders follow
the task's current explicit non-recurring time, even after its original slot
has passed. For undated, all-day, recurring, or floating times without a known
timezone, only the sync-owned absolute reminder is removed; no time is guessed.
Your other reminders are left alone. Set a Todoist reminder yourself for these
plans if needed. A protected session handed back to you keeps its owned reminder
aligned with its explicit current plan at that handoff; later edits are yours.

An illustrative assignment description, using fictional data:

```text
Course: COMS-1102-V13-2268
Estimated share of final grade: 0.14%
20% category weight × 2 / 280 points.
Estimate based on currently visible Canvas assignments; future assignments
or grading changes can change it.
Points possible: 2
```

The calendar-only connection still works without a Canvas API token. It can
classify tests/quizzes by title and create study tasks, but it cannot provide
grades, grade weights, or verified submission completion.

## Upgrade an existing installation

1. **Keep the existing sync state and caches.** The new version migrates the old
   `synced_events` state automatically and updates existing task IDs. Do not
   delete caches to fix duplicates.
2. In Canvas, open **Account → Settings → New Access Token** (under Approved
   Integrations, when your institution allows personal tokens). Give it a
   descriptive name and an appropriate expiration date. Copy it directly into
   a new GitHub Actions repository secret named **`CANVAS_API_TOKEN`**. Do not
   paste tokens into issues, PRs, chat, or committed files.
3. Keep the existing **`CANVAS_ICS_URL`** and **`TODOIST_API_TOKEN`** secrets.
   The Canvas site is inferred from the feed URL. If needed, set repository
   variable **`CANVAS_BASE_URL`** to the HTTPS root of your Canvas site.
4. After the code is on the default branch, use **Actions → Canvas to Todoist
   Sync → Run workflow** with **dry_run checked** for a read-only check. Logs
   contain counts and errors, not your grades or assignment titles.
5. Run with **dry_run unchecked** to apply the changes. Leave
   **initialize_state unchecked** when upgrading.

Pushing sync code to `master` also starts a live run, as in the original
workflow. Configure the new secret before merging when you want grade features
available immediately. A feature branch or pull request runs only the test
workflow; it does not activate the live sync.

If your institution prevents personal API tokens, leave the new secret unset.
Calendar syncing and title-based study tasks remain available; the script does
not attempt to bypass Canvas permissions.
Cal Poly students must use the university's
[API token request process](https://canvassupport.calpoly.edu/user-token-information-and-request).

## First-time setup

1. Fork or clone this repository.
2. Copy your private feed URL from **Canvas → Calendar → Calendar Feed**.
3. Get a Todoist API token from **Settings → Integrations → Developer**.
4. Add the `CANVAS_ICS_URL` and `TODOIST_API_TOKEN` GitHub Actions secrets, plus
   `CANVAS_API_TOKEN` for grades and confirmed submission status.
5. Enable Actions. Run the workflow once with **dry_run unchecked** and
   **initialize_state checked**. Only use initialization when there is no
   previous synced task history to preserve.

## Configuration

Use GitHub Actions **secrets** for tokens and the private calendar URL. Use
repository **variables** for these nonsecret preferences, or the equivalent
environment variables when running locally:

- `STUDY_DAYS_BEFORE`: `7`; range 0–30. Zero disables new study sessions and
  removes future sessions owned by this sync.
- `STUDY_TIME`: `18:00` in 24-hour HH:MM form.
- `STUDY_TIMEZONE`: `America/Los_Angeles`; use an IANA zone so daylight-saving
  changes are handled correctly.
- `STUDY_EXCLUDED_COURSES`: comma-separated course codes or names, such as
  `COMS-1102-V13-2268,Public Speaking`. Matching ignores case, spaces, and
  punctuation, but requires the full code or name. Excluded courses keep their
  assignment deadlines and grade summaries. No study tasks are created for
  them, and active study tasks previously created by this sync are removed,
  including overdue sessions, unless you changed their work date/recurrence.
  Completed sessions and manually created tasks are preserved. Add a new code
  if the course code changes in another term.
- `SYNC_COURSE_GRADES`: `true`; set `false` to stop updating summary tasks.
  Existing summaries stay in place with their last checked date.
- `REMINDER_DAYS_BEFORE`: `1`; set `0` to disable the assignment deadline
  reminder. This is separate from daily study reminders.
- `CANVAS_BASE_URL`: optional HTTPS Canvas site root; otherwise inferred.
- Local-only `TODOIST_PROJECT_NAME`: `Canvas Assignments` by default. The
  workflow keeps that existing project name; change its environment entry to
  customize it. Grade summaries use the separate `Canvas Grades` project.
- `STATE_FILE`: `sync_state.json` for local runs. The Actions cache expects the
  default filename.

Todoist push notifications must be enabled on your device, and your account
must permit reminders. A failed reminder leaves its task saved and is retried
on the next sync. The run reports failure instead of claiming all notifications
were configured. Other assignments and grade summaries still get processed.
Your Todoist account must also support deadlines; an API rejection is reported
rather than falling back to overwriting your planned work date.

Useful Todoist filter queries you can save manually:

```text
@Study & (today | overdue)
(@Exam | @Quiz) & !@Study & 7 days
@High_grade_impact & !@Study
@Grade
@COMS-1102-V13-2268
```

The integration creates labels; it does not modify your existing saved filters.

## What the numbers mean

The calendar feed does not include enough information to calculate final-grade
weights. With the API connected, the script reads **all visible assignments**
in each active course, not just upcoming calendar events, and the assignment
group weights and current user's submission status.

- **Weighted course:** category weight × assignment points ÷ total eligible
  points in that category.
- **Points-based course:** assignment points ÷ total eligible course points ×
  100%.
- Omitted, ungraded, unpublished, and excused assignments are excluded.
- Dropped-score rules, multiple grading periods, missing points/weights,
  nonstandard category-weight totals, and zero-point extra credit can prevent
  a defensible fixed percentage. These show an explanation instead of an
  invented number.
- Even a calculated weight is an **estimate based on the assignments visible
  to you now**. Future assignments, hidden work, instructor adjustments, and
  syllabus rules outside Canvas can change the final contribution.
- A course summary displays Canvas's **current grade**, which may exclude
  ungraded assignments. It is not a projected final grade. Hidden/unavailable
  grades show **Grade unavailable**, never an assumed zero.

## Completion, retries, and scheduling

Calendar disappearance does **not** prove an assignment was submitted. Tasks
are auto-completed only when Canvas's API confirms the current user's
submission or excusal. Missing-work automatic zeroes are not treated as proof
of submission. Without the API, complete tasks manually in Todoist.

Completed or manually deleted tasks are not recreated. Untouched future study
tasks are removed when the parent assignment is completed, or when a changed
deadline makes those sessions obsolete. Manually rescheduled or recurring
sessions are preserved as described above. Study tasks cancelled by the sync
can return if the assessment moves back; manually completed tasks stay done.

Exact Canvas cutoffs are retained with explicit time zones, and all-day calendar
entries stay on their original date. Priorities are recalculated each run (within 1 day P1,
within 3 days P2, within 7 days P3, later P4).

The workflow requests a run every five minutes, but GitHub schedules can be
delayed. Study reminders are created in advance in Todoist and do not require
a workflow to run at the exact reminder time.

State is saved atomically after each task write and cached even after partial
failure. Runs do not overlap. Stable request IDs protect creation retries;
the persistent state remains necessary because API idempotency is not an
unlimited replacement for history. If state is missing or corrupt, the live
sync stops instead of assuming a fresh account and creating duplicates.

The state contains task IDs, source assignment identifiers, Canvas deadline
dates/exact cutoffs/source, observed planned dates and recurrence flags, original
generated study times, labels, and change hashes, not grade scores, descriptions,
or API tokens. These fields extend version 2 in place; legacy records are
migrated without replacing task IDs. GitHub caches are not a
secret store. Use a private repository if course labels or scheduling metadata
must also remain private. Detailed previews stay local and are never uploaded
by the workflow.

## Local development

Python 3.11 or later is required.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt pytest
cp .env.example .env
# Edit .env; quote values containing spaces. Then load the trusted local file:
set -a
source .env
set +a
python sync.py --dry-run --preview-file preview.json
python -m pytest -q
```

For an intentional first live sync only, use `python sync.py --initialize-state`.
After that, use `python sync.py`. `--dry-run` never contacts Todoist or writes
state. The optional preview contains private assignment/grade information;
keep it out of Git and public logs.

Tests use synthetic Canvas data and an in-memory Todoist fake. A network-blocking
fixture prevents accidental live API requests. The regression suite covers
priority/title/deadline updates, manual notes, recurring and completed tasks,
legacy state, retries after uncertain writes, local dates/DST, and study reminders.

### Review and rollback

Keep this change on a feature branch until reviewed. Its test workflow has no
Canvas/Todoist secrets; the live workflow remains on `master`. Merging sync code
to `master` starts a live sync, so review that transition separately. Preserve
the existing state cache; do not initialize a new state over existing tasks.

Code changes can be reverted with Git. Reverting to the previous sync also
restores its planned-date reset behavior. If rolling back, stop the scheduled
live sync before running that older version against your plans. A code revert
does not undo task changes already applied by a live run. No rollback, deployment,
workflow change, or live task repair is performed by the offline tests.

## Troubleshooting

- **Missing state:** restore the latest `sync-state-` cache or your local
  backup. Do not initialize over existing tasks unless you have deliberately
  reconciled them first.
- **Canvas 401/403:** renew the Canvas API token and check institutional
  permissions. An API error stops the run before Todoist changes.
- **Reminder failures:** check Todoist reminder access and the device's push
  settings, then rerun. Existing tasks are kept.
- **No grade percentage:** read the explanation in the task. Some course
  grading structures cannot be represented by one reliable fixed percentage.
- **Incorrect assessment detection:** titles containing quiz/test/exam/midterm
  or final are classified automatically; `final project/paper/draft/essay/report/
  presentation` are excluded from title-based exam detection. Canvas online
  quizzes are also recognized through their API metadata.
  Run logs show aggregate counts of detected assessments, course exclusions,
  and planned future study sessions, without exposing assignment titles.

## API references

- [Canvas assignment groups and weighting](https://developerdocs.instructure.com/services/canvas/resources/assignment_groups)
- [Canvas assignments and current-user submissions](https://developerdocs.instructure.com/services/canvas/resources/assignments)
- [Canvas courses and total scores](https://developerdocs.instructure.com/services/canvas/resources/courses)
- [Todoist API v1: tasks, reminders, and pagination](https://developer.todoist.com/api/v1/)

## License

MIT, as stated by the original template.

