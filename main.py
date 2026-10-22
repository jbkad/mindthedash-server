"""Server-side adapter for RTT's next-generation location API."""
import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from time import monotonic
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware

load_dotenv(Path(__file__).with_name('.env'))
LONDON = ZoneInfo('Europe/London')


def object_value(value):
    if not isinstance(value, dict):
        raise ValueError('Expected an RTT object')
    return value


def timestamp(value, local_timezone=None):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError('Invalid RTT timestamp')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        if local_timezone is None:
            raise ValueError('RTT timestamp has no timezone')
        parsed = parsed.replace(tzinfo=local_timezone)
    return parsed


def normalize_board(payload):
    payload = object_value(payload)
    services = payload.get('services')
    if not isinstance(services, list):
        raise ValueError('RTT response has no services array')
    rows = []
    system = object_value(payload.get('systemStatus') or {})
    schedule_only = system.get('rttCore') == 'SCHEDULE_ONLY' or system.get('realtimeNetworkRail') == 'REALTIME_DATA_NONE'
    for service in services:
        service = object_value(service)
        metadata = object_value(service.get('scheduleMetadata') or {})
        temporal = object_value(service.get('temporalData') or {})
        departure = object_value(temporal.get('departure') or {})
        scheduled = timestamp(departure.get('scheduleAdvertised'), LONDON)
        # exclude arrivals, passing trains, non-passenger and non-train services
        if not scheduled or metadata.get('inPassengerService') is False:
            continue
        if metadata.get('modeType') not in (None, 'TRAIN'):
            continue
        if temporal.get('scheduledCallType') not in ('ADVERTISED_OPEN', 'ADVERTISED_PICK_UP'):
            continue
        if departure.get('realtimeActual'):
            continue
        cancelled = departure.get('isCancelled') is True or temporal.get('displayAs') == 'CANCELLED'
        if temporal.get('displayAs') in ('PASS', 'DIVERTED', 'TERMINATES') and not cancelled:
            continue
        forecast = None if schedule_only else timestamp(departure.get('realtimeForecast') or departure.get('realtimeEstimate'), LONDON)
        delay = round((forecast - scheduled).total_seconds() / 60) if forecast else None
        status = 'cancelled' if cancelled else 'delayed' if delay is not None and delay > 0 else 'on_time' if forecast else 'scheduled'
        platform = object_value(object_value(service.get('locationMetadata') or {}).get('platform') or {})
        platform_kind = next((kind for kind in ('actual', 'forecast', 'planned') if isinstance(platform.get(kind), str) and platform[kind].strip()), None)
        destinations = service.get('destination') or []
        if not isinstance(destinations, list):
            raise ValueError('Invalid RTT destinations')
        destinations = [object_value(object_value(item).get('location') or {}) for item in destinations]
        reasons = service.get('reasons') or []
        if not isinstance(reasons, list):
            raise ValueError('Invalid RTT reasons')
        def reason(kind):
            return next((r.get('longText') or r.get('shortText') for r in map(object_value, reasons) if r.get('type') == kind), None)
        rows.append({
            'service_id': metadata.get('uniqueIdentity'),
            'departure_at': scheduled.isoformat(),
            'source_departure_at': departure.get('scheduleAdvertised'),
            'source_expected_departure_at': departure.get('realtimeForecast') or departure.get('realtimeEstimate'),
            'expected_departure_at': forecast.isoformat() if forecast else None,
            'aimed_departure_time': scheduled.astimezone(LONDON).strftime('%H:%M'),
            'expected_departure_time': 'Cancelled' if cancelled else forecast.astimezone(LONDON).strftime('%H:%M') if forecast else None,
            'status': status,
            'delay_minutes': max(0, delay) if delay is not None else None,
            'platform': platform[platform_kind] if platform_kind else None,
            'platform_status': platform_kind or 'unknown',
            'platform_values': platform,
            'operator_name': object_value(metadata.get('operator') or {}).get('name'),
            'is_cancelled': cancelled,
            'delay_reason': reason('DELAY'),
            'cancel_reason': reason('CANCEL'),
            'destination_name': ' / '.join(d.get('description') or 'Unknown destination' for d in destinations) or 'Unknown destination',
        })
    rows.sort(key=lambda row: timestamp(row['departure_at']))
    return {'departures': {'all': rows}, 'query': payload.get('query'), 'system_status': system, 'source': 'Realtime Trains'}


