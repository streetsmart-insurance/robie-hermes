import sys
import json
import requests
from google.cloud import secretmanager

client = secretmanager.SecretManagerServiceClient()
name = 'projects/streetsmart-hermes-poc/secrets/carrier_voice_api_key/versions/latest'
response = client.access_secret_version(request={'name': name})
key = response.payload.data.decode('UTF-8').strip()

cid = sys.argv[1] if len(sys.argv) > 1 else 'f87261b9-c41b-40d4-97d3-798033c059b7'
headers = {'authorization': key}
r = requests.get(f'https://api.bland.ai/v1/calls/{cid}', headers=headers)
if r.status_code == 200:
    data = r.json()
    print('Call ID:', data.get('call_id'))
    print('Queue Status:', data.get('queue_status'))
    print('Status:', data.get('status'))
    print('Answered By:', data.get('answered_by'))
    print('Call Length (mins):', data.get('call_length'))
    print('Summary:', data.get('summary'))
    trans = data.get('transcripts', [])
    print(f'Transcripts Count: {len(trans)}')
    for t in trans:
        user = t.get('user')
        text = t.get('text')
        print(f'  [{user}]: {text}')
else:
    print('Error:', r.status_code, r.text)
