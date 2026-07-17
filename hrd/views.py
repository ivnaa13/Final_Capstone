import json
from datetime import date, timedelta, datetime
from decimal import Decimal
from functools import wraps
from collections import defaultdict
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from types import SimpleNamespace
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q, Count, Sum
from django.http import JsonResponse, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from employee.models import Attendance, Employee, Holiday, Leave, Payroll, PayrollApproval, Profile

OT_WEEKDAY_RATE  = Decimal('35000')    # lembur > 10 jam di hari kerja
OT_DAYOFF_RATE   = Decimal('100000')   # lembur di hari libur/weekend, minimal 5 jam kerja
OT_HOLIDAY_RATE  = Decimal('200000')   # lembur di hari raya/libur nasional, minimal 5 jam kerja

WORKING_DAYS_PER_WEEK  = 5           
STANDARD_WORK_MINUTES  = 9 * 60   
OVERTIME_THRESHOLD     = 10 * 60     
DAYOFF_MIN_MINUTES     = 5 * 60      

STANDARD_CHECK_IN_HOUR  = 8     
LATE_TOLERANCE_MINUTES  = 0            
LATE_PENALTY_PER_MINUTE = Decimal('0')   

VALID_PAYROLL_STATUSES = [
    'Pending', 'Draft', 'Waiting Supervisor',
    'Supervisor Approved', 'Supervisor Rejected',
    'Approved', 'Reject', 'Done',
]

PAYROLL_CUTOFF_DAY = 20   


def hrd_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('login')
        if not hasattr(request.user, 'profile') or request.user.profile.role != 'hrd':
            messages.error(request, "Akses ditolak. Halaman ini hanya untuk HRD.")
            return redirect('login')
        return view_func(request, *args, **kwargs)
    return wrapper


def supervisor_required(view_func):
    """
    Sama seperti hrd_required, tapi untuk role 'supervisor' (Atasan) —
    dipakai untuk endpoint approval payroll level 1.
    """
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('login')
        if not hasattr(request.user, 'profile') or request.user.profile.role != 'supervisor':
            messages.error(request, "Akses ditolak. Halaman ini hanya untuk Atasan/Supervisor.")
            return redirect('login')
        return view_func(request, *args, **kwargs)
    return wrapper

def _payroll_range(year: int, month: int):
    
    end = date(year, month, PAYROLL_CUTOFF_DAY)

    if month == 1:
        prev_year, prev_month = year - 1, 12
    else:
        prev_year, prev_month = year, month - 1

    start = date(prev_year, prev_month, PAYROLL_CUTOFF_DAY + 1)
    return start, end


def _available_periods():
    MONTH_ID = [
        '', 'Januari', 'Februari', 'Maret', 'April', 'Mei', 'Juni',
        'Juli', 'Agustus', 'September', 'Oktober', 'November', 'Desember'
    ]
    today   = timezone.localdate()
    periods = []

    for i in range(11, -1, -1):
        raw_month = today.month - i
        if raw_month <= 0:
            y = today.year - 1
            m = raw_month + 12
        else:
            y = today.year
            m = raw_month

        start, end = _payroll_range(y, m)
        periods.append({
            'year':          y,
            'month':         m,
            'label':         f"{MONTH_ID[m]} {y}",
            'is_complete':   today > end,
            'payroll_start': start.strftime('%d %b'),
            'payroll_end':   end.strftime('%d %b %Y'),
            'selected':      (y == today.year and m == today.month),
        })

    return periods


def _duration_minutes(check_in, check_out) -> int:
    """Hitung selisih menit antara dua DateTimeField atau TimeField."""
    import datetime as dt_module

    if isinstance(check_in, dt_module.datetime) and isinstance(check_out, dt_module.datetime):
        total = int((check_out - check_in).total_seconds() / 60)
        return max(0, total)

    dummy = date(2000, 1, 1)
    ci    = datetime.combine(dummy, check_in)
    co    = datetime.combine(dummy, check_out)
    total = int((co - ci).total_seconds() / 60)
    return max(0, total)


def _get_day_type(check_date: date, holiday_dates: set | None = None) -> str:
  
    if holiday_dates is not None:
        is_holiday = check_date in holiday_dates
    else:
        is_holiday = Holiday.objects.filter(date=check_date).exists()

    if is_holiday:
        return 'holiday'
    if check_date.weekday() >= 5:
        return 'weekend'
    return 'workday'


def _is_dayoff(check_date: date) -> bool:
    """
    Backward-compat helper: True jika tanggal BUKAN hari kerja normal
    (weekend ATAUPUN hari raya/libur nasional).
    """
    return _get_day_type(check_date) != 'workday'


