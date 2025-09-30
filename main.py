from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import requests
import os
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)

@app.get('/departures')
def get_departures(station: str = 'KGX'):
    api_id = os.getenv('TRANSPORT_API_ID')
    api_key = os.getenv('TRANSPORT_API_KEY')

    if not api_id or not api_key:
        raise HTTPException(status_code=500, detail='Transport API credentials not set')

    url = f'https://transportapi.com/v3/uk/train/station_timetables/{station}.json'
    params = {
        'app_id': api_id,
        'app_key': api_key,
        'darwin': 'true',
        'train_status': 'passenger',
        'station_detail': 'destination,calling_at',
        'to_offset': 'PT06:00:00',
        'limit': '50'
    }

    try:
        response = requests.get(url, params=params)
        response.raise_for_status()
        return {
            'station': station,
            'departures': response.json().get('departures', {})
        }
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f'Error fetching data from TransportAPI: {e}')