class RTTClient:
    def __init__(self, client, access_token=None, refresh_token=None, version=None):
        self.client = client
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.version = version
        self.valid_until = None
        self.cache = OrderedDict()
        # coalesce identical requests, including concurrent browser requests.
        self.lock = asyncio.Lock()

    async def request_json(self, path, token, params=None):
        headers = {'Authorization': f'Bearer {token}'}
        if self.version:
            headers['Version'] = self.version
        try:
            response = await self.client.get(path, params=params, headers=headers)
        except httpx.RequestError:
            raise HTTPException(502, '502: Unable to reach Realtime Trains Data API.') from None
        if response.status_code == 429:
            retry = response.headers.get('Retry-After')
            raise HTTPException(429, 'Realtime Trains rate limit reached; please try again later', headers={'Retry-After': retry} if retry and retry.isdigit() else None)
        if response.status_code == 401:
            raise HTTPException(503, 'RTT rejected the credential. Check token expiry and use RTT_REFRESH_TOKEN for a refresh token.')
        if response.status_code == 403:
            raise HTTPException(503, 'The RTT token does not have permission for this endpoint or search. Check the account entitlements.')
        if response.status_code == 400:
            raise HTTPException(502, 'RTT rejected the search; check the configured API version and time-window entitlement')
        if response.status_code == 204:
            return None
        if response.status_code != 200:
            raise HTTPException(502, 'Realtime Trains returned an upstream error')
        try:
            return object_value(response.json())
        except (ValueError, TypeError):
            raise HTTPException(502, 'Realtime Trains returned an invalid response') from None

    async def token(self):
        if self.refresh_token:
            if not self.access_token or not self.valid_until or datetime.now(timezone.utc) >= self.valid_until - timedelta(seconds=30):
                data = await self.request_json('/api/get_access_token', self.refresh_token)
                try:
                    token = data['token']
                    valid_until = timestamp(data['validUntil'])
                    if not isinstance(token, str) or not token or not valid_until or valid_until <= datetime.now(timezone.utc):
                        raise ValueError('Invalid token')
                except (KeyError, TypeError, ValueError):
                    raise HTTPException(502, 'RTT returned an invalid access-token response') from None
                self.access_token, self.valid_until = token, valid_until
        if not self.access_token:
            raise HTTPException(503, 'Configure RTT_REFRESH_TOKEN on the server')
        return self.access_token

    async def board(self, station, destination, minutes):
        key = (station, destination, minutes)
        async with self.lock:
            cached = self.cache.get(key)
            if cached and monotonic() - cached[0] < 60:
                self.cache.move_to_end(key)
                return cached[1]
            now = datetime.now(LONDON)
            params = {'code': f'gb-nr:{station}', 'timeFrom': now.isoformat(), 'timeTo': (now + timedelta(minutes=minutes)).isoformat(), 'timeTolerance': 'true'}
            if destination:
                params['filterTo'] = f'gb-nr:{destination}'
            data = await self.request_json('/rtt/location', await self.token(), params)
            try:
                result = normalize_board(data) if data is not None else {'departures': {'all': []}, 'query': {'timeFrom': params['timeFrom'], 'timeTo': params['timeTo']}, 'system_status': None, 'source': 'Realtime Trains'}
            except (ValueError, TypeError, KeyError):
                raise HTTPException(502, 'Realtime Trains returned an invalid departure board') from None
            result['fetched_at'] = now.isoformat()
            self.cache[key] = (monotonic(), result)
            self.cache.move_to_end(key)
            while len(self.cache) > 128:
                self.cache.popitem(last=False)
            return result


@asynccontextmanager
async def lifespan(app):
    async with httpx.AsyncClient(base_url='https://data.rtt.io', timeout=15.0) as client:
        app.state.rtt = RTTClient(client, os.getenv('RTT_ACCESS_TOKEN'), os.getenv('RTT_REFRESH_TOKEN'), os.getenv('RTT_API_VERSION'))
        yield


app = FastAPI(lifespan=lifespan)

app.add_middleware(CORSMiddleware, allow_origins=['http://localhost:3000', 'http://127.0.0.1:3000', 'https://mindthedash.netlify.app'], allow_methods=['GET'], allow_headers=['*'])


@app.get('/')
async def get_station_board(request: Request, station: str = Query(..., pattern='^[A-Za-z]{3}$'), to: Optional[str] = Query(None, pattern='^[A-Za-z]{3}$'), numRows: int = Query(15, ge=1, le=15), windowMinutes: int = Query(1439, ge=1, le=1439)):
    result = await request.app.state.rtt.board(station.upper(), to.upper() if to else None, windowMinutes)
    rows = result['departures']['all']
    return {**result, 'departures': {'all': rows[:numRows]}, 'coverage': {'matching_departures': len(rows), 'returned_departures': min(len(rows), numRows), 'truncated': len(rows) > numRows}}


@app.get('/health')
async def health():
    return {'status': 'ok', 'provider': 'rtt', 'configured': bool(os.getenv('RTT_ACCESS_TOKEN') or os.getenv('RTT_REFRESH_TOKEN'))}