def _calc_attendance_summary(employee_pk: int, start_date: date, end_date: date) -> dict:
    """
    Rekap absensi satu karyawan dalam periode cut-off.
    Return dict berisi semua komponen yang dibutuhkan untuk payroll.
    """
    records = Attendance.objects.filter(
        employee_id=employee_pk,
        date__range=(start_date, end_date),
    )

    total_days = (end_date - start_date).days + 1

    # Query semua tanggal holiday dalam periode ini SEKALI di awal,
    # supaya _get_day_type() tidak query ke DB berulang kali di dalam loop.
    holiday_dates = set(
        Holiday.objects.filter(date__range=(start_date, end_date))
        .values_list('date', flat=True)
    )

    # working_days = hari kerja EFEKTIF: Senin-Jumat DAN bukan hari raya.
    # Kalau hari raya jatuh di hari kerja, hari itu tidak dihitung sebagai
    # hari kerja wajib (karyawan tidak wajib masuk).
    working_days = sum(
        1 for d in (start_date + timedelta(n) for n in range(total_days))
        if _get_day_type(d, holiday_dates) == 'workday'
    )

    present_days    = 0
    leave_days      = 0   # izin/sakit resmi (status='leave') — TIDAK memotong gaji, dipisah dari absent
    late_count      = 0
    total_late_min  = 0
    ot_weekday_sess = 0
    ot_dayoff_sess  = 0
    ot_holiday_sess = 0

    for rec in records:
        status = (rec.status or '').lower()
        dtype  = _get_day_type(rec.date, holiday_dates)

        if dtype in ('weekend', 'holiday'):
            # Lembur di hari libur (weekend/hari raya): minimal 5 jam kerja = 1 sesi.
            # Rate-nya BEDA tergantung jenis hari (dipisah di _calc_payroll_components).
            if rec.check_in and rec.check_out:
                dur = _duration_minutes(rec.check_in, rec.check_out)
                if dur >= DAYOFF_MIN_MINUTES:
                    if dtype == 'holiday':
                        ot_holiday_sess += 1
                    else:
                        ot_dayoff_sess += 1
        else:
            # Hari kerja biasa
            if status in ('present', 'late', 'overtime'):
                present_days += 1
            elif status == 'leave':
                leave_days += 1

            if status == 'late':
                late_count += 1
                if rec.check_in:
                    ci_local = timezone.localtime(rec.check_in) if timezone.is_aware(rec.check_in) else rec.check_in
                    work_start = ci_local.replace(
                        hour=STANDARD_CHECK_IN_HOUR, minute=0, second=0, microsecond=0
                    )
                    late_min_raw = max(0, int((ci_local - work_start).total_seconds() / 60))
                    late_min     = max(0, late_min_raw - LATE_TOLERANCE_MINUTES)
                    total_late_min += late_min

            if rec.check_in and rec.check_out:
                dur = _duration_minutes(rec.check_in, rec.check_out)
                if dur > OVERTIME_THRESHOLD:
                    ot_weekday_sess += 1

    # absent_days = hari kerja yang TIDAK present DAN TIDAK leave (termasuk yang
    # sama sekali tidak ada record Attendance-nya = dianggap mangkir tanpa keterangan).
    # Izin/sakit (leave_days) sengaja DIKELUARKAN dari rumus ini sesuai kebijakan
    # "izin/sakit tidak mempengaruhi gaji".
    absent_days = max(0, working_days - present_days - leave_days)

    return {
        'working_days':        working_days,
        'present_days':        present_days,
        'leave_days':          leave_days,
        'absent_days':         absent_days,
        'late_count':          late_count,
        'total_late_minutes':  total_late_min,
        'ot_weekday_sessions': ot_weekday_sess,
        'ot_dayoff_sessions':  ot_dayoff_sess,
        'ot_holiday_sessions': ot_holiday_sess,
    }


def _calc_payroll_components(employee: Employee, summary: dict) -> dict:
    """
    Hitung semua komponen gaji berdasarkan summary absensi.

    Returns:
        basic_salary, overtime_pay, allowance, deduction,
        late_penalty, gross_salary, net_salary
    """
    basic_salary = Decimal(str(employee.salary or 0))

    # ── FIX: sebelumnya ot_holiday_sessions dihitung di summary tapi
    # tidak pernah dikalikan OT_HOLIDAY_RATE dan tidak masuk overtime_pay.
    # Sekarang ketiga jenis lembur (weekday/dayoff/holiday) diikutkan semua.
    ot_weekday_pay = Decimal(summary['ot_weekday_sessions']) * OT_WEEKDAY_RATE
    ot_dayoff_pay  = Decimal(summary['ot_dayoff_sessions'])  * OT_DAYOFF_RATE
    ot_holiday_pay = Decimal(summary['ot_holiday_sessions']) * OT_HOLIDAY_RATE
    overtime_pay   = ot_weekday_pay + ot_dayoff_pay + ot_holiday_pay

    allowance = Decimal('0')

    working_days = summary['working_days'] or 1
    absent_days  = summary['absent_days']
    if absent_days > 0 and basic_salary > 0:
        daily_rate = basic_salary / working_days
        deduction  = daily_rate * absent_days
    else:
        deduction = Decimal('0')

    late_penalty = Decimal(summary['total_late_minutes']) * LATE_PENALTY_PER_MINUTE

    gross_salary = basic_salary + overtime_pay + allowance
    net_salary   = gross_salary - deduction - late_penalty

    return {
        'basic_salary':  basic_salary.quantize(Decimal('0.01')),
        'overtime_pay':  overtime_pay.quantize(Decimal('0.01')),
        'allowance':     allowance.quantize(Decimal('0.01')),
        'deduction':     deduction.quantize(Decimal('0.01')),
        'late_penalty':  late_penalty.quantize(Decimal('0.01')),
        'gross_salary':  gross_salary.quantize(Decimal('0.01')),
        'net_salary':    net_salary.quantize(Decimal('0.01')),
    }


