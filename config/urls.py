"""
URL configuration for config project (Final Capstone).
"""
from django.contrib import admin
from django.urls import path
from django.conf import settings
from django.conf.urls.static import static
from django.contrib.auth import views as auth_views
from django.shortcuts import redirect
from employee import views as employee_views
from hrd import views as hrd_views


def dashboard_redirect(request):
    """
    Penentu redirect setelah login berhasil.
    LOGIN_REDIRECT_URL di settings.py arahkan ke sini.
    """
    if not request.user.is_authenticated:
        return redirect('login')
    try:
        role = request.user.profile.role
        if role == 'hrd':
            return redirect('hrd-dashboard')
        return redirect('employee-dashboard')
    except Exception:
        return redirect('login')


urlpatterns = [
    path('admin/', admin.site.urls),

    path('', lambda req: redirect('login'), name='root'),

    path('login/',  auth_views.LoginView.as_view(template_name='login.html'), name='login'),
    path('logout/', auth_views.LogoutView.as_view(next_page='login'),         name='logout'),

    path('dashboard/', dashboard_redirect, name='dashboard'),

    path('hrd/dashboard/',                       hrd_views.dashboard,              name='hrd-dashboard'),

    path('hrd/employees/',                       hrd_views.employees,              name='hrd-employees'),
    path('hrd/employees/create/',                hrd_views.create_employee,        name='hrd-create-employee'),
    path('hrd/employees/<int:user_id>/edit/',    hrd_views.edit_employee,          name='hrd-edit-employee'),
    path('hrd/employees/<int:user_id>/toggle/',  hrd_views.toggle_employee_active, name='hrd-toggle-employee'),

    path('hrd/attendance/',                      hrd_views.attendance,             name='hrd-attendance'),

    path('hrd/attendance/detail/<int:record_id>/', hrd_views.attendance_detail, name='hrd-attendance-detail'),

    path('hrd/api/attendance/',         hrd_views.attendance_api,    name='hrd-attendance-api'),
    path('hrd/api/attendance/export/',  hrd_views.attendance_export, name='hrd-attendance-export'),
    
    path('hrd/leave/',                           hrd_views.leave_approval,         name='hrd-leave'),
    path('hrd/leave/<int:leave_id>/approve/',    hrd_views.approve_leave,          name='hrd-approve-leave'),
    path('hrd/leave/<int:leave_id>/reject/',     hrd_views.reject_leave,           name='hrd-reject-leave'),

   path('hrd/payroll/',                         hrd_views.payroll,                name='hrd-payroll'),
   path('hrd/payroll/generate/',                hrd_views.generate_payroll,       name='hrd-generate-payroll'),
   path('hrd/payroll/update-status/',           hrd_views.update_payroll_status,  name='hrd-payroll-update-status'),
   path('hrd/payroll/<int:payroll_id>/',        hrd_views.payroll_detail,         name='hrd-payroll-detail'),

    path('employee/dashboard/',                  employee_views.dashboard,         name='employee-dashboard'),

    path('employee/checkin/',                    employee_views.checkin,           name='employee-checkin'),

    path('employee/reports/',                    employee_views.reports,           name='employee-reports'),
    path('employee/reports/api/',                employee_views.report_stats_api,  name='employee-report-stats-api'),
    path('employee/reports/pdf/',                employee_views.report_pdf,        name='employee-report-pdf'),

    path('employee/leave/',                      employee_views.leave_request,     name='employee-leave'),
    path('employee/leave/<int:leave_id>/cancel/', employee_views.cancel_leave,     name='employee-cancel-leave'),

    path('employee/payroll/',                    employee_views.payroll,           name='employee-payroll'),
    path('employee/payslip_pdf/',                employee_views.payslip_pdf,       name='employee-payslip-pdf'),

    path('employee/profile/',                    employee_views.profile,           name='employee-profile'),
    path('employee/profile/update-phone/',       employee_views.update_phone,      name='employee-update-phone'),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)