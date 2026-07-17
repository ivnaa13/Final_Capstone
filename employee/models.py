"""
models.py  —  employee app
"""
import base64
import random
import uuid
from datetime import timedelta
from pathlib import Path

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone


def _save_b64_photo(b64_string: str, upload_to: str) -> "str | None":
    """Simpan foto base64 ke disk, kembalikan path relatif."""
    if not b64_string or ',' not in b64_string:
        return None
    try:
        header, data = b64_string.split(',', 1)
        ext      = 'png' if 'png' in header else 'jpg'
        filename = f"{uuid.uuid4().hex}.{ext}"
        rel_path = Path(upload_to) / filename

        from django.conf import settings
        abs_path = Path(settings.MEDIA_ROOT) / rel_path
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_bytes(base64.b64decode(data))
        return str(rel_path)
    except Exception:
        return None


class Employee(models.Model):

    # Kebijakan perusahaan: Employee ID harus berupa angka saja (tanpa huruf/simbol).
    # NOTE: model ini managed=False (mirror tabel existing) -> validator ini
    # hanya berlaku di level Python/form (full_clean(), ModelForm, Django Admin),
    # TIDAK ada CHECK constraint di level database.
    employee_id_validator = RegexValidator(
        regex=r'^\d+$',
        message='Employee ID harus berupa angka saja, tanpa huruf atau simbol.'
    )

    employee_id     = models.CharField(
        max_length=50, unique=True, validators=[employee_id_validator]
    )
    npwp            = models.CharField(max_length=50, blank=True)
    full_name       = models.CharField(max_length=255)

    shift           = models.CharField(max_length=100, blank=True)
    organization    = models.CharField(max_length=100, blank=True)
    job_position    = models.CharField(max_length=100, blank=True)
    job_level       = models.CharField(max_length=100, blank=True)

    join_date       = models.DateField(null=True, blank=True)
    employment_type = models.CharField(max_length=50, blank=True)
    employee_status = models.CharField(max_length=50, blank=True)
    end_date        = models.DateField(null=True, blank=True)
    # Note: original schema from some environments used a different column name.
    # Keep only `employee_status` which matches the actual DB column.

    branch_name     = models.CharField(max_length=100, blank=True)

    religion        = models.CharField(max_length=50, blank=True)
    gender          = models.CharField(max_length=20, blank=True)
    marital_status  = models.CharField(max_length=30, blank=True)

    salary          = models.DecimalField(
        max_digits=15, decimal_places=2, null=True, blank=True
    )

    created_at      = models.DateTimeField(null=True, blank=True)
    updated_at      = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table            = "employee"
        managed             = False
        verbose_name        = 'Employee'
        verbose_name_plural = 'Employees'

    def __str__(self):
        return f"{self.full_name} ({self.employee_id})"


class Profile(models.Model):
    ROLE_CHOICES = (
        ('hrd',        'HRD'),
        ('supervisor', 'Supervisor'),
        ('employee',   'Employee'),
    )

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name='profile',
    )

    employee = models.OneToOneField(
        Employee,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='profile',
        help_text="Tautkan ke baris Employee jika role = employee",
    )

    role          = models.CharField(max_length=20, choices=ROLE_CHOICES, default='employee')
    department    = models.CharField(max_length=100, blank=True)
    phone         = models.CharField(max_length=20, blank=True)
    join_date     = models.DateField(null=True, blank=True)
    avatar        = models.ImageField(upload_to='avatars/', null=True, blank=True)

    employee_code = models.CharField(max_length=50, blank=True, unique=True, null=True)
    npwp          = models.CharField(max_length=30, blank=True)

    face_descriptor = models.TextField(
        blank=True, null=True,
        help_text="128-dimensi face descriptor (JSON array) dari face-api.js — dipakai untuk verifikasi wajah saat absen"
    )

    class Meta:
        verbose_name        = 'Profile'
        verbose_name_plural = 'Profiles'

    def __str__(self):
        return f"{self.user.username} ({self.role})"

    def sync_from_employee(self):
        """Sinkron field dari relasi Employee. Panggil sebelum .save()."""
        if self.employee:
            self.employee_code = self.employee.employee_id
            self.npwp          = self.employee.npwp
            self.department    = self.employee.organization
            self.join_date     = self.employee.join_date