AVATAR_COLORS = [
    '#3b82f6', '#8b5cf6', '#ec4899', '#14b8a6',
    '#f59e0b', '#ef4444', '#10b981', '#6366f1',
    '#f97316', '#06b6d4',
]


def _get_avatar_color(pk: int) -> str:
    return AVATAR_COLORS[pk % len(AVATAR_COLORS)]


def _get_initials(user) -> str:
    initials = ''.join(p[0].upper() for p in [user.first_name, user.last_name] if p)
    return initials or user.username[:2].upper()


def _get_login_status(user) -> str:
    if not user.last_login:
        return 'Never'
    if user.is_active:
        return 'Successful'
    return 'Failed'


def _fmt_rupiah(val) -> str:
    if val is None:
        return 'Rp 0'
    return 'Rp {:,.0f}'.format(float(val)).replace(',', '.')


@login_required
@hrd_required
def dashboard(request):
    today = timezone.localdate()

    total_employees = Profile.objects.filter(role='employee').count()
    present_today   = Attendance.objects.filter(date=today, status='present').count()
    pending_leaves  = Leave.objects.filter(status='pending').count()
    late_today      = Attendance.objects.filter(date=today, status='late').count()

    bar_labels, bar_present, bar_late = [], [], []
    for i in range(6, -1, -1):
        d = today - timedelta(days=i)
        qs = Attendance.objects.filter(date=d)
        bar_labels.append(d.strftime('%d/%m'))
        bar_present.append(qs.filter(status='present').count())
        bar_late.append(qs.filter(status='late').count())

    month_start = today.replace(day=1)
    dept_qs = (
        Attendance.objects
        .filter(date__gte=month_start, date__lte=today)
        .values('employee__organization')
        .annotate(total=Count('id'))
        .order_by('-total')
    )
    pie_labels = [d['employee__organization'] or 'Unknown' for d in dept_qs]
    pie_values = [d['total'] for d in dept_qs]
    recent_attendance = []
    profiles = Profile.objects.filter(role='employee').select_related('user', 'employee')
    for p in profiles:
        user = p.user
        emp = getattr(p, 'employee', None)

        # fetch latest attendance for this user (if any)
        att = (
            Attendance.objects
            .filter(user=user)
            .order_by('-date', '-check_in')
            .select_related('employee')
            .first()
        )

        if att:
            # ensure we expose an `employee` object with expected attributes used by the template
            rec_emp = emp if emp is not None else SimpleNamespace(
                full_name=(user.get_full_name() or user.username),
                organization='',
                employee_id='' 
            )
            rec = SimpleNamespace(
                employee=rec_emp,
                status=(att.status or '').lower(),
                check_in=att.check_in,
                check_out=att.check_out,
                duration=att.duration,
                date=att.date,
            )
        else:
            rec_emp = emp if emp is not None else SimpleNamespace(
                full_name=(user.get_full_name() or user.username),
                organization='',
                employee_id='' 
            )
            rec = SimpleNamespace(
                employee=rec_emp,
                status='absent',
                check_in=None,
                check_out=None,
                duration=None,
                date=None,
            )

        recent_attendance.append(rec)

    return render(request, 'hrd/dashboard.html', {
        'total_employees':   total_employees,
        'present_today':     present_today,
        'pending_leaves':    pending_leaves,
        'late_today':        late_today,
        'today':             today,
        'today_str':         today.strftime('%d %B %Y'),
        # Template expects `recent_attendances` (plural) in JS loop — provide both names for compatibility.
        'recent_attendance': recent_attendance,
        'recent_attendances': recent_attendance,
        'bar_labels':        bar_labels,
        'bar_present':       bar_present,
        'bar_late':          bar_late,
        'pie_labels':        pie_labels,
        'pie_values':        pie_values,
    })

