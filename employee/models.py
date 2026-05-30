"""
models.py  —  employee app
Handles Profile, Attendance (daily check-in/out) and Leave requests.
"""
import base64, uuid
from pathlib import Path

from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone


# ─────────────────────────────────────────────
#  HELPER — save base64 photo sent from browser
# ─────────────────────────────────────────────
def _save_b64_photo(b64_string: str, upload_to: str) -> str | None:
    if not b64_string or ',' not in b64_string:
        return None
    try:
        header, data = b64_string.split(',', 1)
        ext = 'jpg'
        if 'png' in header:
            ext = 'png'
        filename = f"{uuid.uuid4().hex}.{ext}"
        rel_path = Path(upload_to) / filename

        from django.conf import settings
        abs_path = Path(settings.MEDIA_ROOT) / rel_path
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_bytes(base64.b64decode(data))

        return str(rel_path)
    except Exception:
        return None


# ─────────────────────────────────────────────
#  PROFILE
# ─────────────────────────────────────────────
class Profile(models.Model):
    ROLE_CHOICES = (
        ('hrd',      'HRD'),
        ('employee', 'Employee'),
    )

    user       = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    role       = models.CharField(max_length=20, choices=ROLE_CHOICES)
    department = models.CharField(max_length=100, blank=True)
    phone      = models.CharField(max_length=20, blank=True)
    join_date  = models.DateField(null=True, blank=True)
    avatar     = models.ImageField(upload_to='avatars/', null=True, blank=True)

    class Meta:
        verbose_name        = 'Profile'
        verbose_name_plural = 'Profiles'

    def __str__(self):
        return f"{self.user.username} ({self.role})"


# ─────────────────────────────────────────────
#  ATTENDANCE
# ─────────────────────────────────────────────
class Attendance(models.Model):
    STATUS_CHOICES = (
        ('present', 'Present'),
        ('late',    'Late'),
        ('absent',  'Absent'),
        ('leave',   'Leave'),
    )

    user             = models.ForeignKey(User, on_delete=models.CASCADE, related_name='attendances')
    date             = models.DateField()

    check_in         = models.DateTimeField(null=True, blank=True)
    check_in_lat     = models.FloatField(null=True, blank=True)
    check_in_lng     = models.FloatField(null=True, blank=True)
    check_in_address = models.TextField(blank=True)

    check_out         = models.DateTimeField(null=True, blank=True)
    check_out_lat     = models.FloatField(null=True, blank=True)
    check_out_lng     = models.FloatField(null=True, blank=True)
    check_out_address = models.TextField(blank=True)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='present')
    note   = models.TextField(blank=True)

    photo_checkin  = models.ImageField(upload_to='attendance/checkin/',  null=True, blank=True)
    photo_checkout = models.ImageField(upload_to='attendance/checkout/', null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering            = ['-date']
        unique_together     = ('user', 'date')
        verbose_name        = 'Attendance'
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
        standard = 8 * 60  # 480 minutes
        excess   = max(0, self.duration_minutes - standard)
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


# ─────────────────────────────────────────────
#  LEAVE
# ─────────────────────────────────────────────
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
        null=True, blank=True, related_name='reviewed_leaves'
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering            = ['-created_at']
        verbose_name        = 'Leave Request'
        verbose_name_plural = 'Leave Requests'

    def __str__(self):
        return f"{self.user.username} — {self.get_leave_type_display()} [{self.status}]"

    @property
    def duration_days(self) -> int:
        return (self.end_date - self.start_date).days + 1