class LoginOTP(models.Model):
    """OTP sementara untuk proses login (2FA via WhatsApp)"""
    user       = models.ForeignKey(User, on_delete=models.CASCADE, related_name='login_otps')
    code       = models.CharField(max_length=6)
    is_used    = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    expired_at = models.DateTimeField()

    class Meta:
        verbose_name        = 'Login OTP'
        verbose_name_plural = 'Login OTPs'

    def save(self, *args, **kwargs):
        if not self.expired_at:
            self.expired_at = timezone.now() + timedelta(minutes=5)
        super().save(*args, **kwargs)

    def is_valid(self):
        return (not self.is_used) and (timezone.now() <= self.expired_at)

    @staticmethod
    def generate_code():
        return str(random.randint(100000, 999999))

    def __str__(self):
        return f"OTP {self.code} — {self.user.username}"


class Attendance(models.Model):
    STATUS_CHOICES = (
        ('present', 'Present'),
        ('late',    'Late'),
        ('absent',  'Absent'),
        ('leave',   'Leave'),
    )
    APPROVAL_CHOICES = (
        ('pending',  'Pending'),
        ('approved', 'Approved'),
        ('reject', 'Reject'),
    )

    user     = models.ForeignKey(User, on_delete=models.CASCADE, related_name='attendances')
    employee = models.ForeignKey(
        Employee,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='user_attendances',
    )
    date             = models.DateField()

    check_in         = models.DateTimeField(null=True, blank=True)
    check_in_lat     = models.FloatField(null=True, blank=True)
    check_in_lng     = models.FloatField(null=True, blank=True)
    check_in_address = models.TextField(blank=True)

    check_out         = models.DateTimeField(null=True, blank=True)
    check_out_lat     = models.FloatField(null=True, blank=True)
    check_out_lng     = models.FloatField(null=True, blank=True)
    check_out_address = models.TextField(blank=True)

    status          = models.CharField(max_length=20, choices=STATUS_CHOICES, default='present')
    approval_status = models.CharField(max_length=20, choices=APPROVAL_CHOICES, default='pending')
    note            = models.TextField(blank=True)

    photo_checkin  = models.ImageField(upload_to='attendance/checkin/',  null=True, blank=True)
    photo_checkout = models.ImageField(upload_to='attendance/checkout/', null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering        = ['-date']
        unique_together = ('user', 'date')
        verbose_name    = 'Attendance'
        verbose_name_plural = 'Attendances'

    def __str__(self):
        return f"{self.user.username} — {self.date} [{self.status}]"

    @property
    def duration(self) -> str | None:
        if self.check_in and self.check_out:
            total   = int((self.check_out - self.check_in).total_seconds())
            hours   = total // 3600
            minutes = (total % 3600) // 60
            return f"{hours}h {minutes}m"
        return None

    @property
    def duration_minutes(self) -> int:
        if self.check_in and self.check_out:
            return int((self.check_out - self.check_in).total_seconds() / 60)
        return 0

    @property
    def overtime_hours(self) -> float:
        # NOTE: Perhitungan overtime resmi untuk PAYROLL dilakukan di
        # employee_hrd/views.py (_calc_attendance_summary), berbasis SESI
        # (>10 jam hari kerja / >=5 jam hari libur & hari raya), BUKAN
        # properti ini. Properti ini hanya estimasi kasar per-record
        # untuk ditampilkan di halaman absensi individual, threshold-nya
        # sengaja dibedakan (8 jam) dan TIDAK dipakai untuk hitung gaji.
        excess = max(0, self.duration_minutes - 480)
        return round(excess / 60, 2)

    @property
    def is_late(self) -> bool:
        return self.status == 'late'

    @property
    def counts_toward_deduction(self) -> bool:
        """
        Kebijakan perusahaan: izin/sakit (status='leave') TIDAK memotong gaji.
        Hanya status 'absent' (tanpa keterangan) yang dihitung sebagai
        potongan gaji di payroll.

        SATU-SATUNYA tempat aturan ini didefinisikan — payroll
        (_calc_attendance_summary di employee_hrd/views.py) HARUS memanggil
        property ini, bukan bandingkan string 'absent' secara manual.
        """
        return self.status == 'absent'

    @property
    def counts_as_paid_day(self) -> bool:
        """
        Hari yang TETAP dibayar penuh meski karyawan tidak fisik hadir:
        present, late (tetap masuk meski telat), leave (izin/sakit resmi).
        Hanya 'absent' yang tidak dibayar / kena potongan.
        """
        return self.status in ('present', 'late', 'leave')

    def save_checkin_photo(self, b64: str):
        path = _save_b64_photo(b64, 'attendance/checkin')
        if path:
            self.photo_checkin = path

    def save_checkout_photo(self, b64: str):
        path = _save_b64_photo(b64, 'attendance/checkout')
        if path:
            self.photo_checkout = path


class Holiday(models.Model):
    """
    Kalender hari libur nasional / cuti bersama (hari raya).

    Dipakai untuk membedakan rate lembur:
      - Lembur di hari libur/weekend biasa (Sabtu-Minggu) → OT_DAYOFF_RATE  (Rp100.000)
      - Lembur di hari raya / libur nasional               → OT_HOLIDAY_RATE (Rp200.000)

    HRD mengisi tanggal-tanggal ini secara manual tiap tahun (mis. lewat Django Admin),
    karena tanggal hari raya berubah tiap tahun dan butuh keputusan HR (bukan bisa
    dihitung otomatis dari rumus kalender).
    """
    date       = models.DateField(unique=True)
    name       = models.CharField(max_length=150, help_text="Contoh: 'Idul Fitri', 'Hari Kemerdekaan'")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering            = ['date']
        verbose_name        = 'Holiday'
        verbose_name_plural  = 'Holidays'

    def __str__(self):
        return f"{self.date} — {self.name}"


class Leave(models.Model):
    LEAVE_TYPE_CHOICES = (
        ('sick',      'Sick Leave'),
        ('annual',    'Annual Leave'),
        ('personal',  'Personal Leave'),
        ('urgent',    'Urgent Matter'),
        ('maternity', 'Maternity Leave'),
    )
    STATUS_CHOICES = (
        ('pending',   'Pending'),
        ('approved',  'Approved'),
        ('rejected',  'Rejected'),
        ('cancelled', 'Cancelled'),
    )

    # Jenis cuti yang WAJIB memenuhi syarat minimal 1 tahun masa kerja.
    # sick / urgent / maternity TIDAK dibatasi karena sifatnya darurat/medis.
    ELIGIBILITY_REQUIRED_TYPES = ('annual', 'personal')
    MIN_SERVICE_DAYS_FOR_LEAVE = 365  # kebijakan perusahaan: 1 tahun kerja

    user       = models.ForeignKey(User, on_delete=models.CASCADE, related_name='leaves')
    leave_type = models.CharField(max_length=50, choices=LEAVE_TYPE_CHOICES)
    start_date = models.DateField()
    end_date   = models.DateField()
    reason     = models.TextField()
    document   = models.FileField(upload_to='leave_documents/', null=True, blank=True)
    status     = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')

    reviewed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='reviewed_leaves',
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.user.username} — {self.get_leave_type_display()} [{self.status}]"

    @property
    def duration_days(self) -> int:
        return (self.end_date - self.start_date).days + 1

    @staticmethod
    def check_eligibility(user: User, leave_type: str) -> tuple[bool, str]:
        """
        Cek apakah `user` sudah eligible mengajukan `leave_type`,
        berdasarkan kebijakan: cuti tahunan/personal baru bisa diajukan
        setelah >= 1 tahun kerja (dihitung dari Profile.join_date).

        Return: (eligible: bool, error_message: str)
        - eligible=True, message=''  → boleh diajukan
        - eligible=False, message='...' → tidak boleh, alasan di message
        """
        if leave_type not in Leave.ELIGIBILITY_REQUIRED_TYPES:
            return True, ''

        profile   = getattr(user, 'profile', None)
        join_date = getattr(profile, 'join_date', None) if profile else None

        if not join_date:
            # Data join_date belum ada di sistem — jangan blokir otomatis,
            # tapi ini kondisi yang harus direview manual oleh HRD.
            return True, ''

        days_worked = (timezone.localdate() - join_date).days
        if days_worked < Leave.MIN_SERVICE_DAYS_FOR_LEAVE:
            remaining_days = Leave.MIN_SERVICE_DAYS_FOR_LEAVE - days_worked
            return False, (
                f"Cuti tahunan/personal hanya bisa diajukan setelah 1 tahun masa kerja. "
                f"Karyawan ini baru bekerja {days_worked} hari (kurang {remaining_days} hari lagi)."
            )
        return True, ''

    def clean(self):
        """
        Validasi model-level. Dipanggil saat form/ModelForm melakukan
        full_clean(), termasuk lewat Django Admin. Untuk raw .save() biasa
        (tanpa full_clean()), validasi tambahan tetap dilakukan eksplisit
        di view (lihat employee_hrd/views.py: approve_leave).
        """
        eligible, message = Leave.check_eligibility(self.user, self.leave_type)
        if not eligible:
            raise ValidationError({'leave_type': message})


