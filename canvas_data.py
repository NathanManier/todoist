"""Read-only Canvas enrichment. Calendar presence is never submission evidence."""

import math
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import requests


class CanvasError(RuntimeError):
    pass


@dataclass(frozen=True)
class Impact:
    percent: float | None
    explanation: str


def number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def counts_for_grade(assignment):
    return not (
        assignment.get('published') is False
        or assignment.get('omit_from_final_grade')
        or assignment.get('grading_type') == 'not_graded'
        or (assignment.get('submission') or {}).get('excused')
    )


def grade_impact(assignment, course, groups, assignments):
    """Nominal weight from all visible assignments, never just calendar events."""
    if not counts_for_grade(assignment):
        return Impact(0, 'Excluded, ungraded, unpublished, or excused in Canvas.')
    points = number(assignment.get('points_possible'))
    if points is None or points <= 0:
        return Impact(None, 'No positive point value; extra credit cannot be assigned a fixed weight.')
    if course.get('grading_periods') or any(
        e.get('multiple_grading_periods_enabled') for e in (course.get('enrollments') or [])
    ):
        return Impact(None, 'Multiple grading periods require additional weighting information.')
    weighted = course.get('apply_assignment_group_weights')
    if not isinstance(weighted, bool):
        return Impact(None, 'Canvas did not provide the course weighting setting.')
    relevant = [a for a in assignments if counts_for_grade(a)]
    relevant_groups = groups
    multiplier = 100
    formula_prefix = ''
    if weighted:
        group = next((g for g in groups if str(g['id']) == str(assignment.get('assignment_group_id'))), None)
        if group is None:
            return Impact(None, 'Assignment grading category is unavailable.')
        weights = [number(g.get('group_weight')) for g in groups]
        if any(w is None or w < 0 for w in weights) or not math.isclose(sum(weights), 100, abs_tol=0.01):
            return Impact(None, 'Category weights do not total 100%; a fixed final-grade share is ambiguous.')
        multiplier = number(group.get('group_weight'))
        if multiplier == 0:
            return Impact(0, 'This grading category has zero weight.')
        relevant = [a for a in relevant if str(a.get('assignment_group_id')) == str(group['id'])]
        relevant_groups = [group]
        formula_prefix = f'{multiplier:g}% category weight × '
    if any((g.get('rules') or {}).get('drop_lowest', 0) or
           (g.get('rules') or {}).get('drop_highest', 0) for g in relevant_groups):
        return Impact(None, 'Dropped-score rules make the effective weight depend on results.')
    point_values = [number(a.get('points_possible')) for a in relevant]
    if not point_values or any(p is None or p < 0 for p in point_values):
        return Impact(None, 'The visible assignment point totals are incomplete.')
    total = sum(point_values)
    if total <= 0:
        return Impact(None, 'No positive total points are available.')
    share = multiplier * points / total
    suffix = '' if weighted else ' × 100%'
    return Impact(share, f'{formula_prefix}{points:g} / {total:g} points{suffix}. '
                  'Estimate based on currently visible Canvas assignments; future assignments or grading changes can change it.')


def assessment_type(title, assignment=None):
    if re.search(r'\b(exams?|tests?|midterms?|finals?)\b', title, re.I):
        # Avoid treating "final project" / "final draft" as an exam.
        if not re.search(r'\bfinal\s+(project|paper|draft|essay|report|presentation)\b', title, re.I):
            return 'Exam'
    if re.search(r'\bquizz?(?:es)?\b', title, re.I):
        return 'Quiz'
    assignment = assignment or {}
    if 'online_quiz' in assignment.get('submission_types', []) or assignment.get('is_quiz_assignment'):
        return 'Quiz'
    return None


def confirmed_submitted(assignment):
    sub = (assignment or {}).get('submission') or {}
    if sub.get('excused'):
        return True
    if sub.get('workflow_state') in ('submitted', 'pending_review', 'graded') and sub.get('submitted_at'):
        return True
    return (sub.get('workflow_state') == 'graded' and sub.get('missing') is False
            and sub.get('score') is not None)


def current_grade(course):
    if course.get('hide_final_grades'):
        return None
    for enrollment in course.get('enrollments') or []:
        if enrollment.get('type') not in ('student', 'StudentEnrollment'):
            continue
        # The course endpoint uses computed_current_score; enrollment objects
        # from other Canvas endpoints can instead contain grades.current_score.
        value = enrollment.get('computed_current_score')
        if value is None:
            value = (enrollment.get('grades') or {}).get('current_score')
        if value is not None:
            return number(value)
    return None


class CanvasClient:
    def __init__(self, base_url, token, session=None):
        parsed = urlparse(base_url)
        if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password:
            raise CanvasError('CANVAS_BASE_URL must be an HTTPS Canvas site URL.')
        self.base_url = f'{parsed.scheme}://{parsed.netloc}'
        self.session = session or requests.Session()
        self.session.headers.update({'Authorization': f'Bearer {token}'})

    def list(self, path, params=None):
        url = self.base_url + '/api/v1/' + path.lstrip('/')
        result, seen = [], set()
        query = {'per_page': 100, **(params or {})}
        while url:
            if url in seen or not url.startswith(self.base_url + '/api/v1/'):
                raise CanvasError('Canvas returned an invalid pagination link.')
            seen.add(url)
            try:
                response = self.session.get(url, params=query, timeout=30, allow_redirects=False)
                if response.status_code != 200:
                    raise CanvasError(f'Canvas read failed (HTTP {response.status_code}); no tasks were changed.')
                page = response.json()
                if not isinstance(page, list):
                    raise CanvasError('Canvas returned an invalid list response.')
            except requests.RequestException:
                raise CanvasError('Canvas could not be reached; no tasks were changed.') from None
            result.extend(page)
            next_url = response.links.get('next', {}).get('url')
            url = urljoin(url, next_url) if next_url else None
            query = None
        return result

    def snapshot(self):
        courses = self.list('courses', {'enrollment_type': 'student', 'enrollment_state': 'active',
                                       'include[]': ['total_scores', 'current_grading_period_scores']})
        data = {}
        for course in courses:
            if course.get('workflow_state') not in (None, 'available'):
                continue
            cid = str(course['id'])
            assignments = self.list(f'courses/{cid}/assignments', {'include[]': 'submission'})
            groups = self.list(f'courses/{cid}/assignment_groups')
            data[cid] = {'course': course, 'assignments': assignments, 'groups': groups,
                         'grade_url': f'{self.base_url}/courses/{cid}/grades'}
        return data


def match_assignment(event, snapshot):
    """Match only IDs from a Canvas link or UID, never similar assignment names."""
    link = re.search(r'/courses/(\d+)/assignments/(\d+)', event.get('url', '') + ' ' + event.get('description', ''))
    aid, cid = None, None
    if link:
        cid, aid = link.groups()
    else:
        match = re.search(r'assignment[-_](\d+)', event['uid'], re.I)
        if match:
            aid = match.group(1)
    for course_id, context in snapshot.items():
        if cid and cid != course_id:
            continue
        for assignment in context['assignments']:
            if aid == str(assignment['id']):
                return assignment, context
    return None, None