@login_required
@hrd_required
def employees(request):
    search_query  = request.GET.get('q',          '').strip()
    department    = request.GET.get('department', '').strip()
    status_filter = request.GET.get('status',     '').strip()
    login_filter  = request.GET.get('login',      '').strip()

    emp_list = Profile.objects.filter(role='employee').select_related('user')

    if search_query:
        emp_list = emp_list.filter(
            Q(user__first_name__icontains=search_query) |
            Q(user__last_name__icontains=search_query)  |
            Q(user__username__icontains=search_query)   |
            Q(user__email__icontains=search_query)      |
            Q(employee_code__icontains=search_query)    |
            Q(department__icontains=search_query)
        )

    if department:
        emp_list = emp_list.filter(department__iexact=department)

    if status_filter == 'active':
        emp_list = emp_list.filter(user__is_active=True)
    elif status_filter == 'inactive':
        emp_list = emp_list.filter(user__is_active=False)

    employees_data = []
    for profile in emp_list:
        u            = profile.user
        login_status = _get_login_status(u)

        if login_filter and login_filter.lower() != login_status.lower():
            continue

        employees_data.append({
            'user':          u,
            'profile':       profile,
            'full_name':     u.get_full_name() or u.username,
            'employee_code': getattr(profile, 'employee_code', '') or '—',
            'npwp':          getattr(profile, 'npwp', '') or '—',
            'department':    getattr(profile, 'department', '') or '—',
            'login_status':  login_status,
            'avatar_color':  _get_avatar_color(u.pk),
            'initials':      _get_initials(u),
        })

    all_profiles    = Profile.objects.filter(role='employee')
    total_employees = all_profiles.count()
    active_count    = all_profiles.filter(user__is_active=True).count()
    inactive_count  = all_profiles.filter(user__is_active=False).count()

    departments = (
        all_profiles
        .values_list('department', flat=True)
        .distinct()
        .order_by('department')
    )
    departments = [d for d in departments if d]

    return render(request, 'hrd/manage employee.html', {
        'employees':         employees_data,
        'total_employees':   total_employees,
        'active_count':      active_count,
        'inactive_count':    inactive_count,
        'departments':       departments,
        'query':             search_query,
        'department_filter': department,
        'status_filter':     status_filter,
        'login_filter':      login_filter,
    })


@login_required
@hrd_required
def create_employee(request):
    if request.method == 'POST':
        username   = request.POST.get('username', '').strip()
        email      = request.POST.get('email', '').strip()
        password   = request.POST.get('password', '')
        first_name = request.POST.get('first_name', '').strip()
        last_name  = request.POST.get('last_name', '').strip()
        department = request.POST.get('department', '').strip()
        phone      = request.POST.get('phone', '').strip()
        join_date  = request.POST.get('join_date', '').strip()

        errors = []
        if not username:
            errors.append("Username wajib diisi.")
        elif User.objects.filter(username=username).exists():
            errors.append(f"Username '{username}' sudah digunakan.")
        if not password:
            errors.append("Password wajib diisi.")
        elif len(password) < 8:
            errors.append("Password minimal 8 karakter.")
        if email and User.objects.filter(email=email).exists():
            errors.append(f"Email '{email}' sudah terdaftar.")
        if not first_name:
            errors.append("Nama depan wajib diisi.")

        if errors:
            for err in errors:
                messages.error(request, err)
            return render(request, 'hrd/create_employee.html', {'form_data': request.POST})

        user = User.objects.create_user(
            username=username, email=email, password=password,
            first_name=first_name, last_name=last_name,
        )
        profile_data = {'role': 'employee', 'department': department, 'phone': phone}
        if join_date:
            try:
                profile_data['join_date'] = date.fromisoformat(join_date)
            except ValueError:
                messages.warning(request, "Format tanggal bergabung tidak valid, diabaikan.")

        Profile.objects.update_or_create(user=user, defaults=profile_data)
        messages.success(request, f"Karyawan '{first_name} {last_name}' (@{username}) berhasil dibuat. Karyawan perlu login dan mendaftarkan wajahnya sendiri melalui halaman Profil sebelum bisa absen.")
        return redirect('hrd-employees')

    return render(request, 'hrd/form add manage.html', {'form_data': {}})


@login_required
@hrd_required
def edit_employee(request, user_id):
    employee = get_object_or_404(User, pk=user_id)
    profile  = get_object_or_404(Profile, user=employee, role='employee')

    if request.method == 'POST':
        first_name = request.POST.get('first_name', '').strip()
        last_name  = request.POST.get('last_name', '').strip()
        email      = request.POST.get('email', '').strip()

        if email and User.objects.filter(email=email).exclude(pk=employee.pk).exists():
            messages.error(request, f"Email '{email}' sudah digunakan akun lain.")
            return render(request, 'hrd/edit_employee.html', {
                'employee': employee, 'profile': profile,
            })

        employee.first_name = first_name
        employee.last_name  = last_name
        employee.email      = email
        employee.is_active  = request.POST.get('is_active') == 'on'
        employee.save()

        profile.department = request.POST.get('department', '').strip()
        profile.phone      = request.POST.get('phone', '').strip()
        join_date = request.POST.get('join_date', '').strip()
        if join_date:
            try:
                profile.join_date = date.fromisoformat(join_date)
            except ValueError:
                messages.warning(request, "Format tanggal bergabung tidak valid, diabaikan.")

        # ===== Update face descriptor kalau ada rekam ulang =====
        face_descriptor = request.POST.get('face_descriptor', '').strip()
        if face_descriptor:
            profile.face_descriptor = face_descriptor
        # ===== END =====

        profile.save()

        messages.success(request, f"{employee.get_full_name()} berhasil diperbarui.")
        return redirect('hrd-employees')

    return render(request, 'hrd/form manage.html', {
        'employee': employee, 'profile': profile,
    })