class Payroll(models.Model):
    """
    Mirror tabel 'payroll' di database.
    managed = False  →  Django TIDAK membuat/mengubah tabel ini.
    """

    STATUS_CHOICES = (
        ('Pending',              'Pending'),              # baru digenerate HRD
        ('Waiting Supervisor',   'Waiting Supervisor'),    # menunggu review Atasan (level 1)
        ('Supervisor Approved',  'Supervisor Approved'),   # Atasan setuju, menunggu HRD (level 2)
        ('Supervisor Rejected',  'Supervisor Rejected'),   # Atasan menolak
        ('Draft',                'Draft'),
        ('Approved',             'Approved'),              # HRD setuju (final)
        ('Reject',               'Reject'),                # HRD menolak
        ('Done',                 'Done'),
    )

    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        db_column='employee_id',
        related_name='payrolls',
        null=True,
        blank=True,
    )

    period_start = models.DateField()
    period_end   = models.DateField()

    basic_salary  = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    overtime_pay  = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    allowance     = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    deduction     = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    late_penalty  = models.DecimalField(max_digits=15, decimal_places=2, default=0)

    gross_salary  = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    net_salary    = models.DecimalField(max_digits=15, decimal_places=2, default=0)

    status = models.CharField(
        max_length=50,
        choices=STATUS_CHOICES,
        default='Pending',
    )

    created_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table  = 'payroll'
        managed   = False
        ordering  = ['-period_end', 'employee__full_name']
        verbose_name        = 'Payroll'
        verbose_name_plural = 'Payrolls'

    def __str__(self):
        emp  = self.employee.full_name if self.employee else f"emp#{self.employee_id}"
        return f"{emp} — {self.period_start} s/d {self.period_end} [{self.status}]"

    @property
    def total_salary(self) -> float:
        return float(self.net_salary or 0)

    @property
    def period_label(self) -> str:
        return f"{self.period_start.strftime('%d %b %Y')} – {self.period_end.strftime('%d %b %Y')}"

    @property
    def is_waiting_supervisor(self) -> bool:
        return self.status == 'Waiting Supervisor'

    @property
    def is_waiting_hrd(self) -> bool:
        """Atasan sudah approve — menunggu HRD (level kedua/final)."""
        return self.status == 'Supervisor Approved'

    def get_approval(self, level: str):
        """Ambil record PayrollApproval untuk level tertentu ('supervisor'/'hrd')."""
        return self.approvals.filter(level=level).first()


