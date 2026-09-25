from django.conf import settings


def build_payload(booking):
    payload = {'id': booking.id}
    payload['ota_name'] = getattr(settings, 'CHANNEX_OC_OTA_NAME', 'DolceTech')
    return payload
