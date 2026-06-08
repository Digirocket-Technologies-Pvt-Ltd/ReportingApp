"""Role & permission layer for the helpdesk / ticketing module.

Everyone signs in with Google (see auth.py). After login we look the
user's email up in the `staff` table to grant a ROLE and (optionally) a
TEAM. The existing PMO admin whitelist (auth.is_pmo_admin) is treated as
a built-in super-admin so the agency account keeps full access without a
staff row.

Roles
-----
  admin       PMO super-user (from PMO_ADMINS). Can do everything,
              including managing staff. Not stored in `staff`.
  supervisor  Shubh / Sunny. See every ticket + dashboard, READ-ONLY
              (purely supervisory, never execute).
  triage      Sidharth / Nikhar / Shweta. Review & approve incoming
              tickets; can also assign. See everything.
  dispatcher  Jasleen / Apoorv. Assign approved tickets to the right
              team member. See everything.
  employee    Team members (seo/content/performance/developer/sales/
              physio...). See ONLY their own assigned tickets; resolve them.
"""
from flask import session, g
from auth import is_authenticated, is_pmo_admin
import db

# Teams an employee can belong to / a ticket can be routed to.
TEAMS = [
    ('seo',          'SEO'),
    ('content',      'Content'),
    ('performance',  'Performance Marketing'),
    ('graphic',      'Graphic Design'),
    ('developer',    'Developer'),
    ('sales',        'Sales'),
    ('physio',       'Physio'),
]
TEAM_KEYS = [k for k, _ in TEAMS]
TEAM_LABELS = dict(TEAMS)

ROLES = ['admin', 'supervisor', 'triage', 'dispatcher', 'employee']

# Friendly display names for the roles (internal keys stay the same).
ROLE_LABELS = {
    'admin':      'Admin',
    'supervisor': 'Founder',
    'triage':     'PMO',
    'dispatcher': 'Assigner',
    'employee':   'Employee',
}


def role_label(r):
    return ROLE_LABELS.get(r, (r or '').capitalize())

# The 6-stage roadmap (order matters - drives the Amazon-style tracker).
STAGES = [
    ('raised',      'Raised'),
    ('reviewed',    'Reviewed'),
    ('assigned',    'Assigned'),
    ('in_progress', 'In progress'),
    ('resolved',    'Resolved'),
    ('closed',      'Closed'),
]
STAGE_KEYS = [k for k, _ in STAGES]
STAGE_LABELS = dict(STAGES)

PRIORITIES = ['low', 'medium', 'high', 'urgent']


def current_staff():
    """The logged-in user's staff profile as a dict, or None if they are
    not recognised as internal staff. PMO admins get a synthetic
    {'role': 'admin'} profile even without a staff row.

    Cached per-request on flask.g: every permission helper (role(),
    can_see_all_tickets(), can_review() ...) funnels through here, and the
    templates call them 10-20x per page. Without this cache that meant
    10-20 Supabase round-trips per page just to resolve the role — the
    single biggest cause of slow page loads. Now it's ONE lookup per request."""
    if not is_authenticated():
        return None
    # Request-scoped memo (safe: the session email can't change mid-request).
    have_ctx = True
    try:
        if getattr(g, '_staff_cached', False):
            return g._staff_value
    except RuntimeError:
        have_ctx = False  # no app/request context (e.g. a CLI script)

    def _compute():
        email = (session.get('user_email') or '').strip().lower()
        if not email:
            return None
        # PMO super-admin: full access regardless of the staff table.
        if is_pmo_admin():
            row = db.get_staff_by_email(email) or {}
            return {
                'email': email,
                'name': row.get('name') or session.get('user_name') or email,
                'role': 'admin',
                'team': row.get('team'),
                'active': True,
            }
        row = db.get_staff_by_email(email)
        if not row or not row.get('active', True):
            return None
        return row

    value = _compute()
    if have_ctx:
        try:
            g._staff_cached = True
            g._staff_value = value
        except RuntimeError:
            pass
    return value


def role():
    s = current_staff()
    return s['role'] if s else None


def my_team():
    s = current_staff()
    return s.get('team') if s else None


def my_email():
    return (session.get('user_email') or '').strip().lower()


# ---------------- Permission predicates ----------------
def is_staff():
    """Any recognised internal user (not a client / stranger)."""
    return current_staff() is not None


def can_see_all_tickets():
    """Supervisors, triage, dispatch and admins see every ticket."""
    return role() in ('admin', 'supervisor', 'triage', 'dispatcher')


def is_readonly():
    """Supervisors watch everything but never execute."""
    return role() == 'supervisor'


def can_review():
    """Approve / reject an incoming (raised) ticket."""
    return role() in ('admin', 'triage')


def can_assign():
    """Assign an approved ticket to a team member."""
    return role() in ('admin', 'triage', 'dispatcher')


def can_manage_staff():
    """Add / edit staff and their roles. Admin + supervisors + triage."""
    return role() in ('admin', 'supervisor', 'triage')


def can_create_ticket():
    """Raise a NEW ticket from the staff board. Only PMO/triage (and admin).
    Employees, dispatchers and supervisors never create tickets — those come
    from clients (portal) or PMO. Clients raise their own from the portal."""
    return role() in ('admin', 'triage')


def is_assignee(ticket):
    """True if the current user is any assignee of this ticket — the primary
    `assigned_to`, or anyone in the `assignees` list (multi-assign / @mention).
    Routes attach `ticket['assignees']` (list of emails) before checking."""
    if not ticket:
        return False
    me = my_email()
    if (ticket.get('assigned_to') or '').strip().lower() == me:
        return True
    return me in [(a or '').strip().lower() for a in (ticket.get('assignees') or [])]


def can_work_ticket(ticket):
    """Start / resolve a ticket: the assigned employee, or an admin."""
    return role() == 'admin' or is_assignee(ticket)


def can_view_ticket(ticket):
    """Can the current user open this specific ticket?"""
    if can_see_all_tickets():
        return True
    if is_assignee(ticket):
        return True
    # The client who raised it can view their own (handled in the portal).
    return False


def team_label(key):
    return TEAM_LABELS.get(key, key or '—')


def stage_index(status):
    """Position of a status in the roadmap (closed counts as fully done)."""
    try:
        return STAGE_KEYS.index(status)
    except ValueError:
        return 0
