"""
models.py  —  employee app
"""
import base64
import uuid
from pathlib import Path

from django.contrib.auth.models import User
from django.db import models

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

    employee_id     = models.CharField(max_length=50, unique=True)
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
    status_employee = models.CharField(max_length=50, blank=True)

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
        ('hrd',      'HRD'),
        ('employee', 'Employee'),
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
        ('rejected', 'Rejected'),
    )

    user     = models.ForeignKey(User, on_delete=models.CASCADE, related_name='attendances')
    employee = models.ForeignKey(          # ← tambahan — link ke tabel legacy
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
        excess = max(0, self.duration_minutes - 480)
        return round(excess / 60, 2)

    @property
    def is_late(self) -> bool:
        return self.status == 'late'

    def save_checkin_photo(self, b64: str):
        path = _save_b64_photo(b64, 'attendance/checkin')
        if path:
            self.photo_checkin = path

    def save_checkout_photo(self, b64: str):
        path = _save_b64_photo(b64, 'attendance/checkout')
        if path:
            self.photo_checkout = path
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
class Payroll(models.Model):
    """
    Mirror tabel 'payroll' di database.
    managed = False  →  Django TIDAK membuat/mengubah tabel ini.

    Perhitungan overtime:
      - Lebih dari 10 jam di hari kerja          → Rp 35.000 / sesi
      - Masuk hari libur/cuti bersama ≥ 5 jam   → Rp 100.000 / sesi

    Formula:
      gross_salary = basic_salary + overtime_pay + allowance
      net_salary   = gross_salary - deduction - late_penalty
    """

    STATUS_CHOICES = (
        ('Pending',             'Pending'),
        ('Draft',               'Draft'),
        ('Waiting Supervisor',  'Waiting Supervisor'),
        ('Supervisor Approved', 'Supervisor Approved'),
        ('Supervisor Rejected', 'Supervisor Rejected'),
        ('Approved',            'Approved'),
        ('Reject',              'Reject'),
        ('Done',                'Done'),
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
        """Alias untuk net_salary (kompatibilitas template)."""
        return float(self.net_salary or 0)

    @property
    def period_label(self) -> str:
        return f"{self.period_start.strftime('%d %b %Y')} – {self.period_end.strftime('%d %b %Y')}"

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