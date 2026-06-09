"""One-shot (re-runnable) staff seeder for the helpdesk module.

Run:  ./venv/Scripts/python.exe seed_staff.py
Idempotent: uses upsert by email, so running again just updates roles/teams.

Role mapping
------------
  supervisor : founders — monitor everything, read-only (Shubh, Sunny)
  triage     : PMO — full visibility, review/approve, manage staff (Nikhar, Sidharth, Shweta)
  dispatcher : assigns tickets to employees (Jasleen; Apoorv pending email)
  employee   : team members who resolve their own assigned tickets
"""
from dotenv import load_dotenv
load_dotenv()
import db

# (email, name, role, team)
STAFF = [
    # ---- Founders / supervisors (read-only, watch all) ----
    ('shubhranshu.srivastava@digirockettechnologies.com', 'Shubhranshu Srivastava', 'supervisor', None),
    ('sunny.kumar@digirockettechnologies.com',            'Sunny Kumar',            'supervisor', None),

    # ---- PMO / triage (all access: see all, approve, manage staff) ----
    ('nikhar.makkar@digirockettechnologies.com',  'Nikhar Makkar',  'triage', None),
    ('sidharth.anant@digirockettechnologies.com', 'Sidharth Anant', 'triage', None),
    ('shweta.singh@digirockettechnologies.com',   'Shweta Singh',   'triage', None),

    # ---- Dispatch (both distribute tickets to employees) ----
    ('jasleen.kalra@digirockettechnologies.com',   'Jasleen Kalra',  'dispatcher', None),
    ('apoorv.dwivedi@digirockettechnologies.com',  'Apoorv Dwivedi', 'dispatcher', None),

    # ---- Performance Marketing ----
    ('shashwat.srivastava@digirockettechnologies.com', 'Shashwat Srivastava', 'employee', 'performance'),
    ('anurag.sharma@digirockettechnologies.com',       'Anurag Sharma',       'employee', 'performance'),

    # ---- SEO ----
    ('navneet.raj@digirockettechnologies.com',   'Navneet Raj',   'employee', 'seo'),
    ('mahesh.gajai@digirockettechnologies.com',  'Mahesh Gajai',  'employee', 'seo'),
    ('sandeep.kumar@digirockettechnologies.com', 'Sandeep Kumar', 'employee', 'seo'),
    ('kajal.singh@digirockettechnologies.com',   'Kajal Singh',   'employee', 'seo'),

    # ---- Sales ----
    ('suraj.kumar@digirockettechnologies.com',   'Suraj Kumar',   'employee', 'sales'),
    ('keshav.pundir@digirockettechnologies.com', 'Keshav Pundir', 'employee', 'sales'),
    ('yash.m@digirockettechnologies.com',        'Yash Meena',    'employee', 'sales'),

    # ---- Graphic Design ----
    ('pratyaksh.srivastava@digirockettechnologies.com', 'Pratyaksh Srivastava', 'employee', 'graphic'),

    # ---- Development ----
    ('akib@digirockettechnologies.com',         'Akib Mirza',    'employee', 'developer'),
    ('ritik.sharma@digirockettechnologies.com', 'Ritik Sharma',  'employee', 'developer'),
    ('ritika.singh@digirockettechnologies.com', 'Ritika Singh',  'employee', 'developer'),  # Shopify developer
]

if __name__ == '__main__':
    if not db.is_configured():
        raise SystemExit('Supabase not configured (check .env SUPABASE_URL / SUPABASE_KEY).')
    ok, fail = 0, 0
    for email, name, role, team in STAFF:
        row = db.upsert_staff(email, name=name, role=role, team=team)
        if row:
            ok += 1
            print(f'  [ok]   {role:11} {team or "-":12} {email}')
        else:
            fail += 1
            print(f'  [FAIL] {email}')
    print(f'\nDone: {ok} upserted, {fail} failed, {len(STAFF)} total.')
