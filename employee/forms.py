from django import forms
from django.contrib.auth.forms import AuthenticationForm
from django_recaptcha.fields import ReCaptchaField
from django_recaptcha.widgets import ReCaptchaV2Checkbox


class LoginFormCaptcha(AuthenticationForm):
    """Extend AuthenticationForm bawaan Django, tambah field captcha"""
    captcha = ReCaptchaField(widget=ReCaptchaV2Checkbox)


class OTPVerifyForm(forms.Form):
    otp_code = forms.CharField(
        max_length=6,
        min_length=6,
        widget=forms.TextInput(attrs={
            'class': 'form-control text-center',
            'placeholder': '000000',
            'autocomplete': 'off',
            'autofocus': True,
        })
    )