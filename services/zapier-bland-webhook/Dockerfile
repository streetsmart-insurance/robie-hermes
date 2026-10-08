FROM python:3.11-slim

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py *.json ./

# Cloud Run sets $PORT; gunicorn serves the Flask app.
CMD exec gunicorn --bind :$PORT --workers 2 --threads 4 --timeout 0 app:app