class PayrollApproval(models.Model):
    """
    Jejak approval payroll 2 TINGKAT: Atasan (supervisor) dulu, baru HRD.

    Dibuat sebagai model TERPISAH (bukan menambah kolom langsung ke tabel
    `payroll`), karena Payroll.Meta.managed=False — tabel itu mirror dari
    database existing yang TIDAK boleh diubah strukturnya lewat Django
    migration. Model ini (managed=True default) bikin tabel BARU sendiri,
    tidak menyentuh tabel `payroll` sama sekali.

    Payroll.status tetap jadi status ringkas untuk filter/tampilan cepat;
    PayrollApproval adalah audit trail detail: siapa approve, kapan, catatan apa.
    """
    LEVEL_CHOICES = (
        ('supervisor', 'Supervisor (Atasan)'),
        ('hrd',        'HRD'),
    )
    DECISION_CHOICES = (
        ('pending',  'Pending'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
    )

    payroll = models.ForeignKey(
        Payroll, on_delete=models.CASCADE, related_name='approvals'
    )
    level       = models.CharField(max_length=20, choices=LEVEL_CHOICES)
    decision    = models.CharField(max_length=20, choices=DECISION_CHOICES, default='pending')
    reviewed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='payroll_approvals'
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    note        = models.TextField(blank=True)
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together     = ('payroll', 'level')
        ordering            = ['payroll_id', 'level']
        verbose_name        = 'Payroll Approval'
        verbose_name_plural  = 'Payroll Approvals'

    def __str__(self):
        return f"Payroll#{self.payroll_id} — {self.get_level_display()} — {self.get_decision_display()}"


from django.db.models.signals import post_save
from django.dispatch import receiver


@receiver(post_save, sender=Profile)
def sync_profile_employee_fields(sender, instance: Profile, **kwargs):
    """
    Auto-sync employee_code, npwp, department, join_date dari Employee
    setiap kali Profile di-save.
    """
    if not instance.employee:
        return

    updates = {}
    if instance.employee_code != instance.employee.employee_id:
        updates['employee_code'] = instance.employee.employee_id
    if instance.npwp != instance.employee.npwp:
        updates['npwp'] = instance.employee.npwp
    if instance.department != instance.employee.organization:
        updates['department'] = instance.employee.organization
    if instance.join_date != instance.employee.join_date:
        updates['join_date'] = instance.employee.join_date

    if updates:
        Profile.objects.filter(pk=instance.pk).update(**updates)