@login_required
@hrd_required
def toggle_employee_active(request, user_id):
    if request.method != 'POST':
        return redirect('hrd-employees')

    employee = get_object_or_404(User, pk=user_id)
    if not hasattr(employee, 'profile') or employee.profile.role != 'employee':
        messages.error(request, "User ini bukan karyawan.")
        return redirect('hrd-employees')
    if employee == request.user:
        messages.error(request, "Kamu tidak dapat menonaktifkan akun sendiri.")
        return redirect('hrd-employees')

    action = request.POST.get('action', '').strip()
    if action == 'activate':
        employee.is_active = True
    elif action == 'deactivate':
        employee.is_active = False
    else:
        employee.is_active = not employee.is_active

    employee.save(update_fields=['is_active'])
    status_label = "diaktifkan" if employee.is_active else "dinonaktifkan"
    messages.success(request, f"Akun @{employee.username} berhasil {status_label}.")
    return redirect('hrd-employees')


@login_required
@hrd_required
def attendance(request):
    available_periods = _available_periods()

    today = timezone.localdate()
    if not available_periods:
        MONTH_ID = ['', 'Januari', 'Februari', 'Maret', 'April', 'Mei', 'Juni',
                    'Juli', 'Agustus', 'September', 'Oktober', 'November', 'Desember']
        start, end = _payroll_range(today.year, today.month)
        available_periods = [{
            'year':          today.year,
            'month':         today.month,
            'label':         f"{MONTH_ID[today.month]} {today.year}",
            'is_complete':   False,
            'payroll_start': start.strftime('%d %b'),
            'payroll_end':   end.strftime('%d %b %Y'),
            'selected':      True,
        }]

    return render(request, 'hrd/attandance.html', {
        'available_periods': available_periods,
    })


@login_required
@hrd_required
def attendance_api(request):
    if request.method != 'GET':
        return JsonResponse({'error': 'Method tidak diizinkan.'}, status=405)

    try:
        year     = int(request.GET.get('year',  0))
        month    = int(request.GET.get('month', 0))
        page     = max(1, int(request.GET.get('page',     1)))
        per_page = min(100, max(1, int(request.GET.get('per_page', 10))))
    except (ValueError, TypeError):
        return JsonResponse({'error': 'Parameter tidak valid.'}, status=400)

    today = timezone.localdate()
    if not (1 <= month <= 12) or not (2000 <= year <= today.year + 1):
        return JsonResponse({'error': 'Tahun atau bulan tidak valid.'}, status=400)

    search     = request.GET.get('search',     '').strip()
    department = request.GET.get('department', '').strip()
    status_f   = request.GET.get('status',     '').strip()
    flag       = request.GET.get('flag',       '').strip()

    start_date, end_date = _payroll_range(year, month)
    if end_date > today:
        end_date = today

    qs = (
        Attendance.objects
        .filter(date__range=(start_date, end_date))
        .select_related('employee')
        .order_by('-date', 'employee__full_name')
    )

    if search:
        qs = qs.filter(
            Q(employee__full_name__icontains=search) |
            Q(employee__employee_id__icontains=search)
        )

    if department:
        qs = qs.filter(employee__organization__iexact=department)

    STATUS_MAP = {'Present': 'present', 'Late': 'late', 'Overtime': 'overtime'}
    if status_f and status_f in STATUS_MAP:
        qs = qs.filter(status__iexact=STATUS_MAP[status_f])

    if flag == 'late':
        qs = qs.filter(status__iexact='late')
    elif flag == 'missing_co':
        qs = qs.filter(check_out__isnull=True)

    qs_full = Attendance.objects.filter(date__range=(start_date, end_date))
    stats = {
        'invalid_location':  0,
        'excessive_late':    qs_full.filter(status__iexact='late').count(),
        'missing_clock_out': qs_full.filter(check_out__isnull=True).count(),
        'missing_photo':     0,
    }

    paginator = Paginator(qs, per_page)
    page_obj  = paginator.get_page(page)

    STATUS_DISPLAY = {
        'present':  'Present',
        'late':     'Late',
        'overtime': 'Overtime',
        'absent':   'Absent',
        'leave':    'Leave',
    }

    records = []
    for rec in page_obj.object_list:
        emp = rec.employee

        full_name    = emp.full_name    if emp else '(no employee)'
        emp_code     = emp.employee_id  if emp else ''
        emp_division = emp.organization if emp else ''
        emp_npwp     = emp.npwp         if emp else ''

        clock_in_str  = rec.check_in.strftime('%H:%M')  if rec.check_in  else '—'
        clock_out_str = rec.check_out.strftime('%H:%M') if rec.check_out else None

        raw_status     = (rec.status or 'present').lower().strip()
        display_status = STATUS_DISPLAY.get(raw_status, rec.status.title() if rec.status else 'Present')

        records.append({
            'id':                rec.pk,
            'employee_code':     emp_code,
            'employee_name':     full_name,
            'employee_division': emp_division,
            'employee_npwp':     emp_npwp,
            'date':              rec.date.isoformat(),
            'clock_in':          clock_in_str,
            'clock_out':         clock_out_str,
            'photo_url':         None,
            'latitude':          '—',
            'longitude':         '—',
            'status':            display_status,
            'is_valid_location': True,
            'delay_minutes':     0,
        })

    return JsonResponse({
        'records':    records,
        'stats':      stats,
        'pagination': {
            'total':       paginator.count,
            'page':        page_obj.number,
            'per_page':    per_page,
            'total_pages': paginator.num_pages,
        },
    })


