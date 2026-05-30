import json
from datetime import date, timedelta
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.paginator import Paginator
from django.db.models import Q, Count, Case, When, IntegerField
from django.http import JsonResponse, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from employee.models import Attendance, Leave, Profile


PAYROLL_CUTOFF_DAY = 25 


# ─────────────────────────────────────────────────────────────────────────────
# Decorator
# ─────────────────────────────────────────────────────────────────────────────

def hrd_required(view_func):
    """Pastikan user sudah login dan berperan sebagai HRD."""
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('login')
        if not hasattr(request.user, 'profile') or request.user.profile.role != 'hrd':
            messages.error(request, "Akses ditolak. Halaman ini hanya untuk HRD.")
            return redirect('login')
        return view_func(request, *args, **kwargs)
    return wrapper


# ─────────────────────────────────────────────────────────────────────────────
# Helper: hitung rentang periode payroll
# Periode payroll: tgl (cutoff+1) bulan lalu s/d tgl cutoff bulan ini
# ─────────────────────────────────────────────────────────────────────────────

def _payroll_range(year: int, month: int):
    """
    Kembalikan (start_date, end_date) untuk periode payroll bulan `month`/`year`.
    Contoh cutoff=25:
      - Periode Jan 2025 → 26 Des 2024 s/d 25 Jan 2025
    """
    end   = date(year, month, PAYROLL_CUTOFF_DAY)

    # Hitung bulan sebelumnya
    if month == 1:
        prev_year, prev_month = year - 1, 12
    else:
        prev_year, prev_month = year, month - 1

    start = date(prev_year, prev_month, PAYROLL_CUTOFF_DAY + 1)
    return start, end


def _available_periods():
    """
    Kembalikan list dict periode yang tersedia (12 bulan ke belakang + bulan berjalan).
    Di-filter hanya jika ada record Attendance pada rentang tersebut.
    """
    today   = timezone.localdate()
    periods = []

    for i in range(11, -1, -1):  # 11 bulan lalu → bulan ini
        # Hitung tahun & bulan
        total_months = today.month - i
        if total_months <= 0:
            y = today.year - 1
            m = total_months + 12
        else:
            y = today.year
            m = total_months

        start, end = _payroll_range(y, m)
        is_complete = (today > end)   # periode sudah lewat cutoff

        # Batas tampil: jika bulan berjalan, max s/d hari ini
        display_end = end if is_complete else today

        # Cek ada data atau tidak
        has_data = Attendance.objects.filter(
            date__range=(start, display_end)
        ).exists()

        if not has_data:
            continue   # skip bulan tanpa data

        label = date(y, m, 1).strftime('%-d %B %Y')   # Linux
        # Windows: '%#d %B %Y'
        # Atau pakai cara manual:
        MONTH_ID = ['', 'Januari','Februari','Maret','April','Mei','Juni',
                    'Juli','Agustus','September','Oktober','November','Desember']
        label = f"{MONTH_ID[m]} {y}"

        periods.append({
            'year':          y,
            'month':         m,
            'label':         label,
            'is_complete':   is_complete,
            'payroll_start': start.strftime('%d %b'),
            'payroll_end':   end.strftime('%d %b %Y'),
            'selected':      (y == today.year and m == today.month),
        })

    return periods


# ─────────────────────────────────────────────────────────────────────────────
# Helper: manage employee
# ─────────────────────────────────────────────────────────────────────────────

AVATAR_COLORS = [
    '#3b82f6', '#8b5cf6', '#ec4899', '#14b8a6',
    '#f59e0b', '#ef4444', '#10b981', '#6366f1',
    '#f97316', '#06b6d4',
]


def _get_avatar_color(pk: int) -> str:
    return AVATAR_COLORS[pk % len(AVATAR_COLORS)]


def _get_initials(user) -> str:
    """Return up to 2 initials from full name or username."""
    initials = ''.join(p[0].upper() for p in [user.first_name, user.last_name] if p)
    return initials or user.username[:2].upper()


