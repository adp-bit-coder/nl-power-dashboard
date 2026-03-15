import os
from datetime import date, timedelta
from dotenv import load_dotenv
import requests

load_dotenv()

api_key = os.getenv("TENNET_API_KEY")
yesterday = date.today() - timedelta(days=1)

url = "https://api.tennet.eu/publications/v1/settlement-prices"
params = {
    "date_from": f"{yesterday.strftime('%d-%m-%Y')} 00:00:00",
    "date_to":   f"{yesterday.strftime('%d-%m-%Y')} 23:59:59",
}
headers = {
    "apikey": api_key,
    "Accept": "application/json",
}

print(f"Fetching: {url}")
print(f"Params: {params}\n")

response = requests.get(url, params=params, headers=headers, timeout=15)

print(f"Status: {response.status_code}")
print(f"Headers: {dict(response.headers)}\n")
print("Body:")
print(response.text)
