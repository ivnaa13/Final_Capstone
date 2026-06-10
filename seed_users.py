import os, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.contrib.auth.models import User
from employee.models import Employee, Profile, Attendance
from django.utils import timezone
from datetime import datetime
from django.db import connection

print("=" * 50)
print("STEP 1: Link Employee → User → Profile")
print("=" * 50)

linked   = 0
skipped  = 0
conflict = 0

for emp in Employee.objects.all():
    if Profile.objects.filter(employee=emp).exists():
        skipped += 1
        continue

    user, is_new = User.objects.get_or_create(
        username=emp.employee_id,
        defaults={'first_name': emp.full_name}
    )
    if is_new:
        user.set_password('garuda123')
        user.save()

    profile, _ = Profile.objects.get_or_create(user=user)
    if profile.employee is not None and profile.employee_id != emp.pk:
        conflict += 1
        print(f"⚠️  Conflict: user={user.username} sudah tautkan ke emp_pk={profile.employee_id}, skip emp_pk={emp.pk}")
        continue

    Profile.objects.filter(pk=profile.pk).update(
        employee   = emp,
        role       = 'employee',
        department = emp.organization,
        join_date  = emp.join_date,
    )
    linked += 1

print(f"Total Employee : {Employee.objects.count()}")
print(f"Total User     : {User.objects.count()}")
print(f"Total Profile  : {Profile.objects.count()}")
print(f"✅ Linked      : {linked}")
print(f"⏭  Skipped     : {skipped} (sudah terhubung)")
print(f"⚠️  Conflict    : {conflict}")

print("\n" + "=" * 50)
print("STEP 2: Migrate tabel attendance → employee_attendance")
print("=" * 50)

STATUS_MAP = {
    'Present':  'present',
    'Late':     'late',
    'Absent':   'absent',
    'Leave':    'leave',
    'Overtime': 'overtime',
}

with connection.cursor() as cursor:
    cursor.execute("""
        SELECT id, employee_id, attendance_date, check_in, check_out,
               working_hours, attendance_status
        FROM attendance
        ORDER BY attendance_date, employee_id
    """)
    rows = cursor.fetchall()

print(f"Total data legacy: {len(rows)}")

created = skipped_att = no_user = no_emp = 0

for row in rows:
    _, emp_id, att_date, check_in, check_out, working_hours, status = row

    try:
        emp = Employee.objects.get(pk=emp_id)
    except Employee.DoesNotExist:
        print(f"❌ Employee pk={emp_id} tidak ditemukan")
        no_emp += 1
        continue

    profile = getattr(emp, 'profile', None)
    if not profile:
        print(f"⚠️  No profile: {emp.full_name} (pk={emp_id})")
        no_user += 1
        continue

    user = profile.user

    if Attendance.objects.filter(user=user, date=att_date).exists():
        skipped_att += 1
        continue

    ci_dt = co_dt = None
    if check_in:
        ci_dt = timezone.make_aware(datetime.combine(att_date, check_in))
    if check_out:
        co_dt = timezone.make_aware(datetime.combine(att_date, check_out))

    mapped = STATUS_MAP.get((status or 'present').strip(), 'present')

    Attendance.objects.create(
        user              = user,
        employee          = emp,
        date              = att_date,
        check_in          = ci_dt,
        check_out         = co_dt,
        check_in_address  = '',
        check_out_address = '',
        status            = mapped,
        approval_status   = 'approved',
    )
    created += 1

print(f"\n=== HASIL ===")
print(f"✅ Created     : {created}")
print(f"⏭  Skipped     : {skipped_att} (sudah ada)")
print(f"⚠️  No profile  : {no_user}")
print(f"❌ No employee : {no_emp}")
print(f"\nTotal attendance sekarang: {Attendance.objects.count()}")