def _get_login_status(user) -> str:
    """
    Derive login status:
      - 'Never'      → user belum pernah login
      - 'Successful' → last_login ada dan akun aktif
      - 'Failed'     → akun tidak aktif tapi last_login ada (terkunci)
    """
    if not user.last_login:
        return 'Never'
    if user.is_active:
        return 'Successful'
    return 'Failed'


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@hrd_required
def dashboard(request):
    today = timezone.localdate()

    total_employees = Profile.objects.filter(role='employee').count()
    present_today   = Attendance.objects.filter(date=today, status='present').count()
    pending_leaves  = Leave.objects.filter(status='pending').count()
    late_today      = Attendance.objects.filter(date=today, status='late').count()

    recent_attendance = (
        Attendance.objects
        .filter(date__lte=today)
        .select_related('user')
        .order_by('-date', 'user__first_name')[:50]
    )

    return render(request, 'hrd/dashboard.html', {
        'total_employees':   total_employees,
        'present_today':     present_today,
        'pending_leaves':    pending_leaves,
        'late_today':        late_today,
        'today':             today,
        'recent_attendance': recent_attendance,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Manage Employee
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@hrd_required
def employees(request):
    """
    Daftar karyawan dengan filter:
      - q           : nama / employee_code / email
      - department  : nama departemen
      - status      : active / inactive
      - login       : successful / failed / never
    """
    search_query    = request.GET.get('q',          '').strip()
    department      = request.GET.get('department', '').strip()
    status_filter   = request.GET.get('status',     '').strip()
    login_filter    = request.GET.get('login',      '').strip()

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

    # Build enriched employee list (+ login_filter diterapkan di sini)
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

    # Stats dari SEMUA employee (tidak terpengaruh filter)
    all_profiles     = Profile.objects.filter(role='employee')
    total_employees  = all_profiles.count()
    active_count     = all_profiles.filter(user__is_active=True).count()
    inactive_count   = all_profiles.filter(user__is_active=False).count()

    # Daftar departemen unik untuk dropdown
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
        # Retain filter state
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
        messages.success(request, f"Karyawan '{first_name} {last_name}' (@{username}) berhasil dibuat.")
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

    # Support parameter action eksplisit (activate/deactivate) atau toggle biasa
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


# ─────────────────────────────────────────────────────────────────────────────
# Attendance — halaman utama (render template)
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@hrd_required
def attendance(request):
    """
    Render halaman hrd_attendance.html.
    Data tabel dimuat via AJAX ke attendance_api() di bawah.
    Template hanya butuh `available_periods` untuk mengisi <select>.
    """
    available_periods = _available_periods()

    # Pastikan selalu ada minimal 1 opsi walau belum ada data absensi
    today = timezone.localdate()
    if not available_periods:
        MONTH_ID = ['', 'Januari','Februari','Maret','April','Mei','Juni',
                    'Juli','Agustus','September','Oktober','November','Desember']
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


# ─────────────────────────────────────────────────────────────────────────────
# Attendance — API endpoint (AJAX JSON)
# GET /api/hrd/attendance/
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@hrd_required
def attendance_api(request):
    """
    Mengembalikan JSON data absensi untuk periode tertentu.

    Query params:
      year        int  — tahun (wajib)
      month       int  — bulan 1-based (wajib)
      search      str  — nama / employee_code
      department  str  — nama departemen
      status      str  — Present | Late | Overtime
      flag        str  — invalid | late | missing_co | missing_photo
      page        int  — halaman (default 1)
      per_page    int  — jumlah per halaman (default 10, max 100)

    Response JSON:
    {
      "records":    [ {...}, ... ],
      "stats":      { "invalid_location": n, "excessive_late": n,
                      "missing_clock_out": n, "missing_photo": n },
      "pagination": { "total": n, "page": n, "per_page": n, "total_pages": n }
    }
    """
    if request.method != 'GET':
        return JsonResponse({'error': 'Method tidak diizinkan.'}, status=405)

    # ── Parse & validasi parameter ───────────────────────────────────────────
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

    # ── Rentang tanggal periode payroll ─────────────────────────────────────
    start_date, end_date = _payroll_range(year, month)

    # Jika periode masih berjalan, batasi s/d hari ini
    if end_date > today:
        end_date = today

    # ── Base queryset ────────────────────────────────────────────────────────
    qs = (
        Attendance.objects
        .filter(date__range=(start_date, end_date))
        .select_related('user', 'user__profile')
        .order_by('-date', 'user__first_name')
    )

    # ── Filter: search (nama / employee_code) ────────────────────────────────
    if search:
        qs = qs.filter(
            Q(user__first_name__icontains=search) |
            Q(user__last_name__icontains=search)  |
            Q(user__profile__employee_code__icontains=search)
        )

    # ── Filter: departemen ───────────────────────────────────────────────────
    if department:
        qs = qs.filter(user__profile__department__iexact=department)

    # ── Filter: status absensi ───────────────────────────────────────────────
    # Model menyimpan lowercase (present/late/overtime), HTML kirim Title Case
    STATUS_MAP = {
        'Present': 'present',
        'Late':    'late',
        'Overtime':'overtime',
    }
    if status_f and status_f in STATUS_MAP:
        qs = qs.filter(status=STATUS_MAP[status_f])

    # ── Filter: flag dari stat card ──────────────────────────────────────────
    if flag == 'invalid':
        qs = qs.filter(is_valid_location=False)
    elif flag == 'late':
        qs = qs.filter(status='late')
    elif flag == 'missing_co':
        qs = qs.filter(clock_out__isnull=True)
    elif flag == 'missing_photo':
        qs = qs.filter(Q(photo__isnull=True) | Q(photo=''))

    # ── Stats (selalu dihitung dari full periode tanpa flag/search) ──────────
    qs_full = Attendance.objects.filter(date__range=(start_date, end_date))
    stats = {
        'invalid_location':  qs_full.filter(is_valid_location=False).count(),
        'excessive_late':    qs_full.filter(status='late').count(),
        'missing_clock_out': qs_full.filter(clock_out__isnull=True).count(),
        'missing_photo':     qs_full.filter(Q(photo__isnull=True) | Q(photo='')).count(),
    }

    # ── Pagination ───────────────────────────────────────────────────────────
    paginator   = Paginator(qs, per_page)
    page_obj    = paginator.get_page(page)

    # ── Serialize records ────────────────────────────────────────────────────
    records = []
    for rec in page_obj.object_list:
        profile = getattr(rec.user, 'profile', None)

        # Resolve URL foto (field ImageField di model → .url)
        photo_url = None
        if rec.photo:
            try:
                photo_url = request.build_absolute_uri(rec.photo.url)
            except Exception:
                photo_url = None

        # Status: simpan di DB lowercase, kirim Title Case ke frontend
        STATUS_DISPLAY = {
            'present': 'Present',
            'late':    'Late',
            'overtime':'Overtime',
            'absent':  'Absent',
        }

        records.append({
            'id':                rec.pk,
            'employee_code':     getattr(profile, 'employee_code', '') or '',
            'employee_name':     rec.user.get_full_name() or rec.user.username,
            'employee_division': getattr(profile, 'department', '') or '',
            'employee_npwp':     getattr(profile, 'npwp', '') or '',
            'date':              rec.date.isoformat(),
            # clock_in / clock_out: bisa TimeField atau CharField di model
            'clock_in':          str(rec.clock_in)[:5]  if rec.clock_in  else '—',
            'clock_out':         str(rec.clock_out)[:5] if rec.clock_out else None,
            'photo_url':         photo_url,
            'latitude':          str(rec.latitude)  if getattr(rec, 'latitude',  None) else '—',
            'longitude':         str(rec.longitude) if getattr(rec, 'longitude', None) else '—',
            'status':            STATUS_DISPLAY.get(rec.status, rec.status.title()),
            'is_valid_location': getattr(rec, 'is_valid_location', True),
            'delay_minutes':     getattr(rec, 'delay_minutes', 0) or 0,
        })

    return JsonResponse({
        'records': records,
        'stats':   stats,
        'pagination': {
            'total':       paginator.count,
            'page':        page_obj.number,
            'per_page':    per_page,
            'total_pages': paginator.num_pages,
        },
    })


# ─────────────────────────────────────────────────────────────────────────────
# Attendance — Export CSV
# GET /api/hrd/attendance/export/
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@hrd_required
def attendance_export(request):
    """
    Kembalikan CSV seluruh data absensi untuk periode & filter tertentu.
    Tidak ada pagination — semua record dikembalikan sekaligus.
    """
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
        .select_related('user', 'user__profile')
        .order_by('-date', 'user__first_name')
    )

    if search:
        qs = qs.filter(
            Q(user__first_name__icontains=search) |
            Q(user__last_name__icontains=search)  |
            Q(user__profile__employee_code__icontains=search)
        )
    if department:
        qs = qs.filter(user__profile__department__iexact=department)

    STATUS_MAP = {'Present': 'present', 'Late': 'late', 'Overtime': 'overtime'}
    if status_f and status_f in STATUS_MAP:
        qs = qs.filter(status=STATUS_MAP[status_f])

    if flag == 'invalid':
        qs = qs.filter(is_valid_location=False)
    elif flag == 'late':
        qs = qs.filter(status='late')
    elif flag == 'missing_co':
        qs = qs.filter(clock_out__isnull=True)
    elif flag == 'missing_photo':
        qs = qs.filter(Q(photo__isnull=True) | Q(photo=''))

    MONTH_ID = ['','Januari','Februari','Maret','April','Mei','Juni',
                'Juli','Agustus','September','Oktober','November','Desember']
    filename = f"Attendance_{MONTH_ID[month]}_{year}.csv"

    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    response.write('\ufeff')   # BOM agar Excel terbaca UTF-8

    # Header CSV
    response.write(
        'ID,Kode Karyawan,Nama,Divisi,NPWP,'
        'Tanggal,Clock In,Clock Out,Status,Delay (menit),'
        'Latitude,Longitude,Valid Lokasi,Ada Foto\n'
    )

    for rec in qs:
        profile  = getattr(rec.user, 'profile', None)
        has_photo = bool(getattr(rec, 'photo', None))
        row = (
            f'{rec.pk},'
            f'{getattr(profile, "employee_code", "") or ""},'
            f'"{rec.user.get_full_name()}",'
            f'"{getattr(profile, "department", "") or ""}",'
            f'{getattr(profile, "npwp", "") or ""},'
            f'{rec.date.isoformat()},'
            f'{str(rec.clock_in)[:5] if rec.clock_in else ""},'
            f'{str(rec.clock_out)[:5] if rec.clock_out else ""},'
            f'{rec.status},'
            f'{getattr(rec, "delay_minutes", 0) or 0},'
            f'{getattr(rec, "latitude",  "") or ""},'
            f'{getattr(rec, "longitude", "") or ""},'
            f'{"Ya" if getattr(rec, "is_valid_location", True) else "Tidak"},'
            f'{"Ya" if has_photo else "Tidak"}\n'
        )
        response.write(row)

    return response


# ─────────────────────────────────────────────────────────────────────────────
# Attendance — Detail record
# ─────────────────────────────────────────────────────────────────────────────

@login_required
@hrd_required
def attendance_detail(request, record_id):
    rec     = get_object_or_404(Attendance, pk=record_id)
    profile = getattr(rec.user, 'profile', None)

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'save_notes':
            rec.hrd_notes = request.POST.get('hrd_notes', '')
            rec.save(update_fields=['hrd_notes'])

            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({'ok': True})

        elif action == 'approve':
            rec.approval_status = 'approve'
            rec.save(update_fields=['approval_status'])
            messages.success(request, f"Absensi {rec.user.get_full_name()} berhasil disetujui.")

        elif action == 'reject':
            rec.approval_status = 'reject'
            rec.save(update_fields=['approval_status'])
            messages.warning(request, f"Absensi {rec.user.get_full_name()} ditolak.")

    return render(request, 'hrd/attandance_detail.html', {
        'rec':     rec,
        'profile': profile,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Leave Approval
# ─────────────────────────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────────────────────────
# Payroll
# ─────────────────────────────────────────────────────────────────────────────

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

    return JsonResponse({
        'success': True, 'month': month, 'year': year,
        'payroll_list': [],
        'message': f'Payroll periode {month}/{year} berhasil dimuat.',
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

    valid_statuses = ['pending', 'approved', 'paid', 'rejected']
    if status not in valid_statuses:
        return JsonResponse(
            {'error': f'Status tidak valid. Pilihan: {", ".join(valid_statuses)}.'}, status=400
        )
    updated = len(payroll_ids)
    return JsonResponse({
        'success': True, 'updated': updated, 'status': status,
        'message': f'{updated} data payroll berhasil diupdate ke status "{status}".',
    })


@login_required
@hrd_required
def payroll_detail(request, payroll_id):
    if request.method != 'GET':
        return JsonResponse({'error': 'Method tidak diizinkan.'}, status=405)
    if payroll_id <= 0:
        return JsonResponse({'error': 'Payroll tidak ditemukan.'}, status=404)
    return JsonResponse({
        'id': payroll_id, 'name': 'Data Belum Tersedia',
        'username': '-', 'email': '-', 'department': '-', 'phone': '-',
        'month': '-', 'year': '-', 'basic_salary': '0', 'allowance': '0',
        'deduction': '0', 'net_salary': '0', 'payroll_status': 'Pending',
        'paid_at': None, 'message': 'Model Payroll belum dikonfigurasi.',
    })