@login_required
@hrd_required
def attendance_export(request):
    try:
        year  = int(request.GET.get('year',  0))
        month = int(request.GET.get('month', 0))
    except (ValueError, TypeError):
        return HttpResponse('Parameter tidak valid.', status=400, content_type='text/plain')

    today = timezone.localdate()
    if not (1 <= month <= 12) or not (2000 <= year <= today.year + 1):
        return HttpResponse('Tahun atau bulan tidak valid.', status=400, content_type='text/plain')

    search     = request.GET.get('search',     '').strip()
    department = request.GET.get('department', '').strip()
    status_f   = request.GET.get('status',     '').strip()
    flag       = request.GET.get('flag',       '').strip()

    start_date, end_date = _payroll_range(year, month)
    if end_date > today:
        end_date = today

    qs = (
        Attendance.objects
        .filter(date__range=(start_date, end_date))
        .select_related('employee')
        .order_by('-date', 'employee__full_name')
    )

    if search:
        qs = qs.filter(
            Q(employee__full_name__icontains=search) |
            Q(employee__employee_id__icontains=search)
        )
    if department:
        qs = qs.filter(employee__organization__iexact=department)

    STATUS_MAP = {'Present': 'present', 'Late': 'late', 'Overtime': 'overtime'}
    if status_f and status_f in STATUS_MAP:
        qs = qs.filter(status__iexact=STATUS_MAP[status_f])

    if flag == 'late':
        qs = qs.filter(status__iexact='late')
    elif flag == 'missing_co':
        qs = qs.filter(check_out__isnull=True)

    MONTH_ID = ['', 'Januari', 'Februari', 'Maret', 'April', 'Mei', 'Juni',
                'Juli', 'Agustus', 'September', 'Oktober', 'November', 'Desember']
    filename = f"Attendance_{MONTH_ID[month]}_{year}.csv"

    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    response.write('\ufeff')
    response.write(
        'ID,Kode Karyawan,Nama,Divisi,NPWP,'
        'Tanggal,Clock In,Clock Out,Status,Working Hours\n'
    )

    for rec in qs:
        emp          = rec.employee
        full_name    = emp.full_name    if emp else ''
        emp_code     = emp.employee_id  if emp else ''
        emp_division = emp.organization if emp else ''
        emp_npwp     = emp.npwp         if emp else ''
        cin_str      = rec.check_in.strftime('%H:%M')  if rec.check_in  else ''
        cout_str     = rec.check_out.strftime('%H:%M') if rec.check_out else ''

        response.write(
            f'{rec.pk},{emp_code},"{full_name}","{emp_division}",'
            f'{emp_npwp},{rec.date.isoformat()},{cin_str},{cout_str},'
            f'{rec.status or ""},{rec.working_hours or ""}\n'
        )

    return response


@login_required
@hrd_required
def attendance_detail(request, record_id):
    rec     = get_object_or_404(Attendance, pk=record_id)

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'save_notes':
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({'ok': True})
        elif action in ('approve', 'reject'):
            messages.success(request, f"Absensi diperbarui.")

    emp = rec.employee

    full_name    = emp.full_name    if emp else '—'
    emp_code     = emp.employee_id  if emp else '—'
    emp_division = emp.organization if emp else '—'
    emp_position = emp.job_position if emp else '—'
    emp_npwp     = emp.npwp         if emp else '—'

    clock_in_str  = rec.check_in.strftime('%H:%M')  if rec.check_in  else '—'
    clock_out_str = rec.check_out.strftime('%H:%M') if rec.check_out else None

    STATUS_DISPLAY = {
        'present':  'Present', 'late': 'Late', 'overtime': 'Overtime',
        'absent':   'Absent',  'leave': 'Leave',
    }

    return render(request, 'hrd/attandance_detail.html', {
        'record': {
            'id':                  rec.pk,
            'date':                rec.date,
            'status':              STATUS_DISPLAY.get((rec.status or '').lower(), 'Present'),
            'clock_in':            clock_in_str,
            'clock_out':           clock_out_str,
            'delay_minutes':       0,
            'latitude':            '—',
            'longitude':           '—',
            'is_valid_location':   True,
            'photo_url':           None,
            'hrd_notes':           '',
            'notes':               '',
            'attachment':          None,
            'approval_status':     'pending',
            'employee_name':       full_name,
            'employee_code':       emp_code,
            'employee_email':      '—',
            'employee_phone':      '—',
            'employee_npwp':       emp_npwp,
            'employee_division':   emp_division,
            'employee_position':   emp_position,
        },
        'rec': rec,
    })

VALID_LEAVE_STATUSES = ('pending', 'approved', 'rejected', 'cancelled')


@login_required
@hrd_required
def leave_approval(request):
    status_filter = request.GET.get('status', 'pending')
    if status_filter not in VALID_LEAVE_STATUSES:
        status_filter = 'pending'

    leave_list = (
        Leave.objects
        .select_related('user', 'user__profile', 'reviewed_by')
        .order_by('-created_at')
        .filter(status=status_filter)
    )
    counts = {s: Leave.objects.filter(status=s).count() for s in VALID_LEAVE_STATUSES}

    return render(request, 'hrd/leave_approval.html', {
        'leave_list':    leave_list,
        'status_filter': status_filter,
        'counts':        counts,
    })


