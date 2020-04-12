import json
import logging

import geoip2.database
import user_agents
from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from core.models import Service

from .models import Hit, Session

log = logging.getLogger(__name__)

_geoip2_city_reader = None
_geoip2_asn_reader = None


def _geoip2_lookup(ip):
    global _geoip2_city_reader, _geoip2_asn_reader  # TODO: is there a better way to do global Django vars? Is this thread safe?
    try:
        if settings.MAXMIND_CITY_DB == None or settings.MAXMIND_ASN_DB == None:
            return None
        if _geoip2_city_reader == None or _geoip2_asn_reader == None:
            _geoip2_city_reader = geoip2.database.Reader(settings.MAXMIND_CITY_DB)
            _geoip2_asn_reader = geoip2.database.Reader(settings.MAXMIND_ASN_DB)
        city_results = _geoip2_city_reader.city(ip)
        asn_results = _geoip2_asn_reader.asn(ip)
        return {
            "asn": asn_results.autonomous_system_organization,
            "country": city_results.country.iso_code,
            "longitude": city_results.location.longitude,
            "latitude": city_results.location.latitude,
            "time_zone": city_results.location.time_zone,
        }
    except geoip2.errors.AddressNotFoundError:
        return {}


@shared_task
def ingress_request(
    service_uuid, tracker, time, payload, ip, location, user_agent, identifier=""
):
    try:
        service = Service.objects.get(uuid=service_uuid, status=Service.ACTIVE)
        log.debug(f"Linked to service {service}")

        ip_data = _geoip2_lookup(ip)
        log.debug(f"Found geoip2 data")

        # Create or update session
        session = Session.objects.filter(
            service=service,
            last_seen__gt=timezone.now() - timezone.timedelta(minutes=10),
            ip=ip,
            user_agent=user_agent,
            identifier=identifier,
        ).first()
        if session is None:
            log.debug("Cannot link to existing session; creating a new one...")
            ua = user_agents.parse(user_agent)
            initial = True
            device_type = "OTHER"
            if ua.is_mobile:
                device_type = "PHONE"
            elif ua.is_tablet:
                device_type = "TABLET"
            elif ua.is_pc:
                device_type = "DESKTOP"
            elif ua.is_bot:
                device_type = "ROBOT"
            session = Session.objects.create(
                service=service,
                ip=ip,
                user_agent=user_agent,
                identifier=identifier,
                browser=ua.browser.family or "",
                device=ua.device.model or "",
                device_type=device_type,
                os=ua.os.family or "",
                asn=ip_data.get("asn", ""),
                country=ip_data.get("country", ""),
                longitude=ip_data.get("longitude"),
                latitude=ip_data.get("latitude"),
                time_zone=ip_data.get("time_zone", ""),
            )
        else:
            log.debug("Updating old session with new data...")
            initial = False
            # Update last seen time
            session.last_seen = timezone.now()
            session.save()

        # Create or update hit
        idempotency = payload.get("idempotency")
        idempotency_path = f"hit_idempotency_{idempotency}"
        hit = None
        if idempotency is not None:
            if cache.get(idempotency_path) is not None:
                cache.touch(idempotency_path, 10 * 60)
                hit = Hit.objects.filter(
                    pk=cache.get(idempotency_path), session=session
                ).first()
                if hit is not None:
                    # There is an existing hit with an identical idempotency key. That means
                    # this is a heartbeat.
                    log.debug("Hit is a heartbeat; updating old hit with new data...")
                    hit.heartbeats += 1
                    hit.last_seen = timezone.now()
                    hit.save()
        if hit is None:
            log.debug("Hit is a page load; creating new hit...")
            # There is no existing hit; create a new one
            hit = Hit.objects.create(
                session=session,
                initial=initial,
                tracker=tracker,
                location=location,
                referrer=payload.get("referrer", ""),
                load_time=payload.get("loadTime"),
            )
            # Set idempotency (if applicable)
            if idempotency is not None:
                cache.set(idempotency_path, hit.pk, timeout=10 * 60)
    except Exception as e:
        log.exception(e)
        raise e
