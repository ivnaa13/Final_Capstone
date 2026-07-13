import json
import math

import requests
from django.conf import settings


# ========================================================
# WHATSAPP OTP (Login 2FA)
# ========================================================

def send_whatsapp_otp(phone_number, otp_code):
    """
    Kirim kode OTP via WhatsApp menggunakan Fonnte API.
    phone_number format: 628xxxxxxxxxx (tanpa +, tanpa spasi, tanpa 0 di depan)
    """
    if not phone_number:
        return False

    headers = {'Authorization': settings.FONNTE_TOKEN}
    message = (
        f"🔐 *Kode OTP Login Garuda TV*\n\n"
        f"Kode OTP: *{otp_code}*\n"
        f"Berlaku selama 5 menit.\n\n"
        f"Jangan berikan kode ini kepada siapapun, termasuk yang mengaku admin."
    )
    payload = {'target': phone_number, 'message': message}

    try:
        response = requests.post(
            settings.FONNTE_API_URL,
            headers=headers,
            data=payload,
            timeout=10
        )
        response.raise_for_status()
        return response.json().get('status', False)
    except requests.RequestException as e:
        print(f"Error kirim WA OTP: {e}")
        return False


# ========================================================
# FACE RECOGNITION (1 Akun 1 Wajah — Check-in/Check-out)
# ========================================================

FACE_MATCH_THRESHOLD = 0.6  # Standar face-api.js — makin kecil makin ketat


def face_distance(descriptor1, descriptor2):
    """
    Hitung Euclidean distance antara 2 face descriptor.
    descriptor1, descriptor2: list of 128 float
    """
    if len(descriptor1) != len(descriptor2):
        return 999  # dianggap tidak cocok kalau panjangnya beda

    total = sum((a - b) ** 2 for a, b in zip(descriptor1, descriptor2))
    return math.sqrt(total)


def verify_face(stored_descriptor_json, submitted_descriptor_json):
    """
    Bandingkan descriptor wajah yang tersimpan (Profile.face_descriptor)
    dengan descriptor yang dikirim saat check-in.

    Return: (is_match: bool, distance: float, message: str)
    """
    if not stored_descriptor_json:
        return False, 999, "Wajah belum terdaftar. Hubungi HRD untuk mendaftarkan wajah Anda."

    if not submitted_descriptor_json:
        return False, 999, "Gagal mendeteksi wajah dari kamera. Coba lagi."

    try:
        stored = json.loads(stored_descriptor_json)
        submitted = json.loads(submitted_descriptor_json)
    except (ValueError, TypeError):
        return False, 999, "Data wajah tidak valid."

    distance = face_distance(stored, submitted)

    if distance <= FACE_MATCH_THRESHOLD:
        return True, distance, "Wajah cocok."
    else:
        return False, distance, "Wajah tidak dikenali. Ini bukan akun Anda."