@login_required
@hrd_required
def approve_leave(request, leave_id):
    if request.method != 'POST':
        return redirect('hrd-leave')
    leave = get_object_or_404(Leave, pk=leave_id)
    if leave.status != 'pending':
        messages.warning(request, "Permintaan ini sudah diproses sebelumnya.")
        return redirect('hrd-leave')
    leave.status      = 'approved'
    leave.reviewed_by = request.user
    leave.reviewed_at = timezone.now()
    leave.save()
    messages.success(request, f"Permintaan cuti {leave.user.get_full_name()} disetujui.")
    return redirect('hrd-leave')


@login_required
@hrd_required
def reject_leave(request, leave_id):
    if request.method != 'POST':
        return redirect('hrd-leave')
    leave = get_object_or_404(Leave, pk=leave_id)
    if leave.status != 'pending':
        messages.warning(request, "Permintaan ini sudah diproses sebelumnya.")
        return redirect('hrd-leave')
    leave.status      = 'rejected'
    leave.reviewed_by = request.user
    leave.reviewed_at = timezone.now()
    leave.save()
    messages.success(request, f"Permintaan cuti {leave.user.get_full_name()} ditolak.")
    return redirect('hrd-leave')

@login_required
@hrd_required
def payroll(request):
    today = timezone.localdate()
    try:
        month = int(request.GET.get('month', today.month))
        year  = int(request.GET.get('year',  today.year))
    except (ValueError, TypeError):
        month, year = today.month, today.year

    if not (1 <= month <= 12):
        month = today.month
    if not (2000 <= year <= today.year + 1):
        year = today.year

    return render(request, 'hrd/payroll.html', {
        'payroll_list': [], 'month': month, 'year': year, 'today': today,
    })

@login_required
@hrd_required
def generate_payroll(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'Method tidak diizinkan.'}, status=405)

    try:
        body  = json.loads(request.body)
        month = int(body.get('month', 0))
        year  = int(body.get('year',  0))
    except (json.JSONDecodeError, ValueError, TypeError):
        return JsonResponse({'error': 'Format request tidak valid.'}, status=400)

    if not (1 <= month <= 12):
        return JsonResponse({'error': 'Bulan tidak valid (1–12).'}, status=400)

    today = timezone.localdate()
    if not (2000 <= year <= today.year + 1):
        return JsonResponse({'error': f'Tahun tidak valid (2000–{today.year + 1}).'}, status=400)

    start_date, end_date = _payroll_range(year, month)

    effective_end = min(end_date, today)

    employee_ids = (
        Attendance.objects
        .filter(date__range=(start_date, effective_end))
        .values_list('employee_id', flat=True)
        .distinct()
    )

    if not employee_ids:
        return JsonResponse({
            'success':   True,
            'month':     month,
            'year':      year,
            'period_id': None,
            'employees': [],
            'message':   f'Tidak ada data absensi untuk periode {start_date} s/d {effective_end}.',
        })

    employees_qs = Employee.objects.filter(pk__in=employee_ids)

    existing_payrolls = {
        p.employee_id: p
        for p in Payroll.objects.filter(
            employee_id__in=employee_ids,
            period_start=start_date,
            period_end=end_date,
        )
    }

    result_list = []
    now = timezone.now()

    with transaction.atomic():
        for emp in employees_qs:
            summary    = _calc_attendance_summary(emp.pk, start_date, effective_end)
            components = _calc_payroll_components(emp, summary)

            if emp.pk in existing_payrolls:
                pr = existing_payrolls[emp.pk]
                pr.basic_salary  = components['basic_salary']
                pr.overtime_pay  = components['overtime_pay']
                pr.allowance     = components['allowance']
                pr.deduction     = components['deduction']
                pr.late_penalty  = components['late_penalty']
                pr.gross_salary  = components['gross_salary']
                pr.net_salary    = components['net_salary']
                pr.updated_at    = now
                pr.save(update_fields=[
                    'basic_salary', 'overtime_pay', 'allowance',
                    'deduction', 'late_penalty', 'gross_salary',
                    'net_salary', 'updated_at',
                ])
            else:
                pr = Payroll(
                    employee_id  = emp.pk,
                    period_start = start_date,
                    period_end   = end_date,
                    status       = 'Pending',
                    created_at   = now,
                    updated_at   = now,
                    **components,
                )
                pr.save()

            ot_weekday_amount = int(summary['ot_weekday_sessions']) * int(OT_WEEKDAY_RATE)
            ot_dayoff_amount  = int(summary['ot_dayoff_sessions'])  * int(OT_DAYOFF_RATE)
            # ── FIX: tambahkan amount lembur hari raya nasional supaya
            # ikut tampil di response (sebelumnya tidak dikirim sama sekali).
            ot_holiday_amount = int(summary['ot_holiday_sessions']) * int(OT_HOLIDAY_RATE)

            result_list.append({
                'payroll_id':   pr.pk,
                'employee_pk':  emp.pk,
                'employee_id':  emp.employee_id,
                'name':         emp.full_name,
                'department':   emp.organization or '',
                'position':     emp.job_position or '',
                'npwp':         emp.npwp or '',

                'working_days': summary['working_days'],
                'present_days': summary['present_days'],
                'absent_days':  summary['absent_days'],
                'late_count':   summary['late_count'],

                'ot_weekday_sessions': summary['ot_weekday_sessions'],
                'ot_dayoff_sessions':  summary['ot_dayoff_sessions'],
                'ot_holiday_sessions': summary['ot_holiday_sessions'],
                'ot_weekday_amount':   ot_weekday_amount,
                'ot_dayoff_amount':    ot_dayoff_amount,
                'ot_holiday_amount':   ot_holiday_amount,
                'overtime_amount':     int(components['overtime_pay']),

                'base_salary':      int(components['basic_salary']),
                'salary':           int(components['basic_salary']),
                'salary_formatted': _fmt_rupiah(components['basic_salary']),
                'allowance':        int(components['allowance']),
                'deduction':        int(components['deduction']),
                'late_penalty':     int(components['late_penalty']),
                'gross_salary':     int(components['gross_salary']),
                'net_salary':       int(components['net_salary']),

                'payroll_status': pr.status,
            })

    return JsonResponse({
        'success':    True,
        'month':      month,
        'year':       year,
        'period_id':  None,
        'period_start': start_date.isoformat(),
        'period_end':   end_date.isoformat(),
        'employees':  result_list,
        'message':    f'{len(result_list)} record payroll dimuat — periode {start_date} s/d {end_date}.',
    })

@login_required
@hrd_required
def update_payroll_status(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'Method tidak diizinkan.'}, status=405)

    try:
        body        = json.loads(request.body)
        payroll_ids = body.get('payroll_ids', [])
        status      = body.get('status', '').strip()
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'error': 'Format request tidak valid (bukan JSON).'}, status=400)

    if not payroll_ids:
        return JsonResponse({'error': 'payroll_ids tidak boleh kosong.'}, status=400)
    if not isinstance(payroll_ids, list):
        return JsonResponse({'error': 'payroll_ids harus berupa array.'}, status=400)
    if status not in VALID_PAYROLL_STATUSES:
        return JsonResponse(
            {'error': f'Status tidak valid. Pilihan: {", ".join(VALID_PAYROLL_STATUSES)}.'}, status=400
        )

    now     = timezone.now()
    updated = Payroll.objects.filter(pk__in=payroll_ids).update(
        status=status, updated_at=now
    )

    return JsonResponse({
        'success': True,
        'updated': updated,
        'status':  status,
        'message': f'{updated} data payroll berhasil diupdate ke status "{status}".',
    })

@login_required
@hrd_required
def payroll_detail(request, payroll_id):
    if request.method != 'GET':
        return JsonResponse({'error': 'Method tidak diizinkan.'}, status=405)

    pr = get_object_or_404(Payroll, pk=payroll_id)
    emp = pr.employee

    if not emp:
        return JsonResponse({'error': 'Data employee tidak ditemukan.'}, status=404)

    summary = _calc_attendance_summary(emp.pk, pr.period_start, pr.period_end)

    ot_weekday_amount = int(summary['ot_weekday_sessions']) * int(OT_WEEKDAY_RATE)
    ot_dayoff_amount  = int(summary['ot_dayoff_sessions'])  * int(OT_DAYOFF_RATE)
    # ── FIX: tambahkan amount lembur hari raya nasional supaya
    # ikut tampil di response (sebelumnya tidak dikirim sama sekali).
    ot_holiday_amount = int(summary['ot_holiday_sessions']) * int(OT_HOLIDAY_RATE)

    return JsonResponse({
        'payroll_id':    pr.pk,
        'employee_pk':   emp.pk,
        'name':          emp.full_name,
        'department':    emp.organization or '',
        'position':      emp.job_position or '',
        'npwp':          emp.npwp or '',

        'working_days':  summary['working_days'],
        'present_days':  summary['present_days'],
        'absent_days':   summary['absent_days'],
        'late_count':    summary['late_count'],

        'ot_weekday_sessions': summary['ot_weekday_sessions'],
        'ot_dayoff_sessions':  summary['ot_dayoff_sessions'],
        'ot_holiday_sessions': summary['ot_holiday_sessions'],
        'ot_weekday_amount':   ot_weekday_amount,
        'ot_dayoff_amount':    ot_dayoff_amount,
        'ot_holiday_amount':   ot_holiday_amount,
        'overtime_amount':     int(pr.overtime_pay),

        'base_salary':      int(pr.basic_salary),
        'salary':           int(pr.basic_salary),
        'salary_formatted': _fmt_rupiah(pr.basic_salary),
        'allowance':        int(pr.allowance),
        'deduction':        int(pr.deduction),
        'late_penalty':     int(pr.late_penalty),
        'gross_salary':     int(pr.gross_salary),
        'net_salary':       int(pr.net_salary),

        'payroll_status':   pr.status,
        'period_start':     pr.period_start.isoformat(),
        'period_end':       pr.period_end.isoformat(),
    })